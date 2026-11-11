#!/usr/bin/env python3
"""Inventory-scoped AIDP teardown, private local backup and secret scheduling.

Called by poc.py under its operation lock. Preview uses GET requests only.
Remote data exports are outside this local backup; --delete-data approves their
loss. No compartment-wide discovery or deletion is performed.
"""
import contextlib
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tarfile
import time
from urllib.parse import quote, urlencode
from uuid import uuid4

from aidp_workspace import Api, ApiError
from poc import ROOT, PocError, oci, output, private_json


def utc():
    return datetime.now(timezone.utc).isoformat()


def key(value):
    if not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9_.-]{1,255}', value):
        raise PocError('Invalid recorded resource key; reconcile the inventory.')
    return quote(value, safe='')


def load(path):
    if path.is_symlink():
        raise PocError('Cleanup inventories must be regular files.')
    return json.loads(path.read_text()) if path.is_file() else {}


@contextlib.contextmanager
def runtime_locks(folder):
    with contextlib.ExitStack() as stack:
        for path in (folder/'aidp-workspace.lock', folder/'rag'/'operation.lock'):
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            stream = stack.enter_context(path.open('a'))
            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise PocError('An AIDP/RAG operation is running. Retry cleanup after it finishes.') from None
        yield


def backup_local(cfg, config_path, *, root=None):
    """Capture source, config and deployment files once under the held locks."""
    root = Path(root or ROOT).resolve()
    deployment = Path(cfg['state_directory']).parent.resolve()
    destination = root/'.local'/'cleanup-backups'/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    destination.mkdir(parents=True, mode=0o700)
    archive = destination/'deployment-records.tgz'
    sources = [('config/'+Path(config_path).name, Path(config_path))]
    excluded = {'.terraform', '.venv', '__pycache__', '.git', 'providers'}
    for path in sorted(deployment.rglob('*')):
        if any(p in excluded for p in path.relative_to(deployment).parts):
            continue
        if path.is_symlink():
            if '.tfstate' in path.name or path.name.endswith('inventory.json'):
                raise PocError('State/inventory symlinks cannot be archived safely.')
            continue
        if path.is_file():
            sources.append(('deployment/'+str(path.relative_to(deployment)), path))
    manifest = load(root/'MANIFEST.json')
    for name in [*manifest.get('sha256', {}), 'MANIFEST.json']:
        path = root/name
        if root not in path.resolve().parents or path.is_symlink() or not path.is_file():
            raise PocError('Source backup contains a missing or unsafe path.')
        sources.append(('source/'+name, path))
    hashes = {}
    try:
        with archive.open('xb') as stream:
            os.chmod(archive, 0o600)
            with tarfile.open(fileobj=stream, mode='w:gz') as tar:
                for name, path in sources:
                    if path.is_symlink() or not path.is_file():
                        raise PocError('A backup input changed or is not a regular file.')
                    data = path.read_bytes()
                    item = tarfile.TarInfo(name)
                    item.size, item.mode, item.mtime = len(data), 0o600, int(time.time())
                    tar.addfile(item, io.BytesIO(data))
                    hashes[name] = hashlib.sha256(data).hexdigest()
        with tarfile.open(archive, 'r:gz') as tar:
            for item in tar:
                if not item.isfile() or hashlib.sha256(tar.extractfile(item).read()).hexdigest() != hashes[item.name]:
                    raise PocError('Automatic backup verification failed.')
        record = {'archive': str(archive), 'sha256': hashlib.sha256(archive.read_bytes()).hexdigest(),
                  'files': hashes, 'verified_utc': utc(), 'remote_data_exported': False}
        private_json(destination/'backup.json', record)
        print('Verified private local backup: '+str(archive), flush=True)
        return {k: record[k] for k in ('archive', 'sha256', 'verified_utc', 'remote_data_exported')}
    except Exception:
        print('Backup did not complete. No cleanup mutations have been authorized by this step.', flush=True)
        raise


class RuntimeCleanup:
    def __init__(self, cfg, *, api=None, additional_workspaces=(), wait_seconds=1800):
        self.cfg = cfg
        self.folder = Path(cfg['state_directory']).parent
        self.path = self.folder/'cleanup'/'journal.json'
        self.saved = load(self.path)
        self.baseline = load(self.folder/'aidp-workspace.json')
        self.rag = load(self.folder/'rag'/'inventory.json')
        self.aidp_id = output(cfg, 'aidp').get('aidp_id')
        self.target = {k: cfg[k] for k in ('name', 'tenancy_id', 'compartment_id', 'region')}
        self.additional = set(additional_workspaces)
        for value in self.additional:
            key(value)
        if self.saved and self.saved.get('target') != self.target:
            raise PocError('Cleanup journal belongs to another deployment target.')
        self.wait_seconds = wait_seconds
        self.pending = []
        self.api = api
        self.operations, self.blockers = [], []
        if not self.aidp_id:
            if (self.baseline or self.rag) and not self.saved.get('runtime_verified'):
                raise PocError('AIDP state/output is missing but runtime inventory exists. Reconcile before cleanup.')
            if self.additional and not self.saved.get('runtime_verified'):
                raise PocError('Additional workspaces require a recorded AIDP instance.')
            return
        if not self.baseline:
            raise PocError('AIDP exists without its workspace ownership inventory. Reconcile before automatic cleanup.')
        for inventory in (self.baseline, self.rag):
            if inventory and inventory.get('aidp_id') != self.aidp_id:
                raise PocError('AIDP/RAG inventory belongs to another AIDP instance.')
            pending = set(inventory.get('create_intents', {}))-set(inventory.get('resources', {}))
            if pending:
                raise PocError('Unresolved create intents must be reconciled before cleanup: '+', '.join(sorted(pending)))
        if self.saved.get('aidp_id') not in (None, self.aidp_id):
            raise PocError('Cleanup journal belongs to another AIDP instance.')
        self.api = api or Api(cfg)
        if self.api.aidp_id != self.aidp_id:
            raise PocError('Cleanup API target differs from the recorded AIDP instance.')
        self.workspace = self.baseline.get('resources', {}).get('workspace', {})
        if not self.workspace or self.workspace.get('name') != cfg['name']+'-private':
            raise PocError('The owned private workspace cannot be established.')
        self.prefix = '/workspaces/'+key(self.workspace['key'])
        if self.workspace['key'] in self.additional:
            raise PocError('The primary workspace is already owned; omit it from --additional-workspace.')

    def save(self):
        self.saved.update(target=self.target, aidp_id=self.aidp_id or self.saved.get('aidp_id'))
        private_json(self.path, self.saved)

    def request(self, method, path, body=None, **kwargs):
        try:
            return self.api.request(method, path, body, **kwargs)
        except ApiError as error:
            if error.status != 404:
                diagnostic = self.folder/'cleanup'/'last-api-error.json'
                # Never capture request bodies, authorization headers or credentials.
                private_json(diagnostic, {'method': method, 'path': path, 'http_status': error.status,
                    'code': error.code, 'request_id': error.request_id,
                    'service_message': error.service_message, 'recorded_utc': utc()})
                error.args = (str(error)+' during '+method+' '+path+
                              '. Private service error: '+str(diagnostic),)
            raise
        except subprocess.TimeoutExpired:
            raise PocError('AIDP cleanup request timed out. Keep the journal and rerun the same command to reconcile it.') from None

    def items(self, path):
        items, pages, page = [], set(), None
        while True:
            url = path + (('&' if '?' in path else '?')+urlencode({'page': page}) if page else '')
            data, headers = self.request('GET', url)
            if not isinstance(data, dict) or not isinstance(data.get('items'), list):
                raise PocError('Cleanup listing returned an unexpected response; absence cannot be established.')
            if any(not isinstance(item, dict) for item in data['items']):
                raise PocError('Cleanup listing contains invalid resource metadata.')
            items.extend(item for item in data['items']
                         if item.get('lifecycleState', item.get('state')) != 'DELETED')
            page = headers.get('opc-next-page')
            if not page:
                return items
            if page in pages:
                raise PocError('Cleanup listing repeated its pagination token.')
            pages.add(page)

    def get(self, path, collection, resource_key):
        try:
            item, headers = self.request('GET', path)
            if not isinstance(item, dict) or item.get('key') != resource_key:
                raise PocError('Cleanup GET returned invalid or mismatched resource metadata.')
            if item.get('lifecycleState', item.get('state')) == 'DELETED':
                return None, headers
            return item, headers
        except ApiError as error:
            if error.status != 404:
                raise
            # A 404 alone can hide permission failures. Require successful listing.
            if any(item.get('key') == resource_key for item in self.items(collection)):
                raise PocError('Resource GET returned 404 but its parent listing still contains it.') from None
            return None, {}

    def add(self, kind, path, collection, resource_key, name, **extra):
        op = dict(kind=kind, path=path, collection=collection, key=resource_key, name=name, **extra)
        self.operations.append(op)
        return op

    def preview(self):
        self.operations, self.blockers = [], []
        self.pending = []
        if not self.aidp_id:
            return self.report()
        for record in self.saved.get('operations', {}).values():
            if record.get('status') == 'REQUESTED' and record.get('async_operation_key') and not record.get('async_completed'):
                info = self.request('GET', '/asyncOperations/'+key(record['async_operation_key']))[0]
                if info.get('status') in ('FAILED', 'CANCELED'):
                    self.blockers.append('A previously requested cleanup operation '+info['status']+
                                         '; inspect the journal and service work request before continuing.')
                elif info.get('status') == 'IN_PROGRESS':
                    self.pending.append(record['async_operation_key'])
                elif info.get('status') != 'SUCCEEDED':
                    raise PocError('Unknown pending cleanup state; inspect its service request.')
        resources = self.baseline['resources']
        prefix = self.cfg['name'].replace('-', '_')
        workspaces = self.items('/workspaces')
        workspace_keys = {i['key'] for i in workspaces}
        approved = set(self.additional) | set(self.saved.get('approved_workspaces', []))
        missing_approval = approved-workspace_keys
        completed = self.saved.get('operations', {})
        for value in missing_approval:
            if completed.get('/workspaces/'+key(value), {}).get('status') != 'DONE':
                self.blockers.append('Additional workspace is absent or not visible: '+value)
        for item in workspaces:
            if item['key'] == self.workspace['key']:
                if item.get('displayName') != self.workspace['name']:
                    self.blockers.append('Owned workspace name changed; reconcile its identity.')
            elif item.get('type') == 'DEFAULT' and item.get('displayName') == self.cfg['name']+'-workspace':
                if item['key'] in approved:
                    self.blockers.append('The default workspace is managed with the AIDP instance; omit its explicit key.')
                continue  # Default workspace stays until Terraform removes the instance.
            elif item['key'] not in approved:
                self.blockers.append('Untracked workspace '+item.get('displayName', '?')+' ['+item['key']+
                                     ']; review it and explicitly select --additional-workspace '+item['key'])
            else:
                self.add('workspace', '/workspaces/'+key(item['key']), '/workspaces', item['key'],
                         item.get('displayName', item['key']), explicit_selection=True)
        extras = self.operations
        self.operations = []
        agents = []
        for inventory, expected in ((self.rag, prefix+'_rag_agent'), (self.baseline, prefix+'_agent')):
            record = inventory.get('resources', {}).get('agent')
            if record:
                key(record['key'])
                if record.get('name') not in (expected, prefix+'_identity_probe'):
                    raise PocError('Unexpected recorded agent name.')
                agents.append((inventory, record))
        if self.workspace['key'] in workspace_keys:
            listed = self.items(self.prefix+'/agents')
            owned_keys = {r['key'] for _, r in agents}
            if any(i['key'] not in owned_keys for i in listed):
                self.blockers.append('The private workspace contains unrecorded agents.')
            compute = resources.get('compute')
            owned_compute = {compute['key']} if compute else set()
            clusters = self.items(self.prefix+'/clusters?type=AI_COMPUTE') + self.items(self.prefix+'/clusters?type=USER')
            if any(i['key'] not in owned_compute for i in clusters):
                self.blockers.append('The private workspace contains unrecorded compute.')
            if self.items(self.prefix+'/jobs'):
                self.blockers.append('The private workspace contains unrecorded workflow jobs.')
            for inventory, record in agents:
                path = self.prefix+'/agents/'+key(record['key'])
                item, _ = self.get(path, self.prefix+'/agents', record['key'])
                if item is None:
                    continue
                if item.get('displayName') != record['name'] or item.get('type') != 'CODE':
                    raise PocError('Recorded agent identity/type changed.')
                if item.get('computeKey') not in (None, compute['key'] if compute else None):
                    self.blockers.append('An owned agent is attached to unrecorded compute.')
                # Include every live version under the exact owned agent, plus TEST.
                deployments = self.items(path+'/deployments')
                for dep in deployments:
                    if dep.get('agentKey') not in (None, record['key']):
                        raise PocError('Deployment belongs to another agent.')
                    self.add('deployment', path+'/deployments/'+key(dep['key']), path+'/deployments',
                             dep['key'], dep.get('displayName', dep['key']))
                self.add('agent', path, self.prefix+'/agents', record['key'], record['name'],
                         detach_compute=bool(item.get('computeKey')),
                         test_state=(item.get('deploymentState') or {}).get('TEST'))
        schema = self.rag.get('resources', {}).get('schema')
        volume = self.rag.get('resources', {}).get('volume')
        if schema:
            expected = 'default.'+prefix+'_rag'
            if schema.get('key') != expected or schema.get('name') != prefix+'_rag':
                raise PocError('RAG schema is outside the owned dedicated namespace.')
            if volume and volume.get('key') != expected+'.synthetic_documents':
                raise PocError('RAG volume is outside the owned dedicated schema.')
            kb = self.rag.get('native_knowledge_base', {})
            if kb and (kb.get('catalog') != 'default' or kb.get('schema') != prefix+'_rag'):
                raise PocError('Native knowledge base belongs to another schema.')
            if volume:
                vp = '/volumes/'+key(volume['key'])
                item, _ = self.get(vp, '/volumes', volume['key'])
                if item is not None:
                    if item.get('volumeType') != 'MANAGED' or item.get('schemaName') != prefix+'_rag':
                        raise PocError('Refusing to purge an external or mismatched RAG volume.')
                    files = self.items(vp+'/files?path=%2F')
                    for f in files:
                        name = (f.get('path') or '').lstrip('/')
                        if f.get('type') != 'FILE' or '/' in name or any(c in name for c in '\r\n\x00') or name not in self.rag.get('uploaded_documents', {}):
                            self.blockers.append('RAG volume contains an unrecorded file or folder: '+name)
                        else:
                            self.add('volume-file', vp+'/actions/deleteFile', vp+'/files?path=%2F',
                                     name, name, file_path='/'+name)
            item, _ = self.get('/schemas/'+key(expected), '/schemas?catalogKey=default', expected)
            if item is not None:
                if item.get('catalogName') != 'default' or item.get('displayName') != prefix+'_rag':
                    raise PocError('Live RAG schema identity changed.')
                self.add('schema', '/schemas/'+key(expected), '/schemas?catalogKey=default', expected,
                         schema['name'], cascade=True, volume_key=volume['key'] if volume else None,
                         contents='ALL objects in this dedicated schema, including KB, vectors and managed volume')
        elif self.rag:
            self.blockers.append('RAG inventory exists without its schema ownership record.')
        # Credential list responses are metadata only; never fetch credential contents.
        credentials = self.items('/credentials')
        owned_credentials = []
        for kind, suffix in (('synthetic_credential', '_credential_probe'), ('vault_credential', '_vllm_key')):
            record = self.baseline.get(kind)
            if record:
                if record.get('name') != prefix+suffix:
                    raise PocError('Unexpected recorded credential name.')
                owned_credentials.append(record['key'])
                match = next((i for i in credentials if i['key'] == record['key']), None)
                if match:
                    if match.get('displayName') != record['name']:
                        raise PocError('Credential identity changed.')
                    self.add('credential', '/credentials/'+key(record['key']), '/credentials',
                             record['key'], record['name'])
        if any(i['key'] not in owned_credentials for i in credentials):
            self.blockers.append('AIDP contains unrecorded credentials; reconcile before removing the instance.')
        if self.workspace['key'] in workspace_keys:
            compute = resources.get('compute')
            if compute:
                path = self.prefix+'/clusters/'+key(compute['key'])
                item, _ = self.get(path, self.prefix+'/clusters?type=AI_COMPUTE', compute['key'])
                if item is not None:
                    if item.get('type') != 'AI_COMPUTE' or item.get('displayName') != compute['name']:
                        raise PocError('Compute identity/type changed.')
                    self.add('compute', path, self.prefix+'/clusters?type=AI_COMPUTE', compute['key'], compute['name'])
            self.add('workspace', self.prefix, '/workspaces', self.workspace['key'], self.workspace['name'])
        self.operations.extend(extras)
        return self.report()

    def report(self):
        result = {'target': self.target, 'aidp_id': self.aidp_id,
                  'operations': self.operations, 'blockers': self.blockers,
                  'pending_async_operations': self.pending,
                  'remote_data_exported': False, 'generated_utc': utc()}
        private_json(self.folder/'cleanup'/'preview.json', result)
        print('AIDP/RAG cleanup preview:', flush=True)
        for op in self.operations:
            print('  '+op['kind']+': '+op['name']+' ['+op['key']+']'+
                  (' — CASCADE: '+op['contents'] if op.get('cascade') else '')+
                  (' — ALL workspace contents (explicit selection)' if op.get('explicit_selection') else ''), flush=True)
        for blocker in self.blockers:
            print('  BLOCKED: '+blocker, flush=True)
        print('Local backup: config, source, state and inventories. Remote data is not exported.', flush=True)
        return result

    def exists(self, op):
        if op['kind'] in ('credential', 'volume-file'):
            return any((i.get('key') if op['kind'] == 'credential' else (i.get('path') or '').lstrip('/')) == op['key']
                       for i in self.items(op['collection']))
        item, _ = self.get(op['path'], op['collection'], op['key'])
        return item is not None and item.get('lifecycleState', item.get('state')) not in ('DELETED',)

    def wait_absent(self, op, record):
        deadline, last = time.monotonic()+self.wait_seconds, 0
        while True:
            async_key = record.get('async_operation_key')
            if async_key:
                try:
                    info = self.request('GET', '/asyncOperations/'+key(async_key))[0]
                except ApiError as error:
                    if error.status == 404 and not self.exists(op):
                        return
                    raise
                status = info.get('status')
                if status in ('FAILED', 'CANCELED'):
                    raise PocError('Cleanup asynchronous operation '+status+' for '+op['kind']+'. Journal retained.')
                if status not in ('IN_PROGRESS', 'SUCCEEDED'):
                    raise PocError('Unknown cleanup asynchronous state; inspect the retained request ID.')
            else:
                status = 'SUCCEEDED'
            if status == 'SUCCEEDED' and not self.exists(op):
                return
            if time.monotonic() >= deadline:
                raise PocError('Cleanup still pending for '+op['kind']+'. Rerun the same command to resume.')
            if time.monotonic()-last >= 30:
                print('Waiting for cleanup: '+op['kind']+' '+op['name'], flush=True)
                last = time.monotonic()
            time.sleep(5)

    def execute(self):
        if self.blockers:
            raise PocError('Cleanup preview contains blockers. No automatic deletion started.')
        if not self.aidp_id:
            return
        self.saved['runtime_verified'] = False
        self.saved['approved_workspaces'] = sorted(self.additional | set(self.saved.get('approved_workspaces', [])))
        self.save()
        # A resource may disappear before its asynchronous cleanup finishes.
        # Reconcile pending requests even if discovery no longer lists it.
        for record in self.saved.get('operations', {}).values():
            if record.get('status') != 'REQUESTED' or not record.get('async_operation_key') or record.get('async_completed'):
                continue
            deadline = time.monotonic()+self.wait_seconds
            while True:
                info = self.request('GET', '/asyncOperations/'+key(record['async_operation_key']))[0]
                status = info.get('status')
                if status == 'SUCCEEDED':
                    record['async_completed'] = True
                    self.save()
                    break
                if status != 'IN_PROGRESS':
                    raise PocError('Pending cleanup operation did not succeed. Infrastructure was preserved.')
                if time.monotonic() >= deadline:
                    raise PocError('Previous cleanup operation is still running; rerun to resume.')
                time.sleep(5)
        for op in self.operations:
            records = self.saved.setdefault('operations', {})
            record = records.setdefault(op['path']+(':'+op['key'] if op['kind'] == 'volume-file' else ''), {})
            if record.get('status') == 'DONE' and not self.exists(op):
                continue
            if record.get('status') == 'REQUESTED' and record.get('async_operation_key'):
                self.wait_absent(op, record)
            elif not self.exists(op):
                record['status'] = 'DONE'
            else:
                headers = {}
                if op['kind'] not in ('credential', 'volume-file'):
                    current, current_headers = self.get(op['path'], op['collection'], op['key'])
                    if current is None:
                        record['status'] = 'DONE'
                        self.save()
                        continue
                    if current_headers.get('etag'):
                        headers['if-match'] = current_headers['etag']
                    if current.get('displayName') not in (None, op['name']):
                        raise PocError('Resource changed since preview. Regenerate and review the cleanup plan.')
                    if op['kind'] == 'agent' and (current.get('computeKey') or record.get('playground_detach')):
                        current, current_headers = self.detach_playground(op, current, record)
                        if current is None:
                            record['status'] = 'DONE'
                            self.save()
                            continue
                        headers = {'if-match': current_headers['etag']} if current_headers.get('etag') else {}
                if op.get('cascade'):
                    headers['is-forced'] = 'true'
                if op.get('file_path'):
                    headers['path'] = op['file_path']
                record.update(status='INTENT', updated_utc=utc())
                self.save()
                _, response = self.request('DELETE', op['path'], headers=headers)
                record.update(status='REQUESTED', async_operation_key=response.get('aidp-async-operation-key'),
                              request_id=response.get('opc-request-id'), updated_utc=utc())
                self.save()
                self.wait_absent(op, record)
            record.update(status='DONE', updated_utc=utc())
            self.save()
            print('Cleanup verified: '+op['kind']+' '+op['name'], flush=True)
        # Re-discover rather than trusting journal flags. A new object blocks teardown.
        report = self.preview()
        if report['blockers'] or report['operations'] or report['pending_async_operations']:
            raise PocError('Runtime cleanup is incomplete or resources changed. Infrastructure was preserved.')
        for record in self.saved.get('operations', {}).values():
            record['status'] = 'DONE'
        self.saved['runtime_verified'] = True
        self.saved['runtime_verified_utc'] = utc()
        self.save()

    def detach_playground(self, op, current, record):
        """Use the Console's detach action; a null computeKey update is ignored.

        This action currently exists on the Console's 20240831 data-plane API,
        but not the public 20260430 Agent client. Keep it scoped to the exact
        inventoried agent/compute and verify completion through the public API.
        """
        compute_key = self.baseline.get('resources', {}).get('compute', {}).get('key')
        detach = record.get('playground_detach', {})
        if current.get('computeKey') not in (None, compute_key):
            raise PocError('Agent compute changed after preview. Infrastructure was preserved.')
        if detach and detach.get('compute_key') != compute_key:
            raise PocError('Recorded Playground detach target changed; reconcile the journal.')
        if current.get('computeKey') and detach.get('status') not in ('REQUESTED', 'DONE'):
            detach = record.setdefault('playground_detach', {
                'compute_key': compute_key, 'retry_token': str(uuid4()), 'status': 'INTENT'})
            self.save()
            endpoint = (f'https://aidp.{self.cfg["region"]}.oci.oraclecloud.com/20240831/'
                        f'dataLakes/{key(self.aidp_id)}{self.prefix}/agentFlows/{key(op["key"])}/actions/detachCompute')
            print('Deselecting Playground AI Compute: '+op['name'], flush=True)
            _, response = self.request('POST', op['path']+'/actions/detachCompute',
                                      {'agentFlowComputeKey': compute_key}, endpoint=endpoint,
                                      token=detach['retry_token'])
            detach.update(status='REQUESTED', request_id=response.get('opc-request-id'), updated_utc=utc())
            self.save()
        deadline = time.monotonic()+self.wait_seconds
        while True:
            current, headers = self.get(op['path'], op['collection'], op['key'])
            if current and current.get('displayName') != op['name']:
                raise PocError('Agent identity changed while detaching. Infrastructure was preserved.')
            if current and current.get('computeKey') not in (None, compute_key):
                raise PocError('Agent compute changed while detaching. Infrastructure was preserved.')
            test_state = ((current or {}).get('deploymentState') or {}).get('TEST')
            if current is None or (not current.get('computeKey') and test_state in (None, 'INACTIVE', 'DELETED')):
                detach.update(status='DONE', updated_utc=utc())
                record['playground_detach'] = detach
                self.save()
                return current, headers
            if time.monotonic() >= deadline:
                raise PocError('Playground detach was requested but is not verified yet. '
                               'Infrastructure was preserved; rerun the same command to resume waiting.')
            time.sleep(5)


def schedule_secret(cfg, journal, *, apply=False):
    record_path = Path(cfg['state_directory']).parent/'runtime'/'inference-secret.json'
    record = load(record_path)
    if not record:
        if (record_path.parent/'secret-create-intent.json').exists():
            raise PocError('An interrupted inference-secret create must be reconciled before cleanup.')
        return None
    secret_id = record.get('id')
    if not isinstance(secret_id, str) or not secret_id.startswith('ocid1.vaultsecret.'):
        raise PocError('Inference secret inventory is invalid.')
    secret = oci(cfg, 'vault', 'secret', 'get', '--secret-id', secret_id)
    if secret.get('compartment-id') != cfg['compartment_id']:
        raise PocError('Recorded inference secret belongs to another compartment.')
    vault = output(cfg, 'aidp-foundation').get('vault_id') or record.get('vault_id')
    expected_name = cfg['name']+'-inference-api-key'
    if (not vault or secret.get('vault-id') != vault or record.get('vault_id') != vault or
            record.get('name') != expected_name or secret.get('secret-name') != expected_name):
        raise PocError('Inference secret identity/Vault differs from its owned inventory.')
    state = secret.get('lifecycle-state')
    if state not in ('ACTIVE', 'PENDING_DELETION', 'DELETED'):
        raise PocError('Inference secret cannot be scheduled while in state '+str(state))
    result = {'id': secret_id, 'state': state, 'time_of_deletion': secret.get('time-of-deletion')}
    if state == 'ACTIVE':
        deadline = (datetime.now(timezone.utc)+timedelta(days=8)).strftime('%Y-%m-%dT%H:%M:%SZ')
        result['proposed_time_of_deletion'] = deadline
        print('Inference secret: schedule deletion after runtime cleanup (8-day retention).', flush=True)
        if apply:
            if not journal.saved.get('runtime_verified'):
                raise PocError('Secret scheduling requires verified runtime cleanup.')
            journal.saved['secret_deletion_intent'] = result
            journal.save()
            oci(cfg, 'vault', 'secret', 'schedule-secret-deletion', '--secret-id', secret_id,
                '--time-of-deletion', deadline)
            secret = oci(cfg, 'vault', 'secret', 'get', '--secret-id', secret_id)
            if secret.get('lifecycle-state') != 'PENDING_DELETION':
                raise PocError('Inference-secret deletion is not yet confirmed; rerun to resume.')
            result.update(state='PENDING_DELETION', time_of_deletion=secret.get('time-of-deletion'))
    if apply:
        journal.saved['secret'] = result
        journal.save()
    return result


def automated_destroy(cfg, args, *, preview_infrastructure, apply_infrastructure):
    """Plan all phases first. Mutations require existing destructive gates too."""
    if args.apply and (not args.delete_data or args.confirm_name != cfg['name']):
        raise PocError('Automated apply requires --delete-data and a matching --confirm-name.')
    folder = Path(cfg['state_directory']).parent
    with runtime_locks(folder):
        cleanup = RuntimeCleanup(cfg, additional_workspaces=args.additional_workspace,
                                 wait_seconds=args.cleanup_wait_minutes*60)
        cleanup.preview()
        schedule_secret(cfg, cleanup)
        preview_infrastructure()
        if not args.apply:
            print('Full preview only. No backup, API deletion, secret scheduling or Terraform apply was run.', flush=True)
            return
        if cleanup.blockers:
            raise PocError('Resolve the full cleanup preview blockers before apply. No deletion started.')
        phrase = 'DELETE '+cfg['name']+' INCLUDING RUNTIME'
        if not args.yes and input('Type '+phrase+': ') != phrase:
            raise PocError('Cancelled. No deletion started.')
        # Mandatory verified controller backup before the first mutating API call.
        backup = backup_local(cfg, args.config)
        cleanup.saved.setdefault('backups', []).append(backup)
        cleanup.save()
        cleanup.execute()
        if not cleanup.aidp_id:
            cleanup.saved['runtime_verified'] = True
            cleanup.save()
        schedule_secret(cfg, cleanup, apply=True)
        apply_infrastructure()
        cleanup.saved.update(infrastructure_completed=True, infrastructure_completed_utc=utc())
        cleanup.save()
        print('Automated cleanup completed. Check pending Vault retention and retained service backups separately.', flush=True)

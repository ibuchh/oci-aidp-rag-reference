#!/usr/bin/env python3
"""Own separate synthetic RAG artifacts; reuse the existing AIDP workspace/compute.

Native KB creation/ingestion uses the AIDP UI where the current public SDK lacks
knowledge-base client operations. This helper uses documented public operations
for schema, managed volume and isolated coded-agent lifecycle.
"""
import argparse
import fcntl
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid
from urllib.parse import quote, urlencode

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from aidp_workspace import Api
from poc import read_config, target_check, private_json, PocError
from private_transport import validate_correlation_id

VOLUME = 'synthetic_documents'


class PendingCreate(PocError):
    """An accepted owned create is awaiting discovery, not another POST."""


class RagPoc:
    def __init__(self, config):
        self.cfg = read_config(config)
        target_check(self.cfg)
        self.api = Api(self.cfg)
        self.folder = Path(self.cfg['state_directory']).parent / 'rag'
        self.folder.mkdir(exist_ok=True, mode=0o700)
        self.inventory_path = self.folder / 'inventory.json'
        self.saved = json.loads(self.inventory_path.read_text()) if self.inventory_path.exists() else {
            'aidp_id': self.api.aidp_id, 'resources': {}, 'tokens': {}}
        if self.saved['aidp_id'] != self.api.aidp_id:
            raise PocError('RAG inventory belongs to another AIDP instance.')
        self.baseline = json.loads((self.folder.parent / 'aidp-workspace.json').read_text())
        if self.baseline['aidp_id'] != self.api.aidp_id:
            raise PocError('Baseline workspace belongs to another AIDP instance.')
        self.workspace = self.baseline['resources']['workspace']['key']
        self.compute = self.baseline['resources']['compute']['key']
        self.prefix = '/workspaces/' + quote(self.workspace, safe='')
        self.schema = self.cfg['name'].replace('-', '_') + '_rag'
        self.agent_name = self.cfg['name'].replace('-', '_') + '_rag_agent'

    def save(self):
        private_json(self.inventory_path, self.saved)

    def pending_create_status(self, kind):
        accepted = self.saved.get('create_responses', {}).get(kind)
        intent = self.saved.get('create_intents', {}).get(kind, {})
        details = {'displayName': intent.get('body', {}).get('displayName'),
                   'create_status': 'ACCEPTED' if accepted else 'UNCONFIRMED'}
        operation = (accepted or {}).get('headers', {}).get('aidp-async-operation-key')
        if operation:
            info = self.api.get('/asyncOperations/' + quote(operation, safe=''))
            details.update(async_operation_key=operation, operation_status=info.get('status'),
                           resource_name=info.get('resourceName'), error_code=info.get('errorCode'))
        return details

    def pending_create(self, kind):
        details = self.pending_create_status(kind)
        status = details.get('operation_status', 'ACCEPTED')
        if status in ('FAILED', 'CANCELED'):
            raise PocError(f'Owned {kind} create operation {status}; inspect operation '
                           f'{details["async_operation_key"]}. Inventory retained; no replacement create submitted.')
        operation = details.get('async_operation_key', 'not returned')
        raise PendingCreate(f'Owned {kind} create is still pending discovery (operation {operation}, status {status}). '
                            'Rerun the same prepare command after readiness; no second create was submitted.')

    def create(self, kind, path, body, collision_path):
        found = self.saved['resources'].get(kind)
        if found:
            return self.api.get(found['resource_path'])
        matches = [item for item in self.api.items(collision_path)
                   if item.get('displayName') == body['displayName']]
        intent = self.saved.get('create_intents', {}).get(kind)
        accepted = self.saved.get('create_responses', {}).get(kind)
        if accepted and intent != {'path': path, 'body': body}:
            raise PocError('Accepted RAG create intent differs from this request; inspect the retained inventory.')
        if matches:
            if len(matches) != 1 or not accepted or intent != {'path': path, 'body': body}:
                raise PocError('Name already exists without RAG ownership: ' + body['displayName'])
            item = matches[0]
            self.saved['resources'][kind] = {'key': item['key'], 'name': body['displayName'],
                'resource_path': (self.saved['resources']['agent']['resource_path']+'/deployments/'+quote(item['key'],safe='')
                                  if kind=='deployment' else path+'/'+quote(item['key'],safe=''))}
            operation = accepted.get('headers', {}).get('aidp-async-operation-key')
            if operation:
                self.saved['resources'][kind]['async_operation_key'] = operation
            self.save()
            return item
        if accepted:
            self.pending_create(kind)
        token = self.saved['tokens'].setdefault(kind, str(uuid.uuid4()))
        self.saved.setdefault('create_intents', {})[kind] = {'path':path, 'body':body}
        self.save()
        item, headers = self.api.request('POST', path, body, token)
        self.saved.setdefault('create_responses', {})[kind] = {
            'data': item, 'headers': {k:v for k,v in headers.items() if k in
                ['location','aidp-async-operation-key','opc-request-id','schema-key','volume-key']}}
        self.save()
        if not item.get('key'):
            matches = [i for i in self.api.items(collision_path) if i.get('displayName') == body['displayName']]
            if not matches:
                self.pending_create(kind)
            if len(matches) != 1:
                raise PocError('Accepted RAG create matches multiple resources; inspect the retained intent before continuing.')
            item = matches[0]
        key = item['key']
        resource_path = (path + '/' + quote(key, safe='') if kind != 'deployment' else
                         self.saved['resources']['agent']['resource_path'] + '/deployments/' + quote(key, safe=''))
        self.saved['resources'][kind] = {'key': key, 'name': body['displayName'], 'resource_path': resource_path}
        if headers.get('aidp-async-operation-key'):
            self.saved['resources'][kind]['async_operation_key'] = headers['aidp-async-operation-key']
        self.save()
        return item

    def prepare(self):
        try:
            self.prepare_resources()
        except PendingCreate as error:
            print(str(error), flush=True)

    def prepare_resources(self):
        schema = self.create('schema', '/schemas', {
            'displayName': self.schema, 'catalogName': 'default',
            'description': 'Synthetic vLLM/AIDP RAG POC; owned by integration/rag'},
            '/schemas?catalogKey=default')
        if schema.get('lifecycleState') != 'ACTIVE':
            print('Schema state:', schema.get('lifecycleState')); return
        volume = self.create('volume', '/volumes', {
            'displayName': VOLUME, 'catalogName': 'default', 'schemaName': self.schema,
            'volumeType': 'MANAGED', 'description': 'Only the bundled synthetic Aster TXT documents'},
            '/volumes?' + urlencode({'catalogKey': 'default', 'schemaKey': schema['key']}))
        print(json.dumps({'schema': {k: schema.get(k) for k in ['key','displayName','lifecycleState']},
                          'volume': {k: volume.get(k) for k in ['key','displayName','lifecycleState','storageLocation','volumeType']}}))
        self.saved['documents'] = {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                   for p in sorted((HERE / 'documents').glob('*.txt'))}
        self.saved['knowledge_base_plan'] = {'catalog': 'default', 'schema': self.schema,
            'name': 'aster_knowledge_base', 'embedding_model': 'ALL_MINILM_L12_V2',
            'chunk_size_characters': 1000, 'chunk_overlap_characters': 150,
            'top_k': 3, 'source_volume': VOLUME}
        self.save()

    def upload(self, source):
        if source.resolve() != (self.folder/'rag_agent.generated.py').resolve():
            raise PocError('Upload only the rendered entry in this deployment RAG directory.')
        if not self.saved.get('native_knowledge_base'):
            raise PocError('Record the created native knowledge base first.')
        self.create('agent', self.prefix + '/agents', {
            'displayName': self.agent_name, 'description': 'Synthetic document RAG with private vLLM generation',
            'pathInfo': '/Workspace/Shared', 'type': 'CODE', 'computeKey': self.compute}, self.prefix + '/agents')
        folder = '/Workspace/Shared/poc_rag_' + self.saved['resources']['agent']['key'].replace('-', '_')
        objects = self.prefix + '/objects'
        if not any(i['path'] == folder for i in self.api.items(objects + '?' + urlencode({'path':'/Workspace/Shared'}))):
            self.api.request('POST', objects, '', headers={'path':folder,'type':'FOLDER'}, raw=True)
        files = {source.name: source.read_text(), 'requirements.txt':'# Use platform-provided libraries.\n'}
        for name, text in files.items():
            self.api.request('POST', objects, text, headers={'path':folder+'/'+name,'type':'FILE','is-overwrite':'true'}, raw=True)
        self.api.request('PUT', self.saved['resources']['agent']['resource_path'], {
            'computeKey': self.compute, 'entryFilePath': folder+'/'+source.name, 'dependenciesFilePath':folder+'/requirements.txt'})
        self.saved['entry_file'] = folder+'/'+source.name
        self.save()
        print('Uploaded separate RAG agent entry:', source.name)

    def publish(self):
        agent = self.saved['resources']['agent']
        path = agent['resource_path']
        if self.saved['resources'].get('deployment'):
            item = self.api.get(self.saved['resources']['deployment']['resource_path'])
            if item.get('lifecycleState') != 'ACTIVE':
                raise PocError('Wait until the current RAG deployment is ACTIVE before republishing.')
            data, headers = self.api.request('POST', path + '/deployments/actions/redeploy',
                {'agentKey': agent['key'], 'agentComputeKey': self.compute})
            if data.get('key') and data['key'] != item['key']:
                self.saved.setdefault('previous_deployments', []).append(dict(self.saved['resources']['deployment']))
                self.saved['resources']['deployment'].update(key=data['key'],
                    resource_path=path+'/deployments/'+quote(data['key'],safe=''))
                self.save()
            print('Redeployment requested:', data.get('lifecycleState'))
            return
        item = self.create('deployment', path + '/deployments/actions/deploy', {
            'displayName': self.agent_name+'_endpoint', 'agentKey': agent['key'], 'agentComputeKey': self.compute},
            path + '/deployments')
        print('Deployment state:', item.get('lifecycleState'))

    def invoke(self, query, *, emit=True, correlation_id=None, trace=False):
        deployment = self.api.get(self.saved['resources']['deployment']['resource_path'])
        if deployment.get('lifecycleState') != 'ACTIVE':
            raise PocError('RAG endpoint is not ACTIVE.')
        correlation_id=validate_correlation_id(correlation_id or str(uuid.uuid4()))
        session=str(uuid.uuid4())
        directory=self.folder/'requests';directory.mkdir(exist_ok=True,mode=0o700)
        record=directory/(correlation_id+'.json')
        if record.exists():raise PocError('Correlation ID was already used; select a new ID for each request.')
        metadata={'correlation_id':correlation_id,'session_key':session,'aidp_trace_requested':bool(trace),
            'started_utc':datetime.now(timezone.utc).isoformat(),'status':'SUBMITTED'}
        private_json(record,metadata)
        try:
            data, headers = self.api.request('POST', '', {'isStreamEnabled':False, 'sessionKey':session,
                'trace':bool(trace), 'input':[{'role':'User','content':[{'type':'INPUT_TEXT',
                    'text':'__RAG_REQUEST_ID__:'+correlation_id+'\n'+query}]}]},
                endpoint=deployment['endpointUrl'],headers={'opc-request-id':correlation_id})
        except Exception as error:
            metadata.update(status='ERROR_OR_UNKNOWN',error_type=type(error).__name__)
            private_json(record,metadata)
            raise
        metadata.update(status='RESPONSE_RECEIVED',ended_utc=datetime.now(timezone.utc).isoformat(),
            aidp_gateway_request_id=headers.get('opc-request-id'),response_id=data.get('id'),
            aidp_response_stored=data.get('isStoreEnabled'))
        private_json(record,metadata)
        private_json(self.folder/'last-request.json',metadata)
        private_json(self.folder / 'last-response.json', data)
        if emit:print(json.dumps(data))
        return data

    def status(self):
        for kind, record in self.saved['resources'].items():
            item = self.api.get(record['resource_path'])
            print(kind, json.dumps({k:item.get(k) for k in ['key','displayName','lifecycleState','state','endpointUrl']}))
        pending = set(self.saved.get('create_intents', {})) - set(self.saved['resources'])
        for kind in sorted(pending):
            print(kind, json.dumps(self.pending_create_status(kind)), flush=True)
        if not self.saved['resources'] and not pending:
            print('No RAG resources or create intents recorded. Run prepare --apply to begin.', flush=True)

    def wait(self, timeout=600):
        deadline=time.monotonic()+timeout
        while time.monotonic()<deadline:
            item=self.api.get(self.saved['resources']['deployment']['resource_path'])
            state=item.get('lifecycleState')
            print('RAG endpoint:',state,flush=True)
            if state=='ACTIVE':return item
            if state in {'FAILED','DELETED','DELETING','INACTIVE'}:
                raise PocError('RAG endpoint did not become ACTIVE; inspect its operation in AIDP.')
            time.sleep(10)
        raise PocError('RAG endpoint readiness timed out; preserve ownership inventory and inspect status.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['prepare','record-kb','upload','publish','invoke','status','wait'])
    parser.add_argument('--config', type=Path, default=ROOT/'poc-config.json')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--source', type=Path, default=None)
    parser.add_argument('--kb-name', default='aster_knowledge_base')
    parser.add_argument('--query', default='Report the installed RAG utility interface.')
    parser.add_argument('--correlation-id',help='Optional unique ID; defaults to a UUID.')
    parser.add_argument('--trace',action='store_true',help='Opt in to AIDP session tracing for this invocation.')
    args = parser.parse_args()
    if args.action in {'prepare','record-kb','upload','publish'} and not args.apply:
        parser.error('Cloud mutations require --apply.')
    poc = RagPoc(args.config)
    with (poc.folder/'operation.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise PocError('Another RAG operation is running; retry after it finishes.')
        if args.action=='prepare':poc.prepare()
        elif args.action=='record-kb':
            if not args.kb_name.replace('_','').isalnum():raise PocError('Invalid knowledge base name.')
            poc.saved['native_knowledge_base']={'catalog':'default','schema':poc.schema,'name':args.kb_name}
            poc.save()
            print('Recorded UI-created KB identity. Run live acceptance to verify retrieval.')
        elif args.action=='upload':poc.upload(args.source or poc.folder/'rag_agent.generated.py')
        elif args.action=='publish':poc.publish()
        elif args.action=='invoke':poc.invoke(args.query,correlation_id=args.correlation_id,trace=args.trace)
        elif args.action=='wait':poc.wait()
        else:poc.status()


if __name__=='__main__':
    try:main()
    except (PocError,KeyError,OSError) as error:raise SystemExit(str(error))

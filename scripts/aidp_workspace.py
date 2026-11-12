#!/usr/bin/env python3
"""AIDP data-plane setup using signed OCI requests; no browser credentials.

Uses Oracle's 20260430 public REST API. The regional gateway follows the
official oracle/aidataplatform-sdk Python CLI endpoint resolver.
"""
import argparse
import fcntl
import importlib.util
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from urllib.parse import quote, urlencode, urlsplit

from poc import ROOT, PocError, output, private_json, read_config, target_check


class ApiError(PocError):
    def __init__(self, status, code, request_id, service_message=None):
        self.status = status
        self.code = code
        self.request_id = request_id
        self.service_message = service_message
        super().__init__(f'AIDP API returned HTTP {status}, code={code}, request-id={request_id}')


class Api:
    def __init__(self, cfg):
        self.cfg = cfg
        self.aidp_id = output(cfg, 'aidp')['aidp_id']
        self.base = (f'https://aidp.{cfg["region"]}.oci.oraclecloud.com/20260430/'
                     f'aiDataPlatforms/{quote(self.aidp_id, safe="")}')

    def request(self, method, path, body=None, token=None, endpoint=None, headers=None, raw=False):
        uri = self.base+path
        if endpoint:
            parsed = urlsplit(endpoint)
            if (parsed.scheme != 'https' or not parsed.hostname or
                    not parsed.hostname.endswith('.oci.oraclecloud.com') or
                    parsed.username or parsed.password or parsed.port not in (None, 443) or
                    parsed.query or parsed.fragment):
                raise PocError('Refusing to sign an unexpected agent endpoint URL.')
            uri = endpoint
        cmd = ['oci', '--profile', self.cfg['oci_profile'], '--region', self.cfg['region'],
               '--connection-timeout', '10', '--read-timeout', '180' if endpoint else '60', '--no-retry',
               'raw-request', '--http-method', method, '--target-uri', uri]
        # Bodies contain source code and non-secret configuration only. Temp files
        # avoid placing source text or any future values on the command line.
        with tempfile.TemporaryDirectory(prefix='aidp-request-', dir=ROOT/'.local') as tmp:
            if body is not None:
                p = Path(tmp)/'body.json'
                if raw:
                    p.write_text(body)
                    p.chmod(0o600)
                else:
                    private_json(p, body)
                cmd += ['--request-body', 'file://'+str(p)]
            request_headers = dict(headers or {})
            if token:
                request_headers['opc-retry-token'] = token
            if request_headers:
                cmd += ['--request-headers', json.dumps(request_headers)]
            if raw:
                # OCI raw-request accepts JSON bodies only. Use the existing
                # OCI CLI Python/SDK for the documented binary file operation.
                cli = shutil.which('oci')
                with Path(cli).open() as entry:
                    first = entry.readline(1024)
                interpreter = shlex.split(first.removeprefix('#!').strip())
                if (len(interpreter) != 1 or not Path(interpreter[0]).is_absolute() or
                        'python' not in Path(interpreter[0]).name.lower()):
                    raise PocError('Cannot locate OCI CLI Python runtime for binary uploads. Use a Python-based OCI CLI installation.')
                hp = Path(tmp)/'headers.json'
                private_json(hp, request_headers)
                cmd = [interpreter[0], str(ROOT/'scripts/aidp_binary_request.py'),
                       '--profile', self.cfg['oci_profile'], '--region', self.cfg['region'],
                       '--uri', uri, '--headers', str(hp), '--body', str(p)]
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=210 if endpoint else 90)
        if result.returncode:
            raise PocError('Signed AIDP request failed. Check OCI authentication/network; response body withheld.')
        envelope = json.loads(result.stdout)
        status = int(str(envelope['status']).split()[0])
        headers = {k.lower(): v for k, v in envelope.get('headers', {}).items()}
        data = envelope.get('data') or {}
        if not 200 <= status < 300:
            raise ApiError(status, data.get('code', 'unknown'), headers.get('opc-request-id', 'unavailable'),
                           service_message=data.get('message'))
        return data, headers

    def get(self, path):
        return self.request('GET', path)[0]

    def items(self, path):
        items, page = [], None
        while True:
            url = path + (('?' if '?' not in path else '&') + urlencode({'page': page}) if page else '')
            data, headers = self.request('GET', url)
            items.extend(data.get('items') or [])
            page = headers.get('opc-next-page')
            if not page:
                return items


class Workspace:
    def __init__(self, cfg, api=None):
        self.cfg, self.api = cfg, api or Api(cfg)
        self.root = ROOT/'deployments'/cfg['name']
        self.path = self.root/'aidp-workspace.json'
        self.saved = json.loads(self.path.read_text()) if self.path.exists() else {
            'aidp_id': self.api.aidp_id, 'resources': {}, 'tokens': {}}
        if self.saved['aidp_id'] != self.api.aidp_id:
            raise PocError('Workspace inventory belongs to another AIDP instance.')
        self.name = cfg['name']+'-private'

    def save(self):
        private_json(self.path, self.saved)

    def create(self, kind, path, body):
        # Persist intent before POST. Retrying a timed-out create uses the same
        # service retry token. Name collisions without local ownership fail closed.
        token = self.saved['tokens'].setdefault(kind, str(uuid.uuid4()))
        self.save()
        item, headers = self.api.request('POST', path, body, token)
        self.saved['resources'][kind] = {'key': item['key'], 'name': body['displayName']}
        if headers.get('aidp-async-operation-key'):
            self.saved['resources'][kind]['async_operation_key'] = headers['aidp-async-operation-key']
        self.save()
        return item

    def workspace(self, apply=False):
        found = self.saved['resources'].get('workspace')
        foundation = output(self.cfg, 'aidp-foundation')
        expected = {'subnetId': foundation['workspace_subnet_id'],
                    'nsgIds': [foundation['workspace_nsg_id']]}
        if found:
            item = self.api.get('/workspaces/'+quote(found['key'], safe=''))
            network = item.get('networkConfigurationDetails') or {}
            if not item.get('isPrivateNetworkEnabled') or any(network.get(k) != v for k, v in expected.items()):
                raise PocError('Owned workspace network differs from the prepared private subnet/NSG.')
            return item
        if any(x['displayName'] == self.name for x in self.api.items('/workspaces')):
            raise PocError('Workspace name already exists without ownership inventory. Review it before adoption.')
        body = {'displayName': self.name, 'description': 'Private vLLM POC; managed by aidp_workspace.py',
                'networkConfigurationDetails': expected}
        if not apply:
            return {'planned': body}
        return self.create('workspace', '/workspaces', body)

    def apply(self):
        workspace = self.workspace(apply=True)
        if workspace.get('lifecycleState') != 'ACTIVE':
            if workspace.get('lifecycleState') == 'FAILED':
                raise PocError('Workspace FAILED. Inspect AIDP async operation before retrying.')
            print(f'Workspace state: {workspace.get("lifecycleState")} '
                  f'({workspace.get("lifecycleDetails")}). Rerun apply once ACTIVE.'); return
        prefix = '/workspaces/'+quote(workspace['key'], safe='')
        owned = self.saved['resources'].get('compute')
        if not owned:
            name = self.cfg['name'].replace('-', '_')+'_ai_compute'
            if any(x['displayName'] == name for x in self.api.items(prefix+'/clusters')):
                raise PocError('AI Compute name collision without ownership inventory.')
            compute = self.create('compute', prefix+'/clusters', {
                'type': 'AI_COMPUTE', 'displayName': name,
                'driverConfig': {'driverShapeConfig': {'ocpus': 1, 'memoryInGBs': 16}},
                'replicaConfig': {'minReplica': 1, 'maxReplica': 1}})
        else:
            compute = self.api.get(prefix+'/clusters/'+quote(owned['key'], safe=''))
        if compute.get('type') != 'AI_COMPUTE':
            raise PocError('Recorded compute is not AI Compute.')
        if compute.get('state') != 'ACTIVE':
            if compute.get('state') == 'FAILED':
                raise PocError('AI Compute FAILED. Inspect status/async operation; no duplicate will be created.')
            print(f'AI Compute state: {compute.get("state")}. Rerun apply when ACTIVE.'); return
        if 'agent' not in self.saved['resources']:
            name = self.cfg['name'].replace('-', '_')+'_identity_probe'
            if any(x['displayName'] == name for x in self.api.items(prefix+'/agents')):
                raise PocError('Agent name collision without ownership inventory.')
            agent = self.create('agent', prefix+'/agents', {
                'displayName': name, 'description': 'Metadata-only runtime identity probe',
                'pathInfo': '/Workspace/Shared', 'type': 'CODE', 'computeKey': compute['key']})
        else:
            agent = self.api.get(prefix+'/agents/'+quote(self.saved['resources']['agent']['key'], safe=''))
        # Creation supplies the actual project directory. Do not invent one or
        # overwrite an unrelated existing workspace file tree.
        self.saved['agent_metadata'] = {k: agent.get(k) for k in
                                       ('key', 'pathInfo', 'entryFilePath', 'dependenciesFilePath', 'computeKey', 'lifecycleState')}
        self.save()
        print('AI Compute is ACTIVE; coded agent created. Inspect status before uploading source.')
        return True

    def repair_agent_path(self, apply=False):
        workspace = self.workspace()
        resources = self.saved['resources']
        prefix = '/workspaces/' + quote(workspace['key'], safe='')
        agent_path = prefix + '/agents/' + quote(resources['agent']['key'], safe='')
        agent = self.api.get(agent_path)
        if (agent.get('key') != resources['agent']['key'] or agent.get('type') != 'CODE' or
                agent.get('computeKey') != resources['compute']['key']):
            raise PocError('Agent identity/type/compute differs from the owned baseline; no path repair attempted.')
        if agent.get('pathInfo') != 'agents':
            self.api.items(prefix + '/objects?' + urlencode({'path': agent['pathInfo']}))
            print('Agent workspace path resolves; no legacy path repair needed.', flush=True)
            return
        canonical = '/Workspace/agents'
        objects = prefix + '/objects'
        matches = [item for item in self.api.items(objects + '?' + urlencode({'path':'/Workspace'}))
                   if item.get('path') == canonical]
        if matches:
            if len(matches) != 1 or matches[0].get('type') != 'FOLDER':
                raise PocError('Legacy agent path collides with a non-folder or ambiguous workspace object.')
            self.api.items(objects + '?' + urlencode({'path':'agents'}))
            print('Legacy agent workspace folder already resolves; no object changed.', flush=True)
            return
        record = self.saved.get('agent_ui_path')
        identity = {'agent_key': agent['key'], 'workspace_key': workspace['key'], 'canonical_path':canonical}
        if record and any(record.get(k) != value for k,value in identity.items()):
            raise PocError('Agent path repair record belongs to another resource; inspect inventory.')
        if not apply:
            print('Preview: create the missing empty folder /Workspace/agents in the owned workspace. '
                  'Agent, source, compute and deployed endpoint remain unchanged. Use --apply.', flush=True)
            return
        observed = {k: agent.get(k) for k in ('key','pathInfo','entryFilePath','dependenciesFilePath',
                                            'computeKey','deploymentState')}
        record = record or dict(identity, token=str(uuid.uuid4()), original_agent=observed)
        record['status'] = 'REQUESTED'
        self.saved['agent_ui_path'] = record
        self.save()
        self.api.request('POST', objects, '', record['token'],
                         headers={'path':canonical, 'type':'FOLDER', 'is-overwrite':'false'}, raw=True)
        self.api.items(objects + '?' + urlencode({'path':'agents'}))
        current = self.api.get(agent_path)
        if observed != {k:current.get(k) for k in observed}:
            raise PocError('Agent metadata changed during folder repair; inspect the retained repair record.')
        record['status'] = 'VERIFIED'
        self.save()
        print('Agent workspace folder restored and verified; refresh the baseline agent UI. '
              'Agent, source, compute and deployed endpoint are unchanged.', flush=True)

    def status(self):
        result = {}
        for kind, record in self.saved['resources'].items():
            w = self.saved['resources']['workspace']['key']
            if kind == 'deployment':
                agent = self.saved['resources']['agent']['key']
                suffix = '/agents/'+quote(agent, safe='')+'/deployments/'
            else:
                suffix = {'workspace': '', 'compute': '/clusters/', 'agent': '/agents/'}[kind]
            path = '/workspaces/'+quote(w, safe='') + (suffix+quote(record['key'], safe='') if suffix else '')
            item = self.api.get(path)
            result[kind] = {k: item.get(k) for k in ('key', 'displayName', 'type', 'lifecycleState', 'state',
                                                       'isPrivateNetworkEnabled', 'networkConfigurationDetails',
                                                       'driverConfig', 'replicaConfig', 'computeKey', 'pathInfo',
                                                       'entryFilePath', 'dependenciesFilePath', 'stateDetails', 'lifecycleDetails',
                                                       'endpointUrl')}
        print(json.dumps(result, indent=2))

    def upload_probe(self):
        workspace = self.workspace()
        if workspace.get('lifecycleState') != 'ACTIVE':
            raise PocError('Private workspace is not ACTIVE.')
        resources = self.saved['resources']
        if not all(k in resources for k in ('compute', 'agent')):
            raise PocError('Run apply until the compute and coded agent have been created.')
        prefix = '/workspaces/'+quote(workspace['key'], safe='')
        agent_path = prefix+'/agents/'+quote(resources['agent']['key'], safe='')
        agent = self.api.get(agent_path)
        if agent.get('type') != 'CODE' or agent.get('computeKey') != resources['compute']['key']:
            raise PocError('Agent type or compute attachment differs from the owned setup.')
        compute = self.api.get(prefix+'/clusters/'+quote(resources['compute']['key'], safe=''))
        if compute.get('state') != 'ACTIVE':
            raise PocError('Attached AI Compute is not ACTIVE.')
        # Unique directory inside the owned workspace. Source contains no keys.
        folder = '/Workspace/Shared/poc_probe_'+resources['agent']['key'].replace('-', '_')
        objects = prefix+'/objects'
        self.saved['probe_folder'] = folder
        self.save()
        existing = self.api.items(objects+'?'+urlencode({'path': '/Workspace/Shared'}))
        if not any(item['path'] == folder for item in existing):
            self.api.request('POST', objects, '', headers={'path': folder, 'type': 'FOLDER'}, raw=True)
        filenames = {'probe_identity.py': (ROOT/'integration/aidp/probe_identity.py').read_text(),
                     'requirements.txt': '# Use the platform-provided OCI SDK; no package overrides.\n'}
        if self.saved.get('synthetic_credential'):
            filenames['probe_identity.py'] = filenames['probe_identity.py'].replace(
                'SYNTHETIC_CREDENTIAL_NAME = None', 'SYNTHETIC_CREDENTIAL_NAME = '+
                repr(self.saved['synthetic_credential']['name']), 1)
        if self.saved.get('vault_credential'):
            filenames['probe_identity.py'] = filenames['probe_identity.py'].replace(
                'INFERENCE_CREDENTIAL_NAME = None', 'INFERENCE_CREDENTIAL_NAME = '+
                repr(self.saved['vault_credential']['name']), 1)
            filenames['probe_identity.py'] = filenames['probe_identity.py'].replace(
                'INFERENCE_SECRET_ID = None', 'INFERENCE_SECRET_ID = '+
                repr(self.saved['vault_credential']['secret_id']), 1)
        runtime = self.root/'runtime'
        if (runtime/'runtime.json').is_file() and (runtime/'inference-ca.crt').is_file():
            cfg = json.loads((runtime/'runtime.json').read_text())
            ca = (runtime/'inference-ca.crt').read_text()
            if 'PRIVATE KEY' in ca:
                raise PocError('Refusing certificate file containing private-key material.')
            filenames['inference-ca.crt'] = ca
            network = {'base_url': cfg['base_url'], 'ca_pem': ca}
            if self.saved.get('private_dns_name'):
                name = self.saved['private_dns_name']
                network.update(base_url='https://'+name+'/v1', private_dns_hostname=name)
            # Deployed code did not resolve adjacent configuration in this
            # runtime. Embed only the non-secret URL and public trust anchor.
            filenames['probe_identity.py'] = filenames['probe_identity.py'].replace(
                'EMBEDDED_NETWORK = None', 'EMBEDDED_NETWORK = '+repr(network), 1)
            filenames['probe_network.json'] = json.dumps({k:v for k,v in dict(
                network, ca_file='inference-ca.crt').items() if k != 'ca_pem'})
        uploaded = {}
        for name, text in filenames.items():
            path = folder+'/'+name
            _, headers = self.api.request('POST', objects, text, headers={
                'path': path, 'type': 'FILE', 'is-overwrite': 'true'}, raw=True)
            uploaded[name] = headers.get('path', path)
        self.api.request('PUT', agent_path, {'entryFilePath': uploaded['probe_identity.py'],
                                          'dependenciesFilePath': uploaded['requirements.txt']})
        self.saved['probe_files'] = uploaded
        self.save()
        print('Uploaded metadata-only probe and set the coded-agent entry/dependency files.')

    def publish_probe(self):
        resources = self.saved['resources']
        if not self.saved.get('probe_files'):
            raise PocError('Run upload-probe first.')
        prefix = ('/workspaces/'+quote(resources['workspace']['key'], safe='')+
                  '/agents/'+quote(resources['agent']['key'], safe=''))
        agent = self.api.get(prefix)
        if (agent.get('entryFilePath') != self.saved['probe_files']['probe_identity.py'] or
                agent.get('computeKey') != resources['compute']['key']):
            raise PocError('Agent entry or attached compute changed; review before publishing.')
        if resources.get('deployment'):
            item = self.api.get(prefix+'/deployments/'+quote(resources['deployment']['key'], safe=''))
        else:
            name = self.cfg['name'].replace('-', '_')+'_identity_probe_endpoint'
            if any(x['displayName'] == name for x in self.api.items(prefix+'/deployments')):
                raise PocError('Deployment name exists without ownership inventory.')
            item = self.create('deployment', prefix+'/deployments/actions/deploy', {
                'displayName': name, 'agentKey': resources['agent']['key'],
                'agentComputeKey': resources['compute']['key']})
        print(f'Probe deployment state: {item.get("lifecycleState")}. '
              'This endpoint requires OCI authentication.')
        if item.get('lifecycleState') == 'FAILED':
            raise PocError('Probe deployment FAILED. Inspect its execution error before retrying.')
        return item.get('lifecycleState') == 'ACTIVE'

    def invoke_probe(self):
        resources = self.saved['resources']
        if not resources.get('deployment'):
            raise PocError('Run publish-probe first.')
        prefix = ('/workspaces/'+quote(resources['workspace']['key'], safe='')+
                  '/agents/'+quote(resources['agent']['key'], safe='')+'/deployments/')
        deployment = self.api.get(prefix+quote(resources['deployment']['key'], safe=''))
        if deployment.get('lifecycleState') != 'ACTIVE' or not deployment.get('endpointUrl'):
            raise PocError('Probe deployment is not ACTIVE yet.')
        response, _ = self.api.request('POST', '', {
            'isStreamEnabled': False, 'sessionKey': str(uuid.uuid4()), 'trace': False,
            'input': [{'role': 'User', 'content': [{'type': 'INPUT_TEXT', 'text': 'Report runtime identity metadata.'}]}]
        }, endpoint=deployment['endpointUrl'])
        # Only keep the known probe report, never arbitrary platform response data.
        reports = []
        def visit(value, depth=0):
            if depth > 20: return
            if isinstance(value, dict):
                if value.get('status') in ('SIGNER_AVAILABLE', 'RESOURCE_PRINCIPAL_UNAVAILABLE',
                                          'OCI_SDK_UNAVAILABLE', 'CLAIM_READER_UNAVAILABLE'):
                    reports.append({k: value[k] for k in ('status', 'signer_class', 'identity_claims',
                                                        'policy_values_verified', 'error_type',
                                                        'private_transport', 'private_transport_direct', 'credential_api',
                                                        'runtime_proxy_variables_present', 'credential_probe',
                                                        'vault_credential_probe') if k in value})
                else:
                    for child in value.values(): visit(child, depth+1)
            elif isinstance(value, list):
                for child in value: visit(child, depth+1)
            elif isinstance(value, str) and len(value) < 32768:
                try: visit(json.loads(value), depth+1)
                except (ValueError, RecursionError): pass
        visit(response)
        if not reports:
            raise PocError('Endpoint returned no recognized identity report. Inspect its sanitized execution error in AIDP.')
        private_json(self.root/'runtime-identity.json', {'reports': reports,
                     'workspace_key': resources['workspace']['key'], 'compute_key': resources['compute']['key'],
                     'agent_key': resources['agent']['key']})
        if self.saved.get('private_dns_name') and any(
                r.get('private_transport', {}).get('status') == 'PASS' and
                r.get('private_transport', {}).get('tls_verified') is True and
                r.get('private_transport', {}).get('http_status') == 401 for r in reports):
            self.saved['private_dns_mode'] = 'vcn-default-resolution-verified'
            self.save()
        print(json.dumps(reports, indent=2))

    def redeploy_probe(self, inference=False):
        r = self.saved['resources']
        if not r.get('deployment'):
            raise PocError('Publish the probe before redeploying it.')
        path = ('/workspaces/'+quote(r['workspace']['key'], safe='')+'/agents/'+
                quote(r['agent']['key'], safe='')+'/deployments/actions/redeploy')
        body = {'agentKey': r['agent']['key'], 'agentComputeKey': r['compute']['key']}
        if inference:
            if not self.saved.get('inference_entry'):
                raise PocError('Upload the inference entry before redeploying it.')
            name = self.cfg['name'].replace('-', '_')+'_inference_endpoint'
            if name != r['deployment']['name']:
                body['displayName'] = name
        item, _ = self.api.request('POST', path, body)
        if item['key'] != r['deployment']['key']:
            self.saved.setdefault('previous_deployments', []).append(dict(r['deployment']))
            r['deployment']['key'] = item['key']
        if item.get('displayName'):
            r['deployment']['name'] = item['displayName']
        if inference:
            self.saved['inference_publish_requested'] = True
        self.save()
        print(f'Agent redeployment state: {item.get("lifecycleState")}.')

    def prepare_credential_probe(self):
        if self.saved.get('synthetic_credential'):
            print('Synthetic Credential Store probe is already recorded.'); return
        name = self.cfg['name'].replace('-', '_')+'_credential_probe'
        if any(x.get('displayName') == name for x in self.api.items('/credentials')):
            raise PocError('Credential name exists without ownership inventory.')
        token = self.saved['tokens'].setdefault('synthetic_credential', str(uuid.uuid4()))
        self.save()
        item, _ = self.api.request('POST', '/credentials', {
            'displayName': name, 'credentialDescription': 'Synthetic utility-connectivity probe; no customer key',
            'type': 'SECRET_TOKEN', 'credentialDetails': {'credentialType': 'SECRET_TOKEN',
                'secretTokenPair': [{'secretKey': 'value', 'secretValue': 'synthetic-poc-probe'}]}}, token)
        key = item.get('key')
        if not key:
            # This deployment's create API returns an empty successful body.
            # Reconcile only after our successful POST, never adopt on startup.
            matches = [x for x in self.api.items('/credentials') if x.get('displayName') == name]
            if len(matches) != 1:
                raise PocError('Credential POST succeeded but its metadata key could not be reconciled.')
            key = matches[0]['key']
        self.saved['synthetic_credential'] = {'key': key, 'name': name}
        self.save()
        print('Created a synthetic Credential Store probe. No inference key was copied.')

    def prepare_vault_credential(self):
        output(self.cfg, 'aidp-credential-iam')
        if self.saved.get('vault_credential'):
            print('Vault reference is already recorded.'); return
        record = json.loads((self.root/'runtime/inference-secret.json').read_text())
        name = self.cfg['name'].replace('-', '_')+'_vllm_key'
        if any(x.get('displayName') == name for x in self.api.items('/credentials')):
            raise PocError('Vault credential name exists without ownership inventory.')
        token = self.saved['tokens'].setdefault('vault_credential', str(uuid.uuid4()))
        self.save()
        item, _ = self.api.request('POST', '/credentials', {'displayName': name,
            'credentialDescription': 'Reference to the POC inference key in OCI Vault',
            'type': 'VAULT_REFERENCE', 'credentialDetails': {
                'credentialType': 'VAULT_REFERENCE', 'secretId': record['id']}}, token)
        key = item.get('key')
        if not key:
            matches = [x for x in self.api.items('/credentials') if x.get('displayName') == name]
            if len(matches) != 1:
                raise PocError('Vault credential POST succeeded but its key could not be reconciled.')
            key = matches[0]['key']
        self.saved['vault_credential'] = {'key': key, 'name': name, 'secret_id': record['id']}
        self.save()
        print('Created a Vault reference; the controller did not retrieve or embed the key.')

    def upload_inference(self):
        if not self.saved.get('private_dns_name') or not self.saved.get('vault_credential'):
            raise PocError('Prepare private DNS and the Vault reference first.')
        if not self.saved.get('probe_files'):
            raise PocError('Bootstrap the owned coded agent first.')
        evidence = self.root/'runtime-identity.json'
        if not evidence.is_file():
            raise PocError('Run the deployed identity/transport/credential probe first.')
        proof = json.loads(evidence.read_text())
        r = self.saved['resources']
        if any(proof.get(k) != r[kind]['key'] for k,kind in
               (('workspace_key','workspace'),('compute_key','compute'),('agent_key','agent'))):
            raise PocError('Probe evidence belongs to another workspace, compute or agent.')
        if not any(x.get('private_transport',{}).get('status') == 'PASS' and
                   x.get('private_transport',{}).get('tls_verified') is True and
                   x.get('private_transport',{}).get('http_status') == 401 and
                   x.get('vault_credential_probe',{}).get('status') == 'PASS'
                   for x in proof.get('reports', [])):
            raise PocError('Private HTTPS and Vault-reference checks must pass before publishing inference.')
        cfg = json.loads((self.root/'runtime/runtime.json').read_text())
        config = {'base_url': 'https://'+self.saved['private_dns_name']+'/v1',
                  'private_dns_hostname': self.saved['private_dns_name'],
                  'model': cfg['model'], 'region': self.cfg['region'],
                  'ca_pem': (self.root/'runtime/inference-ca.crt').read_text(),
                  'api_key_credential_name': self.saved['vault_credential']['name'],
                  'api_key_credential_secret_id': self.saved['vault_credential']['secret_id'],
                  'timeout_seconds': 120, 'max_tokens': 512}
        spec = importlib.util.spec_from_file_location('aidp_renderer', ROOT/'integration/aidp/render_agent.py')
        renderer = importlib.util.module_from_spec(spec); spec.loader.exec_module(renderer)
        source = renderer.render(config)
        generated = self.root/'runtime/inference_agent.generated.py'
        generated.write_text(source); generated.chmod(0o600)
        prefix = '/workspaces/'+quote(r['workspace']['key'], safe='')
        path = self.saved['probe_folder']+'/inference_agent.py'
        self.api.request('POST', prefix+'/objects', source, headers={
            'path': path, 'type': 'FILE', 'is-overwrite': 'true'}, raw=True)
        name = self.cfg['name'].replace('-', '_')+'_agent'
        self.api.request('PUT', prefix+'/agents/'+quote(r['agent']['key'], safe=''), {
            'displayName': name, 'entryFilePath': path,
            'dependenciesFilePath': self.saved['probe_files']['requirements.txt'],
            'computeKey': r['compute']['key']})
        self.saved.pop('playground_verification', None)
        self.saved.pop('playground_status', None)
        r['agent']['name'] = name
        self.saved['inference_entry'] = {'path': path, 'model': cfg['model'],
                                          'credential_name': self.saved['vault_credential']['name']}
        self.save()
        print('Uploaded the self-contained LangGraph inference agent with a Vault credential name.')

    def playground_status(self):
        """Check TEST metadata and show the UI refresh step; PROD chat is not TEST proof."""
        r = self.saved['resources']
        prefix = ('/workspaces/'+quote(r['workspace']['key'], safe='')+'/agents/'+
                  quote(r['agent']['key'], safe=''))
        agent = self.api.get(prefix)
        if (not self.saved.get('inference_entry') or
                agent.get('entryFilePath') != self.saved['inference_entry']['path'] or
                agent.get('computeKey') != r['compute']['key']):
            raise PocError('Playground entry or compute differs from the owned inference configuration.')
        states = agent.get('deploymentState') or {}
        self.saved['playground_status'] = {
            'test_state': states.get('TEST', 'UNKNOWN'), 'chat_verification': 'UI_CHECK_REQUIRED',
            'agent_key': r['agent']['key'], 'compute_key': r['compute']['key'],
            'entry_path': self.saved['inference_entry']['path']}
        self.save()
        print('Published inference acceptance does not verify Playground chat.', flush=True)
        print('Playground TEST runtime: '+str(states.get('TEST', 'UNKNOWN')), flush=True)
        print('Required UI check: open this agent in Playground and ask "What is AI?". '
              'If it returns SIGNER_AVAILABLE or other probe JSON, deselect its Playground AI Compute, '
              'then select the same existing compute and choose Select & Deploy. '
              'Wait for Active and create a new session. Keep the published endpoint deployed.', flush=True)
        return states.get('TEST') == 'ACTIVE'

    def invoke_inference(self):
        r = self.saved['resources']
        prefix = ('/workspaces/'+quote(r['workspace']['key'], safe='')+'/agents/'+
                  quote(r['agent']['key'], safe='')+'/deployments/')
        deployed = self.api.get(prefix+quote(r['deployment']['key'], safe=''))
        if deployed.get('lifecycleState') != 'ACTIVE':
            raise PocError('Inference deployment is not ACTIVE yet; inspect status before retrying.')
        response, _ = self.api.request('POST', '', {'isStreamEnabled': False,
            'sessionKey': str(uuid.uuid4()), 'trace': False,
            'input': [{'role':'User','content':[{'type':'INPUT_TEXT','text':'__POC_VERIFY__'}]}]
        }, endpoint=deployed['endpointUrl'])
        reports = []
        def visit(value, depth=0):
            if depth > 20: return
            if isinstance(value, dict):
                if value.get('stage') == 'coded_agent' and value.get('status') in ('PASS','FAIL'):
                    reports.append({k:value[k] for k in ('status','stage','private_chat_verified',
                        'tool_observed','synthetic_inventory_verified','final_answer_present','error_type') if k in value})
                else:
                    for child in value.values(): visit(child, depth+1)
            elif isinstance(value, list):
                for child in value: visit(child, depth+1)
            elif isinstance(value, str) and len(value) < 32768:
                try: visit(json.loads(value), depth+1)
                except (ValueError, RecursionError): pass
        visit(response)
        if not reports:
            raise PocError('No recognized coded-agent acceptance report was returned.')
        private_json(self.root/'aidp-inference-acceptance.json', {'reports':reports,
            'workspace_key':r['workspace']['key'], 'compute_key':r['compute']['key'], 'agent_key':r['agent']['key']})
        print(json.dumps(reports, indent=2))
        flags = ('private_chat_verified','tool_observed','synthetic_inventory_verified','final_answer_present')
        if not any(x.get('status') == 'PASS' and all(x.get(k) is True for k in flags) for x in reports):
            raise PocError('The managed-runtime inference acceptance check did not pass.')
        self.saved['inference_verified'] = True
        self.save()

    def configure_private_dns(self):
        # The private zone is in the VCN resolver's default view. Custom
        # workspace dnsZones is a gated feature and is not needed to test
        # the workspace's existing private-network/default-resolver path.
        name = output(self.cfg, 'aidp-dns')['private_dns_name']
        workspace = self.workspace()
        if workspace.get('lifecycleState') != 'ACTIVE':
            print('Waiting for workspace: '+workspace.get('lifecycleState', 'unknown')); return False
        r = self.saved['resources']
        path = ('/workspaces/'+quote(r['workspace']['key'], safe='')+'/clusters/'+
                quote(r['compute']['key'], safe=''))
        compute = self.api.get(path)
        if self.saved.get('private_dns_name') != name or not self.saved.get('private_dns_mode'):
            self.saved['private_dns_mode'] = 'vcn-default-resolution-pending-test'
        self.saved['private_dns_name'] = name
        self.save()
        if compute['state'] == 'STOPPED':
            self.api.request('POST', path+'/actions/start', {})
            print('Starting the owned AI Compute for private DNS validation.'); return False
        if compute['state'] == 'FAILED':
            raise PocError('AI Compute FAILED; inspect the service operation.')
        if compute['state'] == 'ACTIVE':
            print('AI Compute ACTIVE; private DNS alias prepared for runtime validation.'); return True
        print('Waiting for AI Compute: '+compute['state']); return False

    def wait_active(self):
        r = self.saved['resources']
        prefix = '/workspaces/'+quote(r['workspace']['key'], safe='')
        workspace = self.workspace()
        compute = self.api.get(prefix+'/clusters/'+quote(r['compute']['key'], safe=''))
        agent = self.api.get(prefix+'/agents/'+quote(r['agent']['key'], safe=''))
        deployed = self.api.get(prefix+'/agents/'+quote(r['agent']['key'], safe='')+
                                '/deployments/'+quote(r['deployment']['key'], safe=''))
        if agent.get('computeKey') != r['compute']['key']:
            raise PocError('The owned agent is attached to a different compute.')
        states = [workspace.get('lifecycleState'), compute.get('state'), deployed.get('lifecycleState')]
        if 'FAILED' in states:
            raise PocError('AIDP runtime reports FAILED; inspect status before retrying.')
        print('Workspace / AI Compute / deployment: '+', '.join(states))
        return all(value == 'ACTIVE' for value in states)

    def connect(self, wait_minutes=30):
        """Connect the prepared Terraform foundation; resume without resetting a live agent."""
        output(self.cfg, 'aidp-dns')
        output(self.cfg, 'aidp-credential-iam')
        for name in ('runtime.json', 'inference-ca.crt', 'inference-secret.json'):
            if not (self.root/'runtime'/name).is_file():
                raise PocError('Prepare the runtime bundle and publish the Vault key before connect.')
        deadline = time.monotonic()+wait_minutes*60
        def wait(check):
            while not check():
                if time.monotonic() >= deadline:
                    raise PocError('Connect reached its wait deadline. Rerun the same command to resume.')
                time.sleep(30)
        if self.saved.get('inference_entry'):
            if not self.saved.get('inference_publish_requested') and not self.saved.get('inference_verified'):
                self.redeploy_probe(inference=True)
            wait(self.wait_active)
            self.invoke_inference()
            self.playground_status()
            return
        wait(self.apply)
        wait(self.configure_private_dns)
        self.prepare_credential_probe()
        self.prepare_vault_credential()
        self.upload_probe()
        if self.saved['resources'].get('deployment'):
            self.redeploy_probe()
        else:
            self.publish_probe()
        wait(self.wait_active)
        self.invoke_probe()
        self.upload_inference()  # Requires live private TLS and Vault probe evidence.
        self.redeploy_probe(inference=True)
        wait(self.wait_active)
        self.invoke_inference()
        self.playground_status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['plan', 'apply', 'bootstrap', 'connect', 'status', 'repair-agent-path', 'wait-active', 'upload-probe', 'publish-probe', 'invoke-probe', 'redeploy-probe', 'prepare-credential-probe', 'configure-private-dns', 'prepare-vault-credential', 'upload-inference', 'redeploy-inference', 'invoke-inference', 'playground-status'])
    parser.add_argument('--config', type=Path, default=ROOT/'poc-config.json')
    parser.add_argument('--apply', action='store_true', help='Apply repair-agent-path; omitted previews without changes.')
    parser.add_argument('--wait-minutes', type=int, default=0,
                        help='For apply, poll every 30 seconds until ready (maximum 60 minutes).')
    args = parser.parse_args()
    if args.apply and args.command != 'repair-agent-path':
        parser.error('--apply is only used with repair-agent-path; other commands retain their existing behavior.')
    if not 0 <= args.wait_minutes <= 60:
        parser.error('--wait-minutes must be between 0 and 60')
    cfg = read_config(args.config)
    target_check(cfg)
    cfg_path = ROOT/'.local'; cfg_path.mkdir(exist_ok=True, mode=0o700)
    lock = ROOT/'deployments'/cfg['name']/'aidp-workspace.lock'
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        workspace = Workspace(cfg)
        if args.command == 'plan':
            print(json.dumps({'workspace': workspace.workspace(), 'compute': '1 OCPU, 16 GB, one fixed replica',
                              'agent': 'CODE identity probe; no key access or IAM changes'}, indent=2))
        elif args.command == 'repair-agent-path':
            workspace.repair_agent_path(apply=args.apply)
        elif args.command == 'connect':
            workspace.connect(args.wait_minutes or 30)
        elif args.command in ('apply', 'bootstrap', 'configure-private-dns', 'wait-active'):
            deadline = time.monotonic()+args.wait_minutes*60
            while True:
                if args.command == 'configure-private-dns':
                    ready = workspace.configure_private_dns()
                elif args.command == 'wait-active':
                    ready = workspace.wait_active()
                else:
                    ready = workspace.apply()
                if ready and args.command == 'bootstrap':
                    if not workspace.saved.get('probe_files'):
                        workspace.upload_probe()
                    ready = workspace.publish_probe()
                    if ready:
                        workspace.invoke_probe()
                if ready:
                    break
                if time.monotonic() >= deadline:
                    if args.wait_minutes:
                        raise PocError('Still provisioning at the wait deadline. Resume apply using the same inventory.')
                    break
                time.sleep(30)
        elif args.command == 'upload-probe':
            workspace.upload_probe()
        elif args.command == 'publish-probe':
            workspace.publish_probe()
        elif args.command == 'invoke-probe':
            workspace.invoke_probe()
        elif args.command == 'redeploy-probe':
            workspace.redeploy_probe()
        elif args.command == 'prepare-credential-probe':
            workspace.prepare_credential_probe()
        elif args.command == 'prepare-vault-credential':
            workspace.prepare_vault_credential()
        elif args.command == 'upload-inference':
            workspace.upload_inference()
        elif args.command == 'redeploy-inference':
            workspace.redeploy_probe(inference=True)
        elif args.command == 'invoke-inference':
            workspace.invoke_inference()
        elif args.command == 'playground-status':
            workspace.playground_status()
        else:
            workspace.status()


if __name__ == '__main__':
    try:
        main()
    except (PocError, subprocess.TimeoutExpired, BlockingIOError) as exc:
        print(str(exc) if isinstance(exc, PocError) else 'Request timed out or another setup process is running.', file=sys.stderr)
        sys.exit(1)

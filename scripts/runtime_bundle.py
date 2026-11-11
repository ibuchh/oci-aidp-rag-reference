"""Prepare agent source and trust certificate; publish an existing key only on request."""
import base64
import json
import os
from pathlib import Path
import shutil
import ssl
import subprocess

from inference_stack import PocError, ROOT, private_json
from hosts import Access
from configure_public_apps import host_context


def prepare(cfg, *, publish=False):
    from poc import output, oci
    foundation=output(cfg,'aidp-foundation')
    destination=Path(cfg['state_directory']).parent/'runtime'
    destination.mkdir(parents=True,exist_ok=True,mode=0o700)
    lab,context,known=host_context(cfg)
    gpu=context['vllm']['private_ip']['value']
    with Access(lab,context,'vllm',known) as access:
        subnet=foundation['workspace_cidr']
        access.ssh('sudo bash -s -- '+subnet+' --apply',
                   data=(ROOT/'runtime/aidp/configure-gpu-firewall.sh').read_bytes())
        certificate=access.ssh('sudo cat /etc/nginx/tls/server.crt',capture=True)
        cert=destination/'inference-ca.crt';cert.write_bytes(certificate);cert.chmod(0o644)
        # OpenSSL validates current validity and the actual new private-IP SAN.
        for flags in (['-checkend','86400'],['-checkip',gpu]):
            result=subprocess.run(['openssl','x509','-in',str(cert),'-noout',*flags],capture_output=True)
            if result.returncode:raise PocError('Inference certificate expiry/SAN validation failed; trust was not published.')
        record=destination/'inference-secret.json'
        if publish:
            if record.exists():
                secret=oci(cfg,'vault','secret','get','--secret-id',json.loads(record.read_text())['id'])
                if secret['lifecycle-state']!='ACTIVE':raise PocError('Recorded inference secret is not ACTIVE; review rotation/cleanup.')
                print('Inference secret already recorded; no new secret created.')
            else:
                if (destination/'secret-create-intent.json').exists():
                    raise PocError('An interrupted secret-create intent exists. Reconcile it in Vault before retrying; see docs/CLEANUP.md.')
                env=access.ssh('sudo cat /etc/vllm/server.env',capture=True).decode().splitlines()
                if len(env)!=1 or not env[0].startswith('VLLM_API_KEY='):raise PocError('Unexpected GPU credential file.')
                key=env[0].split('=',1)[1]
                if not key or any(c.isspace() for c in key):raise PocError('Unexpected API key format.')
                payload={'compartmentId':cfg['compartment_id'],'vaultId':foundation['vault_id'],
                         'keyId':foundation['key_id'],'secretName':cfg['name']+'-inference-api-key',
                         'description':'Synthetic POC private vLLM API key',
                         'secretContentContent':base64.b64encode(key.encode()).decode()}
                # Persist non-secret recovery identity before creation so interrupted creates can be reconciled.
                private_json(destination/'secret-create-intent.json',{k:v for k,v in payload.items() if k!='secretContentContent'})
                result=subprocess.run(['oci','--profile',cfg['oci_profile'],'--region',cfg['region'],
                                       'vault','secret','create-base64','--from-json','file:///dev/stdin'],
                                      input=json.dumps(payload),capture_output=True,text=True,timeout=180)
                del key,env,payload
                if result.returncode:raise PocError('Secret creation failed. Output withheld; reconcile the recorded intent before retrying.')
                secret=json.loads(result.stdout)['data']
                private_json(record,{'id':secret['id'],'name':secret['secret-name'],'vault_id':secret['vault-id']})
    for name in ('agent.py','private_client.py','probe_identity.py','verify_agent.py','requirements.txt'):
        shutil.copy2(ROOT/'integration/aidp'/name,destination/name)
    runtime={'base_url':f'https://{gpu}/v1','model':'qwen3-8b','ca_file':'inference-ca.crt',
             'api_key_file':None,'api_key_secret_id':json.loads(record.read_text())['id'] if record.exists() else None,
             'region':cfg['region'],'timeout_seconds':120,'max_tokens':512}
    private_json(destination/'runtime.json',runtime)
    print(f'Agent bundle: {destination}. Configure the actual AI Compute identity/credential binding before runtime validation.')

#!/usr/bin/env python3
"""Explicit recovery of the known first-boot APT-lock failure on an existing GPU.

The original cloud-init error records remain intact. A root-owned repair record
is accepted only with matching instance/source and current package/service/HTTP
health checks. No lock files, package processes or cloud-init history are removed.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
import urllib.request

RESULT = Path('/var/lib/cloud/data/result.json')
STATUS = Path('/var/lib/cloud/data/status.json')
MARKER = Path('/var/lib/vllm/bootstrap-recovery.json')
BOOTSTRAP = Path('/opt/vllm/bootstrap.sh')
PACKAGES = ['docker.io', 'nfs-common', 'nginx', 'openssl', 'curl', 'gnupg']


def cloud_errors():
    result = json.loads(RESULT.read_text())['v1']['errors']
    stages = json.loads(STATUS.read_text())['v1']
    earlier = [error for name in ('init-local', 'init', 'modules-config')
               for error in (stages.get(name, {}).get('errors', [])+
                             stages.get(name, {}).get('recoverable_errors', {}).get('ERROR', []))]
    if earlier:
        raise RuntimeError('Earlier cloud-init stages also failed; APT-only recovery is refused.')
    if not isinstance(result, list) or any(not isinstance(e, str) for e in result):
        raise RuntimeError('Invalid cloud-init failure record.')
    return result


def eligible(errors):
    return bool(errors) and any('Could not get APT lock' in e for e in errors) and all(
        (e.startswith("('package_update_upgrade_install',") and 'Could not get APT lock' in e)
        or e.startswith("('scripts_user', RuntimeError('Runparts: 1 failures (runcmd)")
        for e in errors)


def capture(arguments):
    return subprocess.check_output(arguments, text=True).strip()


def assert_healthy(expected):
    for unit in ('docker', 'nginx', 'vllm'):
        subprocess.run(['systemctl', 'is-active', '--quiet', unit], check=True)
    states = capture(['dpkg-query', '-W', '-f=${db:Status-Status}\n', *PACKAGES]).splitlines()
    if states != ['installed']*len(PACKAGES):
        raise RuntimeError('Required bootstrap packages are not all installed.')
    if capture(['findmnt', '-n', '-o', 'SOURCE', '--target', '/mnt/models']) != expected['mount_source']:
        raise RuntimeError('The model mount does not match this deployment.')
    unit = capture(['systemctl', 'cat', 'vllm'])
    if expected['model_revision'] not in unit or expected['vllm_image'] not in unit:
        raise RuntimeError('The vLLM service does not match the configured model revision/image.')
    with urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=5) as response:
        if response.status != 200:
            raise RuntimeError('vLLM health did not return HTTP 200.')


def validate_expected(expected):
    if (not expected.get('instance_id', '').startswith('ocid1.instance.') or
            not re.fullmatch('[0-9a-f]{40}', expected.get('model_revision', '')) or
            not re.fullmatch('[0-9a-f]{64}', expected.get('bootstrap_sha256', ''))):
        raise RuntimeError('Invalid recovery identity/source expectations.')


def verify(expected):
    validate_expected(expected)
    info = MARKER.lstat()
    if MARKER.is_symlink() or info.st_uid != 0 or info.st_mode & 0o777 != 0o600:
        raise RuntimeError('A private root-owned bootstrap repair record is required.')
    marker = json.loads(MARKER.read_text())
    if marker.get('status') != 'REPAIRED' or any(marker.get(k) != v for k,v in expected.items()):
        raise RuntimeError('Bootstrap repair record does not match this deployment/source.')
    errors = cloud_errors()
    if not eligible(errors) or errors != marker.get('original_cloud_init_errors'):
        raise RuntimeError('Cloud-init failures changed or are outside the APT-lock recovery scope.')
    if hashlib.sha256(BOOTSTRAP.read_bytes()).hexdigest() != expected['bootstrap_sha256']:
        raise RuntimeError('The repaired bootstrap source changed.')
    assert_healthy(expected)
    return {'status':'REPAIRED', 'historical_cloud_init_errors_retained':True,
            'instance_source_mount_and_live_health_verified':True}


def install_payload(payload):
    bootstrap = payload['bootstrap'].encode()
    if hashlib.sha256(bootstrap).hexdigest() != payload['expected']['bootstrap_sha256']:
        raise RuntimeError('Recovery bootstrap checksum mismatch.')
    backup = Path('/var/backups/poc-bootstrap')/datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    backup.mkdir(parents=True, mode=0o700)
    if BOOTSTRAP.exists():
        shutil.copy2(BOOTSTRAP, backup/'bootstrap.sh')
    helper = Path('/usr/local/sbin/poc-apt')
    helper.write_text(payload['apt_helper']); helper.chmod(0o755)
    BOOTSTRAP.write_bytes(bootstrap); BOOTSTRAP.chmod(0o700)
    print('Original bootstrap preserved in '+str(backup), flush=True)


def repair(payload):
    validate_expected(payload['expected'])
    if MARKER.exists():
        return verify(payload['expected'])
    errors = cloud_errors()
    if not eligible(errors):
        raise RuntimeError('Recovery applies only to the recorded APT-lock/package failure and dependent runcmd failure.')
    unit = subprocess.run(['systemctl','show','-p','SubState','--value','cloud-final.service'],
                          capture_output=True,text=True,check=True).stdout.strip()
    if unit not in ('failed', 'dead', 'exited'):
        raise RuntimeError('cloud-final is still running; wait before recovery.')
    if not shutil.which('nvidia-smi'):
        raise RuntimeError('The required NVIDIA driver/image prerequisite is absent.')
    install_payload(payload)
    print('Retrying GPU bootstrap; APT lock waits are bounded and existing package processes remain running.', flush=True)
    subprocess.run(['bash',str(BOOTSTRAP)], check=True)
    deadline = time.monotonic()+600
    while True:
        try:
            assert_healthy(payload['expected'])
            break
        except (OSError, RuntimeError, subprocess.CalledProcessError):
            if time.monotonic() >= deadline:
                raise RuntimeError('Repaired bootstrap did not pass live health checks within ten minutes.') from None
            print('Waiting for private vLLM model readiness...', flush=True)
            time.sleep(10)
    marker = dict(payload['expected'], status='REPAIRED',
                  original_cloud_init_errors=errors, repaired_utc=datetime.now(timezone.utc).isoformat())
    MARKER.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary = MARKER.with_suffix('.tmp')
    with temporary.open('w') as stream:
        os.chmod(temporary,0o600);json.dump(marker,stream,indent=2);stream.write('\n')
    temporary.replace(MARKER)
    return verify(payload['expected'])


def remote_main(payload):
    try:
        if payload['action'] == 'inspect':
            errors=cloud_errors()
            result={'cloud_init_errors':errors, 'eligible_for_apt_lock_recovery':eligible(errors),
                    'apt_processes':[line for line in capture(['ps','-eo','pid,etimes,comm']).splitlines()
                                     if any(name in line.lower() for name in ('apt','dpkg','unattended'))],
                    'tools':{n:bool(shutil.which(n)) for n in ('nvidia-smi','nvidia-ctk','docker','nginx')}}
        elif payload['action'] == 'verify':result=verify(payload['expected'])
        elif payload['action'] == 'repair':result=repair(payload)
        else:raise RuntimeError('Unknown recovery action.')
        print(json.dumps(result),flush=True)
    except (OSError, RuntimeError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None


def configured_image(root, cfg):
    if cfg.get('vllm_image'):
        return cfg['vllm_image']
    declaration=(root/'terraform/vllm/variables.tf').read_text()
    found=re.search(r'variable\s+"vllm_image"\s*\{\s*default\s*=\s*"([^"]+)"',declaration)
    if not found or '@sha256:' not in found[1]:
        raise ValueError('Cannot establish the pinned vLLM image from its Terraform declaration.')
    return found[1]


def render_bootstrap(root, cfg, context):
    template=(root/'terraform/vllm/cloud-init.yaml.tftpl').read_text()
    marker='  - path: /opt/vllm/bootstrap.sh\n'
    if template.count(marker) != 1:
        raise ValueError('Expected exactly one GPU bootstrap write_files entry.')
    entry=template.split(marker,1)[1].split('    content: |\n',1)[1]
    lines=[]
    for line in entry.splitlines():
        if line and not line.startswith('      '):break
        lines.append(line[6:] if line else '')
    script='\n'.join(lines)+'\n'
    values={'mount_ip':context['shared']['fss_mount_ip']['value'],
            'inference_private_ip':context['vllm']['private_ip']['value'],
            'model_revision':cfg['model_revision'], 'vllm_image':configured_image(root,cfg)}
    for k,v in values.items():script=script.replace('${'+k+'}',v)
    if '${' in script or not script.startswith('#!/bin/bash\n'):
        raise ValueError('Unresolved or invalid bootstrap template.')
    return script


def program(root, action, cfg, context):
    bootstrap=render_bootstrap(root,cfg,context)
    expected={'instance_id':context['vllm']['instance_id']['value'],
              'model_revision':cfg['model_revision'],
              'vllm_image':configured_image(root,cfg),
              'mount_source':context['shared']['fss_mount_ip']['value']+':/models',
              'bootstrap_sha256':hashlib.sha256(bootstrap.encode()).hexdigest()}
    payload={'action':action,'expected':expected}
    if action=='repair':
        payload.update(bootstrap=bootstrap,apt_helper=(root/'terraform/vllm/apt-retry.py.tftpl').read_text())
    return ('PAYLOAD = '+repr(payload)+'\n'+Path(__file__).read_text()+
            '\nremote_main(PAYLOAD)\n').encode()


def launch(cfg, *, apply=False):
    import contextlib
    import fcntl
    from configure_public_apps import host_context
    from hosts import Access
    from inference_stack import ROOT, PocError
    with contextlib.ExitStack() as locks:
        for path in (Path(cfg['state_directory']).parent/'operation.lock',
                     Path(cfg['state_directory'])/'.pocctl/operation.lock'):
            stream=locks.enter_context(path.open('a'))
            try:fcntl.flock(stream,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise PocError('A deployment operation is running; wait before GPU recovery.') from None
        lab,context,known=host_context(cfg)
        lab.snapshot()
        with Access(lab,context,'vllm',known) as host:
            try:
                host.ssh('sudo python3 -',data=program(ROOT,'repair' if apply else 'inspect',cfg,context),
                         timeout=cfg['bootstrap_timeout_seconds']+1200)
            except subprocess.CalledProcessError:
                raise PocError('GPU bootstrap recovery stopped; inspect the preceding error. Infrastructure and cloud-init evidence were retained.') from None

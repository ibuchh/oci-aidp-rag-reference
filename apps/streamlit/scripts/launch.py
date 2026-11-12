#!/usr/bin/env python3
"""Own temporary tunnels and a local UI; never display or log credentials."""
import argparse
import atexit
import contextlib
from datetime import datetime, timezone
import io
import json
import os
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import tarfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
PROCESSES = []
SESSIONS = []
CREDENTIALS = []


def private_write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'wb') as file:
        file.write(data if isinstance(data, bytes) else data.encode())


def run(command, data=None, *, error_log=None):
    result = subprocess.run(command, input=data, capture_output=True)
    if result.returncode:
        if error_log is not None:
            private_write(error_log, b'stdout:\n' + result.stdout + b'\nstderr:\n' + result.stderr)
            raise RuntimeError(f'{Path(command[0]).name} failed (exit {result.returncode}). '
                               f'Private build diagnostics: {error_log}')
        # stdout may contain a credential. It is never printed, including on error.
        raise RuntimeError(f'{Path(command[0]).name} failed. Check credentials, host identity and access; no captured output was displayed.')
    return result.stdout


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def require_free(port):
    with socket.socket() as s:
        try:
            s.bind(('127.0.0.1', port))
        except OSError:
            raise RuntimeError(f'Local port {port} is already in use. Select --port with another value.') from None


def spawn(command, log, env=None):
    file = log.open('ab')
    log.chmod(0o600)
    process = subprocess.Popen(command, env=env, stdout=file, stderr=file, start_new_session=True)
    file.close()
    PROCESSES.append(process)
    return process


def wait_port(port, process, seconds=240):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('A tunnel or application exited. Inspect the private launcher log; check the Bastion allowlist and key.')
        try:
            with socket.create_connection(('127.0.0.1', port), timeout=1):
                return
        except OSError:
            time.sleep(1)
    raise RuntimeError('Timed out waiting for the tunnel/application port.')


def cleanup():
    for process in reversed(PROCESSES):
        if process.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGINT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGTERM)
                with contextlib.suppress(subprocess.TimeoutExpired):
                    process.wait(timeout=5)
    # Also catch helper interruption during session activation, before its finally.
    for base, log in SESSIONS:
        if log.exists():
            for session in set(re.findall(r'Creating temporary session: (ocid1\.bastionsession\.[\w.-]+)', log.read_text(errors='replace'))):
                result = subprocess.run(base+['bastion', 'session', 'get', '--session-id', session], capture_output=True)
                try:
                    state = json.loads(result.stdout)['data']['lifecycle-state']
                except (ValueError, KeyError, TypeError):
                    state = None
                if state and state not in {'DELETED', 'DELETING'}:
                    subprocess.run(base+['bastion', 'session', 'delete', '--session-id', session, '--force'], capture_output=True)
    for path in CREDENTIALS:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()


atexit.register(cleanup)


def deployment(path):
    path = path.resolve()
    config = json.loads(path.read_text())
    state = Path(config['state_directory']).expanduser()
    if not state.is_absolute():
        state = path.parent/state
    state = state.resolve()
    outputs = state/'.pocctl/outputs.json'
    if outputs.exists():
        context = json.loads(outputs.read_text())
        known = state/'.pocctl/known_hosts'
    else:
        context = {role: json.loads(run(['terraform', f'-chdir={state/role}', 'output', '-json'])) for role in ['shared', 'vllm', 'monitoring']}
        known = path.parent/'secrets/known_hosts'
    if not known.is_file():
        raise RuntimeError('Verified known_hosts is missing. Complete deployment host verification first.')
    key = Path(config['ssh_private_key_path']).expanduser().resolve()
    if not key.is_file() or not Path(str(key)+'.pub').is_file():
        raise RuntimeError('The SSH key pair configured for this deployment is missing.')
    return config, context, known, key


def access(role, cfg, context, known, key, directory):
    port = free_port()
    log = directory/f'{role}-bastion.log'
    context_file = directory/'outputs.json'
    private_write(context_file, json.dumps(context))
    base = ['oci', '--profile', cfg['oci_profile'], '--region', cfg['region']]
    command = [sys.executable, str(ROOT/'scripts/bastion-tunnel.py'),
               '--profile', cfg['oci_profile'], '--region', cfg['region'],
               '--key', str(key), '--context-file', str(context_file),
               '--known-hosts', str(known), '--target', role, '--local-port', str(port)]
    print(f'Opening verified Bastion access to {role}…', flush=True)
    process = spawn(command, log)
    SESSIONS.append((base, log))
    wait_port(port, process)
    instance = context[role]['instance_id']['value']
    alias = instance
    if subprocess.run(['ssh-keygen', '-F', alias, '-f', str(known)], capture_output=True).returncode:
        alias = '[127.0.0.1]:' + ('2222' if role == 'vllm' else '2223')
        if subprocess.run(['ssh-keygen', '-F', alias, '-f', str(known)], capture_output=True).returncode:
            raise RuntimeError('No verified SSH host key exists for this VM.')
    ssh = ['ssh', '-F', '/dev/null', '-i', str(key), '-o', 'IdentitiesOnly=yes',
           '-o', 'StrictHostKeyChecking=yes', '-o', f'UserKnownHostsFile={known}',
           '-o', f'HostKeyAlias={alias}', '-o', 'ConnectTimeout=15',
           '-o', 'ServerAliveInterval=30', '-p', str(port), 'ubuntu@127.0.0.1']
    run(ssh+['true'])
    return ssh


def forward(ssh, local, target_port, directory):
    command = ssh[:-1]+['-o', 'ExitOnForwardFailure=yes', '-N',
                       '-L', f'127.0.0.1:{local}:127.0.0.1:{target_port}', ssh[-1]]
    process = spawn(command, directory/f'forward-{local}.log')
    wait_port(local, process, 45)
    return process


def package():
    required = ['app.py', 'client', '.streamlit/config.toml', 'requirements.txt',
                'Dockerfile', 'compose.yml', '.dockerignore']
    missing = [name for name in required if not (ROOT/name).exists()]
    if missing:
        raise RuntimeError('Application package is incomplete; missing: ' + ', '.join(missing))
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode='w:gz') as archive:
        for name in ['app.py', 'client', '.streamlit/config.toml', 'requirements.txt', 'Dockerfile', 'compose.yml', '.dockerignore']:
            path = ROOT/name
            candidates = sorted(path.rglob('*')) if path.is_dir() else [path]
            for child in candidates:
                if child.is_file() and '__pycache__' not in child.parts:
                    archive.add(child, arcname=child.relative_to(ROOT), recursive=False)
    return buffer.getvalue()


def monitoring_readiness_program():
    return '''import json,subprocess,time,urllib.request
deadline=time.monotonic()+120
while True:
    result=subprocess.run(['docker','inspect','--format','{{json .State}}','vllm-streamlit-client'],
                          capture_output=True,text=True,check=True)
    state=json.loads(result.stdout)
    if state.get('Status') in ('restarting','dead','exited') or state.get('OOMKilled'):
        raise SystemExit('Streamlit container failed during startup (exit '+str(state.get('ExitCode'))+
                         '). Inspect docker logs vllm-streamlit-client on the monitoring VM.')
    try:
        with urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health',timeout=5) as response:
            if response.status==200:
                print('Streamlit loopback health passed.',flush=True)
                break
    except OSError:pass
    if time.monotonic()>=deadline:
        raise SystemExit('Streamlit loopback health did not pass within 120 seconds. Inspect its container logs.')
    time.sleep(2)
'''


def install_monitoring(ssh, context):
    host = context['vllm']['private_ip']['value']
    grafana = context['monitoring']['grafana_url']['value']
    connection = {'host': host, 'port': 443, 'tls_hostname': 'inference.lab',
                  'ca_file': '/run/inference/gpu.crt', 'key_file': '',
                  'label': 'Monitoring VM · private HTTPS', 'model': 'qwen3-8b',
                  'context_limit': 4096, 'grafana_url': grafana}
    script = '''import io,json,os,pathlib,shutil,tarfile,sys
root=pathlib.Path('/opt/vllm-streamlit-client')
root.mkdir(exist_ok=True,mode=0o755)
with tarfile.open(fileobj=io.BytesIO(sys.stdin.buffer.read()),mode='r:gz') as archive:
    archive.extractall(root,filter='data')
private=pathlib.Path('/etc/vllm-streamlit-client')
private.mkdir(exist_ok=True,mode=0o700)
values=dict(line.split('=',1) for line in pathlib.Path('/etc/monitoring/inference.env').read_text().splitlines() if line and not line.startswith('#'))
from urllib.parse import urlsplit
assert urlsplit(values['INFERENCE_URL']).hostname == HOST, 'Monitoring credential destination does not match this GPU'
assert '\\n' not in values['VLLM_API_KEY'] and '\\r' not in values['VLLM_API_KEY']
p=private/'inference.env'
fd=os.open(p,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
os.fchmod(fd,0o600)
with os.fdopen(fd,'w') as f:f.write('VLLM_API_KEY='+values['VLLM_API_KEY']+'\\n')
shutil.copyfile('/etc/monitoring/gpu.crt',private/'gpu.crt')
os.chmod(private/'gpu.crt',0o644)
(root/'connection.json').write_text(json.dumps(CONNECTION))
os.chmod(root/'connection.json',0o644)
print('Application files and protected credential prepared.')
'''
    script = 'HOST='+repr(host)+'\nCONNECTION='+repr(connection)+'\n'+script
    print('Installing the isolated monitoring client; existing monitoring services stay running…', flush=True)
    run(ssh+['sudo -n python3 -c '+shlex.quote(script)], data=package())
    # Resolve and persist the official Python image digest before the build.
    remote = '''set -eu
cd /opt/vllm-streamlit-client
if ! sudo docker buildx version >/dev/null 2>&1; then
  sudo apt-get update -qq
  sudo env DEBIAN_FRONTEND=noninteractive apt-get install -y -qq --no-install-recommends docker-buildx
fi
sudo docker buildx version >/dev/null
sudo docker pull python:3.12-slim >/dev/null
IMAGE=$(sudo docker image inspect python:3.12-slim --format '{{index .RepoDigests 0}}')
printf 'PYTHON_IMAGE=%s\\n' "$IMAGE" | sudo tee .env >/dev/null
sudo docker compose build --quiet
sudo docker compose up -d
'''
    run(ssh+['bash -c '+shlex.quote(remote)],
        error_log=ROOT/'.runtime/install-monitoring-error.log')
    print('Checking Streamlit startup on loopback port 8501 (up to 120 seconds)…', flush=True)
    run(ssh+['sudo -n python3 -'], data=monitoring_readiness_program().encode(),
        error_log=ROOT/'.runtime/install-monitoring-error.log')
    print('Monitoring app healthy on the VM’s loopback port 8501.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=['mac', 'monitoring'])
    parser.add_argument('--poc-config', type=Path, default=ROOT.parents[1]/'poc-config.json')
    parser.add_argument('--connection', type=Path, help='Mac only: use an already provisioned connection without OCI/Terraform.')
    parser.add_argument('--port', type=int, default=8501, help='Mac browser port; the remote monitoring app remains on loopback 8501.')
    parser.add_argument('--reuse', action='store_true', help='Monitoring only: reuse its installed app without rebuilding.')
    parser.add_argument('--stop', action='store_true', help='Monitoring only: stop this app and exit.')
    args = parser.parse_args()
    if not 1024 <= args.port <= 65535:
        parser.error('Use a local port between 1024 and 65535.')
    if args.mode == 'mac' and (args.stop or args.reuse):
        parser.error('--stop and --reuse apply only to monitoring mode.')
    if args.mode == 'monitoring' and args.connection:
        parser.error('--connection is for Mac mode.')
    if not args.stop:
        require_free(args.port)
    runtime = ROOT/'.runtime'
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime.chmod(0o700)
    directory = runtime/(datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-'+args.mode)
    directory.mkdir(mode=0o700)
    print('Private launcher logs:',directory,flush=True)
    if args.connection:
        profile = args.connection.resolve()
    else:
        cfg, context, known, key = deployment(args.lab_config)
        role = 'vllm' if args.mode == 'mac' else 'monitoring'
        ssh = access(role,cfg,context,known,key,directory)
        if args.mode == 'monitoring':
            if args.stop:
                run(ssh+['sudo -n docker compose -f /opt/vllm-streamlit-client/compose.yml stop'])
                print('Stopped the Streamlit application only.',flush=True)
                return
            if not args.reuse:
                install_monitoring(ssh,context)
            forward(ssh,args.port,8501,directory)
            profile = None
        else:
            # Capture key material directly into process memory, never terminal/log output.
            extract = "from pathlib import Path;v=dict(l.split('=',1) for l in Path('/etc/vllm/server.env').read_text().splitlines() if l and not l.startswith('#'));print(v['VLLM_API_KEY'],end='')"
            secret = run(ssh+['sudo -n python3 -c '+shlex.quote(extract)])
            certificate = run(ssh+['sudo -n cat /etc/nginx/tls/server.crt'])
            key_path, ca_path = directory/'api-key', directory/'gpu.crt'
            private_write(key_path,secret)
            CREDENTIALS.append(key_path)
            private_write(ca_path,certificate)
            target = free_port()
            forward(ssh,target,443,directory)
            connection = {'host':'127.0.0.1','port':target,'tls_hostname':'inference.lab',
                          'ca_file':str(ca_path),'key_file':str(key_path),'label':'Mac · encrypted Bastion tunnel',
                          'model':'qwen3-8b','context_limit':4096,
                          'grafana_url':context['monitoring']['grafana_url']['value']}
            profile = directory/'connection.json'
            private_write(profile,json.dumps(connection))
            private_write(runtime/'latest-mac.json',json.dumps({'connection_file':str(profile),'url':f'http://localhost:{args.port}'}))
    if args.mode == 'mac':
        from client.chat import ChatClient
        from client.settings import Connection
        client = ChatClient(Connection.load(profile),15)
        try:
            models=client.models()
            print('Verified private TLS and authenticated model discovery:',', '.join(models),flush=True)
        finally:
            client.close()
        env = dict(os.environ,POC_CONNECTION_CONFIG=str(profile),STREAMLIT_BROWSER_GATHER_USAGE_STATS='false')
        process=spawn([sys.executable,'-m','streamlit','run',str(ROOT/'app.py'),
                       '--server.address=127.0.0.1',f'--server.port={args.port}','--server.headless=true'],directory/'streamlit.log',env)
        wait_port(args.port,process,60)
    # The browser connection is local HTTP inside the encrypted SSH path when remote.
    deadline=time.monotonic()+60
    while True:
        try:
            with urllib.request.urlopen(f'http://127.0.0.1:{args.port}/_stcore/health',timeout=5) as r:
                if r.status==200:break
        except OSError:
            if time.monotonic()>deadline:raise RuntimeError('Streamlit did not pass its health check.') from None
            time.sleep(1)
    print(f'Open http://localhost:{args.port} in your browser.',flush=True)
    print('Keep this command running. Control-C closes only its app/tunnels; monitoring mode leaves the remote app running.',flush=True)
    while True:
        if any(p.poll() is not None for p in PROCESSES):
            raise RuntimeError('An owned tunnel or application stopped. Restart the launcher and check its private logs.')
        time.sleep(2)


if __name__=='__main__':
    try:
        main()
    except KeyboardInterrupt:
        print('\nClosing this launcher’s temporary connections…',flush=True)
    except Exception as error:
        print('Error:',str(error),file=sys.stderr,flush=True)
        sys.exit(1)

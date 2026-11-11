#!/usr/bin/env python3
"""Deploy reviewed host configs over verified Bastion SSH tunnels.

Open GPU SSH on localhost:2222 and monitoring SSH on localhost:2223 first.
The inference key passes directly between subprocesses and is never printed.
"""
import argparse
import io
import json
from pathlib import Path
import subprocess
import tarfile
import shlex

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--known-hosts', type=Path, required=True)
parser.add_argument('--stage', choices=['gpu', 'monitoring', 'all'], default='all')
parser.add_argument('--key', type=Path, default=Path.home()/'.ssh/id_rsa')
parser.add_argument('--context-file', type=Path, required=True)
parser.add_argument('--gpu-port', type=int, default=2222)
parser.add_argument('--monitoring-port', type=int, default=2223)
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent
context = json.loads(args.context_file.read_text())
gpu_ip = context['vllm']['private_ip']['value']
monitoring_ip = context['monitoring']['private_ip']['value']
bastion_cidr = context['shared']['bastion_subnet_cidr']['value']


def host_port(port):
    return args.gpu_port if port == 2222 else args.monitoring_port


def ssh(port, command, data=None, capture=False):
    return subprocess.run([
        'ssh', '-F', '/dev/null', '-i', str(args.key),
        '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
        '-o', f'UserKnownHostsFile={args.known_hosts}',
        *(['-o', 'HostKeyAlias=' + context['vllm' if port == 2222 else 'monitoring']['instance_id']['value']]
          if context else []),
        '-p', str(host_port(port)), 'ubuntu@127.0.0.1', command],
        input=data, check=True, stdout=subprocess.PIPE if capture else None)


def upload(port, destination, paths):
    archive = io.BytesIO()
    with tarfile.open(fileobj=archive, mode='w') as tar:
        for relative, local in paths:
            paths_to_add = sorted(local.rglob('*')) if local.is_dir() else [local]
            for source in paths_to_add:
                if not source.is_file():
                    continue
                name = str(Path(relative)/source.relative_to(local)) if local.is_dir() else relative
                content = source.read_text().replace('__GPU_PRIVATE_IP__', gpu_ip)
                content = content.replace('__MONITORING_PRIVATE_IP__', monitoring_ip)
                content = content.replace('__BASTION_SUBNET_CIDR__', bastion_cidr).encode()
                info = tarfile.TarInfo(name)
                info.size, info.mode = len(content), 0o644
                tar.addfile(info, io.BytesIO(content))
    ssh(port, f'sudo install -d -m 755 {destination}')
    ssh(port, f'sudo tar -xf - -C {destination}', archive.getvalue())


if args.stage in ('gpu', 'all'):
    upload(2222, '/opt/vllm-monitoring', [
        (name, root/'runtime/monitoring'/name) for name in
        ['gpu-compose.yml', 'dcgm-counters.csv', 'install-gpu.sh', 'host-health.py']
    ] + [('pin-monitoring-images.py', root/'scripts/pin-monitoring-images.py')])
    ssh(2222, 'sudo bash /opt/vllm-monitoring/install-gpu.sh')

if args.stage in ('monitoring', 'all'):
    output = context['monitoring'] if context else json.loads(subprocess.check_output([
        'terraform', f'-chdir={root / "terraform/monitoring"}', 'output', '-json']))
    hostname = output['grafana_url']['value'].removeprefix('https://')
    upload(2223, '/opt/monitoring', [
        (name, root/'runtime/monitoring'/name) for name in
        ['compose.yml', 'prometheus.yml', 'grafana', 'inference-probe.py',
         'install-monitoring.sh', 'validate-monitoring.py',
         'inference-load.py', 'summarize-load.py']
    ] + [('pin-monitoring-images.py', root/'scripts/pin-monitoring-images.py')])
    certificate = ssh(2222, 'sudo cat /etc/nginx/tls/server.crt', capture=True).stdout
    ssh(2223, 'sudo install -d -m 700 /etc/monitoring')
    ssh(2223, 'sudo sh -c "umask 077; cat > /etc/monitoring/gpu.crt"', certificate)
    credential = ssh(2222, 'sudo cat /etc/vllm/server.env', capture=True).stdout
    # Do not persist credentials on the laptop, put them in arguments or log them.
    if not credential.startswith(b'VLLM_API_KEY=') or credential.count(b'\n') > 1:
        raise RuntimeError('Unexpected inference credential format')
    credential = credential.rstrip(b'\n') + f'\nINFERENCE_URL=https://{gpu_ip}\n'.encode()
    ssh(2223, 'sudo sh -c "umask 077; cat > /etc/monitoring/inference.env"', credential)
    del credential
    ssh(2223, f'sudo env GRAFANA_HOST={shlex.quote(hostname)} bash /opt/monitoring/install-monitoring.sh')

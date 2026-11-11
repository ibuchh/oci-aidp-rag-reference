#!/usr/bin/env python3
"""Create a short-lived OCI Bastion tunnel using the deployed Terraform outputs."""
import argparse
import json
from pathlib import Path
import subprocess
import time

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--profile', default='DEFAULT')
parser.add_argument('--region', default='eu-frankfurt-1')
parser.add_argument('--key', type=Path, default=Path.home() / '.ssh/id_rsa')
parser.add_argument('--target-port', type=int, choices=[22, 443], default=22)
parser.add_argument('--local-port', type=int, default=2222)
parser.add_argument('--target', choices=['vllm', 'monitoring'], default='vllm')
parser.add_argument('--known-hosts', type=Path,
                    help='Use an existing verified SSH known_hosts file')
parser.add_argument('--context-file', type=Path, required=True,
                    help='Deployment outputs JSON written by inference_stack')
parser.add_argument('--accept-new-bastion-host', action='store_true',
                    help='TOFU for the public Bastion host only; reject changed keys')
args = parser.parse_args()
root = Path(__file__).resolve().parent.parent


def terraform_outputs(name):
    return json.loads(subprocess.check_output(
        ['terraform', f'-chdir={root / "infra" / name}', 'output', '-json'], text=True))


def oci(*arguments):
    return json.loads(subprocess.check_output(
        ['oci', '--profile', args.profile, '--region', args.region, *arguments], text=True))['data']


if args.context_file:
    context = json.loads(args.context_file.read_text())
    shared = context['shared']
    vm = context[args.target]
else:
    shared = terraform_outputs('shared')
    vm = terraform_outputs(args.target)
session = oci(
    'bastion', 'session', 'create-port-forwarding',
    '--bastion-id', shared['bastion_id']['value'],
    '--target-private-ip', vm['private_ip']['value'],
    '--target-port', str(args.target_port),
    '--ssh-public-key-file', str(args.key) + '.pub',
    '--session-ttl', '10800', '--display-name', f'vllm-{args.target}-tunnel')
session_id = session['id']
print(f'Creating temporary session: {session_id}', flush=True)
deadline = time.monotonic() + 180
while time.monotonic() < deadline:
    session = oci('bastion', 'session', 'get', '--session-id', session_id)
    if session['lifecycle-state'] == 'ACTIVE':
        break
    if session['lifecycle-state'] in {'FAILED', 'DELETED'}:
        raise SystemExit(f'Session failed: {session.get("lifecycle-details")}')
    time.sleep(3)
else:
    raise SystemExit('Timed out waiting for Bastion session activation')

print(f'Forwarding 127.0.0.1:{args.local_port} to private port {args.target_port}. Keep this process running.', flush=True)
command = [
    'ssh', '-F', '/dev/null', '-i', str(args.key),
    '-o', 'IdentitiesOnly=yes', '-o', 'ExitOnForwardFailure=yes',
    '-o', 'ServerAliveInterval=30', '-o', 'ConnectTimeout=15', '-N',
    '-L', f'127.0.0.1:{args.local_port}:{vm["private_ip"]["value"]}:{args.target_port}',
    f'{session_id}@host.bastion.{args.region}.oci.oraclecloud.com',
]
if args.known_hosts:
    command[1:1] = ['-o', f'UserKnownHostsFile={args.known_hosts}',
                    '-o', 'StrictHostKeyChecking=' +
                    ('accept-new' if args.accept_new_bastion_host else 'yes')]
try:
    for attempt in range(5):
        started = time.monotonic()
        result = subprocess.run(command)
        if (result.returncode != 255 or attempt == 4
                or time.monotonic() - started > 15):
            result.check_returncode()
            break
        print('Retrying SSH after Bastion activation propagation...', flush=True)
        time.sleep(5)
except KeyboardInterrupt:
    pass
finally:
    subprocess.run(['oci', '--profile', args.profile, '--region', args.region,
                    'bastion', 'session', 'delete', '--session-id', session_id,
                    '--force'], check=False, stdout=subprocess.DEVNULL)

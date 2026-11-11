"""Bastion-based host setup used by inference_stack; no keys are printed or passed in argv."""
import contextlib
import json
import os
from pathlib import Path
import shlex
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request

from inference_stack import PocError, ROOT, STACKS, private_json


def free_port():
    with socket.socket() as connection:
        connection.bind(('127.0.0.1', 0))
        return connection.getsockname()[1]


class Access:
    def __init__(self, lab, context, role, known_hosts):
        self.lab, self.context, self.role, self.known_hosts = lab, context, role, known_hosts
        self.port = free_port()
        self.process = None
        self.output = None

    def __enter__(self):
        self.output = (self.lab.run/f'{self.role}-bastion.log').open('w')
        self.process = subprocess.Popen([
            sys.executable, str(ROOT/'scripts/bastion-tunnel.py'),
            '--profile', self.lab.config['oci_profile'], '--region', self.lab.config['region'],
            '--key', self.lab.config['ssh_private_key_path'], '--target', self.role,
            '--context-file', str(self.lab.run/'outputs.json'),
            '--known-hosts', str(self.known_hosts), '--accept-new-bastion-host',
            '--local-port', str(self.port)], stdout=self.output, stderr=self.output,
            start_new_session=True)
        try:
            deadline = time.monotonic()+240
            while time.monotonic() < deadline:
                if self.process.poll() is not None:
                    raise PocError(f'Bastion failed. Inspect {self.output.name}; check OCI-specific egress allowlist.')
                try:
                    with socket.create_connection(('127.0.0.1', self.port), timeout=1):
                        break
                except OSError:
                    time.sleep(2)
            else:
                raise PocError('Bastion tunnel did not become ready within four minutes.')
            self.verify_host_key()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        if self.process and self.process.poll() is None:
            os.killpg(self.process.pid, signal.SIGINT)
            try:
                self.process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                self.process.wait(timeout=10)
        if self.output:
            self.output.close()
        # Also remove a session if the helper was interrupted before entering its SSH finally block.
        if self.output:
            import re
            text = Path(self.output.name).read_text()
            ids = set(re.findall(r'Creating temporary session: (ocid1\.bastionsession\.[\w.-]+)', text))
            for session_id in ids:
                try:
                    session = self.lab.oci('bastion', 'session', 'get', '--session-id', session_id)
                    if session and session.get('lifecycle-state') in {'DELETING', 'DELETED'}:
                        continue
                    self.lab.oci('bastion', 'session', 'delete', '--session-id', session_id,
                                 '--force', capture=False)
                except PocError:
                    print(f'Inspect temporary Bastion session cleanup: {session_id}', flush=True)

    def scan_host_key(self):
        """Wait for guest SSH, not just the local forwarding socket."""
        timeout = min(600, self.lab.config.get('bootstrap_timeout_seconds', 600))
        deadline = time.monotonic() + timeout
        waiting = False
        while time.monotonic() < deadline:
            if self.process is not None and self.process.poll() is not None:
                raise PocError(f'Bastion tunnel exited while waiting for {self.role} SSH. '
                               f'Inspect {self.lab.run}/{self.role}-bastion.log')
            try:
                scan = self.lab.command(
                    ['ssh-keyscan', '-T', '15', '-t', 'ed25519',
                     '-p', str(self.port), '127.0.0.1'],
                    capture=True, accepted=(0, 1), timeout=20)
            except subprocess.TimeoutExpired:
                scan = ''
            keys = [line.split() for line in scan.splitlines()
                    if not line.startswith('#')]
            if any(len(key) == 3 and key[1] == 'ssh-ed25519' for key in keys):
                return scan
            if not waiting:
                print(f'{self.role}: waiting up to {timeout}s for guest SSH readiness...', flush=True)
                waiting = True
            time.sleep(5)
        raise PocError(f'{self.role}: no SSH host key received within {timeout}s. '
                       'Check guest boot, SSH service and Bastion-to-instance port 22. '
                       'No host key accepted; infrastructure retained.')

    def verify_host_key(self):
        instance = self.context[self.role]['instance_id']['value']
        scan = self.scan_host_key()
        # A verified pin remains authoritative after boot console logs rotate.
        # Never silently replace an existing pin, even if another capture exists.
        pinned = [line.split() for line in self.known_hosts.read_text().splitlines()
                  if line.startswith(instance+' ')] if self.known_hosts.exists() else []
        if pinned:
            scanned = [line.split() for line in scan.splitlines() if ' ssh-ed25519 ' in line]
            if (len(pinned) != 1 or len(scanned) != 1 or
                    pinned[0][1:3] != scanned[0][1:3]):
                raise PocError(f'SSH host key differs from the verified pin: {instance}. No key accepted.')
            print(f'{self.role}: SSH identity matched its previously verified instance pin.', flush=True)
            return
        histories = self.lab.oci('compute', 'console-history', 'list',
                                '--compartment-id', self.lab.config['compartment_id'],
                                '--instance-id', instance, '--all')
        # OCI CLI can return no output or data:null for an empty collection.
        if histories is None:
            histories = []
        if not isinstance(histories, list):
            raise PocError('Unexpected OCI console-history response; cannot verify SSH host identity.')
        own_history = None
        usable = [h for h in histories if h['lifecycle-state'] == 'SUCCEEDED']
        if usable:
            history = sorted(usable, key=lambda h: h['time-created'])[-1]
        else:
            history = self.lab.oci('compute', 'console-history', 'capture', '--instance-id', instance)
            own_history = history['id']
        try:
            deadline = time.monotonic()+120
            while history['lifecycle-state'] != 'SUCCEEDED':
                if history['lifecycle-state'] == 'FAILED' or time.monotonic() > deadline:
                    raise PocError('OCI console capture failed; cannot verify SSH host identity.')
                time.sleep(3)
                history = self.lab.oci('compute', 'console-history', 'get',
                                       '--instance-console-history-id', history['id'])
            console_file = self.lab.run/f'{self.role}-console.txt'
            self.lab.command(['oci', '--profile', self.lab.config['oci_profile'],
                              '--region', self.lab.config['region'], 'compute', 'console-history',
                              'get-content', '--instance-console-history-id', history['id'],
                              '--length', '1000000', '--file', console_file])
            key_file = self.lab.run/f'{self.role}-host-key.pub'
            key_file.write_text(scan)
            fingerprint = self.lab.command(['ssh-keygen', '-lf', key_file, '-E', 'sha256'], capture=True).split()[1]
            if fingerprint not in console_file.read_text(errors='replace'):
                raise PocError(f'SSH host fingerprint does not match OCI console evidence: {instance}. No key accepted.')
            key = next(line.split() for line in scan.splitlines() if ' ssh-ed25519 ' in line)
            alias = instance
            current = self.known_hosts.read_text() if self.known_hosts.exists() else ''
            kept = [line for line in current.splitlines() if not line.startswith(alias+' ')]
            kept.append(f'{alias} {key[1]} {key[2]}')
            self.known_hosts.write_text('\n'.join(kept)+'\n')
            self.known_hosts.chmod(0o600)
            print(f'{self.role}: SSH identity verified through OCI console: {fingerprint}', flush=True)
        finally:
            if own_history:
                self.lab.oci('compute', 'console-history', 'delete',
                             '--instance-console-history-id', own_history, '--force', capture=False)

    def ssh(self, command, data=None, capture=False, timeout=None):
        args = ['ssh', '-F', '/dev/null', '-i', self.lab.config['ssh_private_key_path'],
                '-o', 'IdentitiesOnly=yes', '-o', 'StrictHostKeyChecking=yes',
                '-o', f'UserKnownHostsFile={self.known_hosts}',
                '-o', 'HostKeyAlias='+self.context[self.role]['instance_id']['value'],
                '-o', 'ServerAliveInterval=30', '-o', 'ConnectTimeout=15',
                '-p', str(self.port), 'ubuntu@127.0.0.1', command]
        result = subprocess.run(args, input=data, stdout=subprocess.PIPE if capture else None,
                                timeout=timeout, check=True)
        return result.stdout if capture else None


def configure_hosts(lab):
    context = {s: lab.output(s) for s in STACKS}
    if lab.config['grafana_hostname']:
        context['monitoring']['grafana_url']['value'] = 'https://'+lab.config['grafana_hostname']
    private_json(lab.run/'outputs.json', context)
    known_hosts = lab.meta/'known_hosts'
    known_hosts.touch(mode=0o600, exist_ok=True)
    known_hosts.chmod(0o600)
    print('Opening temporary Bastion access; first contact to the public Bastion uses SSH accept-new.', flush=True)
    with contextlib.ExitStack() as stack:
        gpu = stack.enter_context(Access(lab, context, 'vllm', known_hosts))
        monitor = stack.enter_context(Access(lab, context, 'monitoring', known_hosts))
        timeout = lab.config['bootstrap_timeout_seconds']
        print('Waiting for GPU cloud-init/model download and CPU cloud-init. This can take many minutes.', flush=True)
        gpu.ssh('sudo cloud-init status --wait --long', timeout=timeout)
        monitor.ssh('sudo cloud-init status --wait --long', timeout=timeout)
        revision = shlex.quote(lab.config['model_revision'])
        gpu.ssh('sudo systemctl cat vllm | grep -F -- '+revision+' >/dev/null')
        ready = b'''import time,urllib.request
for attempt in range(120):
 try:
  with urllib.request.urlopen('http://127.0.0.1:8000/health',timeout=5) as r:
   if r.status==200:
    print('GPU model ready',flush=True)
    break
 except Exception:pass
 if attempt%6==0:print('Waiting for model readiness...',flush=True)
 time.sleep(5)
else:raise SystemExit('Model failed to become ready within ten minutes')
'''
        gpu.ssh('sudo python3 -', data=ready, timeout=660)
        result = subprocess.run([
            sys.executable, str(ROOT/'scripts/deploy-monitoring.py'), '--context-file', str(lab.run/'outputs.json'),
            '--known-hosts', str(known_hosts), '--key', lab.config['ssh_private_key_path'],
            '--gpu-port', str(gpu.port), '--monitoring-port', str(monitor.port)], timeout=2400)
        if result.returncode:
            raise PocError('Host monitoring configuration failed. Cloud resources remain; fix the log and rerun.')
        # Wait for two scrapes and the provisioned Grafana dashboards, then validate actual queries.
        checks = b'''import subprocess,time
for attempt in range(18):
 r=subprocess.run(['python3','/opt/monitoring/validate-monitoring.py'],capture_output=True)
 if r.returncode==0:
  print(r.stdout.decode(),flush=True)
  break
 time.sleep(10)
else:raise SystemExit('Monitoring validation failed; inspect targets, probe and Grafana logs')
'''
        validation = monitor.ssh('sudo python3 -', data=checks, capture=True, timeout=240)
        (lab.run/'host-validation.json').write_bytes(validation)
        url = context['monitoring']['grafana_url']['value']
        deadline = time.monotonic()+300
        while True:
            try:
                with urllib.request.urlopen(url+'/login', timeout=15) as response:
                    if response.status == 200:
                        break
            except (OSError, urllib.error.URLError):
                if time.monotonic() > deadline:
                    raise PocError(f'Public HTTPS is not ready at {url}; check DNS and ACME. Infrastructure retained.')
                time.sleep(5)
        password = monitor.ssh('sudo cat /etc/monitoring/grafana-secrets/admin_password', capture=True).decode().strip()
        credentials = lab.meta/'grafana-login.txt'
        credentials.write_text(f'Grafana: {url}\nUsername: admin\nPassword: {password}\n')
        credentials.chmod(0o600)
        del password
        private_json(lab.meta/'outputs.json', context)
        print(f'\nReady: {url}\nLogin saved locally (0600): {credentials}', flush=True)

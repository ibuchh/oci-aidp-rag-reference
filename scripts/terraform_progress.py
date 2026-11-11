"""Stream Terraform lifecycle progress while retaining raw output in a private log."""
import math
import os
from pathlib import Path
import queue
import re
import subprocess
import threading
import time

STREAM_COMMANDS = {'init', 'validate', 'plan', 'apply', 'destroy'}
ANSI = re.compile(r'\x1b\[[0-?]*[ -/]*[@-~]')
RESOURCE = re.compile(
    r'^([A-Za-z0-9_.\[\]"-]+): '
    r'((?:Creating|Modifying|Destroying|Reading)\.\.\.'
    r'|Still (?:creating|modifying|destroying|reading)\.\.\. \[[0-9hms.]+ elapsed\]'
    r'|(?:Creation|Modifications|Destruction|Read) complete after [0-9hms.]+)')
SUMMARY = re.compile(
    r'^(?:Plan: \d+ to add, \d+ to change, \d+ to destroy\.'
    r'|Apply complete! Resources: \d+ added, \d+ changed, \d+ destroyed\.'
    r'|Destroy complete! Resources: \d+ destroyed\.)$')


def progress_line(line, log_path):
    """Allow only lifecycle messages; provider diagnostics can contain secrets."""
    line = ANSI.sub('', line).strip()
    match = RESOURCE.match(line)
    if match:
        return f'{match[1]}: {match[2]}'
    if SUMMARY.fullmatch(line):
        return line
    if line == 'No changes. Your infrastructure matches the configuration.':
        return line
    if line.startswith('Error:'):
        return f'Terraform reported an error. Private details: {log_path}'
    if line.startswith('Warning:'):
        return f'Terraform reported a warning. Private details: {log_path}'
    return None


def run_terraform(argv, *, log_path, env=None, timeout=None, label='Terraform',
                  poll=None, interval=30):
    """Drain output continuously, flush logs, and keep machine-readable calls separate."""
    log_path = Path(log_path)
    fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    os.fchmod(fd, 0o600)
    started = time.monotonic()
    chunks = []
    events = queue.Queue()
    print(f'{label}: started. Private log: {log_path}', flush=True)
    with os.fdopen(fd, 'a') as log, subprocess.Popen(
            [str(a) for a in argv], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, env=env, bufsize=1) as process:
        def read_output():
            try:
                for line in process.stdout:
                    events.put(line)
            finally:
                events.put(None)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        next_report = started + interval
        try:
            while True:
                now = time.monotonic()
                if timeout is not None and now - started >= timeout:
                    raise subprocess.TimeoutExpired(argv, timeout)
                if now >= next_report:
                    print(f'{label}: still running [{int(now-started)}s elapsed]', flush=True)
                    if poll is not None:
                        try:
                            message = poll()
                        except Exception:
                            message = 'AIDP status check unavailable; Terraform continues.'
                        if message:
                            print(message, flush=True)
                    next_report = time.monotonic() + interval
                try:
                    line = events.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line is None:
                    break
                chunks.append(line)
                log.write(line)
                log.flush()
                message = progress_line(line, log_path)
                if message:
                    print(message, flush=True)
            code = process.wait(timeout=None if timeout is None else max(0.1, timeout-(time.monotonic()-started)))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            raise
        finally:
            # Popen's context manager preserves normal signal/interrupt handling.
            if process.poll() is not None:
                reader.join(timeout=2)
                process.stdout.close()
    print(f'{label}: finished with exit {code} [{int(time.monotonic()-started)}s elapsed]', flush=True)
    return subprocess.CompletedProcess(argv, code, ''.join(chunks), '')


def aidp_progress(cfg):
    """Read only the selected POC's control-plane status; never print raw responses."""
    import json
    base = ['oci', '--profile', cfg['oci_profile'], '--region', cfg['region'],
            '--connection-timeout', '3', '--read-timeout', '5', '--no-retry']
    def items(*args):
        result = subprocess.run(base + list(args), capture_output=True, text=True, timeout=12)
        if result.returncode:
            raise RuntimeError('AIDP progress query failed')
        data = json.loads(result.stdout)['data'] if result.stdout.strip() else []
        return data.get('items', []) if isinstance(data, dict) else data or []
    platforms = items('ai-data-platform', 'ai-data-platform-collection', 'list-ai-data-platforms',
                      '--compartment-id', cfg['compartment_id'], '--display-name', cfg['name'], '--all')
    owned = [p for p in platforms if p.get('display-name') == cfg['name']
             and p.get('freeform-tags', {}).get('Project') == cfg['name']]
    if len(owned) != 1:
        return 'AIDP: waiting for the uniquely identified POC instance.'
    requests = items('ai-data-platform', 'work-request', 'list',
                     '--compartment-id', cfg['compartment_id'], '--resource-id', owned[0]['id'], '--all')
    if not requests:
        return 'AIDP: waiting for work-request status.'
    latest = max(requests, key=lambda r: r.get('time-accepted') or '')
    operation, status = latest.get('operation-type', ''), latest.get('status', '')
    if not re.fullmatch('[A-Z_]+', operation) or not re.fullmatch('[A-Z_]+', status):
        return 'AIDP: work-request status received; details remain private.'
    percent = latest.get('percent-complete')
    suffix = (f' ({percent:.1f}%)' if isinstance(percent, (int, float))
              and math.isfinite(percent) and 0 <= percent <= 100 else '')
    return f'AIDP {operation}: {status}{suffix}'

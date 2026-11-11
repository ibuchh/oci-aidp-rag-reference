#!/usr/bin/env python3
"""Export bounded systemd states without giving a container access to D-Bus."""
import calendar
from pathlib import Path
import subprocess
import time

lines = ['# HELP vllm_host_service_active Service is active on the GPU host.',
         '# TYPE vllm_host_service_active gauge']
for unit in ['vllm', 'nginx', 'nvidia-dcgm', 'vllm-firewall']:
    result = subprocess.run(['systemctl', 'is-active', '--quiet', unit+'.service'])
    lines.append(f'vllm_host_service_active{{unit="{unit}.service"}} {int(result.returncode == 0)}')
try:
    expiry = subprocess.check_output([
        'openssl', 'x509', '-in', '/etc/nginx/tls/server.crt', '-noout', '-enddate'],
        text=True).strip().split('=', 1)[1]
    seconds = calendar.timegm(time.strptime(expiry, '%b %d %H:%M:%S %Y %Z'))
    lines.extend(['# TYPE vllm_inference_certificate_expiry_timestamp_seconds gauge',
                  f'vllm_inference_certificate_expiry_timestamp_seconds {seconds}'])
except (OSError, ValueError, subprocess.CalledProcessError):
    pass
lines.extend(['# TYPE vllm_host_health_timestamp_seconds gauge',
              f'vllm_host_health_timestamp_seconds {time.time():.3f}'])
path = Path('/var/lib/vllm-monitoring/textfile/health.prom')
temporary = path.with_suffix('.tmp')
temporary.write_text('\n'.join(lines)+'\n')
temporary.chmod(0o644)
temporary.replace(path)

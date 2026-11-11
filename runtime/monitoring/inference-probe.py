#!/usr/bin/env python3
"""Five-minute bounded inference probe. Credentials and content are never logged."""
import json
import os
from pathlib import Path
import ssl
import time
import urllib.request

started = time.monotonic()
success = 0
try:
    payload = json.dumps({
        "model": "qwen3-8b",
        "messages": [{"role": "user", "content": "Reply with the word OK."}],
        "temperature": 0, "max_tokens": 16,
    }).encode()
    request = urllib.request.Request(
        os.environ['INFERENCE_URL'] + '/v1/chat/completions', data=payload,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + os.environ['VLLM_API_KEY']})
    context = ssl.create_default_context(cafile='/etc/monitoring/gpu.crt')
    with urllib.request.urlopen(request, context=context, timeout=60) as response:
        data = json.load(response)
    success = int(bool(data.get('choices', [{}])[0].get('message', {}).get('content')))
except Exception:
    # Failure detail is deliberately bounded; no request headers or bodies in logs.
    print('Authenticated inference probe failed; check TLS, service and routing.')
duration = time.monotonic() - started
metrics = (
    '# HELP inference_probe_success Last authenticated inference probe succeeded.\n'
    '# TYPE inference_probe_success gauge\n'
    f'inference_probe_success {success}\n'
    '# HELP inference_probe_duration_seconds Last probe total client duration.\n'
    '# TYPE inference_probe_duration_seconds gauge\n'
    f'inference_probe_duration_seconds {duration:.6f}\n'
    '# HELP inference_probe_timestamp_seconds Last completed probe Unix time.\n'
    '# TYPE inference_probe_timestamp_seconds gauge\n'
    f'inference_probe_timestamp_seconds {time.time():.3f}\n')
destination = Path('/var/lib/monitoring/textfile/inference.prom')
temporary = destination.with_suffix('.tmp')
temporary.write_text(metrics)
temporary.chmod(0o644)
temporary.replace(destination)
print(f'Inference probe success={success} duration_seconds={duration:.3f}')

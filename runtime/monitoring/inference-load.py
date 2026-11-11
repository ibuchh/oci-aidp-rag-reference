#!/usr/bin/env python3
"""Bounded synthetic streaming traffic for the lab's Grafana dashboards.

Run on the monitoring VM with the same environment file as inference-probe.
Credentials, prompts and generated response content are never logged.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import datetime
import itertools
import json
import os
from pathlib import Path
import ssl
import threading
import time
import urllib.error
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--output-dir', type=Path, required=True)
parser.add_argument('--max-requests', type=int, default=500)
args = parser.parse_args()
if not 1 <= args.max_requests <= 1000:
    parser.error('max-requests must be between 1 and 1000')
args.output_dir.mkdir(parents=True, exist_ok=True)
context = ssl.create_default_context(cafile='/etc/monitoring/gpu.crt')
endpoint = os.environ['INFERENCE_URL'].rstrip('/')
headers = {'Content-Type': 'application/json',
           'Authorization': 'Bearer ' + os.environ['VLLM_API_KEY']}
lock = threading.Lock()
stopping = threading.Event()
sequence = itertools.count(1)
results = []
consecutive_errors = 0
phases = [('warmup', 60, 1), ('steady', 150, 4),
          ('queued', 150, 8), ('cooldown', 60, 2)]


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def query(expression):
    import urllib.parse
    url = 'http://127.0.0.1:9090/api/v1/query?' + urllib.parse.urlencode({'query': expression})
    with urllib.request.urlopen(url, timeout=10) as response:
        return json.load(response)['data']['result']


def snapshot():
    return {name: query(expression) for name, expression in {
        'success': 'sum(vllm:request_success_total)',
        'prompt_tokens': 'sum(vllm:prompt_tokens_total)',
        'generation_tokens': 'sum(vllm:generation_tokens_total)',
        'gpu_memory_bytes': 'DCGM_FI_DEV_FB_USED_BYTES',
        'gpu_utilization': 'DCGM_FI_DEV_GPU_UTIL_RATIO',
    }.items()}


def save_summary(state):
    successes = [r for r in results if r['success']]
    summary = {'state': state, 'started_at_utc': started_at,
               'updated_at_utc': now(), 'requests': len(results),
               'succeeded': len(successes), 'failed': len(results)-len(successes),
               'prompt_tokens_reported': sum(r['prompt_tokens'] for r in successes),
               'completion_tokens_reported': sum(r['completion_tokens'] for r in successes),
               'phases': phase_records,
               'note': 'Synthetic dashboard demonstration, not a customer capacity benchmark.'}
    p = args.output_dir/'summary.json'
    temporary = p.with_suffix('.tmp')
    temporary.write_text(json.dumps(summary, indent=2)+'\n')
    temporary.replace(p)


def worker(phase, deadline):
    global consecutive_errors
    while time.monotonic() < deadline and not stopping.is_set():
        with lock:
            number = next(sequence)
            if number > args.max_requests:
                stopping.set()
                return
        variant = number % 3
        rows = [4, 16, 32][variant] if phase != 'warmup' else 4
        max_tokens = [128, 256, 512][variant] if phase != 'warmup' else 128
        records = '\n'.join(
            f'Synthetic site {i}: capacity {70+(number+i)%25} units; '
            f'usage {30+(number*3+i)%35} units; delay {1+(i+number)%9} ms; '
            'planned action: review resource allocation and service responsiveness.'
            for i in range(rows))
        prompt = (
            f'Synthetic monitoring demonstration request {number}. '
            'Use only the fictional records below. Write a detailed operational report '
            'with numbered findings, capacity observations, latency considerations, '
            'and practical follow-up actions. Explain each finding fully. '
            'Aim for 600 words; do not call external tools.\n' + records)
        body = json.dumps({'model': 'qwen3-8b',
                           'messages': [{'role': 'user', 'content': prompt}],
                           'temperature': 0.3, 'max_tokens': max_tokens,
                           'stream': True, 'stream_options': {'include_usage': True}}).encode()
        started = time.monotonic()
        first_token = None
        got_content = False
        done = False
        usage = {}
        finish_reason = None
        error = None
        status = None
        try:
            request = urllib.request.Request(endpoint+'/v1/chat/completions',
                                             data=body, headers=headers)
            with urllib.request.urlopen(request, context=context, timeout=120) as response:
                status = response.status
                for line in response:
                    if stopping.is_set():
                        break
                    if not line.startswith(b'data: '):
                        continue
                    payload = line[6:].strip()
                    if payload == b'[DONE]':
                        done = True
                        break
                    chunk = json.loads(payload)
                    if chunk.get('usage'):
                        usage = chunk['usage']
                    for choice in chunk.get('choices', []):
                        if choice.get('delta', {}).get('content'):
                            got_content = True
                            if first_token is None:
                                first_token = time.monotonic()-started
                        if choice.get('finish_reason'):
                            finish_reason = choice['finish_reason']
            if not (done and got_content):
                error = 'incomplete_stream'
        except urllib.error.HTTPError as exc:
            status, error = exc.code, 'http_error'
        except Exception as exc:
            error = type(exc).__name__
        record = {'phase': phase, 'request': number, 'completed_at_utc': now(),
                  'success': error is None, 'http_status': status,
                  'duration_seconds': round(time.monotonic()-started, 4),
                  'client_ttft_seconds': round(first_token, 4) if first_token is not None else None,
                  'prompt_tokens': usage.get('prompt_tokens', 0),
                  'completion_tokens': usage.get('completion_tokens', 0),
                  'max_tokens': max_tokens, 'finish_reason': finish_reason,
                  'error': error}
        with lock:
            results.append(record)
            consecutive_errors = 0 if record['success'] else consecutive_errors+1
            with (args.output_dir/'requests.jsonl').open('a') as output:
                output.write(json.dumps(record)+'\n')
            if consecutive_errors >= 5:
                stopping.set()
            save_summary('running')
        time.sleep(0.2)


started_at = now()
phase_records = []
ready_request = urllib.request.Request(endpoint+'/v1/models', headers=headers)
with urllib.request.urlopen(ready_request, context=context, timeout=15) as response:
    assert any(m['id'] == 'qwen3-8b' for m in json.load(response)['data'])
assert all(v['value'][1] == '1' for v in query('up'))
(args.output_dir/'metrics-before.json').write_text(json.dumps(snapshot(), indent=2)+'\n')
print(f'Workload started {started_at}; 7 minutes of phases, at most 8 clients.', flush=True)
for name, seconds, concurrency in phases:
    if stopping.is_set():
        break
    record = {'phase': name, 'started_at_utc': now(), 'planned_seconds': seconds,
              'concurrency': concurrency}
    phase_records.append(record)
    print(f'Phase {name}: {seconds}s, concurrency={concurrency}', flush=True)
    deadline = time.monotonic()+seconds
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        tasks = [executor.submit(worker, name, deadline) for _ in range(concurrency)]
        while not all(t.done() for t in tasks):
            time.sleep(5)
        for task in tasks:
            task.result()
    record['finished_at_utc'] = now()
    record['requests'] = sum(r['phase'] == name for r in results)
    save_summary('running')
    print(f'Phase {name} completed; total requests={len(results)} failures='
          f'{sum(not r["success"] for r in results)}', flush=True)
save_summary('aborted' if stopping.is_set() else 'completed')
(args.output_dir/'metrics-after.json').write_text(json.dumps(snapshot(), indent=2)+'\n')
print(f'Workload finished {now()}; requests={len(results)} failures='
      f'{sum(not r["success"] for r in results)}', flush=True)

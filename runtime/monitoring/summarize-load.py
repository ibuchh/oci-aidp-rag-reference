#!/usr/bin/env python3
"""Summarize a completed synthetic run and collect its Prometheus time series."""
import argparse
import datetime
import json
import math
from pathlib import Path
import urllib.parse
import urllib.request

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('directory', type=Path)
args = parser.parse_args()
summary = json.loads((args.directory/'summary.json').read_text())
assert summary['state'] == 'completed', summary['state']
records = [json.loads(line) for line in (args.directory/'requests.jsonl').read_text().splitlines()]
start = datetime.datetime.fromisoformat(summary['started_at_utc']).timestamp()
end = datetime.datetime.fromisoformat(summary['updated_at_utc']).timestamp()
expressions = {
    'generation_tokens_per_second': 'sum(rate(vllm:generation_tokens_total[1m]))',
    'prompt_tokens_per_second': 'sum(rate(vllm:prompt_tokens_total[1m]))',
    'requests_running': 'sum(vllm:num_requests_running)',
    'requests_waiting': 'sum(vllm:num_requests_waiting)',
    'kv_cache_ratio': 'max(vllm:kv_cache_usage_perc)',
    'gpu_utilization_ratio': 'DCGM_FI_DEV_GPU_UTIL_RATIO',
    'gpu_power_watts': 'DCGM_FI_DEV_BOARD_POWER_WATTS',
    'gpu_temperature_celsius': 'DCGM_FI_DEV_GPU_TEMP_CELSIUS',
    'server_ttft_p95_seconds': 'histogram_quantile(0.95, sum by (le) (rate(vllm:time_to_first_token_seconds_bucket[1m])))',
    'server_queue_p95_seconds': 'histogram_quantile(0.95, sum by (le) (rate(vllm:request_queue_time_seconds_bucket[1m])))',
}
series = {}
peaks = {}
for name, expression in expressions.items():
    parameters = urllib.parse.urlencode({'query': expression, 'start': start, 'end': end, 'step': 15})
    with urllib.request.urlopen('http://127.0.0.1:9090/api/v1/query_range?'+parameters, timeout=15) as response:
        data = json.load(response)
    assert data['status'] == 'success'
    series[name] = data['data']['result']
    values = [float(value) for s in series[name] for _, value in s['values'] if math.isfinite(float(value))]
    peaks[name] = max(values) if values else None


def percentiles(values):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return {f'p{p}': round(values[max(0, math.ceil(len(values)*p/100)-1)], 4) for p in (50, 95, 99)}


phase_summary = []
for phase in summary['phases']:
    requests = [r for r in records if r['phase'] == phase['phase']]
    successes = [r for r in requests if r['success']]
    phase_summary.append({**phase, 'succeeded': len(successes),
                          'failed': len(requests)-len(successes),
                          'client_ttft_seconds': percentiles([r['client_ttft_seconds'] for r in successes]),
                          'client_duration_seconds': percentiles([r['duration_seconds'] for r in successes])})
report = {**summary, 'elapsed_seconds': round(end-start, 2),
          'phases': phase_summary, 'observed_prometheus_peaks': peaks,
          'client_ttft_seconds': percentiles([r['client_ttft_seconds'] for r in records if r['success']]),
          'client_duration_seconds': percentiles([r['duration_seconds'] for r in records if r['success']]),
          'grafana_from_ms': int((start-60)*1000), 'grafana_to_ms': int((end+60)*1000)}
(args.directory/'prometheus-series.json').write_text(json.dumps(series, indent=2)+'\n')
(args.directory/'report.json').write_text(json.dumps(report, indent=2)+'\n')
print(json.dumps(report, indent=2))

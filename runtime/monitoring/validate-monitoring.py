#!/usr/bin/env python3
"""Check scrape health, real dashboard queries and authenticated Grafana APIs."""
import base64
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import time
import urllib.parse
import urllib.request


def get(url, headers=None):
    request = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.load(response)


def query(expression):
    url = 'http://127.0.0.1:9090/api/v1/query?' + urllib.parse.urlencode({'query': expression})
    result = get(url)
    assert result['status'] == 'success', result
    return result['data']['result']


targets = get('http://127.0.0.1:9090/api/v1/targets')['data']['activeTargets']
assert len(targets) == 5 and all(t['health'] == 'up' for t in targets), targets
assert query('count(DCGM_FI_DEV_FB_USED_BYTES)')[0]['value'][1] == '2'
assert query('inference_probe_success')[0]['value'][1] == '1'
assert float(query('time() - inference_probe_timestamp_seconds')[0]['value'][1]) < 360
assert len(query('vllm_host_service_active == 1')) == 4
assert float(query('time() - vllm_host_health_timestamp_seconds')[0]['value'][1]) < 120
failed_collectors = query('node_scrape_collector_success == 0')
assert not failed_collectors, failed_collectors
password = Path('/etc/monitoring/grafana-secrets/admin_password').read_text().strip()
auth = base64.b64encode(('admin:' + password).encode()).decode()
headers = {'Authorization': 'Basic ' + auth}
del password
assert get('http://127.0.0.1:3000/api/health')['database'] == 'ok'
dashboards = sorted(Path('/opt/monitoring/grafana/dashboards').glob('*.json'))
checks = []
for path in dashboards:
    dashboard = json.loads(path.read_text())
    result = get('http://127.0.0.1:3000/api/dashboards/uid/' + dashboard['uid'], headers)
    assert result['dashboard']['title'] == dashboard['title']
    for panel in dashboard['panels']:
        for target in panel.get('targets', []):
            expression = target['expr'].replace('$__rate_interval', '5m')
            expression = expression.replace('$model', 'qwen3-8b').replace('$node', '__GPU_PRIVATE_IP__')
            checks.append((dashboard['uid'], panel['title'], expression))


def evaluate(check):
    uid, title, expression = check
    values = query(expression)
    return {'dashboard': uid, 'panel': title, 'series': len(values),
            'expression': expression,
            'optional': title.endswith('(optional)'),
            'nonfinite': sum(v.get('value', [0, 'NaN'])[1] in ('NaN', '+Inf', '-Inf') for v in values)}


with ThreadPoolExecutor(max_workers=6) as executor:
    results = list(executor.map(evaluate, checks))
empty = [r for r in results if not r['series'] and not r['optional']]
assert not empty, empty
summary = {'checked_at_utc': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'targets': [{'job': t['labels']['job'], 'health': t['health']} for t in targets],
           'gpu_count': 2, 'authenticated_probe_success': True,
           'grafana_dashboards': len(dashboards), 'query_checks': len(results),
           'optional_unavailable': [r['panel'] for r in results if not r['series']],
           'queries': results}
print(json.dumps(summary, indent=2))

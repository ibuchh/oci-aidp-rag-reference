#!/usr/bin/env python3
"""Run as root on the VM; credentials remain on the VM and are never printed."""
import json
from pathlib import Path
import subprocess
import tempfile

key = Path('/etc/vllm/server.env').read_text().strip().split('=', 1)[1]
with tempfile.NamedTemporaryFile(mode='w', dir='/run') as header:
    header.write(f'Authorization: Bearer {key}\n')
    header.flush()

    def request(body):
        result = subprocess.check_output([
            'curl', '--silent', '--show-error', '--fail', '--max-time', '120',
            '--cacert', '/etc/nginx/tls/server.crt',
            '--resolve', 'inference.lab:443:127.0.0.1',
            '--header', '@' + header.name,
            '--header', 'Content-Type: application/json',
            '--data-binary', json.dumps(body),
            'https://inference.lab/v1/chat/completions',
        ], text=True)
        return result if body.get('stream') else json.loads(result)

    streamed = request({
        'model': 'qwen3-8b', 'stream': True, 'max_tokens': 32, 'temperature': 0,
        'messages': [{'role': 'user', 'content': 'Reply with STREAM_OK only.'}],
    })
    assert 'data: [DONE]' in streamed, 'Missing SSE completion marker'
    chunks = [json.loads(line[6:]) for line in streamed.splitlines()
              if line.startswith('data: ') and line != 'data: [DONE]']
    assert any(chunk['choices'][0].get('delta', {}).get('content') for chunk in chunks if chunk['choices'])
    print('Authenticated HTTPS streaming passed')

    tools = [{
        'type': 'function', 'function': {
            'name': 'get_weather', 'description': 'Return the current weather for a city.',
            'parameters': {'type': 'object', 'properties': {'city': {'type': 'string'}}, 'required': ['city']},
        },
    }]
    messages = [{'role': 'user', 'content': 'Use get_weather to find the current weather in Paris. Do not guess the weather.'}]
    result = request({'model': 'qwen3-8b', 'messages': messages, 'tools': tools,
                      'tool_choice': 'auto', 'max_tokens': 128, 'temperature': 0})
    message = result['choices'][0]['message']
    call = message['tool_calls'][0]
    assert call['function']['name'] == 'get_weather'
    assert json.loads(call['function']['arguments'])['city'].lower() == 'paris'
    print('Automatic tool selection and JSON arguments passed')
    # Synthetic local result; this does not contact a weather service or AIDP.
    messages += [message, {'role': 'tool', 'tool_call_id': call['id'],
                          'content': '{"city":"Paris","temperature_c":20,"condition":"sunny"}'}]
    followup = request({'model': 'qwen3-8b', 'messages': messages,
                        'tools': tools, 'tool_choice': 'none', 'max_tokens': 128, 'temperature': 0})
    answer = followup['choices'][0]['message']['content']
    assert '20' in answer, answer
    print('Synthetic tool-result round trip passed')

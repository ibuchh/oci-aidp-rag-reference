from dataclasses import dataclass, field
import json
import re
import ssl
import time

import urllib3


class InferenceError(Exception):
    pass


def redact(value, secret=''):
    if isinstance(value, dict):
        return {k: ('[REDACTED]' if any(t in k.lower() for t in ('authorization', 'api_key', 'password', 'secret')) else redact(v, secret)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v, secret) for v in value]
    if isinstance(value, str):
        if secret:
            value = value.replace(secret, '[REDACTED]')
        return re.sub(r'(?i)Bearer\s+[^\s"\x27<>]+', 'Bearer [REDACTED]', value)
    return value


@dataclass
class Result:
    content: str = ''
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)
    elapsed: float = 0
    ttft: float | None = None
    response_id: str | None = None
    model: str | None = None
    chunk_count: int = 0

    def inspection(self):
        return {'id': self.response_id, 'model': self.model,
                'choices': [{'message': {'role': 'assistant', 'content': self.content},
                             'finish_reason': self.finish_reason}],
                'usage': self.usage or None, 'stream_chunk_count': self.chunk_count,
                'client_measurements': {'elapsed_seconds': round(self.elapsed, 3),
                                        'first_content_seconds': self.ttft}}


def sse_objects(response):
    """Parse bounded SSE events independently of network chunk boundaries."""
    pending = bytearray()
    data = []
    for chunk in response.stream(amt=1024, decode_content=True):
        pending.extend(chunk)
        if len(pending) > 2_000_000:
            raise InferenceError('The server sent an oversized stream event.')
        while b'\n' in pending:
            line, _, remaining = pending.partition(b'\n')
            pending = bytearray(remaining)
            line = line.rstrip(b'\r')
            if not line:
                if data:
                    payload = b'\n'.join(data).decode('utf-8')
                    data = []
                    if payload == '[DONE]':
                        yield None
                        return
                    try:
                        yield json.loads(payload)
                    except (ValueError, UnicodeError):
                        raise InferenceError('The server sent an invalid streaming response.') from None
            elif line.startswith(b'data:'):
                data.append(line[5:].lstrip(b' '))
                if sum(map(len, data)) > 2_000_000:
                    raise InferenceError('The server sent an oversized stream event.')
    raise InferenceError('The response stream ended before its completion marker. The partial answer was not saved.')


class ChatClient:
    def __init__(self, connection, timeout=120):
        if not 5 <= timeout <= 300:
            raise ValueError('Timeout must be 5–300 seconds.')
        self.connection, self.timeout = connection, timeout
        self.key = connection.credential()
        context = ssl.create_default_context(cafile=connection.ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        self.pool = urllib3.HTTPSConnectionPool(
            connection.host, connection.port, ssl_context=context,
            server_hostname=connection.tls_hostname,
            assert_hostname=connection.tls_hostname, maxsize=1, block=True)

    def close(self):
        self.pool.close()

    def _request(self, method, path, body=None):
        try:
            response = self.pool.request(
                method, path, body=json.dumps(body).encode() if body is not None else None,
                headers={'Authorization': 'Bearer ' + self.key,
                         'Host': self.connection.tls_hostname,
                         'Content-Type': 'application/json', 'Accept': 'application/json'},
                timeout=urllib3.Timeout(connect=min(self.timeout, 15), read=self.timeout),
                retries=False, redirect=False, preload_content=False,
                assert_same_host=False)
        except urllib3.exceptions.SSLError:
            raise InferenceError('TLS verification failed. Check the trusted certificate, hostname and expiry.') from None
        except urllib3.exceptions.HTTPError:
            raise InferenceError('Cannot reach the private API. Check the tunnel, endpoint and timeout.') from None
        if response.status != 200:
            code = response.status
            response.close()
            response.release_conn()
            descriptions = {401: 'Authentication failed. Check this connection’s credential.',
                            403: 'Access was denied by the inference endpoint.',
                            400: 'Request rejected. Check sampling values and the combined prompt/output context limit.',
                            404: 'Model or API path not found. Refresh the served model list.',
                            429: 'The endpoint is busy. Wait and retry.',
                            422: 'Request validation failed. Check the submitted settings.'}
            raise InferenceError(f'HTTP {code}: ' + descriptions.get(code, 'The inference endpoint could not complete this request.'))
        return response

    def models(self):
        response = self._request('GET', '/v1/models')
        try:
            data = json.loads(response.read(2_000_000))
            return [m['id'] for m in data['data'] if isinstance(m.get('id'), str)]
        except (ValueError, KeyError, TypeError):
            raise InferenceError('Model discovery returned an invalid response.') from None
        finally:
            response.close()
            response.release_conn()

    def generate(self, payload, result):
        """Yield text only; never log headers, prompts, model output or credentials."""
        start = time.monotonic()
        response = self._request('POST', '/v1/chat/completions', payload)
        try:
            if payload['stream']:
                for event in sse_objects(response):
                    if time.monotonic()-start > self.timeout:
                        raise InferenceError('Request deadline exceeded. The partial answer was not saved.')
                    if event is None:
                        break
                    if not isinstance(event, dict) or event.get('error'):
                        raise InferenceError('The server reported a streaming error.')
                    result.chunk_count += 1
                    result.response_id = event.get('id', result.response_id)
                    result.model = event.get('model', result.model)
                    if event.get('usage'):
                        result.usage = event['usage']
                    for choice in event.get('choices', []):
                        delta = choice.get('delta', {})
                        if delta.get('tool_calls'):
                            raise InferenceError('This client supports text chat. Tool-call execution is not enabled.')
                        text = delta.get('content') or ''
                        if text:
                            if result.ttft is None:
                                result.ttft = time.monotonic()-start
                            result.content += text
                            yield redact(text, self.key)
                        if choice.get('finish_reason'):
                            result.finish_reason = choice['finish_reason']
            else:
                data = json.loads(response.read(4_000_000))
                result.response_id, result.model = data.get('id'), data.get('model')
                choice = data['choices'][0]
                if choice['message'].get('tool_calls'):
                    raise InferenceError('This client supports text chat. Tool-call execution is not enabled.')
                result.content = choice['message'].get('content') or ''
                result.finish_reason, result.usage = choice.get('finish_reason'), data.get('usage') or {}
                yield redact(result.content, self.key)
            result.elapsed = time.monotonic()-start
            result.content = redact(result.content, self.key)
            if not result.content:
                raise InferenceError('The model returned no text answer.')
        except (urllib3.exceptions.HTTPError, OSError):
            raise InferenceError('The connection was interrupted or timed out. The partial answer was not saved.') from None
        except (ValueError, KeyError, TypeError, IndexError):
            raise InferenceError('The model returned an invalid chat response.') from None
        finally:
            response.close()
            response.release_conn()

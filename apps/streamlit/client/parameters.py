import math


RANGES = {
    'temperature': (0, 2), 'top_p': (0.000001, 1),
    'presence_penalty': (-2, 2), 'frequency_penalty': (-2, 2),
    'min_p': (0, 1), 'repetition_penalty': (0.000001, 2),
}


def build_payload(model, messages, max_tokens, stream, overrides=None, context_limit=4096):
    if not model or not isinstance(model, str):
        raise ValueError('Select a served model.')
    if type(max_tokens) is not int or not 1 <= max_tokens < context_limit:
        raise ValueError(f'Output limit must be between 1 and {context_limit - 1}.')
    if not messages or not any(m.get('role') == 'user' for m in messages):
        raise ValueError('Enter a prompt first.')
    if any(m.get('role') not in {'system', 'user', 'assistant'} or
           not isinstance(m.get('content'), str) for m in messages):
        raise ValueError('Messages must contain text and supported roles.')
    if sum(len(m['content']) for m in messages) > 100_000:
        raise ValueError('Conversation is too large. Clear history or shorten your prompt.')
    payload = {'model': model, 'messages': messages, 'max_tokens': max_tokens, 'stream': bool(stream)}
    if stream:
        payload['stream_options'] = {'include_usage': True}
    for key, value in (overrides or {}).items():
        if key in RANGES:
            low, high = RANGES[key]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
                raise ValueError(f'Invalid {key}.')
        elif key == 'top_k':
            if type(value) is not int or (value != -1 and not 1 <= value <= 100_000):
                raise ValueError('Top-k must be -1 (disabled) or a positive integer.')
        elif key == 'seed':
            if type(value) is not int or not 0 <= value <= 2**31-1:
                raise ValueError('Seed must be an integer between 0 and 2147483647.')
        elif key == 'stop':
            if not isinstance(value, list) or not 1 <= len(value) <= 4 or any(not isinstance(v, str) or not v or len(v) > 200 for v in value):
                raise ValueError('Use 1–4 nonempty stop strings, each at most 200 characters.')
        else:
            raise ValueError('Unsupported request setting.')
        payload[key] = value
    return payload

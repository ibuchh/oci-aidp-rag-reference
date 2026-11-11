"""Session-only chat UI. The app server owns endpoint and credential settings."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time

import streamlit as st

from client.chat import ChatClient, InferenceError, Result, redact
from client.parameters import build_payload
from client.settings import Connection
from client.appearance import apply_theme, brand, header, conversation_heading, empty_state, chat_avatar

st.set_page_config(page_title='Private AI · OCI POC', page_icon='💬', layout='wide')
apply_theme()

path = os.environ.get('POC_CONNECTION_CONFIG', '')
if not path:
    st.info('Start with ./run-local.sh or ./run-monitoring.sh. For an existing connection, set POC_CONNECTION_CONFIG to your protected profile.')
    st.stop()
try:
    connection = Connection.load(path)
except (ValueError, OSError, TypeError):
    st.error('The connection profile or trusted certificate is missing or invalid. Check the launcher’s terminal output.')
    st.stop()

if st.session_state.get('connection_id') != connection.identity:
    st.session_state.update(connection_id=connection.identity, messages=[], last_request=None,
                            models=[connection.model], active_model=connection.model, status='Not checked')

with st.sidebar:
    brand()
    st.header('Connection')
    st.caption(connection.label)
    st.code(f'HTTPS {connection.host}:{connection.port}\nVerified as {connection.tls_hostname}', language=None)
    if st.button('Check connection / refresh models', width='stretch', type='primary'):
        client = None
        try:
            client = ChatClient(connection, 15)
            models = client.models()
            if not models:
                raise InferenceError('The server returned no served models.')
            st.session_state.models = models
            st.session_state.status = 'Model API reachable'
        except (InferenceError, ValueError, OSError) as error:
            st.session_state.status = 'Connection check failed'
            st.error(str(error) if isinstance(error, InferenceError) else 'Check the protected credential and certificate files.')
        finally:
            if client:
                client.close()
    st.caption(st.session_state.status)
    st.divider()
    st.subheader('Model & generation')
    models = st.session_state.models
    model = st.selectbox('Model', models, key='model_choice')
    if model != st.session_state.active_model:
        st.session_state.update(active_model=model, messages=[], last_request=None)
        st.info('Conversation cleared for the selected model.')
    streaming = st.toggle('Stream the answer', value=True)
    maximum = st.number_input('Maximum output tokens', min_value=1,
                              max_value=connection.context_limit-1, value=256, step=32,
                              help='Output budget. Prompt, system instructions, chat history and output must fit the server’s combined context limit.')
    timeout = st.number_input('Timeout in seconds', min_value=5, max_value=300, value=120, step=5)
    overrides = {}
    with st.expander('Sampling controls'):
        for name, label, low, high, default, step in [
            ('temperature', 'Temperature', 0.0, 2.0, 0.7, 0.05),
            ('top_p', 'Top-p', 0.01, 1.0, 0.8, 0.01),
        ]:
            if st.checkbox('Override ' + label, key='enable_'+name):
                overrides[name] = st.slider(label, low, high, default, step, key=name)
        st.caption('Unchecked controls are omitted, preserving the server/model defaults.')
    with st.expander('Penalties and advanced controls'):
        for name, label, low, high, default in [
            ('presence_penalty', 'Presence penalty', -2.0, 2.0, 0.0),
            ('frequency_penalty', 'Frequency penalty', -2.0, 2.0, 0.0),
            ('repetition_penalty', 'Repetition penalty', 0.01, 2.0, 1.0),
            ('min_p', 'Minimum probability', 0.0, 1.0, 0.0),
        ]:
            if st.checkbox('Override ' + label, key='enable_'+name):
                overrides[name] = st.number_input(label, min_value=low, max_value=high, value=default, step=0.05, key=name)
        if st.checkbox('Override top-k'):
            overrides['top_k'] = st.number_input('Top-k (-1 disables)', min_value=-1, max_value=100000, value=20)
        if st.checkbox('Set seed'):
            overrides['seed'] = st.number_input('Seed', min_value=0, max_value=2147483647, value=42,
                                                help='Best-effort reproducibility, not a guarantee.')
        if st.checkbox('Set stop strings'):
            stops = st.text_area('One stop string per line', height=90)
            overrides['stop'] = stops.splitlines()
    if st.button('Clear conversation', width='stretch'):
        st.session_state.messages = []
        st.session_state.last_request = None
    st.caption('History stays in this browser session’s server memory. Reloading or reconnecting may clear it.')
    if connection.grafana_url:
        st.link_button('Open Grafana', connection.grafana_url, width='stretch')

header(model, connection.context_limit, connection.label)

with st.expander('System instructions', expanded=False):
    system = st.text_area('Optional instructions for this conversation', value='You are a helpful assistant. Answer clearly and concisely.', height=95)
    st.caption(f'The combined context limit is {connection.context_limit:,} tokens. Counts shown after a request come from the API; no character-based token estimate is used.')

conversation_heading()
empty_placeholder = st.empty()
if not st.session_state.messages:
    with empty_placeholder.container():
        empty_state()
for message in st.session_state.messages:
    with st.chat_message(message['role'], avatar=chat_avatar(message['role'])):
        st.markdown(message['content'])

prompt = st.chat_input('Write your question…', max_chars=16000)
if prompt:
    empty_placeholder.empty()
    client = None
    result = Result()
    st.session_state.last_request = None
    started_at = datetime.now(timezone.utc).isoformat()
    request_ms = int(time.time()*1000)
    try:
        client = ChatClient(connection, int(timeout))
        safe_prompt = redact(prompt, client.key)
        messages = ([{'role': 'system', 'content': redact(system, client.key)}] if system.strip() else [])
        messages += st.session_state.messages + [{'role': 'user', 'content': safe_prompt}]
        payload = build_payload(model, messages, int(maximum), streaming, overrides, connection.context_limit)
        st.session_state.last_request = {'started_at_utc': started_at,
                                        'request': redact(payload, client.key), 'response': None}
        with st.chat_message('user', avatar=chat_avatar('user')):
            st.markdown(safe_prompt)
        with st.chat_message('assistant', avatar=chat_avatar('assistant')):
            if streaming:
                st.write_stream(client.generate(payload, result))
            else:
                with st.spinner('Generating an answer…'):
                    answer = ''.join(client.generate(payload, result))
                st.markdown(answer)
        st.session_state.messages += [{'role': 'user', 'content': safe_prompt},
                                      {'role': 'assistant', 'content': result.content}]
        st.session_state.last_request['response'] = redact(result.inspection(), client.key)
        st.session_state.last_request['grafana_from_ms'] = request_ms-60000
        st.session_state.last_request['grafana_to_ms'] = int(time.time()*1000)+60000
    except (InferenceError, ValueError, OSError) as error:
        description = str(error) if isinstance(error, (InferenceError, ValueError)) else 'Check the local credential/certificate files.'
        if client:
            description = redact(description, client.key)
        st.error(description)
        st.caption('The failed exchange was not added to conversation history. You can correct the settings and submit again.')
        if st.session_state.last_request:
            st.session_state.last_request['error'] = description
    finally:
        if client:
            client.close()

last = st.session_state.last_request
if last:
    response = last.get('response')
    if response:
        measurements = response['client_measurements']
        usage = response.get('usage') or {}
        cols = st.columns(4)
        cols[0].metric('Client elapsed', f"{measurements['elapsed_seconds']:.2f} s")
        first = measurements['first_content_seconds']
        cols[1].metric('First content', f'{first:.2f} s' if first is not None else 'Not streamed')
        cols[2].metric('Prompt tokens', usage.get('prompt_tokens', 'Unavailable'))
        cols[3].metric('Output tokens', usage.get('completion_tokens', 'Unavailable'))
        output_tokens = usage.get('completion_tokens')
        elapsed = measurements['elapsed_seconds']
        rate = f' · Client output rate: {output_tokens/elapsed:.1f} tokens/s' if isinstance(output_tokens, int) and elapsed > 0 else ''
        st.caption(f"Finish reason: {response['choices'][0]['finish_reason']} · Total tokens: {usage.get('total_tokens', 'unavailable')}{rate}")
        st.caption('Client timing includes the request path and queueing. It differs from server-only Grafana latency. “length” means the output cap was reached.')
        if connection.grafana_url:
            st.link_button('View this time window in Grafana', connection.grafana_url.rstrip('/') + '/d/vllm-inference?from=' + str(last['grafana_from_ms']) + '&to=' + str(last['grafana_to_ms']))
    with st.expander('Inspect last request / response (redacted)'):
        st.caption('Includes your prompt and model answer. Authorization headers and credentials are excluded. A streamed response is shown as an assembled summary.')
        st.json(last)

with st.expander('Export this session'):
    st.caption('Export is optional and includes your conversation. Nothing is saved automatically. Review the file before sharing it.')
    st.download_button('Download conversation JSON',
                       json.dumps({'messages': st.session_state.messages, 'last_request': last}, indent=2),
                       file_name='private-ai-conversation.json', mime='application/json', on_click='ignore')

with st.expander('About the POC serving configuration'):
    st.write('Configured reference profile: Qwen3-8B on two A10 GPUs, FP16, tensor parallelism 2, four scheduled sequences and an 85% GPU memory budget. Thinking is disabled by the deployed chat template.')
    st.caption('These are deployment settings, not live hardware discovery. Chat controls do not change GPU count, precision, context capacity or the server process. This app does not execute tools or run load tests.')

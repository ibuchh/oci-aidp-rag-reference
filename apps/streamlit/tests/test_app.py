import os
from pathlib import Path
import sys
import tempfile
import json
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from streamlit.testing.v1 import AppTest


class FakeClient:
    calls=[]
    key='test-secret-value'
    def __init__(self,*args,**kwargs):pass
    def close(self):pass
    def models(self):return ['qwen3-8b']
    def generate(self,payload,result):
        self.calls.append(payload)
        result.content='Hello from the private model.';result.finish_reason='stop'
        result.usage={'prompt_tokens':12,'completion_tokens':6,'total_tokens':18}
        result.elapsed=.5;result.ttft=.1 if payload['stream'] else None;result.model='qwen3-8b'
        yield result.content


class UI(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();folder=Path(self.temp.name)
        (folder/'ca').write_text('test')
        config={'host':'127.0.0.1','port':8443,'tls_hostname':'inference.lab','ca_file':str(folder/'ca')}
        p=folder/'connection.json';p.write_text(json.dumps(config))
        self.environment=patch.dict(os.environ,{'POC_CONNECTION_CONFIG':str(p)});self.environment.start()
        self.client=patch('client.chat.ChatClient',FakeClient);self.client.start();FakeClient.calls=[]

    def tearDown(self):
        self.client.stop();self.environment.stop();self.temp.cleanup()

    def app(self):return AppTest.from_file(str(ROOT/'app.py'),default_timeout=15).run()

    def test_submit_stream_render_usage_and_rerun_without_duplicate(self):
        app=self.app();self.assertFalse(app.exception)
        app.chat_input[0].set_value('Hello').run()
        self.assertFalse(app.exception);self.assertEqual(len(FakeClient.calls),1)
        self.assertEqual(len(app.session_state['messages']),2)
        self.assertEqual(app.metric[2].value,'12')
        app.run();self.assertEqual(len(FakeClient.calls),1)

    def test_clear_and_sessions_do_not_share_history(self):
        one=self.app();one.chat_input[0].set_value('Hello').run()
        two=self.app();self.assertEqual(two.session_state['messages'],[])
        clear=next(x for x in one.button if x.label=='Clear conversation')
        clear.click().run();self.assertEqual(one.session_state['messages'],[])

    def test_selected_override_is_sent_and_secrets_are_redacted(self):
        app=self.app()
        next(x for x in app.checkbox if x.label=='Override Temperature').check().run()
        next(x for x in app.slider if x.label=='Temperature').set_value(.25).run()
        app.chat_input[0].set_value('Do not repeat test-secret-value').run()
        self.assertFalse(app.exception)
        self.assertEqual(FakeClient.calls[0]['temperature'],.25)
        self.assertNotIn('test-secret-value',json.dumps(app.session_state['last_request']))
        self.assertNotIn('test-secret-value',json.dumps(app.session_state['messages']))

    def test_nonstream_request(self):
        app=self.app();app.toggle[0].set_value(False).run()
        app.chat_input[0].set_value('Hello').run()
        self.assertFalse(app.exception);self.assertFalse(FakeClient.calls[0]['stream'])
        self.assertEqual(app.metric[1].value,'Not streamed')

    def test_invalid_new_request_does_not_relabel_previous_response(self):
        app=self.app();app.chat_input[0].set_value('Hello').run()
        next(x for x in app.checkbox if x.label=='Set stop strings').check().run()
        app.chat_input[0].set_value('Second request').run()
        self.assertFalse(app.exception)
        self.assertEqual(len(FakeClient.calls),1)
        self.assertEqual(len(app.session_state['messages']),2)
        self.assertIsNone(app.session_state['last_request'])
        self.assertTrue(app.error)

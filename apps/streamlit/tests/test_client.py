import json
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from client.chat import ChatClient, InferenceError, Result, redact, sse_objects
from client.parameters import build_payload
from client.settings import Connection


class Parameters(unittest.TestCase):
    def test_unselected_values_preserve_server_defaults(self):
        data=build_payload('m',[{'role':'user','content':'hi'}],128,False)
        self.assertNotIn('temperature',data)
        self.assertNotIn('stream_options',data)

    def test_validate_advanced_and_protected_fields(self):
        for overrides in [{'top_k':0},{'min_p':1.5},{'temperature':float('nan')},
                          {'stop':['']},{'seed':-1},{'Authorization':'secret'}, {'model':'foreign'}]:
            with self.subTest(overrides=overrides),self.assertRaises(ValueError):
                build_payload('m',[{'role':'user','content':'hi'}],128,True,overrides)

    def test_output_does_not_claim_all_context_is_available(self):
        with self.assertRaises(ValueError):
            build_payload('m',[{'role':'user','content':'hi'}],4096,False)

    def test_redaction_in_nested_json_and_text(self):
        data={'Authorization':'Bearer abc','messages':[{'content':'a token-sensitive-value'}]}
        result=redact(data,'token-sensitive-value')
        self.assertNotIn('abc',json.dumps(result))
        self.assertNotIn('token-sensitive-value',json.dumps(result))

    def test_sse_handles_fragmented_utf8_and_multiline_frames(self):
        class Response:
            def stream(self,**kwargs):
                yield b': keepalive\r\ndata: {"choices":\r\ndata: [{"delta":{"content":"caf\xc3'
                yield b'\xa9"}}]}\r\n\r\ndata: [DONE]\r\n\r\n'
        values=list(sse_objects(Response()))
        self.assertEqual(values[0]['choices'][0]['delta']['content'],'café')
        self.assertIsNone(values[-1])

    def test_incomplete_stream_is_failure(self):
        class Response:
            def stream(self,**kwargs):yield b'data: {"choices":[]}\n\n'
        with self.assertRaises(InferenceError):list(sse_objects(Response()))


class HTTPS(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();cls.folder=Path(cls.temp.name)
        cls.cert=cls.folder/'cert.pem';cls.key=cls.folder/'private.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
                        '-keyout',str(cls.key),'-out',str(cls.cert),'-days','1',
                        '-subj','/CN=inference.test','-addext','subjectAltName=DNS:inference.test'],
                       check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def send_json(self,status,data):
                body=json.dumps(data).encode();self.send_response(status)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(body)))
                self.end_headers();self.wfile.write(body)
            def do_GET(self):
                if self.headers.get('Authorization')!='Bearer test-key':
                    return self.send_json(401,{'error':'test-key must never be displayed'})
                self.send_json(200,{'data':[{'id':'qwen3-8b'}]})
            def do_POST(self):
                data=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                if data['stream']:
                    body=(b'data: {"id":"test","model":"qwen3-8b","choices":[{"delta":{"content":"Hello"},"finish_reason":null}]}\n\n'
                          b'data: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4}}\n\n'
                          b'data: [DONE]\n\n')
                    self.send_response(200);self.send_header('Content-Type','text/event-stream')
                    self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
                else:
                    self.send_json(200,{'model':'qwen3-8b','choices':[{'message':{'content':'Hello'},'finish_reason':'stop'}],
                                        'usage':{'prompt_tokens':3,'completion_tokens':1,'total_tokens':4}})
        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                pass  # Intentional rejected TLS tests close their sockets.
        cls.server=QuietServer(('127.0.0.1',0),Handler)
        ctx=ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER);ctx.load_cert_chain(cls.cert,cls.key)
        cls.server.socket=ctx.wrap_socket(cls.server.socket,server_side=True)
        cls.thread=threading.Thread(target=cls.server.serve_forever,daemon=True);cls.thread.start()
        cls.credential=cls.folder/'token';cls.credential.write_text('test-key');cls.credential.chmod(0o600)

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown();cls.server.server_close();cls.thread.join();cls.temp.cleanup()

    def connection(self,hostname='inference.test'):
        return Connection('127.0.0.1',self.server.server_port,hostname,str(self.cert),str(self.credential))

    def test_tunnel_address_and_verified_tls_hostname_are_separate(self):
        client=ChatClient(self.connection(),10)
        try:self.assertEqual(client.models(),['qwen3-8b'])
        finally:client.close()

    def test_wrong_hostname_rejected(self):
        client=ChatClient(self.connection('wrong.test'),10)
        try:
            with self.assertRaisesRegex(InferenceError,'TLS verification'):client.models()
        finally:client.close()

    def test_untrusted_certificate_rejected(self):
        # A valid but unrelated certificate must not become a trust bypass.
        other = self.folder/'other.pem'
        subprocess.run(['openssl','req','-x509','-newkey','rsa:2048','-nodes',
                        '-keyout',str(self.folder/'other.key'),'-out',str(other),
                        '-days','1','-subj','/CN=other.test'],check=True,
                       stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        c=self.connection()
        c=Connection(c.host,c.port,c.tls_hostname,str(other),c.key_file)
        client=ChatClient(c,10)
        try:
            with self.assertRaisesRegex(InferenceError,'TLS verification'):client.models()
        finally:client.close()

    def test_unauthorized_does_not_echo_server_body(self):
        self.credential.write_text('wrong-key')
        client=ChatClient(self.connection(),10)
        try:
            with self.assertRaises(InferenceError) as error:client.models()
            self.assertIn('401',str(error.exception));self.assertNotIn('test-key',str(error.exception))
        finally:
            client.close();self.credential.write_text('test-key')

    def test_stream_and_nonstream_usage_measurements(self):
        for streaming in [True,False]:
            with self.subTest(streaming=streaming):
                client=ChatClient(self.connection(),10);result=Result()
                try:
                    body=build_payload('qwen3-8b',[{'role':'user','content':'hi'}],128,streaming)
                    text=''.join(client.generate(body,result))
                    self.assertEqual(text,'Hello');self.assertEqual(result.usage['total_tokens'],4)
                    self.assertGreater(result.elapsed,0)
                    self.assertEqual(result.ttft is None,not streaming)
                finally:client.close()

    def test_insecure_credential_permissions_rejected(self):
        self.credential.chmod(0o644)
        try:
            with self.assertRaisesRegex(ValueError,'chmod 600'):ChatClient(self.connection(),10)
        finally:self.credential.chmod(0o600)

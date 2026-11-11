import json
from pathlib import Path
import ssl
import sys
import unittest
from unittest.mock import patch
import urllib.error

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from private_client import PrivateClient, tool_result


CONFIG = {"base_url": "https://10.140.33.19/v1", "model": "qwen3-8b", "max_tokens": 512}


class IntegrationTest(unittest.TestCase):
    def test_refuses_public_http_and_embedded_credentials(self):
        for url in ("http://10.140.33.19/v1", "https://8.8.8.8/v1",
                    "https://key@10.140.33.19/v1", "https://10.140.33.19/v1?api_key=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                PrivateClient(dict(CONFIG, base_url=url), key="test")

    def test_tls_verification_and_proxy_bypass(self):
        with patch("private_client.urllib.request.build_opener"), \
                patch("private_client.urllib.request.HTTPSHandler") as handler, \
                patch("private_client.urllib.request.ProxyHandler") as proxy:
            PrivateClient(CONFIG, key="test")
            context = handler.call_args.kwargs["context"]
            self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
            self.assertTrue(context.check_hostname)
            proxy.assert_called_once_with({})

    def test_http_error_body_is_withheld(self):
        client = PrivateClient(CONFIG, key="secret-value")
        with patch.object(client.opener, "open", side_effect=urllib.error.HTTPError(
                client.endpoint, 401, "contains secret-value", {}, None)):
            with self.assertRaisesRegex(RuntimeError, "HTTP 401") as error:
                client.chat([{"role": "user", "content": "synthetic"}])
            self.assertNotIn("secret-value", str(error.exception))

    def test_request_uses_chat_endpoint_and_auth(self):
        client = PrivateClient(CONFIG, key="secret-value")
        class Reply:
            def __enter__(self): return self
            def __exit__(self, *args): return False
            def read(self, size):
                return json.dumps({"choices": [{"message": {"role": "assistant", "content": "ok"}}]}).encode()
        with patch.object(client.opener, "open", return_value=Reply()) as opened:
            client.chat([{"role": "user", "content": "synthetic"}])
            request = opened.call_args.args[0]
            self.assertEqual(request.full_url, "https://10.140.33.19/v1/chat/completions")
            self.assertEqual(request.get_header("Authorization"), "Bearer secret-value")
            self.assertEqual(json.loads(request.data)["model"], "qwen3-8b")

    def test_tool_requires_real_observed_call(self):
        client = PrivateClient(CONFIG, key="test")
        with patch.object(client, "chat", return_value=({"role": "assistant", "content": "48 GB"}, {})):
            with self.assertRaisesRegex(ValueError, "No tool call"):
                client.run("synthetic", tools=True, require_tool=True)

    def test_synthetic_tool_roundtrip_and_unknown_function_rejected(self):
        call = {"id": "call1", "type": "function", "function": {"name": "get_poc_inventory", "arguments": "{}"}}
        client = PrivateClient(CONFIG, key="test")
        with patch.object(client, "chat", side_effect=[
            ({"role": "assistant", "content": None, "tool_calls": [call]}, {}),
            ({"role": "assistant", "content": "Synthetic inventory: 48 GB total."}, {}),
        ]) as chat:
            self.assertTrue(client.run("synthetic", tools=True, require_tool=True)["tool_observed"])
            self.assertEqual(chat.call_args.args[0][-2]["role"], "tool")
        with self.assertRaises(ValueError):
            tool_result({"function": {"name": "run_shell", "arguments": "{}"}})



if __name__ == "__main__":
    unittest.main()

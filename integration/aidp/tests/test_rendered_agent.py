import asyncio
import base64
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from render_agent import render
from private_client import PrivateClient


class RenderedAgent(unittest.TestCase):
    def config(self):
        return {'base_url': 'https://inference.lab/v1', 'private_dns_hostname': 'inference.lab',
                'model': 'qwen3-8b', 'region': 'eu-frankfurt-1',
                'ca_pem': 'public certificate fixture', 'api_key_credential_name': 'poc_vllm_key',
                'api_key_credential_secret_id': 'ocid1.vaultsecret.oc1.fixture'}

    def test_generated_entry_runs_real_graph_and_validates_linked_tool_result(self):
        namespace = {'__name__': 'deployed_agent'}
        exec(compile(render(self.config()), '<generated-agent>', 'exec'), namespace)
        client = Mock()
        def run(prompt, tools=False):
            if not tools:
                return {'messages': [{'role':'assistant','content':'Private chat verified.'}]}
            return {'messages': [
                {'role':'assistant','tool_calls':[{'id':'call-1','function':{'name':'get_poc_inventory','arguments':'{}'}}]},
                {'role':'tool','tool_call_id':'call-1','content':json.dumps({
                    'synthetic':True,'gpus':2,'gpu_model':'NVIDIA A10','vram_gb_each':24})},
                {'role':'assistant','content':'The synthetic inventory has 48 GB total VRAM.'}]}
        client.run.side_effect = run
        namespace['PrivateClient'] = Mock(return_value=client)
        agent = namespace['AgentBasic'](); agent.setup()
        result = asyncio.run(agent.invoke('__POC_VERIFY__'))
        report = json.loads(result['messages'][0]['content'])
        self.assertEqual(report['status'], 'PASS')
        self.assertTrue(report['private_chat_verified'])
        self.assertTrue(report['synthetic_inventory_verified'])
        self.assertEqual(client.run.call_count, 2)

    def test_plaintext_key_fields_and_private_key_material_cannot_be_rendered(self):
        cfg = self.config(); cfg['api_key'] = 'private-token-fixture'
        with self.assertRaises(ValueError): render(cfg)
        cfg = self.config(); cfg['ca_pem'] = '-----BEGIN PRIVATE KEY-----'
        with self.assertRaises(ValueError): render(cfg)


class CredentialAndDns(unittest.TestCase):
    def test_only_the_explicit_private_dns_hostname_is_allowed(self):
        cfg = {'base_url':'https://inference.lab/v1','private_dns_hostname':'inference.lab',
               'model':'qwen3-8b','ca_pem':'public certificate fixture'}
        with patch('private_client.ssl.create_default_context') as context:
            client = PrivateClient(cfg, key='fixture-token')
        self.assertEqual(client.endpoint, 'https://inference.lab/v1/chat/completions')
        context.assert_called_once_with(cadata=cfg['ca_pem'])
        cfg['base_url'] = 'https://other.example/v1'
        with self.assertRaises(ValueError): PrivateClient(cfg, key='fixture-token')

    def test_vault_reference_lookup_uses_supported_helper_and_redacts_failures(self):
        getter = Mock(return_value={'secretId':'ocid1.vaultsecret.oc1.fixture',
                                   'secretContentBase64':base64.b64encode(b'fixture-token').decode()})
        with patch.dict(sys.modules, {'aidputils':SimpleNamespace(secrets=SimpleNamespace(get=getter))}), \
                patch.dict('os.environ', {}, clear=True):
            self.assertEqual(PrivateClient._read_key({'api_key_credential_name':'poc_vllm_key'}), 'fixture-token')
            getter.assert_called_once_with(name='poc_vllm_key')
            getter.side_effect = RuntimeError('private-token-fixture')
            with self.assertRaises(RuntimeError) as caught:
                PrivateClient._read_key({'api_key_credential_name':'poc_vllm_key'})
        self.assertNotIn('private-token-fixture', str(caught.exception))

    def test_vault_reference_cannot_silently_fall_back_to_another_key_source(self):
        with patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(ValueError):
                PrivateClient._read_key({'api_key_credential_name':'poc_vllm_key', 'api_key_secret_id':'other'})

    def test_vault_reference_must_match_the_configured_secret_id(self):
        getter = Mock(return_value={'secretId':'other-secret','secretContentBase64':base64.b64encode(b'fixture-token').decode()})
        with patch.dict(sys.modules, {'aidputils':SimpleNamespace(secrets=SimpleNamespace(get=getter))}), \
                patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(RuntimeError):
                PrivateClient._read_key({'api_key_credential_name':'poc_key','api_key_credential_secret_id':'expected-secret'})

    def test_invalid_base64_cannot_be_used_as_a_key(self):
        getter = Mock(return_value={'secretId':'expected-secret','secretContentBase64':'invalid!base64'})
        with patch.dict(sys.modules, {'aidputils':SimpleNamespace(secrets=SimpleNamespace(get=getter))}), \
                patch.dict('os.environ', {}, clear=True):
            with self.assertRaises(RuntimeError):
                PrivateClient._read_key({'api_key_credential_name':'poc_key'})


if __name__ == '__main__': unittest.main()

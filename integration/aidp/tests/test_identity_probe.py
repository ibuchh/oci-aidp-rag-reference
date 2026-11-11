import asyncio
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from probe_identity import AgentBasic, inspect_runtime, inspect_transport, inspect_credential_probe


class IdentityProbe(unittest.TestCase):
    def test_allowlists_claims_without_accessing_token_or_private_key(self):
        class Signer:
            def get_claim(self, name):
                return {"sub": "ocid1.aidataplatform.oc1.eu-frankfurt-1.fixture",
                        "res_tenant": "ocid1.tenancy.oc1..fixture",
                        "res_compartment": "ocid1.compartment.oc1..fixture",
                        "res_type": "aidataplatform"}.get(name)
            def get_security_token(self):
                raise AssertionError('Token contents must never be accessed')
        report = inspect_runtime(lambda: Signer())
        self.assertEqual(report['status'], 'SIGNER_AVAILABLE')
        self.assertEqual(report['identity_claims']['res_type'], 'aidataplatform')
        self.assertFalse(report['policy_values_verified'])

    def test_exception_messages_are_not_disclosed(self):
        report = inspect_runtime(mock.Mock(side_effect=ValueError('private-token-secret')))
        self.assertEqual(report['status'], 'RESOURCE_PRINCIPAL_UNAVAILABLE')
        self.assertNotIn('private-token-secret', json.dumps(report))

    def test_missing_claim_reader_does_not_fall_back_to_token_decoding(self):
        report = inspect_runtime(lambda: object())
        self.assertEqual(report['status'], 'CLAIM_READER_UNAVAILABLE')

    def test_missing_or_unsafe_claims_are_not_invented_or_echoed(self):
        class Signer:
            def get_claim(self, name):
                return {'sub': 'unstructured-token-value', 'res_type': 'unsafe\nvalue'}.get(name)
        report = inspect_runtime(lambda: Signer())
        self.assertEqual(report['identity_claims'], {})
        self.assertFalse(report['policy_values_verified'])

    def test_agent_entry_returns_metadata_report_without_fetching_secret(self):
        with mock.patch('probe_identity.inspect_runtime', return_value={'status': 'SIGNER_AVAILABLE'}):
            agent = AgentBasic(); agent.setup()
            result = asyncio.run(agent.invoke('Inspect identity'))
        self.assertEqual(json.loads(result['messages'][0]['content'])['status'], 'SIGNER_AVAILABLE')


class TransportProbe(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.path = self.root/'probe_network.json'
        self.ca = self.root/'inference-ca.crt'
        self.ca.write_text('public certificate fixture')
        self.config = {'base_url': 'https://10.160.33.19/v1', 'ca_file': self.ca.name}
        self.path.write_text(json.dumps(self.config))

    def test_private_401_passes_with_verified_tls_and_no_authorization(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError(self.config['base_url'], 401, 'unauthorized', {}, None)
        with mock.patch('probe_identity.ssl.create_default_context') as context, \
                mock.patch('probe_identity.urllib.request.build_opener', return_value=opener):
            report = inspect_transport(self.path)
        context.assert_called_once_with(cafile=str(self.ca.resolve()))
        request = opener.open.call_args.args[0]
        self.assertFalse(request.has_header('Authorization'))
        self.assertEqual(opener.open.call_args.kwargs['timeout'], 20)
        self.assertEqual(report['status'], 'PASS')
        self.assertTrue(report['tls_verified'])

    def test_public_endpoint_is_rejected_before_network(self):
        self.config['base_url'] = 'https://8.8.8.8/v1'
        self.path.write_text(json.dumps(self.config))
        with mock.patch('probe_identity.urllib.request.build_opener') as build:
            self.assertEqual(inspect_transport(self.path)['status'], 'FAIL')
        build.assert_not_called()

    def test_missing_auth_is_not_considered_enforced_when_http_200(self):
        response = mock.MagicMock()
        response.__enter__.return_value.status = 200
        opener = mock.Mock(); opener.open.return_value = response
        with mock.patch('probe_identity.ssl.create_default_context'), \
                mock.patch('probe_identity.urllib.request.build_opener', return_value=opener):
            report = inspect_transport(self.path)
        self.assertEqual(report['status'], 'UNEXPECTED_HTTP_STATUS')

    def test_tls_or_network_exception_text_is_not_disclosed(self):
        opener = mock.Mock(); opener.open.side_effect = ValueError('secret exception text')
        with mock.patch('probe_identity.ssl.create_default_context'), \
                mock.patch('probe_identity.urllib.request.build_opener', return_value=opener):
            report = inspect_transport(self.path)
        self.assertEqual(report['status'], 'FAIL')
        self.assertNotIn('secret exception text', json.dumps(report))

    def test_private_key_material_is_rejected(self):
        self.ca.write_text('-----BEGIN PRIVATE KEY-----')
        with mock.patch('probe_identity.ssl.create_default_context') as context:
            self.assertEqual(inspect_transport(self.path)['status'], 'FAIL')
        context.assert_not_called()

    def test_embedded_public_ca_works_without_adjacent_files(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.HTTPError('https://10.160.33.19/v1/models', 401, 'unauthorized', {}, None)
        embedded = {'base_url': 'https://10.160.33.19/v1', 'ca_pem': 'public certificate fixture'}
        with mock.patch('probe_identity.EMBEDDED_NETWORK', embedded), \
                mock.patch('probe_identity.ssl.create_default_context') as context, \
                mock.patch('probe_identity.urllib.request.build_opener', return_value=opener):
            report = inspect_transport(self.root/'missing.json')
        self.assertEqual(report['status'], 'PASS')
        context.assert_called_once_with(cadata=embedded['ca_pem'])

    def test_connection_failure_reports_reason_type_without_message(self):
        opener = mock.Mock()
        opener.open.side_effect = urllib.error.URLError(TimeoutError('internal secret exception message'))
        with mock.patch('probe_identity.ssl.create_default_context'), \
                mock.patch('probe_identity.urllib.request.build_opener', return_value=opener):
            report = inspect_transport(self.path)
        self.assertEqual(report['reason_type'], 'TimeoutError')
        self.assertNotIn('internal secret', json.dumps(report))


class CredentialProbe(unittest.TestCase):
    def test_getter_is_called_with_named_synthetic_credential_and_value_is_not_echoed(self):
        getter = mock.Mock(return_value='synthetic-poc-probe')
        with mock.patch.dict(sys.modules, {'aidputils': SimpleNamespace(secrets=SimpleNamespace(get=getter))}), \
                mock.patch('probe_identity.SYNTHETIC_CREDENTIAL_NAME', 'poc_probe'):
            report = inspect_credential_probe()
        getter.assert_called_once_with(name='poc_probe', key='value')
        self.assertEqual(report['status'], 'PASS')
        self.assertNotIn('synthetic-poc-probe', json.dumps(report))

    def test_unexpected_secret_contents_are_not_echoed(self):
        getter = mock.Mock(return_value='private-token-fixture')
        with mock.patch.dict(sys.modules, {'aidputils': SimpleNamespace(secrets=SimpleNamespace(get=getter))}), \
                mock.patch('probe_identity.SYNTHETIC_CREDENTIAL_NAME', 'poc_probe'):
            report = inspect_credential_probe()
        self.assertEqual(report['status'], 'UNEXPECTED_VALUE')
        self.assertNotIn('private-token-fixture', json.dumps(report))

    def test_credential_exception_text_is_redacted(self):
        getter = mock.Mock(side_effect=RuntimeError('private-token-fixture'))
        with mock.patch.dict(sys.modules, {'aidputils': SimpleNamespace(secrets=SimpleNamespace(get=getter))}), \
                mock.patch('probe_identity.SYNTHETIC_CREDENTIAL_NAME', 'poc_probe'):
            report = inspect_credential_probe()
        self.assertEqual(report['status'], 'FAIL')
        self.assertNotIn('private-token-fixture', json.dumps(report))


if __name__ == '__main__': unittest.main()

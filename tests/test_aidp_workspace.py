import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import aidp_workspace as module


class FakeApi:
    aidp_id = 'ocid1.aidataplatform.test'

    def __init__(self):
        self.calls = []
        self.detail = {'key': 'owned', 'isPrivateNetworkEnabled': True,
                       'networkConfigurationDetails': {'subnetId': 'subnet', 'nsgIds': ['nsg']}}
        self.list = []

    def get(self, path):
        return self.detail

    def items(self, path):
        return self.list

    def request(self, *args):
        self.calls.append(args)
        return dict(self.detail, lifecycleState='CREATING'), {}


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.cfg = {'name': 'test-poc'}
        self.api = FakeApi()
        self.patches = [patch.object(module, 'ROOT', self.root),
                        patch.object(module, 'output', return_value={
                            'workspace_subnet_id': 'subnet', 'workspace_nsg_id': 'nsg'})]
        for p in self.patches: p.start()
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: [p.stop() for p in reversed(self.patches)])

    def test_plan_does_not_mutate_cloud_or_write_inventory(self):
        w = module.Workspace(self.cfg, self.api)
        self.assertIn('planned', w.workspace())
        self.assertEqual(self.api.calls, [])
        self.assertFalse(w.path.exists())

    def test_same_name_without_ownership_cannot_be_adopted(self):
        self.api.list = [{'displayName': 'test-poc-private'}]
        with self.assertRaises(module.PocError):
            module.Workspace(self.cfg, self.api).workspace(apply=True)
        self.assertEqual(self.api.calls, [])

    def test_resume_reuses_owned_workspace_and_validates_private_binding(self):
        w = module.Workspace(self.cfg, self.api)
        w.workspace(apply=True)
        restored = module.Workspace(self.cfg, self.api)
        restored.workspace(apply=True)
        self.assertEqual(len(self.api.calls), 1)
        self.api.detail['networkConfigurationDetails']['nsgIds'] = ['other']
        with self.assertRaises(module.PocError): restored.workspace(apply=True)

    def test_retry_intent_is_saved_before_post(self):
        w = module.Workspace(self.cfg, self.api)
        def interrupted(*args):
            saved = json.loads(w.path.read_text())
            self.assertEqual(args[3], saved['tokens']['workspace'])
            raise TimeoutError()
        self.api.request = interrupted
        with self.assertRaises(TimeoutError): w.workspace(apply=True)
        self.assertNotIn('workspace', json.loads(w.path.read_text())['resources'])

    def test_inventory_cannot_cross_instances(self):
        w = module.Workspace(self.cfg, self.api)
        w.save()
        self.api.aidp_id = 'other'
        with self.assertRaises(module.PocError): module.Workspace(self.cfg, self.api)

    def test_signer_never_receives_non_oracle_agent_url(self):
        api = module.Api.__new__(module.Api)
        api.base = 'https://aidp.eu-frankfurt-1.oci.oraclecloud.com'
        api.cfg = {}
        for url in ('https://example.com/chat', 'http://aidp.eu-frankfurt-1.oci.oraclecloud.com/chat',
                    'https://eviloci.oraclecloud.com/chat',
                    'https://aidp.eu-frankfurt-1.oci.oraclecloud.com.evil.example/chat',
                    'https://user:password@aidp.eu-frankfurt-1.oci.oraclecloud.com/chat'):
            with self.subTest(url=url), patch.object(module.subprocess, 'run') as run:
                with self.assertRaises(module.PocError): api.request('POST', '', {}, endpoint=url)
                run.assert_not_called()

    def test_upload_cannot_use_a_different_compute(self):
        w = module.Workspace(self.cfg, self.api)
        w.saved['resources'] = {'workspace': {'key': 'owned'}, 'compute': {'key': 'compute'},
                                'agent': {'key': 'agent'}}
        def get(path):
            if '/agents/' in path: return {'type': 'CODE', 'computeKey': 'other'}
            return dict(self.api.detail, lifecycleState='ACTIVE')
        self.api.get = get
        with self.assertRaises(module.PocError): w.upload_probe()
        self.assertEqual(self.api.calls, [])

    def test_successful_empty_credential_response_is_reconciled_without_second_create(self):
        w = module.Workspace(self.cfg, self.api)
        self.api.items = Mock(side_effect=[[], [{'displayName': 'test_poc_credential_probe', 'key': 'credential'}]])
        self.api.request = Mock(return_value=({}, {}))
        w.prepare_credential_probe()
        w.prepare_credential_probe()
        self.assertEqual(w.saved['synthetic_credential']['key'], 'credential')
        self.api.request.assert_called_once()

    def prepare_connect_files(self, w):
        runtime = w.root/'runtime'; runtime.mkdir(parents=True)
        for name in ('runtime.json','inference-ca.crt','inference-secret.json'):
            (runtime/name).write_text('fixture')

    def test_connect_does_not_replace_a_verified_inference_agent_with_probe(self):
        w = module.Workspace(self.cfg, self.api); self.prepare_connect_files(w)
        w.saved.update(inference_entry={'path':'model.py'}, inference_verified=True)
        w.wait_active = Mock(return_value=True); w.invoke_inference = Mock(); w.playground_status = Mock()
        w.upload_probe = Mock(); w.apply = Mock(); w.redeploy_probe = Mock()
        w.connect()
        w.invoke_inference.assert_called_once()
        w.playground_status.assert_called_once()
        w.upload_probe.assert_not_called(); w.apply.assert_not_called(); w.redeploy_probe.assert_not_called()

    def test_connect_stops_before_model_publication_when_probe_errors(self):
        w = module.Workspace(self.cfg, self.api); self.prepare_connect_files(w)
        for name in ('apply','configure_private_dns','prepare_credential_probe','prepare_vault_credential',
                     'upload_probe','publish_probe','wait_active'):
            setattr(w, name, Mock(return_value=True))
        w.invoke_probe = Mock(side_effect=module.PocError('private transport failed'))
        w.upload_inference = Mock(); w.redeploy_probe = Mock()
        with self.assertRaises(module.PocError): w.connect()
        w.upload_inference.assert_not_called(); w.redeploy_probe.assert_not_called()


    def playground_fixture(self):
        w = module.Workspace(self.cfg, self.api)
        w.saved['resources'] = {'workspace': {'key': 'workspace'}, 'compute': {'key': 'compute'},
                                'agent': {'key': 'agent'}, 'deployment': {'key': 'prod'}}
        w.saved['inference_entry'] = {'path': 'inference.py'}
        self.api.get = Mock(return_value={'entryFilePath': 'inference.py', 'computeKey': 'compute',
                                         'deploymentState': {'TEST': 'ACTIVE', 'PROD': 'ACTIVE'}})
        self.api.list = [{'key': 'test-session', 'agentKey': 'agent', 'agentComputeKey': 'compute',
                          'endpointUrl': 'https://gateway.aidp.example.oci.oraclecloud.com/chat',
                          'timeCreated': '2026-11-11T00:00:00Z'}]
        agent = self.api.get.return_value
        self.api.get.side_effect = lambda path: dict(self.api.list[0]) if '/sessions/' in path else agent
        self.api.request = Mock(return_value=({'output': [{'role': 'assistant', 'content': [
            {'type': 'output_text', 'text': 'PLAYGROUND_CHECK_OK',
             'traces': {'attributes': {'deploymentType': 'TEST'}}}]}]}, {}))
        return w

    def test_playground_metadata_is_not_mistaken_for_chat_acceptance(self):
        w = self.playground_fixture()
        self.assertTrue(w.playground_status())
        self.api.request.assert_not_called()
        self.assertEqual(w.saved['playground_status']['chat_verification'], 'UI_CHECK_REQUIRED')
        self.assertNotIn('playground_verification', w.saved)

    def test_pending_test_runtime_is_reported_without_changing_prod(self):
        w = self.playground_fixture()
        self.api.get.side_effect = None
        self.api.get.return_value = {'entryFilePath': 'inference.py', 'computeKey': 'compute',
                                     'deploymentState': {'TEST': 'CREATING', 'PROD': 'ACTIVE'}}
        self.assertFalse(w.playground_status())
        self.api.request.assert_not_called()
        self.assertEqual(w.saved['playground_status']['test_state'], 'CREATING')

    def test_wrong_source_is_rejected_without_cloud_mutation(self):
        w = self.playground_fixture()
        self.api.get.side_effect = None
        self.api.get.return_value = {'entryFilePath': 'probe.py', 'computeKey': 'compute'}
        with self.assertRaisesRegex(module.PocError, 'entry or compute differs'):
            w.playground_status()
        self.api.request.assert_not_called()

    def test_connect_always_reports_playground_separately(self):
        w = module.Workspace(self.cfg, self.api); self.prepare_connect_files(w)
        w.saved.update(inference_entry={'path': 'model.py'}, inference_verified=True)
        w.wait_active = Mock(return_value=True); w.invoke_inference = Mock()
        w.playground_status = Mock(return_value=True)
        w.connect()
        w.invoke_inference.assert_called_once()
        w.playground_status.assert_called_once()


if __name__ == '__main__': unittest.main()

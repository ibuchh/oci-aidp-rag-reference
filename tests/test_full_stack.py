"""Exercise account isolation, private routing inputs, proxy auth and safety gates."""
import argparse
import configparser
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'scripts'))
import poc

spec=importlib.util.spec_from_file_location('proxy',ROOT/'runtime/monitoring/configure-streamlit-https.py')
proxy=importlib.util.module_from_spec(spec);spec.loader.exec_module(proxy)


class Configuration(unittest.TestCase):
    def setUp(self):
        self.cfg=json.loads((ROOT/'tests/fixtures/poc.example.json').read_text())
        self.parser=configparser.ConfigParser(interpolation=None)
        self.parser[self.cfg['oci_profile']]={'tenancy':self.cfg['tenancy_id']}
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.path=Path(self.temp.name)/'config.json'

    def load(self):
        self.path.write_text(json.dumps(self.cfg))
        with mock.patch.object(poc.configparser,'ConfigParser',return_value=self.parser),mock.patch.object(self.parser,'read'):
            return poc.read_config(self.path,complete=False)

    def test_state_is_isolated_from_existing_infrastructure(self):
        cfg=self.load()
        self.assertEqual(Path(cfg['state_directory']),ROOT/'deployments/vllm-aidp-poc/states')
        self.cfg['state_directory']=str(ROOT.parent/'infra')
        with self.assertRaises(poc.PocError):self.load()

    def test_profile_tenancy_mismatch_is_rejected(self):
        self.parser[self.cfg['oci_profile']]['tenancy']='ocid1.tenancy.oc1..other'
        with self.assertRaises(poc.PocError):self.load()

    def test_unknown_config_and_invalid_username_are_rejected(self):
        self.cfg['not_a_setting']=True
        with self.assertRaises(poc.PocError):self.load()
        del self.cfg['not_a_setting'];self.cfg['streamlit_username']='user\nadmin'
        with self.assertRaises(poc.PocError):self.load()

    def test_complete_config_validates_engine_inputs_and_keeps_one_state_namespace(self):
        key=Path(self.temp.name)/'key';key.write_text('fixture key, never used for SSH')
        Path(str(key)+'.pub').write_text('ssh-ed25519 fixture')
        self.cfg.update(availability_domain='fixture:EU-FRANKFURT-1-AD-3',admin_cidr='203.0.113.5/32',
                        gpu_image_id='ocid1.image.oc1.eu-frankfurt-1.fixture',
                        monitoring_image_id='ocid1.image.oc1.eu-frankfurt-1.fixture',
                        ssh_private_key_path=str(key))
        self.path.write_text(json.dumps(self.cfg))
        with mock.patch.object(poc.configparser,'ConfigParser',return_value=self.parser),mock.patch.object(self.parser,'read'):
            cfg=poc.read_config(self.path)
        self.assertEqual(cfg['tenancy_id'],self.cfg['tenancy_id'])
        self.assertEqual(Path(cfg['state_directory']),ROOT/'deployments/vllm-aidp-poc/states')
        self.assertEqual(cfg['gpu_shape'],'VM.GPU.A10.2')

    def test_missing_image_is_rejected_before_any_preflight_cloud_request(self):
        self.cfg['gpu_image_id']=None
        self.path.write_text(json.dumps(self.cfg))
        with mock.patch.object(poc.configparser,'ConfigParser',return_value=self.parser),mock.patch.object(self.parser,'read'),mock.patch.object(poc,'target_check') as cloud:
            with self.assertRaises(poc.PocError):poc.main(['preflight','--config',str(self.path)])
            cloud.assert_not_called()


class ComponentInputs(unittest.TestCase):
    def setUp(self):
        self.cfg=json.loads((ROOT/'tests/fixtures/poc.example.json').read_text())
        self.cfg['state_directory']=str(ROOT/'deployments/test/states')
        self.cfg['vcn_cidr']='172.20.0.0/16'

    def test_private_subnet_uses_customer_cidr_and_actual_attached_nsg(self):
        states={'shared':{'vcn_id':'vcn','gpu_subnet_id':'gpu-subnet'},'vllm':{'private_ip':'172.20.33.19'},'monitoring':{'gpu_metrics_nsg_id':'gpu-nsg'}}
        dhcp={'options':[{'type':'DomainNameServer','server-type':'VcnLocalPlusInternet'}]}
        with mock.patch.object(poc,'output',side_effect=lambda c,s:states[s]),mock.patch.object(poc,'oci',side_effect=[{'route-table-id':'route','dhcp-options-id':'dhcp'},dhcp,[]]):
            values=poc.stage_vars(self.cfg,'aidp-foundation')
        self.assertEqual(values['workspace_cidr'],'172.20.36.0/24')
        self.assertEqual(values['vcn_dns_ip'],'169.254.169.254')
        self.assertEqual(values['gpu_https_nsg_id'],'gpu-nsg')

    def test_runtime_policy_requires_observed_identity(self):
        with mock.patch.object(poc,'output',return_value={}):
            with self.assertRaises(poc.PocError):poc.stage_vars(self.cfg,'runtime-iam')

    def test_managed_service_tenancy_identity_is_not_assumed_to_be_customer_runtime(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = dict(self.cfg, state_directory=str(Path(directory)/'states'),
                       runtime_principal_type='dataflowcomputecluster', runtime_principal_id='ocid1.dataflowcomputecluster.oc1.fixture')
            (Path(directory)/'runtime-identity.json').write_text(json.dumps({'reports': [{
                'identity_claims': {'res_tenant': 'ocid1.tenancy.oc1..managed-service'}}]}))
            with mock.patch.object(poc, 'output', return_value={}):
                with self.assertRaisesRegex(poc.PocError, 'managed service tenancy'):
                    poc.stage_vars(cfg, 'runtime-iam')

    def test_private_dns_uses_only_the_owned_vcn_resolver(self):
        outputs = {'shared': {'vcn_id':'owned-vcn'}, 'vllm': {'private_ip':'172.20.33.19'}, 'aidp-foundation':{}}
        responses = [[{'id':'other','attached-vcn-id':'other-vcn'}, {'id':'owned','attached-vcn-id':'owned-vcn'}],
                     {'default-view-id':'owned-view'}, []]
        with mock.patch.object(poc,'output',side_effect=lambda c,s:outputs[s]), \
                mock.patch.object(poc,'oci',side_effect=responses), mock.patch.object(poc,'state',return_value={}):
            values = poc.stage_vars(self.cfg,'aidp-dns')
        self.assertEqual(values['view_id'],'owned-view')
        self.assertEqual(values['inference_private_ip'],'172.20.33.19')

    def test_credential_policy_inputs_are_exact_aidp_and_secret_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = dict(self.cfg, state_directory=str(Path(directory)/'states'))
            runtime = Path(directory)/'runtime'; runtime.mkdir()
            (runtime/'inference-secret.json').write_text(json.dumps({'id':'ocid1.vaultsecret.oc1.fixture'}))
            with mock.patch.object(poc,'output',side_effect=lambda c,s: {'aidp_id':'ocid1.aidataplatform.oc1.fixture'} if s=='aidp' else {'key_id':'ocid1.key.oc1.fixture'}):
                values = poc.stage_vars(cfg,'aidp-credential-iam')
        self.assertEqual(values['aidp_id'],'ocid1.aidataplatform.oc1.fixture')
        self.assertEqual(values['inference_secret_id'],'ocid1.vaultsecret.oc1.fixture')
        self.assertNotIn('runtime_principal_id', values)

    def test_private_aidp_bootstrap_gate_runs_before_terraform(self):
        with mock.patch.object(poc.subprocess,'run') as run:
            with self.assertRaises(poc.PocError):poc.tf_stage(self.cfg,'aidp',apply=True)
            run.assert_not_called()

    def test_destroy_gate_runs_before_loading_config_or_cloud(self):
        with mock.patch.object(poc,'read_config') as read:
            with self.assertRaises(SystemExit):poc.main(['destroy','--apply'])
            read.assert_not_called()

    def test_destroy_preview_keeps_active_secret_and_still_reviews_infrastructure(self):
        for flags in ([], ['--plan']):
            with self.subTest(flags=flags), tempfile.TemporaryDirectory() as directory:
                cfg=dict(self.cfg,state_directory=str(Path(directory)/'states'))
                record=Path(directory)/'runtime/inference-secret.json'
                record.parent.mkdir();record.write_text(json.dumps({'id':'ocid1.vaultsecret.oc1.fixture'}))
                with mock.patch.object(poc,'read_config',return_value=cfg),mock.patch.object(poc,'target_check'),mock.patch.object(poc,'oci',return_value={'lifecycle-state':'ACTIVE'}) as api,mock.patch.object(poc,'inference') as inference:
                    poc.main(['destroy', *flags])
                api.assert_called_once_with(cfg,'vault','secret','get','--secret-id','ocid1.vaultsecret.oc1.fixture')
                self.assertFalse(inference.call_args.kwargs['apply'])
                self.assertTrue(inference.call_args.kwargs['destroy'])

    def test_destroy_plan_and_apply_conflict_before_configuration_or_cloud(self):
        with mock.patch.object(poc,'read_config') as read, mock.patch.object(poc,'target_check') as cloud:
            with self.assertRaises(SystemExit) as error:
                poc.main(['destroy','--plan','--apply'])
            self.assertEqual(error.exception.code, 2)
            read.assert_not_called()
            cloud.assert_not_called()

    def test_destroy_plan_flag_cannot_be_combined_with_apply_action(self):
        with mock.patch.object(poc,'read_config') as read, mock.patch.object(poc,'target_check') as cloud:
            with self.assertRaises(SystemExit) as error:
                poc.main(['apply','--stage','aidp','--plan'])
            self.assertEqual(error.exception.code, 2)
            read.assert_not_called()
            cloud.assert_not_called()


class Proxy(unittest.TestCase):
    def test_every_streamlit_route_requires_auth_and_browser_auth_is_stripped(self):
        text=proxy.snippet('pocuser','$2a$14$'+'a'*53)
        self.assertIn('basic_auth {',text)
        self.assertIn('header_up -Authorization',text)
        self.assertIn('127.0.0.1:8501',text)
        self.assertNotIn('handle_path',text)

    def test_plaintext_passwords_and_config_injection_are_rejected(self):
        for username,hashed in [('pocuser','plain-password'),('bad\nrespond 200','$2a$14$'+'a'*53)]:
            with self.assertRaises(ValueError):proxy.snippet(username,hashed)

if __name__=='__main__':unittest.main()

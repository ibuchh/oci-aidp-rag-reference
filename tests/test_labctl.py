"""Safety and real local-Terraform tests. No OCI resource is created or deleted."""
import argparse
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import inference_stack


def change(actions, mode='managed'):
    return {'resource_changes': [{'address': 'oci_core_instance.gpu', 'mode': mode,
                                  'change': {'actions': actions}}]}


class PlanSafety(unittest.TestCase):
    def test_deploy_rejects_delete_and_both_replacement_orders(self):
        for actions in [['delete'], ['create', 'delete'], ['delete', 'create'], ['forget']]:
            with self.subTest(actions=actions), self.assertRaises(inference_stack.PocError):
                inference_stack.guard_plan(change(actions), 'deploy')

    def test_destroy_rejects_create_update_and_replacement(self):
        for actions in [['create'], ['update'], ['delete', 'create']]:
            with self.subTest(actions=actions), self.assertRaises(inference_stack.PocError):
                inference_stack.guard_plan(change(actions), 'destroy')

    def test_expected_actions_and_data_reads(self):
        self.assertEqual(len(inference_stack.guard_plan(change(['create']), 'deploy')), 1)
        self.assertEqual(len(inference_stack.guard_plan(change(['delete']), 'destroy')), 1)
        self.assertEqual(inference_stack.guard_plan(change(['read'], mode='data'), 'destroy'), [])
        self.assertEqual(inference_stack.guard_plan(change(['no-op']), 'deploy'), [])

    def test_destroy_apply_requires_delete_data_before_loading_configuration(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            inference_stack.main(['destroy', '--apply', '--config', '/does/not/exist'])
        self.assertEqual(error.exception.code, 2)


class ConfigSafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'key').write_text('not used for signing in local tests')
        (self.root/'key.pub').write_text('ssh-ed25519 fixture')
        self.config = dict(name='fixture-lab', oci_profile='fixture',
                           compartment_id='ocid1.compartment.oc1..fixture',
                           availability_domain='fixture:EU-FRANKFURT-1-AD-3',
                           admin_cidr='203.0.113.8/32',
                           gpu_image_id='ocid1.image.oc1.eu-frankfurt-1.gpu',
                           monitoring_image_id='ocid1.image.oc1.eu-frankfurt-1.cpu',
                           ssh_private_key_path=str(self.root/'key'), state_directory='state')
        self.file = self.root/'config.json'

    def load(self):
        self.file.write_text(json.dumps(self.config))
        return inference_stack.load_config(self.file)

    def test_relative_state_is_resolved_against_config_directory(self):
        self.assertEqual(self.load()['state_directory'], str((self.root/'state').resolve()))

    def test_accepts_explicit_any_ipv4_source(self):
        self.config['admin_cidr'] = '0.0.0.0/0'
        cfg = self.load()
        self.assertEqual(cfg['admin_cidr'], '0.0.0.0/0')
        stack = inference_stack.InferenceStack(cfg, argparse.Namespace())
        self.assertEqual(stack.variables('shared')['admin_cidr'], '0.0.0.0/0')

    def test_rejects_wide_admin_network_and_unknown_options(self):
        self.config['admin_cidr'] = '8.8.4.0/24'
        with self.assertRaises(inference_stack.PocError):
            self.load()
        self.config['admin_cidr'] = '203.0.113.8/32'
        self.config['compartmet_id'] = 'typo'
        with self.assertRaises(inference_stack.PocError):
            self.load()

    def test_legacy_region_keys_are_accepted_for_both_images(self):
        for region, key in [('us-ashburn-1', 'iad'), ('us-phoenix-1', 'phx'),
                            ('eu-frankfurt-1', 'fra'), ('uk-london-1', 'lhr')]:
            with self.subTest(region=region):
                self.config.update(region=region, gpu_image_id=f'ocid1.image.oc1.{key}.gpu',
                                   monitoring_image_id=f'ocid1.image.oc1.{key}.cpu')
                self.assertEqual(self.load()['region'], region)

    def test_wrong_legacy_region_is_rejected(self):
        self.config.update(region='us-ashburn-1', gpu_image_id='ocid1.image.oc1.phx.gpu')
        with self.assertRaises(inference_stack.PocError):
            self.load()

    def test_rejects_image_in_another_region(self):
        self.config['gpu_image_id'] = 'ocid1.image.oc1.us-ashburn-1.gpu'
        with self.assertRaises(inference_stack.PocError):
            self.load()

    def test_monitoring_state_accepts_legacy_and_full_region_names(self):
        for region, key in [('us-ashburn-1', 'iad'), ('us-phoenix-1', 'phx'),
                            ('eu-frankfurt-1', 'fra'), ('uk-london-1', 'lhr')]:
            self.config.update(region=region, gpu_image_id=f'ocid1.image.oc1.{key}.gpu',
                               monitoring_image_id=f'ocid1.image.oc1.{key}.cpu')
            cfg = self.load()
            for state_region in (key, region):
                with self.subTest(region=region, state_region=state_region):
                    path = Path(cfg['state_directory'])/'monitoring'/'terraform.tfstate'
                    inference_stack.private_json(path, {'resources': [{'mode': 'managed',
                        'type': 'oci_core_instance', 'name': 'monitoring', 'instances': [
                        {'attributes': {'region': state_region, 'compartment_id': cfg['compartment_id']}}]}]})
                    before = path.read_bytes()
                    inference_stack.InferenceStack(cfg, argparse.Namespace()).snapshot()
                    self.assertEqual(path.read_bytes(), before, 'Snapshot must not modify Terraform state')

    def test_monitoring_state_rejects_foreign_and_unknown_regions(self):
        self.config.update(region='us-ashburn-1', gpu_image_id='ocid1.image.oc1.iad.gpu',
                           monitoring_image_id='ocid1.image.oc1.iad.cpu')
        cfg = self.load()
        for region in ('phx', 'us-phoenix-1', 'unknown-region'):
            with self.subTest(region=region):
                path = Path(cfg['state_directory'])/'monitoring'/'terraform.tfstate'
                inference_stack.private_json(path, {'resources': [{'mode': 'managed',
                    'type': 'oci_core_instance', 'name': 'monitoring', 'instances': [
                    {'attributes': {'region': region, 'compartment_id': cfg['compartment_id']}}]}]})
                with self.assertRaisesRegex(inference_stack.PocError, 'different region'):
                    inference_stack.InferenceStack(cfg, argparse.Namespace()).snapshot()

    def test_foreign_compartment_state_blocks_before_planning(self):
        cfg = self.load()
        path = Path(cfg['state_directory'])/'shared'/'terraform.tfstate'
        inference_stack.private_json(path, {'resources': [{'mode': 'managed', 'type': 'oci_core_vcn',
            'name': 'lab', 'instances': [{'attributes': {'compartment_id': 'foreign'}}]}]})
        lab = inference_stack.InferenceStack(cfg, argparse.Namespace())
        with self.assertRaises(inference_stack.PocError):
            lab.snapshot()

    def test_cleanup_inventory_retains_disk_after_instance_state_is_empty(self):
        cfg = self.load()
        lab = inference_stack.InferenceStack(cfg, argparse.Namespace())
        path = Path(cfg['state_directory'])/'vllm'/'terraform.tfstate'
        inference_stack.private_json(path, {'resources': [{'mode': 'managed', 'type': 'oci_core_instance',
            'name': 'gpu', 'instances': [{'attributes': {'id': 'instance-fixture',
                'boot_volume_id': 'boot-fixture', 'compartment_id': cfg['compartment_id'],
                'availability_domain': cfg['availability_domain']}}]}]})
        lab.snapshot()
        inference_stack.private_json(path, {'resources': [], 'outputs': {}})
        lab.snapshot()
        self.assertIn('boot-fixture', lab.inventory['boot_volumes'])
        first = json.loads((lab.run/'vllm-before.json').read_text())
        self.assertEqual(len(inference_stack.managed(first)), 1, 'Original recovery evidence must not be overwritten')

    def test_foreign_resource_in_same_compartment_is_not_in_cleanup_scope(self):
        cfg = self.load()
        path = Path(cfg['state_directory'])/'shared'/'terraform.tfstate'
        inference_stack.private_json(path, {'resources': [{'mode': 'managed', 'type': 'oci_identity_policy',
            'name': 'unrelated', 'instances': [{'attributes': {'compartment_id': cfg['compartment_id']}}]}]})
        with self.assertRaises(inference_stack.PocError):
            inference_stack.InferenceStack(cfg, argparse.Namespace()).snapshot()

    def test_noninteractive_delete_requires_matching_name(self):
        cfg = self.load()
        args = argparse.Namespace(yes=True, action='destroy', confirm_name='another-lab')
        with self.assertRaises(inference_stack.PocError):
            inference_stack.InferenceStack(cfg, args).confirm('DELETE fixture-lab')


@unittest.skipUnless(shutil.which('terraform'), 'Terraform not installed')
class LocalTerraformIntegration(unittest.TestCase):
    setUp = ConfigSafety.setUp
    load = ConfigSafety.load
    def test_destroy_override_is_isolated_and_preview_does_not_change_real_state(self):
        cfg = self.load()
        source = self.root/'templates'/'terraform'/'shared'
        source.mkdir(parents=True)
        original = '''resource "terraform_data" "fixture" {
  input = "original"
  lifecycle {
    prevent_destroy = true
    ignore_changes = [input]
  }
}
'''
        (source/'main.tf').write_text(original)
        args = argparse.Namespace(apply=False, yes=False, action='destroy')
        with mock.patch.object(inference_stack, 'ROOT', self.root/'templates'), \
             mock.patch.dict(inference_stack.PROTECTED, {'shared': [('terraform_data', 'fixture')]}):
            lab = inference_stack.InferenceStack(cfg, args)
            folder = lab.prepare('shared')
            # Built-in terraform_data only: this cannot contact OCI or create cloud resources.
            lab.command(['terraform', f'-chdir={folder}', 'apply', '-auto-approve', '-input=false'])
            actual = Path(cfg['state_directory'])/'shared'/'terraform.tfstate'
            before = actual.read_bytes()
            with contextlib.redirect_stdout(io.StringIO()):
                _, _, changes = lab.plan('shared', destroy=True)
            self.assertEqual(changes, [('terraform_data.fixture', ['delete'])])
            self.assertEqual(actual.read_bytes(), before)
            self.assertEqual((source/'main.tf').read_text(), original)
            canonical = Path(cfg['state_directory'])/'shared'/'automation_override.tf.json'
            self.assertNotIn('resource', json.loads(canonical.read_text()))
            # The lifecycle override preserves ignore_changes. Changed input still has no diff.
            (source/'main.tf').write_text(original.replace('"original"', '"changed"'))
            second = lab.prepare('shared', destroy=True)
            lab.command(['terraform', f'-chdir={second}', 'plan', '-out=local.tfplan'])
            detail = json.loads(lab.command(['terraform', f'-chdir={second}', 'show', '-json', 'local.tfplan'], capture=True))
            self.assertEqual(inference_stack.guard_plan(detail, 'deploy'), [])


if __name__ == '__main__':
    unittest.main()

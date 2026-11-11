"""Stage orchestration streams lifecycle commands while capturing JSON plans privately."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import poc


class StageProgress(unittest.TestCase):
    def test_json_plan_stays_captured_and_exit_two_remains_an_accepted_plan(self):
        document = {'resource_changes': [{'address': 'oci_database_autonomous_database.lakehouse',
                    'mode': 'managed', 'change': {'actions': ['create']}}]}
        def captured(argv, **kwargs):
            self.assertEqual(argv[2], 'show')
            text = json.dumps(document) if '-json' in argv else 'PRIVATE_PLAN_FIXTURE'
            return subprocess.CompletedProcess(argv, 0, text, '')
        def streamed(argv, **kwargs):
            return subprocess.CompletedProcess(argv, 2 if argv[2] == 'plan' else 0, '', '')
        with tempfile.TemporaryDirectory() as folder:
            cfg = dict(name='fixture', compartment_id='fixture-compartment', tenancy_id='fixture-tenancy',
                       state_directory=str(Path(folder)/'states'))
            with mock.patch.object(poc, 'stage_vars', return_value={}), \
                 mock.patch.object(poc.subprocess, 'run', side_effect=captured), \
                 mock.patch.object(poc, 'run_terraform', side_effect=streamed) as stream:
                work = poc.tf_stage(cfg, 'lakehouse', apply=True, yes=True)
            self.assertEqual([c.args[0][2] for c in stream.call_args_list], ['init', 'validate', 'plan', 'apply'])
            self.assertEqual(json.loads((work/'plan.json').read_text()), document)
            self.assertEqual((work/'plan.txt').read_text(), 'PRIVATE_PLAN_FIXTURE')


    def test_destroy_overrides_initialize_for_every_stage_without_cloud_access(self):
        for stage in poc.AIDP_STAGES:
            protected = bool(poc.PROTECTED.get(stage))
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as folder:
                folder = Path(folder)
                templates = folder/'templates'
                source = templates/'terraform'/stage
                source.mkdir(parents=True)
                original = 'resource "terraform_data" "fixture" {\n'
                if protected:
                    original += '  lifecycle { prevent_destroy = true }\n'
                original += '}\n'
                (source/'main.tf').write_text(original)
                cfg = dict(name='fixture', compartment_id='fixture-compartment',
                           tenancy_id='fixture-tenancy', state_directory=str(folder/'states'))
                canonical = folder/'states'/stage
                canonical.mkdir(parents=True)
                (canonical/'terraform.tfvars.json').write_text('{}')
                protections = {stage: [('terraform_data', 'fixture')]} if protected else {}
                with mock.patch.object(poc, 'ROOT', templates), \
                     mock.patch.object(poc, 'PROTECTED', protections), \
                     mock.patch.object(poc, 'oci', side_effect=AssertionError('No OCI calls allowed')), \
                     contextlib.redirect_stdout(io.StringIO()):
                    work = poc.tf_stage(cfg, stage, destroy=True)
                override = json.loads((work/'automation_override.tf.json').read_text())
                if protected:
                    self.assertEqual(override['resource']['terraform_data']['fixture'],
                                     {'lifecycle': {'prevent_destroy': False}})
                else:
                    self.assertNotIn('resource', override)
                self.assertNotIn('resource', json.loads((canonical/'automation_override.tf.json').read_text()))
                self.assertEqual((source/'main.tf').read_text(), original)
                document = json.loads((work/'plan.json').read_text())
                self.assertFalse(document.get('resource_changes'))



if __name__ == '__main__':
    unittest.main()

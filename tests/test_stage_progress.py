"""Stage orchestration streams lifecycle commands while capturing JSON plans privately."""
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


if __name__ == '__main__':
    unittest.main()

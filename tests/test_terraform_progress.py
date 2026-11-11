"""Exercise actual subprocess streaming, privacy, failure, timeout and AIDP polling."""
import contextlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import terraform_progress as progress


class LiveProgress(unittest.TestCase):
    def test_output_and_log_are_updated_before_process_exit_without_exposing_secrets(self):
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder)/'commands.log'
            release = Path(folder)/'release'
            seen = threading.Event()
            class Terminal(io.StringIO):
                def write(self, text):
                    result = super().write(text)
                    if 'Still creating' in self.getvalue():
                        seen.set()
                    return result
            terminal = Terminal()
            code = """import pathlib,sys,time
print('password=PRIVATE_FIXTURE',flush=True)
print('oci_core_instance.gpu: Still creating... [10s elapsed]',flush=True)
while not pathlib.Path(sys.argv[1]).exists():time.sleep(.01)
print('Apply complete! Resources: 1 added, 0 changed, 0 destroyed.',flush=True)
"""
            results, errors = [], []
            def execute():
                try:
                    results.append(progress.run_terraform([sys.executable, '-u', '-c', code, str(release)],
                                                          log_path=log, timeout=5))
                except BaseException as error:
                    errors.append(error)
            with contextlib.redirect_stdout(terminal):
                worker = threading.Thread(target=execute)
                worker.start()
                try:
                    self.assertTrue(seen.wait(3), terminal.getvalue())
                    self.assertTrue(worker.is_alive(), 'Progress must arrive before process exit')
                    self.assertIn('PRIVATE_FIXTURE', log.read_text())
                    self.assertNotIn('PRIVATE_FIXTURE', terminal.getvalue())
                finally:
                    release.touch()
                    worker.join(7)
            self.assertFalse(worker.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(results[0].returncode, 0)
            self.assertIn('Apply complete!', terminal.getvalue())
            self.assertEqual(log.stat().st_mode & 0o777, 0o600)

    def test_errors_report_private_log_immediately_and_preserve_nonzero_exit(self):
        with tempfile.TemporaryDirectory() as folder:
            terminal = io.StringIO()
            log = Path(folder)/'commands.log'
            code = "import sys;print('Error: PRIVATE_DIAGNOSTIC',file=sys.stderr,flush=True);sys.exit(7)"
            with contextlib.redirect_stdout(terminal):
                result = progress.run_terraform([sys.executable, '-c', code], log_path=log)
            self.assertEqual(result.returncode, 7)
            self.assertIn('Terraform reported an error', terminal.getvalue())
            self.assertNotIn('PRIVATE_DIAGNOSTIC', terminal.getvalue())
            self.assertIn('PRIVATE_DIAGNOSTIC', log.read_text())

    def test_heartbeat_and_service_poll_work_during_silent_commands(self):
        with tempfile.TemporaryDirectory() as folder:
            terminal = io.StringIO()
            poll = mock.Mock(return_value='AIDP ENABLE_AI_FEATURES: IN_PROGRESS (78.6%)')
            with contextlib.redirect_stdout(terminal):
                result = progress.run_terraform([sys.executable, '-c', 'import time;time.sleep(.35)'],
                                                log_path=Path(folder)/'commands.log', interval=.05, poll=poll)
            self.assertEqual(result.returncode, 0)
            self.assertIn('still running', terminal.getvalue())
            self.assertIn('(78.6%)', terminal.getvalue())
            poll.assert_called()

    def test_poll_failure_does_not_interrupt_terraform_or_leak_diagnostics(self):
        with tempfile.TemporaryDirectory() as folder:
            terminal = io.StringIO()
            with contextlib.redirect_stdout(terminal):
                result = progress.run_terraform([sys.executable, '-c', 'import time;time.sleep(.2)'],
                    log_path=Path(folder)/'commands.log', interval=.02,
                    poll=mock.Mock(side_effect=RuntimeError('PRIVATE_DIAGNOSTIC')))
            self.assertEqual(result.returncode, 0)
            self.assertIn('Terraform continues', terminal.getvalue())
            self.assertNotIn('PRIVATE_DIAGNOSTIC', terminal.getvalue())

    def test_timeout_stops_child(self):
        with tempfile.TemporaryDirectory() as folder, contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(subprocess.TimeoutExpired):
                progress.run_terraform([sys.executable, '-c', 'import time;time.sleep(30)'],
                                        log_path=Path(folder)/'commands.log', timeout=.1)


class AidpProgress(unittest.TestCase):
    cfg = dict(name='fixture', compartment_id='compartment-fixture',
               oci_profile='fixture', region='us-ashburn-1')

    def result(self, data):
        return subprocess.CompletedProcess([], 0, json.dumps({'data': {'items': data}}), '')

    def test_only_the_owned_platform_is_polled_and_latest_operation_is_reported(self):
        platform = {'display-name': 'fixture', 'freeform-tags': {'Project': 'fixture'}, 'id': 'owned-id'}
        requests = [{'operation-type': 'CREATE_AI_DATA_PLATFORM', 'status': 'SUCCEEDED',
                     'percent-complete': 100, 'time-accepted': '2026-11-10T10:00:00Z'},
                    {'operation-type': 'ENABLE_AI_FEATURES', 'status': 'IN_PROGRESS',
                     'percent-complete': 78.57143, 'time-accepted': '2026-11-10T10:10:00Z'}]
        with mock.patch.object(progress.subprocess, 'run', side_effect=[self.result([platform]),
                                                                      self.result(requests)]) as api:
            message = progress.aidp_progress(self.cfg)
        self.assertEqual(message, 'AIDP ENABLE_AI_FEATURES: IN_PROGRESS (78.6%)')
        self.assertIn('owned-id', api.call_args.args[0])
        self.assertEqual(api.call_args.kwargs['timeout'], 12)

    def test_same_name_without_project_ownership_is_not_polled(self):
        platform = {'display-name': 'fixture', 'freeform-tags': {'Project': 'other'}, 'id': 'foreign-id'}
        with mock.patch.object(progress.subprocess, 'run', return_value=self.result([platform])) as api:
            message = progress.aidp_progress(self.cfg)
        self.assertIn('waiting', message)
        api.assert_called_once()


if __name__ == '__main__':
    unittest.main()

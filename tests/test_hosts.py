"""Console-history regression checks retain strict SSH identity verification."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
from hosts import Access
from inference_stack import PocError


class ConsoleHistory(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.known = self.root / 'known_hosts'
        self.known.write_text('unrelated-host ssh-ed25519 unrelated-key\n')
        self.history = {'id': 'ocid1.consolehistory.fixture', 'lifecycle-state': 'SUCCEEDED',
                        'time-created': '2026-11-08T23:00:00Z'}
        self.console = 'Host key fingerprint: SHA256:fixture\n'
        self.lab = SimpleNamespace(
            config={'compartment_id': 'ocid1.compartment.fixture', 'oci_profile': 'fixture',
                    'region': 'eu-frankfurt-1'},
            run=self.root, oci=mock.Mock(), command=mock.Mock(side_effect=self.command))
        self.access = Access(self.lab, {'vllm': {'instance_id': {'value': 'ocid1.instance.fixture'}}},
                             'vllm', self.known)

    def command(self, argv, **kwargs):
        if argv[0] == 'oci':
            Path(argv[argv.index('--file') + 1]).write_text(self.console)
        elif argv[0] == 'ssh-keyscan':
            return '[127.0.0.1]:1234 ssh-ed25519 fixture-public-key\n'
        elif argv[0] == 'ssh-keygen':
            return '256 SHA256:fixture fixture (ED25519)\n'
        else:
            self.fail(f'Unexpected command: {argv[0]}')

    def test_null_and_empty_history_capture_verify_and_clean_up(self):
        for collection in (None, []):
            with self.subTest(collection=collection):
                self.known.write_text('unrelated-host ssh-ed25519 unrelated-key\n')
                self.lab.oci.reset_mock()
                self.lab.oci.side_effect = [collection, self.history, None]
                self.access.verify_host_key()
                self.assertIn('ocid1.instance.fixture ssh-ed25519 fixture-public-key', self.known.read_text())
                self.assertIn('unrelated-host', self.known.read_text())
                self.assertEqual(self.lab.oci.call_args_list[1].args[:3], ('compute', 'console-history', 'capture'))
                self.assertEqual(self.lab.oci.call_args_list[-1].args[:3], ('compute', 'console-history', 'delete'))

    def test_existing_successful_history_is_reused_without_deleting_it(self):
        self.lab.oci.side_effect = [[self.history]]
        self.access.verify_host_key()
        self.lab.oci.assert_called_once()

    def test_fingerprint_mismatch_rejects_key_and_cleans_owned_capture(self):
        self.console = 'Host key fingerprint: SHA256:different\n'
        self.lab.oci.side_effect = [None, self.history, None]
        original = self.known.read_text()
        with self.assertRaisesRegex(PocError, 'does not match'):
            self.access.verify_host_key()
        self.assertEqual(self.known.read_text(), original)
        self.assertEqual(self.lab.oci.call_args_list[-1].args[:3], ('compute', 'console-history', 'delete'))

    def test_malformed_collection_fails_without_accepting_a_key(self):
        self.lab.oci.return_value = {'unexpected': 'response'}
        with self.assertRaisesRegex(PocError, 'Unexpected OCI console-history'):
            self.access.verify_host_key()
        self.assertEqual(self.lab.command.call_count, 1)
        self.assertEqual(self.lab.command.call_args.args[0][0], 'ssh-keyscan')
        self.assertNotIn('ocid1.instance.fixture ', self.known.read_text())

    def test_guest_ssh_is_retried_before_console_capture(self):
        original = self.lab.command.side_effect
        attempts = []
        def delayed(argv, **kwargs):
            if argv[0] == 'ssh-keyscan':
                attempts.append(argv)
                self.lab.oci.assert_not_called()
                self.assertEqual(kwargs['accepted'], (0, 1))
                if len(attempts) == 1:
                    return ''
            return original(argv, **kwargs)
        self.lab.command.side_effect = delayed
        self.lab.oci.side_effect = [None, self.history, None]
        with mock.patch('hosts.time.sleep'):
            self.access.verify_host_key()
        self.assertEqual(len(attempts), 2)
        self.assertIn('ocid1.instance.fixture ssh-ed25519 fixture-public-key', self.known.read_text())

    def test_ssh_readiness_timeout_accepts_no_key_and_captures_no_console(self):
        self.lab.command.side_effect = None
        self.lab.command.return_value = ''
        original = self.known.read_text()
        with mock.patch('hosts.time.monotonic', side_effect=[0, 0, 601]), \
             mock.patch('hosts.time.sleep'), \
             self.assertRaisesRegex(PocError, 'no SSH host key received'):
            self.access.verify_host_key()
        self.lab.oci.assert_not_called()
        self.assertEqual(self.known.read_text(), original)

    def test_dead_tunnel_fails_without_scanning_or_accepting_a_key(self):
        self.access.process = mock.Mock()
        self.access.process.poll.return_value = 1
        with self.assertRaisesRegex(PocError, 'Bastion tunnel exited'):
            self.access.verify_host_key()
        self.lab.command.assert_not_called()
        self.lab.oci.assert_not_called()

    def test_cleanup_keeps_hyphenated_region_in_full_session_id(self):
        session = 'ocid1.bastionsession.oc1.eu-frankfurt-1.fixture'
        self.access.output = (self.root / 'bastion.log').open('w')
        self.access.output.write('Creating temporary session: '+session+'\n')
        self.access.output.flush()
        self.lab.oci.return_value = {'lifecycle-state': 'ACTIVE'}
        self.access.__exit__(None, None, None)
        self.lab.oci.assert_any_call('bastion', 'session', 'get', '--session-id', session)
        self.assertEqual(self.lab.oci.call_args, mock.call('bastion', 'session', 'delete', '--session-id',
                                             session, '--force', capture=False))

    def test_cleanup_does_not_delete_already_deleted_or_deleting_sessions(self):
        for lifecycle in ('DELETED', 'DELETING'):
            with self.subTest(lifecycle=lifecycle):
                self.lab.oci.reset_mock()
                self.lab.oci.return_value = {'lifecycle-state': lifecycle}
                self.access.output = (self.root / 'bastion.log').open('w')
                self.access.output.write('Creating temporary session: ocid1.bastionsession.oc1.eu-frankfurt-1.fixture\n')
                self.access.output.flush()
                self.access.__exit__(None, None, None)
                self.lab.oci.assert_called_once_with('bastion', 'session', 'get', '--session-id',
                                                     'ocid1.bastionsession.oc1.eu-frankfurt-1.fixture')


if __name__ == '__main__':
    unittest.main()

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from hosts import Access
from inference_stack import PocError


class HostPinTests(unittest.TestCase):
    def test_existing_verified_pin_survives_console_rotation(self):
        with tempfile.TemporaryDirectory() as tmp:
            known=Path(tmp)/'known_hosts'
            known.write_text('ocid1.instance.fixture ssh-ed25519 AAAAFIXTURE\n')
            lab=Mock();lab.config={};lab.command.return_value='[127.0.0.1]:2222 ssh-ed25519 AAAAFIXTURE\n'
            access=Access(lab,{'vllm':{'instance_id':{'value':'ocid1.instance.fixture'}}},'vllm',known)
            access.verify_host_key()
            lab.oci.assert_not_called()

    def test_changed_key_fails_without_replacing_pin(self):
        with tempfile.TemporaryDirectory() as tmp:
            known=Path(tmp)/'known_hosts'
            original='ocid1.instance.fixture ssh-ed25519 AAAAFIXTURE\n'
            known.write_text(original)
            lab=Mock();lab.config={};lab.command.return_value='[127.0.0.1]:2222 ssh-ed25519 AAAACHANGED\n'
            access=Access(lab,{'vllm':{'instance_id':{'value':'ocid1.instance.fixture'}}},'vllm',known)
            with self.assertRaises(PocError):access.verify_host_key()
            self.assertEqual(known.read_text(),original)
            lab.oci.assert_not_called()


if __name__=='__main__':unittest.main()

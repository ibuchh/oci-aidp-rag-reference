import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
import aidp_binary_request


class BinaryUpload(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.body = root/'body.txt'
        self.body.write_bytes(b'print("synthetic source")\n')
        self.headers = root/'headers.json'
        self.headers.write_text(json.dumps({'path': '/Workspace/Shared/probe.py', 'type': 'FILE'}))
        self.argv = ['helper', '--profile', 'fixture', '--region', 'eu-frankfurt-1',
                     '--uri', 'https://aidp.eu-frankfurt-1.oci.oraclecloud.com/20260430/aiDataPlatforms/fixture/objects',
                     '--body', str(self.body), '--headers', str(self.headers)]

    def test_python_source_is_sent_as_exact_bytes_and_not_echoed(self):
        response = SimpleNamespace(status=200, headers={'path': '/Workspace/Shared/probe.py'}, data=Mock())
        client = Mock(); client.call_api.return_value = response
        sdk = SimpleNamespace(config=SimpleNamespace(from_file=Mock(return_value={
            'tenancy': 'fixture', 'user': 'fixture', 'fingerprint': 'fixture', 'key_file': 'fixture'})),
            signer=SimpleNamespace(Signer=Mock()), exceptions=SimpleNamespace(ServiceError=RuntimeError))
        stream = io.StringIO()
        with patch.dict(sys.modules, {'oci': sdk, 'oci.base_client': SimpleNamespace(BaseClient=Mock(return_value=client))}), \
                patch.object(sys, 'argv', self.argv), contextlib.redirect_stdout(stream):
            aidp_binary_request.main()
        self.assertEqual(client.call_api.call_args.kwargs['body'], self.body.read_bytes())
        self.assertEqual(json.loads(stream.getvalue())['data'], {})
        self.assertNotIn('synthetic source', stream.getvalue())
        response.data.close.assert_called_once()

    def test_external_endpoint_is_rejected_before_loading_credentials(self):
        self.argv[self.argv.index('--uri')+1] = 'https://example.com/20260430/aiDataPlatforms/fixture/objects'
        sdk = SimpleNamespace(config=SimpleNamespace(from_file=Mock()))
        with patch.dict(sys.modules, {'oci': sdk}), patch.object(sys, 'argv', self.argv):
            with self.assertRaises(ValueError): aidp_binary_request.main()
        sdk.config.from_file.assert_not_called()


if __name__ == '__main__': unittest.main()

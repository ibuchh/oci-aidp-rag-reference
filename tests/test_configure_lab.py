"""Config wizard boundaries, API discovery and complete interactive flow; no cloud writes."""
import argparse
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/'scripts'))
spec = importlib.util.spec_from_file_location('configure_lab', Path(__file__).resolve().parents[1]/'scripts/discovery.py')
wizard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wizard)

REGION = 'eu-frankfurt-1'
COMPARTMENT = 'ocid1.compartment.oc1..fixture'
TENANCY = 'ocid1.tenancy.oc1..fixture'
GPU = f'ocid1.image.oc1.{REGION}.fixturegpu'
MONITOR = f'ocid1.image.oc1.{REGION}.fixturemonitor'


def image(image_id, *, created='2026-01-01T00:00:00Z', custom=False, size=50*1024):
    return {'id': image_id, 'display-name': 'fixture-'+image_id.rsplit('.', 1)[-1],
            'operating-system': 'Canonical Ubuntu', 'operating-system-version': '24.04',
            'size-in-mbs': size, 'time-created': created, 'lifecycle-state': 'AVAILABLE',
            'compartment-id': COMPARTMENT if custom else None}


class FakeAPI:
    instances = []
    def __init__(self, profile, region):
        self.profile, self.region, self.calls = profile, region, []
        self.instances.append(self)

    def get(self, *args):
        self.calls.append(args)
        if args[:3] == ('iam', 'region-subscription', 'list'):
            return [{'region-name': REGION, 'status': 'READY'}, {'region-name': 'other', 'status': 'IN_PROGRESS'}]
        if args[:3] == ('iam', 'compartment', 'list'):
            return [{'id': COMPARTMENT, 'name': 'team', 'compartment-id': TENANCY, 'lifecycle-state': 'ACTIVE'}]
        if args[:3] == ('iam', 'availability-domain', 'list'):
            return [{'name': f'fixture:EU-FRANKFURT-1-AD-{n}'} for n in [1, 2, 3]]
        if args[:3] == ('network', 'vcn', 'list'):
            return [{'cidr-blocks': ['10.140.0.0/16'], 'lifecycle-state': 'AVAILABLE'}]
        if args[:3] == ('compute', 'image', 'list'):
            if '--operating-system-version' in args:
                return [image(f'ocid1.image.oc1.{REGION}.old'),
                        image(f'ocid1.image.oc1.{REGION}.custom', created='2027-01-01T00:00:00Z', custom=True),
                        image(MONITOR, created='2026-11-01T00:00:00Z')]
            return [image(GPU, custom=True, size=100*1024)]
        if args[:3] == ('compute', 'image', 'get'):
            value = args[args.index('--image-id')+1]
            return image(value, custom=value==GPU, size=100*1024 if value==GPU else 50*1024)
        if args[:3] == ('compute', 'image-shape-compatibility-entry', 'get'):
            return {'shape': args[-1]}
        raise AssertionError(f'Unexpected or non-read-only operation: {args}')


class ImageRegionTests(unittest.TestCase):
    def test_ashburn_legacy_key_reaches_image_and_shape_lookups(self):
        api = FakeAPI('TEAM', 'us-ashburn-1')
        image_id = 'ocid1.image.oc1.iad.fixture'
        self.assertEqual(wizard.validate_image(api, image_id, 'VM.GPU.A10.2')['id'], image_id)
        self.assertEqual(len(api.calls), 2)

    def test_wrong_region_stops_before_api_lookup(self):
        api = FakeAPI('TEAM', 'us-ashburn-1')
        with self.assertRaises(wizard.PocError):
            wizard.validate_image(api, 'ocid1.image.oc1.phx.fixture', 'VM.GPU.A10.2')
        self.assertEqual(api.calls, [])


class WizardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        (self.root/'.ssh').mkdir()
        (self.root/'.ssh/id_ed25519').write_text('fixture; not used for authentication')
        (self.root/'.ssh/id_ed25519.pub').write_text('fixture public key')
        FakeAPI.instances = []
        self.args = argparse.Namespace(profile='TEAM', region=REGION, name='team-lab', output=None)

    def flow(self, answers):
        with mock.patch.object(wizard, 'ROOT', self.root), \
             mock.patch.object(wizard.Path, 'home', return_value=self.root), \
             mock.patch.object(wizard, 'profiles', return_value={'TEAM': {'region': REGION, 'tenancy': TENANCY}}), \
             mock.patch.object(wizard, 'Discovery', FakeAPI), \
             mock.patch('builtins.input', side_effect=answers), \
             contextlib.redirect_stdout(io.StringIO()):
            return wizard.configure(self.args)

    def test_complete_flow_selects_platform_image_and_avoids_overlap(self):
        result = self.flow(['1', 'AD-3', '', '', '1', '', '', '8.8.4.4', 'y'])
        cfg = json.loads(result.read_text())
        self.assertEqual(cfg['monitoring_image_id'], MONITOR)
        self.assertEqual(cfg['gpu_image_id'], GPU)
        self.assertEqual(cfg['vcn_cidr'], '10.141.0.0/16')
        self.assertEqual(cfg['admin_cidr'], '8.8.4.4/32')
        self.assertEqual(cfg['gpu_boot_volume_id'], None)
        self.assertEqual(cfg['state_directory'], 'deployments/team-lab/infra')
        self.assertEqual(result.stat().st_mode & 0o777, 0o600)
        self.assertFalse((self.root/'deployments').exists(), 'Wizard must not initialize a deployment')
        self.assertTrue(all(c[2] in ('get', 'list') for c in FakeAPI.instances[0].calls))
        self.assertNotIn('tenancy', cfg)
        self.assertNotIn('key_file', cfg)

    def test_existing_lab_config_is_preserved_and_a_new_file_is_used(self):
        current = self.root/'poc-config.json'
        current.write_text('existing running lab; do not overwrite')
        before = current.read_bytes()
        result = self.flow(['1', '3', '', '', '1', '', '', '8.8.4.4', 'yes'])
        self.assertEqual(current.read_bytes(), before)
        self.assertEqual(result.name, 'poc-config.team-lab.local.json')

    def test_decline_creates_no_config(self):
        result = self.flow(['1', '3', '', '', '1', '', '', '8.8.4.4', 'n'])
        self.assertIsNone(result)
        self.assertFalse((self.root/'poc-config.json').exists())

    def test_existing_explicit_output_fails_before_oci_discovery(self):
        self.args.output = self.root/'existing.json'
        self.args.output.write_text('preserve')
        with self.assertRaises(wizard.PocError):
            self.flow([])
        self.assertEqual(FakeAPI.instances, [])
        self.assertEqual(self.args.output.read_text(), 'preserve')

    def test_atomic_save_never_overwrites_an_existing_config(self):
        cfg = {**wizard.DEFAULTS, 'name': 'fixture-lab', 'oci_profile': 'TEAM', 'region': REGION,
               'compartment_id': COMPARTMENT, 'availability_domain': 'fixture:EU-FRANKFURT-1-AD-3',
               'admin_cidr': '8.8.4.4/32', 'vcn_cidr': '10.141.0.0/16',
               'gpu_image_id': GPU, 'monitoring_image_id': MONITOR,
               'ssh_private_key_path': str(self.root/'.ssh/id_ed25519'),
               'state_directory': 'deployments/fixture-lab/infra'}
        path = self.root/'existing.json';path.write_text('preserve')
        with self.assertRaises(wizard.PocError):
            wizard.save_new_config(path, cfg)
        self.assertEqual(path.read_text(), 'preserve')
        self.assertEqual(list(self.root.glob('.poc-config-check-*')), [])

    def test_cpu_override_rejects_the_large_gpu_image(self):
        with self.assertRaises(wizard.PocError):
            wizard.validate_image(FakeAPI('TEAM', REGION), GPU, 'VM.Standard.E5.Flex', monitoring=True)

    def test_explicit_any_ipv4_source_survives_wizard_and_config_validation(self):
        result = self.flow(['1', 'AD-3', '', '', '1', '', '', '0.0.0.0/0', 'y'])
        cfg = json.loads(result.read_text())
        self.assertEqual(cfg['admin_cidr'], '0.0.0.0/0')

    def test_private_or_wide_egress_addresses_are_not_accepted(self):
        for value in ['10.1.1.1', '0.0.0.0/32', '8.8.4.0/24', '224.1.1.1', '2001:4860:4860::8888']:
            with self.subTest(value=value), self.assertRaises(ValueError):
                wizard.admin_network(value)

    def test_profile_discovery_does_not_embed_credentials(self):
        path = self.root/'oci-config'
        path.write_text('[DEFAULT]\nregion=eu-frankfurt-1\n'
                        '[TEAM]\ntenancy=ocid1.tenancy.oc1..fixture\n'
                        'key_file=/private/API-signing-key.pem\n'
                        'user=ocid1.user.oc1..fixture\nfingerprint=secret-like-fixture\n')
        result = wizard.profiles(path)
        self.assertEqual(result, {'TEAM': {'region': REGION, 'tenancy': TENANCY}})


if __name__ == '__main__':
    unittest.main()

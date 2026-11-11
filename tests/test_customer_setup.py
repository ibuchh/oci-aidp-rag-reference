"""Customer discovery is account-bound, read-only and preserves existing config."""
import argparse
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import customer_setup as setup


class CustomerSetup(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name)
        (self.root/'config').mkdir()
        source=Path(setup.poc.ROOT)/'config/customer.example.json'
        (self.root/'config/customer.example.json').write_bytes(source.read_bytes())
        self.args=argparse.Namespace(config=self.root/'poc-config.json',profile='TEAM',
                                     region='eu-frankfurt-1',compartment_id=None,name='customer-poc')
        self.profiles={'TEAM':{'region':'eu-frankfurt-1','tenancy':'ocid1.tenancy.oc1..customerfixture'}}
        self.subscriptions=[{'region-name':'us-ashburn-1','is-home-region':True,'status':'READY'},
                            {'region-name':'eu-frankfurt-1','is-home-region':False,'status':'READY'}]
        self.compartments=[{'id':'ocid1.compartment.oc1..customerfixture','name':'team',
                            'compartment-id':'ocid1.tenancy.oc1..customerfixture','lifecycle-state':'ACTIVE'}]
        self.api=Mock();self.api.get.side_effect=self.get
        self.saved=[]

    def get(self,*args):
        if args[:3]==('iam','region-subscription','list'):return self.subscriptions
        if args[:3]==('iam','compartment','list'):return self.compartments
        raise AssertionError('Unexpected API operation: '+str(args))

    def capture(self,args):
        self.saved.append(json.loads(args.seed_config.read_text()))
        self.seed_path=args.seed_config
        self.destination=args.config

    def run_setup(self):
        with patch.object(setup.poc,'ROOT',self.root), \
             patch.object(setup.discovery,'profiles',return_value=self.profiles), \
             patch.object(setup.discovery,'Discovery',return_value=self.api), \
             patch.object(setup.discovery,'choose',side_effect=lambda label,items,*a,**k:items[0]), \
             patch.object(setup.poc,'configure',side_effect=self.capture):
            setup.configure(self.args)

    def test_customer_seed_is_bound_to_selected_profile_home_region_and_compartment(self):
        self.run_setup()
        seed=self.saved[0]
        self.assertEqual(seed['oci_profile'],'TEAM')
        self.assertEqual(seed['tenancy_id'],self.profiles['TEAM']['tenancy'])
        self.assertEqual(seed['home_region'],'us-ashburn-1')
        self.assertEqual(seed['region'],'eu-frankfurt-1')
        self.assertEqual(seed['compartment_id'],self.compartments[0]['id'])
        self.assertEqual(seed['state_directory'],'deployments/customer-poc/states')
        self.assertIsNone(seed['gpu_image_id'])
        self.assertNotIn('key_file',seed)
        self.assertFalse(self.seed_path.exists(),'Temporary metadata seed must be removed')
        self.assertTrue(all(c.args[2]=='list' for c in self.api.get.call_args_list))

    def test_existing_config_stops_before_discovery(self):
        self.args.config.write_text('preserve')
        with self.assertRaisesRegex(setup.poc.PocError,'already exists'):self.run_setup()
        self.api.get.assert_not_called()
        self.assertEqual(self.args.config.read_text(),'preserve')

    def test_wrong_requested_profile_stops_before_any_oci_call(self):
        self.args.profile='ABSENT'
        with self.assertRaisesRegex(setup.poc.PocError,'absent'):self.run_setup()
        self.api.get.assert_not_called()

    def test_unsubscribed_region_is_rejected(self):
        self.args.region='not-subscribed'
        with self.assertRaisesRegex(setup.poc.PocError,'READY subscription'):self.run_setup()
        self.assertEqual(self.saved,[])

    def test_compartment_from_another_tenancy_or_inactive_is_not_accepted(self):
        self.args.compartment_id='ocid1.compartment.oc1..other'
        with self.assertRaisesRegex(setup.poc.PocError,'accessible Active'):self.run_setup()
        self.assertEqual(self.saved,[])

    def test_missing_home_region_stops_before_generating_config(self):
        self.subscriptions[0]['is-home-region']=False
        with self.assertRaisesRegex(setup.poc.PocError,'home region'):self.run_setup()
        self.assertFalse(self.args.config.exists())
        self.assertEqual(self.saved,[])


if __name__=='__main__':unittest.main()

#!/usr/bin/env python3
"""Discover customer-owned OCI identifiers and run the existing POC wizard.

Only get/list OCI discovery calls are made; no resources or policies are created.
Signing credentials stay in the OCI configuration. Existing output is preserved.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import tempfile

import discovery
import poc


def configure(args):
    destination = args.config.expanduser().resolve()
    if destination.exists():
        raise poc.PocError('Configuration already exists. Reuse it for resume; setup never overwrites it.')
    profiles = discovery.profiles(Path.home()/'.oci/config')
    if args.profile:
        if args.profile not in profiles:
            raise poc.PocError('The requested profile is absent from ~/.oci/config.')
        profile = args.profile
    else:
        profile = discovery.choose('OCI API-key profile', list(profiles), str)
    identity = profiles[profile]
    tenancy = identity['tenancy']
    if not tenancy.startswith('ocid1.tenancy.'):
        raise poc.PocError('The selected profile has an invalid tenancy identifier.')
    # The discovery region only routes the IAM subscription lookup. Deployment
    # region is selected and verified against READY subscriptions below.
    api = discovery.Discovery(profile, args.region or identity.get('region') or 'us-ashburn-1')
    subscriptions = api.get('iam','region-subscription','list','--tenancy-id',tenancy)
    homes = [r['region-name'] for r in subscriptions if r.get('is-home-region')]
    if len(homes) != 1:
        raise poc.PocError('Cannot uniquely discover the tenancy IAM home region.')
    ready = [r for r in subscriptions if r.get('status') == 'READY']
    if args.region:
        selected = next((r for r in ready if r['region-name'] == args.region), None)
        if selected is None:
            raise poc.PocError('The requested deployment region is not a READY subscription.')
    else:
        default = next((r for r in ready if r['region-name'] == identity.get('region')), None)
        selected = discovery.choose('Deployment region',ready,lambda r:r['region-name'],default=default)
    api = discovery.Discovery(profile,selected['region-name'])
    compartments = api.get('iam','compartment','list','--compartment-id',tenancy,
                           '--compartment-id-in-subtree','true','--access-level','ACCESSIBLE','--all')
    active = [c for c in compartments if c.get('lifecycle-state') == 'ACTIVE']
    if args.compartment_id:
        compartment = next((c for c in active if c['id'] == args.compartment_id), None)
        if compartment is None:
            raise poc.PocError('The requested compartment is not in the accessible Active compartment list.')
    else:
        compartment = discovery.choose('Existing POC compartment',active,discovery.compartment_labels(active))
    seed = json.loads((poc.ROOT/'config/customer.example.json').read_text())
    seed.update(oci_profile=profile,tenancy_id=tenancy,region=selected['region-name'],
                home_region=homes[0],compartment_id=compartment['id'])
    if args.name:
        seed.update(name=args.name,state_directory=f'deployments/{args.name}/states')
    local = poc.ROOT/'.local'
    local.mkdir(parents=True,exist_ok=True,mode=0o700)
    with tempfile.TemporaryDirectory(prefix='customer-setup-',dir=local) as directory:
        seed_file = Path(directory)/'seed.json'
        poc.private_json(seed_file,seed)
        poc.configure(argparse.Namespace(config=destination,seed_config=seed_file))


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile',help='API-key OCI profile in ~/.oci/config')
    parser.add_argument('--region',help='Subscribed deployment region; IAM home region is discovered')
    parser.add_argument('--compartment-id',help='Optional accessible Active compartment OCID')
    parser.add_argument('--name',help='Default POC name for the remaining wizard')
    parser.add_argument('--config',type=Path,default=poc.ROOT/'poc-config.json')
    args=parser.parse_args()
    os.umask(0o077)
    if not shutil.which('oci'):
        raise poc.PocError('Install OCI CLI and configure an API-key profile before setup.')
    configure(args)


if __name__=='__main__':
    try:
        main()
    except (poc.PocError,OSError,ValueError,EOFError) as error:
        raise SystemExit(str(error)) from None
    except KeyboardInterrupt:
        raise SystemExit('Setup cancelled; no cloud resources were created.') from None

#!/usr/bin/env python3
"""Interactively generate a new POC configuration using read-only OCI discovery."""
import argparse
import configparser
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile

from inference_stack import DEFAULTS, PocError, ROOT, load_config, image_in_region


def ask(label, default=None, validate=None):
    while True:
        suffix = f' [{default}]' if default is not None else ''
        value = input(f'{label}{suffix}: ').strip() or default
        if value is None or value == '':
            print('A value is required.')
            continue
        try:
            return validate(value) if validate else value
        except (ValueError, PocError) as error:
            print(f'Please correct this value: {error}')


def choose(label, items, display, default=None):
    """Searchable numbered selection; no need to copy OCIDs from the console."""
    if not items:
        raise PocError(f'No choices found for {label}.')
    filtered, page, size = items, 0, 12
    while True:
        pages = (len(filtered)+size-1)//size
        print(f'\n{label} — {len(filtered)} matches, page {page+1}/{pages}')
        for index, item in enumerate(filtered[page*size:(page+1)*size], start=page*size+1):
            print(f'  {index}. {display(item)}')
        prompt = 'Number, search text, n=next, p=previous, *=all'
        if default is not None:
            prompt += ', Enter='+display(default)
        value = input(prompt+': ').strip()
        if not value and default is not None:
            return default
        if value.lower() == 'n':
            page = min(page+1, pages-1)
        elif value.lower() == 'p':
            page = max(page-1, 0)
        elif value == '*':
            filtered, page = items, 0
        elif value.isdigit() and 1 <= int(value) <= len(filtered):
            return filtered[int(value)-1]
        else:
            matches = [item for item in items if value.lower() in display(item).lower()]
            if len(matches) == 1:
                return matches[0]
            if matches:
                filtered, page = matches, 0
            else:
                print('No matches. Try a different search or enter *.')


class Discovery:
    def __init__(self, profile, region):
        self.profile, self.region = profile, region

    def get(self, *arguments):
        command = ['oci', '--profile', self.profile, '--region', self.region,
                   '--connection-timeout', '10', '--read-timeout', '30', '--no-retry', *arguments]
        result = subprocess.run(command, capture_output=True, text=True, timeout=90)
        if result.returncode:
            message = 'OCI request failed.'
            try:
                error = json.loads(result.stderr[result.stderr.index('{'):])
                message = f'{error.get("code", "Error")}: {error.get("message", "Request failed")}'
            except (ValueError, KeyError):
                pass
            raise PocError(message+' Check the selected profile, region and read permissions.')
        if not result.stdout.strip():
            return []
        return json.loads(result.stdout)['data']


def profiles(path):
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    if not parser.read(path):
        raise PocError('No ~/.oci/config found. Run `oci setup config` first, then rerun ./setup.sh.')
    names = (['DEFAULT'] if parser.defaults() else []) + parser.sections()
    # Only profile names/region/tenancy identifiers are copied; signing keys stay outside the lab config.
    return {name: {'region': parser[name].get('region'), 'tenancy': parser[name].get('tenancy')}
            for name in names if parser[name].get('tenancy')}


def compartment_labels(compartments):
    by_id = {c['id']: c for c in compartments}

    def label(item):
        chain, current, visited = [], item, set()
        while current and current['id'] not in visited:
            chain.append(current['name'])
            visited.add(current['id'])
            current = by_id.get(current.get('compartment-id'))
        return '/'.join(reversed(chain)) + '  ['+item['id'][-12:]+']'

    return label


def private_network(value):
    network = ipaddress.ip_network(value, strict=True)
    allowed = [ipaddress.ip_network(c) for c in ['10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16']]
    if network.version != 4 or network.prefixlen != 16 or not any(network.subnet_of(c) for c in allowed):
        raise ValueError('Use an RFC1918 IPv4 /16, for example 10.140.0.0/16.')
    return str(network)


def suggest_network(existing):
    for second in range(140, 256):
        candidate = ipaddress.ip_network(f'10.{second}.0.0/16')
        if not any(candidate.overlaps(n) for n in existing):
            return str(candidate)
    raise PocError('No default /16 is free in the checked range. Review the existing network plan.')


def admin_network(value):
    # Explicit opt-in to an unrestricted Bastion client allowlist.
    if value == '0.0.0.0/0':
        return value
    if '/' not in value:
        value += '/32'
    network = ipaddress.ip_network(value, strict=True)
    if network.version != 4 or network.prefixlen != 32:
        raise ValueError('Enter one public OCI SSH egress IPv4 address (/32), or 0.0.0.0/0.')
    if not network.network_address.is_global or network.network_address.is_multicast:
        raise ValueError('Bastion requires your public egress address, not a local/private address.')
    return str(network)


def validate_image(api, image_id, shape, *, monitoring=False):
    if not image_in_region(image_id, api.region):
        raise PocError(f'Choose an image in {api.region}.')
    image = api.get('compute', 'image', 'get', '--image-id', image_id)
    if image.get('lifecycle-state') != 'AVAILABLE' or image.get('operating-system') != 'Canonical Ubuntu':
        raise PocError('Choose an AVAILABLE Canonical Ubuntu image.')
    if monitoring and (image.get('operating-system-version') != '24.04'
                       or image.get('size-in-mbs', 0) > 50*1024):
        raise PocError('The current monitoring template uses Ubuntu 24.04 with a 50 GiB boot disk. '
                       'Use the automatic platform-image selection.')
    if not monitoring and image.get('size-in-mbs', 0) > 200*1024:
        raise PocError('The current GPU template uses a 200 GiB boot disk. '
                       'Choose a validated image that fits, or review the GPU boot-volume configuration first.')
    api.get('compute', 'image-shape-compatibility-entry', 'get',
            '--image-id', image_id, '--shape-name', shape)
    return image


def platform_image(api, compartment_id, shape):
    images = api.get('compute', 'image', 'list', '--compartment-id', compartment_id,
                     '--operating-system', 'Canonical Ubuntu', '--operating-system-version', '24.04',
                     '--shape', shape, '--lifecycle-state', 'AVAILABLE', '--sort-by', 'TIMECREATED',
                     '--sort-order', 'DESC', '--all')
    standard = [i for i in images if i.get('compartment-id') is None]
    if not standard:
        raise PocError('No Ubuntu 24.04 platform image found for the monitoring shape in this region.')
    selected = max(standard, key=lambda i: i['time-created'])
    return validate_image(api, selected['id'], shape, monitoring=True)


def save_new_config(path, data):
    """Validate, then publish atomically without ever overwriting an existing file."""
    path = path.resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        descriptor, name = tempfile.mkstemp(prefix='.poc-config-check-', suffix='.json', dir=path.parent)
        temporary = Path(name)
        with os.fdopen(descriptor, 'w') as out:
            json.dump(data, out, indent=2)
            out.write('\n')
            out.flush()
            os.fsync(out.fileno())
        load_config(temporary)
        # link() fails if the final path appeared meanwhile; replace() would overwrite it.
        os.link(temporary, path)
    except FileExistsError as error:
        raise PocError(f'{path} already exists. It was not changed. Choose a different --output file.') from error
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)


def state_is_used(path):
    return (path/'.pocctl/inventory.json').exists() or any(path.glob('*/terraform.tfstate'))


def configure(args):
    print('Standalone POC configuration wizard')
    print('OCI discovery is read-only. This command writes a configuration file; it creates no cloud resources.\n')
    if args.output and args.output.expanduser().exists():
        raise PocError(f'{args.output} already exists. Existing configuration will not be overwritten.')
    available = profiles(Path.home()/'.oci/config')
    if not available:
        raise PocError('No OCI profile with a tenancy OCID found. Run `oci setup config` first.')
    if args.profile:
        if args.profile not in available:
            raise PocError(f'Profile {args.profile!r} does not exist in ~/.oci/config.')
        profile = args.profile
    else:
        profile = choose('OCI profile', sorted(available), lambda p: p,
                         next(iter(available)) if len(available) == 1 else None)
    initial_region = args.region or available[profile]['region']
    if not initial_region:
        initial_region = ask('OCI region identifier')
    api = Discovery(profile, initial_region)
    tenancy = available[profile]['tenancy']
    subscriptions = api.get('iam', 'region-subscription', 'list', '--tenancy-id', tenancy)
    regions = sorted({r['region-name'] for r in subscriptions if r['status'] == 'READY'})
    if args.region:
        if args.region not in regions:
            raise PocError('The requested region is not a READY tenancy subscription.')
        region = args.region
    else:
        region = choose('Subscribed region', regions, lambda r: r,
                        initial_region if initial_region in regions else None)
    api.region = region
    compartments = api.get('iam', 'compartment', 'list', '--compartment-id', tenancy,
                           '--compartment-id-in-subtree', 'true', '--access-level', 'ACCESSIBLE', '--all')
    compartments = [c for c in compartments if c['lifecycle-state'] == 'ACTIVE']
    label = compartment_labels(compartments)
    compartment = choose('Deployment compartment', sorted(compartments, key=label), label)
    domains = api.get('iam', 'availability-domain', 'list', '--compartment-id', tenancy)
    domain = choose('Availability domain — use one with confirmed A10.2 capacity', domains, lambda d: d['name'])

    def lab_name(value):
        if not re.fullmatch(r'[a-z][a-z0-9-]{2,35}', value):
            raise ValueError('Use 3–36 lowercase letters, digits or hyphens, starting with a letter.')
        return value

    name = lab_name(args.name) if args.name else ask('New POC name', 'my-vllm-lab', lab_name)
    output = args.output.expanduser().resolve() if args.output else ROOT/'poc-config.json'
    if output.exists():
        output = ROOT/f'poc-config.{name}.local.json'
        print(f'Existing poc-config.json is preserved. New configuration: {output.name}')
    if output.exists():
        raise PocError(f'{output} already exists; choose --output with a new filename.')
    state_dir = f'deployments/{name}/infra'
    if state_is_used(output.parent/state_dir):
        raise PocError(f'This POC name already has state at {state_dir}. Choose a new name; '
                       'use its original configuration to manage an existing lab.')

    vcns = api.get('network', 'vcn', 'list', '--compartment-id', compartment['id'], '--all')
    existing = [ipaddress.ip_network(c) for v in vcns if v.get('lifecycle-state') != 'TERMINATED'
                for c in (v.get('cidr-blocks') or [v.get('cidr-block')]) if c]

    def non_overlapping(value):
        value = private_network(value)
        if any(ipaddress.ip_network(value).overlaps(n) for n in existing):
            raise ValueError('That /16 overlaps a VCN visible in the selected compartment. Choose another.')
        return value

    vcn = ask('New VCN IPv4 /16', suggest_network(existing), non_overlapping)
    print('Overlap check covers the selected compartment only. Verify connected/AIDP/on-premises ranges before apply.')

    print('\nGPU image: choose your validated custom Ubuntu image with the required NVIDIA/CUDA/DCGM stack.')
    images = api.get('compute', 'image', 'list', '--compartment-id', compartment['id'],
                     '--operating-system', 'Canonical Ubuntu', '--shape', DEFAULTS['gpu_shape'],
                     '--lifecycle-state', 'AVAILABLE', '--sort-by', 'TIMECREATED', '--sort-order', 'DESC', '--all')
    images = [i for i in images if i.get('compartment-id') is not None]
    def image_selection(value):
        if value not in ('list', 'ocid'):
            raise ValueError('Enter list or ocid.')
        return value

    if images:
        mode = ask('GPU image selection: list or paste an OCID', 'list',
                   image_selection)
        if mode == 'list':
            image = choose('Custom GPU images', images, lambda i: i['display-name']+'  ['+i['id'][-12:]+']')
            gpu_id = image['id']
        else:
            gpu_id = ask('GPU custom image OCID')
    else:
        print('No compatible custom images listed here. You can supply an accessible image from another compartment.')
        gpu_id = ask('GPU custom image OCID')
    gpu = validate_image(api, gpu_id, DEFAULTS['gpu_shape'])
    print('GPU image:', gpu['display-name'])
    print('Compatibility metadata does not verify the installed driver/DCGM or reserve GPU capacity.')

    override = ask('Monitoring image: Enter for automatic Ubuntu 24.04, or paste an OCID', 'auto')
    monitor = (platform_image(api, compartment['id'], DEFAULTS['monitoring_shape']) if override == 'auto'
               else validate_image(api, override, DEFAULTS['monitoring_shape'], monitoring=True))
    print('Selected monitoring image:', monitor['display-name'])

    key_candidates = [p for p in [Path.home()/'.ssh/id_ed25519', Path.home()/'.ssh/id_rsa']
                      if p.is_file() and Path(str(p)+'.pub').is_file()]

    def ssh_key(value):
        key = Path(value).expanduser().resolve()
        if not key.is_file() or not Path(str(key)+'.pub').is_file():
            raise ValueError('Both the SSH private key and matching .pub file must exist.')
        return str(key)

    key = ask('VM SSH private key path', str(key_candidates[0]) if key_candidates else None, ssh_key)
    print('Bastion needs the public egress IP used for SSH to OCI. HTTP IP-check sites may report a different IP.')
    admin = ask('OCI SSH egress IPv4 address (/32), or 0.0.0.0/0 for any IPv4 source', validate=admin_network)
    config = {**DEFAULTS, 'name': name, 'oci_profile': profile, 'region': region,
              'compartment_id': compartment['id'], 'availability_domain': domain['name'],
              'admin_cidr': admin, 'vcn_cidr': vcn, 'gpu_image_id': gpu_id,
              'monitoring_image_id': monitor['id'], 'ssh_private_key_path': key,
              'state_directory': state_dir}
    print('\nReview configuration:\n'+json.dumps(config, indent=2))
    print('Output:', output)
    print('OCI credentials remain in ~/.oci/config and its referenced signing-key file.')
    if input('Write this new configuration? [y/N]: ').strip().lower() not in ('y', 'yes'):
        print('Cancelled. No configuration was written.')
        return None
    save_new_config(output, config)
    quoted = shlex.quote(str(output))
    print(f'\nSaved configuration (0600): {output}')
    print('No cloud resources were created. Next commands:')
    print(f'./deploy.sh --config {quoted} --check')
    print(f'./deploy.sh --config {quoted}')
    print(f'./deploy.sh --config {quoted} --apply')
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, help='New output file; an existing file is never overwritten')
    parser.add_argument('--profile', help='Preselect a profile from ~/.oci/config')
    parser.add_argument('--region', help='Preselect a subscribed region')
    parser.add_argument('--name', help='Preselect a new POC name')
    args = parser.parse_args()
    if not shutil.which('oci'):
        raise PocError('OCI CLI is required. Install it and run `oci setup config` first.')
    os.umask(0o077)
    configure(args)


if __name__ == '__main__':
    try:
        main()
    except (PocError, OSError, ValueError, EOFError, subprocess.SubprocessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print('\nCancelled. No cloud resources were created.', file=sys.stderr)
        sys.exit(130)

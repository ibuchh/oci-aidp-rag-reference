#!/usr/bin/env python3
"""Plan, deploy and remove the vllm lab using isolated Terraform configs."""
import argparse
import datetime as dt
import fcntl
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import time

from terraform_progress import STREAM_COMMANDS, run_terraform

ROOT = Path(__file__).resolve().parent.parent
STACKS = ('shared', 'monitoring', 'vllm')
PROTECTED = {'shared': [('oci_file_storage_file_system', 'models')],
             'vllm': [('oci_core_instance', 'gpu')],
             'monitoring': [('oci_core_instance', 'monitoring'), ('oci_core_volume', 'metrics')]}
DEFAULTS = dict(region='eu-frankfurt-1', vcn_cidr='10.160.0.0/16',
                ssh_private_key_path='~/.ssh/id_rsa',
                model_revision='b968826d9c46dd6066d109eabc6255188de91218',
                gpu_shape='VM.GPU.A10.2', monitoring_shape='VM.Standard.E5.Flex',
                monitoring_ocpus=2, monitoring_memory_gbs=16, metrics_volume_gbs=100,
                bootstrap_timeout_seconds=3600, grafana_hostname=None,
                gpu_boot_volume_id=None, bastion_name=None)
REQUIRED = {'name', 'oci_profile', 'compartment_id', 'availability_domain',
            'admin_cidr', 'gpu_image_id', 'monitoring_image_id', 'state_directory'}


class PocError(RuntimeError):
    pass


def private_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Atomic replacement, including on retries after interruption.
    temporary = path.with_name(path.name + '.tmp')
    with os.fdopen(os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as out:
        json.dump(data, out, indent=2)
        out.write('\n')
    os.chmod(temporary, 0o600)
    temporary.replace(path)


def canonical_region(region):
    """Normalize legacy keys returned in OCIDs and instance region attributes."""
    return {'iad': 'us-ashburn-1', 'phx': 'us-phoenix-1',
            'fra': 'eu-frankfurt-1', 'lhr': 'uk-london-1'}.get(region, region)


def image_in_region(image_id, region):
    """Accept legacy OCID region keys as well as full region identifiers."""
    parts = image_id.split('.')
    return (len(parts) >= 5 and parts[:3] == ['ocid1', 'image', 'oc1']
            and canonical_region(parts[3]) == canonical_region(region)
            and bool(parts[-1]))


def load_config(path):
    path = Path(path).resolve()
    raw = json.loads(path.read_text())
    if not isinstance(raw, dict):
        raise PocError('Configuration must be a JSON object.')
    unknown = set(raw) - set(DEFAULTS) - REQUIRED
    missing = REQUIRED - set(raw)
    if unknown or missing:
        raise PocError(f'Configuration fields: missing={sorted(missing)}, unknown={sorted(unknown)}')
    cfg = {**DEFAULTS, **raw}
    string_fields = REQUIRED | {'region', 'vcn_cidr', 'ssh_private_key_path',
                                'model_revision', 'gpu_shape', 'monitoring_shape'}
    if any(not isinstance(cfg[k], str) or not cfg[k].strip() for k in string_fields):
        raise PocError('Required configuration fields must be non-empty strings.')
    if any('REPLACE_' in str(v) or 'YOUR_' in str(v) for v in cfg.values()):
        raise PocError('Fill the placeholders in your configuration before continuing.')
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,35}', cfg['name']):
        raise PocError('name must be 3–36 lowercase letters, digits or hyphens, starting with a letter.')
    if not cfg['compartment_id'].startswith('ocid1.compartment.'):
        raise PocError('Expected a compartment OCID, not its display name.')
    for field in ('gpu_image_id', 'monitoring_image_id'):
        if not image_in_region(cfg[field], cfg['region']):
            raise PocError(f'{field} must be an image in {cfg["region"]}.')
    network = ipaddress.ip_network(cfg['vcn_cidr'], strict=True)
    admin = ipaddress.ip_network(cfg['admin_cidr'], strict=True)
    private_networks = [ipaddress.ip_network(v) for v in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
    if (network.version != 4 or network.prefixlen != 16
            or not any(network.subnet_of(v) for v in private_networks)):
        raise PocError('This POC requires an RFC1918 IPv4 /16 VCN for its reserved subnet layout.')
    if admin.version != 4 or (admin.prefixlen != 32 and str(admin) != '0.0.0.0/0'):
        raise PocError('admin_cidr must be an OCI SSH egress IPv4 address with /32, or 0.0.0.0/0.')
    if cfg['gpu_shape'] != 'VM.GPU.A10.2':
        raise PocError('This tested two-GPU serving configuration requires VM.GPU.A10.2.')
    if not re.fullmatch(r'[0-9a-f]{40}', cfg['model_revision']):
        raise PocError('model_revision must be an immutable 40-character commit SHA.')
    for field in ('monitoring_ocpus', 'monitoring_memory_gbs', 'metrics_volume_gbs', 'bootstrap_timeout_seconds'):
        if type(cfg[field]) is not int or cfg[field] <= 0:
            raise PocError(f'{field} must be a positive integer.')
    if cfg['metrics_volume_gbs'] < 100:
        raise PocError('The configured 60 GB retention requires at least a 100 GiB volume.')
    hostname = cfg['grafana_hostname']
    if hostname is not None and (not isinstance(hostname, str) or
            not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', hostname)):
        raise PocError('grafana_hostname must be a plain lowercase DNS name, without URL or port.')
    if cfg['bastion_name'] is None:
        cfg['bastion_name'] = re.sub('[^a-z0-9]', '', cfg['name'])[:24] + hashlib.sha256(cfg['name'].encode()).hexdigest()[:6]
    if not isinstance(cfg['bastion_name'], str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,63}', cfg['bastion_name']):
        raise PocError('bastion_name must be alphanumeric, starting with a letter, at most 64 characters.')
    key = Path(cfg['ssh_private_key_path']).expanduser()
    if not key.is_absolute():
        key = path.parent/key
    if not key.is_file() or not Path(str(key)+'.pub').is_file():
        raise PocError('Both ssh_private_key_path and its .pub file must exist.')
    cfg['ssh_private_key_path'] = str(key.resolve())
    state_dir = Path(cfg['state_directory']).expanduser()
    if not state_dir.is_absolute():
        state_dir = path.parent/state_dir
    cfg['state_directory'] = str(state_dir.resolve())
    if cfg['gpu_boot_volume_id'] is not None and (not isinstance(cfg['gpu_boot_volume_id'], str)
            or not cfg['gpu_boot_volume_id'].startswith('ocid1.bootvolume.')):
        raise PocError('gpu_boot_volume_id must be null or an explicit recovery boot-volume OCID.')
    return cfg


def state(path):
    return json.loads(path.read_text()) if path.exists() else {'resources': [], 'outputs': {}}


def managed(document):
    return [r for r in document.get('resources', []) if r.get('mode') == 'managed' and r.get('instances')]


def guard_plan(document, action):
    changes = []
    for resource in document.get('resource_changes', []):
        if resource.get('mode') != 'managed':
            continue
        actions = resource['change']['actions']
        if actions == ['no-op']:
            continue
        if action == 'deploy' and actions not in (['create'], ['update']):
            raise PocError(f'Refusing deletion/replacement: {resource["address"]}: {actions}')
        if action == 'destroy' and actions != ['delete']:
            raise PocError(f'Refusing non-delete action in cleanup: {resource["address"]}: {actions}')
        changes.append((resource['address'], actions))
    return changes


class InferenceStack:
    def __init__(self, config, args):
        self.config, self.args = config, args
        self.states = Path(config['state_directory'])
        self.meta = self.states/'.pocctl'
        self.meta.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.meta.chmod(0o700)
        run_id = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
        self.run = self.meta/'runs'/run_id
        self.run.mkdir(parents=True, mode=0o700)
        self.log = self.run/'commands.log'
        private_json(self.run/'config.json', config)
        self.plugin_cache = self.meta/'providers'
        self.plugin_cache.mkdir(exist_ok=True)
        cached = ROOT/'terraform/shared/.terraform/providers'
        if cached.exists():
            for source in cached.rglob('terraform-provider-*'):
                if source.is_file():
                    destination = self.plugin_cache/source.relative_to(cached)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if not destination.exists():
                        destination.symlink_to(source.resolve())
        self.inventory_path = self.meta/'inventory.json'
        self.inventory = (json.loads(self.inventory_path.read_text()) if self.inventory_path.exists()
                          else {'name': config['name'], 'compartment_id': config['compartment_id'], 'boot_volumes': {}})
        if (self.inventory['name'], self.inventory['compartment_id']) != (config['name'], config['compartment_id']):
            raise PocError('State directory belongs to a different POC name or compartment.')

    def command(self, argv, *, capture=False, accepted=(0,), timeout=None):
        environment = dict(os.environ)
        if str(argv[0]) == 'terraform':
            for key in list(environment):
                if key.startswith(('TF_CLI_ARGS', 'TF_VAR_')) or key in ('TF_DATA_DIR', 'TF_WORKSPACE'):
                    environment.pop(key)
            environment.update(TF_IN_AUTOMATION='1', TF_WORKSPACE='default',
                               TF_PLUGIN_CACHE_DIR=str(self.plugin_cache))
        operation = next((str(a) for a in argv[1:] if not str(a).startswith('-')), '')
        if str(argv[0]) == 'terraform' and operation in STREAM_COMMANDS:
            result = run_terraform(argv, log_path=self.log, env=environment, timeout=timeout,
                                   label=f'Terraform {operation}')
        else:
            result = subprocess.run([str(a) for a in argv], capture_output=True, text=True,
                                    timeout=timeout, env=environment)
            with self.log.open('a') as out:
                out.write('$ ' + ' '.join(str(a) for a in argv) + '\n' + result.stdout + result.stderr + '\n')
        if result.returncode not in accepted:
            raise PocError(f'Command failed ({result.returncode}): {argv[0]}. Details: {self.log}')
        if capture:
            return result.stdout
        return result.returncode

    def oci(self, *argv, capture=True):
        output = self.command(['oci', '--profile', self.config['oci_profile'], '--region', self.config['region'],
                               *argv], capture=True)
        return json.loads(output)['data'] if capture and output.strip() else None

    def output(self, stack):
        return state(self.states/stack/'terraform.tfstate').get('outputs', {})

    def snapshot(self):
        for stack in STACKS:
            source = self.states/stack/'terraform.tfstate'
            document = state(source)
            evidence = self.run/f'{stack}-before.json'
            if not evidence.exists():
                private_json(evidence, document)
            declarations = set()
            for template in (ROOT/'terraform'/stack).glob('*.tf'):
                declarations.update(re.findall(r'\bresource\s+"([^"]+)"\s+"([^"]+)"', template.read_text()))
            for resource in managed(document):
                if resource.get('module') or (resource['type'], resource['name']) not in declarations:
                    raise PocError(f'{stack} contains a resource outside this POC template: '
                                   f'{resource["type"]}.{resource["name"]}. No cleanup allowed.')
                for item in resource['instances']:
                    attrs = item['attributes']
                    compartment = attrs.get('compartment_id')
                    if compartment and compartment != self.config['compartment_id']:
                        raise PocError(f'{stack} contains resources in a different compartment; stop and review state.')
                    project = attrs.get('freeform_tags', {}).get('Project')
                    if project and project != self.config['name']:
                        raise PocError(f'{stack} contains resources tagged for a different Project: {project}')
                    if attrs.get('region') and canonical_region(attrs['region']) != canonical_region(self.config['region']):
                        raise PocError(f'{stack} contains an instance in a different region.')
                    if resource['type'] == 'oci_core_instance' and attrs.get('boot_volume_id'):
                        volume = attrs['boot_volume_id']
                        previous = self.inventory['boot_volumes'].get(volume, {})
                        self.inventory['boot_volumes'][volume] = {
                            **previous, 'instance_id': attrs['id'], 'stack': stack,
                            'compartment_id': attrs['compartment_id'], 'availability_domain': attrs['availability_domain']}
        private_json(self.inventory_path, self.inventory)

    def variables(self, stack):
        c = self.config
        common = {k: c[k] for k in ('name', 'oci_profile', 'region', 'compartment_id', 'availability_domain')}
        network = ipaddress.ip_network(c['vcn_cidr'])
        gpu_ip = str(network.network_address + 33*256 + 19)
        monitor_ip = str(network.network_address + 35*256 + 10)
        if stack == 'shared':
            return {**common, 'admin_cidr': c['admin_cidr'], 'vcn_cidr': c['vcn_cidr'], 'bastion_name': c['bastion_name']}
        if stack == 'monitoring':
            return {**common, 'image_id': c['monitoring_image_id'], 'ssh_public_key_path': c['ssh_private_key_path']+'.pub',
                    'subnet_cidr': str(list(network.subnets(new_prefix=24))[35]), 'private_ip': monitor_ip,
                    'shape': c['monitoring_shape'], 'ocpus': c['monitoring_ocpus'],
                    'memory_gbs': c['monitoring_memory_gbs'], 'metrics_volume_gbs': c['metrics_volume_gbs']}
        nsg = self.output('monitoring').get('gpu_metrics_nsg_id', {}).get('value')
        boot_id = c['gpu_boot_volume_id']
        if boot_id and self.inventory['boot_volumes'].get(boot_id, {}).get('deleted'):
            boot_id = None
        return {**common, 'image_id': c['gpu_image_id'], 'shape': c['gpu_shape'],
                'boot_volume_id': boot_id, 'private_ip': gpu_ip,
                'ssh_public_key_path': c['ssh_private_key_path']+'.pub', 'model_revision': c['model_revision'],
                'monitoring_nsg_ids': [nsg] if nsg else []}

    def prepare(self, stack, destroy=False):
        folder = self.run/'infra'/stack
        folder.mkdir(parents=True, exist_ok=True)
        for source in (ROOT/'terraform'/stack).iterdir():
            if source.name.endswith(('.tf', '.tftpl')) or source.name == '.terraform.lock.hcl':
                shutil.copy2(source, folder/source.name)
        state_path = self.states/stack/'terraform.tfstate'
        state_path.parent.mkdir(parents=True, exist_ok=True)
        override = {'terraform': {'backend': {'local': {'path': str(state_path)}}}}
        if stack != 'shared':
            override['data'] = {'terraform_remote_state': {'shared': {'config': {
                'path': str(self.states/'shared'/'terraform.tfstate')}}}}
        if destroy:
            override['resource'] = {}
            for kind, name in PROTECTED[stack]:
                override['resource'].setdefault(kind, {})[name] = {'lifecycle': {'prevent_destroy': False}}
        private_json(folder/'automation_override.tf.json', override)
        private_json(folder/'terraform.tfvars.json', self.variables(stack))
        # Persist the exact non-destructive configuration/variables alongside new state.
        # Existing hand-authored source trees (including this running POC) stay untouched.
        canonical = self.states/stack
        if canonical.resolve() != (ROOT/'terraform'/stack).resolve():
            for source in folder.iterdir():
                if source.name != 'automation_override.tf.json' and source.is_file():
                    shutil.copy2(source, canonical/source.name)
            normal = {k: v for k, v in override.items() if k != 'resource'}
            private_json(canonical/'automation_override.tf.json', normal)
        self.command(['terraform', f'-chdir={folder}', 'init', '-input=false', '-no-color'])
        self.command(['terraform', f'-chdir={folder}', 'validate', '-no-color'])
        return folder

    def plan(self, stack, destroy=False):
        folder = self.prepare(stack, destroy)
        plan = folder/'operation.tfplan'
        argv = ['terraform', f'-chdir={folder}', 'plan', '-input=false', '-no-color',
                '-lock-timeout=60s', '-detailed-exitcode', f'-out={plan}']
        if destroy:
            argv.append('-destroy')
        self.command(argv, accepted=(0, 2))
        raw = self.command(['terraform', f'-chdir={folder}', 'show', '-json', plan], capture=True)
        document = json.loads(raw)
        private_json(folder/'plan.json', document)
        text = self.command(['terraform', f'-chdir={folder}', 'show', '-no-color', plan], capture=True)
        (folder/'plan.txt').write_text(text)
        changes = guard_plan(document, 'destroy' if destroy else 'deploy')
        print(f'\n{stack}: {len(changes)} resource changes. Full plan: {folder/"plan.txt"}', flush=True)
        for address, actions in changes:
            print(f'  {", ".join(actions)}: {address}', flush=True)
        return folder, plan, changes

    def apply(self, folder, plan):
        self.command(['terraform', f'-chdir={folder}', 'apply', '-input=false', '-no-color',
                      '-lock-timeout=60s', plan])
        self.snapshot()

    def confirm(self, phrase):
        if self.args.yes:
            if self.args.action == 'destroy' and self.args.confirm_name != self.config['name']:
                raise PocError('Non-interactive deletion requires --confirm-name matching the configured POC name.')
            return
        if not sys.stdin.isatty():
            raise PocError('Confirmation requires a terminal; use documented non-interactive flags for CI.')
        if input(f'\nType {phrase!r} to continue: ') != phrase:
            raise PocError('Cancelled; no pending plan was applied.')

    def preflight_cloud(self):
        compartment = self.oci('iam', 'compartment', 'get', '--compartment-id', self.config['compartment_id'])
        print(f'OCI compartment: {compartment["name"]}; region: {self.config["region"]}', flush=True)
        if self.args.action == 'deploy':
            for field in ('gpu_image_id', 'monitoring_image_id'):
                image = self.oci('compute', 'image', 'get', '--image-id', self.config[field])
                if image['lifecycle-state'] != 'AVAILABLE' or image['operating-system'] != 'Canonical Ubuntu':
                    raise PocError(f'{field} must be an AVAILABLE Ubuntu image.')

    def deploy(self):
        for stack in STACKS:
            if stack != 'shared' and not self.output('shared').get('gpu_subnet_id'):
                self.prepare(stack)
                print(f'{stack}: dependent plan awaits shared deployment. Re-run with --apply to proceed in order.')
                continue
            folder, plan, changes = self.plan(stack)
            if self.args.apply:
                if changes:
                    self.confirm(f'APPLY {self.config["name"]} {stack}')
                    self.apply(folder, plan)
                else:
                    print(f'{stack}: no infrastructure change needed.')
        if not self.args.apply:
            print('\nPreview only. No infrastructure or host configuration was changed.')
            return
        if self.args.infra_only:
            print('\nInfrastructure applied. Host setup skipped by --infra-only.')
            return
        from hosts import configure_hosts
        configure_hosts(self)

    def boot_volume_inventory(self):
        result = []
        for volume_id, record in self.inventory['boot_volumes'].items():
            if record.get('deleted'):
                continue
            try:
                data = self.oci('bv', 'boot-volume', 'get', '--boot-volume-id', volume_id)
            except PocError:
                if not record.get('delete_requested'):
                    raise
                # Resolve the delete-accepted/network-disconnected window without scanning by tag.
                listing = self.oci('bv', 'boot-volume', 'list', '--compartment-id',
                                   self.config['compartment_id'], '--availability-domain',
                                   record['availability_domain'], '--all')
                if any(v['id'] == volume_id for v in listing):
                    raise
                record['deleted'] = True
                continue
            if (data['compartment-id'] != self.config['compartment_id'] or
                    data['availability-domain'] != record['availability_domain']):
                raise PocError('Boot-volume ownership/domain differs from the recorded inventory.')
            if data['lifecycle-state'] == 'TERMINATED':
                record['deleted'] = True
                continue
            result.append(volume_id)
            print(f'  retained boot volume to DELETE: {volume_id} ({data["size-in-gbs"]} GiB)', flush=True)
        private_json(self.inventory_path, self.inventory)
        return result

    def destroy(self):
        plans = []
        for stack in ('vllm', 'monitoring', 'shared'):
            if not managed(state(self.states/stack/'terraform.tfstate')):
                print(f'{stack}: no managed resources remain in state.')
                continue
            folder, plan, changes = self.plan(stack, destroy=True)
            if changes:
                plans.append((folder, plan))
        volumes = self.boot_volume_inventory()
        print('\nScope: these three states and their recorded retained boot volumes only.')
        print('FSS models, metric history, Grafana data and both VM disks will be permanently deleted.')
        print('Imported images, compartment, IAM policies, unrelated resources and pre-existing backups are retained.')
        if not self.args.apply:
            print('Preview only. Nothing deleted. Use --apply --delete-data only after reviewing the plans and backups.')
            return
        if not self.args.delete_data:
            raise PocError('Destructive apply requires --delete-data. No plan has been applied.')
        self.confirm(f'DELETE {self.config["name"]}')
        # Saved plans are reviewed before any mutation; dependencies dictate the apply order.
        for folder, plan in plans:
            self.apply(folder, plan)
        for volume_id in volumes:
            deadline = time.monotonic()+600
            while True:
                data = self.oci('bv', 'boot-volume', 'get', '--boot-volume-id', volume_id)
                if data['lifecycle-state'] == 'AVAILABLE':
                    break
                if time.monotonic() > deadline:
                    raise PocError(f'Volume did not detach: {volume_id}; inventory retained for retry.')
                time.sleep(5)
            self.inventory['boot_volumes'][volume_id]['delete_requested'] = True
            private_json(self.inventory_path, self.inventory)
            self.oci('bv', 'boot-volume', 'delete', '--boot-volume-id', volume_id,
                     '--force', '--wait-for-state', 'TERMINATED', '--max-wait-seconds', '600', capture=False)
            self.inventory['boot_volumes'][volume_id]['deleted'] = True
            private_json(self.inventory_path, self.inventory)
        if any(managed(state(self.states/s/'terraform.tfstate')) for s in STACKS):
            raise PocError('Managed resources remain in state. Inspect the cleanup log.')
        print('\nCleanup completed. Keep state and the audit inventory; no state files were removed.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['deploy', 'destroy'])
    parser.add_argument('--config', type=Path, default=ROOT/'poc-config.json')
    parser.add_argument('--apply', action='store_true', help='Apply reviewed plans; default is preview only')
    parser.add_argument('--check', action='store_true', help='Validate config/Terraform locally, without OCI API calls')
    parser.add_argument('--yes', action='store_true', help='Non-interactive mode; destruction also needs --confirm-name')
    parser.add_argument('--confirm-name')
    parser.add_argument('--delete-data', action='store_true')
    parser.add_argument('--infra-only', action='store_true', help='Skip SSH, model readiness and monitoring setup')
    args = parser.parse_args(argv)
    if args.check and args.apply:
        parser.error('--check and --apply are mutually exclusive')
    if args.yes and not args.apply:
        parser.error('--yes requires --apply')
    if args.action == 'deploy' and (args.delete_data or args.confirm_name):
        parser.error('Deletion flags apply only to destroy')
    if args.action == 'destroy' and args.infra_only:
        parser.error('--infra-only applies only to deploy')
    if args.action == 'destroy' and args.apply and not args.delete_data:
        parser.error('--apply on destroy requires --delete-data')
    os.umask(0o077)
    config = load_config(args.config)
    for program in ('terraform', 'oci', 'ssh', 'ssh-keyscan', 'ssh-keygen'):
        if not shutil.which(program):
            raise PocError(f'Required command not found: {program}')
    lab = InferenceStack(config, args)
    with (lab.meta/'operation.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise PocError('Another POC operation is already running against this state directory.') from exc
        print(f'POC: {config["name"]}\nState: {lab.states}\nEvidence: {lab.run}', flush=True)
        lab.snapshot()
        if args.check:
            for stack in STACKS:
                lab.prepare(stack, destroy=args.action == 'destroy')
            print('Configuration and all three Terraform roots validated. No OCI API calls or applies.')
            return
        lab.preflight_cloud()
        if args.action == 'deploy':
            lab.deploy()
        else:
            lab.destroy()
        lab.snapshot()
        for stack in STACKS:
            private_json(lab.run/f'{stack}-after.json', state(lab.states/stack/'terraform.tfstate'))


if __name__ == '__main__':
    sys.modules.setdefault('inference_stack', sys.modules[__name__])
    try:
        main()
    except (PocError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'ERROR: {error}', file=sys.stderr)
        sys.exit(1)

#!/usr/bin/env python3
"""Same-tenancy POC orchestrator. Cloud mutations require explicit commands/flags."""
import argparse
import configparser
import contextlib
import datetime as dt
import fcntl
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

from terraform_progress import STREAM_COMMANDS, aidp_progress, run_terraform

from inference_stack import InferenceStack, PocError, DEFAULTS, REQUIRED, private_json, load_config, managed, state, guard_plan

ROOT = Path(__file__).resolve().parents[1]
AIDP_STAGES = ('aidp-foundation', 'iam', 'lakehouse', 'aidp', 'aidp-dns', 'aidp-credential-iam', 'runtime-iam')
EXTRA = {'tenancy_id', 'home_region', 'streamlit_username', 'lakehouse_db_name',
         'lakehouse_ecpus', 'lakehouse_storage_tbs', 'runtime_principal_type', 'runtime_principal_id'}
PROTECTED = {'aidp-foundation': [('oci_kms_vault', 'poc'), ('oci_kms_key', 'secrets'), ('oci_vault_secret','lakehouse_admin')],
             'lakehouse': [('oci_database_autonomous_database', 'lakehouse')],
             'aidp': [('oci_ai_data_platform_ai_data_platform', 'poc')]}


def read_config(path, *, complete=True):
    path = path.resolve()
    cfg = json.loads(path.read_text())
    if not isinstance(cfg, dict) or set(cfg) - set(DEFAULTS) - REQUIRED - EXTRA:
        raise PocError('Configuration is not an object or contains unknown fields.')
    for key, prefix in [('tenancy_id', 'ocid1.tenancy.'), ('compartment_id', 'ocid1.compartment.')]:
        if not isinstance(cfg.get(key), str) or not cfg[key].startswith(prefix):
            raise PocError(f'{key} must be an OCID.')
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(Path.home()/'.oci/config')
    profile = cfg['oci_profile']
    if profile not in parser or parser[profile].get('tenancy') != cfg['tenancy_id']:
        raise PocError('OCI profile tenancy differs from the explicitly configured tenancy.')
    if not re.fullmatch(r'[a-z][a-z0-9-]{2,35}', cfg['name']):
        raise PocError('Invalid POC name.')
    if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{2,31}', cfg['streamlit_username']):
        raise PocError('Streamlit username must be 3–32 letters, digits, underscore or hyphen.')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9]{0,13}', cfg['lakehouse_db_name']):
        raise PocError('Lakehouse database name must be 1–14 alphanumeric characters, starting with a letter.')
    for key, minimum in [('lakehouse_ecpus', 2), ('lakehouse_storage_tbs', 1)]:
        if type(cfg.get(key)) is not int or cfg[key] < minimum:
            raise PocError(f'{key} must be a whole number of at least {minimum}.')
    states = Path(cfg['state_directory']).expanduser()
    if not states.is_absolute():
        states = ROOT/states
    states = states.resolve()
    expected = ROOT/'deployments'/cfg['name']/'states'
    if states != expected.resolve():
        raise PocError(f'Use this package’s isolated state directory: {expected}')
    cfg['state_directory'] = str(states)
    if complete:
        key=Path(cfg['ssh_private_key_path']).expanduser()
        cfg['ssh_private_key_path']=str(key if key.is_absolute() else ROOT/key)
        local=ROOT/'.local';local.mkdir(exist_ok=True,mode=0o700)
        descriptor,name=tempfile.mkstemp(prefix='config-check-',suffix='.json',dir=local)
        os.close(descriptor)
        temp=Path(name)
        try:
            private_json(temp, {k:v for k,v in cfg.items() if k not in EXTRA})
            checked = load_config(temp)
        finally:temp.unlink(missing_ok=True)
        cfg.update(checked)
    return cfg


def capture(argv, *, data=None):
    result = subprocess.run([str(x) for x in argv], input=data, capture_output=True, text=True, timeout=180)
    if result.returncode:
        raise PocError(f'Command failed ({result.returncode}): {argv[0]}; {result.stderr[:600]}')
    return json.loads(result.stdout) if result.stdout.strip() else {}


def oci(cfg, *args, home=False):
    return capture(['oci', '--profile', cfg['oci_profile'], '--region',
                    cfg['home_region'] if home else cfg['region'], *args])['data']


def target_check(cfg):
    tenancy = oci(cfg, 'iam', 'tenancy', 'get', '--tenancy-id', cfg['tenancy_id'], home=True)
    regions = oci(cfg, 'iam', 'region', 'list', home=True)
    home = next(r['name'] for r in regions if r['key'] == tenancy['home-region-key'])
    if home != cfg['home_region']:
        raise PocError(f'IAM home region is {home}, not {cfg["home_region"]}.')
    current = cfg['compartment_id']
    visited = set()
    while current != cfg['tenancy_id']:
        if current in visited:
            raise PocError('Cannot resolve compartment ancestry.')
        visited.add(current)
        item = oci(cfg, 'iam', 'compartment', 'get', '--compartment-id', current, home=True)
        if item['lifecycle-state'] != 'ACTIVE':
            raise PocError('Target compartment is not ACTIVE.')
        current = item['compartment-id']
    print(f'Target verified: {tenancy["name"]} / {cfg["region"]} / {cfg["compartment_id"][-12:]}', flush=True)


def configure(args):
    # Reuse the reviewed read-only selectors and compatibility checks.
    spec = importlib.util.spec_from_file_location('wizard', ROOT/'scripts/discovery.py')
    wizard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wizard)
    if args.config.exists():
        raise PocError('Configuration already exists. Edit it explicitly or choose another --config path.')
    seed = getattr(args, 'seed_config', None) or ROOT/'config/customer.example.json'
    cfg = read_config(seed, complete=False)
    target_check(cfg)
    api = wizard.Discovery(cfg['oci_profile'], cfg['region'])
    cfg['name'] = wizard.ask('POC name', cfg['name'])
    cfg['state_directory'] = str(ROOT/'deployments'/cfg['name']/'states')
    ads = api.get('iam', 'availability-domain', 'list', '--compartment-id', cfg['tenancy_id'])
    default_ad=next((a for a in ads if a['name']==cfg.get('availability_domain')),None)
    cfg['availability_domain'] = wizard.choose('Availability domain (capacity must be confirmed)', ads, lambda a:a['name'],default=default_ad)['name']
    cfg['gpu_image_id'] = wizard.ask('Imported Ubuntu GPU image OCID in this tenancy',cfg.get('gpu_image_id'))
    wizard.validate_image(api, cfg['gpu_image_id'], cfg['gpu_shape'])
    cpu = wizard.platform_image(api, cfg['compartment_id'], cfg['monitoring_shape'])
    print(f'Monitoring platform image: {cpu["display-name"]} / {cpu["id"]}')
    cfg['monitoring_image_id'] = cpu['id']
    cfg['ssh_private_key_path'] = wizard.ask('Existing SSH private key path', cfg['ssh_private_key_path'])
    cfg['admin_cidr'] = wizard.ask('Public OCI SSH egress address (confirm with your network if needed)', validate=wizard.admin_network)
    cfg['vcn_cidr'] = wizard.ask('New VCN CIDR (verify VPN/peering ranges too)', cfg['vcn_cidr'], wizard.private_network)
    network = ipaddress.ip_network(cfg['vcn_cidr'])
    for vcn in api.get('network', 'vcn', 'list', '--compartment-id', cfg['compartment_id'], '--all'):
        if vcn['lifecycle-state'] != 'TERMINATED' and any(network.overlaps(ipaddress.ip_network(n)) for n in vcn.get('cidr-blocks') or [vcn['cidr-block']]):
            raise PocError('Proposed VCN overlaps another VCN in the target compartment.')
    cfg['streamlit_username'] = wizard.ask('Streamlit HTTPS username', cfg['streamlit_username'])
    args.config.parent.mkdir(parents=True, exist_ok=True)
    # Validate before publishing. No credentials are embedded in this JSON.
    temporary = ROOT/'.local/configure-check.json'
    private_json(temporary, cfg)
    read_config(temporary)
    with args.config.open('x') as out:
        os.chmod(args.config, 0o600)
        json.dump(cfg, out, indent=2); out.write('\n')
    print(f'Created {args.config}. No cloud resources were created.')


def output(cfg, stack):
    values = state(Path(cfg['state_directory'])/stack/'terraform.tfstate').get('outputs', {})
    if not values:
        raise PocError(f'{stack} has no outputs. Deploy that prerequisite first.')
    return {k:v['value'] for k,v in values.items()}


def stage_vars(cfg, stage):
    common = {k:cfg[k] for k in ('oci_profile', 'region', 'compartment_id', 'name')}
    if stage == 'aidp-foundation':
        shared, gpu, monitoring = (output(cfg,s) for s in ('shared','vllm','monitoring'))
        subnet = oci(cfg,'network','subnet','get','--subnet-id',shared['gpu_subnet_id'])
        dhcp = oci(cfg,'network','dhcp-options','get','--dhcp-id',subnet['dhcp-options-id'])
        if not any(o.get('type')=='DomainNameServer' and o.get('server-type')=='VcnLocalPlusInternet' for o in dhcp.get('options',[])):
            raise PocError('The shared subnet must use the OCI Internet and VCN resolver. Review custom DNS requirements before preparing AIDP.')
        net = ipaddress.ip_network(cfg['vcn_cidr'])
        cidr = str(ipaddress.ip_network((int(net.network_address)+36*256,24)))
        existing = oci(cfg,'network','subnet','list','--compartment-id',cfg['compartment_id'],'--vcn-id',shared['vcn_id'],'--all')
        own = state(Path(cfg['state_directory'])/stage/'terraform.tfstate').get('outputs',{}).get('workspace_subnet_id',{}).get('value')
        if any(s['id'] != own and s['lifecycle-state'] != 'TERMINATED' and ipaddress.ip_network(s['cidr-block']).overlaps(ipaddress.ip_network(cidr)) for s in existing):
            raise PocError('AIDP subnet overlaps another subnet.')
        return dict(common, vcn_id=shared['vcn_id'], private_route_table_id=subnet['route-table-id'],
                    dhcp_options_id=subnet['dhcp-options-id'], inference_private_ip=gpu['private_ip'],
                    vcn_dns_ip='169.254.169.254', workspace_cidr=cidr,
                    gpu_https_nsg_id=monitoring['gpu_metrics_nsg_id'])
    foundation = output(cfg,'aidp-foundation')
    if stage == 'aidp-dns':
        vcn = output(cfg, 'shared')['vcn_id']
        resolvers = oci(cfg, 'dns', 'resolver', 'list', '--compartment-id', cfg['compartment_id'], '--scope', 'PRIVATE')
        matches = [r for r in resolvers if r['attached-vcn-id'] == vcn]
        if len(matches) != 1:
            raise PocError('Cannot uniquely identify the POC VCN DNS resolver.')
        resolver = oci(cfg, 'dns', 'resolver', 'get', '--resolver-id', matches[0]['id'], '--scope', 'PRIVATE')
        view = resolver['default-view-id']
        own = state(Path(cfg['state_directory'])/stage/'terraform.tfstate').get('outputs',{}).get('zone_id',{}).get('value')
        zones = oci(cfg, 'dns', 'zone', 'list', '--compartment-id', cfg['compartment_id'], '--scope', 'PRIVATE', '--view-id', view, '--all')
        if any(z['name'].rstrip('.') == 'inference.lab' and z['id'] != own for z in zones):
            raise PocError('The inference.lab private zone already exists outside this stage’s ownership.')
        return dict(common, view_id=view, inference_private_ip=output(cfg, 'vllm')['private_ip'])
    if stage == 'aidp-credential-iam':
        record = json.loads((Path(cfg['state_directory']).parent/'runtime/inference-secret.json').read_text())
        return {k:v for k,v in dict(common, home_region=cfg['home_region'],
                aidp_id=output(cfg, 'aidp')['aidp_id'], inference_secret_id=record['id'],
                kms_key_id=foundation['key_id']).items() if k != 'region'}
    if stage == 'iam':
        return {k:v for k,v in dict(common, tenancy_id=cfg['tenancy_id'], home_region=cfg['home_region'],
                lakehouse_admin_secret_id=foundation['lakehouse_admin_secret_id']).items() if k!='region'}
    if stage == 'lakehouse':
        return dict(common, subnet_id=foundation['lakehouse_subnet_id'], nsg_id=foundation['lakehouse_nsg_id'],
                    admin_secret_id=foundation['lakehouse_admin_secret_id'], db_name=cfg['lakehouse_db_name'],
                    ecpus=cfg['lakehouse_ecpus'],storage_tbs=cfg['lakehouse_storage_tbs'])
    if stage == 'aidp':
        return dict(common, lakehouse_id=output(cfg,'lakehouse')['lakehouse_id'],
                    admin_secret_id=foundation['lakehouse_admin_secret_id'])
    if stage == 'runtime-iam':
        identity_path = Path(cfg['state_directory']).parent/'runtime-identity.json'
        if identity_path.is_file():
            reports = json.loads(identity_path.read_text()).get('reports', [])
            if any(r.get('identity_claims', {}).get('res_tenant') not in (None, cfg['tenancy_id']) for r in reports):
                raise PocError('The observed AI Compute principal belongs to a managed service tenancy. Direct Vault IAM has not been validated for that identity. Keep this stage pending and validate the documented AIDP Credential Store/Vault-reference path; do not copy the managed principal into same-tenancy config.')
        kind, identity = cfg.get('runtime_principal_type'),cfg.get('runtime_principal_id')
        if not kind or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]*',kind) or not identity or not identity.startswith('ocid1.'):
            raise PocError('Runtime principal fields are unset or invalid. Run scripts/aidp_workspace.py bootstrap --wait-minutes 30 to prepare the private workspace, AI Compute and identity-probe agent; see docs/AIDP_WORKSPACE_AUTOMATION.md and docs/AIDP_INTEGRATION.md section 7. Do not substitute a user or AI Compute OCID without verifying the caller identity.')
        record=json.loads((Path(cfg['state_directory']).parent/'runtime/inference-secret.json').read_text())
        return {k:v for k,v in dict(common,home_region=cfg['home_region'],runtime_principal_type=kind,
                runtime_principal_id=identity,inference_secret_id=record['id']).items() if k!='region'}
    raise PocError('Unknown stage.')


def tf_stage(cfg, stage, *, apply=False, destroy=False, yes=False, ack=False):
    if stage == 'aidp' and apply and not destroy and not ack:
        raise PocError('Review docs/AIDP_INTEGRATION.md, then explicitly use --ack-aidp-bootstrap-limit for the unverified private-Lakehouse bootstrap path.')
    states = Path(cfg['state_directory'])
    canonical = states/stage
    canonical.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp=dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%S.%fZ')
    work=states.parent/'runs'/stamp/stage
    work.mkdir(parents=True, mode=0o700)
    variables = json.loads((canonical/'terraform.tfvars.json').read_text()) if destroy else stage_vars(cfg,stage)
    declarations=set()
    for src in (ROOT/'terraform'/stage).iterdir():
        if src.suffix == '.tf' or src.name=='.terraform.lock.hcl':
            shutil.copy2(src,work/src.name)
            shutil.copy2(src,canonical/src.name)
            if src.suffix=='.tf':declarations.update(re.findall(r'resource\s+"([^"]+)"\s+"([^"]+)"',src.read_text()))
    for r in managed(state(canonical/'terraform.tfstate')):
        if r.get('module') or (r['type'],r['name']) not in declarations:
            raise PocError('State contains a resource outside the selected component.')
        for instance in r['instances']:
            attrs=instance['attributes']; compartment=attrs.get('compartment_id')
            allowed={cfg['compartment_id'],cfg['tenancy_id']} if stage=='iam' else {cfg['compartment_id']}
            if compartment and compartment not in allowed:
                raise PocError('State contains resources in another compartment.')
            if attrs.get('freeform_tags',{}).get('Project',cfg['name']) != cfg['name']:
                raise PocError('State contains another project’s resources.')
    backend={'terraform':{'backend':{'local':{'path':str(canonical/'terraform.tfstate')}}}}
    private_json(canonical/'automation_override.tf.json',backend)
    private_json(canonical/'terraform.tfvars.json',variables)
    if destroy:
        backend['resource']={}
        for kind,name in PROTECTED.get(stage,[]):backend['resource'].setdefault(kind,{})[name]={'lifecycle':{'prevent_destroy':False}}
    private_json(work/'automation_override.tf.json',backend)
    private_json(work/'terraform.tfvars.json',variables)
    env={k:v for k,v in os.environ.items() if not k.startswith(('TF_CLI_ARGS','TF_VAR_')) and k not in ('TF_DATA_DIR','TF_WORKSPACE')}
    env.update(TF_IN_AUTOMATION='1',TF_WORKSPACE='default')
    def run(*args, accepted=(0,)):
        command = ['terraform', f'-chdir={work}', *args]
        if args[0] in STREAM_COMMANDS:
            poll = (lambda: aidp_progress(cfg)) if stage == 'aidp' and args[0] == 'apply' and not destroy else None
            result = run_terraform(command, log_path=work/'commands.log', env=env,
                                   label=f'{stage} Terraform {args[0]}', poll=poll)
        else:
            result=subprocess.run(command,capture_output=True,text=True,env=env)
            with (work/'commands.log').open('a') as log:log.write(result.stdout+result.stderr)
        if result.returncode not in accepted:raise PocError(f'Terraform failed: {work}/commands.log')
        return result.stdout
    run('init','-input=false','-no-color');run('validate','-no-color')
    plan=work/'operation.tfplan'
    argv=['plan','-input=false','-no-color','-lock-timeout=60s',f'-out={plan}','-detailed-exitcode']
    if destroy:argv.append('-destroy')
    run(*argv,accepted=(0,2))
    document=json.loads(run('show','-json',str(plan)))
    private_json(work/'plan.json',document)
    (work/'plan.txt').write_text(run('show','-no-color',str(plan)))
    changes=guard_plan(document,'destroy' if destroy else 'deploy')
    print(f'{stage}: {len(changes)} changes. Review {work}/plan.txt',flush=True)
    if apply and changes:
        phrase=f'{"DELETE" if destroy else "APPLY"} {cfg["name"]} {stage}'
        if not yes and input(f'Type {phrase}: ') != phrase:raise PocError('Cancelled.')
        run('apply','-input=false','-no-color','-lock-timeout=60s',str(plan))
    return work


def inference(cfg, *, apply, destroy=False, args=None):
    path=Path(cfg['state_directory']).parent/'inference.generated.json'
    private_json(path,{k:v for k,v in cfg.items() if k not in EXTRA})
    argv=[sys.executable,str(ROOT/'scripts/inference_stack.py'),'destroy' if destroy else 'deploy','--config',str(path)]
    if apply:argv.append('--apply')
    if args.yes:argv.append('--yes')
    if destroy and apply:argv+=['--delete-data','--confirm-name',cfg['name']]
    if not destroy and args.infra_only:argv.append('--infra-only')
    subprocess.run(argv,check=True)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['configure','preflight','deploy','plan','apply','status','runtime-bundle','publish-key','destroy'])
    parser.add_argument('--config',type=Path,default=ROOT/'poc-config.json')
    parser.add_argument('--seed-config',type=Path,help='Optional setup seed; normally supplied by the customer setup wizard')
    parser.add_argument('--stage',choices=AIDP_STAGES)
    parser.add_argument('--through',choices=['inference','lakehouse','aidp'],default='lakehouse')
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--apply',action='store_true',help='Required on deploy/destroy to make cloud changes')
    mode.add_argument('--plan',action='store_true',help='Explicit destroy preview; no cloud resources are deleted')
    parser.add_argument('--yes',action='store_true')
    parser.add_argument('--infra-only',action='store_true')
    parser.add_argument('--delete-data',action='store_true')
    parser.add_argument('--confirm-name')
    parser.add_argument('--confirm-aidp-runtime-removed',action='store_true')
    parser.add_argument('--ack-aidp-bootstrap-limit',action='store_true')
    args=parser.parse_args(argv)
    if args.plan and args.action!='destroy':
        parser.error('--plan is only supported for destroy; use the plan action for a component plan')
    os.umask(0o077)
    if args.action=='configure':return configure(args)
    if args.action in ('plan','apply') and not args.stage:parser.error('--stage is required')
    if args.action=='destroy' and args.apply and (not args.delete_data or not args.confirm_name):
        parser.error('Destruction requires --delete-data and --confirm-name')
    if args.action=='deploy' and args.apply and args.through=='aidp' and not args.ack_aidp_bootstrap_limit:
        parser.error('--through aidp requires --ack-aidp-bootstrap-limit before any deployment begins')
    cfg=read_config(args.config)
    states=Path(cfg['state_directory']);states.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    with (states.parent/'operation.lock').open('a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise PocError('Another operation is running for this POC.') from None
        target_check(cfg)
        if args.action=='preflight':
            spec=importlib.util.spec_from_file_location('wizard',ROOT/'scripts/discovery.py')
            wizard=importlib.util.module_from_spec(spec);spec.loader.exec_module(wizard)
            api=wizard.Discovery(cfg['oci_profile'],cfg['region'])
            wizard.validate_image(api,cfg['gpu_image_id'],cfg['gpu_shape'])
            wizard.validate_image(api,cfg['monitoring_image_id'],cfg['monitoring_shape'],monitoring=True)
            shapes=oci(cfg,'compute','shape','list','--compartment-id',cfg['compartment_id'],'--availability-domain',cfg['availability_domain'],'--all')
            if not any(s['shape']==cfg['gpu_shape'] for s in shapes):raise PocError('GPU shape is not offered in this AD.')
            oci(cfg,'ai-data-platform','ai-data-platform-collection','list-ai-data-platforms','--compartment-id',cfg['compartment_id'],'--all')
            print('Read-only preflight passed. Shape offering does not reserve capacity or prove model/image compatibility.')
        elif args.action in ('plan','apply'):
            tf_stage(cfg,args.stage,apply=args.action=='apply',yes=args.yes,ack=args.ack_aidp_bootstrap_limit)
        elif args.action=='deploy':
            inference(cfg,apply=args.apply,args=args)
            if not args.apply:
                print('Preview only; dependent AIDP plans require applied inference outputs.');return
            if args.through!='inference':
                for stage in ('aidp-foundation','iam','lakehouse'):
                    tf_stage(cfg,stage,apply=True,yes=args.yes)
            if not args.infra_only:
                from configure_public_apps import install
                install(cfg)
            if args.through=='aidp':tf_stage(cfg,'aidp',apply=True,yes=args.yes,ack=args.ack_aidp_bootstrap_limit)
            print('Continue with docs/AIDP_INTEGRATION.md for private workspace, AI Compute, runtime identity and agent publishing.')
        elif args.action=='status':
            for stage in ('shared','monitoring','vllm',*AIDP_STAGES):
                print(stage,':',len(managed(state(states/stage/'terraform.tfstate'))),'managed resource records')
            for stage in ('monitoring','aidp'):
                print(stage,':',state(states/stage/'terraform.tfstate').get('outputs',{}))
        elif args.action in ('runtime-bundle','publish-key'):
            from runtime_bundle import prepare
            prepare(cfg,publish=args.action=='publish-key')
        elif args.action=='destroy':
            if args.apply and args.confirm_name!=cfg['name']:raise PocError('Confirmation name does not match.')
            if args.apply and not args.confirm_aidp_runtime_removed:
                raise PocError('Remove AIDP agents/AI Compute/workspace dependencies first; then use --confirm-aidp-runtime-removed.')
            runtime_record=states.parent/'runtime/inference-secret.json'
            if runtime_record.exists():
                record=json.loads(runtime_record.read_text())
                secret=oci(cfg,'vault','secret','get','--secret-id',record['id'])
                if secret['lifecycle-state'] not in ('PENDING_DELETION','DELETED'):
                    if args.apply:
                        raise PocError('Schedule recorded inference secret deletion first. See docs/CLEANUP.md. No deletion started.')
                    print('Recorded inference secret is active. Preview continues; schedule its deletion before destructive apply.')
            for stage in reversed(AIDP_STAGES):
                if managed(state(states/stage/'terraform.tfstate')):
                    tf_stage(cfg,stage,apply=args.apply,destroy=True,yes=args.yes)
            inference(cfg,apply=args.apply,destroy=True,args=args)


if __name__=='__main__':
    try:main()
    except (PocError,OSError,ValueError,KeyError,subprocess.CalledProcessError) as error:
        raise SystemExit(str(error)) from None

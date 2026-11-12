#!/usr/bin/env python3
"""Run curated health, inference and load checks through verified Bastion SSH."""
import argparse
import os
from pathlib import Path
import shlex

from configure_public_apps import host_context
from hosts import Access
from inference_stack import ROOT, PocError
from poc import read_config, target_check


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['gpu-health','monitoring-health','chat','tool-test','load','repair-bootstrap'])
    parser.add_argument('--config',type=Path,default=ROOT/'poc-config.json')
    parser.add_argument('--apply',action='store_true',help='Apply only an explicit GPU bootstrap repair')
    parser.add_argument('--max-requests',type=int,default=100)
    args=parser.parse_args()
    if args.apply and args.action!='repair-bootstrap':parser.error('--apply is only valid for repair-bootstrap')
    if not 1<=args.max_requests<=1000:parser.error('Use 1–1000 requests.')
    os.umask(0o077)
    cfg=read_config(args.config);target_check(cfg)
    if args.action=='repair-bootstrap':
        from gpu_bootstrap import launch
        launch(cfg,apply=args.apply)
        return
    lab,context,known=host_context(cfg)
    role='monitoring' if args.action in ('load','monitoring-health') else 'vllm'
    with Access(lab,context,role,known) as host:
        if args.action=='gpu-health':
            host.ssh('nvidia-smi; sudo systemctl is-active vllm nginx; mountpoint /mnt/models')
        elif args.action=='monitoring-health':
            host.ssh('sudo python3 /opt/monitoring/validate-monitoring.py')
        elif args.action=='load':
            command=('set -a; source /etc/monitoring/inference.env; set +a; '
                     'exec python3 /opt/monitoring/inference-load.py '
                     '--output-dir /var/lib/monitoring/load-tests/$(date -u +%Y%m%dT%H%M%SZ) '
                     '--max-requests '+str(args.max_requests))
            host.ssh('sudo bash -c '+shlex.quote(command))
        else:
            host.ssh('sudo install -d -m 700 /opt/poc-validation')
            host.ssh('sudo sh -c "umask 077; cat > /opt/poc-validation/private_client.py"',
                     data=(ROOT/'integration/aidp/private_client.py').read_bytes())
            script='''import json,pathlib,sys
sys.path.insert(0,'/opt/poc-validation')
from private_client import PrivateClient
key=pathlib.Path('/etc/vllm/server.env').read_text().strip().split('=',1)[1]
config={'base_url':BASE_URL,'model':'qwen3-8b','ca_file':'/etc/nginx/tls/server.crt','max_tokens':512}
result=PrivateClient(config,key=key).run(PROMPT,tools=TOOLS,require_tool=TOOLS)
print(json.dumps({'status':'PASS','tool_observed':result['tool_observed'],'usage':result['usage']}))
'''
            tools=args.action=='tool-test'
            prompt='Use get_poc_inventory and report the synthetic total GPU VRAM.' if tools else 'Reply: Private inference connection verified.'
            script='BASE_URL='+repr('https://'+context['vllm']['private_ip']['value']+'/v1')+'\nPROMPT='+repr(prompt)+'\nTOOLS='+repr(tools)+'\n'+script
            host.ssh('sudo python3 -',data=script.encode())


if __name__=='__main__':
    try:main()
    except (PocError,OSError,ValueError) as error:raise SystemExit(str(error)) from None

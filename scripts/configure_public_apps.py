"""Install Streamlit and authenticated public HTTPS using verified Bastion SSH."""
import argparse
import importlib.util
import json
from pathlib import Path
import shlex
import sys
import time
import urllib.error
import urllib.request

from inference_stack import InferenceStack, PocError, STACKS, ROOT, private_json
from hosts import Access


def host_context(cfg):
    lab=InferenceStack(cfg,argparse.Namespace())
    context={s:lab.output(s) for s in STACKS}
    if any(not context[s] for s in STACKS):raise PocError('Deploy the three inference components first.')
    if cfg.get('grafana_hostname'):context['monitoring']['grafana_url']['value']='https://'+cfg['grafana_hostname']
    private_json(lab.run/'outputs.json',context)
    known=lab.meta/'known_hosts'
    known.touch(mode=0o600,exist_ok=True);known.chmod(0o600)
    return lab,context,known


def install(cfg):
    lab,context,known=host_context(cfg)
    spec=importlib.util.spec_from_file_location('streamlit_launcher',ROOT/'apps/streamlit/scripts/launch.py')
    launcher=importlib.util.module_from_spec(spec);spec.loader.exec_module(launcher)
    grafana=context['monitoring']['grafana_url']['value']
    url='https://streamlit.'+grafana.removeprefix('https://')
    with Access(lab,context,'monitoring',known) as monitor:
        ssh=['ssh','-F','/dev/null','-i',cfg['ssh_private_key_path'],'-o','IdentitiesOnly=yes',
             '-o','StrictHostKeyChecking=yes','-o',f'UserKnownHostsFile={known}',
             '-o','HostKeyAlias='+context['monitoring']['instance_id']['value'],
             '-p',str(monitor.port),'ubuntu@127.0.0.1']
        launcher.install_monitoring(ssh,context)
        command='sudo python3 - '+shlex.quote(cfg['streamlit_username'])+' '+shlex.quote(url)
        monitor.ssh(command,data=(ROOT/'runtime/monitoring/configure-streamlit-https.py').read_bytes(),timeout=300)
        # This file deliberately contains login credentials, never command logs or stdout.
        login=monitor.ssh('sudo cat /etc/monitoring/streamlit-login.json',capture=True)
        info=json.loads(login)
        private_json(lab.meta/'streamlit-login.json',info)
        del login,info
    deadline=time.monotonic()+300
    while True:
        try:
            urllib.request.urlopen(url,timeout=15)
            raise PocError('Unauthenticated Streamlit returned success; inspect the proxy immediately.')
        except urllib.error.HTTPError as error:
            if error.code==401:break
            if time.monotonic()>deadline:raise PocError(f'Streamlit HTTPS returned {error.code}.') from None
        except urllib.error.URLError:
            if time.monotonic()>deadline:raise PocError('Streamlit DNS/TLS is not ready; inspect Caddy.') from None
        time.sleep(5)
    print(f'Streamlit HTTPS authentication verified: {url}\nLogin file (0600): {lab.meta}/streamlit-login.json',flush=True)


if __name__=='__main__':
    from poc import read_config
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--config',type=Path,default=ROOT/'poc-config.json')
    install(read_config(p.parse_args().config))

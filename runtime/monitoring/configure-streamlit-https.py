#!/usr/bin/env python3
"""Run as root on monitoring. Keep passwords out of process arguments and logs."""
import base64
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.request
from urllib.parse import urlsplit


def snippet(username, hashed):
    if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{2,31}',username):raise ValueError('Invalid username')
    if not re.fullmatch(r'\$2[aby]\$[0-9]{2}\$[./A-Za-z0-9]{53}',hashed):raise ValueError('Invalid bcrypt hash')
    return ('header {\n  Strict-Transport-Security "max-age=31536000"\n'
            '  X-Content-Type-Options "nosniff"\n  -Server\n}\n'
            f'basic_auth {{\n  {username} {hashed}\n}}\n'
            'reverse_proxy 127.0.0.1:8501 {\n  header_up -Authorization\n}\n')


def main():
    if os.geteuid()!=0:raise SystemExit('Run as root')
    username,url=sys.argv[1:]
    parts=urlsplit(url)
    if parts.scheme!='https' or parts.path or not parts.hostname or not re.fullmatch(r'streamlit\.[a-z0-9.-]+',parts.hostname):
        raise SystemExit('Expected the dedicated Streamlit HTTPS hostname')
    login=Path('/etc/monitoring/streamlit-login.json')
    if login.exists():
        info=json.loads(login.read_text());password=info['password']
    else:password=secrets.token_urlsafe(32)
    info={'url':url,'username':username,'password':password}
    fd=os.open(login,os.O_CREAT|os.O_WRONLY|os.O_TRUNC,0o600)
    os.fchmod(fd,0o600)
    with os.fdopen(fd,'w') as f:json.dump(info,f);f.write('\n')
    result=subprocess.run(['docker','exec','-i','monitoring-https','caddy','hash-password'],
                          input=password+'\n',capture_output=True,text=True,check=True)
    hashed=result.stdout.strip()
    body=snippet(username,hashed)
    # This is a bind-mounted file: retain its inode so the container sees updates.
    auth=Path('/etc/monitoring/streamlit-auth.caddy')
    auth.write_text(body);auth.chmod(0o644)
    subprocess.run(['docker','exec','monitoring-https','caddy','validate','--config','/etc/caddy/Caddyfile','--adapter','caddyfile'],check=True,capture_output=True)
    subprocess.run(['docker','exec','monitoring-https','caddy','reload','--config','/etc/caddy/Caddyfile','--adapter','caddyfile'],check=True,capture_output=True)
    token=base64.b64encode((username+':'+password).encode()).decode()
    for _ in range(60):
        try:
            request=urllib.request.Request(url,headers={'Authorization':'Basic '+token})
            with urllib.request.urlopen(request,timeout=10) as response:
                if response.status==200:
                    print('Authenticated public Streamlit HTTPS passed.');return
        except OSError:pass
        time.sleep(5)
    raise SystemExit('Authenticated Streamlit HTTPS did not become ready; inspect proxy/app logs')


if __name__=='__main__':main()

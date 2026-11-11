#!/usr/bin/env python3
"""Resolve reviewed container tags to immutable digests on the deployment host."""
import json
from pathlib import Path
import re
import subprocess
import sys

path = Path(sys.argv[1])
content = path.read_text()
locked = {}
for reference in sorted(set(re.findall(r'^    image: (\S+)$', content, re.M))):
    subprocess.run(['docker', 'pull', reference], check=True,
                   stdout=subprocess.DEVNULL, timeout=600)
    data = json.loads(subprocess.check_output(['docker', 'image', 'inspect', reference]))[0]
    digest = data['RepoDigests'][0]
    locked[reference] = digest
    content = content.replace('image: ' + reference + '\n', 'image: ' + digest + '\n')
    print(f'Pinned {reference} -> {digest}', flush=True)
path.write_text(content)
path.with_suffix('.images.json').write_text(json.dumps(locked, indent=2) + '\n')

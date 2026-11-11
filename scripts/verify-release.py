#!/usr/bin/env python3
"""Verify distributed source integrity without accessing OCI."""
import hashlib
import json
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
manifest=json.loads((ROOT/'MANIFEST.json').read_text())
for name,wanted in manifest['sha256'].items():
    path=(ROOT/name).resolve()
    if ROOT not in path.parents or not path.is_file():
        raise SystemExit('Missing or invalid source path: '+name)
    if hashlib.sha256(path.read_bytes()).hexdigest()!=wanted:
        raise SystemExit('Source differs from release: '+name)
print('Release integrity verified: '+str(len(manifest['sha256']))+' source files. No OCI requests.')

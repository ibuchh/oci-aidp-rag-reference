#!/usr/bin/env python3
"""Upload only the bundled synthetic TXT files to the recorded managed volume."""
import argparse
import hashlib
import json
import os
import shlex
import shutil
import sys
from pathlib import Path
from urllib.parse import quote

from rag_poc import HERE, ROOT, RagPoc, PocError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=ROOT/'poc-config.json')
    parser.add_argument('--documents-dir',type=Path,default=HERE/'documents',
                        help='Folder of TXT, PDF or DOCX documents; defaults to synthetic samples.')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args()
    if not args.apply:parser.error('Uploads require --apply.')
    poc = RagPoc(args.config)
    volume = poc.saved['resources']['volume']
    if volume['name'] != 'synthetic_documents':
        raise PocError('Unexpected owned document volume.')
    try:
        import oci
    except ModuleNotFoundError:
        cli=shutil.which('oci')
        if not cli:raise PocError('OCI CLI is required.')
        interpreter=shlex.split(Path(cli).open().readline().removeprefix('#!').strip())
        if len(interpreter)!=1 or not Path(interpreter[0]).is_absolute():
            raise PocError('Run this script with the Python interpreter belonging to OCI CLI.')
        os.execv(interpreter[0],[interpreter[0],str(Path(__file__).resolve()),*sys.argv[1:]])
    from oci.base_client import BaseClient
    cfg = oci.config.from_file(profile_name=poc.cfg['oci_profile'])
    cfg['region'] = poc.cfg['region']
    signer = oci.signer.Signer(tenancy=cfg['tenancy'], user=cfg['user'],
        fingerprint=cfg['fingerprint'], private_key_file_location=cfg['key_file'],
        pass_phrase=cfg.get('pass_phrase'))
    client = BaseClient('aidp', cfg, signer, {},
        service_endpoint=f'https://aidp.{cfg["region"]}.oci.oraclecloud.com', timeout=(10,60))
    base = '/20260430/aiDataPlatforms/' + quote(poc.api.aidp_id,safe='') + '/volumes/' + quote(volume['key'],safe='')
    directory=args.documents_dir.expanduser().resolve()
    files=sorted(p for p in directory.iterdir() if p.is_file() and p.suffix.lower() in {'.txt','.pdf','.docx'})
    if not files:raise PocError('No TXT, PDF or DOCX documents in the specified directory.')
    for path in files:
        if path.is_symlink():raise PocError('Document symlinks are not accepted.')
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if directory==(HERE/'documents').resolve() and b'SYNTHETIC POC DOCUMENT' not in data:
            raise PocError('Document differs from the recorded synthetic source: '+path.name)
        if poc.saved.get('uploaded_documents',{}).get(path.name)==digest:
            print('Already recorded uploaded:',path.name);continue
        response = client.call_api(base+'/actions/uploadFile', 'POST',
            header_params={'accept':'application/json','content-type':'application/octet-stream','path':'/'+path.name},
            query_params={'isOverwrite':path.name in poc.saved.get('uploaded_documents',{})}, body=data, response_type='stream')
        response.data.close()
        poc.saved.setdefault('documents',{})[path.name]=digest
        poc.saved.setdefault('uploaded_documents',{})[path.name]=digest
        poc.save()
        print('Uploaded synthetic document:',path.name,'HTTP',response.status)
    data=poc.api.get(volume['resource_path']+'/files?path=%2F')
    safe=[{k:v for k,v in item.items() if k in ['name','displayName','path','type','size','sizeInBytes']} for item in data.get('items',[])]
    print(json.dumps({'files':safe}))


if __name__=='__main__':main()

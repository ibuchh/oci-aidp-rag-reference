#!/usr/bin/env python3
"""Internal helper for AIDP's binary workspace-object API; prints no file content."""
import argparse
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--profile', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--uri', required=True)
    parser.add_argument('--headers', required=True, type=Path)
    parser.add_argument('--body', required=True, type=Path)
    args = parser.parse_args()
    url = urlsplit(args.uri)
    if (url.scheme != 'https' or url.netloc != f'aidp.{args.region}.oci.oraclecloud.com' or
            not url.path.startswith('/20260430/aiDataPlatforms/') or not url.path.endswith('/objects') or
            url.query or url.fragment):
        raise ValueError('Unexpected binary-upload target')
    import oci
    from oci.base_client import BaseClient
    cfg = oci.config.from_file(profile_name=args.profile)
    cfg['region'] = args.region
    signer = oci.signer.Signer(tenancy=cfg['tenancy'], user=cfg['user'],
                             fingerprint=cfg['fingerprint'], private_key_file_location=cfg['key_file'],
                             pass_phrase=cfg.get('pass_phrase'))
    client = BaseClient('aidp', cfg, signer, {}, service_endpoint=f'{url.scheme}://{url.netloc}',
                        timeout=(10, 60))
    headers = json.loads(args.headers.read_text())
    headers.update({'accept': 'application/x-yaml', 'content-type': 'application/octet-stream'})
    try:
        response = client.call_api(url.path, 'POST', header_params=headers,
                                   body=args.body.read_bytes(), response_type='stream')
        response.data.close()
        print(json.dumps({'status': str(response.status), 'headers': dict(response.headers), 'data': {}}))
    except oci.exceptions.ServiceError as error:
        print(json.dumps({'status': str(error.status), 'data': {'code': error.code},
                          'headers': {'opc-request-id': error.request_id}}))


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'error_type': type(error).__name__}), file=sys.stderr)
        sys.exit(1)

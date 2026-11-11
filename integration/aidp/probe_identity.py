"""Run in the intended AIDP agent context. Emit selected identity metadata only."""
import asyncio
import base64
import json
import re
import ipaddress
import os
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request

EMBEDDED_NETWORK = None
SYNTHETIC_CREDENTIAL_NAME = None
INFERENCE_CREDENTIAL_NAME = None
INFERENCE_SECRET_ID = None


def inspect_vault_credential():
    if not INFERENCE_CREDENTIAL_NAME:
        return {'status': 'NOT_CONFIGURED'}
    try:
        import aidputils
        value = aidputils.secrets.get(name=INFERENCE_CREDENTIAL_NAME)
        fields = []
        if isinstance(value, dict):
            fields = [k for k in value if isinstance(k, str) and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,31}', k)]
            if INFERENCE_SECRET_ID and value.get('secretId') != INFERENCE_SECRET_ID:
                return {'status': 'SECRET_ID_MISMATCH'}
            if 'secretContentBase64' in value:
                value = base64.b64decode(value['secretContentBase64'], validate=True).decode()
        valid = isinstance(value, str) and 0 < len(value.strip()) <= 8192 and not any(c.isspace() for c in value.strip())
        return {'status': 'PASS' if valid else 'UNEXPECTED_FORMAT', 'value_type': type(value).__name__,
                'field_names': fields}
    except Exception as error:
        report = {'status': 'FAIL', 'error_type': type(error).__name__}
        if isinstance(getattr(error, 'status', None), int):
            report['http_status'] = error.status
        for name in ('code', 'operation_name', 'target_service'):
            value = getattr(error, name, None)
            if isinstance(value, str) and re.fullmatch(r'[A-Za-z0-9_-]{1,100}', value):
                report[name] = value
        return report


def inspect_credential_probe():
    if not SYNTHETIC_CREDENTIAL_NAME:
        return {'status': 'NOT_CONFIGURED'}
    try:
        import aidputils
        value = aidputils.secrets.get(name=SYNTHETIC_CREDENTIAL_NAME, key='value')
        return {'status': 'PASS' if value == 'synthetic-poc-probe' else 'UNEXPECTED_VALUE',
                'value_type': type(value).__name__}
    except Exception as error:
        return {'status': 'FAIL', 'error_type': type(error).__name__}


def inspect_transport(config_path=None, use_runtime_proxy=False):
    """Verify private TLS and missing-auth rejection without sending a key."""
    path = Path(config_path) if config_path else Path(__file__).with_name('probe_network.json')
    if not path.is_file() and not EMBEDDED_NETWORK:
        return {'status': 'NOT_CONFIGURED'}
    try:
        config = EMBEDDED_NETWORK if EMBEDDED_NETWORK is not None else json.loads(path.read_text())
        url = urllib.parse.urlsplit(config['base_url'])
        networks = [ipaddress.ip_network(n) for n in ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')]
        try:
            address = ipaddress.ip_address(url.hostname)
            approved = any(address in n for n in networks)
        except ValueError:
            approved = (url.hostname == config.get('private_dns_hostname') and
                        bool(re.fullmatch(r'[a-z0-9][a-z0-9.-]+', url.hostname or '')))
        if (url.scheme != 'https' or url.username or url.password or url.query or url.fragment or
                url.path.rstrip('/') != '/v1' or not approved):
            raise ValueError('Invalid private endpoint')
        if config.get('ca_pem'):
            ca_text = config['ca_pem']
            context_args = {'cadata': ca_text}
        else:
            ca = path.resolve().parent/config['ca_file']
            if ca.resolve().parent != path.resolve().parent:
                raise ValueError('Certificate must be adjacent to configuration')
            ca_text = ca.read_text()
            context_args = {'cafile': str(ca)}
        if 'PRIVATE KEY' in ca_text:
            raise ValueError('Certificate file contains key material')
        context = ssl.create_default_context(**context_args)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError('Redirect refused')
        proxy = urllib.request.ProxyHandler() if use_runtime_proxy else urllib.request.ProxyHandler({})
        opener = urllib.request.build_opener(proxy, NoRedirect(),
                                             urllib.request.HTTPSHandler(context=context))
        request = urllib.request.Request(config['base_url'].rstrip('/')+'/models', method='GET')
        try:
            with opener.open(request, timeout=20) as response:
                code = response.status
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
        return {'status': 'PASS' if code == 401 else 'UNEXPECTED_HTTP_STATUS',
                'tls_verified': True, 'http_status': code, 'credentials_sent': False}
    except Exception as error:
        report = {'status': 'FAIL', 'error_type': type(error).__name__, 'credentials_sent': False}
        reason = getattr(error, 'reason', None)
        if isinstance(reason, Exception):
            report['reason_type'] = type(reason).__name__
            if isinstance(getattr(reason, 'errno', None), int):
                report['reason_errno'] = reason.errno
            if isinstance(getattr(reason, 'verify_code', None), int):
                report['certificate_verify_code'] = reason.verify_code
        return report


def inspect_credential_api():
    """Inspect the public utility's interface without requesting any credential."""
    try:
        import aidputils
        import inspect
        getter = aidputils.secrets.get
        return {'available': True, 'parameters': list(inspect.signature(getter).parameters),
                'documentation': (inspect.getdoc(getter) or '')[:3000]}
    except Exception as error:
        return {'available': False, 'error_type': type(error).__name__}


def inspect_runtime(signer_factory=None):
    if signer_factory is None:
        try:
            import oci
        except ImportError:
            return {"status": "OCI_SDK_UNAVAILABLE"}
        signer_factory = oci.auth.signers.get_resource_principals_signer
    try:
        signer = signer_factory()
    except Exception as error:
        # SDK exceptions may include environment values. Never echo the message.
        return {"status": "RESOURCE_PRINCIPAL_UNAVAILABLE", "error_type": type(error).__name__}
    report = {"status": "SIGNER_AVAILABLE", "signer_class": type(signer).__name__}
    get_claim = getattr(signer, "get_claim", None)
    if not callable(get_claim):
        report["status"] = "CLAIM_READER_UNAVAILABLE"
        return report
    claims = {}
    # Do not enumerate the JWT, signer attributes or environment variables.
    for name in ("sub", "res_tenant", "res_compartment", "res_type", "ptype"):
        try:
            value = get_claim(name)
        except Exception:
            continue
        if not isinstance(value, str):
            continue
        if name in ("sub", "res_tenant", "res_compartment"):
            if not re.fullmatch(r"ocid1\.[A-Za-z0-9_.-]+", value) or len(value) > 512:
                continue
        elif not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,63}", value):
            continue
        claims[name] = value
    report["identity_claims"] = claims
    report["policy_values_verified"] = False
    return report


class AgentBasic:
    """Temporary entry file for the same agent, compute and intended Run As identity."""
    def setup(self):
        pass

    async def invoke(self, user_query: str, **kwargs):
        report = await asyncio.to_thread(inspect_runtime)
        report['credential_api'] = await asyncio.to_thread(inspect_credential_api)
        report['credential_probe'] = await asyncio.to_thread(inspect_credential_probe)
        report['vault_credential_probe'] = await asyncio.to_thread(inspect_vault_credential)
        transport = await asyncio.to_thread(inspect_transport)
        # Inspect fixed proxy-variable presence only; never emit their values.
        proxy_names = [name for name in ('HTTPS_PROXY', 'https_proxy', 'ALL_PROXY', 'all_proxy')
                       if os.environ.get(name)]
        report['runtime_proxy_variables_present'] = proxy_names
        if transport.get('status') == 'FAIL' and proxy_names:
            report['private_transport_direct'] = transport
            transport = await asyncio.to_thread(inspect_transport, use_runtime_proxy=True)
            transport['proxy_mode'] = 'runtime'
        if transport['status'] != 'NOT_CONFIGURED':
            report['private_transport'] = transport
        return {"messages": [{"role": "assistant", "content": json.dumps(report)}]}


if __name__ == "__main__":
    report = inspect_runtime()
    transport = inspect_transport()
    if transport['status'] != 'NOT_CONFIGURED':
        report['private_transport'] = transport
    print(json.dumps(report, indent=2))

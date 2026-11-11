"""Private vLLM client. Uses verified TLS and never emits credentials or prompts."""

import argparse
import base64
import ipaddress
import json
import os
import re
from pathlib import Path
import ssl
import urllib.error
import urllib.parse
import urllib.request


TOOL = {
    "type": "function",
    "function": {
        "name": "get_poc_inventory",
        "description": "Return the synthetic POC GPU inventory, not live telemetry.",
        "parameters": {
            "type": "object", "properties": {}, "additionalProperties": False,
        },
    },
}


def tool_result(call):
    """Only this named, argument-free synthetic function can execute."""
    function = call.get("function", {})
    if function.get("name") != "get_poc_inventory":
        raise ValueError("Model requested an unsupported tool")
    if json.loads(function.get("arguments", "{}")) != {}:
        raise ValueError("Synthetic inventory tool takes no arguments")
    return {"synthetic": True, "gpus": 2, "gpu_model": "NVIDIA A10", "vram_gb_each": 24}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ValueError("Inference redirects are disabled")


class PrivateClient:
    def __init__(self, config, key=None):
        self.config = config
        url = urllib.parse.urlsplit(config["base_url"])
        if url.scheme != "https" or not url.hostname or url.username or url.password:
            raise ValueError("base_url must be HTTPS without embedded credentials")
        if url.query or url.fragment or url.path.rstrip("/") != "/v1":
            raise ValueError("base_url must end in /v1 without query or fragment")
        # Direct clients use RFC1918. AIDP can use the explicit private DNS
        # alias; TLS verifies the hostname against the trusted certificate.
        try:
            address = ipaddress.ip_address(url.hostname)
        except ValueError:
            address = None
        private_ranges = [ipaddress.ip_network(n) for n in
                          ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")]
        if address is not None:
            approved = any(address in n for n in private_ranges)
        else:
            approved = (url.hostname == config.get('private_dns_hostname') and '.' in url.hostname and
                        all(re.fullmatch(r'[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', label)
                            for label in url.hostname.split('.')))
        if not approved:
            raise ValueError("Endpoint must be RFC1918 or the explicitly configured private DNS hostname")
        self.endpoint = config["base_url"].rstrip("/") + "/chat/completions"
        self.key = key if key is not None else self._read_key(config)
        if not self.key or len(self.key) > 8192 or any(c.isspace() for c in self.key):
            raise ValueError("API key must be a nonempty single token")
        ca_file = config.get("ca_file")
        if ca_file and not Path(ca_file).is_file():
            raise ValueError("Configured CA certificate file does not exist")
        ca_pem = config.get('ca_pem')
        if ca_file and ca_pem:
            raise ValueError('Configure only one CA source')
        if ca_pem and 'PRIVATE KEY' in ca_pem:
            raise ValueError('Trust configuration must contain public certificates only')
        context = ssl.create_default_context(cadata=ca_pem) if ca_pem else ssl.create_default_context(cafile=ca_file or None)
        self.opener = urllib.request.build_opener(
            urllib.request.ProxyHandler({}), NoRedirect(),
            urllib.request.HTTPSHandler(context=context),
        )
        self.timeout = float(config.get("timeout_seconds", 120))
        self.max_tokens = int(config.get("max_tokens", 512))
        if not 1 <= self.timeout <= 600 or not 1 <= self.max_tokens <= 2048:
            raise ValueError("Use timeout 1–600 seconds and max_tokens 1–2048")

    @staticmethod
    def _read_key(config):
        file = config.get("api_key_file")
        secret = config.get("api_key_secret_id")
        credential = config.get('api_key_credential_name')
        env = os.environ.get("VLLM_API_KEY")
        if sum(bool(x) for x in (file, secret, env, credential)) != 1:
            raise ValueError("Configure exactly one API key source: file, Vault, Credential Store, or environment")
        if credential:
            try:
                import aidputils
                reference = aidputils.secrets.get(name=credential)
                if not isinstance(reference, dict) or not isinstance(reference.get('secretContentBase64'), str):
                    raise ValueError('Unexpected Vault reference payload')
                expected = config.get('api_key_credential_secret_id')
                if expected and reference.get('secretId') != expected:
                    raise ValueError('Vault reference points to another secret')
                return base64.b64decode(reference['secretContentBase64'], validate=True).decode().strip()
            except Exception:
                raise RuntimeError('AIDP Credential Store lookup failed; details withheld') from None
        if file:
            path = Path(file)
            if path.stat().st_mode & 0o077:
                raise ValueError("API key file must have mode 0600 or stricter")
            return path.read_text().strip()
        if env:
            return env.strip()
        # No local profile fallback: AIDP execution must prove resource-principal support.
        import oci
        signer = oci.auth.signers.get_resource_principals_signer()
        client = oci.secrets.SecretsClient({"region": config["region"]}, signer=signer)
        bundle = client.get_secret_bundle(secret, stage="CURRENT").data
        return base64.b64decode(bundle.secret_bundle_content.content, validate=True).decode().strip()

    def chat(self, messages, tools=False):
        payload = {
            "model": self.config["model"], "messages": messages,
            "temperature": 0, "max_tokens": self.max_tokens, "stream": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        if tools:
            payload.update(tools=[TOOL], tool_choice="auto", parallel_tool_calls=False)
        request = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode(), method="POST",
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self.key},
        )
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as error:
            raise RuntimeError(f"Inference returned HTTP {error.code}; response body withheld") from None
        except (urllib.error.URLError, TimeoutError, ssl.SSLError):
            raise RuntimeError("Private inference connection failed; check routing, firewall and TLS") from None
        if len(raw) > 2 * 1024 * 1024:
            raise ValueError("Inference response exceeds the POC response limit")
        result = json.loads(raw)
        message = result["choices"][0]["message"]
        if message.get("role") != "assistant":
            raise ValueError("Inference did not return an assistant message")
        return message, result.get("usage", {})

    def run(self, prompt, tools=False, require_tool=False):
        messages = [{"role": "system", "content": "Use synthetic POC data only. Use the inventory tool when asked."},
                    {"role": "user", "content": prompt}]
        observed = False
        usage = {}
        for _ in range(4):
            message, usage = self.chat(messages, tools=tools)
            messages.append(message)
            calls = message.get("tool_calls", [])
            if not calls:
                if not message.get("content"):
                    raise ValueError("Inference returned an empty final answer")
                if require_tool and not observed:
                    raise ValueError("No tool call was observed; tool integration has not passed")
                return {"messages": messages, "tool_observed": observed, "usage": usage}
            if not tools or len(calls) != 1:
                raise ValueError("Only one synthetic tool call per turn is permitted")
            call = calls[0]
            result = tool_result(call)
            messages.append({"role": "tool", "tool_call_id": call["id"], "content": json.dumps(result)})
            observed = True
        raise RuntimeError("Synthetic agent exceeded its four-turn limit")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--tool-test", action="store_true")
    args = parser.parse_args()
    config_path = args.config.resolve()
    config = json.loads(config_path.read_text())
    for field in ("ca_file", "api_key_file"):
        if config.get(field) and not Path(config[field]).is_absolute():
            config[field] = str(config_path.parent / config[field])
    prompt = ("Use get_poc_inventory and report total GPU VRAM. State that the data is synthetic."
              if args.tool_test else "Reply with: Private inference connection verified.")
    result = PrivateClient(config).run(prompt, tools=args.tool_test, require_tool=args.tool_test)
    print(json.dumps({"status": "PASS", "tool_observed": result["tool_observed"],
                      "usage_final_request": result["usage"]}, indent=2))


if __name__ == "__main__":
    try:
        main()
    except (KeyError, ValueError, RuntimeError, OSError) as error:
        raise SystemExit(str(error)) from None

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
from urllib.parse import urlsplit


@dataclass(frozen=True)
class Connection:
    host: str
    port: int
    tls_hostname: str
    ca_file: str
    key_file: str = ''
    model: str = 'qwen3-8b'
    label: str = 'Private inference'
    grafana_url: str = ''
    context_limit: int = 4096

    @classmethod
    def load(cls, path):
        data = json.loads(Path(path).read_text())
        if set(data) - set(cls.__dataclass_fields__):
            raise ValueError('Connection configuration contains unsupported fields.')
        value = cls(**data)
        if not value.host or any(x in value.host for x in '/@?#\r\n'):
            raise ValueError('Invalid connection hostname.')
        if not 1 <= value.port <= 65535 or not value.tls_hostname:
            raise ValueError('Invalid TLS hostname or port.')
        if not Path(value.ca_file).is_file():
            raise ValueError('The trusted inference certificate is missing.')
        if value.grafana_url and urlsplit(value.grafana_url).scheme != 'https':
            raise ValueError('Grafana link must use HTTPS.')
        return value

    @property
    def identity(self):
        return hashlib.sha256(json.dumps(self.__dict__, sort_keys=True).encode()).hexdigest()

    def credential(self):
        if self.key_file:
            path = Path(self.key_file)
            if stat.S_IMODE(path.stat().st_mode) & 0o077:
                raise ValueError('Credential file must be readable only by its owner (chmod 600).')
            key = path.read_text().strip()
        else:
            key = os.environ.get('VLLM_API_KEY', '')
        if not key or any(c in key for c in '\r\n'):
            raise ValueError('A valid server-side inference credential is required.')
        return key

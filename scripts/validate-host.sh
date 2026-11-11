#!/bin/bash
# Run with sudo on the inference VM. Never prints the API credential.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo 'Run this script with sudo.' >&2
  exit 1
fi
nvidia-smi --query-gpu=name,memory.total --format=csv
mountpoint -q /mnt/models
systemctl is-active vllm nginx
auth_header=$(mktemp /run/vllm-auth.XXXXXX)
response_file=$(mktemp /run/vllm-response.XXXXXX)
trap 'rm -f "$auth_header" "$response_file"' EXIT
. /etc/vllm/server.env
printf 'Authorization: Bearer %s\n' "$VLLM_API_KEY" > "$auth_header"
base=https://inference.lab
tls_args=(--silent --show-error --cacert /etc/nginx/tls/server.crt --resolve inference.lab:443:127.0.0.1)
curl "${tls_args[@]}" --fail "$base/health"
unauth_status=$(curl "${tls_args[@]}" --output /dev/null --write-out '%{http_code}' "$base/v1/models")
test "$unauth_status" = 401
echo 'Unauthenticated model API correctly returned 401'
curl "${tls_args[@]}" --fail --header "@$auth_header" "$base/v1/models"
curl "${tls_args[@]}" --fail --max-time 120 --header "@$auth_header" --header 'Content-Type: application/json' --data '{"model":"qwen3-8b","messages":[{"role":"user","content":"Reply with READY only."}],"max_tokens":32,"temperature":0}' --output "$response_file" "$base/v1/chat/completions"
python3 - "$response_file" <<'PY'
import json,sys
d=json.load(open(sys.argv[1]))
assert d['choices'][0]['message']['content'], d
print('Authenticated HTTPS chat passed:', d['choices'][0]['message']['content'])
print('Token usage:', d.get('usage'))
PY

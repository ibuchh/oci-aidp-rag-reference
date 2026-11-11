#!/usr/bin/env bash
set -euo pipefail

# Run on the existing GPU host after reviewing the actual AI Compute source.
# This script manages only its own iptables rule, script and systemd unit.
CIDR="${1:-}"
MODE="${2:---dry-run}"
python3 - "$CIDR" <<'PY'
import ipaddress, sys
n = ipaddress.ip_network(sys.argv[1], strict=True)
allowed = ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
if n.version != 4 or n.prefixlen < 24 or not any(n.subnet_of(ipaddress.ip_network(x)) for x in allowed):
    raise SystemExit("Use a private IPv4 runtime subnet of /24 or smaller")
PY

UNIT="aidp-private-firewall.service"
SCRIPT="/usr/local/sbin/aidp-private-firewall.sh"
UNIT_FILE="/etc/systemd/system/$UNIT"
if [[ "$MODE" == "--dry-run" ]]; then
  echo "Allow TCP 443 from $CIDR using one persistent aidp-private rule."
  echo "Review the source; run with --apply as root to install or --remove to clean up."
  exit 0
fi
if [[ "$MODE" != "--apply" && "$MODE" != "--remove" ]]; then
  echo "Usage: $0 PRIVATE_CIDR [--dry-run|--apply|--remove]" >&2
  exit 2
fi
[[ "$EUID" -eq 0 ]] || { echo "Run as root" >&2; exit 1; }
for owned in "$SCRIPT" "$UNIT_FILE"; do
  if [[ -e "$owned" ]] && ! grep -q 'Managed by aidp-deployment' "$owned"; then
    echo "Refusing to replace a file not owned by this POC: $owned" >&2
    exit 1
  fi
done
if [[ -e "$UNIT_FILE" ]]; then
  systemctl disable --now "$UNIT"
fi
if [[ "$MODE" == "--remove" ]]; then
  if [[ -x "$SCRIPT" ]]; then "$SCRIPT" remove; fi
  rm -f -- "$SCRIPT" "$UNIT_FILE"
  systemctl daemon-reload
  exit 0
fi
umask 077
cat > "$SCRIPT" <<EOF
#!/usr/bin/env bash
# Managed by aidp-deployment
set -euo pipefail
if [[ "\${1:-add}" == "remove" ]]; then
  while iptables -C INPUT -p tcp -s "$CIDR" --dport 443 -m comment --comment aidp-private -j ACCEPT 2>/dev/null; do
    iptables -D INPUT -p tcp -s "$CIDR" --dport 443 -m comment --comment aidp-private -j ACCEPT
  done
else
  iptables -C INPUT -p tcp -s "$CIDR" --dport 443 -m comment --comment aidp-private -j ACCEPT 2>/dev/null ||
    iptables -I INPUT 1 -p tcp -s "$CIDR" --dport 443 -m comment --comment aidp-private -j ACCEPT
fi
EOF
chmod 700 "$SCRIPT"
cat > "$UNIT_FILE" <<EOF
# Managed by aidp-deployment
[Unit]
Description=Allow private cross-account AIDP HTTPS
After=network-online.target vllm-firewall.service
Wants=network-online.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=$SCRIPT add
ExecStop=$SCRIPT remove
[Install]
WantedBy=multi-user.target
EOF
chmod 644 "$UNIT_FILE"
systemctl daemon-reload
systemctl enable --now "$UNIT"

#!/bin/bash
set -euo pipefail
test "$(id -u)" = 0
test -n "${GRAFANA_HOST:?Set the public DNS hostname}"
cd /opt/monitoring
export DEBIAN_FRONTEND=noninteractive
python3 /opt/monitoring/apt-retry.py update -qq
python3 /opt/monitoring/apt-retry.py install -y -qq docker.io docker-compose-v2 docker-buildx curl
systemctl enable --now docker
device=/dev/oracleoci/oraclevdb
test -b "$device"
fstype=$(blkid -s TYPE -o value "$device" || true)
if [ -z "$fstype" ]; then
  # Refuse to format a device containing partitions or any recognized signature.
  test "$(lsblk -nr -o NAME "$device" | wc -l)" -eq 1
  test -z "$(wipefs -n --noheadings "$device")"
  mkfs.ext4 -L monitoring-data "$device"
elif [ "$fstype" != ext4 ]; then
  echo "Refusing unexpected filesystem $fstype" >&2
  exit 1
fi
uuid=$(blkid -s UUID -o value "$device")
install -d /var/lib/monitoring
if ! grep -q "UUID=$uuid " /etc/fstab; then
  printf 'UUID=%s /var/lib/monitoring ext4 defaults,nofail 0 2\n' "$uuid" >>/etc/fstab
fi
systemctl daemon-reload
mountpoint -q /var/lib/monitoring || mount /var/lib/monitoring
test "$(findmnt -n -o UUID --target /var/lib/monitoring)" = "$uuid"
install -d /etc/systemd/system/docker.service.d
cat >/etc/systemd/system/docker.service.d/monitoring-volume.conf <<'EOF'
[Unit]
RequiresMountsFor=/var/lib/monitoring
[Service]
ExecStartPre=/usr/bin/mountpoint -q /var/lib/monitoring
EOF
install -d /var/lib/monitoring/{prometheus,grafana,caddy,caddy-config,textfile}
chown 65534:65534 /var/lib/monitoring/prometheus
chown 472:472 /var/lib/monitoring/grafana
install -d -m 700 /etc/monitoring
install -d -o 472 -g 472 -m 700 /etc/monitoring/grafana-secrets
python3 - <<'PY'
from pathlib import Path
import secrets,os
for name in ('admin_password','secret_key'):
 p=Path('/etc/monitoring/grafana-secrets')/name
 if not p.exists():
  p.write_text(secrets.token_urlsafe(36)+'\n')
  p.chmod(0o400)
  os.chown(p,472,472)
PY
chmod 755 /etc/monitoring
chmod 644 /etc/monitoring/gpu.crt
printf 'GRAFANA_URL=https://%s\n' "$GRAFANA_HOST" >/opt/monitoring/.env
if ! test -f /etc/monitoring/streamlit-auth.caddy; then
  printf 'respond "Streamlit is not configured" 503\n' >/etc/monitoring/streamlit-auth.caddy
fi
cat >/opt/monitoring/Caddyfile <<EOF
{
  admin 127.0.0.1:2019
  auto_https disable_redirects
  acme_ca https://acme-v02.api.letsencrypt.org/directory
}
http://$GRAFANA_HOST {
  redir https://$GRAFANA_HOST{uri} 308
}
$GRAFANA_HOST {
  header {
    Strict-Transport-Security "max-age=31536000"
    X-Content-Type-Options "nosniff"
    Referrer-Policy "strict-origin-when-cross-origin"
    -Server
  }
  reverse_proxy 127.0.0.1:3000
}
http://streamlit.$GRAFANA_HOST {
  redir https://streamlit.$GRAFANA_HOST{uri} 308
}
streamlit.$GRAFANA_HOST {
  import /etc/monitoring/streamlit-auth.caddy
}
EOF
cat >/opt/monitoring/firewall.sh <<'EOF'
#!/bin/bash
set -euo pipefail
for port in 80 443; do
  if ! iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
    iptables -I INPUT 1 -p tcp --dport "$port" -j ACCEPT
  fi
done
EOF
chmod 755 /opt/monitoring/firewall.sh
cat >/etc/systemd/system/monitoring-firewall.service <<'EOF'
[Unit]
Description=Public Grafana HTTPS ingress
After=network.target
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/opt/monitoring/firewall.sh
[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/monitoring.service <<'EOF'
[Unit]
Description=Prometheus Grafana and HTTPS proxy
Requires=docker.service
After=docker.service network-online.target monitoring-firewall.service
RequiresMountsFor=/var/lib/monitoring
ConditionPathIsMountPoint=/var/lib/monitoring
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/monitoring
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose stop
[Install]
WantedBy=multi-user.target
EOF
cat >/etc/systemd/system/inference-probe.service <<'EOF'
[Unit]
Description=Bounded authenticated private HTTPS inference probe
After=monitoring.service
RequiresMountsFor=/var/lib/monitoring
[Service]
Type=oneshot
EnvironmentFile=/etc/monitoring/inference.env
ExecStart=/usr/bin/python3 /opt/monitoring/inference-probe.py
TimeoutStartSec=75
UMask=0022
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/monitoring/textfile
EOF
cat >/etc/systemd/system/inference-probe.timer <<'EOF'
[Unit]
Description=Run the lab inference probe every five minutes
[Timer]
OnBootSec=2min
OnUnitActiveSec=5min
Persistent=true
[Install]
WantedBy=timers.target
EOF
python3 pin-monitoring-images.py compose.yml
docker compose run --rm --no-deps --entrypoint /bin/promtool prometheus \
  check config /etc/prometheus/prometheus.yml
systemctl daemon-reload
systemctl enable --now monitoring-firewall.service monitoring.service
docker compose up -d
systemctl enable --now inference-probe.timer
systemctl start inference-probe.service
echo 'Monitoring services installed. Credentials remain on the VM.'

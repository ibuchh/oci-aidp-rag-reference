#!/bin/bash
set -euo pipefail
test "$(id -u)" = 0
cd /opt/vllm-monitoring
systemctl is-active --quiet nvidia-dcgm
if ! docker compose version >/dev/null 2>&1; then
  python3 /opt/vllm-monitoring/apt-retry.py install -y -qq docker-compose-v2
fi
install -d -m 700 /var/backups/vllm-monitoring
if [ ! -f /var/backups/vllm-monitoring/firewall.sh ]; then
  cp -p /opt/vllm/firewall.sh /var/backups/vllm-monitoring/firewall.sh
fi
python3 pin-monitoring-images.py gpu-compose.yml
cat >/etc/nginx/sites-available/vllm-metrics <<'EOF'
server {
  listen 9443 ssl;
  server_name _;
  ssl_certificate /etc/nginx/tls/server.crt;
  ssl_certificate_key /etc/nginx/tls/server.key;
  ssl_protocols TLSv1.2 TLSv1.3;
  allow __MONITORING_PRIVATE_IP__;
  deny all;
  location = /vllm { proxy_pass http://127.0.0.1:8000/metrics; }
  location = /node { proxy_pass http://127.0.0.1:9100/metrics; }
  location = /gpu  { proxy_pass http://127.0.0.1:9400/metrics; }
  location / { return 404; }
}
EOF
ln -sfn /etc/nginx/sites-available/vllm-metrics /etc/nginx/sites-enabled/vllm-metrics
cat >/opt/vllm/firewall.sh <<'EOF'
#!/bin/bash
set -euo pipefail
if ! iptables -C INPUT -p tcp -s __BASTION_SUBNET_CIDR__ --dport 443 -j ACCEPT 2>/dev/null; then
  iptables -I INPUT 1 -p tcp -s __BASTION_SUBNET_CIDR__ --dport 443 -j ACCEPT
fi
for port in 443 9443; do
  if ! iptables -C INPUT -p tcp -s __MONITORING_PRIVATE_IP__/32 --dport "$port" -j ACCEPT 2>/dev/null; then
    iptables -I INPUT 1 -p tcp -s __MONITORING_PRIVATE_IP__/32 --dport "$port" -j ACCEPT
  fi
done
EOF
chmod 755 /opt/vllm/firewall.sh
/opt/vllm/firewall.sh
install -d -m 755 /var/lib/vllm-monitoring/textfile
cat >/etc/systemd/system/vllm-host-health.service <<'EOF'
[Unit]
Description=Bounded GPU host service health metrics
[Service]
Type=oneshot
ExecStart=/usr/bin/python3 /opt/vllm-monitoring/host-health.py
NoNewPrivileges=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/var/lib/vllm-monitoring/textfile
EOF
cat >/etc/systemd/system/vllm-host-health.timer <<'EOF'
[Unit]
Description=Collect GPU host service states every minute
[Timer]
OnBootSec=30s
OnUnitActiveSec=60s
[Install]
WantedBy=timers.target
EOF
cat >/etc/systemd/system/vllm-exporters.service <<'EOF'
[Unit]
Description=vLLM GPU and host exporters
Requires=docker.service
After=docker.service nvidia-dcgm.service
[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/vllm-monitoring
ExecStart=/usr/bin/docker compose -f gpu-compose.yml up -d
ExecStop=/usr/bin/docker compose -f gpu-compose.yml stop
[Install]
WantedBy=multi-user.target
EOF
nginx -t
systemctl daemon-reload
systemctl enable --now vllm-host-health.timer
systemctl start vllm-host-health.service
systemctl enable --now vllm-exporters.service
docker compose -f gpu-compose.yml up -d
systemctl reload nginx
curl --retry 12 --retry-delay 5 --retry-connrefused -fsS \
  http://127.0.0.1:9400/metrics >/dev/null
curl -fsS http://127.0.0.1:9100/metrics >/dev/null
echo 'GPU exporters installed; vLLM was not restarted.'

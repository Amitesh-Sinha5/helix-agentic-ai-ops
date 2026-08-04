#!/bin/bash
# Bootstraps Helix on a 1 GB free-tier instance.
#
# Rendered through Terraform's templatefile(). Shell variables must be written
# with a doubled dollar sign so they survive interpolation; single-dollar names
# are substituted by Terraform at plan time.
set -euxo pipefail
exec > >(tee /var/log/helix-boot.log) 2>&1

echo "=== Helix bootstrap starting at $(date) ==="

# --- Swap --------------------------------------------------------------------
# t3.micro has 1 GB of RAM. The backend alone (FastAPI + Chroma + scikit-learn
# + numpy/scipy) peaks well above that, and building the images needs more
# still. Without swap the first `docker build` is OOM-killed and the instance
# looks broken. This is the single most important line in the file.
if [ ! -f /swapfile ]; then
  dd if=/dev/zero of=/swapfile bs=1M count=$((${swap_gb} * 1024)) status=none
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  # Lean on swap only under real pressure, not eagerly.
  sysctl -w vm.swappiness=20
  echo 'vm.swappiness=20' > /etc/sysctl.d/99-helix.conf
fi
free -h

# --- Docker ------------------------------------------------------------------
dnf update -y
dnf install -y docker git
systemctl enable --now docker
usermod -aG docker ec2-user

mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

# --- Application -------------------------------------------------------------
cd /opt
rm -rf helix
git clone --depth 1 ${repo_url} helix
cd helix

TOKEN="$(curl -fsS -X PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 300')"
PUBLIC_IP="$(curl -fsS -H "X-aws-ec2-metadata-token: $${TOKEN}" \
  http://169.254.169.254/latest/meta-data/public-ipv4)"

cat > .env <<EOF
ENVIRONMENT=production
JWT_SECRET_KEY=$(openssl rand -base64 48 | tr -d '\n')
LLM_PROVIDER=${llm_provider}
EMBEDDING_PROVIDER=mock

# The browser calls these, so they must be the public address of this box --
# never localhost, never a compose service name.
VITE_API_URL=http://$${PUBLIC_IP}:8000
CORS_ORIGINS=http://$${PUBLIC_IP}:5173
BILLING_SUCCESS_URL=http://$${PUBLIC_IP}:5173/billing?status=success
BILLING_CANCEL_URL=http://$${PUBLIC_IP}:5173/billing?status=cancelled
EOF
chmod 600 .env
chown -R ec2-user:ec2-user /opt/helix

# The free-tier compose file: SQLite and the in-process cache instead of
# Postgres and Redis containers, which saves ~200 MB of RAM.
docker compose -f docker-compose.free.yml up --build -d

# --- Survive reboots ---------------------------------------------------------
cat > /etc/systemd/system/helix.service <<'EOF'
[Unit]
Description=Helix
Requires=docker.service
After=docker.service network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/helix
ExecStart=/usr/bin/docker compose -f docker-compose.free.yml up -d
ExecStop=/usr/bin/docker compose -f docker-compose.free.yml down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF
systemctl enable helix.service

# Reclaim build layers; 30 GB fills faster than you would think.
docker image prune -f

echo "=== Helix bootstrap finished at $(date) ==="
echo "frontend http://$${PUBLIC_IP}:5173"
echo "api      http://$${PUBLIC_IP}:8000"
echo "Sign up immediately -- the first account becomes the administrator."

#!/bin/bash
# Helix on a single EC2 instance — paste this as the instance's User data.
#
# Target: Amazon Linux 2023, arm64 (t4g.small or larger). Runs the whole
# docker compose stack — backend, frontend, Postgres and Redis — on one box.
#
# Deliberately simple. One instance means one availability zone and no
# automatic failover; see README.md for when that is the wrong choice.
set -euxo pipefail

dnf update -y
dnf install -y docker git
systemctl enable --now docker
usermod -aG docker ec2-user

# Compose v2 as a CLI plugin.
mkdir -p /usr/local/lib/docker/cli-plugins
curl -fsSL "https://github.com/docker/compose/releases/latest/download/docker-compose-linux-$(uname -m)" \
  -o /usr/local/lib/docker/cli-plugins/docker-compose
chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

cd /opt
git clone https://github.com/Amitesh-Sinha5/helix-agentic-ai-ops.git helix
cd helix

PUBLIC_IP="$(curl -fsS -H "X-aws-ec2-metadata-token: $(
  curl -fsS -X PUT http://169.254.169.254/latest/api/token \
    -H 'X-aws-ec2-metadata-token-ttl-seconds: 60'
)" http://169.254.169.254/latest/meta-data/public-ipv4)"

# Generated once and persisted, so a reboot does not invalidate every session.
JWT_SECRET="$(openssl rand -base64 48 | tr -d '\n')"
POSTGRES_PASSWORD="$(openssl rand -base64 24 | tr -d '\n/+=')"

cat > .env <<EOF
ENVIRONMENT=production
POSTGRES_PASSWORD=${POSTGRES_PASSWORD}
JWT_SECRET_KEY=${JWT_SECRET}

# The browser calls these, so they must be the instance's public address —
# not localhost, and not the compose service name.
VITE_API_URL=http://${PUBLIC_IP}:8000
CORS_ORIGINS=http://${PUBLIC_IP}:5173
BILLING_SUCCESS_URL=http://${PUBLIC_IP}:5173/billing?status=success
BILLING_CANCEL_URL=http://${PUBLIC_IP}:5173/billing?status=cancelled

# Swap to anthropic/openai and add a key once you want a real model.
LLM_PROVIDER=mock
EMBEDDING_PROVIDER=mock
EOF
chmod 600 .env
chown -R ec2-user:ec2-user /opt/helix

docker compose up --build -d

# Restart the stack on reboot.
cat > /etc/systemd/system/helix.service <<'EOF'
[Unit]
Description=Helix stack
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
WorkingDirectory=/opt/helix
ExecStart=/usr/bin/docker compose up -d
ExecStop=/usr/bin/docker compose down

[Install]
WantedBy=multi-user.target
EOF
systemctl enable helix.service

echo "Helix is up:"
echo "  frontend http://${PUBLIC_IP}:5173"
echo "  api      http://${PUBLIC_IP}:8000"
echo "Sign up immediately — the first account becomes the administrator."

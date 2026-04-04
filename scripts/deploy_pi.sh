#!/bin/bash
set -e

# PI_IP can be set as env var or passed as first argument.
# Find Pi MAC prefix DC:A6:32 or E4:5F:01 via: arp -a (PowerShell)
PI_IP="${PI_IP:-${1:-}}"
if [ -z "$PI_IP" ]; then
  echo "Usage: PI_IP=192.168.1.xx bash scripts/deploy_pi.sh"
  echo "   or: bash scripts/deploy_pi.sh 192.168.1.xx"
  exit 1
fi

PI_USER="${PI_USER:-pi}"
PI_PATH="/home/pi/mara"

echo "Syncing to Pi at $PI_IP..."
rsync -avz --exclude='.env' --exclude='data/db' --exclude='workers/*/src' \
  ./ $PI_USER@$PI_IP:$PI_PATH/
scp .env $PI_USER@$PI_IP:$PI_PATH/.env
ssh $PI_USER@$PI_IP "cd $PI_PATH && docker compose -f docker-compose.yml -f docker-compose.pi.yml pull && docker compose -f docker-compose.yml -f docker-compose.pi.yml up -d"

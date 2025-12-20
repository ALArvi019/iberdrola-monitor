#!/bin/bash

# Configuration
REMOTE_USER="${REMOTE_USER:-pi}"
REMOTE_HOST="${REMOTE_HOST:-raspberrypi.local}"
REMOTE_PATH="${REMOTE_PATH:-/home/pi/stacks/iberdrola-monitor/}"

echo "🚀 Deploying to $REMOTE_HOST..."

ssh $REMOTE_USER@$REMOTE_HOST "cd $REMOTE_PATH && \
    echo '📥 Using explicit pull for master...' && \
    git fetch origin && \
    git reset --hard origin/master && \
    echo '🏗️  Rebuilding container...' && \
    docker compose up -d --build --force-recreate && \
    echo '♻️  Pruning unused images...' && \
    docker image prune -f"

echo "✅ Deploy completed!"

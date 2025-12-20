#!/bin/bash

# Configuration
REMOTE_USER="alex"
REMOTE_HOST="192.168.0.38"
REMOTE_PATH="/home/alex/portainer-stacks/iberdrola-monitor/"

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

#!/bin/bash
# Deploy to Production VPS

set -e

HOST="${DEPLOY_HOST:?Set DEPLOY_HOST env var (e.g. user@hostname)}"
DIR="${DEPLOY_DIR:-/root/oracle}"

echo "🚀 Deploying to $HOST:$DIR..."

# 1. Sync code (rsync is faster and safer than scp)
rsync -avz --progress \
    --exclude '.git' \
    --exclude '.venv' \
    --exclude '__pycache__' \
    --exclude 'node_modules' \
    --exclude '.env' \
    --exclude '.DS_Store' \
    --exclude 'data' \
    ./ "$HOST:$DIR/"

# 2. Apply crontab (idempotent — перезаписывает текущий crontab из файла)
echo "⏰ Installing crontab..."
ssh "$HOST" "crontab $DIR/crontab.conf"

# 3. Rebuild and restart Docker containers
echo "🔄 Restarting services..."
ssh "$HOST" "cd $DIR && docker compose up -d --build --remove-orphans"

# 3.1 Cleanup dangling images and build cache
echo "🧹 Pruning old Docker images..."
ssh "$HOST" "docker image prune -f && docker builder prune -f" 2>/dev/null || true

# 4. Smoke tests — wait for backend to start, then verify
echo "🧪 Running smoke tests..."
sleep 5

FAIL=0

smoke() {
    local name="$1" url="$2" expect="${3:-200}"
    status=$(ssh "$HOST" "curl -s -o /dev/null -w '%{http_code}' '$url'" 2>/dev/null)
    if [ "$status" = "$expect" ]; then
        echo "  ✅ $name — $status"
    else
        echo "  ❌ $name — got $status, expected $expect"
        FAIL=1
    fi
}

smoke "Health"       "http://localhost:8000/health"
smoke "Markets list" "http://localhost:8000/api/markets/"
smoke "PostgreSQL"   "http://localhost:8000/health"  # health endpoint checks DB and Redis internally
smoke "Nginx"        "http://localhost:80"            # nginx serves frontend static files

if [ $FAIL -ne 0 ]; then
    echo ""
    echo "⚠️  Some smoke tests failed! Check logs:"
    echo "  ssh $HOST 'docker logs oraclepoly-backend --tail 30'"
    exit 1
fi

echo ""
echo "✅ Deployment complete — all checks passed!"

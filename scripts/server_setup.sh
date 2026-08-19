#!/bin/bash
# OraclePoly VPS Setup Script
# Ubuntu 22.04+

set -e

echo "🚀 Начинаем установку окружения для OraclePoly..."

# 1. Update system
sudo apt update && sudo apt upgrade -y

# 2. Install Docker
if ! command -v docker &> /dev/null; then
    echo "📦 Устанавливаем Docker..."
    sudo apt install -y docker.io
    sudo systemctl start docker
    sudo systemctl enable docker
    sudo usermod -aG docker $USER
fi

# 3. Docker Compose v2 встроен в docker.io (docker compose, без дефиса)
# Проверяем наличие
if ! docker compose version &> /dev/null; then
    echo "⚠️ Docker Compose v2 не найден. Установите актуальную версию docker.io"
fi

# 4. Install Nginx & Certbot
echo "🌐 Устанавливаем Nginx и Certbot для SSL..."
sudo apt install -y nginx certbot python3-certbot-nginx

# 5. Install PostgreSQL client (для прямого доступа к БД через psql)
if ! command -v psql &> /dev/null; then
    echo "📦 Устанавливаем PostgreSQL client..."
    sudo apt install -y postgresql-client
fi

# 6. Create directories
mkdir -p logs
mkdir -p /root/backups

echo "✅ Базовая настройка завершена!"
echo ""
echo "Следующие шаги:"
echo "  1. Настроить .env: cp .env.example .env && nano .env"
echo "  2. Запустить: docker compose up -d --build"
echo "  3. Настроить SSL: sudo certbot --nginx -d yourdomain.com"
echo "  4. Подключиться к БД: psql -U oraclepoly -d oraclepoly -h localhost"
echo "     Или: docker exec -it oraclepoly-postgres psql -U oraclepoly -d oraclepoly"

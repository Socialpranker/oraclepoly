# OraclePoly: Полное руководство по деплою и эксплуатации

Единый гайд по развертыванию, настройке сервера и обслуживанию проекта OraclePoly.

---

## Содержание

1. [Подготовка сервера (VPS)](#1-подготовка-сервера-vps)
2. [Переменные окружения](#2-переменные-окружения)
3. [Развертывание приложения](#3-развертывание-приложения)
4. [Настройка домена и HTTPS](#4-настройка-домена-и-https)
5. [Быстрый деплой (обновление)](#5-быстрый-деплой-обновление)
6. [Rollback](#6-rollback)
7. [Миграции (PostgreSQL)](#7-миграции-postgresql)
8. [Обслуживание и стабильность](#8-обслуживание-и-стабильность)
9. [Troubleshooting](#9-troubleshooting)
10. [Чек-лист проверки](#10-чек-лист-проверки)

---

## 1. Подготовка сервера (VPS)

### Требования
- **OS**: Ubuntu 22.04 / 24.04
- **RAM**: Минимум 2GB
- **Локация**: Европа (Amsterdam, Frankfurt) для доступа к OpenAI.
- **Провайдеры**: DigitalOcean, Hetzner, Vultr, Linode, Aeza, PQ.Hosting.

### Шаг 1.1: Настройка SSH доступа

```bash
# На вашем локальном компьютере
ssh-copy-id root@<IP_ВАШЕГО_СЕРВЕРА>
# Подключение
ssh root@<IP_ВАШЕГО_СЕРВЕРА>
```

### Шаг 1.2: Установка зависимостей

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install git docker.io nginx certbot python3-certbot-nginx -y
# Docker Compose v2 встроен в docker.io (docker compose, без дефиса)
```

### Шаг 1.3: Настройка Swap (Важно для 2GB RAM)

```bash
sudo fallocate -l 2G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

---

## 2. Переменные окружения

Создайте файл `.env` в корне проекта. Все переменные загружаются через `app/core/config.py` (pydantic-settings).

### Обязательные (без default — приложение не запустится)

| Переменная | Описание |
|---|---|
| `DATABASE_URL` | PostgreSQL connection string |
| `POSTGRES_PASSWORD` | Пароль PostgreSQL (используется в docker-compose) |
| `R2_ACCOUNT_ID` | Cloudflare Account ID |
| `R2_ACCESS_KEY_ID` | R2 Access Key |
| `R2_SECRET_ACCESS_KEY` | R2 Secret Key |
| `QDRANT_URL` | Qdrant Cloud URL |
| `QDRANT_API_KEY` | Qdrant API Key |
| `OPENAI_API_KEY` | OpenAI API Key (embeddings) |

### Опциональные (есть default, можно не указывать)

| Переменная | Default | Описание |
|---|---|---|
| `R2_BUCKET_NAME` | `oracle-data-lake` | Имя бакета R2 |
| `FIRECRAWL_API_KEY` | `""` | Firecrawl API Key для скрапинга |
| `SERPER_API_KEY` | `""` | Serper API Key (fallback scraping) |
| `TELEGRAM_BOT_TOKEN` | `""` | Telegram Bot API Token |
| `TELEGRAM_WEBHOOK_SECRET` | `""` | Telegram Webhook Secret Token |
| `POLYMARKET_API_KEY` | `""` | Polymarket API Key |
| `POLYMARKET_PRIVATE_KEY` | `""` | Polymarket Private Key |
| `POLYMARKET_ADDRESS` | `""` | Polymarket Wallet Address |
| `POLYMARKET_HOST` | `https://clob.polymarket.com` | CLOB API Host |
| `POLYMARKET_CHAIN_ID` | `137` | Chain ID (137 = Polygon) |
| `REDIS_URL` | `redis://:oracle_redis_secret@localhost:6379/0` | Redis connection URL |
| `SENTRY_DSN` | `null` | Sentry DSN для мониторинга ошибок |
| `CORS_ORIGINS` | `["http://localhost:5173"]` | JSON-массив разрешённых origins |

### Docker Compose переменные

| Переменная | Default | Описание |
|---|---|---|
| `REDIS_PASSWORD` | `oracle_redis_secret` | Пароль Redis (используется в `docker-compose.yml`) |

> **Важно:** на проде обязательно смените `REDIS_PASSWORD` и обновите `REDIS_URL` соответственно.

---

## 3. Развертывание приложения

### Шаг 3.1: Клонирование репозитория

```bash
git clone <URL_РЕПОЗИТОРИЯ> /root/oracle
cd /root/oracle
```

### Шаг 3.2: Настройка окружения (.env)

```bash
cp .env.example .env  # если есть .env.example
nano .env
```

Заполните все обязательные переменные из [секции 2](#2-переменные-окружения).

### Шаг 3.3: Запуск через Docker Compose

Сервисы: **postgres** (PostgreSQL 16, порт 5432), **redis** (кэш, порт 6379), **backend** (FastAPI, порт 8000), **frontend** (сборка, порт 3000).

> **На проде:** Frontend раздаётся через nginx из статики (`/root/oracle/client/dist/`), Docker-контейнер frontend используется для сборки.

```bash
docker compose up -d --build
```

Проверка:
```bash
# Статус контейнеров (должно быть 4: postgres, redis, backend, frontend)
docker ps

# PostgreSQL работает
docker exec oraclepoly-postgres pg_isready -U oraclepoly

# Redis работает
docker exec oraclepoly-redis redis-cli -a "${REDIS_PASSWORD:-oracle_redis_secret}" ping
# Ожидаемый ответ: PONG

# Backend healthcheck
curl http://localhost:8000/health
```

---

## 4. Настройка домена и HTTPS

### Шаг 4.1: Конфигурация Nginx

Создайте конфиг `/etc/nginx/sites-available/oraclepoly`:

```nginx
server {
    listen 80;
    server_name example.com; # Замените на ваш домен

    # Frontend (статические файлы)
    location / {
        root /root/oracle/client/dist;
        try_files $uri $uri/ /index.html;
    }

    # Backend API
    location /api {
        proxy_pass http://localhost:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }
}
```

### Шаг 4.2: Активация и SSL

```bash
sudo ln -s /etc/nginx/sites-available/oraclepoly /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl restart nginx

# Получение сертификата
sudo certbot --nginx -d example.com
```

---

## 5. Быстрый деплой (обновление)

### Через deploy.sh (с локальной машины)

```bash
DEPLOY_HOST=root@<IP> ./scripts/deploy.sh
```

Скрипт синхронизирует код через rsync и перезапускает контейнеры.

### Вручную на сервере

```bash
cd /root/oracle
git pull origin main
docker compose up -d --build
```

### Обновить только backend (без пересборки frontend)

```bash
docker compose up -d --build backend
```

---

## 6. Rollback

### Откат на предыдущий коммит

```bash
cd /root/oracle

# Посмотреть историю
git log --oneline -10

# Откатиться на конкретный коммит
git checkout <COMMIT_HASH> -- .
docker compose up -d --build
```

### Откат через Docker (если образ ещё в кэше)

```bash
# Посмотреть предыдущие образы
docker images oraclepoly-backend

# Перезапустить с предыдущим образом (без --build)
git stash  # сохранить текущие изменения
git checkout HEAD~1
docker compose up -d --build
```

### Откат одного сервиса

```bash
# Только backend
docker compose stop backend
git checkout <COMMIT_HASH> -- app/ Dockerfile requirements.txt
docker compose up -d --build backend
```

> После отката убедитесь что `curl http://localhost:8000/health` возвращает 200.

---

## 7. Миграции (PostgreSQL)

База данных управляется через локальный PostgreSQL в Docker. Миграции применяются через `psql` или скрипт.

### Через psql

```bash
# Подключиться к БД на сервере (через Docker)
docker exec -it oraclepoly-postgres psql -U oraclepoly -d oraclepoly

# Или через обёртку
./scripts/psql.sh

# Применить миграцию
docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly < migrations/143_realtime_triggers.sql
```

### Через скрипт

```bash
# Применить миграцию (использует DATABASE_URL из окружения)
python scripts/run_migration.py migrations/143_realtime_triggers.sql
```

### Начальная инициализация

При первом запуске `scripts/init_db.sh` автоматически применяет все миграции из `migrations/` в правильном порядке.

> Перед применением миграций на проде — сделайте бэкап: `scripts/backup_postgres.sh`

---

## 8. Обслуживание и стабильность

### Мониторинг ресурсов

```bash
free -h           # Проверка памяти
docker stats      # Статистика контейнеров
```

### Логирование

```bash
# Логи бэкенда
docker logs -f oraclepoly-backend

# Логи PostgreSQL
docker logs -f oraclepoly-postgres

# Логи Redis
docker logs -f oraclepoly-redis

# Логи фронтенда
docker logs -f oraclepoly-frontend

# Логи Nginx (ошибки)
tail -f /var/log/nginx/error.log
```

### Очистка места

```bash
docker system prune -a  # Удалить неиспользуемые образы и контейнеры
```

### Перезапуск всех сервисов

```bash
docker compose down && docker compose up -d
```

---

## 9. Troubleshooting

### Docker pull rate limit (Too Many Requests)

```
toomanyrequests: You have reached your pull rate limit
```

**Решение:** авторизуйтесь в Docker Hub:
```bash
docker login
# Или подождите ~6 часов для сброса лимита
```

### OOM Killed (контейнер убит из-за нехватки памяти)

```bash
# Проверить что контейнер упал из-за OOM
docker inspect oraclepoly-backend | grep -i oom

# Увеличить swap
sudo fallocate -l 4G /swapfile2
sudo chmod 600 /swapfile2 && sudo mkswap /swapfile2 && sudo swapon /swapfile2
```

Лимиты памяти в `docker-compose.yml`: postgres — 1024MB, backend — 700MB, redis — 128MB, frontend — 128MB.

### SSH подключение отвергнуто

```
ssh: connect to host <IP> port 22: Connection refused
```

**Решения:**
- Проверьте что сервер запущен (панель провайдера)
- Проверьте firewall: `sudo ufw status` — порт 22 должен быть открыт
- Подключитесь через VNC-консоль провайдера

### Redis не подключается

```bash
# Проверить что контейнер запущен
docker ps | grep redis

# Тест подключения
docker exec oraclepoly-redis redis-cli -a oracle_redis_secret ping

# Проверить что REDIS_URL в .env совпадает с паролем в docker-compose.yml
```

### Backend не стартует (env validation error)

```
pydantic_settings.errors.SettingsError: ...
```

Не хватает обязательных переменных. Проверьте `.env` файл — все переменные из секции «Обязательные» должны быть заполнены.

### Healthcheck fails

```bash
# Проверить что backend запустился
docker logs oraclepoly-backend --tail 50

# Проверить healthcheck endpoint
curl -v http://localhost:8000/health
```

---

## 10. Чек-лист проверки

- [ ] `.env` заполнен — все обязательные переменные указаны
- [ ] `REDIS_PASSWORD` изменён с дефолтного значения
- [ ] `CORS_ORIGINS` содержит продакшен домен
- [ ] Все контейнеры запущены: `docker ps` показывает postgres, redis, backend, frontend
- [ ] Redis отвечает: `docker exec oraclepoly-redis redis-cli -a <password> ping` → PONG
- [ ] Backend healthcheck: `curl http://localhost:8000/health` → 200
- [ ] Сайт открывается по HTTPS (`https://example.com`)
- [ ] API доступен (`https://example.com/api/docs`)
- [ ] Redis Queue worker запущен (`scripts/oracle_worker.py`)
- [ ] Cron jobs активны (`scripts/cron_runner.py`)
- [ ] Telegram Bot настроен на Web App URL
- [ ] SSL-сертификат валиден: `sudo certbot certificates`
- [ ] Swap активен: `swapon --show`

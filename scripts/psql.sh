#!/bin/bash
# Подключение к PostgreSQL на сервере
# Использование:
#   ./scripts/psql.sh                      — интерактивная сессия
#   ./scripts/psql.sh -c "SELECT 1"        — выполнить запрос
#   ./scripts/psql.sh -f migrations/X.sql  — применить миграцию

set -e

# Попробовать нативный psql, иначе docker exec
if command -v psql &> /dev/null; then
    PGPASSWORD="${POSTGRES_PASSWORD}" psql -U oraclepoly -d oraclepoly -h localhost "$@"
else
    docker exec -it oraclepoly-postgres psql -U oraclepoly -d oraclepoly "$@"
fi

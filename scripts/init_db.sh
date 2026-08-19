#!/bin/bash
set -e

# ==================================================
# OraclePoly — Инициализация PostgreSQL
# Запускается автоматически при первом старте контейнера
# через docker-entrypoint-initdb.d
# ==================================================

echo "=== OraclePoly: Инициализация базы данных ==="

# Расширения
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
    CREATE EXTENSION IF NOT EXISTS "pg_trgm";
    CREATE EXTENSION IF NOT EXISTS "pgcrypto";
    CREATE EXTENSION IF NOT EXISTS "pg_stat_statements";
EOSQL

echo "=== Расширения созданы ==="

# Применить миграции в правильном порядке
# Нумерация: 001-007, 101-142 (гэп 008-100 намеренный)
# Дубли 134, 136 — результат параллельных веток, оба нужны
MIGRATION_DIR="/docker-entrypoint-initdb.d/migrations"

if [ -d "$MIGRATION_DIR" ]; then
    echo "=== Применяю миграции из $MIGRATION_DIR ==="

    # Сортировка: сначала по числовому префиксу, потом по полному имени
    for migration in $(ls "$MIGRATION_DIR"/*.sql 2>/dev/null | sort); do
        echo "  -> $(basename "$migration")"
        psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" -f "$migration"
    done

    echo "=== Миграции применены ==="
else
    echo "WARN: Директория миграций не найдена: $MIGRATION_DIR"
fi

echo "=== OraclePoly: Инициализация завершена ==="

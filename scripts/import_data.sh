#!/bin/bash
set -euo pipefail

# ==================================================
# OraclePoly — Импорт данных в локальный PostgreSQL
#
# Использование:
#   ./scripts/import_data.sh <DUMP_FILE>
#
# DUMP_FILE — путь к .sql или .sql.gz файлу (pg_dump)
#
# Перед запуском убедитесь:
#   1. PostgreSQL контейнер запущен (docker compose up -d postgres)
#   2. Миграции применены (init_db.sh выполнен)
# ==================================================

if [ $# -lt 1 ]; then
    echo "Usage: $0 <DUMP_FILE>"
    echo "  DUMP_FILE: .sql or .sql.gz file from pg_dump"
    exit 1
fi

DUMP_FILE="$1"
DB_CONTAINER="oraclepoly-postgres"
DB_NAME="oraclepoly"
DB_USER="oraclepoly"

if [ ! -f "$DUMP_FILE" ]; then
    echo "ОШИБКА: Файл не найден: $DUMP_FILE"
    exit 1
fi

echo "=== Импорт данных ==="
echo "Дата: $(date)"
echo "Файл: $DUMP_FILE"

# Проверка контейнера
if ! docker ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$"; then
    echo "ОШИБКА: Контейнер $DB_CONTAINER не запущен"
    echo "Запустите: docker compose up -d postgres"
    exit 1
fi

# Подсчёт строк ДО импорта
echo ""
echo "=== Строки ДО импорта ==="
docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t -A -c "
SELECT tablename, n_live_tup
FROM pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY tablename;
"

# Импорт
echo ""
echo "=== Загрузка данных ==="

if [[ "$DUMP_FILE" == *.gz ]]; then
    echo "Распаковка и импорт .gz файла..."
    gunzip -c "$DUMP_FILE" | docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" --single-transaction -v ON_ERROR_STOP=1
else
    echo "Импорт .sql файла..."
    docker exec -i "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" --single-transaction -v ON_ERROR_STOP=1 < "$DUMP_FILE"
fi

echo ""
echo "=== Импорт завершён ==="

# Подсчёт строк ПОСЛЕ импорта
echo ""
echo "=== Строки ПОСЛЕ импорта ==="
docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -t -A -c "
SELECT tablename, n_live_tup
FROM pg_stat_user_tables
WHERE schemaname = 'public'
ORDER BY tablename;
"

# ANALYZE для актуализации статистики
echo ""
echo "=== ANALYZE ==="
docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c "ANALYZE;"
echo "Статистика обновлена"

echo ""
echo "=== Готово ==="

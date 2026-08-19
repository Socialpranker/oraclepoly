#!/bin/bash
set -euo pipefail

# ==================================================
# OraclePoly — Бэкап PostgreSQL (prod, VPS)
#
# Cron:  0 4 * * * flock -xn /tmp/oracle-backup.lock /root/oracle/scripts/backup_postgres.sh >> /var/log/oracle/backup.log 2>&1
#
# Формат:  pg_dump --format=custom | gzip  (восстановление через pg_restore, см. docs/restore_db.md)
# Ротация: 7 ежедневных + 4 еженедельных (воскресенье)
# Offsite: Cloudflare R2 через rclone (если установлен и настроен)
# Алерт:   Telegram при любой ошибке / пустом дампе (бот из .env)
#
# Restore-процедура: docs/restore_db.md
# ==================================================

BACKUP_DIR="/root/backups"
DB_NAME="oraclepoly"
DB_USER="oraclepoly"
PG_CONTAINER="oraclepoly-postgres"
ENV_FILE="/root/oracle/.env"

# Минимальный валидный размер сжатого дампа. БД ~2.3 GB → сжатый custom-дамп
# сотни МБ. Пустой/битый дамп — единицы КБ. 1 MiB надёжно отделяет валидный
# дамп от мусора, не давая ложных срабатываний при будущем росте/усадке БД.
MIN_BACKUP_BYTES=$((1024 * 1024))

DATE=$(date +%Y-%m-%d_%H%M)
DAY_OF_WEEK=$(date +%u)  # 7 = воскресенье

DAILY_DIR="$BACKUP_DIR/daily"
WEEKLY_DIR="$BACKUP_DIR/weekly"
mkdir -p "$DAILY_DIR" "$WEEKLY_DIR"

BACKUP_FILE="$DAILY_DIR/${DB_NAME}_${DATE}.dump.gz"

# --- Telegram-алерт (читает бот/админов из .env прода) ------------------------
# ADMIN_TELEGRAM_IDS хранится как JSON-массив: [123456789,987654321] — чистим скобки.
send_tg_alert() {
    local msg="$1"
    [ -f "$ENV_FILE" ] || { echo "[$(date)] WARN: $ENV_FILE нет — алерт не отправлен"; return 0; }

    local token admin_raw
    token=$(grep -E '^TELEGRAM_BOT_TOKEN=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'" | tr -d '[:space:]')
    admin_raw=$(grep -E '^ADMIN_TELEGRAM_IDS=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '[]" '"'")

    if [ -z "$token" ] || [ -z "$admin_raw" ]; then
        echo "[$(date)] WARN: нет TELEGRAM_BOT_TOKEN/ADMIN_TELEGRAM_IDS в .env — алерт не отправлен"
        return 0
    fi

    # Реальные \n; curl --data-urlencode сам закодирует их в %0A (не вставлять %0A руками —
    # --data-urlencode перекодировал бы '%' в '%25').
    local host_name text
    host_name=$(hostname)
    text=$(printf '🚨 <b>OraclePoly: БЭКАП БД УПАЛ</b>\n\n%s\n\nХост: %s\nВремя: %s' \
        "$msg" "$host_name" "$(date '+%F %T')")

    local IFS=','
    for chat_id in $admin_raw; do
        [ -n "$chat_id" ] || continue
        curl -fsS --max-time 15 -X POST \
            "https://api.telegram.org/bot${token}/sendMessage" \
            -d "chat_id=${chat_id}" \
            --data-urlencode "text=${text}" \
            -d "parse_mode=HTML" >/dev/null 2>&1 \
            || echo "[$(date)] WARN: не смог отправить TG-алерт на ${chat_id}"
    done
}

# Любая необработанная ошибка (set -e) → алерт + выход с кодом 1.
on_error() {
    local exit_code=$?
    echo "[$(date)] ERROR: бэкап прерван (exit=${exit_code}) на строке ${BASH_LINENO[0]}"
    send_tg_alert "Скрипт прерван с кодом <code>${exit_code}</code> на строке ${BASH_LINENO[0]}. См. /var/log/oracle/backup.log"
    exit "$exit_code"
}
trap on_error ERR

# --- Дамп ---------------------------------------------------------------------
echo "[$(date)] Начинаю бэкап $DB_NAME (формат custom)..."

# pg_dump --format=custom | gzip. pipefail ловит падение pg_dump в середине пайпа,
# но дополнительно проверяем PIPESTATUS И размер: pg_dump может вернуть 0 при
# оборванном соединении, оставив усечённый дамп.
set +e
docker exec "$PG_CONTAINER" pg_dump -U "$DB_USER" -d "$DB_NAME" \
    --format=custom --no-owner --no-acl | gzip > "$BACKUP_FILE"
pipe_status=("${PIPESTATUS[@]}")
set -e

dump_rc="${pipe_status[0]}"
gzip_rc="${pipe_status[1]}"

if [ "$dump_rc" -ne 0 ] || [ "$gzip_rc" -ne 0 ]; then
    echo "[$(date)] ERROR: pg_dump rc=${dump_rc}, gzip rc=${gzip_rc}"
    send_tg_alert "pg_dump завершился с ошибкой (pg_dump rc=<code>${dump_rc}</code>, gzip rc=<code>${gzip_rc}</code>). Дамп НЕ создан."
    rm -f "$BACKUP_FILE"
    exit 1
fi

# Проверка: дамп не пустой / не усечённый.
BACKUP_BYTES=$(stat -c %s "$BACKUP_FILE" 2>/dev/null || echo 0)
if [ "$BACKUP_BYTES" -lt "$MIN_BACKUP_BYTES" ]; then
    echo "[$(date)] ERROR: дамп подозрительно мал: ${BACKUP_BYTES} байт (<${MIN_BACKUP_BYTES})"
    send_tg_alert "Дамп подозрительно мал: <code>${BACKUP_BYTES}</code> байт (порог <code>${MIN_BACKUP_BYTES}</code>). Вероятно битый — НЕ ротируем."
    rm -f "$BACKUP_FILE"
    exit 1
fi

# Проверка целостности gzip-потока (быстро, без распаковки на диск).
if ! gzip -t "$BACKUP_FILE" 2>/dev/null; then
    echo "[$(date)] ERROR: gzip -t не прошёл — поток повреждён"
    send_tg_alert "gzip-поток дампа повреждён (<code>gzip -t</code> failed). Дамп НЕ годен."
    rm -f "$BACKUP_FILE"
    exit 1
fi

BACKUP_SIZE=$(du -h "$BACKUP_FILE" | cut -f1)
echo "[$(date)] Бэкап создан и проверен: $BACKUP_FILE ($BACKUP_SIZE, ${BACKUP_BYTES} байт)"

# --- Еженедельная копия (воскресенье) -----------------------------------------
WEEKLY_FILE=""
if [ "$DAY_OF_WEEK" = "7" ]; then
    WEEKLY_FILE="$WEEKLY_DIR/${DB_NAME}_weekly_${DATE}.dump.gz"
    cp "$BACKUP_FILE" "$WEEKLY_FILE"
    echo "[$(date)] Еженедельный бэкап сохранён: $WEEKLY_FILE"
fi

# --- Ротация ------------------------------------------------------------------
# Удалить все, кроме N новейших по mtime. find+sort вместо `ls | xargs` —
# ротация удаляет бэкапы, поэтому защищаемся от спецсимволов и пустого глоба
# (NUL-разделители, без word-splitting).
rotate_keep() {
    local dir="$1" glob="$2" keep="$3"
    # mtime<TAB>path, по убыванию времени, отрезаем первые $keep, остальное удаляем.
    find "$dir" -maxdepth 1 -type f -name "$glob" -printf '%T@\t%p\0' 2>/dev/null \
        | sort -zrn \
        | tail -z -n "+$((keep + 1))" \
        | cut -z -f2- \
        | xargs -0 -r rm -f --
    local left
    left=$(find "$dir" -maxdepth 1 -type f -name "$glob" 2>/dev/null | wc -l)
    echo "[$(date)] Ротация $glob в $dir: оставлено ${left} файлов"
}

rotate_keep "$DAILY_DIR" "${DB_NAME}_*.dump.gz" 7
rotate_keep "$WEEKLY_DIR" "${DB_NAME}_weekly_*.dump.gz" 4

# --- Offsite: Cloudflare R2 ---------------------------------------------------
if command -v rclone &>/dev/null && rclone listremotes 2>/dev/null | grep -q '^r2:'; then
    echo "[$(date)] Загружаю бэкап в R2..."
    if rclone copy "$BACKUP_FILE" "r2:oracle/backups/daily/" --quiet; then
        echo "[$(date)] Бэкап загружен в R2: oracle/backups/daily/$(basename "$BACKUP_FILE")"
    else
        echo "[$(date)] ERROR: rclone copy (daily) упал"
        send_tg_alert "Локальный дамп ОК, но offsite-загрузка в R2 упала (daily). Бэкап есть только на диске сервера."
    fi
    if [ -n "$WEEKLY_FILE" ]; then
        rclone copy "$WEEKLY_FILE" "r2:oracle/backups/weekly/" --quiet \
            || send_tg_alert "Offsite-загрузка weekly в R2 упала. Локальная копия есть."
    fi
else
    echo "[$(date)] WARN: rclone не установлен/не настроен (remote r2:) — бэкап только локальный, БЕЗ offsite"
fi

echo "[$(date)] Бэкап завершён успешно: $BACKUP_FILE"

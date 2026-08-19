# Backup & Restore — PostgreSQL (full DB + per-table)

**Production environment**: VPS (`<YOUR_SERVER_IP>`), Docker `oraclepoly-postgres`, PostgreSQL 16.13.
**Last verified**: 2026-05-29 full-DB restore test passed (60 tables, 131568 markets / 49132 detector_signals / 16336 transactions — exact match).

Два независимых сценария:
- **Full-DB automated backup** (этот раздел) — ежедневный `pg_dump --format=custom` всей БД, cron 04:00 UTC. Защита от потери диска/БД целиком.
- **Per-table recovery** (разделы ниже) — точечное восстановление одной таблицы (`detector_signals`) после случайного DELETE/UPDATE.

## When to use this runbook

- **Disaster recovery** — БД/диск потеряны, восстановить всё из последнего дампа (Full-DB ниже)
- Pre-migration safety net (например ML re-fit, schema changes, bulk DELETE)
- Recovery after accidental DELETE/UPDATE (per-table strategies)
- Periodic backup verification
- Cross-environment data copy (prod → staging)

---

## Full-DB automated backup (daily)

**Скрипт**: [`scripts/backup_postgres.sh`](../../scripts/backup_postgres.sh).
**Cron**: `0 4 * * *` UTC (см. `crontab.conf`, секция Backups), через `flock -xn /tmp/oracle-backup.lock`.
**Логи**: `/var/log/oracle/backup.log`.

Что делает:
- `pg_dump --format=custom --no-owner --no-acl | gzip` → `/root/backups/daily/oraclepoly_<YYYY-MM-DD_HHMM>.dump.gz`
- Проверки: код возврата pg_dump+gzip (`PIPESTATUS`), размер > 1 MiB, `gzip -t` целостность потока. Любой провал → битый дамп удаляется + **Telegram-алерт** админам (бот из `.env`).
- Ротация: **7 ежедневных** + **4 еженедельных** (воскресный дамп копируется в `weekly/`).
- Offsite: `rclone copy` в Cloudflare R2 (`r2:oracle/backups/`), если rclone установлен и remote `r2:` настроен. Иначе — WARN в лог (бэкап только локальный).

Текущий размер: БД ~2.3 GB → сжатый дамп ~388 MB, время ~40 сек.

### Ручной запуск бэкапа (вне cron)

```bash
ssh <your-server> 'cd /root/oracle && bash scripts/backup_postgres.sh'
```

### Full-DB restore — disaster recovery (восстановить ВСЮ БД)

**⚠️ DESTRUCTIVE для целевой БД.** Сначала всегда восстанавливай в отдельную БД и проверяй (см. «Тест восстановления» ниже), и только осознанно — поверх рабочей.

```bash
ssh <your-server> '
  set -e
  DUMP=$(ls -t /root/backups/daily/oraclepoly_*.dump.gz | head -1)   # или конкретный файл
  echo "Restore from: $DUMP"

  # 1. Остановить писателей (cron + воркеры), чтобы restore не конфликтовал с вставками
  docker exec oraclepoly-backend bash -c "crontab -l > /tmp/crontab.backup 2>/dev/null || true; crontab -r 2>/dev/null || true"

  # 2. Пересоздать целевую БД (DROP + CREATE) — чистое состояние, без остатков
  docker exec oraclepoly-postgres psql -U oraclepoly -d postgres -c "DROP DATABASE IF EXISTS oraclepoly WITH (FORCE);"
  docker exec oraclepoly-postgres psql -U oraclepoly -d postgres -c "CREATE DATABASE oraclepoly OWNER oraclepoly;"

  # 3. Восстановить custom-дамп через pg_restore (gunzip на хосте → stdin контейнера)
  #    -j 4 = параллельное восстановление (custom-формат позволяет; ускоряет на больших БД)
  gunzip -c "$DUMP" | docker exec -i oraclepoly-postgres \
    pg_restore -U oraclepoly -d oraclepoly --no-owner --no-acl -j 4 --exit-on-error

  # 4. Проверить и вернуть cron на backend (host-crontab с backup-задачей восстанавливается отдельно — см. /root/backups/crontab_*.txt)
  docker exec oraclepoly-backend bash -c "cat /tmp/crontab.backup | crontab - 2>/dev/null || true"

  # 5. Перезапустить сервисы, читающие схему на старте
  docker compose -f /root/oracle/docker-compose.yml restart backend worker risk-daemon
'
```

> **Гитчи full-DB restore:**
> - `DROP DATABASE ... WITH (FORCE)` (PG 13+) выселяет активные коннекты — иначе DROP висит на открытых сессиях backend/worker.
> - `pg_restore -j N` нельзя совмещать с `--single-transaction`; при `-j` ошибка в середине оставит частичное состояние, поэтому `--exit-on-error` + восстановление в свежесозданную БД.
> - Host-crontab (где backup-задача) и backend-crontab — **разные** crontab'ы. Шаг 1 трогает только backend-crontab внутри контейнера; host-crontab не затрагивается. Бэкап host-crontab лежит в `/root/backups/crontab_*.txt`.

### Восстановить ОДНУ таблицу из full-DB дампа

custom-формат позволяет вытащить отдельную таблицу без полного restore:

```bash
ssh <your-server> '
  DUMP=$(ls -t /root/backups/daily/oraclepoly_*.dump.gz | head -1)
  # В staging-таблицу, чтобы не затирать текущую (как в Strategy 2 ниже)
  gunzip -c "$DUMP" | docker exec -i oraclepoly-postgres \
    pg_restore -U oraclepoly -d oraclepoly --no-owner --data-only -t markets --exit-on-error
'
```

### Тест восстановления (проверка годности дампа — делать периодически)

Восстановление в **отдельную** БД, без риска для прода. Пройдено 2026-05-29:

```bash
ssh <your-server> '
  set -e
  DUMP=$(ls -t /root/backups/daily/oraclepoly_*.dump.gz | head -1)
  docker exec oraclepoly-postgres psql -U oraclepoly -d postgres -c "DROP DATABASE IF EXISTS oraclepoly_restore_test;"
  docker exec oraclepoly-postgres psql -U oraclepoly -d postgres -c "CREATE DATABASE oraclepoly_restore_test OWNER oraclepoly;"
  gunzip -c "$DUMP" | docker exec -i oraclepoly-postgres \
    pg_restore -U oraclepoly -d oraclepoly_restore_test --no-owner --no-acl --exit-on-error

  # Сверить с оригиналом
  echo "--- restored ---"
  docker exec oraclepoly-postgres psql -U oraclepoly -d oraclepoly_restore_test -t -c \
    "SELECT '"'"'tables='"'"'||count(*) FROM information_schema.tables WHERE table_schema='"'"'public'"'"';"
  docker exec oraclepoly-postgres psql -U oraclepoly -d oraclepoly_restore_test -t -c \
    "SELECT '"'"'markets='"'"'||count(*) FROM markets UNION ALL SELECT '"'"'detector_signals='"'"'||count(*) FROM detector_signals;"

  # Убрать тестовую БД
  docker exec oraclepoly-postgres psql -U oraclepoly -d postgres -c "DROP DATABASE oraclepoly_restore_test;"
'
```

Ожидаемо: число таблиц и row-counts совпадают с продом (на 2026-05-29: 60 таблиц, markets=131568, detector_signals=49132).

### Offsite (Cloudflare R2)

См. секцию «Offsite setup» в конце документа. Без offsite БД и бэкапы лежат на одном диске `/` — потеря диска = потеря обоих.

---

## Per-table recovery (detector_signals и др.)

Точечное восстановление одной таблицы. Применимо к dump'у одной таблицы ИЛИ к full-DB дампу (pg_restore `-t` работает с обоими).

## Pre-flight: create backup

Standard pg_dump for `detector_signals` (5 MB compressed, ~30s):

```bash
ssh <your-server> '
  mkdir -p /root/backups &&
  docker exec -i oraclepoly-postgres pg_dump -U oraclepoly -d oraclepoly \
    -t detector_signals -F c -f /tmp/detector_signals_$(date +%Y-%m-%d_%H%M).dump &&
  docker cp oraclepoly-postgres:/tmp/$(ssh <your-server> "docker exec oraclepoly-postgres ls /tmp | grep detector_signals | tail -1") /root/backups/ &&
  ls -lh /root/backups/
'
```

Or one-liner для single table:

```bash
ssh <your-server> '
  TS=$(date +%Y-%m-%d_%H%M)
  docker exec oraclepoly-postgres pg_dump -U oraclepoly -d oraclepoly \
    -t detector_signals -F c -f /tmp/ds_${TS}.dump
  docker cp oraclepoly-postgres:/tmp/ds_${TS}.dump /root/backups/
  ls -lh /root/backups/ds_${TS}.dump
'
```

## Restore strategies

### Strategy 1: Atomic table replace (DESTRUCTIVE)

**Use when**: complete corruption, willing to lose any recent inserts after backup.
**Risk**: high — wipes current table including new rows added since backup.

```bash
ssh <your-server> '
  # 1. Pause writers (stop crons that insert)
  docker exec oraclepoly-backend bash -c "crontab -l > /tmp/crontab.backup && crontab -r"

  # 2. Copy dump into container
  docker cp /root/backups/detector_signals_2026-05-14.dump oraclepoly-postgres:/tmp/restore.dump

  # 3. TRUNCATE + restore atomic
  docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly -c "
    BEGIN;
    TRUNCATE detector_signals;
    COMMIT;
  "
  docker exec -i oraclepoly-postgres pg_restore --data-only --no-owner \
    -d oraclepoly -U oraclepoly -t detector_signals /tmp/restore.dump

  # 4. Resume crons
  docker exec oraclepoly-backend bash -c "cat /tmp/crontab.backup | crontab -"
'
```

### Strategy 2: Non-destructive merge (RECOMMENDED для recovery)

**Use when**: lost some rows but current data is correct, want to restore only missing rows.
**Risk**: low — keeps current data, fills gaps.
**Verified**: 2026-05-14 incident, 32663 rows restored without disrupting current state.

```bash
ssh <your-server> '
  # 1. Copy dump → container
  docker cp /root/backups/detector_signals_pre_migrate_2026-05-14.dump oraclepoly-postgres:/tmp/restore.dump

  # 2. Create staging table identical to detector_signals
  docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly -c "
    CREATE TABLE IF NOT EXISTS detector_signals_restore (LIKE detector_signals INCLUDING ALL)
  "

  # 3. Convert dump to SQL + rename target table inline (avoid pg_restore -t which forces original name)
  docker exec -i oraclepoly-postgres bash -c "
    pg_restore --data-only --no-owner --no-privileges -t detector_signals -f /tmp/restore.sql /tmp/restore.dump
    sed -i \"s/COPY public.detector_signals /COPY public.detector_signals_restore /\" /tmp/restore.sql
  "

  # 4. Load into staging
  docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly -f /tmp/restore.sql

  # 5. INSERT missing rows back into target (ON CONFLICT skips existing IDs)
  docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly -c "
    INSERT INTO detector_signals
    SELECT * FROM detector_signals_restore
    ON CONFLICT (id) DO NOTHING
  "

  # 6. Cleanup
  docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly -c "DROP TABLE detector_signals_restore"
  docker exec oraclepoly-postgres rm /tmp/restore.dump /tmp/restore.sql
'
```

Expected output: `INSERT 0 <N>` where N = rows restored.

### Strategy 3: Selective restore by predicate

**Use when**: need to restore только конкретные rows (e.g., specific date range).

```bash
# Same as Strategy 2 steps 1-4, but step 5 filtered:
docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly -c "
  INSERT INTO detector_signals
  SELECT * FROM detector_signals_restore
  WHERE source = 'hf_backfill'
    AND created_at < TIMESTAMPTZ '2026-05-14 16:00'
  ON CONFLICT (id) DO NOTHING
"
```

## Post-restore verification

Always verify counts + integrity:

```bash
ssh <your-server> '
  docker exec -i oraclepoly-postgres psql -U oraclepoly -d oraclepoly -c "
    SELECT source, COUNT(*) FROM detector_signals GROUP BY source ORDER BY source;
    SELECT COUNT(*) AS total, COUNT(DISTINCT id) AS unique_ids FROM detector_signals;
  "
'
```

Expected: `total = unique_ids` (no PK violations).

For ML data integrity, re-run the project's own verification suite against the restored DB:

```bash
ssh <your-server> 'docker exec oraclepoly-backend pytest tests/ -q'
```

## Gotchas (learned the hard way)

### `pg_restore -t` only restores в original table name
You **cannot** redirect pg_restore output к different table name. Workaround = export to SQL, sed-rename, then load (Strategy 2 step 3).

### Docker container ran stale Python code
Если делаешь `docker cp scripts/ container:/app/` and then `docker exec container python ...`, container will use **NEW** code on next process spawn (Python re-imports per-process). **BUT** — verify by reading file inside container before relying on it:

```bash
ssh <your-server> 'docker exec oraclepoly-backend python -c "import app; print(app.__file__)"'
```

If you see OLD content → re-copy + retry. Best practice: chain `docker cp` and `docker exec` в одной shell command, не separate ssh calls.

### Permission classifier blocks TRUNCATE
В CCD harness, `TRUNCATE detector_signals` may be permission-denied even if pg_restore is authorized. They are **distinct destructive operations**. Use non-destructive Strategy 2 to avoid this.

### `_restore` staging table holds PostgreSQL identity columns separately
`CREATE TABLE x (LIKE y INCLUDING ALL)` copies indexes including UNIQUE constraints. When you INSERT to target, ON CONFLICT DO NOTHING uses target's PK.

### SSH MaxStartups rate limit
Частые SSH-подключения подряд вызывают `kex_exchange_identification: Connection closed by remote host`. Wait 30-60s. Use `ControlMaster` config to multiplex (настраивается в `~/.ssh/config`).

## Backup retention policy

**Full-DB (automated, см. верх документа)** — `scripts/backup_postgres.sh`, cron 04:00 UTC:

```
/root/backups/
  daily/oraclepoly_<date>.dump.gz          — 7 последних (ротация в скрипте)
  weekly/oraclepoly_weekly_<date>.dump.gz  — 4 последних (воскресные)
  detector_signals_pre_migrate_*.dump      — named migration snapshots (хранить вручную, ротацией не трогаются)
  crontab_*.txt                            — снимки crontab перед изменениями
```

> Single-table weekly cron, который раньше предлагался здесь как «not yet implemented», **больше не нужен**: ежедневный full-DB дамп включает `detector_signals` и все прочие таблицы.

## Offsite setup (Cloudflare R2)

**Зачем именно R2** (решение 2026-05-29): главная угроза — потеря сервера/DC целиком (как 2026-05-11, когда старый VPS удалён без возврата). Блочное хранилище того же провайдера — в том же DC, не защищает от этого. R2 — другой провайдер/география, free до 10 GB (дампы 388 MB × (7+4) влезают), и скрипт уже под него написан. Cloudflare-аккаунт уже есть (под Workers AI / R2 archive).

**Статус**: rclone **установлен** на проде (`apt install rclone`, v1.60.1, `/usr/bin/rclone`). Осталось два шага — `rclone config` с кредами + bucket `oracle`. Пока remote `r2:` не настроен, скрипт делает локальный бэкап и пишет WARN; как только remote появится — следующий бэкап зальёт дамп в R2 без правки кода.

### Шаг 1. `rclone config` (интерактивно, креды НЕ через git/чат)

Креды: Cloudflare Dashboard → R2 → **Manage R2 API Tokens** → Create (Object Read & Write). Account ID — в R2 overview / endpoint.

```bash
ssh <your-server>
rclone config
```

Ответы мастеру (имя remote **обязательно `r2`** — скрипт ждёт `r2:`):

| Шаг | Ответ |
|---|---|
| `e/n/d/...` | `n` (New remote) |
| `name>` | `r2` |
| `Storage>` | `s3` |
| `provider>` | `Cloudflare` |
| `env_auth>` | `1` (false) |
| `access_key_id>` | *R2 Access Key ID* |
| `secret_access_key>` | *R2 Secret Access Key* |
| `region>` | `auto` (или Enter) |
| `endpoint>` | `https://<ACCOUNT_ID>.r2.cloudflarestorage.com` |
| `acl>` | Enter / `private` |
| `Edit advanced config?` | `n` |
| `Keep this remote?` | `y` |
| меню | `q` |

`rclone config` сам кладёт `/root/.config/rclone/rclone.conf` с правами 600.

### Шаг 2. Bucket + проверка

```bash
ssh <your-server> '
  rclone lsd r2:                          # remote виден, список бакетов
  rclone mkdir r2:oracle                  # bucket (или создать в дашборде R2)
  rclone mkdir r2:oracle/backups/daily
  rclone mkdir r2:oracle/backups/weekly

  # Прогнать бэкап — он сам зальёт свежий дамп в R2
  cd /root/oracle && bash scripts/backup_postgres.sh
  rclone ls r2:oracle/backups/daily       # убедиться, что .dump.gz появился
'
```

После этого ежедневный cron-бэкап будет автоматически дублироваться в R2.

> Имя bucket `oracle` зафиксировано в скрипте (коммит b7cdb4eb). Не путать с `r2_bucket_name=oracle-data-lake` из `app/core/config.py` — это отдельный bucket для archive-данных рантайма, не для бэкапов БД.
> Восстановление из R2: `rclone copy r2:oracle/backups/daily/<file> /root/backups/daily/`, далее обычный restore.

## Related docs


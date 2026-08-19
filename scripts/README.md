# OraclePoly Scripts

## Структура

```
scripts/
├── *.py / *.sh             # ACTIVE — daemons, ops, migrations, bootstrap
├── debug/                  # ad-hoc диагностика (ablation, market diagnose)
├── sql/                    # одноразовые SQL фиксы
└── research/               # one-off аналитика и backtest (см. research/README.md)
```

## ACTIVE — Production daemons

| Script | Purpose | Triggered by |
|---|---|---|
| `cron_runner.py` | Dispatcher для всех 50+ cron-команд (вызывает `app/jobs/*`) | crontab.conf |
| `oracle_worker.py` | Redis queue consumer (whale TX → recalc → notify) | start.sh, docker compose |
| `execution_bot.py` | Execution daemon: queue consumer + monitor | start.sh, docker compose |
| `manual_sync.py` | Manual ops: markets/news/cleanup/recalc для local dev | manual / start.sh testing |

## ACTIVE — Migrations & Ops

| Script | Purpose |
|---|---|
| `run_migration.py` | Apply migrations (state-tracking via `schema_migrations`) |
| `verify_migration.py` | Pre-apply validation |
| `create_migration.sh` | Создать timestamped SQL migration файл |
| `init_db.sh` | Initialize PostgreSQL schema (Docker) |
| `import_data.sh` | Import data dump в локальную БД |
| `backup_postgres.sh` | Backup production DB |
| `psql.sh` | Shortcut для psql на VPS |
| `deploy.sh` | SSH deploy на VPS |
| `server_setup.sh` | Initial Ubuntu 22+ setup |
| `setup_ssl.sh` | Let's Encrypt configuration |
| `setup_swap.sh` | Add 2GB swap (prevents server freezes) |
| `vulture_whitelist.py` | Suppression list для vulture dead-code detection |
| `generate_feature_map.py` | Регенерация `docs/feature_map.json` |

## ACTIVE — One-shot bootstrap

Скрипты, которые запускаются однократно при крупных изменениях. **Не на кроне.**

| Script | When to run |
|---|---|
| `feature_store_bulk_load_hf.py` | После добавления feature_store — bulk-load из HF parquet (`/mnt/data/polymarket_dataset/full_hf_enriched.parquet`) |

## DEBUG (ad-hoc диагностика)

| Script | Purpose |
|---|---|
| `debug/ablation_study.py` | Offline ablation для Brier Skill Score |
| `debug/diagnose_markets.py` | Проверка pipeline Gamma API → DB → presence |

## SQL one-shots

`sql/revive_hf_killed_strategies.sql` — fix для 7 стратегий, убитых багом в `validate_dry_strategies.py` (2026-04-20).

---

## RESEARCH (one-off аналитика)

См. [scripts/research/README.md](research/README.md). Backtest, strategy discovery, oracle ablation, HF bias verification, calibration research. **Не на кроне**, запускаются вручную для гипотез.

---

## Когда использовать что?

```
Production cron job? → app/jobs/<name>.py + cron_runner.py
Manual one-shot ops? → scripts/<utility>.py
First-time bootstrap? → scripts/feature_store_*.py
Research / backtest? → scripts/research/<name>.py
Diagnostic ad-hoc? → scripts/debug/<name>.py
```

## Что не делать

- **Не добавлять research-скрипты в кронtab.** Кроны — только через `app/jobs/` + `cron_runner.py`.
- **Не звать `scripts/research/*` из app/.** Если research-результат нужен в проде — фиксировать как константу или в data file (см. `app/core/scoring.py` где константы откалиброваны через `research/historical_category_analysis.py`).
- **Перед удалением скрипта:** проверить `grep -rn '<name>' app/ scripts/ tests/ docs/` и `crontab -l`.

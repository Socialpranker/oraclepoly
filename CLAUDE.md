# CLAUDE.md — OraclePoly

Self-trading bot для Polymarket prediction markets.
Backend: Python 3.11 + FastAPI. Frontend: React 19 + Vite + Tailwind 4. DB: PostgreSQL 16 (asyncpg).

## Commands

```bash
# Full local stack
./start.sh              # backend (8001) + worker (Redis) + frontend (5173)

# Backend only
source .venv/bin/activate && python -m uvicorn app.main:app --reload --port 8001

# Frontend only
cd client && npm run dev

# Docker (prod-like)
docker compose up -d --build
```

## Verification

После каждого изменения — в таком порядке:
1. `pytest tests/ -v --tb=short`
2. `ruff check app/ && cd client && npm run lint`
3. `python -m vulture app/ scripts/vulture_whitelist.py --min-confidence 80`

Перед коммитом: `pre-commit run --all-files`

## Architecture

```
app/
  api/          # REST endpoints
  core/         # config, cache, auth, scoring, logging
  handlers/     # oracle_calculator, whale_watcher, smart_money/
  tasks/        # Redis queue: transactions, notifications, resolve_outcomes
  jobs/         # 89 cron tasks (via scripts/cron_runner.py)
  services/     # DB, external integrations
  repositories/ # Data access layer
client/src/     # React 19 + Vite (pages/, components/, hooks/, lib/, types/)
scripts/        # Deploy, migrations, monitoring
  research/     # One-off скрипты для боевого сервера
migrations/     # SQL (timestamp prefix, state-tracked)
docs/           # архитектура, деплой, runbooks, спеки детекторов
```

## Key Patterns

- **Settings**: `app/core/config.py` — pydantic-settings, `@lru_cache`. Всегда через `get_settings()`.
- **asyncpg**: параметры `$1, $2` (не `%s`). UUID/datetime/Decimal → `_serialize_value()` в `database.py`.
- **Migrations**: `create_migration.sh <name>` (timestamp prefix), state-tracked в `schema_migrations`. Применять через `run_migration.py`.
- **Proxy**: `POLYMARKET_PROXY` проксирует ТОЛЬКО POST-ордера CLOB — селективный монопатч `install_clob_proxy_transport()` в `app/services/wallet_resolver.py` (геофенс IP сервера); все read-запросы к Polymarket — напрямую. Никогда `HTTPS_PROXY`/`HTTP_PROXY` в `.env` — Docker подхватит как системный и сожжёт весь трафик (инцидент 2026-04-05: 1 GB за день).
- **Simulation SSoT**: параметры в `simulationDefaults.ts` (frontend) + `simulation_defaults.py` (backend). Не хардкодить в компонентах или jobs.
- **ML Pipeline V2**: Detector → L1 Beta → Layer A (`ml_event_selector`) → Layer B (`ml_market_picker`) → EV sizer. Cutover (Stage 5) = **только human decision**, никогда автоматически.
- **БД**: PostgreSQL 16 через asyncpg. Локально поднимается `docker compose up -d postgres redis`.
- **detector_signals ID-конвенция**: `market_id` = `markets.polymarket_id` (0x), `event_id` = `events.polymarket_id` — эталон `d38_scan.py`. НИКОГДА внутренние UUID `markets.id`/`events.id`: resolve-JOIN и event-аналитика джойнят по polymarket-id. 100% EXPIRED у детектора = маркер этого бага (инцидент 2026-07-09: M2/D45, PR #683/#685).
- **Detector lifecycle (4 stages)**: 🧪 Backtest → 👁 Shadow → 💸 Virtual → 🔴 Live. Каждый stage имеет один источник правды. Promote только manual через TG. См. [docs/detector_lifecycle.md](docs/detector_lifecycle.md). Backtest projections (`docs/backtests/{detector}.md`) ≠ production performance (filter mismatch — обязательно `honest_haircut_factor`).

## Code Style

- **Python**: Ruff (line-length 120, rules E/F/I/W/UP). Absolute imports: `from app.core.config import get_settings`.
- **TypeScript**: ESLint react-hooks + react-refresh.
- **Commits**: Feature branches only — `no-commit-to-branch` hook блокирует прямые коммиты в `main`.

## Gotchas

- `uvloop.install()` перед всеми импортами в `app/main.py` — E402 suppressed intentionally.
- CI: `SUPABASE_KEY` env var (legacy) — `config.py` ожидает `DATABASE_URL`, fallback работает.
- Миграции: нумерация 001-007, 101-187, далее timestamps. Не нарушать порядок.
- `scripts/research/*.py` — one-off аналитические скрипты, писались под боевой сервер: ожидают полную БД и docker-окружение, на пустой локальной базе бесполезны.

## Environment

Required env vars: `DATABASE_URL`, `OPENAI_API_KEY`, `REDIS_URL`, `TELEGRAM_BOT_TOKEN`, `ADMIN_TELEGRAM_IDS`, `ADMIN_API_KEY`, `POLYMARKET_API_KEY`. См. `.env.example`.

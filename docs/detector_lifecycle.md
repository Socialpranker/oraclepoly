# Detector Lifecycle — 4 stages

> Single source of truth для архитектуры детекторов в OraclePoly.
> Заменяет старый "3 stages" подход. Обновлён 2026-05-02.

Каждый детектор проходит **4 явные стадии**, в каждой — **одна** definition,
**один** источник данных, **один** UI screen. См. также:
- `app/api/bot.py` для API endpoints
- `client/src/pages/BotDashboard.tsx` для UI navigation
- `app/services/backtest_metadata.py` для backtest parser

## Таблица 4 stages

| # | Stage | Что | Где данные | UI место | Money |
|---|---|---|---|---|---|
| 1 | 🧪 **Backtest** | гипотеза проверена на исторических 821 днях canonical HF | `/mnt/data/canonical/*.parquet` (VPS) + `docs/backtests/{detector}.md` | `/bot?stage=backtest` (BacktestTerminal) | $0 |
| 2 | 👁 **Shadow** | детектор эмитит signals на live данных, outcome ждёт резолва | `detector_signals` (postgres) | `/bot?stage=shadow` (StatusTerminal) | $0 |
| 3 | 💸 **Virtual** | shadow signals прошли через ML+sizing+safety, "размещены" виртуально | `execution_orders` где `execution_mode='virtual'` | `/bot?stage=virtual` (VirtualTerminal) | $0 |
| 4 | 🔴 **Live** | реальные ордера в Polymarket CLOB | `execution_orders` где `execution_mode IN ('maker','taker')` | `/bot?stage=live` (LiveTerminal) | реальные $$$ |

## Storage mapping (Postgres)

| Table | Stage data | Notes |
|---|---|---|
| `detector_state.stage` | `'shadow'` / `'virtual'` / `'live'` / `'paused'` | стадия конкретного детектора |
| `detector_signals` | stage 2 (shadow) — все signals здесь | 1 row = 1 signal |
| `execution_orders.execution_mode` | stage 3 (`'virtual'`) vs stage 4 (`'maker'` / `'taker'`) | определяет 3 vs 4 |
| `execution_orders.status` | per stage flow | см. ниже |

### Status flow per stage

- **Stage 3 (Virtual):** `virtual_placed → virtual_filled → virtual_settled` (или `rejected`)
- **Stage 4 (Live):** `intent → placed → filled → settled` (или `rejected`)
- **Legacy:** `dry_run` (Pipeline V1) — мигрирован в `virtual_placed` миграцией `20260502160410_dry_run_cleanup_and_virtual_stage.sql`

## Два независимых рубильника: эмиссия и деньги

**`detector_state.stage` НЕ управляет эмиссией сигналов.** Это by design, не баг.
Детектор в `paused` продолжает писать в `detector_signals` — и должен.

| Рубильник | Что гасит | Источник правды | Как выключить |
|---|---|---|---|
| **cron-строка** | эмиссию сигналов | `crontab.conf` на проде | закомментировать строку `dXX-scan` |
| **`detector_state.stage`** | доступ к деньгам | таблица `detector_state` | `stage='paused'` / `'shadow'` |

Почему разделены: детектор на паузе накапливает shadow-статистику, по которой
потом принимается решение о реабилитации. Если бы `paused` глушил и эмиссию,
после паузы не на чем было бы оценивать детектор — пришлось бы включать вслепую.

**Следствие, которое регулярно принимают за утечку денег:** детектор в `paused`
с большим отрицательным `SUM(realized_roi)` по `detector_signals` **денег не теряет**.
Он не проходит гейт (см. ниже) и до `execution_orders` не доходит. Отрицательная
сумма там — бумажная статистика, а не P&L. Портфельный P&L считается по
`execution_orders`, а не по `detector_signals` — см. §Honest $/day.

### Где стоит денежный гейт

1. **`app/services/detector_signal_service.py`** — `_SIGNAL_SELECT_BASE_SQL`:
   `JOIN detector_state dst ON dst.detector = ds.detector AND dst.stage IN ('virtual','live')`.
   INNER JOIN: `shadow`, `paused` и незарегистрированные детекторы отсекаются
   до входа в `v2_execution`. Общая основа для обоих путей выборки (cron-тик и
   демон по id) — фильтруют одинаково.
2. **`app/services/execution_safety.py`** — `if state.stage != "live"` →
   `detector_stage:{stage}` перед placement реального ордера.

Отброшенное на шаге 1 считается телеметрией `lifecycle_dropped_signals`
(`_DROPPED_SIGNALS_TELEMETRY_SQL`) — чтобы shadow-поток не был чёрной дырой.

### Как реально остановить детектор

- **Остановить деньги, оставить статистику** → `stage='paused'` (cron не трогать).
- **Остановить всё, включая эмиссию** → закомментировать cron-строку **и** выставить
  `stage='paused'`. Только `stage` — сигналы продолжат идти.
- **Убрать из системы** → плюс удалить строку из `detector_state` (historical
  `detector_signals` остаются; `calibration_bins_refit` фильтрует их через INNER JOIN).

`detector_slo_monitor` берёт источником правды именно `crontab.conf`, а не БД —
иначе он ругался бы на cut-детекторы, у которых строка в `detector_state` осталась.

## Stage transitions (promotion criteria)

Detector переходит из stage в stage **только manual decision юзера** через TG callback `promote:{detector}`. Auto-promote запрещён by design (за safety).

```
🧪 Backtest                 👁 Shadow             💸 Virtual           🔴 Live
   ↓ (если PASS verdict)      ↓ (после 7-14d)      ↓ (после 14-28d)    END
   detector module + scan    promote:detector     promote:detector
   migration register stage  Shadow → Virtual     Virtual → Live
   (initial stage = shadow)
```

### Promotion criteria — Shadow → Virtual

- n_signals (stage 2) ≥ 30 events за 7d shadow window
- WR в каждом из top-3 BUCKET_WR keys matches canonical ±5pp
- `docs/backtests/{detector}.md` имеет `verdict: PASS`

### Promotion criteria — Virtual → Live

- virtual_pnl_usd_7d > $0 net of fees+spreads
- fill_rate (`virtual_filled / virtual_placed`) ≥ 50%
- adversarial mirror check pass (если применимо к hypothesis)
- 14-28d virtual data accumulated
- `EXECUTION_ENABLED=true` в .env проды
- Manual user decision через TG (с warning о реальных $$$)

## Что считается "тем же" между stages

- **detector_signals (shadow + virtual + live):** signal эмитит детектор всегда независимо от stage. Stage только определяет что с signal делать дальше.
- **ML pipeline (calibrate → size → safety):** работает на virtual И live одинаково.
- **⚠️ Stage 3 vs 4 НЕ идентичны:** live размещает реальный CLOB-ордер, но live-путь пока BUY-only без exit-механизма — TP/SL (position_manager) действует только в virtual. Promote-метрики D3 получены с выходами (early exits), hold-to-resolution в live их не воспроизводит. Live exit path — Phase 3 плана `docs/superpowers/plans/2026-07-02-audit-fixes-phase3.md` (за флагом, до гейта ≥10 resolved live-ставок с PnL≥0). См. research/design-audit-2026-07-02.md топ-1.

## Что НЕ считается "тем же"

- **Backtest filter (`lifetime_volume_usd >= $5K`) ≠ production filter (`volume_24h >= $5K`).** Поле `lifetime_volume_usd` существует только в canonical parquet. Production scan фильтрует по `volume_24h` (postgres schema). Это даёт **разные populations**: backtest sample может быть в 5-10× шире чем production sample. См. `docs/backtests/D7b_no_favorite_fade.md` (`honest_haircut_reason`).
- **Shadow population ≠ Virtual population.** Shadow signals идут от ВСЕХ детекторов в БД. Virtual — только от детекторов с `stage IN ('virtual', 'live')` (фильтр в `app/services/detector_signal_service.py`). Промоут детектора в `virtual` явно открывает execution pipeline для него.

## Honest $/day calculation

Когда видишь "$X/day daily PnL" — обязательно знай источник:

| Метрика | Источник | Когда честно |
|---|---|---|
| `daily_realistic_usd_at_5k` (canonical) | backtest на canonical filter | теоретический максимум |
| `honest_daily_usd_at_5k` (canonical × haircut) | backtest × haircut_factor | оптимистичная оценка production |
| `virtual_pnl_usd_7d / 7` (production) | реальный virtual exec | actual paper performance |
| `live_pnl_usd_7d / 7` (production) | реальный live exec | actual real performance |

> ⚠️ **Canonical-бэктест = DIRECTIONAL-ONLY research tool, НЕ источник правды для promote (2026-06-15).**
> Бэктест СИСТЕМАТИЧЕСКИ переоценивает vs prod из-за volume-proxy (`lifetime_volume_usd` ≠ `volume_24h`,
> раздувает population 5-10×) + survivorship (только resolved markets). Подтверждённый разрыв:
> D5 canonical +34% → prod **−20.8%**; D31 +36% → **−56.3%**; D44 Wilson 0.894 KEEPER → prod KILL.
> **НЕ использовать canonical-числа в решении о promote.** Promote-gate (`app/services/promote_gates.py`)
> уже live-only (G1=sum_roi_net>0 & Sortino>1 & Wilson WR, G2=Wilson fill-rate — без canonical).
> Свежий forward-only термометр по живому потоку: cron `detectors-honest-audit-report` (вс 05:00,
> Wilson-style per-event dedup, окно 30д) + one-off `scripts/research/detectors_honest_audit.py`.
> Контекст: `memory/decisions/strategy_edge_not_data_2026-06-15.md`.

**В UI BacktestTerminal и DetectorLifecycleCard показывают `honest_*` версию.**

Haircut factor для каждого детектора задан в `docs/backtests/{detector}.md` frontmatter (`honest_haircut_factor`). Типичные значения:
- D7b: 0.4 (filter mismatch in 5-10×)
- D10b: 0.5 (filter mismatch + thin-book risk)

## Где читать code

| Слой | Файл |
|---|---|
| Detector module (BUCKET_WR, gate logic) | `app/detectors/{detector}.py` |
| Scan job (cron, SQL filter, idempotency) | `app/jobs/{detector}_scan.py` |
| ML calibration pipeline | `app/calibration/`, `app/services/detector_signal_service.py` |
| EV-aware sizing | `app/calibration/ev_sizer.py` |
| Execution (virtual + live) | `app/jobs/v2_execution.py` |
| Detector state управление | `app/core/detector_state.py` + TG callback `app/api/telegram.py` |
| Backtest metadata parser | `app/services/backtest_metadata.py` |
| Lifecycle aggregates API | `GET /v2/detectors` (lifecycle field) + `GET /v2/detectors/{detector}/backtest` |
| UI components | `client/src/components/bot/StageBadge.tsx` + `client/src/components/bot/lifecycle/*` |

## Ссылки на artifacts

- Migration: `migrations/20260502160410_dry_run_cleanup_and_virtual_stage.sql`
- Memory: `memory/lifecycle_canonical_2026-05-02.md` (canonical reference)
- Backtest examples: `docs/backtests/D7b_no_favorite_fade.md`, `docs/backtests/D10b_election_ladder.md`
- Audit plan для existing detectors: `docs/detectors_audit_plan_2026-05.md`

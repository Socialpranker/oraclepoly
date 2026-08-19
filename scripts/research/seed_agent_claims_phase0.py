"""Phase 0: seed agent_claims топ-50 материальных клеймов корпуса.

Portfolio Analyst Agent, Phase 0 (docs/specs/portfolio_analyst_agent.md §3.4, §8) —
одноразовая индексация уже добытого знания: топ-50 материальных выводов проекта
(вердикт детектора, параметр политики, риск-оценка, методологическое правило, карта
системы, выбор LLM-модели), извлечённых из `research/fable-research-map-2026-07-19/
06_fable_response.md`, `research/portfolio-analyst-agent-concept-gaps-2026-07/claims.csv`
и auto-memory (`~/.claude/projects/.../memory/MEMORY.md` + topic-файлы). Каждый клейм
прослеживается к конкретному файлу в research/ или конкретной записи в MEMORY.md/
topic-файлам — ничего не изобретено заново (см. docstring каждой записи ниже и
`.superpowers/sdd/task-3-report.md` для сверки происхождения).

Отранжировано по материальности (сколько решений/долларов/часов зависит от клейма):
demote/promote-вердикты детекторов, экономика единственного live-детектора (D3),
регуляторный/платформенный риск (UMA governance attack, MiCA), методологические
правила, которые уже меняли прод-решения (sum-ROI vs portfolio ROI, hold-to-resolution,
bootstrap seed), и архитектурные выборы самого агента (LLM-модель).

=== ВАЖНОЕ ОТЛИЧИЕ от audit_case*.py (Task 4-9) ===

Этот скрипт использует `await init_db()` БЕЗ `dsn` — то есть settings.database_url
(основной DATABASE_URL), а НЕ settings.agent_database_url (read-only роль
oraclepoly_agent_ro из Task 2). Это осознанный, а не забытый выбор: oraclepoly_agent_ro
создана в Task 2 именно как READ-ONLY роль для рантайм-аудитов Task 4-9 (GRANT SELECT
на все таблицы + SELECT/INSERT/UPDATE только на 4 agent_* таблицы для их собственных
audit-записей через agent_runs/agent_claims). У неё НЕТ прав на первичный bulk-сид
agent_claims в смысле "у роли de facto есть INSERT на agent_claims" — но это Phase 0
одноразовый сид справочника знаний, концептуально отдельная операция от рантайм-аудита,
поэтому использует основной DATABASE_URL по аналогии с миграциями. Если в будущем
кто-то "исправит" это на agent_database_url — не делать: read-only-по-духу роль для
рантайм-аудитов не тот же actor, что owner-driven one-off seeding.

Дедуп: `ON CONFLICT (statement) DO NOTHING` на уникальном индексе
`uq_agent_claims_statement` (создан в Task 1) — повторный запуск скрипта безопасен,
дополнительный SELECT EXISTS перед INSERT не требуется (Step 5 брифа).

evidence_run_ids оставлены пустыми ('{}') для всех 50 клеймов на Phase 0 seed — на
момент написания agent_runs уже содержит записи от Task 4-9 (audit_case1/2/3/4/6), но
брифовое условие "если сидинг делается ПОСЛЕ прогона кейсов — можно проставить реальные
id" неприменимо: сами Task 4-9 скрипты не были исполнены на проде (см. их task-N-report.md
"Scope Adjustment" — прод недоступен для этого раунда), поэтому agent_runs на проде пуст
и валидных run_id для проставления нет. Оставлять пустым честнее, чем гадать.

Запускать ТОЛЬКО на проде (см. CLAUDE.md "scripts/research/*.py — one-off скрипты для
VPS"):
    docker exec oraclepoly-backend python scripts/research/seed_agent_claims_phase0.py

До применения Task 1 (миграция agent_claims с uq_agent_claims_statement) на проде
скрипт не подключится осмысленно — INSERT упадёт на отсутствующей таблице/индексе.
Ожидаемо, не баг этого скрипта.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from typing import Any, Literal, TypedDict

# Allow execution from repo root: PYTHONPATH=.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.database import close_db, executemany, init_db  # noqa: E402

Status = Literal["active", "expired", "contradicted", "superseded"]
VerifyPolicy = Literal["time_halflife", "event_trigger"]


class ClaimDict(TypedDict):
    statement: str
    scope: str
    status: Status
    verify_policy: VerifyPolicy
    verify_halflife_days: int | None


# ─── Валидные множества — зеркалят CHECK-constraints migrations/
# 20260720132618_create_portfolio_agent_phase0_tables.sql (agent_claims) ───
VALID_STATUSES: frozenset[str] = frozenset({"active", "expired", "contradicted", "superseded"})
VALID_VERIFY_POLICIES: frozenset[str] = frozenset({"time_halflife", "event_trigger"})

# ─── Топ-50 клеймов корпуса ──────────────────────────────────────────────────
#
# Источники по группам (сверено выборочно построчно в task-3-report.md):
#
# [1-14] Детекторные вердикты и экономика исполнения — MEMORY.md topic-файлы:
#   d3-funnel-audit-2026-07-16, money-funnel-audit-2026-07-07,
#   backtest-prod-gap-is-metric-not-optimism-2026-06-24,
#   demote-alert-sum-roi-semantics-d42-degrades-2026-07-16,
#   virtual-stoploss-is-the-loss-2026-07-03, temperature-markets-adverse-selection-2026-07-07,
#   all-time-audit-hides-decaying-edge, d3-taker-price0-bug-2026-06-28.
#
# [15-24] Методологические правила — validation-bootstrap-seed-flaky-ci-2026-07-12,
#   exit-strategies-hold-to-resolution-confirmed-2026-07-03,
#   fable-review-portfolio-agent-claims-first-2026-07-19 (принцип паритета стадий,
#   curation depth), sizing-staleness-telemetry-7day-verdict-2026-07-12.
#
# [25-32] Инфраструктура/платформенные факты — proxy-not-wired-to-clob-2026-06-25,
#   polymarket-geoblocks-2026-06-25, killswitch-sticky-no-autoclear-2026-06-28,
#   kalshi-key-never-provisioned-2026-07-16, markets-toast-bloat-needs-vacuum-full-2026-06-28,
#   calibration-fallback-chain-dedup-merged-2026-07-05,
#   per-anchor-calibrators-written-not-read-2026-07-05.
#
# [33-44] Внешний ресёрч (UMA governance, Kelly sizing, market impact) —
#   research/portfolio-analyst-agent-concept-gaps-2026-07/claims.csv (C8-C18).
#
# [45-50] Архитектура агента и LLM-выбор — agent-llm-model-choice-2026-07-19,
#   agent-direction-scout-2026-07-20, fable-review-portfolio-agent-claims-first-2026-07-19
#   (§8 четыре примитива, §7 самоокупаемость агента).
CLAIMS: list[ClaimDict] = [
    # ── Детекторные вердикты и экономика исполнения ────────────────────────
    {
        "statement": (
            "D3_cross_mkt_arb — единственный live-детектор, направление подтверждено "
            "(Wilson lower bound 61.9% на 34 сделках), но net −$61.46 — деньги теряются "
            "на экономике исполнения, не на качестве сигнала"
        ),
        "scope": "detector:d3",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 30,
    },
    {
        "statement": (
            "Двойной учёт slippage в net_pnl D3 live (v2_execution_resolve.py:536,541 "
            "вычитал stake*half_spread из net_pnl, хотя slippage уже в gross_pnl) — "
            "backfill выполнен, расширен до virtual_settled (+$25,242 на 2231 строке)"
        ),
        "scope": "detector:d3",
        "status": "superseded",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "Реальный gross slippage на D3 live ≈11% стейка (|fill−signal|×qty=$9.87 "
            "на $87), колонка slippage_usd в БД занижает его в 7.6× ($1.30) и не может "
            "использоваться как метрика издержек исполнения"
        ),
        "scope": "detector:d3",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 30,
    },
    {
        "statement": (
            "D3 taker-ордера отбивались CLOB invalid price(0) из-за client-side бага "
            "(_place_order слал MarketOrderArgs без price через limit-роутер "
            "create_and_post_order) — исправлено на create_and_post_market_order"
        ),
        "scope": "detector:d3",
        "status": "superseded",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "D44_late_convergence_carry — старый статус 'verified KEEPER' (Wilson lower "
            "0.8940/0.9312) отменён постмортемом: по правильной метрике Net ROI детектор "
            "KILL в обоих окнах (backtest −5.10%, prod −2.00%)"
        ),
        "scope": "detector:d44",
        "status": "contradicted",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 90,
    },
    {
        "statement": (
            "Backtest→prod разрыв D44 — иллюзия выбора метрики (Wilson vs Net ROI), не "
            "backtest-оптимизм: prod (Net ROI −2.00%) фактически ЛУЧШЕ backtest (−5.10%)"
        ),
        "scope": "policy:backtest_methodology",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 90,
    },
    {
        "statement": (
            "sum_roi_net в demote-алерте D42 (-1058% на 98-157 сигналах) — СУММА "
            "per-signal realized_roi_net по терминальным сигналам, не портфельный ROI на "
            "вложенный капитал; правильный портфельный ROI D42 ≈ −6.96%/7d"
        ),
        "scope": "detector:d42",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 30,
    },
    {
        "statement": (
            "D42 задемоучен virtual→shadow 2026-07-16 (paused_reason=auto_drawdown): "
            "портфельный ROI −6.96%/7d деградирует (июнь −0.53% → июль −2.92%), "
            "D42×Sports (815/900 сигналов) ушла в минус −1.38%/30d"
        ),
        "scope": "detector:d42",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "D38_resolution_lag оставлен в virtual 2026-07-16 (портфельный ROI ≈ −0.21%/7d, "
            "стабильно −1.2…−1.5%/мес) — убыток целиком делает stop_loss-механика, "
            "hold-to-resolution даёт +$2112/30d"
        ),
        "scope": "detector:d38",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 30,
    },
    {
        "statement": (
            "Весь недельный virtual-убыток (D38+D42) создаёт механизм exit_reason="
            "stop_loss (62 сделки, winrate 0%, −$7589), не качество сигнала: "
            "held_to_resolution (43 сделки) даёт winrate 79%/+$1611"
        ),
        "scope": "policy:exit_strategy",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 60,
    },
    {
        "statement": (
            "Temperature-рынки (question ILIKE '%temperature%') — чистая adverse "
            "selection против метео-ботов: virtual −$12,328 на $79.8k оборота "
            "(ROI −15.4%), ВСЕ детекторы в минусе; прячутся внутри категории "
            "World & Economy без отдельного weather-гейта"
        ),
        "scope": "policy:category_gate",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 60,
    },
    {
        "statement": (
            "All-time агрегатный аудит детектора систематически даёт ложноположительный "
            "вердикт при затухающем edge (инцидент D26: all-time +494.6%/WR 93.6% vs "
            "recent-30d −36.6%/Sortino −0.04) — обязательны recent-окно + monthly-тренд + "
            "Sortino, не только WR/sum-ROI"
        ),
        "scope": "policy:detector_audit_methodology",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 90,
    },
    {
        "statement": (
            "D26_fade_whale_extreme — исторический WR 72.3%/ROI+16.4% как raw signal-level "
            "метрика, но honest decay-траектория (signal-decay-rerun-2026-06-10) нашла "
            "детектор убыточным (-2.11% prod) при любом фиксированном exit"
        ),
        "scope": "detector:d26",
        "status": "contradicted",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 90,
    },
    {
        "statement": (
            "Единственный структурный лимитер throughput живой D3-выборки на 2026-07-07 — "
            "live_max_open_positions=10 (118 reject за 3 дня), не kill-switch и не "
            "category-blacklist (оба к тому моменту сняты); параметр позже поднят до 20"
        ),
        "scope": "detector:d3",
        "status": "superseded",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    # ── Методологические правила ────────────────────────────────────────────
    {
        "statement": (
            "Bootstrap/permutation-тест с незасеянным default_rng(None) — мина CI: "
            "event_cluster_bootstrap_roi вызывался в validate_model без seed, что "
            "флейкало гейт ci[0]>0 непостоянно между прогонами; фикс bootstrap_seed=42"
        ),
        "scope": "policy:methodology",
        "status": "superseded",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "Per-event cluster bootstrap (сэмплировать events with replacement, не rows) "
            "обязателен для ROI confidence interval — несколько сделок на один event "
            "коррелированы, row-level split даёт ложно узкий/смещённый CI"
        ),
        "scope": "policy:methodology",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 180,
    },
    {
        "statement": (
            "Wilson score interval, не Wald/normal-approximation, обязателен для win-rate/"
            "fill-rate CI на малом N — Wald ломается на N<30 и p̂ около 0/1, Wilson "
            "устойчив и не требует scipy"
        ),
        "scope": "policy:methodology",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 180,
    },
    {
        "statement": (
            "Три альтернативные exit-механики (trailing stop, time-decay, partial exit) "
            "протестированы на живых прод-данных и НЕ дали улучшения относительно fixed "
            "TP/SL + hold-to-resolution — partial exit катастрофичен (убыток ×6-21)"
        ),
        "scope": "policy:exit_strategy",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 180,
    },
    {
        "statement": (
            "Truth-path детекторы (D3/D29/D38/D44/D45) правильно держать "
            "hold-to-resolution; ML-предсказатели краткосрочной ценовой дельты (D42 и "
            "аналоги) — кандидат на time-decay к horizon_sec — НЕ доказано эмпирически "
            "для класса, n=138 на одном детекторе"
        ),
        "scope": "policy:exit_strategy",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 60,
    },
    {
        "statement": (
            "Принцип паритета стадий: вердикт lifecycle-стадии валиден только для той "
            "exit/sizing/gate-политики, которую эта стадия фактически симулировала — "
            "смена политики автоматически инвалидирует вердикты, полученные при старой"
        ),
        "scope": "policy:lifecycle_methodology",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "Curation depth (закон-кандидат, НЕ проверен ретроспективно на всех "
            "детекторах): чем выше число человеческих степеней свободы в отборе сигнала, "
            "тем легче он проходит грубую проверку и тем вероятнее падает под честной"
        ),
        "scope": "policy:methodology",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 60,
    },
    {
        "statement": (
            "signal_price — наименее переносимый признак с holdout: доминирующий по "
            "train-важности признак систематически проседает на честном out-of-sample "
            "срезе (методологическая параллель curation-depth закону)"
        ),
        "scope": "policy:ml_methodology",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 90,
    },
    {
        "statement": (
            "Db-цена как entry_price для Kelly-сайзера устарела критично на общем потоке "
            "(median relative delta 27.7%, 52.7% сигналов сдвинулись >10¢) — но на "
            "конкретно D3 sizing-staleness cost = $0, т.к. raw Kelly всегда превышает cap "
            "в 6-19×"
        ),
        "scope": "policy:sizing",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 30,
    },
    {
        "statement": (
            "Устаревшее собственное знание (тихо протухший клейм) — самый повторяющийся "
            "класс потерь корпуса: D44 'verified KEEPER' аннулирован постмортемом через "
            "месяц, D3 'exit-хрупок TP/total 7.42×' перевёрнут через два месяца"
        ),
        "scope": "policy:knowledge_management",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    # ── Инфраструктура и платформенные факты ────────────────────────────────
    {
        "statement": (
            "Polymarket geofence блокирует ордера с IP сервера — "
            "'только US/санкционные юрисдикции блокируются' было ошибочным допущением"
        ),
        "scope": "infra:proxy",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "Селективный прокси-монопатч (только POST-ордера CLOB, геофенс по IP сервера) "
            "успешно обходит geoblock; read-запросы к Polymarket идут напрямую без прокси — "
            "HTTPS_PROXY/HTTP_PROXY глобально в .env запрещены (сжигают весь трафик Docker)"
        ),
        "scope": "infra:proxy",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "bot:kill_switch ставится без TTL и нигде в коде не снимается автоматически — "
            "транзиентные причины (network blip, staleness) залипают на часы/дни после "
            "исчезновения первопричины (наблюдалось 4× независимо)"
        ),
        "scope": "infra:execution_daemon",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 90,
    },
    {
        "statement": (
            "Kalshi API-ключ так и не был выдан проекту (провижининг подтверждён "
            "отсутствующим 2026-07-16) — джобы kalshi-* и D40/D45-scan выключены; "
            "Kalshi Phase 0 регистрация из Молдовы висит без явного статуса с 2026-05-28"
        ),
        "scope": "platform:kalshi",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "markets TOAST/heap-bloat от ежедневного массового UPDATE markets SET raw=NULL "
            "рецидивирует, потому что autovacuum помечает место свободным, но не отдаёт "
            "ОС — лечит только VACUUM FULL, не штатный autovacuum"
        ),
        "scope": "infra:database",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 90,
    },
    {
        "statement": (
            "Три независимые копии calibration fallback chain (G18→LGB→Beta→raw) "
            "разошлись в трёх файлах и уже давали прод-инцидент (KeyError 'g18_bin' после "
            "PR #502) до дедупа в единый app/calibration/fallback_chain.py (PR #649)"
        ),
        "scope": "infra:calibration",
        "status": "superseded",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "Per-anchor калибраторы (detector@{N}h) пишутся fit-джобами, но inference их "
            "полностью игнорирует — мёртвая инфраструктура, оставлена сознательно после "
            "дедупа calibration fallback chain, подключение изменило бы live-поведение"
        ),
        "scope": "infra:calibration",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "G18 bin-калибровка включена на проде (whitelist D14_crypto_winners_"
            "distribute_no, D36_obi_pressure) — контрфактический денежный вклад в "
            "фактически исполненные заказы измерен как ~$0 (оба whitelisted-детектора "
            "никогда не были в live execution_mode)"
        ),
        "scope": "policy:calibration",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 60,
    },
    # ── Внешний ресёрч: UMA governance / Kelly sizing / market impact ───────
    {
        "statement": (
            "Документированный governance-attack на UMA Optimistic Oracle через "
            "Polymarket: ~6M-UMA holder перевернул исход Ukraine mineral-deal рынка "
            "против фактического ответа за $100K-300K профита против $1K asserter-бонда"
        ),
        "scope": "risk:uma_governance",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "UMA dispute-voter conflict-of-interest систематический, не единичный: WSJ-"
            "расследование нашло ≥60% активных голосующих за год связанными с Polymarket "
            "trading-аккаунтами, топ-10 кошельков обычно контролируют >50% голосов в споре"
        ),
        "scope": "risk:uma_governance",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "Реальный инцидент 2025 удался при участии всего 7-8M UMA-токенов из ~85M "
            "market cap — фактическая стоимость атаки далеко ниже теоретического 65% "
            "supply-порога, потому что явка (turnout), не supply, — связывающее ограничение"
        ),
        "scope": "risk:uma_governance",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "Пост-инцидентная реформа UMA (UMIP-189/MOOV2, ~август 2025) ограничила "
            "активное dispute-resolution белым списком ~37 адресов, но сам whitelist "
            "остаётся концентрированным (~4 адреса ~40% релевантного supply)"
        ),
        "scope": "risk:uma_governance",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "MiCA grandfathering для on-chain venues заканчивается в июле 2026 — прямой "
            "регуляторный риск для площадок, доступных из Молдовы; статус на текущий "
            "момент НЕ верифицирован ни в одном внутреннем документе проекта"
        ),
        "scope": "risk:regulatory_mica",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "Practitioner risk-guidance для держателей позиций: применять ментальную "
            "'oracle risk premium' скидку к рынкам с единственным/неоднозначным "
            "источником резолва, сканировать историю споров перед сайзингом"
        ),
        "scope": "policy:risk_management",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 180,
    },
    {
        "statement": (
            "Fractional Kelly (half/quarter-Kelly) — стандартная практика от основателей "
            "поля (Thorp/MacLean/Ziemba): доминирующий квантифицированный риск в Kelly-"
            "сайзинге — ошибка оценки mean/edge, не variance/covariance (~20:2:1)"
        ),
        "scope": "policy:sizing",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 365,
    },
    {
        "statement": (
            "Независимое от Thorp/MacLean/Ziemba теоретическое обоснование sub-full "
            "Kelly существует в ergodicity economics (Ole Peters): оптимальное плечо из "
            "time-average, не ensemble-average роста капитала при мультипликативной динамике"
        ),
        "scope": "policy:sizing",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 365,
    },
    {
        "statement": (
            "Drawdown-triggered deleveraging с явным гистерезисом (tiered risk cuts, "
            "полное re-gear только после восстановления капитала до high-water mark) — "
            "стандартная практика для малых систематических трейдеров без risk-manager'а"
        ),
        "scope": "policy:risk_management",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 365,
    },
    {
        "statement": (
            "Наивное Kelly-stacking по нескольким одновременным стратегиям как "
            "независимым систематически переоценивает безопасное портфельное "
            "распределение капитала, если стратегии на самом деле коррелированы"
        ),
        "scope": "policy:sizing",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 365,
    },
    {
        "statement": (
            "Корреляции стратегий, низкие в нормальном режиме рынка, могут резко "
            "вырасти в стрессовый период (типично 0.30 нормально → 0.60 в кризис) — "
            "корреляционная матрица из calm-режима недооценивает tail-scenario риск"
        ),
        "scope": "policy:risk_management",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 365,
    },
    {
        "statement": (
            "Общая (не prediction-market-специфичная) методология оценки ёмкости "
            "моделирует price impact как растущий с квадратным корнем размера ордера "
            "относительно доступной ликвидности, не линейно — применимость к тонким "
            "discrete-resolution книгам prediction markets НЕ валидирована"
        ),
        "scope": "policy:capacity_estimation",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 365,
    },
    # ── Архитектура агента и LLM-выбор ──────────────────────────────────────
    {
        "statement": (
            "Portfolio Analyst Agent v2.1: центр тяжести концепта смещён с фабрики "
            "гипотез над детекторным конвейером на три детектор-агностичных примитива — "
            "реестр утверждений с полураспадом, экономика внимания владельца, портфель "
            "неэффективностей"
        ),
        "scope": "policy:agent_architecture",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "Marginal value 34-й гипотезы детектора ≈0 (выживаемость гипотез ~3%, "
            "~33 идеи → 24 детектора → 1 live), тогда как крупнейшие денежные сдвиги за "
            "4 месяца дали изменения политики/методологии (hold-to-resolution, снятие "
            "Sports-блэклиста, WR→Net ROI), не новые детекторы"
        ),
        "scope": "policy:agent_architecture",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 90,
    },
    {
        "statement": (
            "Рекомендация модели для Portfolio Analyst Agent: Sonnet 5 для research-"
            "линзы, Haiku 4.5 для брифов, прямой Anthropic API с batch+cache (~$4-7/мес); "
            "judge-роль — другое семейство модели через OpenRouter"
        ),
        "scope": "policy:agent_llm_choice",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "Дефолт gpt-4o-mini в config.py протух как справочная точка выбора модели — "
            "актуальная рекомендация пересмотрена в пользу Anthropic-семейства "
            "(Sonnet 5 / Haiku 4.5), не OpenAI"
        ),
        "scope": "policy:agent_llm_choice",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "Direction Scout (§4.3.1) — отдельный компонент автономного выбора "
            "направления исследований (не проверки готового вопроса): weekly-каденция, "
            "6-шаговый детерминированный пайплайн (SQL-снапшот → версионированные "
            "поисковые темы → web_search → pre-filter → 1 LLM-вызов → reconciliation)"
        ),
        "scope": "policy:agent_architecture",
        "status": "active",
        "verify_policy": "event_trigger",
        "verify_halflife_days": None,
    },
    {
        "statement": (
            "Judge LLM-калибровка (Pearson→Cohen's kappa vs human-human baseline) "
            "требует базы человеческих вердиктов (минимум 50-100), которой при текущем "
            "темпе решений проекта не будет месяцами — right-sizing eval-машинерии "
            "откладывается до порога данных, принцип заимствуется раньше машинерии"
        ),
        "scope": "policy:agent_architecture",
        "status": "active",
        "verify_policy": "time_halflife",
        "verify_halflife_days": 90,
    },
]


def validate_claims(claims: list[ClaimDict]) -> list[str]:
    """Чистая функция-валидатор без side effects — пригодна для unit-теста.

    Проверяет ровно то, что нужно перед bulk INSERT: 50 уникальных непустых
    statement, валидные scope/status/verify_policy, verify_halflife_days
    согласован с verify_policy (NULL допустим только для event_trigger, но
    НЕ обязателен — time_halflife обязан иметь число).

    Returns:
        Список текстовых описаний найденных проблем (пустой список = всё ок).
    """
    problems: list[str] = []

    statements = [c["statement"] for c in claims]
    if len(statements) != len(set(statements)):
        seen: set[str] = set()
        dupes = set()
        for s in statements:
            if s in seen:
                dupes.add(s)
            seen.add(s)
        problems.append(f"duplicate statements: {dupes}")

    for i, claim in enumerate(claims):
        prefix = f"claim[{i}] ({claim.get('statement', '<missing>')[:60]!r})"
        if not claim.get("statement", "").strip():
            problems.append(f"{prefix}: empty statement")
        if not claim.get("scope", "").strip():
            problems.append(f"{prefix}: empty scope")
        if claim.get("status") not in VALID_STATUSES:
            problems.append(f"{prefix}: invalid status {claim.get('status')!r}")
        if claim.get("verify_policy") not in VALID_VERIFY_POLICIES:
            problems.append(f"{prefix}: invalid verify_policy {claim.get('verify_policy')!r}")
        halflife = claim.get("verify_halflife_days")
        if claim.get("verify_policy") == "time_halflife" and halflife is None:
            problems.append(f"{prefix}: time_halflife requires non-null verify_halflife_days")
        if halflife is not None and halflife <= 0:
            problems.append(f"{prefix}: verify_halflife_days must be positive, got {halflife}")

    return problems


def count_by_verify_policy(claims: list[ClaimDict]) -> dict[str, int]:
    """Подсчёт клеймов по verify_policy — для acceptance-проверки "не все time_halflife"."""
    counts: dict[str, int] = {"time_halflife": 0, "event_trigger": 0}
    for claim in claims:
        counts[claim["verify_policy"]] += 1
    return counts


_INSERT_SQL = """
INSERT INTO agent_claims (statement, scope, status, verify_policy, verify_halflife_days, evidence_run_ids)
VALUES ($1, $2, $3, $4, $5, '{}')
ON CONFLICT (statement) DO NOTHING
"""


async def seed(claims: list[ClaimDict]) -> None:
    problems = validate_claims(claims)
    if problems:
        for p in problems:
            print(f"VALIDATION ERROR: {p}", file=sys.stderr)
        sys.exit(1)

    policy_counts = count_by_verify_policy(claims)
    print(f"=== Seeding {len(claims)} agent_claims (Phase 0 §3.4) ===")
    print(f"verify_policy breakdown: {policy_counts}")

    args: list[tuple[Any, ...]] = [
        (c["statement"], c["scope"], c["status"], c["verify_policy"], c["verify_halflife_days"]) for c in claims
    ]
    await executemany(_INSERT_SQL, args)
    print(f"Upserted (ON CONFLICT DO NOTHING) {len(args)} rows into agent_claims.")


async def main() -> None:
    await init_db()  # DATABASE_URL — намеренно НЕ agent_database_url, см. docstring модуля.
    try:
        await seed(CLAIMS)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())

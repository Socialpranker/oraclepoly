"""Shared SQL + computation primitives для 5 detector endpoints.

Раньше каждый endpoint в `app/api/bot.py` собирал свой набор queries
(stage_map, shadow_signals_7d, virtual_metrics_7d, backtest_map, ...).
Логика дублировалась 2-3× с микро-расхождениями (filter conditions,
column aliases) — рефактор Phase 2.1.

Каждая функция здесь делает одно: один SQL + минимальная постобработка
в plain dict. Endpoints собирают response из этих primitives.

Тестовое покрытие: tests/test_endpoints_snapshot.py фиксирует contract
**после** миграции — endpoints рендерят те же ключи что и раньше.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app.services.backtest_metadata import (
    list_available_backtests,
    parse_backtest_md,
)
from app.services.database import fetch
from app.services.wilson_ci import wilson_score_interval

# ─── Stage map: detector → 'shadow'|'virtual'|'live'|'paused' ────────────────


async def fetch_stage_map() -> dict[str, str]:
    """Return {detector: stage} from detector_state."""
    rows = await fetch("SELECT detector, stage FROM detector_state")
    return {r["detector"]: r["stage"] for r in rows}


# ─── Active detector SSoT: db_active ∩ cron_active ──────────────────────────

# Путь к crontab.conf внутри backend container. См. Dockerfile: код копируется в /app.
_CRONTAB_PATH = Path("/app/crontab.conf")

# Map cron-job name (e.g. "d3-scan") → canonical detector name (e.g. "D3_cross_mkt_arb").
# Source: app/detectors/*.py DETECTOR_NAME constants. Hardcoded — это known fixed list.
# При добавлении нового детектора с cron — обновить здесь.
_CRON_NAME_TO_DETECTOR: dict[str, str] = {
    "d3-scan": "D3_cross_mkt_arb",
    "d13-scan": "D13_retail_confidence_fade",
    "d14-scan": "D14_crypto_winners_distribute_no",
    "d26-scan": "D26_fade_whale_extreme",
    "d29-scan": "D29_negrisk_basket_arb",
    "d36-scan": "D36_obi_pressure",
    "d38-scan": "D38_resolution_lag",
    "d40-scan": "D40_kalshi_cross_platform",
    "d42-scan": "D42_event_driven_ml",
    "d44-scan": "D44_late_convergence_carry",
    # d45-scan → D45b, а НЕ d45_chainlink_leadlag (исправлено 2026-08-04).
    # Реестр cron_runner.py:325 ведёт "d45-scan" на app.jobs.d45_lead_lag_scan,
    # который импортирует d45_lead_lag_cross_venue (= D45b); комментарий
    # cron-строки говорит то же ("LeadLagCrossVenue: Polymarket↔Kalshi").
    # Маппинг был единственным местом, утверждавшим обратное, — при
    # расконсервации Kalshi мониторинг записал бы сигналы D45b на чужое имя.
    # Ключ "d45b-scan" удалён как фантомный: такого cron-имени нет ни в
    # реестре, ни в crontab.conf — запустить по нему ничего нельзя.
    # d45_chainlink_leadlag в этом маппинге места не имеет by design: он идёт
    # не через cron, а фоновым loop в app/main.py под флагом
    # d45_chainlink_leadlag_enabled (как M2 — через WSS-хук). Его место —
    # _WSS_REALTIME_DETECTORS ниже, если понадобится вернуть в надзор.
    "d45-scan": "D45b_lead_lag_cross_venue",
    # Пропуск с момента заведения D49 (2026-07-10): cron идёт каждые 5 минут и
    # детектор эмитит ~144 сигнала/неделю, но без строки здесь он выпадал из
    # db_active & cron_active — то есть не проверялся на деградацию edge
    # (edge_decay_monitor: Wilson/Sortino/CUSUM) и не мониторился SLO.
    "d49-scan": "D49_strike_ladder",
}

# WSS-realtime детекторы: активны по БД БЕЗ cron-строки (детекция в WSS-потоке,
# не в scan-cron). db∩cron-пересечение их выкинуло бы — whitelist возвращает.
# M2_liquidity_vacuum детектит вакуум realtime в _handle_market_message
# (app/detectors/m2_vacuum_hook.py); cron-гейт к нему неприменим by-design.
_WSS_REALTIME_DETECTORS: set[str] = {"M2_liquidity_vacuum"}

_DB_ACTIVE_SQL = """
SELECT detector
  FROM detector_state
 WHERE stage IN ('shadow', 'virtual', 'live')
   AND paused_reason IS NULL
"""


async def fetch_db_active_detectors() -> set[str]:
    """Детекторы, активные ПО БД: stage shadow/virtual/live и не на паузе."""
    rows = await fetch(_DB_ACTIVE_SQL)
    return {r["detector"] for r in rows}


def parse_active_cron_detectors(crontab_path: Path = _CRONTAB_PATH) -> set[str]:
    """Детекторы с активной (незакомментированной) cron-строкой в crontab.conf.

    Нет файла (local dev) → fail-open: все известные детекторы из маппинга.
    """
    if not crontab_path.exists():
        return set(_CRON_NAME_TO_DETECTOR.values())
    active: set[str] = set()
    cron_cmd_re = re.compile(r"cron_runner\.py\s+([a-z0-9_-]+)")
    for raw_line in crontab_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = cron_cmd_re.search(line)
        if not match:
            continue
        detector = _CRON_NAME_TO_DETECTOR.get(match.group(1))
        if detector:
            active.add(detector)
    return active


async def fetch_active_detectors() -> set[str]:
    """SSoT: детектор ЭФФЕКТИВНО активен.

    = (активен в БД И имеет активный cron)  — обычные scan-cron детекторы
      ∪ (активен в БД И WSS-realtime)        — M2 и др. без cron-строки.

    Cut-детекторы (cron закомментирован, не в whitelist) исключаются.
    """
    db_active = await fetch_db_active_detectors()
    cron_active = parse_active_cron_detectors()
    return (db_active & cron_active) | (db_active & _WSS_REALTIME_DETECTORS)


async def fetch_virtual_state_rows() -> list[dict[str, Any]]:
    """Detectors в stage='virtual' с promoted_at — для live_readiness.

    adversarial_mirror_pass добавлен Phase 0 Stage 3 для mandatory gate.
    NULL = not_yet_evaluated → MANUAL_OVERRIDE_NEEDED в evaluate_promote_gates.
    """
    rows = await fetch(
        """
        SELECT detector, stage, promoted_at, adversarial_mirror_pass
          FROM detector_state
         WHERE stage = 'virtual'
        """
    )
    return [dict(r) for r in rows]


async def fetch_portfolio_map() -> dict[str, str]:
    """Return {detector: portfolio_id} for batch lookup в endpoints.

    Re-exported из app.services.portfolios для consistency с другими
    fetch_*_map helpers здесь.
    """
    from app.services.portfolios import fetch_portfolio_map as _fetch

    return await _fetch()


# ─── Backtest map: parse_backtest_md per detector ────────────────────────────


def fetch_backtest_map() -> dict[str, dict[str, Any]]:
    """Return {detector: backtest_meta} for all детекторов с canonical .md.

    Sync function — parse_backtest_md читает диск, нет I/O bound benefit
    от async wrapper.
    """
    out: dict[str, dict[str, Any]] = {}
    for det in list_available_backtests():
        meta = parse_backtest_md(det)
        if meta:
            out[det] = meta
    return out


# ─── Shadow signals 7d: distinct events, wins, terminal per detector ────────


_SHADOW_SIGNALS_7D_SQL = """
SELECT
    ds.detector,
    COUNT(DISTINCT m.event_id) FILTER (
        WHERE ds.resolved AND m.event_id IS NOT NULL
    ) AS distinct_events_7d,
    COUNT(DISTINCT ds.market_id) FILTER (WHERE ds.resolved) AS distinct_markets_7d,
    COUNT(*) FILTER (WHERE ds.resolved AND ds.outcome = 'WIN') AS wins_7d,
    COUNT(*) FILTER (WHERE ds.resolved AND ds.outcome IN ('WIN', 'LOSS')) AS terminal_7d,
    COUNT(*) FILTER (WHERE ds.resolved) AS resolved_7d,
    COUNT(*) AS total_7d,
    -- Honest gates 2026-05-27: sum_roi_net учитывает fees+slippage hold-to-resolution.
    -- Это реальная экономика (а не WR), которую и проверяем в G2.
    COALESCE(
        SUM(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS')),
        0
    )::float AS sum_roi_net_7d,
    -- v3 2026-05-27: Sortino numerator = mean ROI.
    AVG(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS'))::float
        AS mean_roi_net_7d,
    -- v3 2026-05-27: downside semivariance (squared negative returns).
    -- Используется для Sortino denominator = √(sum_neg_sq / n_terminal).
    -- Σ(min(R,0)²) делим в Python чтобы избежать nested aggregates.
    COALESCE(
        SUM(POWER(ds.realized_roi_net, 2)) FILTER (
            WHERE ds.outcome IN ('WIN', 'LOSS') AND ds.realized_roi_net < 0
        ),
        0
    )::float AS sum_neg_sq_roi_7d,
    -- v3: median entry price (для payout-aware BE WR fallback).
    -- Для NO side: payout = 1/(1-yes_entry) = 1/no_cost. Для YES: 1/yes_cost.
    -- Used когда virtual fingerprint недоступен (shadow-only детекторы).
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ds.current_price)
        FILTER (WHERE ds.resolved)::float AS median_entry_price_7d,
    -- catastrophic losses = signal где shadow realized_roi_net < -80% от stake.
    -- v3 2026-05-27: threshold tightened 5% → 3% (см. A4 adversarial critique).
    -- UNITS FIX 2026-05-28: realized_roi_net хранится в ПРОЦЕНТАХ
    -- (_realized_roi_net_pct → roi_pct = net_pnl/notional*100), поэтому -80 (не -0.80).
    -- Прежний порог -0.80 ловил ЛЮБОЙ лосс хуже -0.8% → почти все лоссы →
    -- cat_pct раздут → G3 систематически fail → детекторы не проходили promote.
    COUNT(*) FILTER (WHERE ds.outcome = 'LOSS' AND ds.realized_roi_net < -80)
        AS catastrophic_losses_7d
FROM detector_signals ds
LEFT JOIN markets m
    ON m.polymarket_id = ds.market_id OR m.condition_id = ds.market_id
WHERE ds.generated_at > now() - INTERVAL '7 days'
GROUP BY ds.detector
"""


async def fetch_shadow_signals_7d() -> dict[str, dict[str, Any]]:
    """Return {detector: shadow_stats_dict} — distinct events, wins, terminal etc.

    Используется в /v2/lifecycle/promotion_readiness и
    _shadow_ready_candidates() (helper для /v2/lifecycle/live_readiness).
    """
    rows = await fetch(_SHADOW_SIGNALS_7D_SQL)
    return {r["detector"]: dict(r) for r in rows}


# ─── Virtual period signals: от promoted_at до now() ─────────────────────────
# Та же агрегация что _SHADOW_SIGNALS_7D_SQL но окно = promoted_at → now().
# Параметр $1 = список detector names, $2 = promoted_at timestamp.
# Вызывается батчами per-promoted_at группе в fetch_virtual_period_signals().

_VIRTUAL_PERIOD_SIGNALS_SQL = """
SELECT
    ds.detector,
    COUNT(DISTINCT m.event_id) FILTER (
        WHERE ds.resolved AND m.event_id IS NOT NULL
    ) AS distinct_events_7d,
    COUNT(DISTINCT ds.market_id) FILTER (WHERE ds.resolved) AS distinct_markets_7d,
    COUNT(*) FILTER (WHERE ds.resolved AND ds.outcome = 'WIN') AS wins_7d,
    COUNT(*) FILTER (WHERE ds.resolved AND ds.outcome IN ('WIN', 'LOSS')) AS terminal_7d,
    COUNT(*) FILTER (WHERE ds.resolved) AS resolved_7d,
    COUNT(*) AS total_7d,
    COALESCE(
        SUM(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS')),
        0
    )::float AS sum_roi_net_7d,
    AVG(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS'))::float
        AS mean_roi_net_7d,
    COALESCE(
        SUM(POWER(ds.realized_roi_net, 2)) FILTER (
            WHERE ds.outcome IN ('WIN', 'LOSS') AND ds.realized_roi_net < 0
        ),
        0
    )::float AS sum_neg_sq_roi_7d,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ds.current_price)
        FILTER (WHERE ds.resolved)::float AS median_entry_price_7d,
    COUNT(*) FILTER (WHERE ds.outcome = 'LOSS' AND ds.realized_roi_net < -80)
        AS catastrophic_losses_7d
FROM detector_signals ds
LEFT JOIN markets m
    ON m.polymarket_id = ds.market_id OR m.condition_id = ds.market_id
WHERE ds.detector = ANY($1::text[])
  AND ds.generated_at >= $2
GROUP BY ds.detector
"""


async def fetch_virtual_period_signals(
    detectors_with_promoted_at: list[tuple[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Return {detector: signal_stats} за период promoted_at → now().

    Args:
        detectors_with_promoted_at: list of (detector_name, promoted_at).
            promoted_at=None → детектор возвращается как пустой dict.

    Returns same shape как fetch_shadow_signals_7d() — drop-in совместимость.
    """
    from collections import defaultdict

    by_date: dict[Any, list[str]] = defaultdict(list)
    result: dict[str, dict[str, Any]] = {}

    for det, promoted_at in detectors_with_promoted_at:
        if promoted_at is None:
            result[det] = {}
            continue
        by_date[promoted_at].append(det)

    for promoted_at, dets in by_date.items():
        rows = await fetch(_VIRTUAL_PERIOD_SIGNALS_SQL, dets, promoted_at)
        for r in rows:
            result[r["detector"]] = dict(r)
        for det in dets:
            if det not in result:
                result[det] = {}

    return result


# ─── Recent window signals: фиксированное 30д-окно от now() ──────────────────
# Та же агрегация что _VIRTUAL_PERIOD_SIGNALS_SQL, но окно НЕ привязано к
# promoted_at — фиксированные последние 30 дней. Используется в
# evaluate_promote_readiness для recency-компоненты (sum_roi_net/Sortino):
# all-time от promoted_at маскирует затухающий edge (см. memory
# [[all-time-audit-hides-decaying-edge]]) — свежее окно ловит деградацию,
# которую кумулятив размазывает по долгой истории.

_RECENT_WINDOW_SIGNALS_SQL = """
SELECT
    ds.detector,
    COUNT(DISTINCT m.event_id) FILTER (
        WHERE ds.resolved AND m.event_id IS NOT NULL
    ) AS distinct_events_7d,
    COUNT(DISTINCT ds.market_id) FILTER (WHERE ds.resolved) AS distinct_markets_7d,
    COUNT(*) FILTER (WHERE ds.resolved AND ds.outcome = 'WIN') AS wins_7d,
    COUNT(*) FILTER (WHERE ds.resolved AND ds.outcome IN ('WIN', 'LOSS')) AS terminal_7d,
    COUNT(*) FILTER (WHERE ds.resolved) AS resolved_7d,
    COUNT(*) AS total_7d,
    COALESCE(
        SUM(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS')),
        0
    )::float AS sum_roi_net_7d,
    AVG(ds.realized_roi_net) FILTER (WHERE ds.outcome IN ('WIN', 'LOSS'))::float
        AS mean_roi_net_7d,
    COALESCE(
        SUM(POWER(ds.realized_roi_net, 2)) FILTER (
            WHERE ds.outcome IN ('WIN', 'LOSS') AND ds.realized_roi_net < 0
        ),
        0
    )::float AS sum_neg_sq_roi_7d,
    PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY ds.current_price)
        FILTER (WHERE ds.resolved)::float AS median_entry_price_7d,
    COUNT(*) FILTER (WHERE ds.outcome = 'LOSS' AND ds.realized_roi_net < -80)
        AS catastrophic_losses_7d
FROM detector_signals ds
LEFT JOIN markets m
    ON m.polymarket_id = ds.market_id OR m.condition_id = ds.market_id
WHERE ds.detector = ANY($1::text[])
  AND ds.generated_at > now() - INTERVAL '30 days'
GROUP BY ds.detector
"""


async def fetch_recent_window_signals(detectors: list[str], days: int = 30) -> dict[str, dict[str, Any]]:
    """Return {detector: signal_stats} за фиксированное окно last `days` дней от now().

    Same shape как fetch_virtual_period_signals/fetch_shadow_signals_7d — drop-in.
    `days` параметр документирует intent, но текущий SQL фиксирован на 30 дней
    (INTERVAL literal — asyncpg не параметризует INTERVAL напрямую без
    доп. приведения типов; 30d — единственное используемое значение сейчас).
    """
    if not detectors:
        return {}
    if days != 30:
        raise ValueError("fetch_recent_window_signals поддерживает только days=30 (см. SQL INTERVAL literal)")
    rows = await fetch(_RECENT_WINDOW_SIGNALS_SQL, detectors)
    result: dict[str, dict[str, Any]] = {r["detector"]: dict(r) for r in rows}
    for det in detectors:
        if det not in result:
            result[det] = {}
    return result


# ─── Virtual execution metrics 7d ────────────────────────────────────────────


def effective_fill_counts(n_filled: int, n_would_have_filled: int, n_placed: int) -> tuple[int, int]:
    """Reclassify would-have-filled (price-drift) rejections as fills for gate.

    2b semantics: price-drift rejection = цена ушла В НАШУ сторону = edge был,
    опоздали. Считается filled в числителе, остаётся в знаменателе. no_book
    (реальная неликвидность) НЕ входит в would_have_filled → штрафует fill rate.

    Returns (n_filled_effective, n_placed) для Wilson fill rate.
    """
    n_filled_eff = min(n_filled + n_would_have_filled, n_placed)
    return n_filled_eff, n_placed


# 2026-05-19: исключаем outcome='VOIDED_BUG' из всех settled-агрегаций.
# Это 98 исторических ордеров от cleanup'а 2026-05-13 (book ordering bug
# до миграции на VPS). Они засчитывались как 'virtual_settled' с net_pnl=0,
# разбавляя реальные метрики на 25% executed volume. Свежих VOIDED не было >5 дней.
# Все агрегации ниже фильтруют (status='virtual_settled' AND outcome != 'VOIDED_BUG').
#
# 2026-05-20 Phase 0 Stage 2 follow-up: filter по simulation_model_version =
# 'v2_conservative' — только honest fills (strict price-through + AS penalty).
# Без этого filter Wilson promote gates следующие 7 дней будут смешивать
# v1_optimistic (fill on touch, no AS) с новыми v2 записями — gate evaluation
# становится грязным.
_VIRTUAL_METRICS_7D_SQL = """
SELECT
    eo.detector,
    COUNT(*) FILTER (
        WHERE eo.status IN ('virtual_placed', 'virtual_filled', 'virtual_settled', 'rejected')
    ) AS n_placed,
    COUNT(*) FILTER (
        WHERE eo.status IN ('virtual_filled', 'virtual_settled')
          AND COALESCE(eo.outcome, '') != 'VOIDED_BUG'
    ) AS n_filled,
    COUNT(*) FILTER (
        WHERE eo.status = 'virtual_settled'
          AND COALESCE(eo.outcome, '') != 'VOIDED_BUG'
    ) AS n_settled,
    COUNT(*) FILTER (WHERE eo.status = 'virtual_settled' AND eo.outcome = 'WIN') AS wins,
    COUNT(*) FILTER (WHERE eo.status = 'virtual_settled' AND eo.outcome = 'LOSS') AS losses,
    COUNT(*) FILTER (WHERE eo.status = 'virtual_settled' AND eo.outcome = 'EARLY_EXIT') AS early_exits,
    -- Используем net_pnl (после fee+slippage), не gross pnl. Раньше суммировалось
    -- eo.pnl, что завышало UI PnL на ~$8k в 7d (см. инцидент 2026-05-18).
    -- COALESCE(net_pnl, pnl) — fallback для legacy строк без net_pnl.
    COALESCE(SUM(COALESCE(eo.net_pnl, eo.pnl)) FILTER (
        WHERE eo.status = 'virtual_settled' AND COALESCE(eo.outcome, '') != 'VOIDED_BUG'
    ), 0) AS pnl_usd,
    -- Realised PnL = только реально резолвнутые WIN/LOSS. Без mark-to-market.
    -- Это «правда»: что реально произошло когда рынок discided.
    COALESCE(SUM(COALESCE(eo.net_pnl, eo.pnl)) FILTER (
        WHERE eo.status = 'virtual_settled' AND eo.outcome IN ('WIN', 'LOSS')
    ), 0) AS pnl_resolved_usd,
    -- Paper PnL = mark-to-market на момент TP/SL/mid-zone exit. Может быть
    -- завышен в extreme zones из-за разницы mid vs realistic bid/ask.
    COALESCE(SUM(COALESCE(eo.net_pnl, eo.pnl)) FILTER (
        WHERE eo.status = 'virtual_settled' AND eo.outcome = 'EARLY_EXIT'
    ), 0) AS pnl_paper_usd,
    -- would-have-filled: rejected из-за fill_drift_too_large (цена ушла В НАШУ
    -- сторону между сигналом и попыткой исполнения). Это НЕ провал ликвидности —
    -- edge был, мы опоздали. Для fill rate gate считаются как filled (2b semantics).
    -- no_book = реальная неликвидность, НЕ входит (правильно штрафуется).
    -- ВНЕ scope: price_drift rejections (execution_mode='rejected', не 'virtual') —
    -- они вообще не входят в n_placed/n_filled этого SQL (отсекаются внешним
    -- WHERE eo.execution_mode='virtual'), поэтому здесь их учитывать не нужно.
    COUNT(*) FILTER (
        WHERE eo.status = 'rejected'
          AND eo.rejection_reason LIKE 'fill_drift_too_large%'
    ) AS n_would_have_filled
FROM execution_orders eo
WHERE eo.execution_mode = 'virtual'
  AND eo.created_at > now() - INTERVAL '7 days'
  AND eo.simulation_model_version = 'v2_conservative'
  -- Исключаем re-entry позиции (meta.reentry_excluded=true). STANDALONE до
  -- event-guard заходил в один event 10-32× → раздувало промоут-метрики
  -- (Sortino/G1/wins) фантомом. См. standalone_virtual_pnl_inflation_2026-06-04:
  -- гейт видел +$41K/391 settled, честно (1 вход/event) +$781/28. Помечает
  -- одноразовый backfill scripts/backfill_reentry_excluded.py; forward guard
  -- блокирует re-entry в обоих режимах → новых помеченных не будет.
  AND COALESCE((eo.meta->>'reentry_excluded')::bool, false) = false
GROUP BY eo.detector
"""


async def fetch_virtual_metrics_7d() -> dict[str, dict[str, Any]]:
    """Return {detector: {n_placed, n_filled, n_settled, wins, losses, pnl_usd}}."""
    rows = await fetch(_VIRTUAL_METRICS_7D_SQL)
    return {r["detector"]: dict(r) for r in rows}


# ─── Live execution metrics 7d — для demote-мониторинга live-детекторов ─────
# Копия _VIRTUAL_METRICS_7D_SQL: те же статусы без 'virtual_' префикса
# (live execution_orders.status = 'placed'/'filled'/'settled', не
# 'virtual_placed'/...), execution_mode IN ('maker','taker') вместо ='virtual'.
# БЕЗ simulation_model_version-фильтра — этот столбец имеет смысл только для
# paper-симуляции (какая модель execution simulated fill), у реальных
# live-ордеров его нет (NULL) — фильтр на него занулил бы всю live-выборку.

_LIVE_METRICS_7D_SQL = """
SELECT
    eo.detector,
    COUNT(*) FILTER (
        WHERE eo.status IN ('placed', 'filled', 'settled', 'rejected')
    ) AS n_placed,
    COUNT(*) FILTER (
        WHERE eo.status IN ('filled', 'settled')
          AND COALESCE(eo.outcome, '') != 'VOIDED_BUG'
    ) AS n_filled,
    COUNT(*) FILTER (
        WHERE eo.status = 'settled'
          AND COALESCE(eo.outcome, '') != 'VOIDED_BUG'
    ) AS n_settled,
    COUNT(*) FILTER (WHERE eo.status = 'settled' AND eo.outcome = 'WIN') AS wins,
    COUNT(*) FILTER (WHERE eo.status = 'settled' AND eo.outcome = 'LOSS') AS losses,
    COUNT(*) FILTER (WHERE eo.status = 'settled' AND eo.outcome = 'EARLY_EXIT') AS early_exits,
    COALESCE(SUM(COALESCE(eo.net_pnl, eo.pnl)) FILTER (
        WHERE eo.status = 'settled' AND COALESCE(eo.outcome, '') != 'VOIDED_BUG'
    ), 0) AS pnl_usd,
    COALESCE(SUM(COALESCE(eo.net_pnl, eo.pnl)) FILTER (
        WHERE eo.status = 'settled' AND eo.outcome IN ('WIN', 'LOSS')
    ), 0) AS pnl_resolved_usd,
    COALESCE(SUM(COALESCE(eo.net_pnl, eo.pnl)) FILTER (
        WHERE eo.status = 'settled' AND eo.outcome = 'EARLY_EXIT'
    ), 0) AS pnl_paper_usd,
    COUNT(*) FILTER (
        WHERE eo.status = 'rejected'
          AND eo.rejection_reason LIKE 'fill_drift_too_large%'
    ) AS n_would_have_filled
FROM execution_orders eo
WHERE eo.execution_mode IN ('maker', 'taker')
  AND eo.created_at > now() - INTERVAL '7 days'
  AND COALESCE((eo.meta->>'reentry_excluded')::bool, false) = false
GROUP BY eo.detector
"""


async def fetch_live_metrics_7d() -> dict[str, dict[str, Any]]:
    """Return {detector: {n_placed, n_filled, n_settled, wins, losses, pnl_usd}} — live-ордера 7d.

    Копия fetch_virtual_metrics_7d на реальных execution_mode IN ('maker','taker').
    Используется в demote-мониторинге, чтобы live-детекторы (не только virtual)
    попадали под drawdown-alert (audit: live был вне demote-контура).
    """
    rows = await fetch(_LIVE_METRICS_7D_SQL)
    return {r["detector"]: dict(r) for r in rows}


# ─── Per-stage PnL 7d (split virtual vs live) — для /v2/detectors lifecycle ─


_PNL_BY_MODE_7D_SQL = """
SELECT eo.detector,
       eo.execution_mode,
       SUM(COALESCE(eo.net_pnl, 0)) AS pnl_usd_7d
  FROM execution_orders eo
 WHERE eo.created_at >= now() - interval '7 days'
   AND eo.status IN ('virtual_filled', 'virtual_settled', 'filled', 'settled')
   -- Исключаем re-entry (см. standalone_virtual_pnl_inflation_2026-06-04) — иначе
   -- per-detector PnL на дашборде (DetectorLifecycleCard) раздут скальпом одного event.
   AND COALESCE((eo.meta->>'reentry_excluded')::bool, false) = false
 GROUP BY eo.detector, eo.execution_mode
"""


async def fetch_pnl_by_mode_7d() -> tuple[dict[str, float], dict[str, float]]:
    """Return ({detector: virtual_pnl_7d}, {detector: live_pnl_7d}).

    Live = mode IN ('maker','taker'). Virtual = mode='virtual'.
    """
    rows = await fetch(_PNL_BY_MODE_7D_SQL)
    virtual_pnl: dict[str, float] = {}
    live_pnl: dict[str, float] = {}
    for r in rows:
        det = r["detector"]
        pnl = float(r["pnl_usd_7d"] or 0)
        if r["execution_mode"] == "virtual":
            virtual_pnl[det] = virtual_pnl.get(det, 0.0) + pnl
        elif r["execution_mode"] in ("maker", "taker"):
            live_pnl[det] = live_pnl.get(det, 0.0) + pnl
    return virtual_pnl, live_pnl


# ─── Signals/day shadow rate (для /v2/detectors lifecycle.shadow) ────────────


_SIGNALS_PER_DAY_7D_SQL = """
SELECT ds.detector,
       COUNT(*)::DOUBLE PRECISION / 7.0 AS signals_per_day_7d
  FROM detector_signals ds
 WHERE ds.generated_at >= now() - interval '7 days'
 GROUP BY ds.detector
"""


async def fetch_signals_per_day_7d() -> dict[str, float]:
    """Return {detector: signals_per_day} for last 7d window."""
    rows = await fetch(_SIGNALS_PER_DAY_7D_SQL)
    return {r["detector"]: float(r["signals_per_day_7d"] or 0) for r in rows}


# ─── Virtual fingerprint: per-detector TP/SL params (30d aggregation) ────────
#
# Используется в honest promotion gates v2: вместо stylized +5/−15 для shadow
# TP/SL симуляции, берём фактические avg_tp_pnl/avg_sl_pnl из virtual exec.
# Это значимо точнее для ML-driven детекторов где TP fires на меньших move'ах
# (STANDALONE shadow stylized −4% vs real virtual +58% — большой mismatch).
#
# Окно 30d — компромисс между актуальностью и sample size. У низко-активных
# детекторов (D26 settled=1, D29 settled=0) fingerprint будет low-confidence:
# helper возвращает поле n_settled чтобы caller мог решить fallback на
# stylized params для shadow-only детекторов.

_VIRTUAL_FINGERPRINTS_30D_SQL = """
SELECT
    eo.detector,
    COUNT(*) FILTER (WHERE eo.status = 'virtual_settled') AS n_settled,
    COUNT(*) FILTER (WHERE eo.exit_reason = 'take_profit') AS n_tp,
    COUNT(*) FILTER (WHERE eo.exit_reason = 'stop_loss') AS n_sl,
    AVG(eo.net_pnl) FILTER (WHERE eo.exit_reason = 'take_profit')::float AS avg_tp_pnl,
    AVG(eo.net_pnl) FILTER (WHERE eo.exit_reason = 'stop_loss')::float AS avg_sl_pnl,
    AVG(ABS(eo.exit_price - eo.fill_price)) FILTER (
        WHERE eo.exit_reason = 'take_profit' AND eo.exit_price IS NOT NULL
    )::float AS avg_tp_move_abs,
    AVG(ABS(eo.exit_price - eo.fill_price)) FILTER (
        WHERE eo.exit_reason = 'stop_loss' AND eo.exit_price IS NOT NULL
    )::float AS avg_sl_move_abs
FROM execution_orders eo
WHERE eo.execution_mode = 'virtual'
  AND eo.created_at > now() - INTERVAL '30 days'
  -- Исключаем re-entry: иначе avg_tp_pnl/avg_sl_pnl (вход honest promotion gates v2)
  -- искажены повторным скальпом одного event. См. standalone_virtual_pnl_inflation_2026-06-04.
  AND COALESCE((eo.meta->>'reentry_excluded')::bool, false) = false
GROUP BY eo.detector
"""


async def fetch_virtual_fingerprints() -> dict[str, dict[str, Any]]:
    """Per-detector TP/SL fingerprint из последних 30d virtual exec.

    Returns {detector: {n_settled, n_tp, n_sl, avg_tp_pnl, avg_sl_pnl,
                        avg_tp_move_abs, avg_sl_move_abs,
                        break_even_wr, tp_rate, sl_rate}}.

    n_settled = 0 значит детектор ещё не в virtual или нет settled trades —
    caller использует stylized fallback для shadow promotion gates.
    """
    rows = await fetch(_VIRTUAL_FINGERPRINTS_30D_SQL)
    out: dict[str, dict[str, Any]] = {}
    for r in rows:
        # Defensive: _VIRTUAL_FINGERPRINTS_30D_SQL всегда отдаёт n_settled
        # (COUNT с GROUP BY), но строка из иного источника (mock/edge) без
        # этого поля не должна валить весь endpoint — skip, не KeyError.
        if "n_settled" not in r:
            continue
        n_settled = int(r["n_settled"] or 0)
        if n_settled == 0:
            continue
        avg_tp = float(r["avg_tp_pnl"] or 0)
        avg_sl = float(r["avg_sl_pnl"] or 0)
        # break_even_wr = |avg_sl| / (|avg_sl| + |avg_tp|)
        # Если avg_tp+avg_sl = 0 (нет ни TP ни SL fires) — fallback к None.
        denom = abs(avg_sl) + abs(avg_tp)
        be_wr = round(abs(avg_sl) / denom * 100, 1) if denom > 0 else None
        out[r["detector"]] = {
            "n_settled": n_settled,
            "n_tp": int(r["n_tp"] or 0),
            "n_sl": int(r["n_sl"] or 0),
            "avg_tp_pnl": avg_tp,
            "avg_sl_pnl": avg_sl,
            "avg_tp_move_abs": float(r["avg_tp_move_abs"] or 0),
            "avg_sl_move_abs": float(r["avg_sl_move_abs"] or 0),
            "tp_rate_pct": round(100.0 * int(r["n_tp"] or 0) / n_settled, 1),
            "sl_rate_pct": round(100.0 * int(r["n_sl"] or 0) / n_settled, 1),
            "break_even_wr_pct": be_wr,
        }
    return out


# ─── Promotion gate computation (Shadow → Virtual) ───────────────────────────
#
# Honest gates v3 (2026-05-27, после deep-research):
# см. research/shadow-gates-methodology-2026-05/2026-05-27_validation.md
#
# Legacy gates (G1-G3) сохранены для backward-compat: UI/Telegram пока на них
# смотрят. Honest gates v2 → v3 — same payload key gates_v2 чтобы не сломать
# контракт, но threshold-ы и формулы перерабатаны.
#
# v3 vs v2 изменения:
#   - Sample threshold 30 → 40 (Wilson valid для prediction markets, A4)
#   - Catastrophic 5% → 3% (tighter tail risk, A4)
#   - BE WR fallback 50% → payout-aware (NO-fade с payout 8-10× → BE WR 10-12%)
#   - NEW G5 Sortino > 1.0 на realized_roi_net (Sharpe не применима, A1+A4)
#
# Pass rule: ≥4/5 gates → "ready_v2", 5/5 → "strong", ≤3/5 → "blocked".
#
# Why these thresholds:
# - 40 sample: Wilson lower bound валидно для prediction markets (А4 critique)
# - 3% catastrophic: разделяет D38-class extreme flip от STANDALONE-class ML errors
# - Payout-aware BE WR: NO-fade детекторы (D7b, D5) имеют payout 8-10×,
#   у них break-even WR не 50% а 1/(1+payout) ≈ 10-12%
# - Sortino > 1.0: downside-vol-aware (vs Sharpe который assumes Gaussian),
#   industry standard MAR=0


_GATES_V2_PASS_THRESHOLD = 4  # ≥4/5 → ready (v3 имеет 5 gates)

# Sample size threshold для всех G1-G3 (terminal trades).
# v3 2026-05-27: 30 → 40 потому что Wilson lower bound на prediction markets
# (correlated within event, heavy tails) требует N≥40 для validity (см.
# decisions/wilson_min_sample_size.md).
_SAMPLE_SIZE_THRESHOLD = 40

# Tail risk threshold: catastrophic_losses / terminal должно быть < 3%.
# v3 2026-05-27: 5% → 3%. D38 на borderline даёт ~5% catastrophic.
# Хотим reject D38-class, accept STANDALONE-class (~10% catastrophic но
# profit на average через TP/SL asymmetry).
_CATASTROPHIC_TAIL_THRESHOLD_PCT = 3.0

# Sortino target — minimum acceptable. Industry standard для retail/HFT.
# Sortino = mean(R) / √(downside_semivariance). MAR (minimum acceptable
# return) = 0. >1.0 значит positive expectancy достаточно для risk-adjusted.
_SORTINO_THRESHOLD = 1.0

# Издержки входа в п.п. цены, добавляемые к break-even WR в G4.
# 2.0 = минимальный half-spread по категориям (sports, app/core/slippage._HALF_SPREAD);
# у остальных 2.5-3.5%. Берём НИЖНЮЮ границу осознанно: shadow-агрегат идёт по
# детектору, а не по рынку, категории там нет — а гейт не должен заваливать
# детектор из-за завышенной оценки издержек. Fee (0.6-8% в зависимости от entry)
# не добавляем по той же причине: без per-market fee_schedule оценка была бы
# гаданием. То есть порог заведомо мягкий — это нижняя планка, не точная.
_ENTRY_COST_PCT = 2.0

# Demote v4 2026-05-29: переехал на signal-level economics (см.
# evaluate_demotion_candidates) — переиспользует _SAMPLE_SIZE_THRESHOLD (40) и
# те же G2/G5 gates, что promote. Старые order-level пороги
# (_DEMOTE_NET_PNL_THRESHOLD_USD=-500, _DEMOTE_MIN_N_SETTLED=30) удалены: order
# pnl_usd надут paper EARLY_EXIT → порог никогда не срабатывал на STANDALONE.


def compute_sortino(mean_roi_net: float | None, sum_neg_sq: float, terminal: int) -> float | None:
    """Sortino = mean(R) / √(Σmin(R,0)² / n). MAR=0 (Sortino 1991).

    Shared между promote shadow→virtual (compute_promotion_gates G5) и
    promote virtual→live (promote_gates G1) — одна честная downside-aware метрика.
    Degenerate: нет downside (sum_neg_sq=0) → +inf если mean>0, иначе None.
    Sharpe не применима — см. decisions/sharpe_not_applicable.md.
    """
    import math

    if terminal <= 0 or mean_roi_net is None:
        return None
    if sum_neg_sq > 0:
        downside_vol = math.sqrt(sum_neg_sq / terminal)
        if downside_vol > 0:
            return round(float(mean_roi_net) / downside_vol, 3)
        return None
    # sum_neg_sq == 0: нет downside variance
    return float("inf") if float(mean_roi_net) > 0 else None


def compute_promotion_gates(
    shadow_data: dict[str, Any] | None,
    backtest: dict[str, Any] | None,
    fingerprint: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return per-detector promotion readiness payload.

    Args:
        shadow_data: row from fetch_shadow_signals_7d() для конкретного det
                     (None если 0 signals в 7d).
        backtest: row from fetch_backtest_map() (None если нет canonical md).
        fingerprint: row from fetch_virtual_fingerprints() (None если детектор
                     ещё не в virtual или нет settled trades за 30d).

    Returns dict с двумя наборами gates:
      - gates / ready / gates_passed (legacy 3-gate, backward-compat для UI)
      - gates_v2 / ready_v2 / gates_v2_passed (honest economics gates)
    Не включает 'detector'/'stage' — caller добавляет сам.
    """
    sd = shadow_data or {}
    distinct_events = int(sd.get("distinct_events_7d") or 0)
    terminal = int(sd.get("terminal_7d") or 0)
    wins = int(sd.get("wins_7d") or 0)
    recent_wr = round(wins / terminal * 100, 1) if terminal > 0 else None

    canonical_wr = backtest.get("wr_pct") if backtest else None
    wr_delta = round(recent_wr - canonical_wr, 1) if recent_wr is not None and canonical_wr is not None else None

    # --- Legacy gates G1-G3 (backward-compat для UI/Telegram info) ---
    gate1 = distinct_events >= 30
    gate2 = (
        wr_delta is not None and abs(wr_delta) <= 5.0 if recent_wr is not None and canonical_wr is not None else None
    )
    gate3 = backtest is not None and backtest.get("verdict") == "PASS"
    gates_passed = sum(1 for g in (gate1, gate2, gate3) if g is True)

    # --- Honest gates v3 (real money decision) ---
    sum_roi_net = float(sd.get("sum_roi_net_7d") or 0)
    mean_roi_net = sd.get("mean_roi_net_7d")
    sum_neg_sq = float(sd.get("sum_neg_sq_roi_7d") or 0)
    median_entry = sd.get("median_entry_price_7d")

    # G1 sample: terminal ≥ 40 (Wilson valid threshold, см. decisions/wilson_min_sample_size.md)
    g1_v2 = terminal >= _SAMPLE_SIZE_THRESHOLD

    # G2 net PnL: sum_roi_net_7d > 0.
    # realized_roi_net учитывает fees+slippage hold-to-resolution. Shadow proxy
    # для virtual PnL (без TP/SL exit). Может underпредsказывать ML детекторы
    # (STANDALONE shadow −4% → virtual +58%). Это известная ограниченность v3 —
    # см. Phase 3 в research/shadow-gates-methodology-2026-05/2026-05-27_validation.md
    g2_v2 = g1_v2 and sum_roi_net > 0

    # G3 tail risk: catastrophic_losses < 3% от terminal.
    # v3 tightening 5% → 3%. Защищает от D38-class flip risk (entry NO@0.07 →
    # market к 1.0 = ~−93% loss). catastrophic = realized_roi_net < −80%
    # (порог в %, см. UNITS FIX 2026-05-28 в _SHADOW_SIGNALS_7D_SQL).
    catastrophic = int(sd.get("catastrophic_losses_7d") or 0)
    cat_pct = round(100.0 * catastrophic / terminal, 2) if terminal > 0 else None
    g3_v2 = g1_v2 and cat_pct is not None and cat_pct < _CATASTROPHIC_TAIL_THRESHOLD_PCT

    # G4 break-even WR: Wilson lower bound фактического WR > break-even с издержками.
    #
    # Фикс 2026-08-10 (research/favorite-buying-audit-2026-08-10.md §4), два бага:
    #
    # 1. Формула BE была перевёрнута: `100/(1+payout)` при `payout = 1/entry`.
    #    Это верно, когда payout — ПРИБЫЛЬ, но здесь передавался валовой возврат.
    #    Для entry 0.895 давало BE 47.2% вместо 89.5% — занижение на 42пп, и
    #    ошибка росла с ценой входа, то есть била именно по покупателям фаворитов.
    #    При hold-to-resolution BE тривиален: платим `entry` за шар, который
    #    вернёт $1, значит нужен WR > entry (плюс издержки).
    #
    # 2. Приоритетный источник (TP/SL fingerprint) мёртв с 2026-06-29, когда
    #    отключили ранние выходы: 0 TP/SL за 30д → be_wr=None → всегда падали
    #    в сломанный fallback, о чём код не знал. Теперь fingerprint применяется
    #    только если TP/SL реально были.
    #
    # Плюс сравниваем не точечный WR, а Wilson lower bound: у фаворитов WR ≈ цене
    # входа, точечная оценка садится на границу и шум решает исход гейта.
    # На живых данных 7д старая версия пропускала 5/5 детекторов (включая
    # STANDALONE с −89231 roi-пп), новая — 1/5 (D3, единственный прибыльный).
    fp = fingerprint or {}
    be_wr = fp.get("break_even_wr_pct") if (fp.get("n_tp") or 0) + (fp.get("n_sl") or 0) > 0 else None
    be_wr_source = "virtual_fingerprint" if be_wr is not None else None
    if be_wr is None and median_entry is not None and 0 < float(median_entry) < 1:
        # hold-to-resolution: BE = entry + издержки входа (fee + half-spread).
        entry = float(median_entry)
        be_wr = round(entry * 100.0 + _ENTRY_COST_PCT, 1)
        be_wr_source = "entry_plus_costs"
    if be_wr is None:
        be_wr = 50.0
        be_wr_source = "fallback_50"
    # Wilson lower bound: доказанный минимум WR, а не наблюдённая точка.
    wr_lower = None
    if terminal > 0:
        wr_lower = round(wilson_score_interval(wins, terminal)[0] * 100.0, 1)
    g4_v2 = wr_lower is not None and wr_lower > be_wr

    # G5 Sortino > 1.0 на realized_roi_net (NEW v3). Shared helper (см.
    # compute_sortino) — та же метрика, что promote virtual→live G1.
    sortino = compute_sortino(mean_roi_net, sum_neg_sq, terminal)
    g5_v2 = sortino is not None and sortino > _SORTINO_THRESHOLD

    gates_v2_passed = sum(1 for g in (g1_v2, g2_v2, g3_v2, g4_v2, g5_v2) if g is True)
    ready_v2 = gates_v2_passed >= _GATES_V2_PASS_THRESHOLD

    return {
        "shadow_7d": {
            "total_signals": int(sd.get("total_7d") or 0),
            "resolved": int(sd.get("resolved_7d") or 0),
            "terminal": terminal,
            "wins": wins,
            "distinct_events": distinct_events,
            "distinct_markets": int(sd.get("distinct_markets_7d") or 0),
            "recent_wr_pct": recent_wr,
            # sum_roi_net/mean_roi_net уже в ПРОЦЕНТАХ (SQL SUM(realized_roi_net),
            # а realized_roi_net = net_pnl/notional*100 в _realized_roi_net_pct).
            # НЕ домножать на 100 — это завышало promote-дашборд в 100× (fix 2026-05-29).
            "sum_roi_net_pct": round(sum_roi_net, 1),
            "mean_roi_net_pct": round(float(mean_roi_net), 2) if mean_roi_net is not None else None,
            "sortino": sortino if sortino != float("inf") else "inf",
            "catastrophic_losses": catastrophic,
            "catastrophic_pct": cat_pct,
            "median_entry_price": round(float(median_entry), 4) if median_entry is not None else None,
        },
        "canonical": {
            "verdict": backtest.get("verdict") if backtest else None,
            "canonical_wr_pct": canonical_wr,
            "honest_haircut_factor": backtest.get("honest_haircut_factor") if backtest else None,
            "honest_daily_usd_at_5k": backtest.get("honest_daily_usd_at_5k") if backtest else None,
        },
        "virtual_fingerprint": (
            {
                "n_settled": fp.get("n_settled"),
                "tp_rate_pct": fp.get("tp_rate_pct"),
                "sl_rate_pct": fp.get("sl_rate_pct"),
                "avg_tp_pnl": fp.get("avg_tp_pnl"),
                "avg_sl_pnl": fp.get("avg_sl_pnl"),
                "break_even_wr_pct": fp.get("break_even_wr_pct"),
            }
            if fingerprint
            else None
        ),
        "gates": {
            "n_events": {
                "passed": gate1,
                "value": distinct_events,
                "threshold": 30,
                "label": "≥30 distinct events 7d",
            },
            "wr_match": {
                "passed": gate2,
                "value": recent_wr,
                "delta": wr_delta,
                "canonical": canonical_wr,
                "threshold_pp": 5.0,
                "label": "WR в ±5pp от canonical",
            },
            "verdict": {
                "passed": gate3,
                "value": backtest.get("verdict") if backtest else None,
                "label": "Backtest verdict = PASS",
            },
        },
        "gates_v2": {
            "sample": {
                "passed": g1_v2,
                "value": terminal,
                "threshold": _SAMPLE_SIZE_THRESHOLD,
                "label": f"≥{_SAMPLE_SIZE_THRESHOLD} terminal signals 7d",
            },
            "net_pnl": {
                "passed": g2_v2,
                # sum_roi_net уже в % (см. shadow_7d.sum_roi_net_pct выше) — НЕ домножать на 100.
                "value_pct": round(sum_roi_net, 1),
                "label": "shadow net PnL > 0 (fees+slippage учтены)",
            },
            "tail_risk": {
                "passed": g3_v2,
                "value_pct": cat_pct,
                "threshold_pct": _CATASTROPHIC_TAIL_THRESHOLD_PCT,
                "label": f"catastrophic losses (<−80%) <{_CATASTROPHIC_TAIL_THRESHOLD_PCT}% от terminal",
            },
            "break_even_wr": {
                "passed": g4_v2,
                # value = то, что РЕШАЕТ гейт (Wilson lower bound), а не наблюдённая
                # точка: иначе дашборд показывал бы одно, а вердикт считался по другому.
                "value": wr_lower,
                "observed_wr_pct": recent_wr,
                "threshold_pct": be_wr,
                "source": be_wr_source,
                "label": "Wilson lower bound WR > break-even WR (entry + издержки)",
            },
            "sortino": {
                "passed": g5_v2,
                "value": sortino if sortino != float("inf") else "inf",
                "threshold": _SORTINO_THRESHOLD,
                "label": f"Sortino > {_SORTINO_THRESHOLD} (downside-vol-aware)",
            },
        },
        "ready": gate1 and gate2 is True and gate3,
        "gates_passed": gates_passed,
        "gates_total": 3,
        "wr_delta_pp": wr_delta,
        "ready_v2": ready_v2,
        "gates_v2_passed": gates_v2_passed,
        "gates_v2_total": 5,
    }


# ─── Shadow signal price snapshot helpers (PR-A foundation) ──────────────────
#
# detector_signal_price_snapshots table — intraday yes_price snapshots между
# signal.generated_at и signal.resolved_at. Writer cron каждые 5 минут.
#
# Используется для shadow TP/SL simulation (PR-B): replay price path и сим
# выход при достижении TP/SL thresholds per detector.

_FETCH_PRICE_PATH_SQL = """
SELECT captured_at, yes_price
  FROM detector_signal_price_snapshots
 WHERE detector_signal_id = $1
 ORDER BY captured_at
"""


async def fetch_signal_price_path(detector_signal_id: str) -> list[dict[str, Any]]:
    """Return price path для конкретного signal: list of {captured_at, yes_price}.

    Empty list если snapshots ещё не написаны (signal новый и writer не сработал).
    """
    rows = await fetch(_FETCH_PRICE_PATH_SQL, detector_signal_id)
    return [{"captured_at": r["captured_at"], "yes_price": float(r["yes_price"])} for r in rows]


_PRICE_SNAPSHOT_STATS_SQL = """
SELECT
    COUNT(*)::bigint AS total_snapshots,
    COUNT(DISTINCT detector_signal_id)::bigint AS distinct_signals,
    MIN(captured_at) AS oldest,
    MAX(captured_at) AS newest
FROM detector_signal_price_snapshots
"""


async def fetch_price_snapshot_stats() -> dict[str, Any]:
    """Return aggregate stats для health monitoring."""
    from app.services.database import fetchrow

    row = await fetchrow(_PRICE_SNAPSHOT_STATS_SQL)
    if row is None:
        return {"total_snapshots": 0, "distinct_signals": 0, "oldest": None, "newest": None}
    return dict(row)


# ─── Auto-demotion candidates: virtual losing money ──────────────────────────
#
# v4 2026-05-29: судим по SIGNAL-LEVEL realized_roi_net, симметрично promote-гейту
# (НЕ по order-level pnl_usd). Почему:
#   - order-level pnl_usd надут paper EARLY_EXIT: STANDALONE +$12K paper при
#     resolved −$717 → старый порог pnl_usd<−$500 НИКОГДА не срабатывал (баг).
#   - resolved-order метрики нежизнеспособны: STANDALONE 4 WIN/LOSS ордера,
#     D3/D14/D26/D44 = 0 (детекторы выходят через TP/SL по дизайну).
#   - signal-level realized_roi_net робастен (STANDALONE 6292 resolved сигналов)
#     и это РОВНО та экономика, что promote-гейт проверяет (G2 sum_roi_net, G5 Sortino).
# Demote-алерт = детектор в virtual, который на свежих 7d сигналах больше НЕ прошёл
# бы свою promote-экономику. См. memory [[shadow-roi-virtual-pnl]].
#
# Alert-only (не auto-action). Telegram уведомляет admin'а — manual decision
# демотировать обратно в shadow.


def evaluate_demotion_candidates(
    virtual_metrics: dict[str, dict[str, Any]],
    shadow_signals: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    """Return list of detectors deserving demotion alert.

    Args:
        virtual_metrics: result of fetch_virtual_metrics_7d() — какие детекторы
            сейчас в virtual (по которым есть settled-ордера).
        shadow_signals: result of fetch_shadow_signals_7d() — signal-level
            realized_roi_net экономика за 7d (та же, что у promote-гейта).

    Returns: list of {detector, n_terminal, sum_roi_net, sortino, reason}.
    Empty list = nothing to alert. Caller отвечает за TG notification.

    Criteria (v4):
        - terminal >= 40 (Wilson valid, не дёргать на малой выборке/шуме)
        - честная signal-level экономика провалена: sum_roi_net ≤ 0 ИЛИ Sortino < 1.0
          (т.е. детектор больше не прошёл бы promote G2/G5).
    """
    candidates = []
    for det in virtual_metrics:
        sd = shadow_signals.get(det)
        if not sd:
            continue
        gates = compute_promotion_gates(sd, backtest=None, fingerprint=None)
        gv = gates["gates_v2"]
        terminal = int(gates["shadow_7d"]["terminal"])
        if terminal < _SAMPLE_SIZE_THRESHOLD:
            continue
        g2_pass = bool(gv["net_pnl"]["passed"])
        g5_pass = bool(gv["sortino"]["passed"])
        if g2_pass and g5_pass:
            continue
        sum_roi_net = float(sd.get("sum_roi_net_7d") or 0)
        sortino = gates["shadow_7d"]["sortino"]
        candidates.append(
            {
                "detector": det,
                "n_terminal": terminal,
                "sum_roi_net": sum_roi_net,
                "sortino": sortino,
                "reason": (
                    f"virtual 7d signal economics failed: sum_roi_net={sum_roi_net:.1f}% "
                    f"(G2 {'pass' if g2_pass else 'FAIL'}), Sortino={sortino} "
                    f"(G5 {'pass' if g5_pass else 'FAIL'}) на n_terminal={terminal}"
                ),
            }
        )
    return candidates


# ─── Unified promote readiness: SSoT для TG promote callback И /live_readiness ─
#
# Audit finding (research/design-audit-2026-07-02.md, second эшелон): TG
# promote-callback (app/api/telegram.py::_evaluate_and_handle_promote) судил
# virtual→live по fetch_shadow_signals_7d() — фиксированное 7д-окно от NOW().
# С n_min=30 (settings.promote_gate_min_n_terminal) это непроходимо для
# низкочастотных детекторов (D3: ~0.37 сигн/день → ~2.6 сигналов за 7d).
# /v2/lifecycle/live_readiness (app/api/bot.py) тем временем уже использовал
# fetch_virtual_period_signals — кумулятивно от promoted_at, без 7д-потолка —
# поэтому UI и TG расходились в вердикте одного и того же детектора.
#
# evaluate_promote_readiness — единая точка входа для ОБОИХ путей:
#   - N/wins/losses (для n_min и fill/winrate Wilson-гейтов) — кумулятивно
#     от promoted_at (period), без календарного потолка.
#   - sum_roi_net/mean_roi_net/sum_neg_sq_roi/median_entry (для net_pnl-гейта,
#     Sortino, break-even WR) — recency 30д-окно (recent), НЕ all-time period.
#     Методология recent-window: all-time маскирует затухающий edge (см.
#     memory [[all-time-audit-hides-decaying-edge]]) — кумулятивная сумма ROI
#     за месяцы легко остаётся положительной, даже если детектор последние
#     недели уже сливает. period даёт объём для статистической значимости N,
#     recent — честную свежую экономику.
# Пороги/формулы (n_min, Wilson lower bounds, Sortino > 1.0, payout-aware
# break-even WR, adversarial mandatory) НЕ переизобретаются — это
# app.services.promote_gates.evaluate_promote_gates, уже SSoT для
# /live_readiness и TG. Здесь только сборка входных метрик под неё.


async def evaluate_promote_readiness(
    detector: str,
    promoted_at: Any,
    *,
    days_virtual: int,
    adversarial_pass: bool | None,
    execution_enabled: bool,
    period: dict[str, Any] | None = None,
    recent: dict[str, Any] | None = None,
    virtual_metrics: dict[str, Any] | None = None,
    min_n: int | None = None,
    wilson_confidence: float | None = None,
    fill_threshold: float | None = None,
    winrate_threshold: float | None = None,
) -> dict[str, Any]:
    """Единый расчёт promote-гейтов virtual→live для TG И UI (audit: гейт-театр).

    N/wins/losses — кумулятивно от promoted_at (fetch_virtual_period_signals).
    sum_roi_net/Sortino/median_entry — recency 30д-окно (fetch_recent_window_signals).
    n_placed/n_filled/n_would_have_filled — fetch_virtual_metrics_7d (execution_orders,
    для Wilson fill-rate gate; caller-независимый источник, тот же для TG и UI).

    Опциональные `period`/`recent`/`virtual_metrics` — уже полученный per-detector
    dict (та же shape, что значения из fetch_virtual_period_signals /
    fetch_recent_window_signals / fetch_virtual_metrics_7d). Batch-caller (UI
    live_readiness — N детекторов за один проход) передаёт их, чтобы не делать
    N лишних single-detector round-trips; single-detector caller (TG promote
    callback) может не передавать — функция сфетчит сама.

    days_virtual/adversarial_pass/execution_enabled приходят из detector_state —
    вне scope этого модуля (detector_metrics не читает detector_state напрямую),
    caller их передаёт явно.

    Returns: shape evaluate_promote_gates() — {"decision": str, "gates": dict}.
    """
    # Deferred import: promote_gates.py импортирует detector_metrics внутри своей
    # функции (та же причина) — избегаем circular import на module load.
    from app.services.promote_gates import evaluate_promote_gates

    if period is None:
        period_map = await fetch_virtual_period_signals([(detector, promoted_at)])
        period = period_map.get(detector) or {}

    if recent is None:
        recent_map = await fetch_recent_window_signals([detector])
        recent = recent_map.get(detector) or {}

    if virtual_metrics is None:
        virtual_map = await fetch_virtual_metrics_7d()
        virtual_metrics = virtual_map.get(detector) or {}

    n_terminal = int(period.get("terminal_7d") or 0)
    wins = int(period.get("wins_7d") or 0)
    losses = n_terminal - wins

    return evaluate_promote_gates(
        n_terminal=n_terminal,
        n_placed=int(virtual_metrics.get("n_placed", 0) or 0),
        n_filled=int(virtual_metrics.get("n_filled", 0) or 0),
        n_would_have_filled=int(virtual_metrics.get("n_would_have_filled", 0) or 0),
        wins=wins,
        losses=losses,
        sum_roi_net=float(recent.get("sum_roi_net_7d") or 0.0),
        mean_roi_net=recent.get("mean_roi_net_7d"),
        sum_neg_sq_roi=float(recent.get("sum_neg_sq_roi_7d") or 0.0),
        median_entry_price=recent.get("median_entry_price_7d"),
        days_virtual=days_virtual,
        adversarial_pass=adversarial_pass,
        execution_enabled=execution_enabled,
        min_n=min_n,
        wilson_confidence=wilson_confidence,
        fill_threshold=fill_threshold,
        winrate_threshold=winrate_threshold,
    )

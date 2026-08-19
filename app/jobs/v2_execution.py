"""V2 Execution job — единый путь от detector_signal до Polymarket order.

Заменяет связку:
  - pipeline_v2_shadow_executor.py (paper позиции в pipeline_v2_shadow_positions)
  - execution_job.py + execution_bot.py (live consumer на Redis queue)
  - strategy_position_manager.py (legacy producer, удалён в Phase F)

Flow per cron tick (every 1 min, crontab.conf — учащено 5→1 мин 2026-06-13):
  1. fetch_signals_for_active_pipeline()  — calibrated signals (L1 + L2 / Two-Stage)
  2. ev_sizer.size_from_calibrated_p()    — единственный sizing
  3. execution_safety.evaluate()          — kill switch, detector live gate, book/spread/drift
  4. INSERT execution_orders (intent | rejected)
  5. if EXECUTION_ENABLED && detector live  → place_order → status='placed'

Идемпотентность через двухслойный UNIQUE в БД:
  - uq_execution_orders_signal_active: один АКТИВНЫЙ (не rejected/cancelled) ордер
                                        per detector_signal_id. Transient reject
                                        (см. reject_classify.py) освобождает слот —
                                        сигнал ретраится с cooldown (Task 2, audit топ-4).
  - uq_execution_orders_open_market:   одна открытая позиция per (detector, market, side)
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
from contextvars import ContextVar
from typing import Any

import structlog

from app.analytics.notification import Notification, Severity
from app.calibration.ev_sizer import FRACTIONAL_KELLY_COLD, size_from_calibrated_p
from app.core.config import get_settings
from app.core.detector_state import get_state as get_detector_state
from app.core.open_positions import fetch_open_positions_for_risk
from app.core.peak_bank import LIVE_PEAK_BANK_KEY, update_peak_if_higher
from app.core.sizing_controller import get_live_bankroll
from app.services.database import execute, fetchrow, fetchval
from app.services.detector_signal_service import fetch_signals_for_active_pipeline
from app.services.execution_safety import (
    SafetyContext,
    check_daemon_pause,
    check_daily_loss,
    check_daily_pnl_limit_mtm,
    check_kill_switch,
    check_max_inflight_orders,
    check_max_open_positions,
    check_redis_kill_flag,
    resolve_execution_mode,
    shadow_evaluate_virtual,
    zone_drift_threshold,
)
from app.services.execution_safety import (
    evaluate as safety_evaluate,
)
from app.services.notification_bus import publish
from app.services.portfolios import (
    Portfolio,
    fetch_portfolio_for_detector,
)
from app.services.price_staleness_telemetry import log_price_staleness
from app.services.risk_engine import check_risk

logger = structlog.get_logger(__name__)

# Fix-2 (latency-аудит №3): кто вставил строку — резидентный демон или минутный
# cron-watchdog. Демон ставит 'daemon' в process_one (scripts/execution_daemon.py);
# _insert_order пишет значение в execution_orders.meta->>'source' на каждый INSERT.
EXECUTION_SOURCE: ContextVar[str] = ContextVar("execution_source", default="cron")

SHADOW_BANKROLL_USD = 1000.0  # paper money для shadow-стадии

# Virtual stage per-detector exposure cap.
# User decision 2026-05-03: каждый detector в virtual крутится максимум на $1000
# открытых positions (placed + filled). Если current open exposure + new stake > cap →
# reject signal с reason 'virtual_exposure_cap_exceeded'.
# Применяется только к virtual mode; live mode имеет свои bankroll/sizing controls.
#
# 2026-05-13: 1000→3000. ML pipeline (STANDALONE_V2_LGB) часто упирается в $1K cap.
# 2026-05-13 (later): 3000→5000. High-edge filter (≥20pp) дал WR 87.5% на edge≥100pp.
# Чтобы capture больше high-edge alpha, поднимаем per-detector cap.
# ML portfolio total cap = $5K (один detector), Rules = $8K (11 detectors).
VIRTUAL_PER_DETECTOR_EXPOSURE_CAP_USD = 5000.0

# Per-detector exposure cap overrides.
#
# Эмпирика 2026-05-19 (selection bias audit + counterfactual analysis на 30d):
#
# STANDALONE_V2_LGB (+$1393/30d net P&L, ROI +24.5%):
#   - exposure_cap rejected: n=1422, sig_WR=48.2% → winners loss
#   - counterfactual hold-to-resolve: -$1525 vs TP/SL -$864
#     → TP/SL правильно защищает microstructure edge (decays)
#   - Cap raise до $10K = больше масштаб правильно работающего детектора. KEEP.
#
#   ⚠️ УСТАРЕЛО (проверено 2026-08-04). Цифры выше описывают майский режим
#   детектора и больше не действуют. Что изменилось:
#     - Охват вырос 269 → ~1390 рынков в июне (порог confidence НЕ менялся,
#       min ~0.06 в обоих режимах) — расширение на всю вселенную рынков.
#     - Экономика перевернулась: avg net ROI май +21.4% → июнь −5.1% →
#       июль −4.7% → август −15.8%.
#     - Калибровка сломалась: в корзине confidence 0.80–0.90 (n=33380)
#       фактическая WR 0.33 — ХУЖЕ, чем в корзине 0.30–0.40. Разрыв растёт
#       вместе с уверенностью, то есть сигнал верхних корзин антипредиктивен.
#       Половина сигналов сидит именно там.
#     - Исполнение остановлено: последний execution_order 2026-06-12,
#       stage='paused' с 2026-06-24. Cap $10K сейчас ни на что не влияет.
#   Перед перезапуском исполнения ЭТИ цифры перемерить заново — expected_edge_pct
#   считается от сырой confidence (avg 165%) и сделает детектор самым
#   привлекательным в очереди на ровном месте. Разбор:
#   research/detector-inventory-2026-08.md
#
# D36_obi_pressure (-$181/30d net P&L, ROI -0.5%):
#   - exposure_cap rejected: n=86, sig_WR=52.3%
#   - counterfactual hold-to-resolve: -$346 vs TP/SL +$397
#     → TP/SL даёт +$397 на n=50! Cap raise бы выдал больше exposure → больше TP/SL plays.
#     Микроструктурный детектор, edge decays — TP/SL работает.
#   - Поэтому cap $10K OK, оставляем (был revert но передумали по counterfactual).
#
# Override semantics: detector в словаре → используем переопределённый cap.
# Detector НЕ в словаре → используем VIRTUAL_PER_DETECTOR_EXPOSURE_CAP_USD ($5K).
_DETECTOR_EXPOSURE_CAP_OVERRIDES: dict[str, float] = {
    # ⚠️ Обоснование ($1393/30d, май) устарело — см. блок выше. Детектор в paused
    # с 2026-06-24, ордеров нет с 2026-06-12: значение сейчас неактивно.
    "STANDALONE_V2_LGB": 10000.0,
    "D36_obi_pressure": 10000.0,  # 86 rejected с sig_WR 52.3%, TP/SL даёт +$397 на 50 settled
    # D38 promoted to virtual 2026-05-24 — Wilson lower 86.8% на n=430 (signal_resolve).
    # Live rate: 101 cap_rejects / 8 fills за 1h → 92% потеря D38 signals по cap.
    # Поднимаем до $15K чтобы capture больше confirmed-edge plays.
    # D38 sized $750/signal × ~20 параллельных = $15K headroom.
    "D38_resolution_lag": 15000.0,
}


def _exposure_cap_for(detector: str) -> float:
    """Return per-detector virtual exposure cap. Falls back to VIRTUAL_PER_DETECTOR_EXPOSURE_CAP_USD."""
    return _DETECTOR_EXPOSURE_CAP_OVERRIDES.get(detector, VIRTUAL_PER_DETECTOR_EXPOSURE_CAP_USD)


# Per-detector category blacklist.
# Эмпирика 2026-05-18 (14d virtual, см. memory): Sports — самая убыточная категория
# в проде (-$5,818 за 14d, ROI -9.3%). 9 из 11 детекторов в Sports негативны.
# Единственное исключение — D36_obi_pressure (около-нулевой ROI +0.9%, его не блочим).
#
# Reject-логика: если у signal'a category в blacklist → status='rejected' с reason
# 'category_blacklist'. Сигнал сохраняется в БД для audit, но trade не открывается.
#
# Override: чтобы добавить категорию в whitelist для какого-то детектора —
# убрать его из set'а. Это per-detector, чтобы дать гибкость.
#
# D3_cross_mkt_arb исключён из Sports 2026-07-05 (research/fable-gates-audit-2026-07-03/,
# human decision): структурный арбитраж — другая механика риска, чем direction bet.
# Контрфактика по detector_signals устойчиво позитивна и усиливается: 60d/170 сигналов,
# последние 3 недели — 0 LOSS, avg ROI net 14-44%/неделя.
#
# D38_resolution_lag × {Crypto, World & Economy} добавлены 2026-07-07 (human decision,
# денежный аудит воронки): honest virtual P&L 14d по формуле прод-settle —
# W&E −$2,127 на $26.4k оборота, Crypto −$1,217 на $4.5k. Совпадает с топом
# category_health-кандидатов (#667, D38×Crypto ROI −0.57). Sports/Tech&AI у D38 тоже
# в минусе, но мельче/на малом обороте — оценить при пересмотре ~2026-07-21.
_CATEGORY_BLACKLIST: dict[str, frozenset[str]] = {
    "STANDALONE_V2_LGB": frozenset({"Sports"}),
    "D38_resolution_lag": frozenset({"Crypto", "World & Economy"}),
    # D13_retail_confidence_fade — micro-positive в Sports (+$146 на n=3, держим).
    # D36_obi_pressure — около-нулевой в Sports (+$297 на n=90), держим.
}


def _is_category_blacklisted(detector: str, category: str | None) -> bool:
    """True если detector НЕ должен торговать в данной категории."""
    if not category:
        return False
    bl = _CATEGORY_BLACKLIST.get(detector)
    return bl is not None and category in bl


# Temperature-рынки: структурная adverse selection (human decision 2026-07-07,
# см. memory temperature-markets-adverse-selection-2026-07-07). Резолв по METAR
# конкретной метеостанции — контрагент с публичным метео-фидом ЗНАЕТ исход раньше
# любого price/flow-сигнала. Virtual-итог всех детекторов: 206 ставок, $79.8k stake,
# net −$12,328 (ROI −15.4%), в минусе ВСЕ кроме шумового D3 (+2% на 43).
# Срез прячется ВНУТРИ категории World & Economy — категорийный блэклист его не
# видит, поэтому матчим по тексту вопроса. Lock-логика METAR валидирована 178/178
# против фактических резолвов (memory temperature-lockin-edge-measured-2026-07-07).
#
# D3_cross_mkt_arb исключён: структурный арбитраж (та же логика, что вынос D3 из
# Sports-блэклиста 2026-07-05), temp-результат не в минусе (virtual +$365/43,
# live −$6/8). Basket-путь (D29/D40) гейт не проходит by design — тоже структурный.
_TEMPERATURE_GATE_EXEMPT: frozenset[str] = frozenset({"D3_cross_mkt_arb"})

_TEMPERATURE_QUESTION_MARKERS = ("temperature", "°f", "°c", "ºf", "ºc")


def _is_temperature_market(question: str | None) -> bool:
    """True если рынок — температурный (по тексту вопроса, как в денежном аудите)."""
    if not question:
        return False
    q = question.lower()
    return any(marker in q for marker in _TEMPERATURE_QUESTION_MARKERS)


# Гейты на пересмотре (research/fable-gates-audit-2026-07-03/).
# D3_cross_mkt_arb снят из shadow-таргетов 2026-07-05 вместе с исключением из
# _CATEGORY_BLACKLIST — гейта, который он отслеживал, для D3 больше нет.
# D38_resolution_lag добавлен 2026-07-07 вместе с блэклистом Crypto/W&E: книга на
# момент reject нужна для honest-контрфактики пересмотра (~2026-07-21).
_GATE_SHADOW_TARGETS: dict[str, frozenset[str]] = {
    "D38_resolution_lag": frozenset({"category_blacklist"}),
}


def _gate_shadow_enabled(detector: str, gate: str) -> bool:
    return gate in _GATE_SHADOW_TARGETS.get(detector, frozenset())


async def _category_health_candidate(detector: str, category: str | None) -> bool:
    """True если ячейка detector×category — blacklist-кандидат по category_health.

    Читает Redis-ключ, который материализует cron app/jobs/category_health.py
    (event-кластеризованный ROI за 60д, events>=30, roi<0 И median_event_roi<0).
    Fail-open: нет Redis / нет ключа / битый JSON → False — money-path
    не зависит от телеметрии.
    """
    if not category:
        return False
    try:
        from app.core.cache import get_redis
        from app.jobs.category_health import CANDIDATES_KEY

        redis = await get_redis()
        if redis is None:
            return False
        raw = await redis.get(CANDIDATES_KEY)
        if not raw:
            return False
        return f"{detector}:{category}" in json.loads(raw)
    except Exception:  # noqa: BLE001 — fail-open, телеметрия не роняет money-path
        return False


# Per-detector entry-side fill-price guardrail.
# Эмпирика 2026-05-18 (14d virtual):
#   D3_cross_mkt_arb NO:
#     - mid zone [0.10, 0.60]: +$3,346 net на 24 trades (sweet spot)
#     - extreme low (<0.10): -$1,223 на 3 trades (avg -$408)
#     - high (>=0.60):       -$2,766 на 13 trades
#   Решение: рестриктнуть D3 на entry_price ∈ [0.10, 0.60] (entry-side координаты).
#
# Контракт: entry_price в координатах detector signal ([0..1]). Для NO side
# сигнал ставит entry = 1 − yes_at_signal, для YES — entry = yes_at_signal.
# (см. detector_signal_service.py: 'entry_price' уже side-adjusted.)
#
# Format: {detector: (min_inclusive, max_inclusive)}. None в любой границе = open.
_FILL_PRICE_RANGE: dict[str, tuple[float, float]] = {
    # D3 cross-market arb: только mid-zone, [0.10, 0.60] entry-side.
    "D3_cross_mkt_arb": (0.10, 0.60),
}


def _is_entry_price_out_of_range(detector: str, entry_price: float | None) -> bool:
    """True если entry_price вне разрешённого диапазона для детектора.

    Сигналы за границами — reject (status='rejected', reason='entry_price_out_of_range').
    """
    if entry_price is None:
        return False
    rng = _FILL_PRICE_RANGE.get(detector)
    if rng is None:
        return False
    lo, hi = rng
    return entry_price < lo or entry_price > hi


# Per-detector cal_p zone blacklist.
# Эмпирика 2026-05-19 (30d virtual, settled orders only — см. notes ниже):
#   Глобально по cal_p зонам:
#     <0.15:       n=31  ROI=-25.4%  PnL=-$2,219  (главные источники: D31 -$1,751, D10b -$375)
#     0.50-0.70:   n=239 ROI=-5.6%   PnL=-$4,265  (главные: D37 -$1,733, V2_LGB -$1,532)
#     ≥0.85:      n=21  ROI=-1.5%   PnL=-$850   (главный: D3 -$786)
#   Прибыльные зоны (keep):
#     0.15-0.30:   n=31  ROI=+117%   PnL=+$569
#     0.30-0.50:   n=49  ROI=+18.7%  PnL=+$791
#     0.70-0.85:   n=22  ROI=+12%    PnL=+$966
#
# Стратегия: per-(detector, zone) blacklist для материальных убытков (>$300 на n≥5).
# Зоны = list of (lo, hi) intervals в координатах calibrated_p [0..1].
#
# Override semantics: detector в blacklist → проверяем zones по списку;
# если cal_p попадает в любой из intervals — reject с reason 'cal_p_zone_blacklist:detector:zone'.
#
# Format: {detector: list[(lo_inclusive, hi_exclusive)]}.
_CAL_P_ZONE_BLACKLIST: dict[str, list[tuple[float, float]]] = {
    "STANDALONE_V2_LGB": [(0.50, 0.70)],  # -$1,532 на n=66
    "D3_cross_mkt_arb": [(0.85, 1.01)],  # -$786 на n=20 (≥0.85 fee/slippage)
}


def _is_cal_p_zone_blacklisted(detector: str, cal_p: float | None) -> tuple[bool, str | None]:
    """True если (detector, cal_p) попадает в blacklisted zone.

    Returns: (is_blacklisted, zone_label) — label для rejection_reason.
    """
    if cal_p is None:
        return (False, None)
    zones = _CAL_P_ZONE_BLACKLIST.get(detector)
    if zones is None:
        return (False, None)
    for lo, hi in zones:
        if lo <= cal_p < hi:
            return (True, f"{lo:.2f}-{hi:.2f}")
    return (False, None)


_OPEN_EXPOSURE_SQL = """
SELECT COALESCE(SUM(stake), 0)::float
  FROM execution_orders
 WHERE detector = $1
   AND execution_mode = 'virtual'
   AND status IN ('virtual_placed', 'virtual_filled')
"""

# Phase 3: per-portfolio open exposure (sum across все detectors в portfolio).
_PORTFOLIO_OPEN_EXPOSURE_SQL = """
SELECT COALESCE(SUM(eo.stake), 0)::float
  FROM execution_orders eo
  JOIN detector_state ds ON ds.detector = eo.detector
 WHERE ds.portfolio_id = $1
   AND eo.execution_mode = 'virtual'
   AND eo.status IN ('virtual_placed', 'virtual_filled')
"""


async def _virtual_exposure_remaining(detector: str) -> float:
    """Return remaining $ headroom under per-detector exposure cap.

    Cap=$5K by default, $10K для V2_LGB и D36 (см. _DETECTOR_EXPOSURE_CAP_OVERRIDES).
    """
    used = await fetchval(_OPEN_EXPOSURE_SQL, detector)
    cap = _exposure_cap_for(detector)
    return max(0.0, cap - float(used or 0))


# Event-level re-entry guard (2026-06-04, см. memory
# standalone_virtual_pnl_inflation_2026-06-04).
#
# Проблема: STANDALONE_V2_LGB заходит в один neg-risk event 10-32 раза за 16ч.
# TP=+30% (position_manager) срабатывает за ~12 мин → позиция virtual_settled →
# UNIQUE constraint uq_execution_orders_open_market освобождается → predictor
# (cron 30 мин) даёт новый сигнал → вход снова. Артефакт двухслойный: повтор
# внутри (market_id, side) И между рынками одного event (predictor скорит каждый
# market независимо, не зная о neg-risk корреляции). Раздуло virtual P&L:
# raw +$47.5K/14д vs честный event-dedup +$663.
#
# Решение (вариант A, held-to-resolution): один вход на event пока событие
# активно. Считаем ЛЮБУЮ virtual-позицию (placed/filled/settled — НЕ cancelled),
# т.к. сюда доходят только сигналы нерезолвл. рынков (fetch фильтрует
# resolved=false), значит событие ещё активно. Эталон — live D3 (d3_scan.py)
# дедупит по event_id. Guard per-detector: открытая позиция D3 на event не
# блокирует STANDALONE на том же event (независимые торговые единицы).
#
# Безопасность (verified на проде 14д): max одновременных позиций на event у
# всех детекторов кроме STANDALONE = 1, поэтому guard для них no-op (raw≈guarded).
#
# Live-aware (2026-06-04): guard скоупится по mode-family текущего сигнала.
# Virtual-сигнал смотрит на прошлые virtual-позиции; live-сигнал — на прошлые
# live-позиции (maker/taker). Кросс-режим НЕ блокирует (virtual и live — разные
# арены). Раньше SQL имел execution_mode='virtual' хардкодом → live re-entry НЕ
# гейтился (если STANDALONE промоутнут в live, он бы повторял скальп реальными
# деньгами). Теперь оба режима дедуплены, и промоут-гейт оценивает ровно то
# поведение, как live будет торговать.
_EVENT_REENTRY_VIRTUAL_SQL = """
SELECT COUNT(*)::int
  FROM execution_orders
 WHERE detector = $1
   AND event_id = $2
   AND execution_mode = 'virtual'
   AND status IN ('virtual_placed', 'virtual_filled', 'virtual_settled')
"""
# Live mode: execution_mode maker/taker; статусы активной/закрытой live-позиции.
# intent = создана но ещё не размещена; placed/filled = в рынке; settled = резолв.
# rejected/cancelled НЕ считаем (несостоявшийся вход не должен блокировать event).
_EVENT_REENTRY_LIVE_SQL = """
SELECT COUNT(*)::int
  FROM execution_orders
 WHERE detector = $1
   AND event_id = $2
   AND execution_mode IN ('maker', 'taker')
   AND status IN ('intent', 'placed', 'filled', 'settled')
"""


async def _event_reentry_blocked(detector: str, event_id: str | None, live_mode: bool = False) -> bool:
    """True если на (detector, event_id) уже была позиция в ТЕКУЩЕЙ mode-family → re-entry.

    live_mode=False (default): смотрит на прошлые virtual-позиции (placed/filled/settled).
    live_mode=True: смотрит на прошлые live-позиции (maker/taker, intent/placed/filled/settled).
    event_id None/empty → False (нет события — нечего гейтить, БД не трогаем).
    """
    if not event_id:
        return False
    sql = _EVENT_REENTRY_LIVE_SQL if live_mode else _EVENT_REENTRY_VIRTUAL_SQL
    count = await fetchval(sql, detector, event_id)
    return bool(count and int(count) > 0)


async def _portfolio_exposure_used(portfolio_id: str) -> float:
    """Return current $ open exposure summed across portfolio's detectors."""
    used = await fetchval(_PORTFOLIO_OPEN_EXPOSURE_SQL, portfolio_id)
    return float(used or 0)


async def _portfolio_exposure_remaining(portfolio_id: str, cap_usd: float) -> float:
    """Return remaining $ headroom под portfolio cap (Phase 3)."""
    used = await _portfolio_exposure_used(portfolio_id)
    return max(0.0, cap_usd - used)


_INSERT_ORDER_SQL = """
INSERT INTO execution_orders (
    detector, detector_signal_id, market_id, event_id, side,
    execution_mode, signal_price, calibrated_p, meta_p, ev_pct,
    order_type, limit_price, stake, bankroll_at_entry,
    status, rejection_reason, meta,
    simulation_model_version, queue_position_at_placement, as_penalty_usd, effective_fill_price,
    is_transient_reject
)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17,
        $18, $19, $20, $21, $22)
ON CONFLICT (detector_signal_id) WHERE status NOT IN ('rejected', 'cancelled') DO NOTHING
RETURNING id
"""


def _log_stake_staleness_shadow(
    *,
    detector: str,
    sig: dict,
    live_price: float | None,
    stale_stake_usd: float,
    bankroll: float,
    fractional_kelly: float,
) -> dict | None:
    """Fix-3 шаг 1 (latency-аудит №3): shadow «stake до/после».

    Kelly-stake считается по stale sig["entry_price"], а live-цена из
    book-fetch'а safety gate (verdict.current_price) на stake не влияет —
    правит только limit_price. Здесь пересчитываем sizer с live-ценой и
    ТОЛЬКО логируем разницу; decision не трогаем. Пересчёт stake (шаг 2) —
    отдельное решение после накопления данных этой телеметрии.

    live_price=None (virtual-путь: safety gate книгу не фетчит) → no-op.
    Любое исключение глотается — телеметрия не должна ронять money-path.
    """
    if live_price is None:
        return None
    try:
        shadow = size_from_calibrated_p(
            calibrated_p=sig["meta_p"],
            entry_price=float(live_price),
            bankroll_usd=bankroll,
            category=sig.get("category"),
            fee_schedule=sig.get("fee_schedule"),
            fractional_kelly=fractional_kelly,
        )
        stake_live = float(shadow.stake_usd) if shadow.stake_usd is not None else 0.0
        payload: dict[str, Any] = {
            "detector": detector,
            "market_id": str(sig["market_id"]),
            "entry_price": float(sig["entry_price"]),
            "live_price": float(live_price),
            "stake_stale": stale_stake_usd,
            "stake_live": stake_live,
            "stake_delta_usd": round(stake_live - stale_stake_usd, 4),
        }
        if shadow.stake_usd is None:
            payload["shadow_skip_reason"] = shadow.reason
        logger.info("v2_exec_stake_staleness_shadow", **payload)
        return payload
    except Exception as exc:
        logger.warning("v2_exec_stake_staleness_shadow_failed", error=str(exc)[:200])
        return None


async def _apply_risk_engine(
    *,
    proposed_stake: float,
    market_id: str,
    event_id: str | None,
    category: str,
    bankroll: float,
) -> tuple[float, str, list[str]]:
    """Phase 1 Stage 2: vызов risk_engine.check_risk для live mode.

    Returns:
        (approved_stake, rejection_reason, reductions)
        - approved_stake = 0.0 → caller записывает rejected order с reason.
        - approved_stake < proposed_stake → caller использует reduced size.
        - approved_stake == proposed_stake → no-op.
        - reason пустой если просто reduce; "risk_block:..." если is_blocked.

    peak_bank: ОТДЕЛЬНЫЙ ключ (LIVE_PEAK_BANK_KEY) от daemon-exposure знаменателя
    (PEAK_BANK_KEY, risk_daemon.py — #621, не трогать). Задача D (2026-07-03):
    общий ключ застревал на исторических максимумах (из эпохи большого
    банка) → вечная глубокая "просадка" против маленького текущего live-bankroll,
    каждый live-стейк резался ×0.5 навсегда. Живой peak сидится/растёт от текущего
    live bankroll на каждый вызов (GREATEST), drawdown теперь честный.
    """
    positions = await fetch_open_positions_for_risk()
    # update_peak_if_higher уже делает GREATEST(stored, bankroll) и сидит
    # bankroll как initial peak если ключ пуст (no drawdown reduce при first run).
    peak = await update_peak_if_higher(bankroll, key=LIVE_PEAK_BANK_KEY)

    settings = get_settings()
    result = check_risk(
        proposed_stake=proposed_stake,
        market_id=market_id,
        event_id=event_id,
        category=category,
        bank=bankroll,
        peak_bank=peak,
        open_positions=positions,
        max_per_bet=settings.risk_max_per_bet_pct,
        min_stake_usd=settings.risk_min_stake_usd,
    )
    if result.is_blocked:
        return 0.0, f"risk_block:{','.join(result.reductions)}", list(result.reductions)
    return result.approved_stake, "", list(result.reductions)


async def _insert_order(payload: dict[str, Any]) -> str | None:
    """INSERT execution_order. Returns id, или None при duplicate / constraint violation.

    Два UNIQUE индекса блокируют дубликаты:
      - uq_execution_orders_signal_active: один активный (не rejected/cancelled)
        ордер per detector_signal_id (partial index — Task 2, audit топ-4)
      - uq_execution_orders_open_market: одна открытая позиция per (detector, market, side)

    Phase 1 Stage 6: 4 CHECK constraints (stake<=500, status/side whitelist,
    price bounds). CheckViolationError = bug в коде (typo, missed validation).
    Логируем + TG alert, возвращаем None (signal обработан как rejected).
    """
    import asyncpg

    from app.services.reject_classify import is_transient_reject

    try:
        row = await fetchrow(
            _INSERT_ORDER_SQL,
            payload["detector"],
            payload["detector_signal_id"],
            payload["market_id"],
            payload["event_id"],
            payload["side"],
            payload["execution_mode"],
            payload["signal_price"],
            payload["calibrated_p"],
            payload["meta_p"],
            payload["ev_pct"],
            payload["order_type"],
            payload.get("limit_price"),
            payload["stake"],
            payload["bankroll_at_entry"],
            payload["status"],
            payload.get("rejection_reason"),
            # source-tag демон/cron (Fix-2); явные ключи payload приоритетнее.
            json.dumps({"source": EXECUTION_SOURCE.get(), **payload.get("meta", {})}),
            # Phase 0 Stage 2 — v2_conservative audit columns
            payload.get("simulation_model_version"),
            payload.get("queue_position_at_placement"),
            payload.get("as_penalty_usd"),
            payload.get("effective_fill_price"),
            # Task 2 (audit топ-4): вычисляем из rejection_reason централизованно —
            # каждый reject-insert-сайт получает маркер автоматически, без ручной
            # простановки в каждом dict literal.
            is_transient_reject(payload.get("rejection_reason")),
        )
    except asyncpg.UniqueViolationError:
        return None
    except asyncpg.CheckViolationError as e:
        # Phase 1 Stage 6: DB constraint violated → bug в коде, не rate-limit.
        # Логируем + TG alert. Возвращаем None чтобы pipeline продолжил.
        logger.error(
            "execution_order_constraint_violated",
            detector=payload.get("detector"),
            market_id=payload.get("market_id"),
            side=payload.get("side"),
            stake=payload.get("stake"),
            status=payload.get("status"),
            error=str(e),
        )
        try:
            await publish(
                [
                    Notification(
                        severity=Severity.ACTION_REQUIRED,
                        subject="execution:v2",
                        kind="order_insert_rejected",
                        headline="База отвергла запись ордера",
                        what_happened=[
                            f"Детектор {payload.get('detector')}, рынок {payload.get('market_id')}.",
                            f"Ошибка: {str(e)[:200]}",
                            "Это баг в коде: данные не прошли проверку на входе в базу.",
                        ],
                        what_if_ignored="Ордер не записан — сделка либо не состоится, либо пройдёт мимо учёта.",
                        what_to_do="Посмотри логи v2_execution по этому детектору: какое поле не прошло проверку.",
                        actions=[],
                        evidence=[f"detector={payload.get('detector')}", f"market={payload.get('market_id')}"],
                    )
                ],
                actor="v2_execution",
            )
        except Exception as notify_exc:
            logger.warning("constraint_violation_notify_failed", error=str(notify_exc))
        return None
    return str(row["id"]) if row else None


async def _place_order(
    signal: dict, mode: str, limit_price: float | None, price_cap: float | None = None
) -> str | None:
    """Polymarket CLOB placement через py-clob-client v2.

    Поддерживает maker (GTC, лимит = mid - tick) и taker (FOK, market order).
    price_cap (taker only) — потолок цены: FOK не исполнится хуже него, вместо
    того чтобы безусловно вычислять цену из глубины книги (audit 2026-07-02
    топ-2: unbounded taker + stake от устаревшей entry_price = edge съедается
    slippage при drift-допуске до 50%).
    """
    from py_clob_client_v2 import (
        ApiCreds,
        MarketOrderArgs,
        OrderArgs,
        OrderType,
        PartialCreateOrderOptions,
    )

    from app.services.polymarket_service import PolymarketService

    settings = get_settings()
    svc = PolymarketService()
    client = svc.clob_client
    creds = ApiCreds(
        api_key=settings.polymarket_api_key,
        api_secret=settings.polymarket_api_secret,
        api_passphrase=settings.polymarket_api_passphrase,
    )
    client.set_api_creds(creds)

    token_id = signal["token_id"]

    if mode == "maker":
        if limit_price is None or limit_price <= 0:
            raise ValueError("maker mode requires limit_price")
        order_args = OrderArgs(
            token_id=token_id,
            price=limit_price,
            size=round(signal["stake"] / limit_price, 2),
            side="BUY",
        )
        resp = client.create_and_post_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.GTC,
        )
    else:
        order_args = MarketOrderArgs(
            token_id=token_id,
            amount=signal["stake"],
            side="BUY",
        )
        if price_cap is not None and price_cap > 0:
            # Потолок: FOK не уйдёт глубже этой цены. НЕ путать с багом price=0 —
            # там price оставался дефолтным нулём (create_and_post_order читал его
            # напрямую); здесь мы осознанно передаём >0 верхнюю границу, а сам вызов
            # остаётся create_and_post_market_order (выводит цену из книги, но не
            # выше переданного потолка).
            order_args.price = price_cap
        # ВАЖНО: market-метод, не create_and_post_order. Последний роутится в
        # create_order (LIMIT-билдер) и читает order_args.price напрямую — у
        # MarketOrderArgs он дефолт 0 → CLOB роняет "invalid price (0)".
        # create_and_post_market_order выводит цену из стакана перед валидацией.
        # Инцидент 2026-06-28: 11 live-ордеров D3 отбиты этим багом.
        resp = client.create_and_post_market_order(
            order_args=order_args,
            options=PartialCreateOrderOptions(tick_size="0.01"),
            order_type=OrderType.FOK,
        )

    if not resp.get("success"):
        raise RuntimeError(f"order_placement_failed: {resp.get('errorMsg', 'unknown')}")
    return resp.get("orderID")


def _calculate_limit_price(side: str, current_ask: float, tick: float = 0.01) -> float:
    """Maker limit: на tick ниже best_ask для BUY YES, на tick выше best_bid для NO.

    Упрощено: signal['side'] уже YES/NO; current_ask уже представлен в координатах
    выбранной стороны (entry_price = market_price для YES, 1-market_price для NO
    делается до вызова). Цена просто: ask - tick, не уходить ниже tick.
    """
    return max(round(current_ask - tick, 2), tick)


_TAKER_ASK_BUFFER = 0.02  # допуск поверх живого ask: FOK переживает микродвижение, но не сметает уровни


def _taker_price_cap(current_ask: float, signal_price: float) -> float:
    """Потолок цены для taker FOK (audit 2026-07-02 топ-2): чуть выше живого ask,
    но не дальше зонального drift-допуска от сигнальной цены — тот же порог, которым
    safety-гейт уже отбивает драфтующие сигналы, здесь используем как верхнюю границу
    самого ордера вместо безусловного ухода вглубь книги.
    """
    zone_cap = signal_price * (1.0 + zone_drift_threshold(signal_price))
    cap = min(current_ask * (1.0 + _TAKER_ASK_BUFFER), zone_cap)
    return round(max(cap, 0.01), 2)  # tick_size 0.01, никогда не 0 (инцидент price=0 2026-06-28)


_FOK_NOT_FILLED_MARKER = "couldn't be fully filled"


def _is_fok_not_filled_error(e: Exception) -> bool:
    """True если CLOB отбил FOK-ордер за нехватку глубины книги (d3-funnel audit
    2026-07-16 топ-фикс), а не по другой причине (5xx, network, invalid params).

    _place_order оборачивает resp["errorMsg"] в голый RuntimeError (не пробрасывает
    типизированный PolyApiException) — isinstance тут не различит причины отказа,
    остаётся только текст. Если Polymarket сменит формулировку, детекция молча
    перестанет матчиться и сигнал просто пойдёт обычным rejected-путём (не крашится).
    """
    return _FOK_NOT_FILLED_MARKER in str(e)


def _is_transport_error(e: Exception) -> bool:
    """True если ошибка размещения ордера — сетевой сбой транспортного уровня
    (DNS/timeout/connect/TLS), а не осмысленный ответ CLOB.

    py_clob_client_v2.http_helpers.helpers.request() ловит ЛЮБОЙ httpx.RequestError
    и переупаковывает его в голый PolyApiException(status_code=None,
    error_message="Request exception!") — оригинальный httpx-тип теряется, поэтому
    isinstance(e, httpx.TimeoutException | httpx.TransportError | ConnectionError)
    для этого пути никогда не сработает. status_code=None — тот же признак, которым
    сама библиотека помечает transient-ошибку для своего внутреннего ретрая
    (_is_transient_error в той же helpers.py), используем его и здесь.
    """
    from py_clob_client_v2.exceptions import PolyApiException

    return isinstance(e, PolyApiException) and e.status_code is None


_FOK_RETRY_STAKE_HAIRCUT = 0.95  # запас от округления book depth, не бить в тот же потолок ещё раз


async def _fok_retry_stake(token_id: str, side: str, price_cap: float, original_stake: float) -> float | None:
    """Реальная usable-глубина книги по цене <= price_cap → безопасный размер для
    повторного FOK. None если глубины недостаточно для осмысленного ретрая.
    """
    from app.services.virtual_execution import estimate_maker_fill_probability, fetch_full_book

    book = await fetch_full_book(token_id)
    fill_prob = estimate_maker_fill_probability(
        book=book,
        side=side,
        limit_price=price_cap,
        stake_usd=original_stake,
    )
    usable = fill_prob.book_depth_at_or_better * _FOK_RETRY_STAKE_HAIRCUT
    if usable < get_settings().live_min_stake:
        return None
    return round(min(original_stake, usable), 2)


# ── Multi-leg basket signals (Phase 3 Task 4, audit research-gap) ──────────────
# D29 (negrisk_basket_arb) эмитит ОДИН Signal на event, но требует buy NO на ВСЕ
# рынки события атомарно — evidence["basket_markets"] несёт полный список ног.
# Такой сигнал НЕ должен проходить обычный single-leg sizing/placement (buying
# только primary market = exposure 1-leg вместо гарантированного basket payout,
# исходный баг из-за которого D29 был выключен). _is_basket_signal распознаёт
# сигнал по наличию basket_markets в evidence (detector-agnostic — тот же путь
# подойдёт D40 без изменений здесь).


def _parse_evidence(sig: dict[str, Any]) -> dict[str, Any]:
    ev_raw = sig.get("evidence")
    if isinstance(ev_raw, str):
        try:
            return json.loads(ev_raw) or {}
        except Exception:
            return {}
    return ev_raw or {}


def _is_basket_signal(sig: dict[str, Any]) -> bool:
    evidence = _parse_evidence(sig)
    legs = evidence.get("basket_markets")
    return bool(legs) and isinstance(legs, list) and len(legs) >= 2


async def _process_basket_signal(sig: dict[str, Any]) -> str:
    """Basket-путь (D29/D40): распределяет sizing по ногам, координирует через
    basket_execution.place_basket (live) или последовательные virtual-симуляции
    (virtual). Returns: 'opened' | 'rejected' | 'skipped' | 'duplicate'.

    Дизайн: total basket stake считается ОДИН раз обычным sizer'ом на primary
    сигнале (calibrated_p/meta_p детектора — для D29 это mathematical confidence
    1.0), затем делится поровну между ногами. Basket-level risk-гейты (kill
    switch, bankroll, risk_engine) применяются к total stake один раз — per-leg
    depth-check (Invariant 1, basket_execution.place_basket) остаётся
    единственным per-leg entry-гейтом для live, аналогично тому как safety_evaluate
    книгу проверяет для single-leg сигналов.
    """
    import uuid as uuid_mod

    detector = sig["detector"]
    detector_signal_id = sig["detector_signal_id"]
    evidence = _parse_evidence(sig)
    basket_legs_raw = evidence.get("basket_markets") or []

    state = await get_detector_state(detector)
    settings = get_settings()
    stage = state.stage if state is not None else "virtual"
    is_live_mode = settings.execution_enabled and stage == "live" and settings.d29_basket_enabled

    portfolio: Portfolio | None = None
    if not is_live_mode:
        portfolio = await fetch_portfolio_for_detector(detector)
        bankroll = portfolio.bankroll_usd if portfolio is not None else SHADOW_BANKROLL_USD
        fractional_kelly = portfolio.kelly_fraction if portfolio is not None else FRACTIONAL_KELLY_COLD
    else:
        bankroll_snap = await get_live_bankroll()
        bankroll = bankroll_snap.bankroll_usd
        if not bankroll_snap.is_sufficient:
            is_live_mode = False
            portfolio = await fetch_portfolio_for_detector(detector)
            bankroll = portfolio.bankroll_usd if portfolio is not None else SHADOW_BANKROLL_USD
            fractional_kelly = portfolio.kelly_fraction if portfolio is not None else FRACTIONAL_KELLY_COLD
        else:
            fractional_kelly = float(state.fractional_kelly_today) if state is not None else FRACTIONAL_KELLY_COLD

    # Shadow-телеметрия (План 1, event-driven-price-pipeline): полное устаревание
    # цены сайзинга = возраст снапшота детекции + лаг до исполнения. Fire-and-forget.
    # sig.get(...) (не []) — телеметрия не должна ронять money-path, если поле
    # отсутствует (напр. в старых unit-тестах, собирающих sig вручную).
    _sizing_current_price = sig.get("current_price")
    if _sizing_current_price is not None:
        asyncio.create_task(
            log_price_staleness(
                "sizing",
                str(sig["market_id"]),
                sig.get("yes_token_id"),
                float(_sizing_current_price),
            )
        )
    decision = size_from_calibrated_p(
        calibrated_p=sig["meta_p"],
        entry_price=sig["entry_price"],
        bankroll_usd=bankroll,
        category=sig.get("category"),
        fee_schedule=sig.get("fee_schedule"),
        fractional_kelly=fractional_kelly,
    )
    if decision.stake_usd is None or decision.stake_usd <= 0:
        logger.info(
            "v2_exec_sizing_skipped",
            detector=detector,
            reason=decision.reason,
        )
        return "skipped"

    n_legs = len(basket_legs_raw)
    per_leg_stake = round(float(decision.stake_usd) / n_legs, 2)
    if per_leg_stake < settings.live_min_stake:
        # Basket сигналов delen bankroll слишком мелко на ногу — не рентабельно
        # исполнять (fee съест edge). Пишем rejected для видимости в Activity.
        await _insert_order(
            {
                "detector": detector,
                "detector_signal_id": detector_signal_id,
                "market_id": sig["market_id"],
                "event_id": sig.get("event_id"),
                "side": sig["side"],
                "signal_price": float(sig["entry_price"]),
                "calibrated_p": float(sig["calibrated_p"]),
                "meta_p": float(sig["meta_p"]),
                "ev_pct": float(decision.ev_pct),
                "stake": float(decision.stake_usd),
                "bankroll_at_entry": bankroll,
                "execution_mode": "rejected",
                "order_type": "none",
                "status": "rejected",
                "rejection_reason": f"basket_leg_stake_too_small:${per_leg_stake:.2f}/leg,n={n_legs}",
            }
        )
        return "rejected"

    basket_id = uuid_mod.uuid4()

    if not is_live_mode:
        # Virtual: каждая нога — обычная virtual_execution симуляция (taker FOK,
        # книга каждого market'а отдельно), записанная как execution_orders строка
        # с общим parent_basket_id. Никакого unwind в virtual (paper positions).
        from app.detectors.token_resolver import no_token_id
        from app.services.virtual_execution import simulate_place

        n_opened = 0
        for leg in basket_legs_raw:
            # no_token_id normalizes list (already-decoded jsonb) и raw JSON-string
            # (asyncpg default) — см. app/detectors/token_resolver.py docstring.
            token_id = no_token_id(leg.get("clob_token_ids")) or leg.get("token_id")
            if not token_id:
                continue
            leg_entry_price = float(leg.get("no_entry") or (1.0 - float(leg.get("yes_price") or 0)))
            sim = await simulate_place(
                token_id=token_id,
                mode="taker",
                limit_price=None,
                fallback_price=leg_entry_price,
                stake_usd=per_leg_stake,
            )
            order_id = await _insert_order(
                {
                    "detector": detector,
                    "detector_signal_id": detector_signal_id if leg is basket_legs_raw[0] else None,
                    "market_id": str(leg.get("market_id")),
                    "event_id": sig.get("event_id"),
                    "side": "NO",
                    "signal_price": leg_entry_price,
                    "calibrated_p": float(sig["calibrated_p"]),
                    "meta_p": float(sig["meta_p"]),
                    "ev_pct": float(decision.ev_pct),
                    "stake": per_leg_stake,
                    "bankroll_at_entry": bankroll,
                    "execution_mode": "virtual",
                    "order_type": "FOK",
                    "status": sim.status if sim.status != "virtual_failed" else "rejected",
                    "rejection_reason": sim.reason if sim.status == "virtual_failed" else None,
                }
            )
            if order_id:
                await execute("UPDATE execution_orders SET parent_basket_id=$1 WHERE id=$2", str(basket_id), order_id)
                if sim.status == "virtual_filled":
                    await execute(
                        "UPDATE execution_orders SET fill_price=$1, filled_qty=stake/$1, "
                        "placed_at=now(), filled_at=now() WHERE id=$2",
                        float(sim.fill_price),
                        order_id,
                    )
                n_opened += 1
        return "opened" if n_opened > 0 else "rejected"

    # Live: полный account-level safety gate один раз на total stake (kill-switch
    # env+Redis, per-detector daemon-пауза, in-flight/open-position caps, daily
    # loss) — basket обходит single-leg safety_evaluate целиком (там book/drift
    # per-market, не подходит для basket'а), поэтому account-level чеки нужно
    # продублировать явно, иначе Redis kill_switch/daemon-пауза/daily-loss не
    # остановят basket-ордера. Per-leg проверки — Invariant 1 в place_basket.
    ok, reason = check_kill_switch()
    if ok:
        ok, reason = await check_redis_kill_flag()
    if ok:
        ok, reason = await check_daemon_pause(detector)
    if ok:
        ok, reason = await check_max_inflight_orders()
    if ok:
        ok, reason = await check_max_open_positions()
    if ok:
        ok, reason = await check_daily_loss(bankroll)
    if ok:
        ok, reason = await check_daily_pnl_limit_mtm(bankroll)
    if not ok:
        logger.info("basket_signal_rejected_safety_gate", detector=detector, reason=reason)
        return "rejected"

    from app.detectors.token_resolver import no_token_id
    from app.services.basket_execution import place_basket

    legs_for_exec: list[dict[str, Any]] = []
    for leg in basket_legs_raw:
        token_id = no_token_id(leg.get("clob_token_ids")) or leg.get("token_id")
        if not token_id:
            logger.warning("basket_leg_missing_token_id", detector=detector, market_id=leg.get("market_id"))
            return "rejected"
        leg_yes_price = float(leg.get("yes_price") or 0)
        leg_entry_price = float(leg.get("no_entry") or (1.0 - leg_yes_price))
        legs_for_exec.append(
            {
                "token_id": token_id,
                "stake_usd": per_leg_stake,
                "price_cap": _taker_price_cap(1.0 - leg_yes_price, leg_entry_price),
                "market_id": str(leg.get("market_id")),
                "event_id": sig.get("event_id"),
                "signal_price": leg_entry_price,
            }
        )

    result = await place_basket(legs_for_exec, detector=detector, basket_id=basket_id)
    logger.info(
        "basket_signal_processed",
        detector=detector,
        basket_id=str(basket_id),
        n_legs=len(legs_for_exec),
        result=result,
    )
    return "opened" if result == "filled" else "rejected"


# TTL claim'а строго меньше retry-окна транзиентных reject'ов (180с в
# _SIGNAL_SELECT_BASE_SQL) — иначе claim блокировал бы легитимный ретрай.
_SIGNAL_CLAIM_TTL_SEC = 120


async def _try_claim_signal(detector_signal_id: str) -> bool:
    """Атомарный cross-process claim сигнала (fix 2026-07-03, дубль f661c2f2).

    Демон и минутный cron обрабатывают одну очередь сигналов; SQL-дедуп
    (partial UNIQUE + NOT EXISTS) не видит незакоммиченный INSERT соседа и не
    покрывает rejected-строки — измеренный итог: 2 rejected-ордера на сигнал
    с разницей 0.5с, корм для ложного anomaly-kill (класс инцидента D38).

    Fail-open: Redis недоступен → дубль дешевле остановки execution
    (SQL-констрейнт остаётся второй линией для активных ордеров).
    """
    try:
        from app.core.cache import get_redis

        redis = await get_redis()
        if redis is None:
            return True
        ok = await redis.set(f"exec:claim:{detector_signal_id}", "1", nx=True, ex=_SIGNAL_CLAIM_TTL_SEC)
        return bool(ok)
    except Exception as e:
        logger.debug("signal_claim_failed_open", signal_id=str(detector_signal_id)[:16], error=str(e)[:80])
        return True


async def _process_signal(sig: dict[str, Any]) -> str:
    """Один тик одного сигнала. Returns: 'opened' | 'rejected' | 'skipped' | 'duplicate'.

    sig — dict от fetch_signals_for_active_pipeline (см. detector_signal_service.py).
    """
    detector = sig["detector"]
    detector_signal_id = sig["detector_signal_id"]

    # -2. Cross-process claim ДО любых гейтов и basket-роутинга: проигравший
    # гонку daemon-vs-cron путь не должен дойти до insert'а вообще.
    if not await _try_claim_signal(detector_signal_id):
        logger.debug("signal_claimed_elsewhere", detector=detector, signal_id=str(detector_signal_id)[:16])
        return "duplicate"

    # -1. Multi-leg basket signal (Phase 3 Task 4, audit research-gap): D29/D40
    # эмитят ОДИН Signal на event с evidence["basket_markets"] = все ноги.
    # Обычный single-leg путь ниже купил бы ТОЛЬКО primary market (исходный баг
    # из-за которого D29 был выключен) — роутим в отдельный basket-координатор
    # ДО любых single-leg-специфичных гейтов (category/entry-price/cal_p
    # blacklists ниже посчитаны для одиночных сигналов и basket не проходят).
    if _is_basket_signal(sig):
        return await _process_basket_signal(sig)

    # 0. Early skip: без token_id не можем фечить orderbook
    if not sig.get("token_id"):
        return "skipped"

    # 0.5. Per-detector category blacklist (см. _CATEGORY_BLACKLIST).
    # Эмпирика 2026-05-18: Sports -$5,818/14d. Перед sizing — отбрасываем сигнал,
    # пишем как 'rejected' в БД для аналитики (чтобы видно было в Activity tab).
    if _is_category_blacklisted(detector, sig.get("category")):
        if _gate_shadow_enabled(detector, "category_blacklist"):
            from app.services.gate_shadow_telemetry import log_gate_shadow

            await log_gate_shadow(
                detector=detector,
                gate=f"category_blacklist:{sig.get('category')}",
                detector_signal_id=detector_signal_id,
                token_id=sig.get("token_id"),
            )
        await _insert_order(
            {
                "detector": detector,
                "detector_signal_id": detector_signal_id,
                "market_id": sig["market_id"],
                "event_id": sig.get("event_id"),
                "side": sig["side"],
                "signal_price": float(sig["entry_price"]),
                "calibrated_p": float(sig["calibrated_p"]),
                "meta_p": float(sig["meta_p"]),
                "ev_pct": 0.0,
                "stake": 0.0,
                "bankroll_at_entry": 0.0,
                "execution_mode": "rejected",
                "order_type": "none",
                "status": "rejected",
                "rejection_reason": f"category_blacklist:{sig.get('category')}",
            }
        )
        return "rejected"

    # 0.52. Temperature-блэклист (см. _is_temperature_market): adverse selection
    # против метео-фидов, все direction-детекторы в минусе (−$12.3k virtual).
    # Shadow-телеметрия пишется всегда — низкочастотный гейт, книга на момент
    # reject нужна для honest-контрфактики пересмотра (~2026-07-21, вместе с D38).
    if detector not in _TEMPERATURE_GATE_EXEMPT and _is_temperature_market(sig.get("question")):
        from app.services.gate_shadow_telemetry import log_gate_shadow

        await log_gate_shadow(
            detector=detector,
            gate="temperature_blacklist",
            detector_signal_id=detector_signal_id,
            token_id=sig.get("token_id"),
        )
        await _insert_order(
            {
                "detector": detector,
                "detector_signal_id": detector_signal_id,
                "market_id": sig["market_id"],
                "event_id": sig.get("event_id"),
                "side": sig["side"],
                "signal_price": float(sig["entry_price"]),
                "calibrated_p": float(sig["calibrated_p"]),
                "meta_p": float(sig["meta_p"]),
                "ev_pct": 0.0,
                "stake": 0.0,
                "bankroll_at_entry": 0.0,
                "execution_mode": "rejected",
                "order_type": "none",
                "status": "rejected",
                "rejection_reason": "temperature_blacklist",
            }
        )
        return "rejected"

    # 0.55. Category-health shadow (НЕ блокирует): ячейка detector×category с
    # устойчиво отрицательным event-кластеризованным ROI (app/jobs/category_health.py)
    # — пишем would-block в gate_shadow_log. Денежная контрфактика по этим строкам —
    # virtual_weekly_digest; включение реального блэклиста — human decision.
    if await _category_health_candidate(detector, sig.get("category")):
        from app.services.gate_shadow_telemetry import log_gate_shadow

        await log_gate_shadow(
            detector=detector,
            gate=f"category_health:{sig.get('category')}",
            detector_signal_id=detector_signal_id,
            token_id=sig.get("token_id"),
        )

    # 0.6. Per-detector entry-price guardrail (см. _FILL_PRICE_RANGE).
    # Эмпирика 2026-05-18 (D3): mid zone [0.10, 0.60] = +$3,346, остальное −$3,989.
    entry_price = sig.get("entry_price")
    if _is_entry_price_out_of_range(detector, float(entry_price) if entry_price is not None else None):
        await _insert_order(
            {
                "detector": detector,
                "detector_signal_id": detector_signal_id,
                "market_id": sig["market_id"],
                "event_id": sig.get("event_id"),
                "side": sig["side"],
                "signal_price": float(sig["entry_price"]),
                "calibrated_p": float(sig["calibrated_p"]),
                "meta_p": float(sig["meta_p"]),
                "ev_pct": 0.0,
                "stake": 0.0,
                "bankroll_at_entry": 0.0,
                "execution_mode": "rejected",
                "order_type": "none",
                "status": "rejected",
                "rejection_reason": f"entry_price_out_of_range:{float(entry_price):.3f}",
            }
        )
        return "rejected"

    # 0.7. Per-detector cal_p zone blacklist (см. _CAL_P_ZONE_BLACKLIST).
    # Эмпирика 2026-05-19 (30d virtual, settled): материальные убытки в конкретных
    # (detector × cal_p zone) ячейках. Reject signal до sizing, пишем причину в БД.
    cal_p_val = float(sig["calibrated_p"]) if sig.get("calibrated_p") is not None else None
    is_zone_blocked, zone_label = _is_cal_p_zone_blacklisted(detector, cal_p_val)
    if is_zone_blocked:
        await _insert_order(
            {
                "detector": detector,
                "detector_signal_id": detector_signal_id,
                "market_id": sig["market_id"],
                "event_id": sig.get("event_id"),
                "side": sig["side"],
                "signal_price": float(sig["entry_price"]),
                "calibrated_p": cal_p_val,
                "meta_p": float(sig["meta_p"]),
                "ev_pct": 0.0,
                "stake": 0.0,
                "bankroll_at_entry": 0.0,
                "execution_mode": "rejected",
                "order_type": "none",
                "status": "rejected",
                "rejection_reason": f"cal_p_zone_blacklist:{zone_label}",
            }
        )
        return "rejected"

    # 1. Detector state → решаем virtual / live (нужно ДО event-guard, т.к. guard
    # скоупится по mode-family текущего сигнала — см. ниже).
    # NB: signals fetcher (detector_signal_service) уже отфильтровал shadow detectors,
    # сюда попадают только stage IN ('virtual','live'). См. docs/detector_lifecycle.md.
    state = await get_detector_state(detector)
    settings = get_settings()
    stage = state.stage if state is not None else "virtual"  # default: virtual paper exec
    is_live_mode = settings.execution_enabled and stage == "live"
    mode_label = "live" if is_live_mode else "virtual"

    # 1.2. Event-level re-entry guard (см. _event_reentry_blocked + memory
    # standalone_virtual_pnl_inflation_2026-06-04). Один вход на event пока
    # событие активно: если на (detector, event_id) уже была позиция в ТЕКУЩЕЙ
    # mode-family — reject. Лечит скальп одного event (STANDALONE 10-32 входа/16ч
    # раздували virtual P&L raw +$47.5K vs честный +$663). Live-aware: live-сигнал
    # гейтится по прошлым live-позициям, virtual — по virtual (см. SQL выше).
    event_id = sig.get("event_id")
    if await _event_reentry_blocked(detector, event_id, live_mode=is_live_mode):
        await _insert_order(
            {
                "detector": detector,
                "detector_signal_id": detector_signal_id,
                "market_id": sig["market_id"],
                "event_id": event_id,
                "side": sig["side"],
                "signal_price": float(sig["entry_price"]),
                "calibrated_p": float(sig["calibrated_p"]),
                "meta_p": float(sig["meta_p"]),
                "ev_pct": 0.0,
                "stake": 0.0,
                "bankroll_at_entry": 0.0,
                "execution_mode": "rejected",
                "order_type": "none",
                "status": "rejected",
                "rejection_reason": f"event_reentry_blocked:event={event_id}",
            }
        )
        return "rejected"

    # 1.5. Phase 3: lookup portfolio для virtual mode (bankroll/kelly/cap).
    # None допустим в transient state (миграция не применена) — fallback к defaults.
    portfolio: Portfolio | None = None
    if not is_live_mode:
        portfolio = await fetch_portfolio_for_detector(detector)

    # 2. Bankroll: live → реальный wallet, virtual → portfolio bankroll (или fallback)
    if is_live_mode:
        bankroll_snap = await get_live_bankroll()
        bankroll = bankroll_snap.bankroll_usd
        if not bankroll_snap.is_sufficient:
            logger.info("v2_exec_skip_low_bankroll", bankroll=bankroll)
            bankroll = SHADOW_BANKROLL_USD
            is_live_mode = False
            mode_label = "virtual"
            portfolio = await fetch_portfolio_for_detector(detector)
            if portfolio is not None:
                bankroll = portfolio.bankroll_usd
            # Audit second-tier: тихий даунгрейд live→virtual маскировал реальные
            # проблемы с банкроллом (недоступен/ниже floor). Debounce раз в сутки —
            # деградация держится долго, не нужно долбить на каждый tick (1 мин).
            try:
                from app.core.task_queue import debounce_acquire

                if await debounce_acquire("alert:bankroll_downgrade", 86400):
                    await publish(
                        [
                            Notification(
                                severity=Severity.ACTION_REQUIRED,
                                subject="execution:v2",
                                kind="bankroll_downgrade",
                                headline="Live-торговля переключилась на виртуальную",
                                what_happened=[
                                    "Банкролл недоступен или ниже минимального порога.",
                                    "Сигналы продолжают исполняться, но виртуально — реальные ставки не размещаются.",
                                ],
                                what_if_ignored=(
                                    "Живых сделок не будет, пока банкролл не вернётся: система работает вхолостую."
                                ),
                                what_to_do="Проверь баланс кошелька и доступность wallet API.",
                                actions=[],
                                evidence=["mode=live→virtual"],
                            )
                        ],
                        actor="v2_execution",
                    )
            except Exception as notify_exc:
                logger.warning("v2_exec_bankroll_downgrade_notify_failed", error=str(notify_exc)[:200])
    else:
        bankroll = portfolio.bankroll_usd if portfolio is not None else SHADOW_BANKROLL_USD

    # 3. Sizing: kelly из portfolio (virtual) или detector_state (live fallback)
    if portfolio is not None and not is_live_mode:
        fractional_kelly = portfolio.kelly_fraction
    elif state is not None:
        fractional_kelly = float(state.fractional_kelly_today)
    else:
        fractional_kelly = FRACTIONAL_KELLY_COLD

    # Shadow-телеметрия (План 1, event-driven-price-pipeline): полное устаревание
    # цены сайзинга = возраст снапшота детекции + лаг до исполнения. Fire-and-forget.
    # sig.get(...) (не []) — телеметрия не должна ронять money-path, если поле
    # отсутствует (напр. в старых unit-тестах, собирающих sig вручную).
    _sizing_current_price = sig.get("current_price")
    if _sizing_current_price is not None:
        asyncio.create_task(
            log_price_staleness(
                "sizing",
                str(sig["market_id"]),
                sig.get("yes_token_id"),
                float(_sizing_current_price),
            )
        )
    decision = size_from_calibrated_p(
        calibrated_p=sig["meta_p"],
        entry_price=sig["entry_price"],
        bankroll_usd=bankroll,
        category=sig.get("category"),
        fee_schedule=sig.get("fee_schedule"),
        fractional_kelly=fractional_kelly,
    )
    if decision.stake_usd is None or decision.stake_usd <= 0:
        logger.info(
            "v2_exec_sizing_skipped",
            detector=detector,
            reason=decision.reason,
        )
        return "skipped"

    # 3.5. Virtual per-detector exposure cap.
    # Default $5K, override $10K для проверенных winners V2_LGB/D36
    # (см. _DETECTOR_EXPOSURE_CAP_OVERRIDES — selection bias audit 2026-05-19).
    # Live mode use real bankroll controls — этот cap только для virtual paper trading.
    if not is_live_mode:
        remaining = await _virtual_exposure_remaining(detector)
        if float(decision.stake_usd) > remaining:
            detector_cap = _exposure_cap_for(detector)
            await _insert_order(
                {
                    "detector": detector,
                    "detector_signal_id": detector_signal_id,
                    "market_id": sig["market_id"],
                    "event_id": sig.get("event_id"),
                    "side": sig["side"],
                    "signal_price": float(sig["entry_price"]),
                    "calibrated_p": float(sig["calibrated_p"]),
                    "meta_p": float(sig["meta_p"]),
                    "ev_pct": float(decision.ev_pct),
                    "stake": float(decision.stake_usd),
                    "bankroll_at_entry": bankroll,
                    "execution_mode": "rejected",
                    "order_type": "none",
                    "status": "rejected",
                    "rejection_reason": (
                        f"virtual_exposure_cap_exceeded:open=${detector_cap - remaining:.2f},"
                        f"stake=${float(decision.stake_usd):.2f},cap=${detector_cap:.0f}"
                    ),
                }
            )
            return "rejected"

    # 3.6. Phase 3: portfolio cap check (sum across all detectors в portfolio).
    # Применяется только virtual mode + только если portfolio resolved.
    if not is_live_mode and portfolio is not None:
        portfolio_used = await _portfolio_exposure_used(portfolio.portfolio_id)
        portfolio_remaining = max(0.0, portfolio.exposure_cap_usd - portfolio_used)
        if float(decision.stake_usd) > portfolio_remaining:
            await _insert_order(
                {
                    "detector": detector,
                    "detector_signal_id": detector_signal_id,
                    "market_id": sig["market_id"],
                    "event_id": sig.get("event_id"),
                    "side": sig["side"],
                    "signal_price": float(sig["entry_price"]),
                    "calibrated_p": float(sig["calibrated_p"]),
                    "meta_p": float(sig["meta_p"]),
                    "ev_pct": float(decision.ev_pct),
                    "stake": float(decision.stake_usd),
                    "bankroll_at_entry": bankroll,
                    "execution_mode": "rejected",
                    "order_type": "none",
                    "status": "rejected",
                    "rejection_reason": (
                        f"virtual_portfolio_cap_exceeded:portfolio={portfolio.portfolio_id},"
                        f"used=${portfolio_used:.2f},stake=${float(decision.stake_usd):.2f},"
                        f"cap=${portfolio.exposure_cap_usd:.0f}"
                    ),
                }
            )
            return "rejected"

    # 4. Safety gate
    # thin_book fix-bug (audit 2026-07-03, research/fable-gates-audit-2026-07-03/
    # evidence/01 §a): check_market_book считает need=max(2×stake_usd, $20) от
    # СЫРОГО Kelly-стейка — до ramp-cap клампа (шаг 5.3, ниже по коду) и до
    # risk_engine reduce (шаг 5.25). При ramp-cap $10 честный need=$20, а не
    # $49.9+ от нередуцированного стейка. Клампим stake_usd тем же ramp-cap,
    # которым позже клампится decision.stake_usd — floor $20 не трогаем.
    safety_stake_usd = float(decision.stake_usd)
    if is_live_mode and state is not None and state.live_stake_cap_usd is not None:
        safety_stake_usd = min(safety_stake_usd, float(state.live_stake_cap_usd))
    ctx = SafetyContext(
        detector=detector,
        detector_state=state,
        market_id=sig["market_id"],
        token_id=sig["token_id"] or "",
        side=sig["side"],
        signal_price=float(sig["entry_price"]),
        stake_usd=safety_stake_usd,
        bankroll_usd=bankroll,
        generated_at=sig.get("generated_at"),
        market_end_date=sig.get("market_end_date"),
        detector_signal_id=detector_signal_id,
    )
    verdict = await safety_evaluate(ctx, mode=mode_label)

    if not is_live_mode:
        # Shadow-прогон live-гейтов (spec 2026-07-22 §5) — только телеметрия.
        await shadow_evaluate_virtual(ctx)

    # 5. INSERT — записываем intent или rejected
    base = {
        "detector": detector,
        "detector_signal_id": detector_signal_id,
        "market_id": sig["market_id"],
        "event_id": sig.get("event_id"),
        "side": sig["side"],
        "signal_price": float(sig["entry_price"]),
        "calibrated_p": float(sig["calibrated_p"]),
        "meta_p": float(sig["meta_p"]),
        "ev_pct": float(decision.ev_pct),
        "stake": float(decision.stake_usd),
        "bankroll_at_entry": bankroll,
    }

    if not verdict.allowed:
        await _insert_order(
            {
                **base,
                "execution_mode": "rejected",
                "order_type": "none",
                "status": "rejected",
                "rejection_reason": verdict.reason,
            }
        )
        return "rejected"

    # Fix-3 шаг 1 (latency-аудит №3): shadow-телеметрия «stake до/после» —
    # ДО risk_engine reduce и ramp-cap, пока decision.stake_usd = raw Kelly.
    _log_stake_staleness_shadow(
        detector=detector,
        sig=sig,
        live_price=verdict.current_price,
        stale_stake_usd=float(decision.stake_usd),
        bankroll=bankroll,
        fractional_kelly=fractional_kelly,
    )

    # 5.24. Anti-selection fix (spec 2026-07-22, §3): live-стейк на live-цене.
    # Kelly считался на stale sig["entry_price"] (снапшот 10-15 мин) — при
    # снятом 300s age-гейте это опасно. verdict.current_price = live ask
    # НАШЕГО токена (book по sig["token_id"], side-aware) — пересчёт напрямую.
    # Shadow-телеметрия выше остаётся мерой качества пересчёта (delta → 0).
    if is_live_mode and verdict.current_price is not None:
        repriced = size_from_calibrated_p(
            calibrated_p=sig["meta_p"],
            entry_price=float(verdict.current_price),
            bankroll_usd=bankroll,
            category=sig.get("category"),
            fee_schedule=sig.get("fee_schedule"),
            fractional_kelly=fractional_kelly,
        )
        if repriced.stake_usd is None or repriced.stake_usd <= 0:
            await _insert_order(
                {
                    **base,
                    "ev_pct": float(repriced.ev_pct),
                    "stake": 0.0,  # sizer вернул None — стейка нет, stale-число вводит в заблуждение
                    "execution_mode": "rejected",
                    "order_type": "none",
                    "status": "rejected",
                    "rejection_reason": (
                        f"ev_negative_at_live_price:{repriced.reason};"
                        f"signal={float(sig['entry_price']):.3f},live={float(verdict.current_price):.3f}"
                    ),
                }
            )
            return "rejected"
        decision = repriced
        base["ev_pct"] = float(decision.ev_pct)
        base["stake"] = float(decision.stake_usd)

    # 5.25. Phase 1 Stage 2: risk_engine layer (live only).
    # Event/category/heat caps + drawdown reduce. Использует MtM bankroll
    # из Stage 1 (косвенно: через peak_bank tracking из mtm-refresh).
    # Virtual mode не enforced — risk_engine только для real exposure.
    if is_live_mode:
        approved_stake, risk_reason, risk_reductions = await _apply_risk_engine(
            proposed_stake=float(decision.stake_usd),
            market_id=sig["market_id"],
            event_id=sig.get("event_id"),
            category=sig.get("category") or "unknown",
            bankroll=bankroll,
        )
        if risk_reason:
            await _insert_order(
                {
                    **base,
                    "execution_mode": "rejected",
                    "order_type": "none",
                    "status": "rejected",
                    "rejection_reason": risk_reason,
                }
            )
            return "rejected"
        if approved_stake < float(decision.stake_usd):
            logger.info(
                "v2_exec_risk_reduce",
                market=sig["market_id"],
                detector=detector,
                original=float(decision.stake_usd),
                approved=approved_stake,
                reductions=risk_reductions,
            )
            # Replace stake в decision и base payload.
            from dataclasses import replace as _replace

            decision = _replace(decision, stake_usd=approved_stake)
            base["stake"] = approved_stake

    # 5.3. Phase 3 Task 2 (audit research-gap): ramp-лесенка cap. Канареечный
    # размер ставки держался только на малом балансе кошелька — при
    # пополнении стейки выросли бы молча. state.live_stake_cap_usd — потолок
    # per-trade stake для текущей ramp-ступени (см. detector_state,
    # docs/superpowers/plans/2026-06-25-d3-live-proxy-ramp.md §3.1). Cap
    # применяется ПОСЛЕ sizing и risk_engine reduce, ДО whale-sweep filter.
    if is_live_mode and state is not None and state.live_stake_cap_usd is not None:
        if float(decision.stake_usd) > float(state.live_stake_cap_usd):
            from dataclasses import replace as _replace

            decision = _replace(decision, stake_usd=float(state.live_stake_cap_usd))
            base["stake"] = float(state.live_stake_cap_usd)

    # 5.5. Whale-Sweep filter (H9 hypothesis 2026-05-12):
    # Detect informed whale activity sweeping ≥3 book levels in <30 sec.
    # - sweep CONFIRMS our direction → boost (informed flow agrees)
    # - sweep CONTRA our direction → REJECT (informed flow disagrees, мы exit liquidity)
    from app.services.whale_sweep_service import apply_sweep_adjustment, get_sweep_state

    sweep = await get_sweep_state(sig["market_id"])
    _adj_conf, sweep_reason = apply_sweep_adjustment(
        side=sig["side"],
        base_confidence=float(sig.get("calibrated_p") or 0.5),
        sweep=sweep,
    )
    if sweep_reason == "sweep_contra":
        await _insert_order(
            {
                **base,
                "execution_mode": "rejected",
                "order_type": "none",
                "status": "rejected",
                "rejection_reason": (
                    f"whale_sweep_contra:dir={sweep.direction},vol=${sweep.total_volume_usd:.0f},"
                    f"n={sweep.n_sweeps},age={sweep.last_sweep_age_sec:.0f}s"
                    if sweep
                    else "whale_sweep_contra"
                ),
            }
        )
        return "rejected"
    # sweep_confirms → no rejection, sizing increases via fractional_kelly multiplier
    # (мы НЕ меняем calibrated_p чтобы не искажать downstream metrics; вместо этого
    # просто boost'им stake через kelly_multiplier ниже).

    # 6. Decide mode (общий для virtual и live)
    current_ask = verdict.current_price or float(sig["entry_price"])
    sig_age_sec = 0.0
    if sig.get("generated_at"):
        from datetime import UTC, datetime

        gen_at = sig["generated_at"]
        if gen_at.tzinfo is None:
            gen_at = gen_at.replace(tzinfo=UTC)
        sig_age_sec = (datetime.now(UTC) - gen_at).total_seconds()
    # Реальная глубина из book-fetch'а safety_evaluate (live-путь); virtual-путь
    # книгу не фетчит → None → fallback 200.0 (эквивалент прежнего хардкода).
    book_depth = verdict.book_depth_usd if verdict.book_depth_usd is not None else 200.0
    # resolve_execution_mode = decide_mode + принудительный taker для детекторов
    # с гейтом цены входа (spec 2026-08-05): лимитка в прибыльной полосе не
    # исполняется — книга наливает охотнее там, где мы теряем.
    mode = resolve_execution_mode(
        detector, book_depth_usd=book_depth, signal_age_sec=sig_age_sec, ev_pct=float(decision.ev_pct)
    )
    limit_price = _calculate_limit_price(sig["side"], current_ask) if mode == "maker" else None

    # 7. Virtual placement (если detector в shadow или EXECUTION_ENABLED=false)
    if not is_live_mode:
        from app.services.virtual_execution import (
            estimate_maker_fill_probability,
            fetch_full_book,
            simulate_place,
        )

        sim = await simulate_place(
            token_id=sig["token_id"],
            mode=mode,
            limit_price=limit_price,
            fallback_price=float(sig["entry_price"]),
            stake_usd=float(decision.stake_usd),
        )

        # FIX 2026-05-13: price drift gate. Reject если fill ушёл далеко от signal.
        # Раньше: absolute 5pp. Проблема: для underdog entry 0.10 даже 2pp drift =
        # 20% relative move (катастрофа на стороне), а для favorite 0.85 entry
        # 5pp drift = только 6% relative. Делаем proportional: 30% relative
        # (= "fill_price moved away by more than 30% of entry stake worth").
        # И skip drift check для synthetic fills (нет реальной market price).
        if sim.status == "virtual_filled" and sim.fill_price is not None and sim.reason != "synthetic_no_book":
            signal_price = float(sig["entry_price"])
            drift_abs = abs(float(sim.fill_price) - signal_price)
            drift_rel = drift_abs / max(signal_price, 0.01)
            if drift_rel > 0.30:
                drift_pp = drift_abs * 100.0
                sim_reason = (
                    f"price_drift:{drift_pp:.1f}pp ({drift_rel * 100:.0f}% rel) "
                    f"signal={signal_price:.4f} fill={sim.fill_price:.4f}"
                )
                logger.info("v2_exec_reject_price_drift", reason=sim_reason)
                await _insert_order(
                    {
                        **base,
                        "execution_mode": "rejected",
                        "order_type": "none",
                        "status": "rejected",
                        "rejection_reason": sim_reason,
                    }
                )
                return "rejected"

        # PR1: Stake-aware fill probability (для shadow → live divergence монитоинга).
        # Делаем full book fetch только когда у нас maker placement, иначе fee
        # (taker FOK всегда fill'ится по top of book).
        fill_prob = None
        if mode == "maker" and limit_price is not None:
            full_book = await fetch_full_book(sig["token_id"])
            fill_prob = estimate_maker_fill_probability(
                book=full_book,
                side=sig["side"],
                limit_price=limit_price,
                stake_usd=float(decision.stake_usd),
            )

        # Phase 0 Stage 2: записываем v2_conservative audit columns. AS penalty USD
        # = stake × P_informed × half_spread / fill_price (shares × penalty_per_share).
        as_penalty_usd: float | None = None
        if sim.fill_price and sim.as_penalty_per_share:
            shares = float(decision.stake_usd) / float(sim.fill_price)
            as_penalty_usd = round(shares * sim.as_penalty_per_share, 6)

        order_id = await _insert_order(
            {
                **base,
                "execution_mode": "virtual",
                "order_type": "GTC" if mode == "maker" else "FOK",
                "limit_price": limit_price,
                "status": sim.status if sim.status != "virtual_failed" else "rejected",
                "rejection_reason": sim.reason if sim.status == "virtual_failed" else None,
                # Phase 0 Stage 2 — v2_conservative audit
                "simulation_model_version": sim.simulation_model_version,
                "queue_position_at_placement": sim.queue_position_at_placement,
                "as_penalty_usd": as_penalty_usd,
                "effective_fill_price": sim.effective_fill_price,
            }
        )
        if not order_id:
            return "duplicate"

        # PR1: записываем fill probability в order
        if fill_prob is not None:
            await execute(
                """
                UPDATE execution_orders
                   SET expected_fill_rate = $1,
                       book_depth_usd = $2,
                       book_top_size = $3,
                       fill_probability_reason = $4
                 WHERE id = $5
                """,
                float(fill_prob.fill_rate),
                float(fill_prob.book_depth_at_or_better),
                float(fill_prob.book_top_size),
                fill_prob.reason,
                order_id,
            )

        # Запомнить fill_price + placed_at для filled (чтобы settle посчитал PnL правильно)
        if sim.status == "virtual_filled":
            await execute(
                "UPDATE execution_orders SET fill_price=$1, filled_qty=stake/$1, placed_at=now(), filled_at=now() WHERE id=$2",
                float(sim.fill_price),
                order_id,
            )
            return "opened"
        if sim.status == "virtual_placed":
            await execute(
                "UPDATE execution_orders SET placed_at=now() WHERE id=$1",
                order_id,
            )
            return "opened"
        # virtual_failed
        return "rejected"

    # Insert pending row, потом размещаем; на ошибке — статус 'rejected'.
    order_id_db = await _insert_order(
        {
            **base,
            "execution_mode": mode,
            "order_type": "GTC" if mode == "maker" else "FOK",
            "limit_price": limit_price,
            "status": "intent",
        }
    )
    if not order_id_db:
        return "duplicate"

    place_signal = {
        "token_id": sig["token_id"],
        "stake": float(decision.stake_usd),
        "side": sig["side"],
        "strategy_key": detector,  # для совместимости логов _place_order
    }
    # Taker FOK: потолок цены от живого ask вместо безусловного ухода вглубь книги
    # (audit 2026-07-02 топ-2). current_ask уже посчитан выше как verdict.current_price
    # с фолбэком на sig["entry_price"].
    taker_price_cap = _taker_price_cap(current_ask, float(sig["entry_price"])) if mode == "taker" else None
    # Reference price для expected_size сверки на honest-recovery (см. ниже):
    # maker — сам limit_price; taker — price_cap, иначе stale entry_price.
    reference_price = limit_price if mode == "maker" else (taker_price_cap or float(sig["entry_price"]))
    expected_size = place_signal["stake"] / reference_price if reference_price else None
    from datetime import UTC, datetime

    place_attempt_at = datetime.now(UTC)
    try:
        polymarket_order_id = await _place_order(place_signal, mode, limit_price, price_cap=taker_price_cap)
    except Exception as e:
        # Task 2 (audit топ-4): POST-timeout — сеть могла принять ордер сервером,
        # но ответ не дошёл до нас (ambiguous failure). Не помечаем rejected
        # вслепую — сверяемся с CLOB, ищем наш свежий открытый ордер по token_id.
        # _is_transport_error покрывает случай, когда py_clob_client_v2 уже
        # переупаковал httpx.RequestError в голый PolyApiException(status_code=None)
        # (инцидент 2026-07-18: 2 сигнала протухли, потому что isinstance по httpx-типам
        # не матчился на такой обёрнутый exception и сверка с CLOB не запускалась).
        # expected_size + not_before (audit: ужесточение матчинга) — иначе чужой
        # resting-ордер на том же token_id ложно засчитается как наш (double-counted
        # exposure, битый PnL при резолве).
        import httpx

        from app.services.clob_order_lookup import find_our_open_order
        from app.services.polymarket_service import PolymarketService

        recovered_id = None
        if isinstance(e, httpx.TimeoutException | httpx.TransportError | ConnectionError) or _is_transport_error(e):
            try:
                svc = PolymarketService()
                recovered_id = await find_our_open_order(
                    svc.clob_client,
                    sig["token_id"],
                    expected_size=expected_size,
                    not_before=place_attempt_at,
                )
            except Exception as lookup_exc:
                logger.warning("v2_exec_place_timeout_lookup_failed", error=str(lookup_exc)[:200])
        if recovered_id:
            await execute(
                "UPDATE execution_orders SET status='placed', order_id=$1, placed_at=now() WHERE id=$2",
                recovered_id,
                order_id_db,
            )
            logger.warning(
                "v2_exec_place_timeout_recovered",
                detector=detector,
                market_id=sig["market_id"],
                order_id=recovered_id,
            )
            return "opened"

        # FOK-reanimate (d3-funnel audit 2026-07-16 топ-фикс): "not fully filled"
        # значит книга на price_cap тоньше стейка, а не что сигнал плохой —
        # один ретрай реальным usable-размером книги вместо мгновенного rejected.
        # Ретрай ТОЛЬКО если retry_stake строго меньше original — если книга уже
        # показывает depth >= original_stake, второй FOK по тому же size повторит
        # тот же отказ (book либо не изменилась, либо отказ был не про depth).
        if taker_price_cap is not None and _is_fok_not_filled_error(e):
            original_stake = float(decision.stake_usd)
            fok_reason = str(e)[:200]
            retry_stake = await _fok_retry_stake(sig["token_id"], sig["side"], taker_price_cap, original_stake)
            if retry_stake is not None and retry_stake < original_stake:
                try:
                    retry_order_id = await _place_order(
                        {**place_signal, "stake": retry_stake}, mode, limit_price, price_cap=taker_price_cap
                    )
                    await execute(
                        "UPDATE execution_orders SET status='placed', order_id=$1, stake=$2, placed_at=now() "
                        "WHERE id=$3 AND status='intent'",
                        retry_order_id,
                        retry_stake,
                        order_id_db,
                    )
                    logger.warning(
                        "v2_exec_fok_reanimate_recovered",
                        detector=detector,
                        market_id=sig["market_id"],
                        original_stake=original_stake,
                        retry_stake=retry_stake,
                        order_id=retry_order_id,
                        fok_reason=fok_reason,
                    )
                    retry_decision = dataclasses.replace(decision, stake_usd=retry_stake)
                    try:
                        await _notify_live_bet(sig, retry_decision, mode, retry_order_id, order_id_db)
                    except Exception as notify_exc:
                        logger.warning("v2_exec_live_bet_notify_failed", error=str(notify_exc)[:200])
                    return "opened"
                except Exception as retry_exc:
                    logger.warning(
                        "v2_exec_fok_reanimate_retry_failed",
                        original_reason=fok_reason,
                        retry_error=str(retry_exc)[:200],
                    )

        await execute(
            "UPDATE execution_orders SET status='rejected', rejection_reason=$1, is_transient_reject=true WHERE id=$2",
            f"place_error:{str(e)[:200]}",
            order_id_db,
        )
        logger.warning(
            "v2_exec_place_error",
            detector=detector,
            market_id=sig["market_id"],
            error=str(e)[:200],
        )
        try:
            await publish(
                [
                    Notification(
                        severity=Severity.ACTION_REQUIRED,
                        # Subject по рынку, а не общий: с общим вторая неудача
                        # размещения подавилась бы cooldown'ом первой.
                        subject=f"market:{sig['market_id']}",
                        kind="live_place_failed",
                        headline="Не удалось разместить живую ставку",
                        what_happened=[
                            f"Детектор {detector}, сторона {sig['side']}, "
                            f"размер ${decision.stake_usd:.2f}, режим {mode}.",
                            f"Ошибка: {str(e)[:150]}",
                        ],
                        what_if_ignored=(
                            f"Сигнал на ${decision.stake_usd:.2f} не сыграл: ставка не размещена, деньги не работают."
                        ),
                        what_to_do=(
                            "Посмотри причину отказа: чаще всего это недоступный CLOB, "
                            "геоблок или нехватка ликвидности."
                        ),
                        actions=[],
                        evidence=[f"detector={detector}", f"market={sig['market_id']}", f"mode={mode}"],
                    )
                ],
                actor="v2_execution",
            )
        except Exception as notify_exc:
            logger.warning("v2_exec_place_error_notify_failed", error=str(notify_exc)[:200])
        return "rejected"

    await execute(
        "UPDATE execution_orders SET status='placed', order_id=$1, placed_at=now() WHERE id=$2",
        polymarket_order_id,
        order_id_db,
    )
    logger.info(
        "v2_exec_placed",
        detector=detector,
        market_id=sig["market_id"],
        order_id=polymarket_order_id,
        mode=mode,
        stake=float(decision.stake_usd),
    )

    # Live-режим: уведомить админов о реальной ставке (только live, не virtual —
    # virtual идёт другой веткой выше и сюда не доходит).
    try:
        await _notify_live_bet(sig, decision, mode, polymarket_order_id, order_id_db)
    except Exception as notify_exc:
        logger.warning("v2_exec_live_bet_notify_failed", error=str(notify_exc)[:200])

    return "opened"


async def _notify_live_bet(
    sig: dict, decision: Any, mode: str, order_id: str | None, order_id_db: str | None = None
) -> None:
    """TG-уведомление о реальной live-ставке: почему поставил + экономика + order_id.

    Использует только данные, доступные в момент размещения (sig + SizingDecision).
    EV помечен как теоретический — calibrated_p может быть оптимистичен (см.
    backtest→prod gap). Шлёт всем admin_telegram_ids через send_admin_message.

    order_id_db (execution_orders.id) — если передан, добавляется аварийная кнопка
    "🔴 Закрыть позицию" (audit топ-1: live был BUY-only, выхода не было).
    """
    from app.services.wallet_resolver import fetch_balance
    from app.tasks.notifications import send_admin_message, send_admin_message_with_keyboard

    detector = sig.get("detector", "?")
    side = sig.get("side", "?")
    market_id = str(sig.get("market_id", ""))
    entry = float(sig.get("entry_price") or decision.entry_price or 0.0)
    current = float(sig.get("current_price") or 0.0)
    fair = float(sig.get("fair_price") or 0.0)
    stake = float(decision.stake_usd or 0.0)
    ev_pct = float(getattr(decision, "ev_pct", 0.0) or 0.0)

    # evidence может быть dict или JSON-строкой
    ev_raw = sig.get("evidence")
    if isinstance(ev_raw, str):
        try:
            ev_raw = json.loads(ev_raw)
        except Exception:
            ev_raw = {}
    ev_raw = ev_raw or {}
    yes_sum = ev_raw.get("yes_sum")
    n_markets = ev_raw.get("n_markets")

    # Экономика. payout = 1/entry (стоимость 1 share нашей стороны).
    payout = (1.0 / entry) if entry > 0 else 0.0
    to_win = round(stake * (payout - 1.0), 2) if payout > 1 else 0.0
    to_lose = round(stake, 2)
    ev_usd = round(stake * ev_pct / 100.0, 2)

    # Расхождение fair vs current (в процентных пунктах YES-цены).
    mispricing_pp = round(abs(fair - current) * 100, 1) if (fair and current) else None

    mode_label = "taker FOK" if mode == "taker" else "maker GTC"
    oid_short = (order_id[:10] + "…" + order_id[-6:]) if order_id and len(order_id) > 18 else (order_id or "?")

    lines = [
        f"🔴 <b>LIVE-ставка размещена · {detector}</b>",
        "",
        f"<b>Рынок:</b> <code>{market_id[:24]}…</code>",
        f"<b>Сторона:</b> {side} @ {entry:.3f}  ({mode_label})",
        f"<b>Размер:</b> ${stake:.2f}",
        "",
        "<b>📊 Почему:</b>",
    ]
    why = []
    if yes_sum is not None:
        why.append(f"neg-risk перегрет: Σ(YES)={yes_sum} > 1.20" + (f" по {n_markets} рынкам" if n_markets else ""))
    if mispricing_pp is not None:
        why.append(f"current {current:.3f} vs fair {fair:.3f} → недооценён на {mispricing_pp}пп")
    lines += ["• " + w for w in why] if why else ["• (нет evidence)"]

    lines += [
        "",
        "<b>💰 Экономика:</b>",
        f"• EV ~{ev_pct:.0f}% → теор. прибыль ~${ev_usd} (матожидание, может быть завышено)",
        f"• Выигрыш: +${to_win}  ·  Проигрыш: −${to_lose}",
    ]

    # Bankroll после (best-effort, не критично если упадёт)
    try:
        bal = await fetch_balance()
        if bal is not None:
            lines += ["", f"<b>💼 Bankroll:</b> ${bal:.2f}"]
    except Exception:
        pass

    lines += ["", f"<b>🔗 Order:</b> <code>{oid_short}</code>"]

    text = "\n".join(lines)
    if order_id_db:
        buttons = [{"text": "🔴 Закрыть позицию", "callback_data": f"close_pos:{order_id_db}"}]
        await send_admin_message_with_keyboard(text, buttons)
    else:
        await send_admin_message(text)


async def run_v2_execution() -> dict[str, Any]:
    """Cron entry point. */5 min."""
    from app.services.virtual_execution import reset_rest_quota

    reset_rest_quota()  # сбрасываем REST quota для virtual book fetches
    signals = await fetch_signals_for_active_pipeline()
    counts = {"opened": 0, "rejected": 0, "skipped": 0, "duplicate": 0, "errors": 0}

    for sig in signals:
        try:
            result = await _process_signal(sig)
            counts[result] = counts.get(result, 0) + 1
        except Exception as e:
            counts["errors"] += 1
            logger.error(
                "v2_exec_unhandled",
                detector=sig.get("detector"),
                signal_id=sig.get("detector_signal_id"),
                error=str(e)[:200],
            )

    logger.info("v2_execution_done", signals_seen=len(signals), **counts)
    return {"signals_seen": len(signals), **counts}

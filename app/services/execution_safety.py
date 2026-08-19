"""Execution safety gate — чистые check-функции для V2 execution pipeline.

Каждая функция возвращает tuple[bool, str]: (allowed, reason). Никаких
сторонних эффектов (не пишет в БД, не выставляет ордера). v2_execution.py
вызывает evaluate() и записывает rejection_reason в execution_orders.

Источник логики: legacy execution_job.py (zone-aware drift, book depth,
spread checks). Удалены strategy-based проверки (check_strategy_enabled,
check_daily_limit) — заменены на per-detector gate через detector_state.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from app.core.config import get_settings
from app.core.detector_state import DetectorState
from app.core.horizon_limits import MAX_HORIZON, MAX_HORIZON_DAYS
from app.services.database import fetchrow, fetchval
from app.services.live_prices import get_live_yes_price, seed_book_snapshot
from app.services.price_staleness_telemetry import log_price_staleness

logger = structlog.get_logger(__name__)

# --- Constants ---
# Страховочный cap от зомби-сигналов. Свежесть цены судит drift-гейт
# (check_market_book, live-book) — age больше НЕ прокси свежести
# (anti-selection fix 2026-07-22: 300s-гейт резал сигналы с 96.6% wr).
LIVE_SIGNAL_HARD_MAX_AGE_SEC: int = 1800
LIVE_PRICE_DRIFT_MAX: float = 0.05  # zone 0.50+
LIVE_PRICE_DRIFT_MAX_LONGSHOT: float = 0.50  # zone < 0.30 (favorite_longshot edge)
LIVE_PRICE_DRIFT_MAX_MIDLOW: float = 0.30  # zone 0.30-0.50
LIVE_MAX_SPREAD_PCT: float = 0.35
LIVE_MIN_BOOK_DEPTH_MULTIPLIER: float = 2.0
LIVE_MIN_BEST_LEVEL_SIZE_USD: float = 20.0
LIVE_DAILY_LOSS_PCT: float = 0.05  # 5% bankroll = ~$25 на $500
LIVE_BALANCE_BUFFER_USD: float = 10.0


@dataclass(frozen=True)
class SafetyVerdict:
    allowed: bool
    reason: str  # пустая если allowed
    current_price: float | None = None  # перезаписан если check_price_drift refetched
    book_depth_usd: float | None = None  # bid_size+ask_size из check_market_book (None если книга не фетчилась)


# --- Pure (no I/O) checks ---


def zone_drift_threshold(price: float) -> float:
    """Zone-aware drift threshold (HF ablation 2026-04-27)."""
    if price < 0.30:
        return LIVE_PRICE_DRIFT_MAX_LONGSHOT
    if price < 0.50:
        return LIVE_PRICE_DRIFT_MAX_MIDLOW
    return LIVE_PRICE_DRIFT_MAX


def check_signal_age(generated_at: datetime | None) -> tuple[bool, str]:
    """Reject если signal старше 30 минут (страховочный cap; свежесть судит drift-гейт)."""
    if generated_at is None:
        return True, ""
    if generated_at.tzinfo is None:
        generated_at = generated_at.replace(tzinfo=UTC)
    age_sec = (datetime.now(UTC) - generated_at).total_seconds()
    if age_sec > LIVE_SIGNAL_HARD_MAX_AGE_SEC:
        return False, f"stale_signal:{age_sec:.0f}s>{LIVE_SIGNAL_HARD_MAX_AGE_SEC}s"
    return True, ""


def _entry_price_gated_detectors() -> frozenset[str]:
    raw = get_settings().entry_price_gate_detectors or ""
    return frozenset(part.strip() for part in raw.split(",") if part.strip())


def check_entry_price(detector: str, signal_price: float) -> tuple[bool, str]:
    """Гейт цены входа для детекторов с доказанной зависимостью edge от входа.

    Замер 2026-08-05 по 986 отрезолвленным сигналам D3 за 60д (taker-симуляция:
    вход = цена сигнала + реальный slip 0.0451 по 215 фактическим fill'ам, −2% fee):
    вход <0.5 → +60.6% ROI net (устойчиво 13/13 недель), 0.6-0.7 → −22.7%,
    0.7-0.8 → −51.6%. Порог — параметр конфига: точки перегиба в данных нет,
    0.50 выбран владельцем как компромисс объёма и ROI.

    Гейт per-detector осознанно: эффект есть у 8 из 9 активных детекторов, но
    пороги разные, а у D36 эффекта нет вовсе — общий порог навредил бы без выигрыша.

    ⚠️ Живёт в live-ветке evaluate(), поэтому НЕ покрывает basket-путь D29/D40
    (v2_execution.py:953 обходит safety_evaluate целиком). Сейчас безопасно —
    gated-детекторы не basket; при расширении списка это надо продублировать там.
    """
    if detector not in _entry_price_gated_detectors():
        return True, ""
    threshold = get_settings().entry_price_gate_max
    if signal_price >= threshold:
        return False, f"entry_price_too_high:{signal_price:.4f}>={threshold:.2f}"
    return True, ""


def check_kill_switch() -> tuple[bool, str]:
    """Базовый kill-switch (env). Дополнительно см. async_check_kill_switches()."""
    if not get_settings().execution_enabled:
        return False, "kill_switch:execution_enabled=false"
    return True, ""


# ─── Расширенные kill-switches (PR8) ─────────────────────────────────────────


DAILY_LOSS_LIMIT_PCT = 0.03  # -3% bankroll/день — auto-stop
BUCKET_DRAWDOWN_THRESHOLD_PCT = -0.05  # bucket потерял 5% от вложенного → disable
# Phase 1 Stage 1: MtM-aware Daily PnL — позиции с unrealized_pnl_last свежее
# этого порога включаются в aggregate sum. Stale → conservative skip.
# Cron mtm-refresh запускается каждую минуту, поэтому 300s даёт 5× retry окно.
MTM_STALE_THRESHOLD_SEC = 300


async def check_redis_kill_flag() -> tuple[bool, str]:
    """Manual kill-switch через Redis (сейчас снимается только redis-cli DEL по SSH;
    API/TG control-plane — Phase 1 плана docs/superpowers/plans/2026-07-02-audit-fixes-phase1.md)."""
    from app.core.cache import get_redis

    redis = await get_redis()
    if redis is None:
        return True, ""  # Redis down — не блокируем (production принимает решение)
    flag = await redis.get("bot:kill_switch")
    if flag:
        return False, "kill_switch:redis_flag_set"
    return True, ""


async def check_daily_pnl_limit(bankroll_usd: float) -> tuple[bool, str]:
    """Если cumulative live PnL today < -DAILY_LOSS_LIMIT_PCT × bankroll → stop.

    Per-day reset: проверяем PnL только за UTC сегодня.

    DEPRECATED (Phase 1 Stage 1): используй check_daily_pnl_limit_mtm для
    preventive (unrealized-aware) kill switch. Эта функция оставлена для
    backward-compat, но evaluate() переключился на MtM версию.
    """
    from app.services.database import fetchrow

    row = await fetchrow(
        """
        SELECT COALESCE(SUM(net_pnl), 0) AS today_pnl
          FROM execution_orders
         WHERE execution_mode != 'virtual'
           AND status IN ('settled', 'virtual_settled')
           AND settled_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
        """
    )
    today_pnl = float(row["today_pnl"]) if row and row["today_pnl"] else 0.0
    threshold = -bankroll_usd * DAILY_LOSS_LIMIT_PCT
    if today_pnl < threshold:
        return False, f"daily_loss_limit:today_pnl=${today_pnl:.2f}<threshold=${threshold:.2f}"
    return True, ""


async def _fetch_pnl_mtm_aggregate(stale_threshold_sec: int) -> dict[str, float] | None:
    """Single query: realized_today + sum(unrealized_pnl_last) для свежих MtM.

    Returns:
        {"realized_today": float, "unrealized_fresh": float} или None если БД
        полностью недоступна (отдельный exception path).

    Stale-skip: позиции с mtm_updated_at NULL или старше stale_threshold_sec
    исключаются из aggregate. Это conservative — мы не блокируем торговлю
    из-за устаревшей картины unrealized, но и не считаем устаревшие данные
    в свою пользу.
    """
    from app.services.database import fetchrow

    row = await fetchrow(
        """
        SELECT
            COALESCE((
                SELECT SUM(net_pnl)
                  FROM execution_orders
                 WHERE execution_mode != 'virtual'
                   AND status IN ('settled', 'virtual_settled')
                   AND settled_at >= date_trunc('day', now() AT TIME ZONE 'UTC')
            ), 0)::float AS realized_today,
            COALESCE((
                SELECT SUM(unrealized_pnl_last)
                  FROM execution_orders
                 WHERE status IN ('placed', 'filled')
                   AND settled_at IS NULL
                   AND mtm_updated_at IS NOT NULL
                   AND mtm_updated_at >= now() - make_interval(secs => $1)
            ), 0)::float AS unrealized_fresh
        """,
        stale_threshold_sec,
    )
    if row is None:
        return None
    return {
        "realized_today": float(row["realized_today"] or 0.0),
        "unrealized_fresh": float(row["unrealized_fresh"] or 0.0),
    }


async def check_daily_pnl_limit_mtm(bankroll_usd: float) -> tuple[bool, str]:
    """Phase 1 Stage 1: MtM-aware Daily PnL kill switch.

    total_pnl = realized_today + sum(unrealized_pnl_last) для open positions
                с свежим MtM (mtm_updated_at >= now() − MTM_STALE_THRESHOLD_SEC).
    Если total_pnl < -DAILY_LOSS_LIMIT_PCT × bankroll → block.

    Stale-aware: позиции без свежего MtM пропускаются (conservative — не
    блокируем без данных). Только live positions учитываются в unrealized
    (virtual_placed/virtual_filled не блокируют real execution).

    См. docs/superpowers/plans/2026-05-risk-engine-hardening/01-mtm-daily-pnl.md.
    """
    agg = await _fetch_pnl_mtm_aggregate(MTM_STALE_THRESHOLD_SEC)
    if agg is None:
        # DB вернула NULL row — нет данных, разрешаем (отсутствие данных не
        # повод блокировать; reactive check daily_loss всё равно работает).
        return True, ""

    realized = agg["realized_today"]
    unrealized = agg["unrealized_fresh"]
    total = realized + unrealized
    threshold = -bankroll_usd * DAILY_LOSS_LIMIT_PCT

    if total < threshold:
        return False, (
            f"daily_loss_limit_mtm:total=${total:.2f} "
            f"(realized=${realized:.2f}, unrealized=${unrealized:.2f}) "
            f"<threshold=${threshold:.2f}"
        )
    return True, ""


async def check_bucket_disabled(detector: str, market_id: str) -> tuple[bool, str]:
    """Per-bucket auto-disable. Bucket = (detector, category, entry_zone).

    В Redis хранится set 'bot:disabled_buckets' с keys типа
    'D5_long_horizon_extreme_fade:Politics:lt40'. Если bucket в set →
    отклоняем сигнал.

    Сами keys пишутся через cron (PR8 add: bucket_drawdown_check) или
    manually через API endpoint.
    """
    from app.core.cache import get_redis

    redis = await get_redis()
    if redis is None:
        return True, ""

    row = await fetchrow(
        "SELECT category, current_price, clob_token_ids->>0 AS yes_token_id FROM markets WHERE polymarket_id = $1",
        market_id,
    )
    if not row:
        return True, ""  # без category не можем определить bucket — пропускаем

    cat = row["category"] or "unknown"
    db_price = float(row["current_price"] or 0)

    # Shadow-телеметрия (План 1): передаём db-цену — она меряет расхождение
    # db vs live, живая цена ей не нужна. Fire-and-forget.
    asyncio.create_task(log_price_staleness("bucket_gate", market_id, row.get("yes_token_id"), db_price))

    # REST-посев (nine-point-audit 2026-07-05): bucket_gate живёт на db-цене
    # (live_coverage=0%, в отличие от mtm=81.9% после 12d5ec6b) — mtm_refresh.py
    # сеет книги только для MtM-цикла, bucket_gate их не получал. Throttle
    # SETNX 240с внутри seed_book_snapshot — безопасно звать на каждый тик.
    asyncio.create_task(seed_book_snapshot(row.get("yes_token_id")))

    # Fix-1 (latency-аудит 2026-07-06): зона считается по live-цене из WS-кэша,
    # db current_price — только fallback внутри get_live_yes_price.
    price, _is_live = await get_live_yes_price(row.get("yes_token_id"), db_price)

    if price < 0.40:
        zone = "lt40"
    elif price < 0.60:
        zone = "40-60"
    elif price < 0.80:
        zone = "60-80"
    elif price < 0.95:
        zone = "80-95"
    else:
        zone = "gte95_blocked"

    bucket_key = f"{detector}:{cat}:{zone}"
    is_disabled = await redis.sismember("bot:disabled_buckets", bucket_key)
    if is_disabled:
        return False, f"bucket_disabled:{bucket_key}"
    return True, ""


def check_detector_live(state: DetectorState | None) -> tuple[bool, str]:
    if state is None:
        return False, "detector_state_missing"
    if state.stage != "live":
        return False, f"detector_stage:{state.stage}"
    return True, ""


async def check_daemon_pause(detector: str) -> tuple[bool, str]:
    """Per-detector пауза, взведённая risk_daemon (Task 4, audit-fixes phase1).

    risk_daemon.py НЕ пишет в БД (архитектурный контракт: демон только читает
    state и пишет в Redis) — при anomaly с ОДНИМ детектором-нарушителем он
    изолирует его через SADD bot:daemon_paused_detectors вместо глобального
    kill_switch. Этот чек читает тот же Redis-set и блокирует новые
    live-ордера только для конкретного детектора.

    Снимается через TG callback unlock_detector:<detector> (app/api/telegram.py).
    """
    from app.core.cache import get_redis

    redis = await get_redis()
    if redis is None:
        return True, ""  # Redis down — не блокируем (fail-open, как остальные Redis-checks)
    if await redis.sismember("bot:daemon_paused_detectors", detector):
        return False, f"daemon_paused:{detector}"
    return True, ""


def check_horizon_cap(market_end_date: datetime | None) -> tuple[bool, str]:
    """Reject если до резолва маркета больше MAX_HORIZON_DAYS (=30).

    Глобальный horizon-cap. SSoT: app/core/horizon_limits.py.
    NULL end_date — отказ (без даты резолва не можем проверить).
    """
    if market_end_date is None:
        return False, "missing_market_end_date"
    if market_end_date.tzinfo is None:
        market_end_date = market_end_date.replace(tzinfo=UTC)
    delta = market_end_date - datetime.now(UTC)
    if delta > MAX_HORIZON:
        return False, f"horizon_exceeds_{MAX_HORIZON_DAYS}d:{delta.days}d"
    return True, ""


def check_balance(stake_usd: float, bankroll_usd: float) -> tuple[bool, str]:
    required = stake_usd + LIVE_BALANCE_BUFFER_USD
    if bankroll_usd < required:
        return False, f"insufficient_balance:{bankroll_usd:.2f}<{required:.2f}"
    return True, ""


# --- I/O checks (требуют DB / book fetch) ---


async def check_max_inflight_orders() -> tuple[bool, str]:
    """Анти-флуд: working placed-ордера (ещё не filled/rejected/cancelled)."""
    settings = get_settings()
    n = await fetchval("SELECT COUNT(*) FROM execution_orders WHERE status = 'placed'")
    if (n or 0) >= settings.live_max_inflight_orders:
        return False, f"max_inflight_orders:{n}>={settings.live_max_inflight_orders}"
    return True, ""


async def check_max_open_positions() -> tuple[bool, str]:
    """Страховка поверх exposure-caps risk_engine: held filled-позиции."""
    settings = get_settings()
    n = await fetchval("SELECT COUNT(*) FROM execution_orders WHERE status = 'filled'")
    if (n or 0) >= settings.live_max_open_positions:
        return False, f"max_open_positions:{n}>={settings.live_max_open_positions}"
    return True, ""


async def check_daily_loss(bankroll_usd: float) -> tuple[bool, str]:
    """Reject если сегодня уже потеряно >5% bankroll по settled ордерам."""
    today_pnl = await fetchval(
        """
        SELECT COALESCE(SUM(COALESCE(net_pnl, pnl)), 0) FROM execution_orders
        WHERE status = 'settled' AND settled_at::date = CURRENT_DATE
        """
    )
    today_pnl_f = float(today_pnl or 0)
    cap = -bankroll_usd * LIVE_DAILY_LOSS_PCT
    if today_pnl_f < cap:
        return False, f"daily_loss:{today_pnl_f:.2f}<{cap:.2f}"
    return True, ""


async def fetch_live_book(token_id: str) -> dict[str, Any] | None:
    """Fetch best bid/ask + sizes из WS cache (быстро) → REST fallback.

    Returns: {"bid": float, "ask": float, "bid_size": float, "ask_size": float} или None.
    """
    from app.core.cache import cache_get
    from app.services.live_prices import is_ws_snapshot_live

    try:
        ws = await cache_get(f"microstructure:{token_id}")
        # State-TTL (2026-07-03): свежесть решает epoch-гейт, протухший хит → REST
        if ws and isinstance(ws, dict) and await is_ws_snapshot_live(ws):
            b = ws.get("best_bid")
            a = ws.get("best_ask")
            bs = ws.get("best_bid_size")
            as_ = ws.get("best_ask_size")
            if b is not None and a is not None and bs is not None and as_ is not None:
                bf, af = float(b), float(a)
                if 0.0 < bf < 1.0 and 0.0 < af < 1.0:
                    return {"bid": bf, "ask": af, "bid_size": float(bs), "ask_size": float(as_)}
    except Exception as e:
        logger.debug("ws_cache_miss", token_id=token_id[:16], error=str(e)[:80])

    try:
        from app.services.polymarket_poller_service import get_poller_service

        book = await get_poller_service().fetch_order_book_rest(token_id)
    except Exception as e:
        logger.warning("book_fetch_error", token_id=token_id[:16], error=str(e)[:100])
        return None
    if not book:
        return None
    asks = book.get("asks") or []
    bids = book.get("bids") or []
    if not asks or not bids:
        return None
    # Polymarket book ordering (discovered 2026-05-13):
    #   - bids ascending price → best bid = max(prices)
    #   - asks descending price → best ask = min(prices)
    # Использование [0] index давало worst-of-book → fake spreads (~0.01/0.99).
    # См. virtual_execution.py:_fetch_book_rest для reference implementation.
    try:
        bid_levels: dict[float, float] = {}
        for b in bids:
            p = float(b["price"])
            s = float(b["size"])
            if 0.0 < p < 1.0 and s > 0:
                bid_levels[p] = bid_levels.get(p, 0.0) + s
        ask_levels: dict[float, float] = {}
        for a in asks:
            p = float(a["price"])
            s = float(a["size"])
            if 0.0 < p < 1.0 and s > 0:
                ask_levels[p] = ask_levels.get(p, 0.0) + s
        if not bid_levels or not ask_levels:
            return None
        best_bid = max(bid_levels)
        best_ask = min(ask_levels)
        return {
            "bid": best_bid,
            "ask": best_ask,
            "bid_size": bid_levels[best_bid],
            "ask_size": ask_levels[best_ask],
        }
    except (ValueError, TypeError, KeyError):
        return None


async def check_market_book(
    token_id: str, signal_price: float, stake_usd: float
) -> tuple[bool, str, float | None, float | None]:
    """Объединённый book-check: spread + depth + price drift.

    Returns: (allowed, reason, current_ask, depth_usd).
    current_ask/depth_usd = None при market_closed/malformed book.
    """
    book = await fetch_live_book(token_id)
    if book is None:
        return False, "market_closed_or_empty_book", None, None

    bid, ask = book["bid"], book["ask"]
    if bid >= ask:
        return False, "malformed_book", None, None

    depth = book["bid_size"] + book["ask_size"]

    mid = (bid + ask) / 2.0
    spread_pct = (ask - bid) / mid
    if spread_pct > LIVE_MAX_SPREAD_PCT:
        return False, f"wide_spread:{spread_pct:.1%}>{LIVE_MAX_SPREAD_PCT:.0%}", ask, depth

    required = max(stake_usd * LIVE_MIN_BOOK_DEPTH_MULTIPLIER, LIVE_MIN_BEST_LEVEL_SIZE_USD)
    if depth < required:
        return False, f"thin_book:depth_{depth:.1f}_need_{required:.1f}", ask, depth

    if signal_price > 0:
        threshold = zone_drift_threshold(signal_price)
        drift = abs(ask - signal_price) / signal_price
        if drift > threshold:
            return False, f"price_drift:{drift:.1%}>{threshold:.0%}", ask, depth

    return True, "", ask, depth


# --- Top-level gate ---


@dataclass(frozen=True)
class SafetyContext:
    detector: str
    detector_state: DetectorState | None
    market_id: str
    token_id: str
    side: str
    signal_price: float
    stake_usd: float
    bankroll_usd: float
    generated_at: datetime | None
    market_end_date: datetime | None = None
    detector_signal_id: Any | None = None  # для shadow_evaluate_virtual / gate_shadow_log (uuid в БД)


async def evaluate(ctx: SafetyContext, *, mode: str) -> SafetyVerdict:
    """Полный safety gate. mode: 'shadow' | 'virtual' | 'live'.

    Shadow / Virtual: всё разрешено — мы пишем intent или paper position,
    не размещаем ордер на CLOB. Stale signals, kill_switch, тонкие book'и
    и пр. не должны блокировать paper-позицию.
    Live: kill_switch, detector_live, balance, max_open, daily_loss, signal_age,
    market_book (spread + depth + drift).
    """
    if mode in ("shadow", "virtual"):
        return SafetyVerdict(True, "")

    # Live-only checks
    ok, reason = check_horizon_cap(ctx.market_end_date)
    if not ok:
        return SafetyVerdict(False, reason)

    ok, reason = check_signal_age(ctx.generated_at)
    if not ok:
        return SafetyVerdict(False, reason)

    # До async-чеков и book-I/O: чистая арифметика дешевле того, что она отсекает.
    ok, reason = check_entry_price(ctx.detector, ctx.signal_price)
    if not ok:
        return SafetyVerdict(False, reason)

    ok, reason = check_kill_switch()
    if not ok:
        return SafetyVerdict(False, reason)

    # PR8: Redis kill flag (manual, снимается через redis-cli DEL — см. docstring выше)
    ok, reason = await check_redis_kill_flag()
    if not ok:
        return SafetyVerdict(False, reason)

    ok, reason = check_detector_live(ctx.detector_state)
    if not ok:
        return SafetyVerdict(False, reason)

    # Task 4: per-detector daemon-пауза (изоляция одиночного anomaly-нарушителя)
    ok, reason = await check_daemon_pause(ctx.detector)
    if not ok:
        return SafetyVerdict(False, reason)

    # PR8: Per-bucket auto-disable (set via cron bucket_drawdown_check)
    ok, reason = await check_bucket_disabled(ctx.detector, ctx.market_id)
    if not ok:
        return SafetyVerdict(False, reason)

    ok, reason = check_balance(ctx.stake_usd, ctx.bankroll_usd)
    if not ok:
        return SafetyVerdict(False, reason)

    ok, reason = await check_max_inflight_orders()
    if not ok:
        return SafetyVerdict(False, reason)

    ok, reason = await check_max_open_positions()
    if not ok:
        return SafetyVerdict(False, reason)

    ok, reason = await check_daily_loss(ctx.bankroll_usd)
    if not ok:
        return SafetyVerdict(False, reason)

    # Phase 1 Stage 1: MtM-aware Daily PnL (realized + unrealized fresh, -3% bankroll → stop)
    ok, reason = await check_daily_pnl_limit_mtm(ctx.bankroll_usd)
    if not ok:
        return SafetyVerdict(False, reason)

    ok, reason, current, depth = await check_market_book(ctx.token_id, ctx.signal_price, ctx.stake_usd)
    if not ok:
        return SafetyVerdict(False, reason, current_price=current, book_depth_usd=depth)

    return SafetyVerdict(True, "", current_price=current, book_depth_usd=depth)


async def shadow_evaluate_virtual(ctx: SafetyContext) -> None:
    """Shadow-прогон live-гейтов на virtual-потоке (spec 2026-07-22 §5).

    НЕ блокирует — только пишет would-reject в gate_shadow_log с префиксом
    shadow_v2:. Верификация anti-selection фикса: через 1-2 недели join
    gate_shadow_log × detector_signals.outcome должен показать, что
    would-rejected больше не концентрирует победителей.
    Только чеки без book-I/O (429-фон): age-cap, horizon-cap, max_open,
    max_inflight, daily_loss (MtM). Best-effort: никогда не бросает.

    2026-08-05: shadow звал DEPRECATED `check_daily_loss`, тогда как live-путь
    судит по `check_daily_pnl_limit_mtm` (unrealized-aware). Shadow измерял НЕ ТОТ
    гейт, который реально отклоняет — и вопрос «режут ли лимиты хорошие сигналы»
    по этим данным был неотвечаем. Теперь зовём ту же функцию, что и live.
    Добавлены horizon_cap и max_inflight_orders — тоже без book-I/O и тоже
    участвуют в live-решении.
    """
    from app.services.gate_shadow_telemetry import log_gate_shadow

    try:
        would_rejects: list[str] = []
        ok, _ = check_signal_age(ctx.generated_at)
        if not ok:
            would_rejects.append("stale_signal")
        ok, _ = check_horizon_cap(ctx.market_end_date)
        if not ok:
            would_rejects.append("horizon_cap")
        # D3 сейчас в virtual, а live-ветка evaluate() при virtual отдаёт allow
        # сразу — до промоута это единственный источник данных о том, что гейт режет.
        ok, _ = check_entry_price(ctx.detector, ctx.signal_price)
        if not ok:
            would_rejects.append("entry_price")
        ok, _ = await check_max_open_positions()
        if not ok:
            would_rejects.append("max_open_positions")
        ok, _ = await check_max_inflight_orders()
        if not ok:
            would_rejects.append("max_inflight_orders")
        # Та же функция, что в live-ветке evaluate() — иначе shadow измеряет
        # другой гейт и данные не отвечают на вопрос о live-отказах.
        ok, _ = await check_daily_pnl_limit_mtm(ctx.bankroll_usd)
        if not ok:
            would_rejects.append("daily_loss_limit_mtm")
        for gate in would_rejects:
            await log_gate_shadow(
                detector=ctx.detector,
                gate=f"shadow_v2:{gate}",
                detector_signal_id=ctx.detector_signal_id,
                stake_usd=ctx.stake_usd,
                token_id=None,  # без book-fetch — дорого для virtual-потока
            )
    except Exception as exc:  # noqa: BLE001 — телеметрия не роняет money-path
        logger.warning("shadow_evaluate_virtual_failed", detector=ctx.detector, error=str(exc))


# --- Order mode decision ---


def decide_mode(book_depth_usd: float, signal_age_sec: float, ev_pct: float) -> str:
    """Hybrid taker/maker. Polymarket fee=0% — выбор по slippage и speed.

    - Тонкий book OR горящий сигнал OR высокий EV → taker (FOK), берём сразу.
    - Иначе maker (GTC), отменяем через 5 минут если не filled.
    """
    if book_depth_usd < 100.0 or signal_age_sec > 180.0 or ev_pct > 15.0:
        return "taker"
    return "maker"


def resolve_execution_mode(detector: str, *, book_depth_usd: float, signal_age_sec: float, ev_pct: float) -> str:
    """decide_mode + принудительный taker для детекторов с гейтом цены входа.

    Гейт и режим осмысленны только вместе: пропустить сигнал по цене и снова
    не исполниться лимиткой = ничего не изменить. Механизм adverse selection —
    maker получает fill преимущественно когда цена ушла против нас (диагноз
    2026-08-01), поэтому в прибыльной полосе мы платим спред и берём сразу.

    Для прочих детекторов эвристика decide_mode не меняется.
    """
    if detector in _entry_price_gated_detectors():
        return "taker"
    return decide_mode(book_depth_usd=book_depth_usd, signal_age_sec=signal_age_sec, ev_pct=ev_pct)

"""Detector adapter — convert canonical row → market_event dict для detect().

Currently supports D44 only. Add other detectors as needed.

Look-ahead bias prevention: каждый detect() вызов получает `now` parameter
из snapshot timestamp.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from app.detectors.contract import Signal

# Type alias для detect() function
DetectFunc = Callable[..., Signal | None]


# Per-detector mapping
_REGISTRY: dict[str, DetectFunc] = {}


def register_detector(name: str, detect_func: DetectFunc) -> None:
    """Register detector by name (lowercase)."""
    _REGISTRY[name.lower()] = detect_func


def get_detector(name: str) -> DetectFunc:
    """Lookup detect function by name."""
    name = name.lower()
    if name not in _REGISTRY:
        raise KeyError(f"detector '{name}' not registered. Available: {sorted(_REGISTRY)}")
    return _REGISTRY[name]


# Detectors с REQUIRED-полем, которое в backtest приходит ТОЛЬКО из trade_buckets.
# Без use_trade_buckets такой detect() вернёт None на каждом тике → 0 сигналов
# без ошибки (ложное «детектор мёртв»). run_backtest hard-error'ит, см. ниже.
# D14: buy_imbalance_asof (REQUIRED) считается из buckets в build_market_event_at_tick.
_BUCKETS_REQUIRED: frozenset[str] = frozenset({"d14"})


def requires_trade_buckets(name: str) -> bool:
    """True если detector нельзя backtest-нуть без use_trade_buckets (REQUIRED bucket-фича)."""
    return name.lower() in _BUCKETS_REQUIRED


def _make_now_compat(detect_func: DetectFunc) -> DetectFunc:
    """Wrap detect that doesn't accept `now` param — silently drop it.

    Большинство detectors не принимают `now` (только D44 после PR #459).
    Wrapper позволяет передавать `now` универсально из ReplayEngine.
    """

    def _wrapped(market_event: dict[str, Any], now: datetime | None = None) -> Signal | None:
        # now игнорируется — detector использует datetime.now(UTC) inside,
        # но т.к. canonical уже даёт `hours_to_resolve` (offset, not absolute),
        # большинство detectors зависят от hours_to_resolve, не от now.
        return detect_func(market_event)

    return _wrapped


def _register_builtin_detectors() -> None:
    """Side-effect: register all backtestable detectors."""
    # D44 — single market_event, accepts now natively
    from app.detectors import d44_late_convergence_carry

    register_detector("d44", d44_late_convergence_carry.detect)

    # Simple market_event detectors (no now param, use hours_to_resolve from event)
    from app.detectors import (
        d14_crypto_winners_distribute_no,
        d26_fade_whale_extreme,
    )

    register_detector("d14", _make_now_compat(d14_crypto_winners_distribute_no.detect))
    register_detector("d26", _make_now_compat(d26_fade_whale_extreme.detect))


_register_builtin_detectors()


_WALLET_ANCHORS = (4, 24, 72)


def _pit_safe_anchor(hours_to_resolve: float) -> int | None:
    """Smallest anchor A∈{4,24,72} с A >= hours_to_resolve, иначе None.

    Anchor A соответствует моменту `end_date − A` с data-окном
    `[end_date − A − 24h, end_date − A)` (build_wallet_score_at_anchor.py:5,80).
    Тик на hours_to_resolve=H находится в `end_date − H`. Если бы брали anchor
    A < H (ближайший по модулю), окно якоря кончалось бы в `end_date − A` —
    ПОЗЖЕ тика → look-ahead (в smart-money фичу попали бы сделки после момента
    сигнала). Поэтому берём наименьший A >= H: окно кончается в `end_date − A`
    <= `end_date − H`, т.е. на момент тика или раньше (stale, но PIT-safe).
    H > 72 → нет безопасного якоря → None → detector сам skip на None-фичах.
    """
    safe = [a for a in _WALLET_ANCHORS if a >= hours_to_resolve]
    return min(safe) if safe else None


def build_market_event_at_tick(
    market_row: dict[str, Any],
    snapshot_row: dict[str, Any],
    *,
    snapshots_for_market: list[dict[str, Any]] | None = None,
    snapshot_idx: int | None = None,
    trade_buckets_for_market: list[dict[str, Any]] | None = None,
    wallet_score_for_market: dict[int, dict] | None = None,
) -> tuple[dict[str, Any], datetime]:
    """Reconstruct market_event dict + tick datetime for detector.

    Args:
        market_row: row from markets_clean.parquet (per-market metadata)
        snapshot_row: row from price_snapshots.parquet (per-tick state)

    Returns:
        (market_event_dict, tick_now). tick_now is tz-aware UTC.

    Notes:
        - volume_24h / liquidity не доступны per-snapshot в canonical →
          используем lifetime_volume_usd / horizon_days как PROXY.
        - clob_token_ids missing → None (D44 не падает на None, token_resolver
          возвращает None gracefully).
    """
    # trade_ts is unix seconds (uint64)
    trade_ts = int(snapshot_row["trade_ts"])
    tick_now = datetime.fromtimestamp(trade_ts, tz=UTC)

    # end_date = tick_now + hours_to_resolve. Canonical уже даёт relative offset
    # (precisely the time-to-resolution at snapshot).
    hours_to_resolve = int(snapshot_row["hours_to_resolve"])
    end_date = tick_now + timedelta(hours=hours_to_resolve)

    # volume_24h / liquidity: prefer TRUE rolling 24h from trade_buckets
    # (если переданы). Fallback к lifetime-proxy если trade_buckets отсутствуют.
    #
    # lifetime-proxy завышает liquidity для underdog markets с короткими
    # horizon_days (median canonical = 2 days). На D7b backtest 2026-03 → 2026-05
    # это давало density 4376 sig vs ~1260 в prod (3× over). См. PR #498 context
    # (research/d7b-autoresearch-2026-05/gap_findings.md).
    lifetime_vol = float(market_row.get("lifetime_volume_usd") or 0.0)
    horizon_days = max(1.0, float(market_row.get("horizon_days") or 1.0))
    if trade_buckets_for_market is not None:
        from app.backtest.feature_builder import aggregate_trade_buckets_window

        agg_24h = aggregate_trade_buckets_window(trade_buckets_for_market, trade_ts, 24)
        true_volume_24h = float(agg_24h["volume_usd"])
        # Liquidity proxy: 24h volume × 0.05 — empirical literature value для
        # prediction markets order book depth as share of daily turnover.
        true_liquidity = true_volume_24h * 0.05
        volume_24h_value = true_volume_24h
        liquidity_value = true_liquidity
    else:
        # Legacy proxy: lifetime / horizon × 0.1. Сохранили для backwards compat
        # тестов и режима use_trade_buckets=False.
        daily_volume_proxy = lifetime_vol / horizon_days
        volume_24h_value = daily_volume_proxy
        liquidity_value = daily_volume_proxy * 0.1

    # Category: canonical хранит Title-case ('Sports', 'Crypto'). D44 lowercase'ит
    # внутри сам, остальные detectors сравнивают Title-case → передаём в native form.
    raw_category = market_row.get("category") or ""

    market_event = {
        "market_id": market_row["condition_id"],
        "condition_id": market_row["condition_id"],
        "event_id": market_row.get("event_id"),
        "category": raw_category,
        "event_title": market_row.get("event_title") or "",
        "question": market_row.get("question") or "",
        "current_price": float(snapshot_row["yes_price"]),
        "entry_price": float(snapshot_row["yes_price"]),  # D5/D13/D14 alias
        "end_date": end_date,
        "hours_to_resolve": hours_to_resolve,
        "horizon_days": float(market_row.get("horizon_days") or 1.0),  # D5
        "volume_24h": volume_24h_value,
        "liquidity": liquidity_value,
        "clob_token_ids": None,
        "neg_risk": bool(market_row.get("neg_risk", False)),
        # Fake tx_hash (some detectors use as idempotency key, replay-safe)
        "tx_hash": f"backtest_{market_row['condition_id'][:16]}_{trade_ts}",
    }

    # Feature engineering: compute runtime-style фичи если переданы history.
    # Опционально — без history feature-dependent detectors (D34/D41/D_esports)
    # просто получат None/0 и сами skip.
    if snapshots_for_market is not None and snapshot_idx is not None:
        from app.backtest.feature_builder import yes_price_at_offset

        # D34: yes_price_72h_ago
        market_event["yes_price_72h_ago"] = yes_price_at_offset(snapshots_for_market, snapshot_idx, offset_hours=72)

    if trade_buckets_for_market is not None:
        from app.backtest.feature_builder import (
            aggregate_trade_buckets_window,
            compute_buy_imbalance_asof,
            compute_momentum_pp,
        )

        # D41 momentum 3h window
        agg_3h = aggregate_trade_buckets_window(trade_buckets_for_market, trade_ts, 3)
        market_event["momentum_3h_pp"] = compute_momentum_pp(trade_buckets_for_market, trade_ts, 3)
        market_event["volume_3h_usd"] = agg_3h["volume_usd"]
        market_event["n_takers_3h"] = agg_3h["n_takers"]
        market_event["buy_volume_3h_usd"] = agg_3h["buy_volume_usd"]
        market_event["sell_volume_3h_usd"] = agg_3h["sell_volume_usd"]

        # D_esports: price_min_asof — min yes_price от market start до now
        agg_all = aggregate_trade_buckets_window(trade_buckets_for_market, trade_ts, window_hours=24 * 365)
        market_event["price_min_asof"] = agg_all["min_yes_price"]

        # D14: buy_imbalance_asof = buy/(buy+sell) в 24h окне (PIT-safe).
        market_event["buy_imbalance_asof"] = compute_buy_imbalance_asof(
            trade_buckets_for_market, trade_ts, window_hours=24
        )

        # D26/D34 3h/1h shares + whale flow требуют live wallet classification,
        # не реконструируемы из canonical. Detectors сами skip на None.
        market_event["smart_money_taker_share_3h"] = None
        market_event["smart_money_taker_share_1h"] = None
        market_event["fade_whale_push_usd_15min"] = 0.0
        market_event["fade_whale_drift_pp_15min"] = 0.0

    # Wallet quality из wallet_score_at_anchor (PIT-safe: anchor >= hours_to_resolve,
    # окно якоря не позже момента тика). D34 нужен smart_money_taker_share_4h
    # независимо от trade_buckets.
    wrow = None
    if wallet_score_for_market is not None:
        anchor = _pit_safe_anchor(hours_to_resolve)
        wrow = wallet_score_for_market.get(anchor) if anchor is not None else None
    market_event["smart_money_taker_share_4h"] = float(wrow["smart_money_taker_share"]) if wrow else None
    market_event["taker_pnl_weighted_usd_24h"] = float(wrow["taker_pnl_weighted_usd"]) if wrow else None
    market_event["taker_roi_weighted_24h"] = float(wrow["taker_roi_weighted"]) if wrow else None
    market_event["n_takers_24h_before"] = int(wrow["n_takers_24h_before"]) if wrow else None

    return market_event, tick_now

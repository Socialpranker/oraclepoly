"""Feature builder — compute runtime-style features из canonical history.

D41 momentum / D34 retail-rally / D_esports / future detectors требуют поля,
которые в live приходят из RuntimeFeatureBuilder (3h/4h/72h windows). На
backtest заменяем их window-aggregations от canonical trade_buckets +
price_snapshots.

Pure functions, no I/O. Каждая принимает sorted history + current row.
"""

from __future__ import annotations

from typing import Any


def yes_price_at_offset(
    snapshots_sorted: list[dict[str, Any]],
    current_idx: int,
    offset_hours: int,
) -> float | None:
    """Yes_price ~ offset_hours ago from snapshots_sorted[current_idx].

    snapshots_sorted: descending по hours_to_resolve (от far к close).
    Возвращает yes_price ближайшего snapshot где hours_to_resolve =
    current.hours_to_resolve + offset_hours (раньше на N часов).
    """
    if current_idx >= len(snapshots_sorted) or current_idx < 0:
        return None
    current = snapshots_sorted[current_idx]
    target_htr = int(current["hours_to_resolve"]) + offset_hours
    # Linear search — snapshots на 1 market обычно <100 rows
    best_match: dict[str, Any] | None = None
    best_diff = float("inf")
    for snap in snapshots_sorted:
        htr = int(snap["hours_to_resolve"])
        diff = abs(htr - target_htr)
        if diff < best_diff:
            best_diff = diff
            best_match = snap
    # Tolerate ±2h match
    if best_match is None or best_diff > 2:
        return None
    return float(best_match["yes_price"])


def aggregate_trade_buckets_window(
    buckets_for_market: list[dict[str, Any]],
    tick_ts: int,
    window_hours: int,
) -> dict[str, Any]:
    """Aggregate trade_buckets за [tick_ts - window_hours, tick_ts].

    Возвращает dict с: volume_usd, n_takers, buy_volume_usd, sell_volume_usd,
    vwap_yes, n_trades, min_yes_price, max_yes_price. Если buckets пустые
    в окне → все nullable zeros.

    Args:
        buckets_for_market: trade_buckets rows for one condition_id,
            with `hour_bucket` parsed to unix seconds in field `bucket_ts_unix`.
        tick_ts: unix seconds of current tick
        window_hours: lookback window
    """
    start_ts = tick_ts - window_hours * 3600
    matches = [b for b in buckets_for_market if start_ts <= int(b["bucket_ts_unix"]) <= tick_ts]
    if not matches:
        return {
            "volume_usd": 0.0,
            "n_takers": 0,
            "buy_volume_usd": 0.0,
            "sell_volume_usd": 0.0,
            "vwap_yes": None,
            "n_trades": 0,
            "min_yes_price": None,
            "max_yes_price": None,
        }
    total_vol = sum(float(b.get("total_volume_usd") or 0) for b in matches)
    n_takers = sum(int(b.get("n_distinct_takers") or 0) for b in matches)
    buy_vol = sum(float(b.get("buy_volume") or 0) for b in matches)
    sell_vol = sum(float(b.get("sell_volume") or 0) for b in matches)
    n_trades = sum(int(b.get("n_trades") or 0) for b in matches)
    weighted_vwap_num = sum(
        float(b.get("vwap_yes") or 0) * float(b.get("total_volume_usd") or 0)
        for b in matches
        if b.get("vwap_yes") is not None
    )
    vwap = (weighted_vwap_num / total_vol) if total_vol > 0 else None
    min_p = min(
        (float(b["min_yes_price"]) for b in matches if b.get("min_yes_price") is not None),
        default=None,
    )
    max_p = max(
        (float(b["max_yes_price"]) for b in matches if b.get("max_yes_price") is not None),
        default=None,
    )
    return {
        "volume_usd": total_vol,
        "n_takers": n_takers,
        "buy_volume_usd": buy_vol,
        "sell_volume_usd": sell_vol,
        "vwap_yes": vwap,
        "n_trades": n_trades,
        "min_yes_price": min_p,
        "max_yes_price": max_p,
    }


def compute_momentum_pp(
    buckets_for_market: list[dict[str, Any]],
    tick_ts: int,
    window_hours: int,
) -> float | None:
    """yes_price now − yes_price (now − window). Через vwap_yes buckets.

    Возвращает None если в одном из окон нет данных. Иначе — float pp
    (positive = momentum up).
    """
    now_window = aggregate_trade_buckets_window(buckets_for_market, tick_ts, 1)
    past_window = aggregate_trade_buckets_window(
        buckets_for_market,
        tick_ts - window_hours * 3600,
        1,
    )
    if now_window["vwap_yes"] is None or past_window["vwap_yes"] is None:
        return None
    return now_window["vwap_yes"] - past_window["vwap_yes"]


def compute_buy_imbalance_asof(
    buckets_for_market: list[dict[str, Any]],
    cutoff_ts: int,
    window_hours: int = 24,
) -> float:
    """Buy imbalance ratio = buy / (buy + sell) в окне [cutoff - window, cutoff].

    PIT-safe: сделки после cutoff_ts исключены (через aggregate_trade_buckets_window).
    Возвращает 0.5 (нейтрально) при пустом окне — совпадает с прод d13_scan default
    (d13_scan.py:167, shared by D14). D14 detector ждёт ratio 0..1, фильтр < 0.45.
    """
    agg = aggregate_trade_buckets_window(buckets_for_market, cutoff_ts, window_hours)
    buy = float(agg["buy_volume_usd"])
    sell = float(agg["sell_volume_usd"])
    total = buy + sell
    if total <= 0:
        return 0.5
    return buy / total

"""PnL-weighted holding period — диагностика hidden tail risk.

Phase 0 Stage 5. См. docs/superpowers/plans/2026-05-honest-baseline/05-pnl-weighted-holding.md.

Документ §3.5: "Если 80% быстрых сделок по $5, 20% медленных по $500 —
count-weighted медиана обманчива. Hidden tail risk: реальный exposure
концентрирован в медленных tail-сделках."

Helper для:
- /v2/lifecycle/live_readiness (расширенный response — рядом с legacy
  settle_velocity bucketize)
- cron app/jobs/hidden_tail_risk_check.py (опциональный daily check, Stage 5
  follow-up)
"""

from __future__ import annotations

from typing import Any

# Buckets границы (часы)
_UNDER_24 = 24.0
_D1_3 = 72.0
_D3_7 = 168.0

# Threshold для hidden_tail_risk_flag: ratio pnl_median / count_median.
# Документ §3.5: "Если расхождение > 2× — скрытый tail risk".
HIDDEN_TAIL_RISK_THRESHOLD = 2.0


def weighted_median(values: list[float], weights: list[float]) -> float:
    """Weighted median: sort by value, cumulative weight reaches 50% of total at median.

    Args:
        values: list значений (e.g. hours).
        weights: parallel list весов (e.g. abs(net_pnl) для каждой сделки).

    Returns:
        Weighted median. 0.0 при empty input или total_weight=0.

    Raises:
        ValueError если len(values) != len(weights).
    """
    if len(values) != len(weights):
        raise ValueError(f"length mismatch: values={len(values)}, weights={len(weights)}")
    if not values:
        return 0.0

    pairs = sorted(zip(values, weights, strict=True), key=lambda p: p[0])
    total_w = sum(w for _, w in pairs)
    if total_w <= 0:
        return 0.0

    cumulative = 0.0
    target = total_w / 2.0
    for value, weight in pairs:
        cumulative += weight
        if cumulative >= target:
            return value
    return pairs[-1][0]


def bucketize_settle_hours_weighted(
    hours_list: list[float],
    weights_list: list[float],
) -> dict[str, Any]:
    """Распределение settled hours по buckets с PnL-weighted статистикой.

    Args:
        hours_list: hours_to_settle per сделка.
        weights_list: abs(net_pnl) либо stake_usd per сделка (parallel массив).

    Returns:
        {
          "buckets_count_weighted": {under_24h_pct, d1_3_pct, d3_7_pct, over_7d_pct},
          "buckets_pnl_weighted": {...},
          "count_median_hours": float | None,
          "pnl_weighted_median_hours": float | None,
          "pnl_to_count_median_ratio": float | None,
          "hidden_tail_risk_flag": bool,
          "n": int,
          "total_pnl_weight": float,
        }

    На empty input возвращает structure с None medians и flag=False.
    """
    n = len(hours_list)
    if n == 0 or len(weights_list) != n:
        return {
            "buckets_count_weighted": None,
            "buckets_pnl_weighted": None,
            "count_median_hours": None,
            "pnl_weighted_median_hours": None,
            "pnl_to_count_median_ratio": None,
            "hidden_tail_risk_flag": False,
            "n": n,
            "total_pnl_weight": 0.0,
        }

    total_w = sum(weights_list)

    # Count-weighted buckets (legacy compat)
    under_24_c = sum(1 for h in hours_list if h < _UNDER_24)
    d1_3_c = sum(1 for h in hours_list if _UNDER_24 <= h < _D1_3)
    d3_7_c = sum(1 for h in hours_list if _D1_3 <= h < _D3_7)
    over_7_c = sum(1 for h in hours_list if h >= _D3_7)

    # PnL-weighted buckets
    under_24_w = sum(w for h, w in zip(hours_list, weights_list, strict=True) if h < _UNDER_24)
    d1_3_w = sum(w for h, w in zip(hours_list, weights_list, strict=True) if _UNDER_24 <= h < _D1_3)
    d3_7_w = sum(w for h, w in zip(hours_list, weights_list, strict=True) if _D1_3 <= h < _D3_7)
    over_7_w = sum(w for h, w in zip(hours_list, weights_list, strict=True) if h >= _D3_7)

    # Medians
    sorted_h = sorted(hours_list)
    mid = n // 2
    count_median = sorted_h[mid] if n % 2 == 1 else (sorted_h[mid - 1] + sorted_h[mid]) / 2
    pnl_median = weighted_median(hours_list, weights_list)

    ratio = (pnl_median / max(count_median, 1e-9)) if count_median > 0 else None
    hidden_flag = bool(ratio is not None and ratio > HIDDEN_TAIL_RISK_THRESHOLD)

    return {
        "buckets_count_weighted": {
            "under_24h_pct": round(under_24_c / n * 100, 1),
            "d1_3_pct": round(d1_3_c / n * 100, 1),
            "d3_7_pct": round(d3_7_c / n * 100, 1),
            "over_7d_pct": round(over_7_c / n * 100, 1),
        },
        "buckets_pnl_weighted": {
            "under_24h_pct": round(under_24_w / total_w * 100, 1) if total_w > 0 else 0.0,
            "d1_3_pct": round(d1_3_w / total_w * 100, 1) if total_w > 0 else 0.0,
            "d3_7_pct": round(d3_7_w / total_w * 100, 1) if total_w > 0 else 0.0,
            "over_7d_pct": round(over_7_w / total_w * 100, 1) if total_w > 0 else 0.0,
        },
        "count_median_hours": round(count_median, 2),
        "pnl_weighted_median_hours": round(pnl_median, 2),
        "pnl_to_count_median_ratio": round(ratio, 2) if ratio is not None else None,
        "hidden_tail_risk_flag": hidden_flag,
        "n": n,
        "total_pnl_weight": round(total_w, 2),
    }

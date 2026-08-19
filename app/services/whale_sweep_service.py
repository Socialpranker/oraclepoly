"""Whale-Sweep Service — detect informed flow via rapid book sweeping.

Гипотеза H9 (research/polymarket-alpha-hypotheses-2026-05/2026-05-12.md):
  Large taker order, sweeping ≥3 price levels in <30 seconds = informed signal.
  Continuation 15-30 min, directional accuracy 58-65%.

Use case:
  Не standalone alpha. Вместо этого — **confidence booster / direction filter**
  для других detectors. Когда whale активно sweep'ит book в ту же direction
  как detector signal → boost. В противоположную → reject.

Sweep definition:
  - ≥3 transactions от ОДНОГО whale (whale_id) в same direction
  - within 30-second window
  - total volume ≥ $1,000 USD
  - на same market

Integration:
  v2_execution.py:execute_signal()
    → whale_sweep_service.get_sweep_state(market_id, side)
    → boost / reject decision

Sources:
  [S11] arXiv 2025 — informed flow detection
  [S14] 3% informed traders drive prediction market accuracy
  [S20] Ng et al. 2026 — large trades predict returns
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import structlog

from app.services.database import fetch

logger = structlog.get_logger(__name__)


# --- Configuration ---------------------------------------------------------

# Sweep detection thresholds.
# Original H9 спec: ≥3 trades in <30 sec. Но нашa transactions feed получает
# whale txes через polling (median gap 6.9 min), поэтому ≥3 в 30 sec крайне редко.
# Relaxed: ≥2 trades в <60 sec — empirically 21 такие events за 24h на нашем feed.
MIN_TRADES_FOR_SWEEP: int = 2
SWEEP_WINDOW_SEC: int = 60
MIN_SWEEP_VOLUME_USD: float = 1000.0

# Lookback window для query
LOOKBACK_MINUTES: int = 60

# Continuation horizon — sweep сигнал релевантен 30 минут после
SWEEP_RELEVANT_MINUTES: int = 30


@dataclass(frozen=True)
class SweepState:
    """Состояние whale sweep activity на market.

    Attributes:
        market_id: UUID market в нашей БД.
        direction: 'YES' (последний sweep purchase YES tokens) | 'NO' | None (no sweep).
        n_sweeps: количество detected sweeps за lookback window.
        total_volume_usd: cumulative volume в всех sweeps.
        last_sweep_age_sec: seconds since последнего sweep (None если нет).
        confidence_multiplier: рекомендуемый multiplier на signal confidence.
    """

    market_id: str
    direction: Literal["YES", "NO"] | None
    n_sweeps: int
    total_volume_usd: float
    last_sweep_age_sec: float | None
    confidence_multiplier: float


_SWEEP_QUERY_SQL = """
WITH ranked AS (
  SELECT
      market_id,
      whale_id,
      LOWER(outcome) AS outcome,
      direction,
      amount,
      price,
      block_timestamp,
      LAG(block_timestamp) OVER (
        PARTITION BY market_id, whale_id, LOWER(outcome), direction
        ORDER BY block_timestamp
      ) AS prev_ts
    FROM transactions
   WHERE block_timestamp > now() - make_interval(mins => $1)
     AND amount IS NOT NULL
     AND market_id IS NOT NULL
     AND LOWER(outcome) IN ('yes','no')
     AND direction IN ('buy','sell')
),
sweeps AS (
  -- ≥2 trades same whale, same direction, within SWEEP_WINDOW_SEC
  SELECT
      market_id::text  AS market_id,
      whale_id,
      outcome,
      direction,
      block_timestamp,
      amount,
      price
    FROM ranked
   WHERE prev_ts IS NOT NULL
     AND EXTRACT(EPOCH FROM (block_timestamp - prev_ts)) <= $2
)
SELECT
    market_id,
    outcome,
    direction,
    count(*) AS n_sweeps,
    sum(amount * price)::float AS total_volume_usd,
    max(block_timestamp) AS last_sweep_ts,
    EXTRACT(EPOCH FROM (now() - max(block_timestamp))) AS last_age_sec
  FROM sweeps
 GROUP BY market_id, outcome, direction
HAVING sum(amount * price) >= $3
"""


async def fetch_active_sweeps() -> dict[str, SweepState]:
    """Return dict market_id → SweepState для всех active sweeps в lookback window.

    "Active" = sweep detected И последний trade был < SWEEP_RELEVANT_MINUTES назад.
    """
    rows = await fetch(
        _SWEEP_QUERY_SQL,
        LOOKBACK_MINUTES,
        SWEEP_WINDOW_SEC,
        MIN_SWEEP_VOLUME_USD,
    )

    sweeps_by_market: dict[str, list[dict]] = {}
    for r in rows:
        age = float(r["last_age_sec"])
        if age > SWEEP_RELEVANT_MINUTES * 60:
            continue
        mid = r["market_id"]
        sweeps_by_market.setdefault(mid, []).append(
            {
                "outcome": r["outcome"],
                "direction": r["direction"],
                "n_sweeps": int(r["n_sweeps"]),
                "volume_usd": float(r["total_volume_usd"]),
                "age_sec": age,
            }
        )

    # Aggregate: на market несколько sweep'ов могут существовать — берём dominant.
    result: dict[str, SweepState] = {}
    for mid, entries in sweeps_by_market.items():
        # Find dominant direction by total volume
        # YES purchase = buying YES (direction='buy', outcome='yes')
        # NO purchase = buying NO (direction='buy', outcome='no')
        # YES sell = direction='sell', outcome='yes' → effective NO pressure
        yes_pressure = 0.0
        no_pressure = 0.0
        total_volume = 0.0
        max_n_sweeps = 0
        min_age = None
        for e in entries:
            vol = e["volume_usd"]
            total_volume += vol
            max_n_sweeps = max(max_n_sweeps, e["n_sweeps"])
            if min_age is None or e["age_sec"] < min_age:
                min_age = e["age_sec"]
            outcome = e["outcome"]
            direction = e["direction"]
            if (outcome == "yes" and direction == "buy") or (outcome == "no" and direction == "sell"):
                yes_pressure += vol
            else:
                no_pressure += vol

        net_pressure = yes_pressure - no_pressure
        if abs(net_pressure) < MIN_SWEEP_VOLUME_USD:
            # Conflicting sweeps cancel — treat as no signal
            continue
        direction_label = "YES" if net_pressure > 0 else "NO"

        # Confidence multiplier scales with sweep magnitude.
        #   1 sweep small volume:  1.05x
        #   2 sweeps mid volume:   1.15x
        #   3+ sweeps high volume: 1.30x (cap)
        if max_n_sweeps >= 3 and total_volume >= 10000:
            mult = 1.30
        elif max_n_sweeps >= 2 and total_volume >= 3000:
            mult = 1.15
        else:
            mult = 1.05

        result[mid] = SweepState(
            market_id=mid,
            direction=direction_label,
            n_sweeps=max_n_sweeps,
            total_volume_usd=round(total_volume, 2),
            last_sweep_age_sec=min_age,
            confidence_multiplier=mult,
        )

    return result


async def get_sweep_state(market_id: str) -> SweepState | None:
    """Return SweepState для single market_id или None если нет sweep.

    Convenience wrapper над fetch_active_sweeps. Для batch use лучше fetch_active_sweeps.
    """
    all_sweeps = await fetch_active_sweeps()
    return all_sweeps.get(market_id)


def apply_sweep_adjustment(
    *,
    side: str,
    base_confidence: float,
    sweep: SweepState | None,
) -> tuple[float, str]:
    """Apply sweep multiplier к detector signal confidence.

    Args:
        side: signal direction ('YES' | 'NO').
        base_confidence: original detector confidence.
        sweep: sweep state for market (None если no sweep).

    Returns:
        (adjusted_confidence, reason_label).
        reason_label values:
          - "no_sweep" — нет active sweep, без изменения
          - "sweep_confirms" — sweep direction == signal side, boosted
          - "sweep_contra" — sweep direction != signal side, REJECT (return 0.0)
    """
    if sweep is None or sweep.direction is None:
        return base_confidence, "no_sweep"

    if sweep.direction == side:
        # Confirming sweep — boost confidence (capped at 1.0)
        adjusted = min(1.0, base_confidence * sweep.confidence_multiplier)
        return adjusted, "sweep_confirms"

    # Contra-sweep — informed trader betting against us. REJECT.
    return 0.0, "sweep_contra"

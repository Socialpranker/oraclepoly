"""Per-detector live/shadow stage tracking.

Заменяет strategy_state (со 137 dyn_* стратегиями из B2C-эпохи) на простую
таблицу со state per-detector (D1, D3-D6, D6b). Promotion ручная:

    UPDATE detector_state SET stage='live', promoted_at=now()
    WHERE detector='D1_fade_whale';

Используется в execution_safety.evaluate() как gate перед placement.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from app.services.database import execute, fetchrow


@dataclass(frozen=True)
class DetectorState:
    detector: str
    stage: str  # 'shadow' | 'virtual' | 'live' | 'paused'
    paused_reason: str | None
    promoted_at: datetime | None
    fractional_kelly_today: float
    portfolio_id: str | None = None  # Phase 1: NOT NULL after migration backfill
    # Phase 3 Task 2 (audit research-gap): кодификация ramp-лесенки. Потолок
    # per-trade stake для live mode; None = без потолка (нет ramp-лимита).
    # См. docs/superpowers/plans/2026-06-25-d3-live-proxy-ramp.md §3.1.
    live_stake_cap_usd: float | None = None


async def get_state(detector: str) -> DetectorState | None:
    row = await fetchrow(
        """
        SELECT detector, stage, paused_reason, promoted_at,
               fractional_kelly_today, portfolio_id, live_stake_cap_usd
        FROM detector_state WHERE detector = $1
        """,
        detector,
    )
    if not row:
        return None
    return DetectorState(
        detector=row["detector"],
        stage=row["stage"],
        paused_reason=row["paused_reason"],
        promoted_at=row["promoted_at"],
        fractional_kelly_today=float(row["fractional_kelly_today"]),
        portfolio_id=row["portfolio_id"],
        live_stake_cap_usd=float(row["live_stake_cap_usd"]) if row["live_stake_cap_usd"] is not None else None,
    )


async def is_live(detector: str) -> bool:
    state = await get_state(detector)
    return state is not None and state.stage == "live"


async def mark_paused(detector: str, reason: str) -> None:
    await execute(
        """
        UPDATE detector_state
           SET stage = 'paused',
               paused_reason = $2,
               updated_at = now()
         WHERE detector = $1
        """,
        detector,
        reason[:500],
    )

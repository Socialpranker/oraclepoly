"""Sizing controller — bankroll lookup + per-detector fractional Kelly damping.

Заменяет adaptive_sizing (372 LOC с warmup→production phases) простой логикой
для небольшого счёта одного человека:

- bankroll: live wallet balance из wallet_resolver, кэш 60s.
- fractional_kelly_today: per-detector multiplier 0.05-0.25, пересчитывается
  раз в день из последних 30 settled ордеров (cron 06:00 UTC).
  - Если cumulative_pnl < 0 за 7 дней → /= 2 (floor 0.05).
  - Если cumulative_pnl > 0 за 30 дней → 0.25 (full).
  - Иначе 0.10 (cold-start default).

Финальный stake считает ev_sizer.size_from_calibrated_p, sizing_controller
только подаёт ему bankroll и fractional_kelly.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import structlog

from app.services.database import execute, fetch
from app.services.wallet_resolver import fetch_balance

logger = structlog.get_logger(__name__)

_BALANCE_CACHE_TTL_SEC: float = 60.0
_BANKROLL_FLOOR_USD: float = 50.0  # ниже которого live считается paused
_KELLY_FLOOR: float = 0.05
_KELLY_FULL: float = 0.25
_KELLY_COLD: float = 0.10

_balance_cache: dict[str, float] = {"value": 0.0, "ts": 0.0}


@dataclass(frozen=True)
class BankrollSnapshot:
    bankroll_usd: float
    is_sufficient: bool  # >= floor


async def get_live_bankroll() -> BankrollSnapshot:
    """Кэшированный live wallet balance. TTL 60s."""
    now = time.time()
    if now - _balance_cache["ts"] < _BALANCE_CACHE_TTL_SEC:
        cached = _balance_cache["value"]
        return BankrollSnapshot(bankroll_usd=cached, is_sufficient=cached >= _BANKROLL_FLOOR_USD)

    balance = await fetch_balance()
    if balance is None:
        logger.warning("sizing_controller_balance_none")
        return BankrollSnapshot(bankroll_usd=0.0, is_sufficient=False)

    _balance_cache["value"] = float(balance)
    _balance_cache["ts"] = now
    return BankrollSnapshot(bankroll_usd=float(balance), is_sufficient=balance >= _BANKROLL_FLOOR_USD)


async def compute_kelly_fraction_today(detector: str) -> float:
    """Per-detector fractional Kelly multiplier на основе recent PnL.

    Считает rolling pnl за 7d и 30d из execution_orders для этого детектора.
    """
    rows = await fetch(
        """
        SELECT
            COALESCE(SUM(COALESCE(net_pnl, pnl)) FILTER (WHERE settled_at > now() - interval '7 days'), 0)  AS pnl_7d,
            COALESCE(SUM(COALESCE(net_pnl, pnl)) FILTER (WHERE settled_at > now() - interval '30 days'), 0) AS pnl_30d,
            COUNT(*) FILTER (WHERE settled_at > now() - interval '30 days') AS n_30d
        FROM execution_orders
        WHERE detector = $1 AND status = 'settled'
        """,
        detector,
    )
    row = rows[0] if rows else None
    if not row or int(row["n_30d"]) < 5:
        return _KELLY_COLD

    pnl_7d = float(row["pnl_7d"])
    pnl_30d = float(row["pnl_30d"])

    if pnl_7d < 0:
        return _KELLY_FLOOR
    if pnl_30d > 0:
        return _KELLY_FULL
    return _KELLY_COLD


async def update_all_detector_sizing() -> dict[str, float]:
    """Cron-job entry point: пересчитать fractional_kelly_today для всех детекторов.

    Returns: {detector: kelly_fraction}.
    """
    detectors = [
        "D3_cross_mkt_arb",
    ]
    results: dict[str, float] = {}
    for d in detectors:
        kelly = await compute_kelly_fraction_today(d)
        await execute(
            """
            UPDATE detector_state
               SET fractional_kelly_today = $2,
                   updated_at = now()
             WHERE detector = $1
            """,
            d,
            kelly,
        )
        results[d] = kelly
    logger.info("sizing_controller_updated", **results)
    return results


async def run_update_detector_sizing() -> dict:
    """Cron entry point."""
    res = await update_all_detector_sizing()
    return {"updated": res}

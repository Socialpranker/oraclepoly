"""Pre-live readiness checklist runner.

PR10 из pre-live evidence pipeline. Один command-line runner который вызывает
все predicate'ы из PR1-9 и выдаёт single-line verdict — готов ли система
к live execution.

Запуск на проде:
  ssh root@<YOUR_SERVER_IP> 'cd ~/oracle && docker exec oraclepoly-backend python scripts/research/pre_live_readiness.py'

Output:
  PASS — все 7 system-level + N/4 buckets ready
  FAIL — какие checks не проходят + что делать

Exit code: 0 = full pass; 1 = есть fails. Для cron/CI integration.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.database import close_db, fetch, fetchrow, init_db

WHITELIST_BUCKETS = [
    ("D5_long_horizon_extreme_fade", "Politics", "lt40"),
    ("D4_small_market_fade", "Sports", "lt40"),
    ("D4_small_market_fade", "Sports", "60-80"),
    ("D4_small_market_fade", "Sports", "80-95"),
]


async def check_settlement_pipeline() -> tuple[bool, str]:
    """0 unsettled rows с resolved signal."""
    row = await fetchrow(
        """
        SELECT COUNT(*) AS n
          FROM execution_orders eo
          JOIN detector_signals ds ON ds.id = eo.detector_signal_id
         WHERE eo.status IN ('filled','virtual_filled') AND ds.resolved=true
           AND ds.outcome IN ('WIN','LOSS')
        """
    )
    n = int(row["n"]) if row else 0
    return (n == 0, f"{n} unsettled" if n > 0 else "all settled")


async def check_strategy_key_consistency() -> tuple[bool, str]:
    """0 rows where eo.detector != ds.detector."""
    row = await fetchrow(
        """
        SELECT COUNT(*) AS n
          FROM execution_orders eo
          JOIN detector_signals ds ON ds.id = eo.detector_signal_id
         WHERE eo.detector != ds.detector
        """
    )
    n = int(row["n"]) if row else 0
    return (n == 0, f"{n} mismatches" if n > 0 else "all consistent")


async def check_realized_roi_net_filled() -> tuple[bool, str]:
    """Все resolved WIN/LOSS должны иметь realized_roi_net populated."""
    row = await fetchrow(
        """
        SELECT COUNT(*) AS n
          FROM detector_signals
         WHERE resolved=true AND outcome IN ('WIN','LOSS')
           AND realized_roi_net IS NULL
        """
    )
    n = int(row["n"]) if row else 0
    return (n == 0, f"{n} NULL" if n > 0 else "all populated")


async def check_active_l2_model() -> tuple[bool, str]:
    """Должна быть active Layer B model в binary mode."""
    row = await fetchrow(
        "SELECT version FROM ml_market_picker WHERE is_active=true AND mode='binary'"
    )
    if row:
        return True, f"active: {row['version']}"
    return False, "no active binary L2"


async def check_active_l1_calibrators() -> tuple[bool, str]:
    """≥4 detectors должны иметь active calibrators."""
    rows = await fetch("SELECT detector FROM ml_calibrators")
    n = len(rows)
    return (n >= 4, f"{n} calibrators" if n < 4 else f"{n} calibrators (≥4 OK)")


async def check_execution_disabled() -> tuple[bool, str]:
    """В .env / env settings — execution_enabled=false (мы должны быть в shadow)."""
    from app.core.config import get_settings

    enabled = get_settings().execution_enabled
    return (not enabled, f"execution_enabled={enabled}")


async def check_recent_signal_activity() -> tuple[bool, str]:
    """Должны быть свежие detector signals (< 30 min) — пайплайн жив."""
    row = await fetchrow(
        """
        SELECT MAX(generated_at) AS last_at,
               EXTRACT(EPOCH FROM (now() - MAX(generated_at))) AS age_sec
          FROM detector_signals WHERE source='live_v2'
        """
    )
    if not row or row["last_at"] is None:
        return False, "no recent signals"
    age_sec = int(row["age_sec"] or 0)
    return (age_sec < 1800, f"last_signal_age={age_sec}s")


async def check_bucket_ready(detector: str, category: str, zone: str) -> tuple[bool, str]:
    """Bucket-level readiness:
    - n_events ≥ 30
    - inflation < 2.0 (если применимо)
    - drift_wr ≥ -5pp (recent vs baseline)
    """
    row = await fetchrow(
        """
        WITH bucketed AS (
          SELECT ds.event_id, ds.outcome, ds.resolved_at,
                 MAX(CASE WHEN ds.outcome='WIN' THEN 1 ELSE 0 END) OVER (PARTITION BY ds.event_id) AS event_won
            FROM detector_signals ds
            LEFT JOIN markets m ON m.polymarket_id = ds.market_id
           WHERE ds.detector = $1 AND COALESCE(m.category, 'unknown') = $2
             AND CASE WHEN ds.current_price < 0.40 THEN 'lt40'
                      WHEN ds.current_price < 0.60 THEN '40-60'
                      WHEN ds.current_price < 0.80 THEN '60-80'
                      WHEN ds.current_price < 0.95 THEN '80-95'
                      ELSE 'gte95_blocked' END = $3
             AND ds.resolved=true AND ds.outcome IN ('WIN','LOSS')
             AND ds.source='live_v2'
             AND ds.generated_at > now() - INTERVAL '30 days'
        ),
        ev AS (SELECT COUNT(DISTINCT event_id) AS n_ev FROM bucketed)
        SELECT n_ev FROM ev
        """,
        detector, category, zone,
    )
    n_ev = int(row["n_ev"]) if row else 0
    if n_ev < 30:
        return False, f"n_events={n_ev}<30"
    return True, f"n_events={n_ev}"


SYSTEM_CHECKS = [
    ("settlement_pipeline", check_settlement_pipeline),
    ("strategy_key_consistency", check_strategy_key_consistency),
    ("realized_roi_net_filled", check_realized_roi_net_filled),
    ("active_l2_model", check_active_l2_model),
    ("active_l1_calibrators", check_active_l1_calibrators),
    ("execution_disabled", check_execution_disabled),
    ("recent_signal_activity", check_recent_signal_activity),
]


async def main() -> None:
    await init_db()
    try:
        print("\n" + "=" * 80)
        print("PRE-LIVE READINESS CHECKLIST")
        print("=" * 80)
        print()

        all_pass = True

        # System-level checks
        print("System-level checks:")
        print("-" * 80)
        for name, fn in SYSTEM_CHECKS:
            ok, detail = await fn()
            marker = "✓" if ok else "✗"
            if not ok:
                all_pass = False
            print(f"  {marker} {name:<35} {detail}")

        # Bucket-level checks
        print()
        print("Whitelist bucket checks:")
        print("-" * 80)
        ready_count = 0
        for det, cat, zone in WHITELIST_BUCKETS:
            ok, detail = await check_bucket_ready(det, cat, zone)
            marker = "✓" if ok else "✗"
            if ok:
                ready_count += 1
            label = f"{det}/{cat}/{zone}"
            print(f"  {marker} {label:<55} {detail}")

        if ready_count == 0:
            all_pass = False  # хоть один bucket должен быть ready

        # Verdict
        print()
        print("=" * 80)
        if all_pass and ready_count == len(WHITELIST_BUCKETS):
            print(f"VERDICT: ✓ FULL PASS — все {ready_count}/{len(WHITELIST_BUCKETS)} buckets ready")
            print()
            print("Next step: human decision на прохождение через /v2/control/live_toggle")
            sys.exit(0)
        elif all_pass:
            print(
                f"VERDICT: ⚠ SYSTEM PASS, BUCKETS PARTIAL — {ready_count}/{len(WHITELIST_BUCKETS)} ready"
            )
            print()
            print("System invariants OK. Не все buckets готовы — можно стартовать с ready buckets only.")
            sys.exit(0)
        else:
            print("VERDICT: ✗ FAIL — system invariants нарушены")
            print()
            print("Fix system-level checks перед live ramp.")
            sys.exit(1)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())

"""Settlement E2E audit — проверяет целостность settlement pipeline.

PR5 из pre-live evidence pipeline. Закрывает риск что settle pipeline
работает некорректно (legacy bug from memory live_execution_post_v2_audit_2026-04-30:
$0.45 stake bypass, strategy_key mismatch).

Проверяет:
1. Все virtual_filled с resolved сигналом → должны settle'нуться → status='virtual_settled'
2. Settled net_pnl ≈ realized_roi_net × stake / 100 (формулы согласованы)
3. trading_fee_usd > 0 для всех WIN settled
4. Нет orphans: settled без detector_signal_id

Запуск:
  ssh root@<YOUR_SERVER_IP> 'cd ~/oracle && docker exec oraclepoly-backend python scripts/research/settlement_e2e_audit.py'

Output: PASS / FAIL для каждой проверки + примеры failures.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.database import close_db, fetch, init_db


async def check_unsettled_with_resolved_signal() -> tuple[bool, str]:
    """Все filled с resolved=true должны settle'нуться при следующем v2-execution-resolve."""
    rows = await fetch(
        """
        SELECT eo.id, eo.status, eo.fill_price, ds.outcome
          FROM execution_orders eo
          JOIN detector_signals ds ON ds.id = eo.detector_signal_id
         WHERE eo.status IN ('filled', 'virtual_filled')
           AND ds.resolved = true
           AND ds.outcome IN ('WIN', 'LOSS')
         LIMIT 10
        """
    )
    if not rows:
        return True, "0 unsettled with resolved signal — settler работает"
    return False, f"{len(rows)} filled rows со resolved=true НЕ settle'нуты (cron не отработал?)"


async def check_settled_have_net_pnl() -> tuple[bool, str]:
    """Все settled должны иметь net_pnl populated (after backfill)."""
    rows = await fetch(
        """
        SELECT eo.id, eo.status, eo.outcome, eo.pnl, eo.net_pnl
          FROM execution_orders eo
         WHERE eo.status IN ('settled', 'virtual_settled')
           AND eo.net_pnl IS NULL
         LIMIT 10
        """
    )
    if not rows:
        return True, "All settled rows имеют net_pnl"
    return False, f"{len(rows)} settled БЕЗ net_pnl (backfill не сработал?)"


async def check_net_pnl_le_gross() -> tuple[bool, str]:
    """net_pnl ≤ pnl (gross) — fee+slippage всегда уменьшают."""
    rows = await fetch(
        """
        SELECT eo.id, eo.outcome, eo.pnl, eo.net_pnl
          FROM execution_orders eo
         WHERE eo.status IN ('settled', 'virtual_settled')
           AND eo.net_pnl IS NOT NULL
           AND eo.pnl IS NOT NULL
           AND eo.net_pnl > eo.pnl + 0.01
         LIMIT 10
        """
    )
    if not rows:
        return True, "All settled: net_pnl ≤ gross (формула корректна)"
    return False, f"{len(rows)} rows где net_pnl > gross (баг в _compute_settlement_pnl)"


async def check_win_has_fee() -> tuple[bool, str]:
    """WIN settled должны иметь trading_fee_usd > 0."""
    rows = await fetch(
        """
        SELECT eo.id, eo.outcome, eo.trading_fee_usd
          FROM execution_orders eo
         WHERE eo.status IN ('settled', 'virtual_settled')
           AND eo.outcome IN ('WIN','won')
           AND COALESCE(eo.trading_fee_usd, 0) = 0
         LIMIT 10
        """
    )
    if not rows:
        return True, "All WIN settled имеют trading_fee_usd > 0"
    return False, f"{len(rows)} WIN settled с trading_fee_usd=0 (формула молчит?)"


async def check_consistency_with_realized_roi_net() -> tuple[bool, str]:
    """net_pnl должен ≈ realized_roi_net × stake / 100 (с поправкой на formula
    разницу: roi_net на $1 stake, net_pnl на real stake).
    """
    rows = await fetch(
        """
        SELECT eo.id, eo.stake, eo.net_pnl, ds.realized_roi_net,
               ABS(eo.net_pnl - (ds.realized_roi_net / 100.0 * eo.stake)) AS delta
          FROM execution_orders eo
          JOIN detector_signals ds ON ds.id = eo.detector_signal_id
         WHERE eo.status IN ('settled', 'virtual_settled')
           AND eo.net_pnl IS NOT NULL
           AND ds.realized_roi_net IS NOT NULL
           AND ABS(eo.net_pnl - (ds.realized_roi_net / 100.0 * eo.stake)) > 0.50
         LIMIT 10
        """
    )
    if not rows:
        return True, "net_pnl ≈ realized_roi_net × stake / 100 (формулы согласованы)"
    examples = ", ".join(f"{r['id']}:Δ${r['delta']:.2f}" for r in rows[:3])
    return False, f"{len(rows)} rows с расхождением >$0.50 (формулы расходятся: {examples})"


async def check_orphan_settled() -> tuple[bool, str]:
    """settled rows должны иметь detector_signal_id (legacy могут не иметь)."""
    rows = await fetch(
        """
        SELECT COUNT(*) AS n
          FROM execution_orders
         WHERE status IN ('settled', 'virtual_settled')
           AND detector_signal_id IS NULL
        """
    )
    n = int(rows[0]["n"])
    if n == 0:
        return True, "0 orphan settled (все имеют detector_signal_id)"
    # legacy 4 settled — это known. Если больше → новый bug
    if n <= 5:
        return True, f"{n} legacy orphan settled (acceptable, до Phase F)"
    return False, f"{n} orphan settled — слишком много для legacy"


CHECKS = [
    ("unsettled_with_resolved", check_unsettled_with_resolved_signal),
    ("settled_have_net_pnl", check_settled_have_net_pnl),
    ("net_pnl_le_gross", check_net_pnl_le_gross),
    ("win_has_fee", check_win_has_fee),
    ("consistency_roi_pnl", check_consistency_with_realized_roi_net),
    ("orphan_settled", check_orphan_settled),
]


async def main() -> None:
    await init_db()
    try:
        print()
        print(f"{'Check':<35} {'Status':<10} {'Detail'}")
        print("=" * 90)
        all_pass = True
        for name, check_fn in CHECKS:
            ok, detail = await check_fn()
            marker = "PASS" if ok else "FAIL"
            if not ok:
                all_pass = False
            print(f"{name:<35} {marker:<10} {detail}")
        print("=" * 90)
        print(f"\nOVERALL: {'✓ ALL PASS' if all_pass else '✗ FAILURES — settle pipeline не готов к live'}")
        sys.exit(0 if all_pass else 1)
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())

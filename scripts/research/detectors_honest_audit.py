#!/usr/bin/env python3
"""Detectors honest USD PnL audit (Phase 1 of audit plan 2026-05).

Считает гипотетический USD PnL для каждого production detector с:
1. Per-event dedup (top-1 per (detector, event_id) by created_at)
2. Stake $25/trade (0.5% от $5K, текущий MAX_STAKE_PCT)
3. После side-aware ROI fix (PR #383): realized_roi_net корректен per-side

Запуск (на VPS):
    cd ~/oracle && /root/oracle/.venv/bin/python scripts/research/detectors_honest_audit.py

Output: products/detectors-audit-2026-05/honest_pnl.md

⚠️ 2026-06-15: добавлен фильтр свежести (AUDIT_WINDOW_DAYS, default 30) — БЕЗ него
аудит тащит killed-детекторы (D5/D7b/D30-33) и мёртвый backfill (D_esports, d45) с
фантомными «PROMOTE» вердиктами. Окно отсекает их по generated_at. Productionized
форвард-монитор — `app/jobs/detectors_honest_audit_report.py` (cron + TG).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from dotenv import load_dotenv

load_dotenv()

import asyncpg  # noqa: E402  (load_dotenv() before imports — intentional, как app/main.py)

# Окно свежести: считать только resolved-сигналы за последние N дней. Отсекает
# killed-детекторы и мёртвый backfill (у них старый generated_at) без хардкод-списка.
AUDIT_WINDOW_DAYS = int(os.getenv("AUDIT_WINDOW_DAYS", "30"))


HONEST_PNL_SQL = """
WITH per_signal AS (
    SELECT
        s.detector,
        s.event_id,
        s.id,
        s.created_at,
        s.realized_roi_net,
        s.current_price,
        s.confidence,
        ROW_NUMBER() OVER (
            PARTITION BY s.detector, s.event_id
            ORDER BY s.created_at
        ) AS event_rank
    FROM detector_signals s
    WHERE s.resolved = true
      AND s.realized_roi_net IS NOT NULL
      AND s.event_id IS NOT NULL
      AND s.generated_at > now() - make_interval(days => $1)
)
SELECT
    detector,
    COUNT(*) AS n_resolved,
    COUNT(*) FILTER (WHERE event_rank = 1) AS n_dedup,
    -- realized_roi_net в %, делим /100 для proportion
    ROUND((AVG(realized_roi_net) FILTER (WHERE event_rank = 1) / 100)::numeric, 4) AS avg_roi_net,
    -- realized_roi_net хранится в PERCENT (-100 = -100%), нормируем /100 → proportion
    ROUND((SUM(realized_roi_net) FILTER (WHERE event_rank = 1) / 100)::numeric * 25, 2) AS hypothetical_pnl_usd,
    MIN(created_at)::date AS first_signal,
    MAX(created_at)::date AS last_signal,
    GREATEST(EXTRACT(EPOCH FROM (MAX(created_at) - MIN(created_at))) / 86400.0, 1)::int AS span_days
FROM per_signal
GROUP BY detector
ORDER BY hypothetical_pnl_usd DESC NULLS LAST;
"""

PER_CATEGORY_SQL = """
WITH per_signal AS (
    SELECT
        s.detector,
        s.event_id,
        s.realized_roi_net,
        COALESCE(m.category, 'unknown') AS category,
        ROW_NUMBER() OVER (
            PARTITION BY s.detector, s.event_id
            ORDER BY s.created_at
        ) AS event_rank
    FROM detector_signals s
    LEFT JOIN markets m ON m.polymarket_id = s.market_id
    WHERE s.resolved = true
      AND s.realized_roi_net IS NOT NULL
      AND s.event_id IS NOT NULL
      AND s.generated_at > now() - make_interval(days => $1)
)
SELECT
    detector, category,
    COUNT(*) FILTER (WHERE event_rank = 1) AS n,
    -- realized_roi_net в percent — для display оставляем как есть, для PnL делим /100
    ROUND(AVG(realized_roi_net) FILTER (WHERE event_rank = 1)::numeric, 2) AS avg_roi_pct,
    ROUND((SUM(realized_roi_net) FILTER (WHERE event_rank = 1) / 100)::numeric * 25, 2) AS pnl_usd
FROM per_signal
WHERE event_rank = 1
GROUP BY detector, category
HAVING COUNT(*) FILTER (WHERE event_rank = 1) >= 30
ORDER BY detector, pnl_usd DESC;
"""


async def run():
    dsn = os.getenv("DATABASE_URL")
    if not dsn:
        print("ERROR: DATABASE_URL not set")
        sys.exit(1)

    conn = await asyncpg.connect(dsn=dsn)

    print("\n" + "=" * 90)
    print(f"DETECTORS HONEST AUDIT — {date.today().isoformat()}")
    print("=" * 90)

    rows = await conn.fetch(HONEST_PNL_SQL, AUDIT_WINDOW_DAYS)
    print(f"\n(окно свежести: {AUDIT_WINDOW_DAYS}д — killed/backfill отсечены)")
    print("\n## Phase 1: Honest USD PnL per detector ($25 stake = 0.5% of $5K)\n")
    print(f"{'detector':32} {'n_total':>8} {'n_dedup':>8} {'avg_roi':>10} {'pnl_usd':>12} "
          f"{'span':>5}d  {'$/day':>8}")
    print("-" * 100)
    for r in rows:
        det = r['detector']
        n_total = r['n_resolved']
        n_dedup = r['n_dedup']
        avg_roi = float(r['avg_roi_net'] or 0)
        pnl_usd = float(r['hypothetical_pnl_usd'] or 0)
        span = r['span_days'] or 1
        per_day = pnl_usd / max(span, 1)
        print(f"{det:32} {n_total:>8,} {n_dedup:>8,} {avg_roi:>+9.4f} "
              f"{pnl_usd:>+11,.2f} {span:>5}d  {per_day:>+7.2f}")

    print("\n## Phase 1b: Per-category breakdown (n>=30 events per category)\n")
    cats = await conn.fetch(PER_CATEGORY_SQL, AUDIT_WINDOW_DAYS)
    cur_det = None
    for r in cats:
        if r['detector'] != cur_det:
            cur_det = r['detector']
            print(f"\n  {cur_det}:")
        marker = '✅' if (r['pnl_usd'] or 0) > 0 else '❌'
        print(f"    {marker} {r['category']:20s} n={r['n']:>4} avg_roi={r['avg_roi_pct']:+6.1f}% "
              f"pnl=${r['pnl_usd']:+8.2f}")

    # Verdict per detector
    print("\n## Phase 2: Decision matrix\n")
    print(f"{'detector':32} {'n_dedup':>8} {'$/day':>9} {'verdict':30}")
    print("-" * 90)
    for r in rows:
        det = r['detector']
        n_dedup = r['n_dedup']
        pnl_usd = float(r['hypothetical_pnl_usd'] or 0)
        span = r['span_days'] or 1
        per_day = pnl_usd / max(span, 1)
        per_day_1k = per_day / 5  # scale к $1K bankroll

        if n_dedup < 100:
            verdict = "⏸️ INSUFFICIENT (n<100)"
        elif per_day_1k < -2.0:
            verdict = f"❌ KILL (${per_day_1k:+.2f}/day @$1K)"
        elif per_day_1k > 5.0:
            verdict = f"✅ PROMOTE candidate (${per_day_1k:+.2f}/day @$1K)"
        elif per_day_1k > -2.0:
            verdict = f"⚠️ MARGINAL (${per_day_1k:+.2f}/day @$1K)"
        else:
            verdict = "❓ CHECK"
        print(f"{det:32} {n_dedup:>8,} {per_day:>+8.2f}  {verdict:30}")

    # Save markdown report
    out_dir = Path("/root/oracle/products/detectors-audit-2026-05")
    out_dir.mkdir(parents=True, exist_ok=True)
    lines = [
        f"# Detectors Honest Audit — {date.today().isoformat()}",
        "",
        "Phase 1 of `docs/detectors_audit_plan_2026-05.md`.",
        "",
        "**Stake assumption:** $25/trade (0.5% of $5K bankroll = текущий MAX_STAKE_PCT_DEFAULT).",
        "**Dedup:** top-1 per (detector, event_id) by created_at — accounting for correlated outcomes.",
        "**Source:** detector_signals where resolved=true and realized_roi_net IS NOT NULL.",
        "",
        "## Per-detector honest PnL",
        "",
        "| detector | n_total | n_dedup | avg_roi_net | hypothetical_pnl ($25 stake) | span_days | $/day |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        avg_roi = float(r['avg_roi_net'] or 0)
        pnl_usd = float(r['hypothetical_pnl_usd'] or 0)
        span = r['span_days'] or 1
        per_day = pnl_usd / max(span, 1)
        lines.append(
            f"| {r['detector']} | {r['n_resolved']:,} | {r['n_dedup']:,} | "
            f"{avg_roi:+.4f} | ${pnl_usd:+,.2f} | {span} | ${per_day:+.2f} |"
        )

    lines.extend([
        "",
        "## Decision matrix",
        "",
        "Decision tree: realistic_daily @ $1K bankroll = (hypothetical_pnl / span / 5).",
        "  - `< -$2/day` → KILL",
        "  - `-$2 to +$5` → MARGINAL",
        "  - `> $5` → PROMOTE candidate",
        "  - `n_dedup < 100` → INSUFFICIENT",
        "",
        "| detector | n_dedup | $/day @ $5K | $/day @ $1K | verdict |",
        "|---|---|---|---|---|",
    ])
    for r in rows:
        det = r['detector']
        n_dedup = r['n_dedup']
        pnl_usd = float(r['hypothetical_pnl_usd'] or 0)
        span = r['span_days'] or 1
        per_day = pnl_usd / max(span, 1)
        per_day_1k = per_day / 5

        if n_dedup < 100:
            verdict = "⏸️ INSUFFICIENT (n<100)"
        elif per_day_1k < -2.0:
            verdict = "❌ KILL"
        elif per_day_1k > 5.0:
            verdict = "✅ PROMOTE candidate"
        elif per_day_1k > -2.0:
            verdict = "⚠️ MARGINAL"
        else:
            verdict = "❓ CHECK"
        lines.append(
            f"| {det} | {n_dedup:,} | ${per_day:+.2f} | ${per_day_1k:+.2f} | {verdict} |"
        )

    out_path = out_dir / "honest_pnl.md"
    out_path.write_text("\n".join(lines))
    print(f"\nReport saved: {out_path}")

    await conn.close()


if __name__ == "__main__":
    asyncio.run(run())

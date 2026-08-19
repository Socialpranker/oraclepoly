#!/usr/bin/env python3
"""D12 WeekendLiquidityGap — Stage 2 backtest via oracle_snapshots.

Гипотеза: в субботу/воскресенье Polymarket объёмы падают ~50%, retail-impulse
YES-buyers усиливают longshot bias. NO-side в extreme zones (0.85-0.95) в
weekend должна давать большую ROI чем в те же часы weekday.

Adversarial check: weekday снимки в тех же price_zones и time-of-day НЕ должны
давать сопоставимый edge. Если weekday edge ≥ weekend_edge × 0.75 → это не
weekend effect, это generic longshot NO-bias (territory D5).

Stage 2 thresholds (docs/detector_lifecycle.md):
  1. ROI > 30% after fee+slippage per target bucket
  2. Deflated Sharpe > 1.0 (halved nominal SR shortcut)
  3. n >= 100 per target bucket (D12 threshold ниже чем D7 — only 2/7 days)
  4. Adversarial: weekday edge < 75% of weekend edge in same price zone
  5. Post-V2 split не разваливает edge (V2 cutover = 2026-04-28)

Запуск на VPS:
    cd ~/oracle && python3 scripts/research/d12_backtest.py

Данные: oracle_snapshots (прод PostgreSQL через asyncpg).
DATABASE_URL берётся из .env в текущей директории.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import defaultdict
from datetime import date
from pathlib import Path

import asyncpg

# ── Константы D12 ─────────────────────────────────────────────────────────────

VOLUME_FLOOR_USD = 5_000

# Weekend = ISO DOW 6 (Sat) or 7 (Sun)
WEEKEND_DOWS = (6, 7)

# Target zones
NO_ZONE_LOW   = 0.85
NO_ZONE_HIGH  = 0.95
YES_ZONE_LOW  = 0.05
YES_ZONE_HIGH = 0.15

# Categories without Sports (Sports has active weekend schedule — different mechanism)
TARGET_CATEGORIES = ("Politics", "Tech & AI", "Crypto", "World & Economy", "Culture")

V2_CUTOVER = "2026-04-28"

# Fee model: same as D7 (extreme zones pay more — thin books)
FEES = {
    "0.05-0.10": 0.07,
    "0.10-0.15": 0.05,
    "0.85-0.90": 0.05,
    "0.90-0.95": 0.07,
}

ROI_THRESHOLD    = 0.30
DSR_THRESHOLD    = 1.0
N_THRESHOLD      = 100  # lower than D7 — weekends are 2/7 days
ADVERSARIAL_MAX  = 0.75  # weekday ROI must be < 75% of weekend ROI

# ── DB ────────────────────────────────────────────────────────────────────────

def load_db_url() -> str:
    env_file = Path(__file__).parent.parent.parent / ".env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            if line.startswith("DATABASE_URL="):
                return line.split("=", 1)[1].strip()
    url = os.environ.get("DATABASE_URL")
    if not url:
        raise RuntimeError("DATABASE_URL not found in .env or environment")
    return url


# ── SQL ───────────────────────────────────────────────────────────────────────

# Primary: weekend vs weekday × price_zone × category × is_post_v2
PRIMARY_SQL = """
WITH signals AS (
    SELECT
        market_id,
        market_price                                         AS price,
        volume_24h,
        category,
        outcome,
        created_at,
        (end_date::date >= $1::date)                        AS is_post_v2,
        EXTRACT(ISODOW FROM created_at)::int                AS dow,
        EXTRACT(ISODOW FROM created_at) IN (6, 7)           AS is_weekend,
        CASE
            WHEN market_price BETWEEN $2 AND 0.10   THEN '0.05-0.10'
            WHEN market_price BETWEEN 0.10 AND $3   THEN '0.10-0.15'
            WHEN market_price BETWEEN $4 AND 0.90   THEN '0.85-0.90'
            WHEN market_price BETWEEN 0.90 AND $5   THEN '0.90-0.95'
            ELSE NULL
        END AS price_bucket,
        CASE
            WHEN market_price BETWEEN $4 AND $5  THEN 'NO'
            WHEN market_price BETWEEN $2 AND $3  THEN 'YES'
            ELSE NULL
        END AS trade_side,
        CASE
            WHEN market_price BETWEEN $4 AND $5  THEN (outcome = 'no')::int
            WHEN market_price BETWEEN $2 AND $3  THEN (outcome = 'yes')::int
            ELSE NULL
        END AS win,
        CASE
            WHEN market_price BETWEEN $4 AND $5
                THEN CASE WHEN outcome = 'no'  THEN (1.0 / NULLIF(1.0 - market_price, 0)) - 1 ELSE -1.0 END
            WHEN market_price BETWEEN $2 AND $3
                THEN CASE WHEN outcome = 'yes' THEN (1.0 / NULLIF(market_price, 0)) - 1 ELSE -1.0 END
            ELSE NULL
        END AS raw_roi
    FROM oracle_snapshots
    WHERE outcome IN ('yes', 'no')
      AND end_date IS NOT NULL
      AND volume_24h >= $6
      AND category = ANY($7::text[])
      AND (
          (market_price BETWEEN $4 AND $5)
       OR (market_price BETWEEN $2 AND $3)
      )
)
SELECT
    is_weekend,
    price_bucket,
    trade_side,
    is_post_v2,
    COUNT(*)::int                           AS n,
    COUNT(DISTINCT market_id)::int          AS markets,
    ROUND(AVG(win)::numeric, 4)             AS win_rate,
    ROUND(AVG(raw_roi)::numeric, 4)         AS avg_raw_roi,
    ROUND(STDDEV(raw_roi)::numeric, 4)      AS std_raw_roi
FROM signals
WHERE price_bucket IS NOT NULL AND win IS NOT NULL
GROUP BY 1, 2, 3, 4
HAVING COUNT(*) >= 10
ORDER BY trade_side DESC, price_bucket, is_weekend DESC, is_post_v2
"""

# Category breakdown (weekends only, primary target)
CATEGORY_SQL = """
WITH signals AS (
    SELECT
        category,
        market_price AS price,
        outcome,
        CASE
            WHEN market_price BETWEEN $3 AND $4  THEN 'NO'
            WHEN market_price BETWEEN $1 AND $2  THEN 'YES'
            ELSE NULL
        END AS trade_side,
        CASE
            WHEN market_price BETWEEN $3 AND $4
                THEN CASE WHEN outcome = 'no'  THEN (1.0 / NULLIF(1.0 - market_price, 0)) - 1 ELSE -1.0 END
            WHEN market_price BETWEEN $1 AND $2
                THEN CASE WHEN outcome = 'yes' THEN (1.0 / NULLIF(market_price, 0)) - 1 ELSE -1.0 END
            ELSE NULL
        END AS raw_roi,
        CASE
            WHEN market_price BETWEEN $3 AND $4 THEN (outcome = 'no')::int
            WHEN market_price BETWEEN $1 AND $2 THEN (outcome = 'yes')::int
            ELSE NULL
        END AS win
    FROM oracle_snapshots
    WHERE outcome IN ('yes', 'no')
      AND end_date IS NOT NULL
      AND volume_24h >= $5
      AND category = ANY($6::text[])
      AND EXTRACT(ISODOW FROM created_at) IN (6, 7)
      AND (
          (market_price BETWEEN $3 AND $4)
       OR (market_price BETWEEN $1 AND $2)
      )
)
SELECT
    category,
    trade_side,
    COUNT(*)::int                         AS n,
    ROUND(AVG(win)::numeric, 4)           AS win_rate,
    ROUND(AVG(raw_roi)::numeric, 4)       AS avg_raw_roi
FROM signals
WHERE trade_side IS NOT NULL AND win IS NOT NULL
GROUP BY 1, 2
HAVING COUNT(*) >= 10
ORDER BY trade_side DESC, avg_raw_roi DESC
"""

# Per-hour-of-weekend breakdown to see if specific hours drive the effect
HOUR_SQL = """
WITH signals AS (
    SELECT
        EXTRACT(HOUR FROM created_at)::int  AS hour_utc,
        market_price AS price,
        outcome,
        CASE
            WHEN market_price BETWEEN $3 AND $4  THEN 'NO'
            WHEN market_price BETWEEN $1 AND $2  THEN 'YES'
            ELSE NULL
        END AS trade_side,
        CASE
            WHEN market_price BETWEEN $3 AND $4
                THEN CASE WHEN outcome = 'no'  THEN (1.0 / NULLIF(1.0 - market_price, 0)) - 1 ELSE -1.0 END
            WHEN market_price BETWEEN $1 AND $2
                THEN CASE WHEN outcome = 'yes' THEN (1.0 / NULLIF(market_price, 0)) - 1 ELSE -1.0 END
            ELSE NULL
        END AS raw_roi,
        CASE
            WHEN market_price BETWEEN $3 AND $4 THEN (outcome = 'no')::int
            WHEN market_price BETWEEN $1 AND $2 THEN (outcome = 'yes')::int
            ELSE NULL
        END AS win
    FROM oracle_snapshots
    WHERE outcome IN ('yes', 'no')
      AND end_date IS NOT NULL
      AND volume_24h >= $5
      AND category = ANY($6::text[])
      AND EXTRACT(ISODOW FROM created_at) IN (6, 7)
      AND (
          (market_price BETWEEN $3 AND $4)
       OR (market_price BETWEEN $1 AND $2)
      )
)
SELECT
    hour_utc,
    trade_side,
    COUNT(*)::int                         AS n,
    ROUND(AVG(win)::numeric, 4)           AS win_rate,
    ROUND(AVG(raw_roi)::numeric, 4)       AS avg_raw_roi
FROM signals
WHERE trade_side IS NOT NULL AND win IS NOT NULL
GROUP BY 1, 2
HAVING COUNT(*) >= 5
ORDER BY trade_side DESC, hour_utc
"""

TARGET_CATS_ARRAY = list(TARGET_CATEGORIES)

PARAMS_PRIMARY = (
    date.fromisoformat(V2_CUTOVER),  # $1
    YES_ZONE_LOW,                     # $2
    YES_ZONE_HIGH,                    # $3
    NO_ZONE_LOW,                      # $4
    NO_ZONE_HIGH,                     # $5
    VOLUME_FLOOR_USD,                 # $6
    TARGET_CATS_ARRAY,                # $7
)

PARAMS_CATEGORY = (
    YES_ZONE_LOW,       # $1
    YES_ZONE_HIGH,      # $2
    NO_ZONE_LOW,        # $3
    NO_ZONE_HIGH,       # $4
    VOLUME_FLOOR_USD,   # $5
    TARGET_CATS_ARRAY,  # $6
)

# HOUR_SQL same params as CATEGORY_SQL
PARAMS_HOUR = PARAMS_CATEGORY


# ── DSR helper ────────────────────────────────────────────────────────────────

def halved_sr(mean_roi: float, std_roi: float, n: int) -> float:
    if std_roi == 0 or n < 2:
        return 0.0
    nominal = (mean_roi / std_roi) * (max(n, 50) ** 0.5)
    return nominal / 2.0


def vi(passed: bool) -> str:
    return "✅" if passed else "❌"


# ── Report renderer ───────────────────────────────────────────────────────────

def render_report(primary_rows, category_rows, hour_rows, run_date, elapsed):
    lines = []
    A = lines.append

    A("# D12 WeekendLiquidityGap — Stage 2 Backtest")
    A("")
    A(f"**Дата:** {run_date}  ")
    A(f"**Источник:** `oracle_snapshots` (прод PostgreSQL via asyncpg)  ")
    A(f"**Volume floor:** ${VOLUME_FLOOR_USD:,}  ")
    A(f"**Категории:** {', '.join(TARGET_CATEGORIES)}  ")
    A(f"**Price zones:** NO [{NO_ZONE_LOW}–{NO_ZONE_HIGH}] | YES [{YES_ZONE_LOW}–{YES_ZONE_HIGH}]  ")
    A(f"**V2 cutover:** {V2_CUTOVER}  ")
    A("")
    A("---")
    A("")

    # --- 1. Per-zone aggregate: weekend vs weekday × price_zone × side ---
    A("## 1. Weekend vs Weekday — основная таблица")
    A("")
    A("| period | price_bucket | side | is_post_v2 | n | markets | WR | raw_ROI | fee | net_ROI | DSR | ROI✅ | DSR✅ | N✅ |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")

    # Store aggregated stats for adversarial check
    zone_stats: dict = {}  # key=(is_weekend, price_bucket, side, is_post_v2)

    for r in primary_rows:
        is_wknd = bool(r["is_weekend"])
        pb = r["price_bucket"]
        side = r["trade_side"]
        post_v2 = bool(r["is_post_v2"])
        n = int(r["n"])
        wr = float(r["win_rate"] or 0)
        raw = float(r["avg_raw_roi"] or 0)
        std = float(r["std_raw_roi"] or 0)

        fee = FEES.get(pb, 0.05)
        net = raw - fee
        dsr = halved_sr(net, std, n)

        zone_stats[(is_wknd, pb, side, post_v2)] = {
            "n": n, "raw": raw, "net": net, "dsr": dsr, "wr": wr
        }

        period_label = "**weekend**" if is_wknd else "weekday"
        v2_label = "post-V2" if post_v2 else "pre-V2"

        A(f"| {period_label} | {pb} | {side} | {v2_label} | {n} | {r['markets']} | "
          f"{wr:.1%} | {raw:+.1%} | {fee:.0%} | {net:+.1%} | {dsr:.2f} | "
          f"{vi(net > ROI_THRESHOLD)} | {vi(dsr > DSR_THRESHOLD)} | {vi(n >= N_THRESHOLD)} |")

    A("")

    # --- 2. Adversarial check ---
    A("## 2. Adversarial check — Weekend edge vs Weekday edge")
    A("")
    A("D12 требует: weekday ROI в тех же zones < 75% weekend ROI.  ")
    A("Если weekday edge сопоставим → это не weekend effect, это generic longshot bias (D5).")
    A("")
    A("| price_bucket | side | is_post_v2 | weekend_net_ROI | weekday_net_ROI | ratio | PASS? |")
    A("|---|---|---|---|---|---|---|")

    adversarial_pass_count = 0
    adversarial_total = 0

    price_buckets = sorted({pb for (_, pb, _, _) in zone_stats})
    sides = ["NO", "YES"]
    for pb in price_buckets:
        for side in sides:
            for post_v2 in [False, True]:
                wknd = zone_stats.get((True, pb, side, post_v2))
                wkday = zone_stats.get((False, pb, side, post_v2))
                if wknd is None or wkday is None:
                    continue

                adversarial_total += 1
                v2_label = "post-V2" if post_v2 else "pre-V2"
                wknd_roi = wknd["net"]
                wkday_roi = wkday["net"]

                if wknd_roi <= 0:
                    ratio_str = "N/A (weekend negative)"
                    passed = False
                elif wkday_roi <= 0:
                    ratio = 0.0
                    ratio_str = f"{ratio:.0%} (weekday negative)"
                    passed = True
                    adversarial_pass_count += 1
                else:
                    ratio = wkday_roi / wknd_roi
                    ratio_str = f"{ratio:.0%}"
                    passed = ratio < ADVERSARIAL_MAX
                    if passed:
                        adversarial_pass_count += 1

                A(f"| {pb} | {side} | {v2_label} | {wknd_roi:+.1%} | {wkday_roi:+.1%} | "
                  f"{ratio_str} | {vi(passed)} |")

    A("")
    adversarial_result = adversarial_pass_count >= (adversarial_total * 0.6) if adversarial_total > 0 else False
    A(f"**Adversarial check:** {adversarial_pass_count}/{adversarial_total} пар прошли (порог ≥ 60%)  ")
    A(f"**Итог:** {vi(adversarial_result)} {'PASS' if adversarial_result else 'FAIL'}")
    A("")

    # --- 3. Category breakdown (weekend only) ---
    A("## 3. Per-category (weekend only)")
    A("")
    A("| category | side | n | WR | raw_ROI | net_ROI |")
    A("|---|---|---|---|---|---|")
    for r in category_rows:
        cat = r["category"]
        side = r["trade_side"]
        n = int(r["n"])
        wr = float(r["win_rate"] or 0)
        raw = float(r["avg_raw_roi"] or 0)
        # approximate fee by side (no_zone default 5%)
        fee = 0.06
        net = raw - fee
        A(f"| {cat} | {side} | {n} | {wr:.1%} | {raw:+.1%} | {net:+.1%} |")
    A("")

    # --- 4. Hour-of-weekend breakdown ---
    A("## 4. Hour-of-weekend breakdown (UTC)")
    A("")
    A("| hour_utc | side | n | WR | raw_ROI |")
    A("|---|---|---|---|---|")
    for r in hour_rows:
        A(f"| {int(r['hour_utc']):02d}:00 | {r['trade_side']} | {int(r['n'])} | "
          f"{float(r['win_rate'] or 0):.1%} | {float(r['avg_raw_roi'] or 0):+.1%} |")
    A("")

    # --- 5. Final verdict ---
    A("## 5. Final Verdict")
    A("")

    # Collect all weekend target buckets for verdict
    weekend_results = {k: v for k, v in zone_stats.items() if k[0]}  # is_weekend=True
    # Focus on pre-V2 (more data) and NO-side (primary hypothesis)
    primary_buckets = [(k, v) for k, v in weekend_results.items()
                       if k[2] == "NO" and not k[3]]  # side=NO, pre-V2

    if not primary_buckets:
        A("⚠️ Нет данных для NO-side weekend pre-V2 buckets.")
        A("")
        A("**Verdict: ❌ FAIL — недостаточно данных**")
    else:
        # Check Stage 2 thresholds across primary buckets
        roi_passes = sum(1 for _, v in primary_buckets if v["net"] > ROI_THRESHOLD)
        dsr_passes = sum(1 for _, v in primary_buckets if v["dsr"] > DSR_THRESHOLD)
        n_passes = sum(1 for _, v in primary_buckets if v["n"] >= N_THRESHOLD)
        total_b = len(primary_buckets)

        A("### Stage 2 threshold summary (NO-side, pre-V2, weekend)")
        A("")
        A(f"| Threshold | Result | Detail |")
        A(f"|---|---|---|")
        A(f"| ROI > {ROI_THRESHOLD:.0%} | {vi(roi_passes > 0)} | {roi_passes}/{total_b} price buckets pass |")
        A(f"| DSR > {DSR_THRESHOLD:.1f} | {vi(dsr_passes > 0)} | {dsr_passes}/{total_b} price buckets pass |")
        A(f"| n >= {N_THRESHOLD} | {vi(n_passes > 0)} | {n_passes}/{total_b} price buckets pass |")
        A(f"| Adversarial mirror | {vi(adversarial_result)} | weekday edge < 75% weekend edge |")

        # Post-V2 check
        post_v2_buckets = [(k, v) for k, v in weekend_results.items()
                           if k[2] == "NO" and k[3]]
        if post_v2_buckets:
            post_v2_roi_pass = sum(1 for _, v in post_v2_buckets if v["net"] > 0)
            A(f"| Post-V2 split | {vi(post_v2_roi_pass > 0)} | {post_v2_roi_pass}/{len(post_v2_buckets)} post-V2 buckets positive |")
        else:
            A(f"| Post-V2 split | ⚠️ | n_post_v2 < 10 — INCONCLUSIVE (retest 2026-06-02) |")

        A("")

        all_pass = roi_passes > 0 and dsr_passes > 0 and n_passes > 0 and adversarial_result
        if all_pass:
            A("**Verdict: ✅ PASS — переход в Stage 3 (economics analysis)**")
        elif roi_passes == 0 and dsr_passes == 0:
            A("**Verdict: ❌ FAIL — нет edge ни в одном NO-side weekend bucket**")
        else:
            A("**Verdict: ⚠️ CONDITIONAL — часть threshold прошла, нужен refinement**")

        A("")
        A("### Лучшие buckets (NO-side weekend)")
        A("")
        A("| price_bucket | is_post_v2 | n | WR | net_ROI | DSR |")
        A("|---|---|---|---|---|---|")
        for (_, pb, side, post_v2), v in sorted(primary_buckets, key=lambda x: -x[1]["net"]):
            v2_label = "post-V2" if post_v2 else "pre-V2"
            A(f"| {pb} | {v2_label} | {v['n']} | {v['wr']:.1%} | {v['net']:+.1%} | {v['dsr']:.2f} |")

    A("")
    A("---")
    A(f"*Backtest completed in {elapsed:.1f}s*")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

async def main():
    t0 = time.time()
    db_url = load_db_url()
    run_date = date.today().isoformat()

    print("Connecting to database...", flush=True)
    conn = await asyncpg.connect(db_url)

    try:
        print("Running PRIMARY query (weekend vs weekday by zone)...", flush=True)
        primary_rows = await conn.fetch(PRIMARY_SQL, *PARAMS_PRIMARY)
        print(f"  → {len(primary_rows)} rows", flush=True)

        print("Running CATEGORY query (weekend only)...", flush=True)
        category_rows = await conn.fetch(CATEGORY_SQL, *PARAMS_CATEGORY)
        print(f"  → {len(category_rows)} rows", flush=True)

        print("Running HOUR query (weekend only)...", flush=True)
        hour_rows = await conn.fetch(HOUR_SQL, *PARAMS_HOUR)
        print(f"  → {len(hour_rows)} rows", flush=True)

    finally:
        await conn.close()

    elapsed = time.time() - t0
    report = render_report(primary_rows, category_rows, hour_rows, run_date, elapsed)

    # Save output
    out_dir = Path(__file__).parent.parent.parent / "products" / "d12-weekend-liquidity-gap"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "backtest.md"
    out_path.write_text(report)
    print(f"\nReport saved: {out_path}", flush=True)

    # Also print to stdout
    print("\n" + "=" * 80)
    print(report)

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

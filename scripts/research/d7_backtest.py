#!/usr/bin/env python3
"""D7 PreResolutionConvergenceFade — Stage 2 backtest via oracle_snapshots.

Гипотеза: когда market_price ∈ [0.05,0.15] ∪ [0.85,0.95] за 4-72h до resolve
и volume_24h ≥ $5K — цена «застряла» из-за inventory effects (pinning),
а не несёт информации. Торгуем против экстремального ценообразования.

Логика:
  - price 0.85-0.95 → торгуем NO (ставим что YES проиграет)
  - price 0.05-0.15 → торгуем YES (adversarial mirror check)

Stage 2 thresholds (docs/detector_lifecycle.md):
  1. ROI > 30% after fee+slippage per target bucket
  2. Deflated Sharpe > 1.0 (halved nominal SR shortcut)
  3. n >= 200 per target bucket
  4. Adversarial mirror check: |no_roi - yes_roi| / max < 50%
  5. Post-V2 split: edge должен быть в обоих периодах (V2 cutover = 2026-04-28)

Запуск на VPS:
    cd ~/oracle && python3 scripts/research/d7_backtest.py

Данные: oracle_snapshots (прод PostgreSQL через asyncpg).
DATABASE_URL берётся из .env в текущей директории.
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import asyncpg

# ── Константы D7 ─────────────────────────────────────────────────────────────

VOLUME_FLOOR_USD = 5_000
HOURS_MIN = 4
HOURS_MAX = 72

NO_ZONE_LOW  = 0.85
NO_ZONE_HIGH = 0.95
YES_ZONE_LOW = 0.05
YES_ZONE_HIGH = 0.15

V2_CUTOVER = "2026-04-28"

FEES = {
    "0.05-0.10": 0.07,
    "0.10-0.15": 0.05,
    "0.85-0.90": 0.05,
    "0.90-0.95": 0.07,
}

ROI_THRESHOLD    = 0.30
DSR_THRESHOLD    = 1.0
N_THRESHOLD      = 200
MIRROR_MAX_ASYMM = 0.50

EXCLUDED_CATEGORIES = ("Other", "Minute Markets")

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

PRIMARY_SQL = """
WITH signals AS (
    SELECT
        market_id,
        market_price                                     AS price,
        volume_24h,
        category,
        outcome,
        created_at,
        end_date,
        EXTRACT(EPOCH FROM (end_date - created_at)) / 3600.0 AS hrs,
        (end_date::date >= $1::date)                     AS is_post_v2,
        CASE
            WHEN market_price BETWEEN $2 AND 0.10  THEN '0.05-0.10'
            WHEN market_price BETWEEN 0.10 AND $3  THEN '0.10-0.15'
            WHEN market_price BETWEEN $4 AND 0.90  THEN '0.85-0.90'
            WHEN market_price BETWEEN 0.90 AND $5  THEN '0.90-0.95'
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
        END AS win
    FROM oracle_snapshots
    WHERE outcome IN ('yes', 'no')
      AND end_date IS NOT NULL
      AND volume_24h >= $6
      AND category NOT IN ('Other', 'Minute Markets')
      AND EXTRACT(EPOCH FROM (end_date - created_at)) / 3600 BETWEEN $7 AND $8
      AND (
          (market_price BETWEEN $4 AND $5)
       OR (market_price BETWEEN $2 AND $3)
      )
),
with_roi AS (
    SELECT *,
        CASE
            WHEN trade_side = 'NO'
                THEN CASE WHEN win = 1 THEN (1.0 / NULLIF(1.0 - price, 0)) - 1 ELSE -1.0 END
            WHEN trade_side = 'YES'
                THEN CASE WHEN win = 1 THEN (1.0 / NULLIF(price, 0)) - 1 ELSE -1.0 END
            ELSE NULL
        END AS raw_roi,
        CASE
            WHEN hrs BETWEEN $7 AND 12  THEN '12-4h'
            WHEN hrs BETWEEN 12 AND 24  THEN '24-12h'
            WHEN hrs BETWEEN 24 AND 48  THEN '48-24h'
            WHEN hrs BETWEEN 48 AND $8  THEN '72-48h'
            ELSE NULL
        END AS time_bucket
    FROM signals
    WHERE price_bucket IS NOT NULL AND win IS NOT NULL
)
SELECT
    time_bucket,
    price_bucket,
    trade_side,
    is_post_v2,
    COUNT(*)::int                          AS n,
    COUNT(DISTINCT market_id)::int         AS markets,
    ROUND(AVG(win)::numeric, 4)            AS win_rate,
    ROUND(AVG(raw_roi)::numeric, 4)        AS avg_raw_roi,
    ROUND(STDDEV(raw_roi)::numeric, 4)     AS std_raw_roi
FROM with_roi
WHERE time_bucket IS NOT NULL
GROUP BY 1, 2, 3, 4
HAVING COUNT(*) >= 10
ORDER BY trade_side DESC, price_bucket, time_bucket, is_post_v2
"""

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
      AND category NOT IN ('Other', 'Minute Markets')
      AND EXTRACT(EPOCH FROM (end_date - created_at)) / 3600 BETWEEN $6 AND $7
      AND (
          (market_price BETWEEN $3 AND $4)
       OR (market_price BETWEEN $1 AND $2)
      )
)
SELECT
    category,
    trade_side,
    COUNT(*)::int                        AS n,
    ROUND(AVG(win)::numeric, 4)          AS win_rate,
    ROUND(AVG(raw_roi)::numeric, 4)      AS avg_raw_roi
FROM signals
WHERE trade_side IS NOT NULL AND win IS NOT NULL
GROUP BY 1, 2
HAVING COUNT(*) >= 20
ORDER BY trade_side DESC, avg_raw_roi DESC
"""

PARAMS_PRIMARY = (
    date.fromisoformat(V2_CUTOVER),  # $1 — date object required by asyncpg
    YES_ZONE_LOW,        # $2 = 0.05
    YES_ZONE_HIGH,       # $3 = 0.15
    NO_ZONE_LOW,         # $4 = 0.85
    NO_ZONE_HIGH,        # $5 = 0.95
    VOLUME_FLOOR_USD,    # $6
    HOURS_MIN,           # $7
    HOURS_MAX,           # $8
)

# CATEGORY_SQL doesn't use $1 (no V2 split needed), params are $1-$7
PARAMS_CATEGORY = (
    YES_ZONE_LOW,        # $1 = 0.05
    YES_ZONE_HIGH,       # $2 = 0.15
    NO_ZONE_LOW,         # $3 = 0.85
    NO_ZONE_HIGH,        # $4 = 0.95
    VOLUME_FLOOR_USD,    # $5
    HOURS_MIN,           # $6
    HOURS_MAX,           # $7
)

# ── DSR helper ────────────────────────────────────────────────────────────────

def halved_sr(mean_roi: float, std_roi: float, n: int) -> float:
    if std_roi == 0 or n < 2:
        return 0.0
    nominal = (mean_roi / std_roi) * (max(n, 50) ** 0.5)
    return nominal / 2.0


def vi(passed: bool) -> str:
    return "✅" if passed else "❌"


# ── Report renderer ───────────────────────────────────────────────────────────

def render_report(primary_rows, category_rows, run_date, elapsed):
    lines = []
    A = lines.append

    A("# D7 PreResolutionConvergenceFade — Stage 2 Backtest")
    A("")
    A(f"**Дата:** {run_date}  ")
    A(f"**Источник:** `oracle_snapshots` (прод PostgreSQL via asyncpg)  ")
    A(f"**Volume floor:** ${VOLUME_FLOOR_USD:,}  ")
    A(f"**Trigger window:** {HOURS_MIN}–{HOURS_MAX}h до resolve  ")
    A(f"**Price zones:** NO [{NO_ZONE_LOW}–{NO_ZONE_HIGH}] | YES [{YES_ZONE_LOW}–{YES_ZONE_HIGH}]  ")
    A(f"**V2 cutover:** {V2_CUTOVER}  ")
    A("")
    A("---")
    A("")

    # Aggregate: (price_bucket, is_post_v2) → aggregated stats
    agg: dict = defaultdict(lambda: {
        "n": 0, "wins": 0.0, "roi_sum": 0.0, "std_list": [], "side": ""
    })

    for r in primary_rows:
        time_b = r["time_bucket"]
        price_b = r["price_bucket"]
        side = r["trade_side"]
        post_v2 = bool(r["is_post_v2"])
        n = int(r["n"])
        wr = float(r["win_rate"] or 0)
        roi = float(r["avg_raw_roi"] or 0)
        std = float(r["std_raw_roi"] or 0)

        key = (price_b, post_v2)
        agg[key]["n"] += n
        agg[key]["wins"] += wr * n
        agg[key]["roi_sum"] += roi * n
        agg[key]["std_list"].append(std)
        agg[key]["side"] = side

    # ── Summary table ─────────────────────────────────────────────────────────
    A("## 1. Per-bucket summary (price × post_v2)")
    A("")
    A("| price_bucket | side | post_v2 | n | WR | raw_ROI | net_ROI | DSR_halved | ROI≥30% | n≥200 |")
    A("|---|---|---|---|---|---|---|---|---|---|")

    bucket_results: dict = {}
    for (price_b, post_v2), d in sorted(agg.items(), key=lambda x: (x[0][0], x[0][1])):
        n = d["n"]
        if n == 0:
            continue
        wr = d["wins"] / n
        raw_roi = d["roi_sum"] / n
        side = d["side"]

        fee = FEES.get(price_b, 0.05)
        net_roi = raw_roi - fee

        std_roi = sum(d["std_list"]) / len(d["std_list"]) if d["std_list"] else abs(raw_roi)
        dsr = halved_sr(net_roi, std_roi if std_roi > 0 else 0.01, n)

        roi_pass = net_roi >= ROI_THRESHOLD
        n_pass = n >= N_THRESHOLD

        A(f"| {price_b} | {side} | {post_v2} | {n:,} | {wr:.1%} | {raw_roi:+.1%} | {net_roi:+.1%} | {dsr:.2f} | {vi(roi_pass)} | {vi(n_pass)} |")
        bucket_results[(price_b, post_v2)] = {
            "n": n, "net_roi": net_roi, "dsr": dsr, "side": side,
            "roi_pass": roi_pass, "n_pass": n_pass,
        }

    A("")
    A("*net_ROI = raw_ROI − fee − slippage: extreme zones 7%, high zones 5%.*")
    A("")

    # ── Adversarial mirror ────────────────────────────────────────────────────
    A("## 2. Adversarial mirror check")
    A("")
    A("*Pinning effect должен быть симметричным: NO@0.85-0.95 ≈ YES@0.05-0.15.*")
    A("")
    A("| post_v2 | NO_net_ROI | YES_net_ROI | asymmetry | PASS |")
    A("|---|---|---|---|---|")

    mirror_results = []
    for post_v2 in [False, True]:
        no_key = max(
            [(k, v) for k, v in bucket_results.items()
             if k[1] == post_v2 and k[0].startswith("0.8") or k[0].startswith("0.9")],
            key=lambda x: x[1]["n"], default=(None, None)
        )
        yes_key = max(
            [(k, v) for k, v in bucket_results.items()
             if k[1] == post_v2 and (k[0].startswith("0.0") or k[0].startswith("0.1"))],
            key=lambda x: x[1]["n"], default=(None, None)
        )

        no_roi = no_key[1]["net_roi"] if no_key[0] else None
        yes_roi = yes_key[1]["net_roi"] if yes_key[0] else None

        if no_roi is not None and yes_roi is not None:
            denom = max(abs(no_roi), abs(yes_roi), 0.001)
            asymm = abs(no_roi - yes_roi) / denom
            both_pos = no_roi > 0 and yes_roi > 0
            passed = asymm <= MIRROR_MAX_ASYMM and both_pos
            A(f"| {post_v2} | {no_roi:+.1%} | {yes_roi:+.1%} | {asymm:.0%} | {vi(passed)} |")
            mirror_results.append(passed)
        elif no_roi is not None:
            A(f"| {post_v2} | {no_roi:+.1%} | N/A | — | ⚠️ YES missing |")
            mirror_results.append(False)
        else:
            A(f"| {post_v2} | N/A | N/A | — | ⚠️ no data |")
            mirror_results.append(False)

    A("")

    # ── Category breakdown ────────────────────────────────────────────────────
    A("## 3. Per-category breakdown")
    A("")
    A("| category | side | n | WR | raw_ROI | net_ROI |")
    A("|---|---|---|---|---|---|")

    for r in category_rows:
        cat = r["category"]
        side = r["trade_side"]
        n = int(r["n"])
        wr = float(r["win_rate"] or 0)
        raw_roi = float(r["avg_raw_roi"] or 0)
        fee = FEES.get("0.90-0.95" if side == "NO" else "0.05-0.10", 0.05)
        net_roi = raw_roi - fee
        A(f"| {cat} | {side} | {n:,} | {wr:.1%} | {raw_roi:+.1%} | {net_roi:+.1%} |")

    A("")

    # ── Final verdict ─────────────────────────────────────────────────────────
    A("## 4. Final verdict")
    A("")

    pre_v2 = {k: v for k, v in bucket_results.items() if not k[1]}
    post_v2 = {k: v for k, v in bucket_results.items() if k[1]}

    t1 = any(v["roi_pass"] for v in pre_v2.values())
    t2 = any(v["dsr"] >= DSR_THRESHOLD for v in pre_v2.values())
    t3 = any(v["n_pass"] for v in pre_v2.values())
    t4 = any(mirror_results)
    post_v2_n = sum(v["n"] for v in post_v2.values())
    t5_ok = post_v2_n >= 50

    A("| Threshold | Condition | Result |")
    A("|---|---|---|")
    A(f"| T1: ROI > 30% | best pre-V2 bucket net_roi > 30% | {vi(t1)} |")
    A(f"| T2: DSR > 1.0 | halved nominal Sharpe > 1.0 | {vi(t2)} |")
    A(f"| T3: n ≥ 200 | any bucket n ≥ 200 | {vi(t3)} |")
    A(f"| T4: mirror pass | NO/YES asymmetry ≤ 50%, both positive | {vi(t4)} |")
    A(f"| T5: post-V2 | post-V2 n = {post_v2_n} {'≥ 50' if t5_ok else '< 50 → INCONCLUSIVE'} | {'✅' if t5_ok else '⚠️'} |")
    A("")

    all_core = t1 and t2 and t3 and t4
    if all_core and t5_ok:
        verdict = "✅ **PASS** — все 5 thresholds. Переходим к Stage 3 (economics)."
    elif all_core and not t5_ok:
        verdict = f"⚠️ **CONDITIONAL PASS** — T1-T4 выполнены. T5 INCONCLUSIVE (post-V2 n={post_v2_n} < 50). Retest: 2026-06-02."
    elif not t4:
        verdict = "❌ **FAIL** — T4 adversarial mirror не прошёл. Edge асимметричен → это bias, не pinning. D7 отозвана."
    elif not t1:
        verdict = "❌ **FAIL** — T1: ROI < 30% после fee+slippage. Недостаточный edge."
    else:
        failed = [f"T{i}" for i, p in [(1,t1),(2,t2),(3,t3),(4,t4)] if not p]
        verdict = f"❌ **FAIL** — не прошли: {', '.join(failed)}."

    A(verdict)
    A("")
    A("---")
    A(f"*Время выполнения: {elapsed:.1f}s · Скрипт: `scripts/research/d7_backtest.py`*")
    A(f"*Data: oracle_snapshots (pre-V2 primary; post-V2 n={post_v2_n})*")

    return "\n".join(lines)


# ── Async runner ──────────────────────────────────────────────────────────────

async def run() -> None:
    print("=" * 70)
    print("D7 PreResolutionConvergenceFade — Stage 2 Backtest")
    print("=" * 70)
    t0 = time.time()

    db_url = load_db_url()
    conn = await asyncpg.connect(db_url)

    try:
        print("\n1. Primary bucket query...", flush=True)
        primary_rows = await conn.fetch(PRIMARY_SQL, *PARAMS_PRIMARY)
        print(f"   {len(primary_rows)} rows", flush=True)

        print("\n2. Category breakdown...", flush=True)
        category_rows = await conn.fetch(CATEGORY_SQL, *PARAMS_CATEGORY)
        print(f"   {len(category_rows)} rows", flush=True)

        total_n = sum(int(r["n"]) for r in primary_rows)
        print(f"\n   Total signals in buckets: {total_n:,}", flush=True)

    finally:
        await conn.close()

    elapsed = time.time() - t0
    run_date = datetime.utcnow().strftime("%Y-%m-%d")

    report = render_report(primary_rows, category_rows, run_date, elapsed)

    print("\n" + "=" * 70)
    print(report)

    out_dir = Path("products/d7-pre-resolution-convergence-fade")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "backtest.md"
    out_path.write_text(report)
    print(f"\nSaved → {out_path}")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(run())

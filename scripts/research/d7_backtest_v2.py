#!/usr/bin/env python3
"""D7 PreResolutionConvergenceFade — Stage 2 backtest v2.

Источники (НЕ oracle_snapshots — там слишком sparse):
  - quant.parquet (568M trades, real price time-series)
  - markets.parquet (734K markets с end_date, condition_id, neg_risk)
  - full_hf_enriched.parquet (532K resolved positions с win label, side='NO')

Гипотеза та же: trade в окне 4-72h до end_date с YES price ∈ [0.85,0.95]
∪ [0.05,0.15] и market volume ≥ $5K — фейдим экстремальную сторону.

Stage 2 thresholds:
  1. ROI > 30% после fee+slippage
  2. Halved Sharpe > 1.0
  3. n >= 200 per bucket (теперь n очень большие — easy)
  4. Adversarial mirror check: |no_roi - yes_roi| / max < 50%
  5. Post-V2 split (V2 cutover = 2026-04-28, но quant.parquet кончается 2026-03-31 → весь датасет pre-V2)

Запуск на VPS:
    cd ~/oracle && python3 scripts/research/d7_backtest_v2.py
"""

from __future__ import annotations

import sys
import time
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import duckdb

# ── Константы D7 ─────────────────────────────────────────────────────────────

VOLUME_FLOOR_USD = 5_000
HOURS_MIN = 4
HOURS_MAX = 72

NO_ZONE_LOW   = 0.85
NO_ZONE_HIGH  = 0.95
YES_ZONE_LOW  = 0.05
YES_ZONE_HIGH = 0.15

# Quant.parquet ends 2026-03-31, V2 cutover = 2026-04-28 → no post-V2 data here
SINCE_TIMESTAMP = int(datetime(2026, 2, 1).timestamp())  # last 2 months in quant

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

QUANT_PATH       = "/mnt/data/hf_data/quant.parquet"
MARKETS_PATH     = "/mnt/data/polymarket_dataset/markets.parquet"
ENRICHED_PATH    = "/mnt/data/polymarket_dataset/full_hf_enriched.parquet"


# ── SQL ───────────────────────────────────────────────────────────────────────

PRIMARY_SQL = f"""
WITH d7_signals AS (
    SELECT
        q.timestamp,
        q.price                                              AS yes_price,
        q.condition_id,
        m.end_date,
        m.volume,
        m.event_id,
        e.win                                                AS no_win,
        (EXTRACT(EPOCH FROM m.end_date)::BIGINT - q.timestamp)/3600.0 AS hrs_to_end,
        CASE
            WHEN q.price BETWEEN 0.85 AND 0.90 THEN '0.85-0.90'
            WHEN q.price BETWEEN 0.90 AND 0.95 THEN '0.90-0.95'
            WHEN q.price BETWEEN 0.05 AND 0.10 THEN '0.05-0.10'
            WHEN q.price BETWEEN 0.10 AND 0.15 THEN '0.10-0.15'
        END                                                  AS price_bucket,
        CASE
            WHEN q.price BETWEEN 0.85 AND 0.95 THEN 'NO'
            WHEN q.price BETWEEN 0.05 AND 0.15 THEN 'YES'
        END                                                  AS trade_side
    FROM read_parquet('{QUANT_PATH}') q
    JOIN read_parquet('{MARKETS_PATH}') m
      ON m.condition_id = q.condition_id
    JOIN read_parquet('{ENRICHED_PATH}') e
      ON e.market_id = q.condition_id  -- e.side='NO' always
    WHERE q.timestamp >= {SINCE_TIMESTAMP}
      AND m.end_date IS NOT NULL
      AND m.closed = 1
      AND m.volume >= {VOLUME_FLOOR_USD}
      AND ((q.price BETWEEN 0.85 AND 0.95) OR (q.price BETWEEN 0.05 AND 0.15))
      AND (EXTRACT(EPOCH FROM m.end_date)::BIGINT - q.timestamp)/3600.0
            BETWEEN {HOURS_MIN} AND {HOURS_MAX}
),
with_outcome AS (
    SELECT *,
        -- For NO side trade: win = no_win
        -- For YES side trade: win = 1 - no_win
        CASE WHEN trade_side = 'NO'  THEN no_win
             WHEN trade_side = 'YES' THEN 1 - no_win
        END AS win,
        CASE
            WHEN trade_side = 'NO'  -- buy NO at (1 - yes_price), payoff 1 if no_win=1
                THEN CASE WHEN no_win = 1
                          THEN (1.0 / NULLIF(1.0 - yes_price, 0)) - 1
                          ELSE -1.0 END
            WHEN trade_side = 'YES' -- buy YES at yes_price, payoff 1 if no_win=0
                THEN CASE WHEN no_win = 0
                          THEN (1.0 / NULLIF(yes_price, 0)) - 1
                          ELSE -1.0 END
        END AS raw_roi,
        CASE
            WHEN hrs_to_end BETWEEN 4 AND 12  THEN '12-4h'
            WHEN hrs_to_end BETWEEN 12 AND 24 THEN '24-12h'
            WHEN hrs_to_end BETWEEN 24 AND 48 THEN '48-24h'
            WHEN hrs_to_end BETWEEN 48 AND 72 THEN '72-48h'
        END AS time_bucket
    FROM d7_signals
    WHERE price_bucket IS NOT NULL
)
SELECT
    time_bucket,
    price_bucket,
    trade_side,
    COUNT(*)::BIGINT                       AS n,
    COUNT(DISTINCT condition_id)::BIGINT   AS n_markets,
    COUNT(DISTINCT event_id)::BIGINT       AS n_events,
    ROUND(AVG(win)::NUMERIC, 4)            AS win_rate,
    ROUND(AVG(raw_roi)::NUMERIC, 4)        AS avg_raw_roi,
    ROUND(STDDEV(raw_roi)::NUMERIC, 4)     AS std_raw_roi
FROM with_outcome
WHERE time_bucket IS NOT NULL
GROUP BY 1, 2, 3
HAVING COUNT(*) >= 50
ORDER BY trade_side DESC, price_bucket, time_bucket
"""

# Per-event aggregation (для honest sample size — D5 lesson: events not trades)
PER_EVENT_SQL = f"""
WITH d7_signals AS (
    SELECT
        q.timestamp, q.price AS yes_price, q.condition_id,
        m.end_date, m.event_id, e.win AS no_win,
        (EXTRACT(EPOCH FROM m.end_date)::BIGINT - q.timestamp)/3600.0 AS hrs_to_end,
        CASE
            WHEN q.price BETWEEN 0.85 AND 0.95 THEN 'NO'
            WHEN q.price BETWEEN 0.05 AND 0.15 THEN 'YES'
        END AS trade_side
    FROM read_parquet('{QUANT_PATH}') q
    JOIN read_parquet('{MARKETS_PATH}') m ON m.condition_id = q.condition_id
    JOIN read_parquet('{ENRICHED_PATH}') e ON e.market_id = q.condition_id
    WHERE q.timestamp >= {SINCE_TIMESTAMP}
      AND m.end_date IS NOT NULL AND m.closed = 1 AND m.volume >= {VOLUME_FLOOR_USD}
      AND ((q.price BETWEEN 0.85 AND 0.95) OR (q.price BETWEEN 0.05 AND 0.15))
      AND (EXTRACT(EPOCH FROM m.end_date)::BIGINT - q.timestamp)/3600.0
            BETWEEN {HOURS_MIN} AND {HOURS_MAX}
),
with_outcome AS (
    SELECT *,
        CASE WHEN trade_side = 'NO'  THEN no_win ELSE 1 - no_win END AS win,
        CASE
            WHEN trade_side = 'NO'
                THEN CASE WHEN no_win = 1 THEN (1.0/NULLIF(1.0-yes_price,0))-1 ELSE -1.0 END
            WHEN trade_side = 'YES'
                THEN CASE WHEN no_win = 0 THEN (1.0/NULLIF(yes_price,0))-1 ELSE -1.0 END
        END AS raw_roi
    FROM d7_signals
),
per_event AS (
    -- Один сигнал на (event, side) — берём средний ROI всех trades в окне
    SELECT event_id, trade_side, AVG(raw_roi) AS event_roi, MAX(win) AS any_win
    FROM with_outcome
    GROUP BY event_id, trade_side
)
SELECT
    trade_side,
    COUNT(*)::BIGINT AS n_events,
    ROUND(AVG(any_win)::NUMERIC, 4) AS event_win_rate,
    ROUND(AVG(event_roi)::NUMERIC, 4) AS avg_event_roi,
    ROUND(STDDEV(event_roi)::NUMERIC, 4) AS std_event_roi
FROM per_event
GROUP BY 1
ORDER BY 1
"""

# Per-category breakdown
CATEGORY_SQL = f"""
WITH d7_signals AS (
    SELECT
        q.timestamp, q.price AS yes_price, q.condition_id,
        m.end_date, e.category, e.win AS no_win,
        (EXTRACT(EPOCH FROM m.end_date)::BIGINT - q.timestamp)/3600.0 AS hrs_to_end,
        CASE
            WHEN q.price BETWEEN 0.85 AND 0.95 THEN 'NO'
            WHEN q.price BETWEEN 0.05 AND 0.15 THEN 'YES'
        END AS trade_side
    FROM read_parquet('{QUANT_PATH}') q
    JOIN read_parquet('{MARKETS_PATH}') m ON m.condition_id = q.condition_id
    JOIN read_parquet('{ENRICHED_PATH}') e ON e.market_id = q.condition_id
    WHERE q.timestamp >= {SINCE_TIMESTAMP}
      AND m.end_date IS NOT NULL AND m.closed = 1 AND m.volume >= {VOLUME_FLOOR_USD}
      AND ((q.price BETWEEN 0.85 AND 0.95) OR (q.price BETWEEN 0.05 AND 0.15))
      AND (EXTRACT(EPOCH FROM m.end_date)::BIGINT - q.timestamp)/3600.0
            BETWEEN {HOURS_MIN} AND {HOURS_MAX}
),
with_outcome AS (
    SELECT *,
        CASE WHEN trade_side = 'NO' THEN no_win ELSE 1 - no_win END AS win,
        CASE
            WHEN trade_side = 'NO'
                THEN CASE WHEN no_win = 1 THEN (1.0/NULLIF(1.0-yes_price,0))-1 ELSE -1.0 END
            WHEN trade_side = 'YES'
                THEN CASE WHEN no_win = 0 THEN (1.0/NULLIF(yes_price,0))-1 ELSE -1.0 END
        END AS raw_roi
    FROM d7_signals
)
SELECT
    category,
    trade_side,
    COUNT(*)::BIGINT AS n,
    ROUND(AVG(win)::NUMERIC, 4) AS win_rate,
    ROUND(AVG(raw_roi)::NUMERIC, 4) AS avg_raw_roi
FROM with_outcome
WHERE category IS NOT NULL
GROUP BY 1, 2
HAVING COUNT(*) >= 100
ORDER BY trade_side DESC, avg_raw_roi DESC
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def halved_sr(mean_roi: float, std_roi: float, n: int) -> float:
    if std_roi == 0 or n < 2:
        return 0.0
    nominal = (mean_roi / std_roi) * (max(n, 50) ** 0.5)
    return nominal / 2.0


def vi(passed: bool) -> str:
    return "✅" if passed else "❌"


def render_report(primary_rows, per_event_rows, category_rows, run_date, elapsed):
    lines = []
    A = lines.append

    A("# D7 PreResolutionConvergenceFade — Stage 2 Backtest v2 (quant.parquet)")
    A("")
    A(f"**Дата:** {run_date}  ")
    A(f"**Источники:** `quant.parquet` (trades) + `markets.parquet` (end_date) + `full_hf_enriched.parquet` (win)  ")
    A(f"**Volume floor:** ${VOLUME_FLOOR_USD:,} (lifetime)  ")
    A(f"**Trigger window:** {HOURS_MIN}–{HOURS_MAX}h до resolve  ")
    A(f"**Price zones:** NO [{NO_ZONE_LOW}–{NO_ZONE_HIGH}] | YES [{YES_ZONE_LOW}–{YES_ZONE_HIGH}]  ")
    A(f"**Период:** since 2026-02-01 (quant.parquet ends 2026-03-31, all pre-V2)  ")
    A("")
    A("---")
    A("")

    # --- 1. Per-bucket primary ---
    A("## 1. Per-bucket: time × price × side")
    A("")
    A("| time_bucket | price_bucket | side | n_trades | n_markets | n_events | WR | raw_ROI | fee | net_ROI | DSR | ROI✅ | DSR✅ | N✅ |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|---|")

    bucket_stats = {}  # (price_bucket, side) -> aggregated
    for r in primary_rows:
        tb = r["time_bucket"]
        pb = r["price_bucket"]
        side = r["trade_side"]
        n = int(r["n"])
        nm = int(r["n_markets"])
        ne = int(r["n_events"])
        wr = float(r["win_rate"] or 0)
        raw = float(r["avg_raw_roi"] or 0)
        std = float(r["std_raw_roi"] or 0)

        fee = FEES.get(pb, 0.05)
        net = raw - fee
        dsr = halved_sr(net, std, n)

        key = (pb, side)
        if key not in bucket_stats:
            bucket_stats[key] = {"n": 0, "raw": 0, "net": 0, "dsr_list": [], "wr": 0}
        bucket_stats[key]["n"] += n
        bucket_stats[key]["raw"] += raw * n
        bucket_stats[key]["net"] += net * n
        bucket_stats[key]["wr"] += wr * n
        bucket_stats[key]["dsr_list"].append(dsr)

        A(f"| {tb} | {pb} | {side} | {n:,} | {nm:,} | {ne:,} | "
          f"{wr:.1%} | {raw:+.1%} | {fee:.0%} | {net:+.1%} | {dsr:.2f} | "
          f"{vi(net > ROI_THRESHOLD)} | {vi(dsr > DSR_THRESHOLD)} | {vi(n >= N_THRESHOLD)} |")
    A("")

    # --- 2. Aggregate by (price_bucket, side) ---
    A("## 2. Aggregated: price_bucket × side (across all time windows)")
    A("")
    A("| price_bucket | side | n | WR | net_ROI | avg_DSR |")
    A("|---|---|---|---|---|---|")
    for (pb, side), s in sorted(bucket_stats.items()):
        if s["n"] == 0:
            continue
        wr = s["wr"] / s["n"]
        net = s["net"] / s["n"]
        avg_dsr = sum(s["dsr_list"]) / len(s["dsr_list"])
        A(f"| {pb} | {side} | {s['n']:,} | {wr:.1%} | {net:+.1%} | {avg_dsr:.2f} |")
    A("")

    # --- 3. Per-event (honest n) ---
    A("## 3. Per-event aggregation (honest sample size)")
    A("")
    A("Один сигнал на (event_id, side) — средний ROI всех trades в окне.")
    A("")
    A("| side | n_events | event_WR | avg_event_ROI | std_event_ROI |")
    A("|---|---|---|---|---|")
    event_results = {}
    for r in per_event_rows:
        side = r["trade_side"]
        ne = int(r["n_events"])
        wr = float(r["event_win_rate"] or 0)
        roi = float(r["avg_event_roi"] or 0)
        std = float(r["std_event_roi"] or 0)
        # Apply average fee per side
        avg_fee = 0.06
        net_event = roi - avg_fee
        event_results[side] = {"n": ne, "net": net_event, "raw": roi, "wr": wr}
        A(f"| {side} | {ne:,} | {wr:.1%} | {roi:+.1%} (raw) / {net_event:+.1%} (net) | {std:.2f} |")
    A("")

    # --- 4. Adversarial mirror check ---
    A("## 4. Adversarial mirror check (NO vs YES symmetry)")
    A("")
    no_results = [(pb, s) for (pb, side), s in bucket_stats.items() if side == "NO"]
    yes_results = [(pb, s) for (pb, side), s in bucket_stats.items() if side == "YES"]

    if no_results and yes_results:
        no_total_net = sum(s["net"] for _, s in no_results) / len(no_results)
        yes_total_net = sum(s["net"] for _, s in yes_results) / len(yes_results)

        A(f"- **NO-side avg net ROI**: {no_total_net:+.1%}")
        A(f"- **YES-side avg net ROI**: {yes_total_net:+.1%}")

        if no_total_net <= 0 or yes_total_net <= 0:
            mirror_pass = False
            mirror_msg = "⚠️ хотя бы одна сторона убыточна — pinning НЕ подтверждён"
        else:
            asymmetry = abs(no_total_net - yes_total_net) / max(no_total_net, yes_total_net)
            mirror_pass = asymmetry < MIRROR_MAX_ASYMM
            mirror_msg = f"asymmetry = {asymmetry:.0%} (порог < {MIRROR_MAX_ASYMM:.0%})"

        A(f"- **Mirror check:** {vi(mirror_pass)} {mirror_msg}")
    else:
        mirror_pass = False
        A("- Недостаточно данных для mirror check")
    A("")

    # --- 5. Per-category ---
    A("## 5. Per-category breakdown")
    A("")
    A("| category | side | n | WR | raw_ROI | net_ROI |")
    A("|---|---|---|---|---|---|")
    for r in category_rows:
        cat = r["category"]
        side = r["trade_side"]
        n = int(r["n"])
        wr = float(r["win_rate"] or 0)
        raw = float(r["avg_raw_roi"] or 0)
        net = raw - 0.06
        A(f"| {cat} | {side} | {n:,} | {wr:.1%} | {raw:+.1%} | {net:+.1%} |")
    A("")

    # --- 6. Final verdict ---
    A("## 6. Final Verdict")
    A("")

    # Stage 2 thresholds across NO + YES
    all_keys = list(bucket_stats.keys())
    roi_passes = sum(1 for k in all_keys if bucket_stats[k]["n"] > 0
                     and (bucket_stats[k]["net"] / bucket_stats[k]["n"]) > ROI_THRESHOLD)
    n_passes = sum(1 for k in all_keys if bucket_stats[k]["n"] >= N_THRESHOLD)
    dsr_passes = sum(1 for k in all_keys if bucket_stats[k]["dsr_list"]
                     and (sum(bucket_stats[k]["dsr_list"]) / len(bucket_stats[k]["dsr_list"])) > DSR_THRESHOLD)
    total_b = len(all_keys)

    A("### Stage 2 threshold summary")
    A("")
    A("| Threshold | Result | Detail |")
    A("|---|---|---|")
    A(f"| ROI > {ROI_THRESHOLD:.0%} | {vi(roi_passes > 0)} | {roi_passes}/{total_b} buckets pass |")
    A(f"| DSR > {DSR_THRESHOLD:.1f} | {vi(dsr_passes > 0)} | {dsr_passes}/{total_b} buckets pass |")
    A(f"| n >= {N_THRESHOLD} | {vi(n_passes > 0)} | {n_passes}/{total_b} buckets pass |")
    A(f"| Adversarial mirror | {vi(mirror_pass)} | NO vs YES symmetric |")
    A(f"| Post-V2 split | ⚠️ N/A | quant.parquet ends 2026-03-31 (pre-V2 only) |")
    A("")

    # Per-event verdict
    if event_results:
        no_evt = event_results.get("NO", {})
        yes_evt = event_results.get("YES", {})
        A("### Per-event verdict")
        A("")
        if no_evt:
            A(f"- NO side: {no_evt['n']:,} events, net_ROI {no_evt['net']:+.1%}, "
              f"WR {no_evt['wr']:.1%}")
        if yes_evt:
            A(f"- YES side: {yes_evt['n']:,} events, net_ROI {yes_evt['net']:+.1%}, "
              f"WR {yes_evt['wr']:.1%}")
        A("")

    all_pass = roi_passes > 0 and dsr_passes > 0 and n_passes > 0 and mirror_pass
    if all_pass:
        A("**Verdict: ✅ PASS — переход в Stage 3 (economics analysis)**")
    elif roi_passes == 0 and dsr_passes == 0:
        A("**Verdict: ❌ FAIL — нет edge ни в одном bucket**")
    else:
        A("**Verdict: ⚠️ CONDITIONAL — часть threshold прошла, нужен refinement**")

    A("")
    A("---")
    A(f"*Backtest completed in {elapsed:.1f}s*")

    return "\n".join(lines)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    t0 = time.time()
    run_date = date.today().isoformat()

    print("Connecting DuckDB...", flush=True)
    con = duckdb.connect()
    con.execute('SET memory_limit="6GB"')
    con.execute('SET threads=4')

    print("Running PRIMARY query...", flush=True)
    primary_rows = con.execute(PRIMARY_SQL).fetchall()
    primary_dicts = [dict(zip([d[0] for d in con.description], r)) for r in primary_rows]
    print(f"  → {len(primary_dicts)} rows", flush=True)

    print("Running PER-EVENT query...", flush=True)
    per_event_rows = con.execute(PER_EVENT_SQL).fetchall()
    per_event_dicts = [dict(zip([d[0] for d in con.description], r)) for r in per_event_rows]
    print(f"  → {len(per_event_dicts)} rows", flush=True)

    print("Running CATEGORY query...", flush=True)
    category_rows = con.execute(CATEGORY_SQL).fetchall()
    category_dicts = [dict(zip([d[0] for d in con.description], r)) for r in category_rows]
    print(f"  → {len(category_dicts)} rows", flush=True)

    elapsed = time.time() - t0
    report = render_report(primary_dicts, per_event_dicts, category_dicts, run_date, elapsed)

    out_dir = Path(__file__).parent.parent.parent / "products" / "d7-pre-resolution-convergence-fade"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "backtest_v2.md"
    out_path.write_text(report)
    print(f"\nReport saved: {out_path}", flush=True)

    print("\n" + "=" * 80)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

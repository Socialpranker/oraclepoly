#!/usr/bin/env python3
"""D7 PreResolutionConvergenceFade — Stage 2 backtest v3 (HONEST per-market).

Изменения от v2:
  - Один сигнал на маркет (FIRST trade в окне price+time), НЕ per-trade
  - Per-event aggregation как secondary metric
  - Per-trade weighting в v2 переоценивало edge (winning маркет = 1000s of trades)

Источники:
  - quant.parquet (568M trades)
  - markets.parquet (734K markets)
  - full_hf_enriched.parquet (532K labels)

Stage 2 thresholds:
  1. ROI > 30% после fee+slippage per market bucket
  2. Halved Sharpe > 1.0
  3. n_markets >= 200 per bucket
  4. Adversarial mirror: |no_roi - yes_roi| / max < 50% AND обе стороны positive
  5. Post-V2 split: N/A (quant.parquet ends 2026-03-31)

Запуск на VPS:
    cd ~/oracle && python3 scripts/research/d7_backtest_v3.py
"""

from __future__ import annotations

import sys
import time
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

SINCE_TIMESTAMP = int(datetime(2026, 2, 1).timestamp())

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

QUANT_PATH    = "/mnt/data/hf_data/quant.parquet"
MARKETS_PATH  = "/mnt/data/polymarket_dataset/markets.parquet"
ENRICHED_PATH = "/mnt/data/polymarket_dataset/full_hf_enriched.parquet"


# ── SQL: per-market entry (FIRST trade in window) ────────────────────────────

PER_MARKET_SQL = f"""
WITH first_trade_in_window AS (
    SELECT
        q.condition_id,
        m.event_id,
        m.end_date,
        m.volume,
        e.win                         AS no_win,
        e.category,
        FIRST_VALUE(q.price) OVER (
            PARTITION BY q.condition_id
            ORDER BY q.timestamp
        )                             AS entry_price,
        FIRST_VALUE(q.timestamp) OVER (
            PARTITION BY q.condition_id
            ORDER BY q.timestamp
        )                             AS entry_ts
    FROM read_parquet('{QUANT_PATH}') q
    JOIN read_parquet('{MARKETS_PATH}') m ON m.condition_id = q.condition_id
    JOIN read_parquet('{ENRICHED_PATH}') e ON e.market_id  = q.condition_id
    WHERE q.timestamp >= {SINCE_TIMESTAMP}
      AND m.end_date IS NOT NULL
      AND m.closed = 1
      AND m.volume >= {VOLUME_FLOOR_USD}
      AND ((q.price BETWEEN 0.85 AND 0.95) OR (q.price BETWEEN 0.05 AND 0.15))
      AND (EXTRACT(EPOCH FROM m.end_date)::BIGINT - q.timestamp)/3600.0
            BETWEEN {HOURS_MIN} AND {HOURS_MAX}
),
deduped AS (
    SELECT DISTINCT condition_id, event_id, end_date, volume,
                    no_win, category, entry_price, entry_ts
    FROM first_trade_in_window
),
classified AS (
    SELECT *,
        (EXTRACT(EPOCH FROM end_date)::BIGINT - entry_ts)/3600.0 AS hrs_to_end,
        CASE
            WHEN entry_price BETWEEN 0.05 AND 0.10 THEN '0.05-0.10'
            WHEN entry_price BETWEEN 0.10 AND 0.15 THEN '0.10-0.15'
            WHEN entry_price BETWEEN 0.85 AND 0.90 THEN '0.85-0.90'
            WHEN entry_price BETWEEN 0.90 AND 0.95 THEN '0.90-0.95'
        END AS price_bucket,
        CASE
            WHEN entry_price BETWEEN 0.05 AND 0.15 THEN 'YES'
            WHEN entry_price BETWEEN 0.85 AND 0.95 THEN 'NO'
        END AS side
    FROM deduped
),
with_roi AS (
    SELECT *,
        CASE
            WHEN side = 'YES'
                THEN CASE WHEN no_win = 0 THEN (1.0/NULLIF(entry_price,0))-1 ELSE -1.0 END
            WHEN side = 'NO'
                THEN CASE WHEN no_win = 1 THEN (1.0/NULLIF(1.0-entry_price,0))-1 ELSE -1.0 END
        END AS raw_roi,
        CASE
            WHEN side = 'YES' AND no_win = 0 THEN 1
            WHEN side = 'NO'  AND no_win = 1 THEN 1
            ELSE 0
        END AS win,
        CASE
            WHEN hrs_to_end BETWEEN 4 AND 12  THEN '12-4h'
            WHEN hrs_to_end BETWEEN 12 AND 24 THEN '24-12h'
            WHEN hrs_to_end BETWEEN 24 AND 48 THEN '48-24h'
            WHEN hrs_to_end BETWEEN 48 AND 72 THEN '72-48h'
        END AS time_bucket
    FROM classified
    WHERE price_bucket IS NOT NULL
)
SELECT
    time_bucket,
    price_bucket,
    side,
    COUNT(*)::BIGINT                          AS n_markets,
    COUNT(DISTINCT event_id)::BIGINT          AS n_events,
    ROUND(AVG(win)::NUMERIC, 4)               AS win_rate,
    ROUND(AVG(raw_roi)::NUMERIC, 4)           AS avg_raw_roi,
    ROUND(STDDEV(raw_roi)::NUMERIC, 4)        AS std_raw_roi
FROM with_roi
WHERE time_bucket IS NOT NULL
GROUP BY 1, 2, 3
HAVING COUNT(*) >= 30
ORDER BY side DESC, price_bucket, time_bucket
"""

# Per-event aggregation (один результат на event)
PER_EVENT_SQL = f"""
WITH first_trade_in_window AS (
    SELECT
        q.condition_id, m.event_id, m.end_date, m.volume,
        e.win AS no_win, e.category,
        FIRST_VALUE(q.price) OVER (PARTITION BY q.condition_id ORDER BY q.timestamp) AS entry_price
    FROM read_parquet('{QUANT_PATH}') q
    JOIN read_parquet('{MARKETS_PATH}') m ON m.condition_id = q.condition_id
    JOIN read_parquet('{ENRICHED_PATH}') e ON e.market_id = q.condition_id
    WHERE q.timestamp >= {SINCE_TIMESTAMP}
      AND m.end_date IS NOT NULL AND m.closed = 1 AND m.volume >= {VOLUME_FLOOR_USD}
      AND ((q.price BETWEEN 0.85 AND 0.95) OR (q.price BETWEEN 0.05 AND 0.15))
      AND (EXTRACT(EPOCH FROM m.end_date)::BIGINT - q.timestamp)/3600.0 BETWEEN {HOURS_MIN} AND {HOURS_MAX}
),
deduped AS (
    SELECT DISTINCT condition_id, event_id, no_win, category, entry_price
    FROM first_trade_in_window
),
with_roi AS (
    SELECT *,
        CASE
            WHEN entry_price BETWEEN 0.05 AND 0.15 THEN 'YES'
            WHEN entry_price BETWEEN 0.85 AND 0.95 THEN 'NO'
        END AS side,
        CASE
            WHEN entry_price BETWEEN 0.05 AND 0.15
                THEN CASE WHEN no_win = 0 THEN (1.0/NULLIF(entry_price,0))-1 ELSE -1.0 END
            WHEN entry_price BETWEEN 0.85 AND 0.95
                THEN CASE WHEN no_win = 1 THEN (1.0/NULLIF(1.0-entry_price,0))-1 ELSE -1.0 END
        END AS roi
    FROM deduped
),
per_event AS (
    -- Агрегация: один сигнал на (event, side) — берём средний ROI всех маркетов
    SELECT event_id, side, AVG(roi) AS event_roi, MAX(CASE WHEN roi > 0 THEN 1 ELSE 0 END) AS any_win,
           COUNT(*) AS n_mkts_in_event
    FROM with_roi
    GROUP BY event_id, side
)
SELECT
    side,
    COUNT(*)::BIGINT                          AS n_events,
    ROUND(AVG(any_win)::NUMERIC, 4)           AS event_win_rate,
    ROUND(AVG(event_roi)::NUMERIC, 4)         AS avg_event_roi,
    ROUND(STDDEV(event_roi)::NUMERIC, 4)      AS std_event_roi,
    ROUND(AVG(n_mkts_in_event)::NUMERIC, 2)   AS avg_mkts_per_event
FROM per_event
GROUP BY 1
ORDER BY 1
"""

# Per-category
CATEGORY_SQL = f"""
WITH first_trade_in_window AS (
    SELECT
        q.condition_id, m.end_date, m.volume,
        e.win AS no_win, e.category,
        FIRST_VALUE(q.price) OVER (PARTITION BY q.condition_id ORDER BY q.timestamp) AS entry_price
    FROM read_parquet('{QUANT_PATH}') q
    JOIN read_parquet('{MARKETS_PATH}') m ON m.condition_id = q.condition_id
    JOIN read_parquet('{ENRICHED_PATH}') e ON e.market_id = q.condition_id
    WHERE q.timestamp >= {SINCE_TIMESTAMP}
      AND m.end_date IS NOT NULL AND m.closed = 1 AND m.volume >= {VOLUME_FLOOR_USD}
      AND ((q.price BETWEEN 0.85 AND 0.95) OR (q.price BETWEEN 0.05 AND 0.15))
      AND (EXTRACT(EPOCH FROM m.end_date)::BIGINT - q.timestamp)/3600.0 BETWEEN {HOURS_MIN} AND {HOURS_MAX}
),
deduped AS (
    SELECT DISTINCT condition_id, no_win, category, entry_price
    FROM first_trade_in_window
),
with_roi AS (
    SELECT *,
        CASE
            WHEN entry_price BETWEEN 0.05 AND 0.15 THEN 'YES'
            WHEN entry_price BETWEEN 0.85 AND 0.95 THEN 'NO'
        END AS side,
        CASE
            WHEN entry_price BETWEEN 0.05 AND 0.15
                THEN CASE WHEN no_win = 0 THEN (1.0/NULLIF(entry_price,0))-1 ELSE -1.0 END
            WHEN entry_price BETWEEN 0.85 AND 0.95
                THEN CASE WHEN no_win = 1 THEN (1.0/NULLIF(1.0-entry_price,0))-1 ELSE -1.0 END
        END AS roi,
        CASE
            WHEN entry_price BETWEEN 0.05 AND 0.15 AND no_win = 0 THEN 1
            WHEN entry_price BETWEEN 0.85 AND 0.95 AND no_win = 1 THEN 1
            ELSE 0
        END AS win
    FROM deduped
)
SELECT
    category,
    side,
    COUNT(*)::BIGINT AS n_markets,
    ROUND(AVG(win)::NUMERIC, 4) AS win_rate,
    ROUND(AVG(roi)::NUMERIC, 4) AS avg_roi
FROM with_roi
WHERE category IS NOT NULL AND side IS NOT NULL
GROUP BY 1, 2
HAVING COUNT(*) >= 30
ORDER BY side DESC, avg_roi DESC
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def halved_sr(mean_roi: float, std_roi: float, n: int) -> float:
    if std_roi == 0 or n < 2:
        return 0.0
    nominal = (mean_roi / std_roi) * (max(n, 50) ** 0.5)
    return nominal / 2.0


def vi(p: bool) -> str:
    return "✅" if p else "❌"


def render_report(per_market_rows, per_event_rows, category_rows, run_date, elapsed):
    lines = []
    A = lines.append

    A("# D7 PreResolutionConvergenceFade — Stage 2 Backtest v3 (HONEST per-market)")
    A("")
    A(f"**Дата:** {run_date}  ")
    A(f"**Источники:** `quant.parquet` + `markets.parquet` + `full_hf_enriched.parquet`  ")
    A(f"**Volume floor:** ${VOLUME_FLOOR_USD:,} (lifetime)  ")
    A(f"**Trigger window:** {HOURS_MIN}–{HOURS_MAX}h до resolve  ")
    A(f"**Price zones:** NO [{NO_ZONE_LOW}–{NO_ZONE_HIGH}] | YES [{YES_ZONE_LOW}–{YES_ZONE_HIGH}]  ")
    A(f"**Период:** since 2026-02-01 (pre-V2 only — quant.parquet ends 2026-03-31)  ")
    A(f"**Sample method:** ОДИН сигнал на маркет (первый trade в окне), НЕ per-trade  ")
    A("")
    A("---")
    A("")

    # --- 1. Per-bucket ---
    A("## 1. Per-bucket: time × price × side")
    A("")
    A("| time | price | side | n_mkts | n_evts | WR | raw_ROI | fee | net_ROI | DSR | ROI✅ | DSR✅ | N✅ |")
    A("|---|---|---|---|---|---|---|---|---|---|---|---|---|")

    bucket_agg = {}  # (price_bucket, side) -> {n, weighted_net, dsr_list, wr}
    for r in per_market_rows:
        tb = r["time_bucket"]
        pb = r["price_bucket"]
        side = r["side"]
        n = int(r["n_markets"])
        ne = int(r["n_events"])
        wr = float(r["win_rate"] or 0)
        raw = float(r["avg_raw_roi"] or 0)
        std = float(r["std_raw_roi"] or 0)

        fee = FEES.get(pb, 0.05)
        net = raw - fee
        dsr = halved_sr(net, std, n)

        key = (pb, side)
        if key not in bucket_agg:
            bucket_agg[key] = {"n": 0, "weighted_net": 0, "weighted_wr": 0, "dsr_list": []}
        bucket_agg[key]["n"] += n
        bucket_agg[key]["weighted_net"] += net * n
        bucket_agg[key]["weighted_wr"] += wr * n
        bucket_agg[key]["dsr_list"].append(dsr)

        A(f"| {tb} | {pb} | {side} | {n:,} | {ne:,} | "
          f"{wr:.1%} | {raw:+.1%} | {fee:.0%} | {net:+.1%} | {dsr:.2f} | "
          f"{vi(net > ROI_THRESHOLD)} | {vi(dsr > DSR_THRESHOLD)} | {vi(n >= N_THRESHOLD)} |")
    A("")

    # --- 2. Aggregated per (price, side) ---
    A("## 2. Aggregated per (price_bucket × side)")
    A("")
    A("| price_bucket | side | n_markets | WR | net_ROI | avg_DSR |")
    A("|---|---|---|---|---|---|")
    for key, s in sorted(bucket_agg.items()):
        if s["n"] == 0:
            continue
        net = s["weighted_net"] / s["n"]
        wr = s["weighted_wr"] / s["n"]
        avg_dsr = sum(s["dsr_list"]) / len(s["dsr_list"])
        A(f"| {key[0]} | {key[1]} | {s['n']:,} | {wr:.1%} | {net:+.1%} | {avg_dsr:.2f} |")
    A("")

    # --- 3. Per-event ---
    A("## 3. Per-event aggregation (один сигнал на event)")
    A("")
    A("| side | n_events | event_WR | avg_event_ROI raw | net | std | avg_mkts/event |")
    A("|---|---|---|---|---|---|---|")
    event_results = {}
    for r in per_event_rows:
        side = r["side"]
        ne = int(r["n_events"])
        wr = float(r["event_win_rate"] or 0)
        roi = float(r["avg_event_roi"] or 0)
        std = float(r["std_event_roi"] or 0)
        avg_m = float(r["avg_mkts_per_event"] or 0)
        net = roi - 0.06
        event_results[side] = {"n": ne, "net": net, "wr": wr}
        A(f"| {side} | {ne:,} | {wr:.1%} | {roi:+.1%} | {net:+.1%} | {std:.2f} | {avg_m:.1f} |")
    A("")

    # --- 4. Adversarial mirror (per-market aggregated) ---
    A("## 4. Adversarial mirror check")
    A("")
    no_buckets = [(k, s) for k, s in bucket_agg.items() if k[1] == "NO" and s["n"] > 0]
    yes_buckets = [(k, s) for k, s in bucket_agg.items() if k[1] == "YES" and s["n"] > 0]

    if no_buckets and yes_buckets:
        no_total_n = sum(s["n"] for _, s in no_buckets)
        no_avg_net = sum(s["weighted_net"] for _, s in no_buckets) / no_total_n

        yes_total_n = sum(s["n"] for _, s in yes_buckets)
        yes_avg_net = sum(s["weighted_net"] for _, s in yes_buckets) / yes_total_n

        A(f"- **NO-side avg net ROI** ({no_total_n:,} markets): {no_avg_net:+.1%}")
        A(f"- **YES-side avg net ROI** ({yes_total_n:,} markets): {yes_avg_net:+.1%}")

        both_positive = no_avg_net > 0 and yes_avg_net > 0
        if both_positive:
            asymmetry = abs(no_avg_net - yes_avg_net) / max(no_avg_net, yes_avg_net)
            mirror_pass = asymmetry < MIRROR_MAX_ASYMM
            A(f"- **Asymmetry:** {asymmetry:.0%} (порог < {MIRROR_MAX_ASYMM:.0%})")
            A(f"- **Both sides positive:** ✅")
            A(f"- **Mirror check:** {vi(mirror_pass)} {'PASS' if mirror_pass else 'FAIL — асимметрия выше порога, но обе стороны позитивны'}")
        else:
            mirror_pass = False
            A(f"- **Both sides positive:** ❌")
            A(f"- **Mirror check:** ❌ FAIL — хотя бы одна сторона убыточна")
    else:
        mirror_pass = False
        A("- Недостаточно данных")
    A("")

    # --- 5. Per-category ---
    A("## 5. Per-category breakdown")
    A("")
    A("| category | side | n_markets | WR | avg_ROI | net_ROI |")
    A("|---|---|---|---|---|---|")
    for r in category_rows:
        cat = r["category"]
        side = r["side"]
        n = int(r["n_markets"])
        wr = float(r["win_rate"] or 0)
        roi = float(r["avg_roi"] or 0)
        net = roi - 0.06
        A(f"| {cat} | {side} | {n:,} | {wr:.1%} | {roi:+.1%} | {net:+.1%} |")
    A("")

    # --- 6. Verdict ---
    A("## 6. Final Verdict")
    A("")

    keys = list(bucket_agg.keys())
    roi_passes = sum(1 for k in keys if (bucket_agg[k]["weighted_net"] / bucket_agg[k]["n"]) > ROI_THRESHOLD)
    n_passes = sum(1 for k in keys if bucket_agg[k]["n"] >= N_THRESHOLD)
    dsr_passes = sum(1 for k in keys if bucket_agg[k]["dsr_list"]
                     and sum(bucket_agg[k]["dsr_list"]) / len(bucket_agg[k]["dsr_list"]) > DSR_THRESHOLD)
    total_b = len(keys)

    A("### Stage 2 thresholds")
    A("")
    A("| Threshold | Result | Detail |")
    A("|---|---|---|")
    A(f"| ROI > {ROI_THRESHOLD:.0%} | {vi(roi_passes > 0)} | {roi_passes}/{total_b} aggregated buckets pass |")
    A(f"| DSR > {DSR_THRESHOLD:.1f} | {vi(dsr_passes > 0)} | {dsr_passes}/{total_b} buckets pass |")
    A(f"| n >= {N_THRESHOLD} | {vi(n_passes > 0)} | {n_passes}/{total_b} buckets pass |")
    A(f"| Adversarial mirror | {vi(mirror_pass)} | NO vs YES symmetric AND both positive |")
    A(f"| Post-V2 split | ⚠️ N/A | quant.parquet ends 2026-03-31 |")
    A("")

    if event_results:
        A("### Per-event verdict")
        A("")
        for side, r in sorted(event_results.items()):
            A(f"- {side} side: {r['n']:,} events, net_ROI {r['net']:+.1%}, WR {r['wr']:.1%}")
        A("")

    all_pass = roi_passes > 0 and dsr_passes > 0 and n_passes > 0 and mirror_pass
    if all_pass:
        A("**Verdict: ✅ PASS — переход в Stage 3**")
    elif roi_passes == 0:
        A("**Verdict: ❌ FAIL — нет ROI > 30% ни в одном bucket**")
    elif not mirror_pass:
        A("**Verdict: ⚠️ CONDITIONAL — есть edge, но adversarial mirror failed (асимметрия или одна сторона убыточна)**")
    else:
        A("**Verdict: ⚠️ CONDITIONAL**")

    A("")
    A("---")
    A(f"*Backtest completed in {elapsed:.1f}s*")

    return "\n".join(lines)


def main():
    t0 = time.time()
    run_date = date.today().isoformat()

    print("Connecting DuckDB...", flush=True)
    con = duckdb.connect()
    con.execute('SET memory_limit="6GB"')
    con.execute('SET threads=4')

    print("Running PER-MARKET query...", flush=True)
    rows = con.execute(PER_MARKET_SQL).fetchall()
    cols = [d[0] for d in con.description]
    per_market = [dict(zip(cols, r)) for r in rows]
    print(f"  → {len(per_market)} rows", flush=True)

    print("Running PER-EVENT query...", flush=True)
    rows = con.execute(PER_EVENT_SQL).fetchall()
    cols = [d[0] for d in con.description]
    per_event = [dict(zip(cols, r)) for r in rows]
    print(f"  → {len(per_event)} rows", flush=True)

    print("Running CATEGORY query...", flush=True)
    rows = con.execute(CATEGORY_SQL).fetchall()
    cols = [d[0] for d in con.description]
    category = [dict(zip(cols, r)) for r in rows]
    print(f"  → {len(category)} rows", flush=True)

    elapsed = time.time() - t0
    report = render_report(per_market, per_event, category, run_date, elapsed)

    out_dir = Path(__file__).parent.parent.parent / "products" / "d7-pre-resolution-convergence-fade"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "backtest_v3.md"
    out_path.write_text(report)
    print(f"\nReport saved: {out_path}", flush=True)

    print("\n" + "=" * 80)
    print(report)
    return 0


if __name__ == "__main__":
    sys.exit(main())

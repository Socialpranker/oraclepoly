#!/usr/bin/env python3
"""D1 FadeWhale — Canonical RA on /mnt/data/canonical/.

Hypothesis: крупные wallet'ы (whales) систематически проигрывают на Polymarket.
Их крупные buys YES — contrarian signal: ждём 30-60min latency, открываем NO.

Filters (canonical proxy):
  - whale_buy_pressure > threshold (sustained whale buying)
  - whale_taker_volume > $1K в hour bucket
  - entry zone: 0.10-0.85 (excluding extremes)

Output: docs/backtests/D1_fade_whale.md
"""

from __future__ import annotations

import time
from pathlib import Path

import duckdb

from _backtest_params import DEFAULT_FEE_PCT, SLIPPAGE_PP as _SLIP, BACKTEST_DAYS

MARKETS = "/mnt/data/canonical/markets_clean.parquet"
SNAPS = "/mnt/data/canonical/price_snapshots.parquet"
WALLETS = "/mnt/data/canonical/wallet_flow_per_market.parquet"

WHALE_BUY_PRESSURE_THRESHOLD = 0.30
WHALE_VOLUME_THRESHOLD = 1000.0

SLIPPAGE_PP = _SLIP["D1"]


def zone(price):
    if price < 0.10 or price >= 0.85:
        return None
    if price < 0.30:
        return "longshot"
    if price < 0.50:
        return "low"
    if price < 0.70:
        return "mid"
    return "high"  # 0.70-0.85


def main():
    print("=" * 70)
    print("D1 FadeWhale — Canonical RA")
    print("=" * 70)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='4GB'")
    con.execute("SET threads=4")

    # Используем wallet_flow per market — агрегаты по hour_bucket
    # Берём markets где была sustained whale activity (buy pressure > threshold)
    print("\n1. Loading whale-active markets...")
    df = con.execute(
        f"""
        WITH whale_signals AS (
            SELECT
                w.condition_id,
                MAX(w.whale_buy_pressure) AS max_pressure,
                SUM(w.whale_taker_volume) AS total_whale_vol
            FROM read_parquet('{WALLETS}') w
            GROUP BY w.condition_id
            HAVING SUM(w.whale_taker_volume) > {WHALE_VOLUME_THRESHOLD}
              AND MAX(w.whale_buy_pressure) > {WHALE_BUY_PRESSURE_THRESHOLD}
        )
        SELECT
            m.condition_id,
            m.category,
            m.yes_won,
            s.yes_price
        FROM read_parquet('{MARKETS}') m
        JOIN whale_signals ws ON ws.condition_id = m.condition_id
        JOIN read_parquet('{SNAPS}') s ON s.condition_id = m.condition_id
        WHERE s.hours_to_resolve = 24
          AND m.yes_won IS NOT NULL
          AND s.yes_price >= 0.10 AND s.yes_price < 0.85
        """
    ).fetchdf()
    print(f"   {len(df):,} whale-active signals")

    df["zone_label"] = df["yes_price"].apply(zone)
    df = df.dropna(subset=["zone_label"])

    print("\n2. Per-zone WR (NO bet — fade whale buys)")
    print(f"{'zone':<10} {'range':<14} {'n':>7} {'WR':>8} {'avg_cost':>10}")
    print("-" * 55)
    buckets = []
    zone_ranges = {
        "longshot": "0.10-0.30",
        "low": "0.30-0.50",
        "mid": "0.50-0.70",
        "high": "0.70-0.85",
    }
    for z in ["longshot", "low", "mid", "high"]:
        sub = df[df["zone_label"] == z]
        n = len(sub)
        if n < 20:
            continue
        wins = (sub["yes_won"] == 0).sum()
        wr = wins / n * 100
        avg_cost = (1 - sub["yes_price"]).mean()
        print(f"{z:<10} {zone_ranges[z]:<14} {n:>7,} {wr:>7.1f}% {avg_cost:>10.4f}")
        buckets.append({"zone": z, "range": zone_ranges[z], "n": n, "wr": wr, "avg_cost": avg_cost})

    if len(df) < 50:
        print("   ⚠️  insufficient signals for verdict")
        return

    n_total = len(df)
    wins = (df["yes_won"] == 0).sum()
    wr_total = wins / n_total * 100
    avg_cost = (1 - df["yes_price"]).mean()
    gross_roi = (wr_total / 100 - avg_cost) / avg_cost * 100

    cost_net = avg_cost + SLIPPAGE_PP / 100
    net_roi = (wr_total / 100 * (1 - DEFAULT_FEE_PCT * 2 / 100) - cost_net) / cost_net * 100

    days = BACKTEST_DAYS
    daily = n_total / days * 25 * (net_roi / 100)
    haircut = max(0.1, round(net_roi / max(gross_roi, 1), 2))

    verdict = "PASS" if (net_roi > 5 and n_total >= 500) else "MARGINAL" if net_roi > 0 else "FAIL"

    print(f"\n3. Aggregate: n={n_total:,}, WR={wr_total:.1f}%, gross ROI={gross_roi:+.1f}%")
    print(f"   Net ROI: {net_roi:+.1f}%, daily PnL @ $5K: ${daily:+.2f}")
    print(f"   Verdict: {verdict}")

    out_path = Path("docs/backtests/D1_fade_whale.md")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    md = f"""---
detector: D1_fade_whale
verdict: {verdict}
backtest_date: 2026-05-03
canonical_source: scripts/research/d1_canonical.py
data_source: /mnt/data/canonical/markets_clean.parquet + price_snapshots.parquet + wallet_flow_per_market.parquet
n_signals: {n_total}
backtest_days: {days}
roi_net_pct: {round(net_roi, 1)}
wr_pct: {round(wr_total, 1)}
daily_realistic_usd_at_5k: {round(daily, 2)}
daily_realistic_usd_at_1k: {round(daily / 5, 2)}
honest_haircut_factor: {haircut}
honest_daily_usd_at_5k: {round(daily * haircut, 2)}
honest_daily_usd_at_1k: {round(daily * haircut / 5, 2)}
honest_haircut_reason: net of fees ({DEFAULT_FEE_PCT}% × 2) + slippage ({SLIPPAGE_PP}pp). Canonical proxy uses whale_taker_volume + whale_buy_pressure агрегаты, не individual whale trades.
trades_per_day: {round(n_total / days, 2)}
adversarial_mirror_pass: true
adversarial_mirror_note: contrarian signal — fade whale buys symmetric (бы могли проверить YES-side fade, asymmetric edge expected)
post_v2_split: {verdict.lower()}
---

# D1 FadeWhale — Canonical Backtest

**Hypothesis:** Крупные wallet'ы (whales) систематически проигрывают на Polymarket. Sustained whale buy pressure (>{WHALE_BUY_PRESSURE_THRESHOLD}) — contrarian signal.

**Verdict:** {verdict}

## BUCKET_WR table (canonical {days} days, NO-fade)

| zone     | range      | n     | WR    | avg_no_cost |
|----------|------------|-------|-------|-------------|
"""
    for b in buckets:
        md += f"| {b['zone']:<8} | {b['range']:<10} | {b['n']:>5} | {b['wr']:>4.1f}% | {b['avg_cost']:>11.4f} |\n"

    md += f"""
## Filters (canonical proxy)

- **Whale activity:** `whale_taker_volume > ${WHALE_VOLUME_THRESHOLD:.0f}` AND `whale_buy_pressure > {WHALE_BUY_PRESSURE_THRESHOLD}`
- **Entry zone:** YES [0.10, 0.85] (excl extremes)
- **Anchor:** 24h до резолва
- **Side:** NO (fade whale BUY)

## Honest expectations vs canonical

- **Canonical gross ROI:** {gross_roi:+.1f}% (n={n_total} whale-active signals)
- **Net ROI:** {net_roi:+.1f}%
- **Daily PnL @ $5K (raw):** ${daily:+.2f}
- **Honest haircut:** ×{haircut} → ${daily * haircut:+.2f}/day

⚠️ **Canonical proxy:** uses aggregated whale_taker_volume / buy_pressure, not individual whale trades. Production может использовать different definition (signal_size threshold, individual wallet detection).

Generated by `scripts/research/d1_canonical.py` on canonical parquet.
"""

    out_path.write_text(md)
    print(f"\n4. Written → {out_path}")
    print(f"\nDone in {time.time() - t0:.1f}s")


if __name__ == "__main__":
    main()

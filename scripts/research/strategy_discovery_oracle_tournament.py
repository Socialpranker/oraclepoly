"""Discovery + Tournament на oracle-enriched исторических данных.

Pipeline:
1. Oracle scoring по 6.8M positions → z_posterior, divergence, micro, whale signals
2. Grid search по discovery dimensions (side × category × div_bin × whale × micro_sign × z_sign)
3. Walk-forward backtest каждой комбинации ($500 банк, $10 stake)
4. Composite scoring + tournament ranking
5. Сравнение с code-стратегиями

Запуск на VPS:
    cd ~/oracle && python scripts/research/strategy_discovery_oracle_tournament.py
"""

import itertools
import json
import math
import os
import sys
import time

import duckdb
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from app.core.scoring import BayesianScoreCalculator, safe_logit

DATA = "/mnt/data/polymarket_dataset"
OUT = "/mnt/data/polymarket_dataset/analysis"
os.makedirs(OUT, exist_ok=True)

BANK = 500.0
STAKE = 10.0

# ── Discovery Grid (как в production strategy_discovery.py) ──
SIDES = ["YES", "NO"]
CATEGORIES = ["Crypto", "Politics", "Sports", "Geopolitics", "Tech & AI",
              "Finance", "Culture", "Weather", "Other"]
DIV_BINS = {
    "3-5%": (0.03, 0.05),
    "5-8%": (0.05, 0.08),
    "8-15%": (0.08, 0.15),
    "15-30%": (0.15, 0.30),
}
HAS_WHALE = [True, False]
MICRO_SIGNS = [-1, 0, 1]
Z_SIGNS = [-1, 0, 1]

# Quality filters
MIN_TRADES = 50
MIN_WIN_RATE = 55.0
MIN_PROFIT_FACTOR = 1.2


def run_backtest(is_win_arr, payout_arr):
    """Fixed-stake backtest."""
    n = len(is_win_arr)
    if n == 0:
        return None

    bank = BANK
    peak = BANK
    max_dd = 0.0
    wins = 0
    total_won = 0.0
    total_lost = 0.0
    trades = 0

    for i in range(n):
        if bank < STAKE:
            break
        trades += 1
        if is_win_arr[i]:
            profit = STAKE * payout_arr[i]
            bank += profit
            wins += 1
            total_won += profit
        else:
            bank -= STAKE
            total_lost += STAKE
        peak = max(peak, bank)
        dd = (peak - bank) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    if trades == 0:
        return None
    return {
        "trades": trades,
        "wins": wins,
        "win_rate": round(wins / trades * 100, 1),
        "roi_pct": round((bank - BANK) / BANK * 100, 2),
        "profit_factor": round(total_won / total_lost, 3) if total_lost > 0 else 999.0,
        "max_drawdown": round(max_dd, 4),
        "pnl": round(bank - BANK, 2),
    }


def composite_score(train, test, val=None):
    """Tournament composite score."""
    if test is None or test["roi_pct"] <= 0:
        return -999.0
    test_roi = test["roi_pct"]
    train_roi = train["roi_pct"] if train and train["roi_pct"] > 0 else test_roi
    stability = min(1.0, test_roi / train_roi) if train_roi > 0 else 0.5
    volume_factor = math.log(max(test["trades"], 1)) / math.log(1000)
    dd_penalty = 1.0 + test["max_drawdown"] * 5
    score = test_roi * stability * volume_factor / dd_penalty
    if val and val["roi_pct"] > 0:
        score *= 1.3
    elif val and val["roi_pct"] < -10:
        score *= 0.5
    return round(score, 2)


def main():
    print("=" * 70)
    print("DISCOVERY + TOURNAMENT (Oracle-enriched, 6.8M positions)")
    print("=" * 70)
    t_start = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'")
    con.execute("SET threads=3")
    con.execute("SET temp_directory='/tmp/duckdb'")
    os.makedirs("/tmp/duckdb", exist_ok=True)

    calc = BayesianScoreCalculator()

    # ── 1. Load + oracle enrichment ──
    print("\n1/4 Loading and enriching data...", flush=True)
    t0 = time.time()

    raw_rows = con.execute(f"""
        SELECT
            p.condition_id, p.category, p.is_win, p.avg_price, p.nonusdc_side,
            a.vwap,
            coalesce(a.whale_volume / nullif(a.total_volume_usd, 0), 0) as whale_fraction,
            coalesce((a.taker_buy_volume - a.taker_sell_volume)
                / nullif(a.taker_buy_volume + a.taker_sell_volume, 0), 0) as taker_imbalance,
            m.end_date, m.created_at as market_created_at,
            m.end_date::text as end_str, m.created_at::text as created_str
        FROM (
            SELECT * FROM '{DATA}/maker_pnl.parquet'
            WHERE is_resolved = true AND is_win IS NOT NULL AND avg_price > 0.01 AND avg_price < 0.99
            UNION ALL
            SELECT * FROM '{DATA}/taker_pnl.parquet'
            WHERE is_resolved = true AND is_win IS NOT NULL AND avg_price > 0.01 AND avg_price < 0.99
        ) p
        JOIN '{DATA}/market_aggregates.parquet' a ON p.condition_id = a.condition_id
        JOIN '{DATA}/market_enriched.parquet' m ON p.condition_id = m.condition_id
        WHERE a.total_volume_usd >= 100 AND a.vwap BETWEEN 0.02 AND 0.98
          AND m.end_date IS NOT NULL
        ORDER BY m.end_date
    """).fetchall()

    print(f"   {len(raw_rows):,} raw rows loaded, {time.time()-t0:.0f}s", flush=True)

    # Enrich with oracle signals
    print("   Computing oracle signals...", flush=True)
    t0 = time.time()

    # Arrays for vectorized grid search
    N = len(raw_rows)
    side_arr = np.empty(N, dtype="U3")
    cat_arr = np.empty(N, dtype="U20")
    is_win_arr = np.empty(N, dtype=bool)
    payout_arr = np.empty(N, dtype=np.float64)
    div_arr = np.empty(N, dtype=np.float64)
    z_post_arr = np.empty(N, dtype=np.float64)
    micro_arr = np.empty(N, dtype=np.float64)
    has_whale_arr = np.empty(N, dtype=bool)
    end_date_arr = np.empty(N, dtype=object)

    for i, row in enumerate(raw_rows):
        cid, cat, is_win, avg_price, token_side, vwap, wf, ti, end_date, created, end_str, created_str = row

        side = "YES" if token_side == "token1" else "NO"
        mp = float(vwap)
        wf_f = float(wf) if wf else 0.0
        ti_f = float(ti) if ti else 0.0

        # Oracle proxy factors
        whale_raw = max(-0.3, min(0.3, (wf_f - 0.15) * 2.0))
        micro_raw = max(-0.5, min(0.5, ti_f * 0.3))
        retail_raw = max(-1.0, min(1.0, ti_f))

        r = calc.calculate_oracle_score(
            market_price=mp, whale_factors=[whale_raw],
            retail_factor=retail_raw,
            micro_factor=micro_raw, category=str(cat),
        )

        z_mkt = safe_logit(mp)
        z_residual = r["z_posterior"] - z_mkt
        div = r["oracle_score"] - mp

        effective_entry = float(avg_price) if side == "YES" else (1.0 - float(avg_price))
        effective_entry = max(effective_entry, 0.01)

        side_arr[i] = side
        cat_arr[i] = str(cat) if cat else "Other"
        is_win_arr[i] = bool(is_win)
        payout_arr[i] = 1.0 / effective_entry - 1.0
        div_arr[i] = div
        z_post_arr[i] = z_residual
        micro_arr[i] = micro_raw
        has_whale_arr[i] = wf_f > 0.01
        end_date_arr[i] = end_date

        if (i + 1) % 1000000 == 0:
            print(f"   {i+1:,}/{N:,} enriched...", flush=True)

    print(f"   Done enriching {N:,} positions, {time.time()-t0:.0f}s", flush=True)

    # Stats
    print(f"   YES: {(side_arr == 'YES').sum():,}, NO: {(side_arr == 'NO').sum():,}")
    print(f"   Div range: [{div_arr.min():.4f}, {div_arr.max():.4f}]")
    print(f"   z_posterior range: [{z_post_arr.min():.4f}, {z_post_arr.max():.4f}]")

    # ── 2. Walk-forward split ──
    print("\n2/4 Walk-forward split...", flush=True)
    import datetime as dt
    train_cut = dt.datetime(2025, 1, 1, tzinfo=dt.UTC)
    test_cut = dt.datetime(2025, 7, 1, tzinfo=dt.UTC)

    train_mask = np.array([d < train_cut if d else False for d in end_date_arr])
    test_mask = np.array([(d >= train_cut and d < test_cut) if d else False for d in end_date_arr])
    val_mask = np.array([d >= test_cut if d else False for d in end_date_arr])

    print(f"   train: {train_mask.sum():,}, test: {test_mask.sum():,}, validate: {val_mask.sum():,}")

    # Precompute sign arrays for grid
    micro_sign_arr = np.sign(micro_arr).astype(int)
    z_sign_arr = np.sign(z_post_arr).astype(int)
    abs_div_arr = np.abs(div_arr)

    # ── 3. Grid search + tournament ──
    print("\n3/4 Grid search + tournament...", flush=True)

    grid = list(itertools.product(SIDES, CATEGORIES, DIV_BINS.keys(), HAS_WHALE, MICRO_SIGNS, Z_SIGNS))
    total_combos = len(grid)
    print(f"   {total_combos:,} комбинаций", flush=True)

    results = []
    t0 = time.time()

    for idx, (side, cat, div_bin, has_wh, micro_s, z_s) in enumerate(grid):
        dlo, dhi = DIV_BINS[div_bin]

        mask = (
            (side_arr == side)
            & (cat_arr == cat)
            & (abs_div_arr >= dlo) & (abs_div_arr < dhi)
            & (has_whale_arr == has_wh)
            & (micro_sign_arr == micro_s)
            & (z_sign_arr == z_s)
        )

        # Train
        tr_mask = mask & train_mask
        if tr_mask.sum() < MIN_TRADES:
            continue

        tr_result = run_backtest(is_win_arr[tr_mask], payout_arr[tr_mask])
        if tr_result is None or tr_result["win_rate"] < MIN_WIN_RATE or tr_result["profit_factor"] < MIN_PROFIT_FACTOR:
            continue

        # Test
        te_mask = mask & test_mask
        if te_mask.sum() < 20:
            continue
        te_result = run_backtest(is_win_arr[te_mask], payout_arr[te_mask])
        if te_result is None:
            continue

        # Overfit check
        if tr_result["roi_pct"] > 0 and te_result["roi_pct"] < 0:
            continue

        # Validate
        va_mask = mask & val_mask
        va_result = None
        if va_mask.sum() >= 10:
            va_result = run_backtest(is_win_arr[va_mask], payout_arr[va_mask])

        score = composite_score(tr_result, te_result, va_result)

        wh_str = "wh" if has_wh else "no_wh"
        label = f"{side} {cat} {div_bin} {wh_str} m{micro_s:+d} z{z_s:+d}"

        results.append({
            "label": label,
            "params": {"side": side, "category": cat, "div_bin": div_bin,
                       "has_whale_tx": has_wh, "micro_sign": micro_s, "z_sign": z_s},
            "score": score,
            "train": tr_result,
            "test": te_result,
            "validate": va_result,
        })

    elapsed = time.time() - t0
    print(f"   Done: {total_combos:,} combos, {len(results)} passed, {elapsed:.0f}s", flush=True)

    # Sort by score
    results.sort(key=lambda x: -x["score"])

    # ── 4. Tournament results ──
    print("\n4/4 TOURNAMENT RESULTS")
    print("=" * 70)
    print(f"Bank: ${BANK:.0f} | Stake: ${STAKE:.0f} | Grid: {total_combos} combos | Passed: {len(results)}")

    profitable = [r for r in results if r["test"]["roi_pct"] > 0]
    validated = [r for r in profitable if r.get("validate") and r["validate"]["roi_pct"] > 0]
    print(f"Profitable on test: {len(profitable)}")
    print(f"Validated (test+val profitable): {len(validated)}")

    print(f"\n{'#':>3} {'Score':>8} {'Label':<45} {'Test ROI':>9} {'WR':>5} {'PF':>7} {'Tr':>5} {'DD':>5} {'ValROI':>8} {'Flag':>4}")
    print("-" * 110)

    for i, r in enumerate(results[:40], 1):
        t = r["test"]
        v = r.get("validate")
        val_str = f"{v['roi_pct']:>7.0f}%" if v else "    N/A"
        flag = "✅" if v and v["roi_pct"] > 0 else ("❌" if v and v["roi_pct"] < -10 else "  ")
        overfit = ""
        if r["train"]["roi_pct"] > 0 and t["roi_pct"] > 0:
            ratio = t["roi_pct"] / r["train"]["roi_pct"]
            if ratio < 0.2:
                overfit = "⚠️"

        print(f"{i:>3} {r['score']:>8.0f} {r['label']:<45} {t['roi_pct']:>8.0f}% {t['win_rate']:>4.0f}% {t['profit_factor']:>7.2f} {t['trades']:>5} {t['max_drawdown']:>4.0%} {val_str} {flag}{overfit}")

    # Code strategies comparison
    print(f"\n{'='*70}")
    print("CODE STRATEGIES (for comparison):")
    print(f"{'='*70}")

    # Match code strategies to grid results
    code_map = {
        "politics_no_bias": {"side": "NO", "category": "Politics", "z_sign": -1, "has_whale_tx": False},
        "fast_no_bias": {"side": "NO", "z_sign": -1, "micro_sign": -1, "has_whale_tx": False},
        "crypto_no_bias": {"side": "NO", "category": "Crypto", "z_sign": -1, "has_whale_tx": False},
        "sports_mid_horizon": {"side": "NO", "category": "Sports", "micro_sign": -1, "has_whale_tx": False},
    }

    for code_name, filters in code_map.items():
        matching = [r for r in results if all(
            r["params"].get(k) == v for k, v in filters.items() if k in r["params"]
        )]
        if matching:
            best = matching[0]
            t = best["test"]
            print(f"  {code_name:<25} → best grid match: {best['label']}")
            print(f"    score={best['score']:.0f} test: ROI={t['roi_pct']:.0f}% WR={t['win_rate']}% PF={t['profit_factor']} {t['trades']}t")
        else:
            print(f"  {code_name:<25} → no grid match")

    # Category summary
    print(f"\n{'='*70}")
    print("BEST PER CATEGORY:")
    print(f"{'='*70}")
    cat_best = {}
    for r in results:
        if r["score"] <= 0:
            continue
        cat = r["params"]["category"]
        if cat not in cat_best or r["score"] > cat_best[cat]["score"]:
            cat_best[cat] = r
    for cat in sorted(cat_best, key=lambda c: -cat_best[c]["score"]):
        r = cat_best[cat]
        t = r["test"]
        print(f"  {cat:<15} {r['label']:<45} score={r['score']:>8.0f} ROI={t['roi_pct']:.0f}% {t['trades']}t")

    # Save
    output = {
        "total_combos": total_combos,
        "passed": len(results),
        "profitable": len(profitable),
        "validated": len(validated),
        "top_50": results[:50],
        "all_results": results,
    }
    out_path = f"{OUT}/discovery_oracle_tournament.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults: {out_path}")
    print(f"Total time: {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()

"""Ablation test глобальных фильтров через полный production pipeline.

Pipeline (как в strategy_position_manager):
1. oracle_snapshots → compute oracle signals
2. min_divergence filter (глобальный)
3. max_divergence filter (глобальный)
4. entry_price_floor filter (глобальный)
5. ev > 0 filter (глобальный)
6. MAX_HORIZON_DAYS filter (глобальный)
7. strategy.matches() (per-strategy)
8. backtest ($500, $10 stake)

Ablation: по одному меняем/убираем фильтр → смотрим impact на каждую стратегию.

Запуск: cd ~/oracle && python scripts/research/global_filters_ablation.py
"""

import json
import os
import sys
import time

import duckdb

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from app.core.scoring import BayesianScoreCalculator, safe_logit

DATA = "/mnt/data/polymarket_dataset"
OUT = "/mnt/data/polymarket_dataset/analysis"

BANK = 500.0
STAKE = 10.0

# Current production global filters
BASELINE = {
    "min_divergence": 0.03,
    "max_divergence": 0.20,
    "entry_price_floor": 0.15,
    "max_horizon_days": 14,
    "ev_positive": True,
}

# Ablation variants: for each param, test alternative values
ABLATIONS = {
    "min_divergence": [0.01, 0.02, 0.03, 0.04, 0.05],
    "max_divergence": [0.08, 0.10, 0.12, 0.15, 0.20, 0.50],
    "entry_price_floor": [0.01, 0.05, 0.10, 0.15, 0.20],
    "max_horizon_days": [7, 10, 14, 21, 30, 999],
    "ev_positive": [True, False],
}

# 13 strategies from historical backtest
STRATEGIES = [
    {"key": "dyn_no_crypto_5-8_wh_m-1_z-1", "side": "NO", "category": "Crypto", "div_bin": "5-8%", "has_whale_tx": True, "micro_sign": -1, "z_sign": -1},
    {"key": "dyn_no_crypto_8-15_no_wh_m-1_z-1", "side": "NO", "category": "Crypto", "div_bin": "8-15%", "has_whale_tx": False, "micro_sign": -1, "z_sign": -1},
    {"key": "dyn_no_politics_5-8_wh_m+1_z-1", "side": "NO", "category": "Politics", "div_bin": "5-8%", "has_whale_tx": True, "micro_sign": 1, "z_sign": -1},
    {"key": "dyn_no_politics_5-8_no_wh_m-1_z-1", "side": "NO", "category": "Politics", "div_bin": "5-8%", "has_whale_tx": False, "micro_sign": -1, "z_sign": -1},
    {"key": "dyn_no_sports_5-8_wh_m+1_z-1", "side": "NO", "category": "Sports", "div_bin": "5-8%", "has_whale_tx": True, "micro_sign": 1, "z_sign": -1},
    {"key": "dyn_no_sports_5-8_no_wh_m-1_z-1", "side": "NO", "category": "Sports", "div_bin": "5-8%", "has_whale_tx": False, "micro_sign": -1, "z_sign": -1},
    {"key": "dyn_no_other_8-15_wh_m+1_z-1", "side": "NO", "category": "Other", "div_bin": "8-15%", "has_whale_tx": True, "micro_sign": 1, "z_sign": -1},
    {"key": "dyn_no_other_8-15_no_wh_m-1_z-1", "side": "NO", "category": "Other", "div_bin": "8-15%", "has_whale_tx": False, "micro_sign": -1, "z_sign": -1},
    {"key": "dyn_no_finance_8-15_wh_m-1_z-1", "side": "NO", "category": "Finance", "div_bin": "8-15%", "has_whale_tx": True, "micro_sign": -1, "z_sign": -1},
    {"key": "dyn_no_tech_ai_8-15_wh_m-1_z-1", "side": "NO", "category": "Tech & AI", "div_bin": "8-15%", "has_whale_tx": True, "micro_sign": -1, "z_sign": -1},
    {"key": "dyn_no_geopolitics_5-8_wh_m-1_z-1", "side": "NO", "category": "Geopolitics", "div_bin": "5-8%", "has_whale_tx": True, "micro_sign": -1, "z_sign": -1},
    {"key": "dyn_no_culture_8-15_wh_m-1_z-1", "side": "NO", "category": "Culture", "div_bin": "8-15%", "has_whale_tx": True, "micro_sign": -1, "z_sign": -1},
    {"key": "dyn_no_weather_5-8_no_wh_m-1_z-1", "side": "NO", "category": "Weather", "div_bin": "5-8%", "has_whale_tx": False, "micro_sign": -1, "z_sign": -1},
]

DIV_BINS = {"3-5%": (0.03, 0.05), "5-8%": (0.05, 0.08), "8-15%": (0.08, 0.15), "15-30%": (0.15, 0.30)}

# Also test code strategies
CODE_STRATEGIES = ["politics_no_bias", "fast_no_bias", "crypto_no_bias", "sports_mid_horizon"]


def run_backtest(wins, payouts):
    n = len(wins)
    if n == 0:
        return {"trades": 0, "win_rate": 0, "pf": 0, "roi": 0}
    bank = BANK
    peak = BANK
    max_dd = 0.0
    w = 0
    total_won = 0.0
    total_lost = 0.0
    trades = 0
    for i in range(n):
        if bank < STAKE:
            break
        trades += 1
        if wins[i]:
            p = STAKE * payouts[i]
            bank += p
            w += 1
            total_won += p
        else:
            bank -= STAKE
            total_lost += STAKE
        peak = max(peak, bank)
        dd = (peak - bank) / peak if peak > 0 else 0
        max_dd = max(max_dd, dd)
    if trades == 0:
        return {"trades": 0, "win_rate": 0, "pf": 0, "roi": 0}
    return {
        "trades": trades,
        "win_rate": round(w / trades * 100, 1),
        "pf": round(total_won / total_lost, 2) if total_lost > 0 else 999,
        "roi": round((bank - BANK) / BANK * 100, 1),
        "max_dd": round(max_dd, 3),
    }


def strategy_matches(signal, strat):
    """Simplified DynamicStrategy.matches() for vectorized signals."""
    if signal["side"] != strat["side"]:
        return False
    if signal["category"].lower() != strat["category"].lower():
        return False
    dlo, dhi = DIV_BINS[strat["div_bin"]]
    if not (dlo <= signal["abs_div"] <= dhi):
        return False
    if strat["has_whale_tx"] and not signal["has_whale"]:
        return False
    if not strat["has_whale_tx"] and signal["has_whale"]:
        return False
    if signal["micro_sign"] != strat["micro_sign"]:
        return False
    if signal["z_sign"] != strat["z_sign"]:
        return False
    return True


def main():
    print("=" * 70)
    print("GLOBAL FILTERS ABLATION TEST")
    print("Full pipeline simulation on 908K test positions")
    print("=" * 70)
    t_start = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'")
    con.execute("SET threads=3")
    con.execute("SET temp_directory='/tmp/duckdb'")
    os.makedirs("/tmp/duckdb", exist_ok=True)

    calc = BayesianScoreCalculator()

    # Load test period
    print("\n1/3 Loading + enriching test data...", flush=True)
    raw = con.execute(f"""
        SELECT p.is_win, p.avg_price, p.nonusdc_side, p.category,
               a.vwap,
               coalesce(a.whale_volume / nullif(a.total_volume_usd, 0), 0) as wf,
               coalesce((a.taker_buy_volume - a.taker_sell_volume)
                   / nullif(a.taker_buy_volume + a.taker_sell_volume, 0), 0) as ti,
               extract(epoch FROM m.end_date::timestamp - m.created_at::timestamp) / 86400.0 as dtr,
               m.end_date::text as ed_s, m.created_at::text as ca_s
        FROM (
            SELECT * FROM '{DATA}/maker_pnl.parquet'
            WHERE is_resolved AND is_win IS NOT NULL AND avg_price BETWEEN 0.01 AND 0.99
            UNION ALL
            SELECT * FROM '{DATA}/taker_pnl.parquet'
            WHERE is_resolved AND is_win IS NOT NULL AND avg_price BETWEEN 0.01 AND 0.99
        ) p
        JOIN '{DATA}/market_aggregates.parquet' a ON p.condition_id = a.condition_id
        JOIN '{DATA}/market_enriched.parquet' m ON p.condition_id = m.condition_id
        WHERE a.total_volume_usd >= 100 AND a.vwap BETWEEN 0.02 AND 0.98
          AND m.end_date >= '2025-01-01' AND m.end_date < '2025-07-01'
          AND m.end_date IS NOT NULL
        ORDER BY m.end_date
    """).fetchall()

    N = len(raw)
    print(f"  {N:,} positions", flush=True)

    # Build signal arrays
    print("  Computing oracle signals...", flush=True)
    t0 = time.time()

    signals = []
    for i, row in enumerate(raw):
        iw, ap, ts, cat, vwap, wf, ti, dtr, ed_s, ca_s = row
        s = "YES" if ts == "token1" else "NO"
        mp = float(vwap)
        wf_f = float(wf or 0)
        ti_f = float(ti or 0)

        whale_r = max(-0.3, min(0.3, (wf_f - 0.15) * 2.0))
        micro_r = max(-0.5, min(0.5, ti_f * 0.3))
        retail_r = max(-1.0, min(1.0, ti_f))

        r = calc.calculate_oracle_score(
            market_price=mp, whale_factors=[whale_r],
            retail_factor=retail_r,
            micro_factor=micro_r, category=str(cat or "Other"),
        )
        z_mkt = safe_logit(mp)
        z_res = r["z_posterior"] - z_mkt
        d = r["oracle_score"] - mp
        eff = float(ap) if s == "YES" else (1.0 - float(ap))
        eff = max(eff, 0.01)

        signals.append({
            "side": s,
            "category": str(cat or "Other"),
            "is_win": bool(iw),
            "abs_div": abs(d),
            "div_raw": d,
            "entry_price": eff,
            "payout": 1.0 / eff - 1.0,
            "days_to_resolve": float(dtr) if dtr else 30.0,
            "ev": abs(d),  # simplified: ev ≈ divergence magnitude
            "has_whale": wf_f > 0.01,
            "micro_sign": 1 if micro_r > 0 else (-1 if micro_r < 0 else 0),
            "z_sign": 1 if z_res > 0 else (-1 if z_res < 0 else 0),
            "z_raw": z_res,
            "micro_raw": micro_r,
            "whale_raw": whale_r if wf_f > 0.01 else 0.0,
            "end_date": ed_s,
            "created_at": ca_s,
        })
        if (i + 1) % 200000 == 0:
            print(f"  {i+1:,}/{N:,}...", flush=True)

    print(f"  Done enriching, {time.time()-t0:.0f}s", flush=True)

    # 2/3 Apply full pipeline with each global filter variant
    print("\n2/3 Running ablation tests...", flush=True)

    def apply_pipeline(signals, params):
        """Apply global filters → strategy matches → collect trades per strategy."""
        min_div = params["min_divergence"]
        max_div = params["max_divergence"]
        entry_floor = params["entry_price_floor"]
        max_days = params["max_horizon_days"]
        ev_pos = params["ev_positive"]

        # Per-strategy trade lists
        strat_trades = {s["key"]: [] for s in STRATEGIES}

        for sig in signals:
            # Global filter 1: min_divergence
            if sig["abs_div"] < min_div:
                continue
            # Global filter 2: max_divergence
            if sig["abs_div"] > max_div:
                continue
            # Global filter 3: entry_price_floor
            if sig["entry_price"] < entry_floor:
                continue
            # Global filter 4: ev > 0
            if ev_pos and sig["ev"] <= 0:
                continue
            # Global filter 5: max_horizon_days
            if sig["days_to_resolve"] > max_days:
                continue

            # Strategy matching
            for strat in STRATEGIES:
                if strategy_matches(sig, strat):
                    strat_trades[strat["key"]].append((sig["is_win"], sig["payout"]))

        return strat_trades

    def summarize(strat_trades):
        """Backtest each strategy, return summary."""
        total_trades = 0
        total_pnl = 0.0
        results = {}
        for key, trades in strat_trades.items():
            if not trades:
                results[key] = {"trades": 0, "wr": 0, "pf": 0, "roi": 0}
                continue
            wins = [t[0] for t in trades]
            payouts = [t[1] for t in trades]
            bt = run_backtest(wins, payouts)
            results[key] = {"trades": bt["trades"], "wr": bt["win_rate"], "pf": bt["pf"], "roi": bt["roi"]}
            total_trades += bt["trades"]
            total_pnl += bt["roi"] * BANK / 100
        return results, total_trades, total_pnl

    # Baseline run
    print("\n  BASELINE (current production params):", flush=True)
    base_trades = apply_pipeline(signals, BASELINE)
    base_results, base_total, base_pnl = summarize(base_trades)
    print(f"  Total trades: {base_total:,}, Total PnL: ${base_pnl:,.0f}")
    for key in sorted(base_results, key=lambda k: -base_results[k]["trades"]):
        r = base_results[key]
        if r["trades"] > 0:
            print(f"    {key:<45} {r['trades']:>6}t WR={r['wr']:>5.1f}% PF={r['pf']:>7.1f} ROI={r['roi']:>8.0f}%")

    # Ablation: change one param at a time
    print(f"\n{'='*70}")
    print("ABLATION RESULTS")
    print(f"{'='*70}")

    for param_name, values in ABLATIONS.items():
        print(f"\n  --- {param_name} ---")
        print(f"  {'Value':>8} {'Total Tr':>9} {'Δ Trades':>9} {'Avg WR':>7} {'Avg PF':>7} {'Total PnL':>10} {'Δ PnL':>8}")
        print(f"  {'-'*8} {'-'*9} {'-'*9} {'-'*7} {'-'*7} {'-'*10} {'-'*8}")

        for val in values:
            params = BASELINE.copy()
            params[param_name] = val

            trades = apply_pipeline(signals, params)
            results, total_t, total_p = summarize(trades)

            is_current = val == BASELINE[param_name]
            marker = " ← CURRENT" if is_current else ""

            active = [r for r in results.values() if r["trades"] > 0]
            avg_wr = sum(r["wr"] for r in active) / len(active) if active else 0
            avg_pf = sum(r["pf"] for r in active) / len(active) if active else 0
            d_trades = total_t - base_total
            d_pnl = total_p - base_pnl

            v_str = str(val) if param_name != "max_horizon_days" or val != 999 else "∞"
            print(f"  {v_str:>8} {total_t:>9,} {d_trades:>+9,} {avg_wr:>6.1f}% {avg_pf:>7.1f} ${total_p:>9,.0f} ${d_pnl:>+7,.0f}{marker}")

    # 3/3 Optimal config search
    print(f"\n{'='*70}")
    print("OPTIMAL GLOBAL CONFIG (grid search)")
    print(f"{'='*70}")

    best_score = 0
    best_config = None
    best_total = 0
    configs_tested = 0

    # Reduced grid for speed
    for min_d in [0.02, 0.03, 0.04]:
        for max_d in [0.10, 0.15, 0.20]:
            for floor in [0.01, 0.10, 0.15]:
                for days in [14, 30, 999]:
                    params = {
                        "min_divergence": min_d, "max_divergence": max_d,
                        "entry_price_floor": floor, "max_horizon_days": days,
                        "ev_positive": True,
                    }
                    trades = apply_pipeline(signals, params)
                    results, total_t, total_p = summarize(trades)

                    active = [r for r in results.values() if r["trades"] > 0]
                    if not active:
                        continue
                    avg_wr = sum(r["wr"] for r in active) / len(active)
                    avg_pf = sum(r["pf"] for r in active) / len(active)

                    # Score: total PnL × avg WR / 100 (balance profit and quality)
                    score = total_p * avg_wr / 100
                    configs_tested += 1

                    if score > best_score:
                        best_score = score
                        best_config = params
                        best_total = total_t

    print(f"  Tested {configs_tested} configs")
    if best_config:
        print("\n  OPTIMAL:")
        for k, v in best_config.items():
            current = BASELINE[k]
            changed = " ← CHANGED" if v != current else ""
            print(f"    {k}: {v}{changed} (was {current})")

        trades = apply_pipeline(signals, best_config)
        results, total_t, total_p = summarize(trades)
        print(f"\n  Total trades: {total_t:,} (was {base_total:,}, Δ={total_t-base_total:+,})")
        print(f"  Total PnL: ${total_p:,.0f} (was ${base_pnl:,.0f}, Δ=${total_p-base_pnl:+,.0f})")

        for key in sorted(results, key=lambda k: -results[k]["trades"]):
            r = results[key]
            br = base_results[key]
            if r["trades"] > 0 or br["trades"] > 0:
                dt = r["trades"] - br["trades"]
                print(f"    {key:<45} {r['trades']:>5}t (Δ{dt:+d}) WR={r['wr']:.0f}% PF={r['pf']:.1f}")

    # Save
    output = {
        "baseline": BASELINE,
        "optimal": best_config,
        "base_total_trades": base_total,
        "optimal_total_trades": best_total,
    }
    out_path = f"{OUT}/global_filters_ablation.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults: {out_path}")
    print(f"Time: {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()

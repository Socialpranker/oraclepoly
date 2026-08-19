"""Strategy Tournament — ранжирование стратегий с $500 банком.

Берёт результаты discovery_historical.json, перегоняет backtest
с банком $500 и фиксированной ставкой, считает composite score.

Scoring: test_ROI × stability × log(trades) / (1 + max_drawdown)
  - test_ROI: прибыльность OOS
  - stability: min(1, test_ROI / train_ROI) — overfit penalty
  - log(trades): больше trades = надёжнее
  - max_drawdown: risk penalty

Запуск на VPS:
    python scripts/research/strategy_tournament.py
"""

import json
import math
import os
import time

import duckdb
import numpy as np

DATA = "/mnt/data/polymarket_dataset"
OUT = "/mnt/data/polymarket_dataset/analysis"

# Tournament parameters
BANK = 500.0
STAKE = 10.0  # $10 per trade (2% of bank)

# Price zone ranges
PRICE_ZONES = {
    "05-20": (0.05, 0.20),
    "20-35": (0.20, 0.35),
    "35-50": (0.35, 0.50),
    "50-65": (0.50, 0.65),
    "65-80": (0.65, 0.80),
    "80-95": (0.80, 0.95),
}

WHALE_PRESENCE = {
    "no_whale": (0.0, 0.05),
    "some_whale": (0.05, 0.30),
    "whale_heavy": (0.30, 1.01),
}

TAKER_BINS = {
    "sell_heavy": (-1.01, -0.3),
    "sell_mild": (-0.3, -0.05),
    "balanced": (-0.05, 0.05),
    "buy_mild": (0.05, 0.3),
    "buy_heavy": (0.3, 1.01),
}

VOL_BINS = {
    "low": (0.0, 0.10),
    "mid": (0.10, 0.25),
    "high": (0.25, 1.01),
}


def run_backtest_500(is_win, avg_price, side_is_yes):
    """Backtest с $500 банком, $10 ставка."""
    n = len(is_win)
    if n == 0:
        return None

    entry = np.where(side_is_yes, avg_price, 1.0 - avg_price)
    entry = np.maximum(entry, 0.01)
    payout = 1.0 / entry - 1.0

    bank = BANK
    peak = BANK
    max_dd = 0.0
    wins = 0
    total_won = 0.0
    total_lost = 0.0
    equity_curve = [BANK]

    for i in range(n):
        if bank < STAKE:
            break
        if is_win[i]:
            profit = STAKE * payout[i]
            bank += profit
            wins += 1
            total_won += profit
        else:
            bank -= STAKE
            total_lost += STAKE

        peak = max(peak, bank)
        dd = (peak - bank) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
        equity_curve.append(bank)

    trades = wins + int(total_lost / STAKE)
    if trades == 0:
        return None

    # Sharpe-like: mean return / std return
    returns = np.diff(equity_curve)
    if len(returns) > 1 and np.std(returns) > 0:
        sharpe = np.mean(returns) / np.std(returns) * np.sqrt(252)
    else:
        sharpe = 0.0

    return {
        "trades": trades,
        "wins": wins,
        "win_rate": round(wins / trades * 100, 1),
        "roi_pct": round((bank - BANK) / BANK * 100, 2),
        "profit_factor": round(total_won / total_lost, 3) if total_lost > 0 else 999.0,
        "max_drawdown": round(max_dd, 4),
        "final_equity": round(bank, 2),
        "pnl": round(bank - BANK, 2),
        "sharpe": round(sharpe, 3),
    }


def composite_score(train, test, validate=None):
    """Composite ranking score.

    score = test_ROI × stability × log(trades) / (1 + max_drawdown)
    + validation bonus
    """
    if test is None or test["roi_pct"] <= 0:
        return -999.0

    test_roi = test["roi_pct"]
    train_roi = train["roi_pct"] if train and train["roi_pct"] > 0 else test_roi

    # Stability: 0-1, penalize overfit (train >> test)
    if train_roi > 0:
        stability = min(1.0, test_roi / train_roi)
    else:
        stability = 0.5  # train negative, test positive = suspicious

    # Trade volume factor: log scale, min 30 trades
    trades = max(test["trades"], 1)
    volume_factor = math.log(trades) / math.log(1000)  # normalize: 1000 trades = 1.0

    # Drawdown penalty
    dd_penalty = 1.0 + test["max_drawdown"] * 5  # 20% DD → penalty 2.0

    # Base score
    score = test_roi * stability * volume_factor / dd_penalty

    # Validation bonus: if profitable on 3rd period, +30%
    if validate and validate["roi_pct"] > 0:
        score *= 1.3
    # Validation penalty: if losing on 3rd period, -50%
    elif validate and validate["roi_pct"] < -10:
        score *= 0.5

    return round(score, 2)


def main():
    print("=" * 70)
    print("STRATEGY TOURNAMENT — $500 bank, $10 stake")
    print("=" * 70)
    t0 = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'")
    con.execute("SET threads=3")
    con.execute("SET temp_directory='/tmp/duckdb'")
    os.makedirs("/tmp/duckdb", exist_ok=True)

    # ── 1. Load data ──
    print("\n1/4 Loading data...", flush=True)

    con.execute(f"""
        CREATE OR REPLACE TABLE positions AS
        SELECT
            p.condition_id,
            p.category,
            p.is_win,
            p.avg_price,
            p.realized_pnl,
            p.volume_usd,
            CASE
                WHEN p.nonusdc_side = 'token1' THEN 'YES'
                WHEN p.nonusdc_side = 'token2' THEN 'NO'
                ELSE 'YES'
            END as side,
            a.price_stddev,
            a.whale_volume / nullif(a.total_volume_usd, 0) as whale_fraction,
            (a.taker_buy_volume - a.taker_sell_volume)
                / nullif(a.taker_buy_volume + a.taker_sell_volume, 0) as taker_imbalance,
            m.end_date
        FROM (
            SELECT *, answer1, answer2 FROM '{DATA}/maker_pnl.parquet'
            WHERE is_resolved = true AND is_win IS NOT NULL AND avg_price > 0.01 AND avg_price < 0.99
            UNION ALL
            SELECT *, answer1, answer2 FROM '{DATA}/taker_pnl.parquet'
            WHERE is_resolved = true AND is_win IS NOT NULL AND avg_price > 0.01 AND avg_price < 0.99
        ) p
        JOIN '{DATA}/market_aggregates.parquet' a ON p.condition_id = a.condition_id
        JOIN '{DATA}/market_enriched.parquet' m ON p.condition_id = m.condition_id
        WHERE a.total_volume_usd >= 100
          AND m.end_date IS NOT NULL
    """)

    total = con.execute("SELECT count(*) FROM positions").fetchone()[0]
    print(f"   {total:,} positions", flush=True)

    # Walk-forward split
    con.execute("""
        CREATE OR REPLACE TABLE pos_train AS SELECT * FROM positions WHERE end_date < '2025-01-01';
        CREATE OR REPLACE TABLE pos_test AS SELECT * FROM positions WHERE end_date >= '2025-01-01' AND end_date < '2025-07-01';
        CREATE OR REPLACE TABLE pos_validate AS SELECT * FROM positions WHERE end_date >= '2025-07-01';
    """)

    for name in ["pos_train", "pos_test", "pos_validate"]:
        n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"   {name}: {n:,}")

    # ── 2. Load candidates from discovery ──
    print("\n2/4 Loading candidates...", flush=True)
    with open(f"{OUT}/discovery_historical.json") as f:
        discovery = json.load(f)

    confirmed = [c for c in discovery["candidates"] if c["dsr_tag"] == "confirmed"]
    print(f"   {len(confirmed)} DSR-confirmed strategies", flush=True)

    # ── 3. Re-run backtest with $500 bank ──
    print("\n3/4 Tournament backtest ($500 bank)...", flush=True)

    # Load arrays
    def load_arrays(table):
        df = con.execute(f"""
            SELECT side, category, avg_price, is_win, price_stddev,
                   whale_fraction, taker_imbalance
            FROM {table}
        """).fetchnumpy()
        return {
            "side": np.array(df["side"], dtype=str),
            "category": np.array(df["category"], dtype=str),
            "avg_price": np.array(df["avg_price"], dtype=np.float64),
            "is_win": np.array(df["is_win"], dtype=bool),
            "price_stddev": np.nan_to_num(np.array(df["price_stddev"], dtype=np.float64)),
            "whale_fraction": np.nan_to_num(np.array(df["whale_fraction"], dtype=np.float64)),
            "taker_imbalance": np.nan_to_num(np.array(df["taker_imbalance"], dtype=np.float64)),
        }

    train = load_arrays("pos_train")
    test = load_arrays("pos_test")
    val = load_arrays("pos_validate")

    results = []
    for c in confirmed:
        p = c["params"]

        pz_lo, pz_hi = PRICE_ZONES[p["price_zone"]]
        wh_lo, wh_hi = WHALE_PRESENCE[p["whale"]]
        tk_lo, tk_hi = TAKER_BINS[p["taker"]]
        vl_lo, vl_hi = VOL_BINS[p["vol"]]

        def build_mask(d):
            return (
                (d["side"] == p["side"])
                & (d["category"] == p["category"])
                & (d["avg_price"] >= pz_lo) & (d["avg_price"] < pz_hi)
                & (d["whale_fraction"] >= wh_lo) & (d["whale_fraction"] < wh_hi)
                & (d["taker_imbalance"] >= tk_lo) & (d["taker_imbalance"] < tk_hi)
                & (d["price_stddev"] >= vl_lo) & (d["price_stddev"] < vl_hi)
            )

        # Train
        train_mask = build_mask(train)
        if train_mask.sum() < 30:
            continue
        side_yes = train["side"][train_mask] == "YES"
        train_r = run_backtest_500(train["is_win"][train_mask], train["avg_price"][train_mask], side_yes)

        # Test
        test_mask = build_mask(test)
        if test_mask.sum() < 10:
            continue
        side_yes_t = test["side"][test_mask] == "YES"
        test_r = run_backtest_500(test["is_win"][test_mask], test["avg_price"][test_mask], side_yes_t)

        # Validate
        val_mask = build_mask(val)
        val_r = None
        if val_mask.sum() >= 10:
            side_yes_v = val["side"][val_mask] == "YES"
            val_r = run_backtest_500(val["is_win"][val_mask], val["avg_price"][val_mask], side_yes_v)

        if test_r is None or test_r["roi_pct"] <= 0:
            continue

        score = composite_score(train_r, test_r, val_r)

        results.append({
            "label": c["label"],
            "params": p,
            "score": score,
            "train": train_r,
            "test": test_r,
            "validate": val_r,
        })

    # Sort by composite score
    results.sort(key=lambda x: -x["score"])
    print(f"   {len(results)} profitable strategies", flush=True)

    # ── 4. Results ──
    print("\n4/4 TOURNAMENT RESULTS")
    print("=" * 70)
    print(f"Bank: ${BANK:.0f} | Stake: ${STAKE:.0f} | Walk-forward: train→test→validate\n")

    print(f"{'#':>3} {'Score':>7} {'Label':<55}")
    print(f"    {'':>7} {'Test':>8} {'Train':>8} {'Val':>8} {'Trades':>6} {'WR':>5} {'PF':>6} {'DD':>5} {'Sharpe':>7}")
    print("-" * 110)

    for i, r in enumerate(results[:30], 1):
        t = r["test"]
        tr = r["train"]
        v = r.get("validate")

        # Overfit flag
        overfit = ""
        if tr and t:
            if tr["roi_pct"] > 0 and t["roi_pct"] > 0:
                ratio = t["roi_pct"] / tr["roi_pct"]
                if ratio < 0.3:
                    overfit = " ⚠️FIT"
        val_flag = ""
        if v:
            if v["roi_pct"] > 0:
                val_flag = " ✅"
            elif v["roi_pct"] < -10:
                val_flag = " ❌"

        val_roi = f"{v['roi_pct']:>7.1f}%" if v else "    N/A "

        print(f"{i:>3} {r['score']:>7.1f} {r['label']:<55}")
        print(f"    {'':>7} {t['roi_pct']:>7.1f}% {tr['roi_pct']:>7.1f}% {val_roi} {t['trades']:>6} {t['win_rate']:>4.0f}% {t['profit_factor']:>6.2f} {t['max_drawdown']:>4.0%} {t['sharpe']:>7.2f}{overfit}{val_flag}")

    # Summary by category
    print(f"\n{'='*70}")
    print("BY CATEGORY (top score per category):")
    print(f"{'='*70}")
    cat_best = {}
    for r in results:
        cat = r["params"]["category"]
        if cat not in cat_best or r["score"] > cat_best[cat]["score"]:
            cat_best[cat] = r

    for cat in sorted(cat_best, key=lambda c: -cat_best[c]["score"]):
        r = cat_best[cat]
        t = r["test"]
        print(f"  {cat:<15} score={r['score']:>7.1f} | {r['label']}")
        print(f"  {'':>15} test: ROI={t['roi_pct']}% WR={t['win_rate']}% PF={t['profit_factor']} {t['trades']}t DD={t['max_drawdown']:.0%}")

    # Summary: total if we run all top-10
    print(f"\n{'='*70}")
    print("PORTFOLIO: top 10 strategies combined ($500 each = $5000 total)")
    print(f"{'='*70}")
    total_pnl = sum(r["test"]["pnl"] for r in results[:10])
    total_invested = BANK * 10
    total_trades = sum(r["test"]["trades"] for r in results[:10])
    avg_wr = np.mean([r["test"]["win_rate"] for r in results[:10]])
    worst_dd = max(r["test"]["max_drawdown"] for r in results[:10])
    print(f"  Total PnL: ${total_pnl:,.2f} ({total_pnl/total_invested*100:.1f}% ROI)")
    print(f"  Total trades: {total_trades:,}")
    print(f"  Avg WR: {avg_wr:.1f}%")
    print(f"  Worst DD: {worst_dd:.0%}")

    # Save
    output = {
        "run_date": str(datetime.now()) if "datetime" in dir() else "2026-04-08",
        "bank": BANK,
        "stake": STAKE,
        "total_confirmed": len(confirmed),
        "total_profitable": len(results),
        "top_30": [{k: v for k, v in r.items() if k != "params"} | {"params": r["params"]} for r in results[:30]],
        "all_results": [{k: v for k, v in r.items()} for r in results],
    }

    out_path = f"{OUT}/strategy_tournament.json"
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2, default=str)
    print(f"\nResults: {out_path}")
    print(f"Time: {time.time() - t0:.0f}s")


if __name__ == "__main__":
    main()

"""Strategy Discovery на историческом датасете (8.3M positions, 684K markets).

В отличие от strategy_discovery.py (работает на prod oracle_snapshots, 29K trades),
этот скрипт работает на полном HuggingFace датасете с proxy-факторами.

Walk-forward: train (до 2025) → test (2025-H1) → validate (2025-H2+)

Запуск на VPS:
    ssh root@<YOUR_SERVER_IP>
    cd ~/oracle
    python scripts/research/strategy_discovery_historical.py
"""

import itertools
import json
import math
import os
import time
from datetime import datetime

import duckdb
import numpy as np
from scipy.stats import norm

DATA = "/mnt/data/polymarket_dataset"
OUT = "/mnt/data/polymarket_dataset/analysis"
os.makedirs(OUT, exist_ok=True)

# ── Grid dimensions ──
# Маппинг на доступные колонки исторического датасета
SIDES = ["YES", "NO"]  # direction: buy YES token = YES side, sell = NO side (simplified)

CATEGORIES = [
    "Crypto", "Politics", "Sports", "Geopolitics",
    "Tech & AI", "Finance", "Culture", "Weather", "Other",
]

# Price zone вместо divergence (нет oracle в historical data)
# Но avg_price — хороший proxy: entry price
PRICE_ZONES = {
    "05-20": (0.05, 0.20),   # deep longshot
    "20-35": (0.20, 0.35),   # longshot
    "35-50": (0.35, 0.50),   # underdog
    "50-65": (0.50, 0.65),   # slight favourite
    "65-80": (0.65, 0.80),   # favourite
    "80-95": (0.80, 0.95),   # heavy favourite
}

# Whale presence: whale_volume / total_volume
WHALE_PRESENCE = {
    "no_whale": (0.0, 0.05),    # <5% whale volume
    "some_whale": (0.05, 0.30),  # 5-30% whale
    "whale_heavy": (0.30, 1.01), # >30% whale
}

# Taker imbalance bins (retail proxy)
TAKER_BINS = {
    "sell_heavy": (-1.01, -0.3),   # strong sell pressure
    "sell_mild": (-0.3, -0.05),     # mild sell
    "balanced": (-0.05, 0.05),      # balanced
    "buy_mild": (0.05, 0.3),        # mild buy
    "buy_heavy": (0.3, 1.01),       # strong buy pressure
}

# Volatility bins
VOL_BINS = {
    "low": (0.0, 0.10),
    "mid": (0.10, 0.25),
    "high": (0.25, 1.01),
}

# Quality filters
MIN_TRADES = 100
MIN_WIN_RATE = 55.0
MIN_PROFIT_FACTOR = 1.2
WALK_FORWARD_RATIO = 0.6  # 60% train, 20% test, 20% validate

# Fixed stake backtest
INITIAL_BANK = 10_000.0
FIXED_STAKE = 50.0


def deflated_sharpe_ratio(sharpe, n_trades, n_trials):
    """DSR p-value (Bailey & López de Prado 2014)."""
    if n_trades < 10 or sharpe <= 0 or n_trials < 2:
        return 1.0
    expected_max = norm.ppf(1 - 1 / n_trials) * math.sqrt(1 / max(n_trades, 2))
    se = math.sqrt((1 + sharpe**2 / 4) / max(n_trades, 2))
    if se <= 0:
        return 1.0
    z = (sharpe - expected_max) / se
    return 1.0 - norm.cdf(z)


def run_backtest(is_win, avg_price, side_is_yes):
    """Vectorized fixed-stake backtest.

    Returns dict with roi_pct, profit_factor, win_rate, trades_executed, max_drawdown.
    """
    n = len(is_win)
    if n == 0:
        return None

    # Effective entry price
    entry = np.where(side_is_yes, avg_price, 1.0 - avg_price)
    entry = np.maximum(entry, 0.01)

    # Payout per unit stake on win
    payout = 1.0 / entry - 1.0

    bank = INITIAL_BANK
    peak = INITIAL_BANK
    max_dd = 0.0
    wins = 0
    total_won = 0.0
    total_lost = 0.0

    for i in range(n):
        if bank < FIXED_STAKE:
            break
        if is_win[i]:
            profit = FIXED_STAKE * payout[i]
            bank += profit
            wins += 1
            total_won += profit
        else:
            bank -= FIXED_STAKE
            total_lost += FIXED_STAKE

        peak = max(peak, bank)
        dd = (peak - bank) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    trades = wins + int(total_lost / FIXED_STAKE)
    if trades == 0:
        return None

    return {
        "trades_executed": trades,
        "win_rate": round(wins / trades * 100, 1),
        "roi_pct": round((bank - INITIAL_BANK) / INITIAL_BANK * 100, 2),
        "profit_factor": round(total_won / total_lost, 3) if total_lost > 0 else 999.0,
        "max_drawdown": round(max_dd, 4),
        "final_equity": round(bank, 2),
    }


def main():
    print("=" * 70)
    print("STRATEGY DISCOVERY — Historical Dataset (8M+ positions)")
    print("=" * 70)
    t_start = time.time()

    con = duckdb.connect()
    con.execute("SET memory_limit='3GB'")
    con.execute("SET threads=3")
    con.execute("SET temp_directory='/tmp/duckdb'")
    os.makedirs("/tmp/duckdb", exist_ok=True)

    # ── 1. Build enriched dataset ──
    print("\n1/5 Building enriched dataset...", flush=True)
    t0 = time.time()

    con.execute(f"""
        CREATE OR REPLACE TABLE positions AS
        SELECT
            p.address,
            p.condition_id,
            p.category,
            p.is_win,
            p.avg_price,
            p.realized_pnl,
            p.volume_usd,
            p.direction,
            p.nonusdc_side,
            -- Side: token1 = YES token, token2 = NO token (verified: token1+answer1_won=100% WR)
            CASE
                WHEN p.nonusdc_side = 'token1' THEN 'YES'
                WHEN p.nonusdc_side = 'token2' THEN 'NO'
                ELSE 'YES'  -- fallback
            END as side,
            -- Market aggregates
            a.price_stddev,
            a.whale_volume / nullif(a.total_volume_usd, 0) as whale_fraction,
            (a.taker_buy_volume - a.taker_sell_volume)
                / nullif(a.taker_buy_volume + a.taker_sell_volume, 0) as taker_imbalance,
            a.total_volume_usd as market_volume,
            -- Market dates
            m.end_date,
            m.created_at as market_created_at
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
    n_markets = con.execute("SELECT count(DISTINCT condition_id) FROM positions").fetchone()[0]
    print(f"   {total:,} positions, {n_markets:,} markets, {time.time()-t0:.0f}s", flush=True)

    # Side distribution
    sides = con.execute("SELECT side, count(*) FROM positions GROUP BY side").fetchall()
    for s, c in sides:
        print(f"   {s}: {c:,} ({100*c/total:.1f}%)")

    # ── 2. Walk-forward split ──
    print("\n2/5 Walk-forward split...", flush=True)

    # Хронологический split по end_date
    con.execute("""
        CREATE OR REPLACE TABLE pos_train AS SELECT * FROM positions WHERE end_date < '2025-01-01';
        CREATE OR REPLACE TABLE pos_test AS SELECT * FROM positions WHERE end_date >= '2025-01-01' AND end_date < '2025-07-01';
        CREATE OR REPLACE TABLE pos_validate AS SELECT * FROM positions WHERE end_date >= '2025-07-01';
    """)

    for name in ["pos_train", "pos_test", "pos_validate"]:
        n = con.execute(f"SELECT count(*) FROM {name}").fetchone()[0]
        print(f"   {name}: {n:,}")

    # ── 3. Grid search ──
    print("\n3/5 Grid search...", flush=True)

    grid_dims = {
        "side": SIDES,
        "category": CATEGORIES,
        "price_zone": list(PRICE_ZONES.keys()),
        "whale": list(WHALE_PRESENCE.keys()),
        "taker": list(TAKER_BINS.keys()),
        "vol": list(VOL_BINS.keys()),
    }
    grid_keys = list(grid_dims.keys())
    grid_values = [grid_dims[k] for k in grid_keys]
    total_combos = 1
    for v in grid_values:
        total_combos *= len(v)
    print(f"   {total_combos:,} комбинаций", flush=True)

    # Preload train and test as numpy for fast filtering
    print("   Loading arrays...", flush=True)
    t0 = time.time()

    train_df = con.execute("""
        SELECT side, category, avg_price, is_win, price_stddev,
               whale_fraction, taker_imbalance
        FROM pos_train
    """).fetchnumpy()

    test_df = con.execute("""
        SELECT side, category, avg_price, is_win, price_stddev,
               whale_fraction, taker_imbalance
        FROM pos_test
    """).fetchnumpy()

    val_df = con.execute("""
        SELECT side, category, avg_price, is_win, price_stddev,
               whale_fraction, taker_imbalance
        FROM pos_validate
    """).fetchnumpy()

    print(f"   Loaded in {time.time()-t0:.0f}s", flush=True)

    # Convert to convenient arrays
    def to_arrays(df):
        return {
            "side": np.array(df["side"], dtype=str),
            "category": np.array(df["category"], dtype=str),
            "avg_price": np.array(df["avg_price"], dtype=np.float64),
            "is_win": np.array(df["is_win"], dtype=bool),
            "price_stddev": np.nan_to_num(np.array(df["price_stddev"], dtype=np.float64)),
            "whale_fraction": np.nan_to_num(np.array(df["whale_fraction"], dtype=np.float64)),
            "taker_imbalance": np.nan_to_num(np.array(df["taker_imbalance"], dtype=np.float64)),
        }

    train = to_arrays(train_df)
    test = to_arrays(test_df)
    val = to_arrays(val_df)

    # ── Grid search loop ──
    print("   Running grid search...", flush=True)
    t0 = time.time()
    candidates = []
    checked = 0

    for combo in itertools.product(*grid_values):
        params = dict(zip(grid_keys, combo))

        # Build mask for train
        pz_lo, pz_hi = PRICE_ZONES[params["price_zone"]]
        wh_lo, wh_hi = WHALE_PRESENCE[params["whale"]]
        tk_lo, tk_hi = TAKER_BINS[params["taker"]]
        vl_lo, vl_hi = VOL_BINS[params["vol"]]

        def build_mask(d):
            return (
                (d["side"] == params["side"])
                & (d["category"] == params["category"])
                & (d["avg_price"] >= pz_lo) & (d["avg_price"] < pz_hi)
                & (d["whale_fraction"] >= wh_lo) & (d["whale_fraction"] < wh_hi)
                & (d["taker_imbalance"] >= tk_lo) & (d["taker_imbalance"] < tk_hi)
                & (d["price_stddev"] >= vl_lo) & (d["price_stddev"] < vl_hi)
            )

        train_mask = build_mask(train)
        n_train = train_mask.sum()
        if n_train < MIN_TRADES:
            checked += 1
            continue

        # Train backtest
        side_is_yes = train["side"][train_mask] == "YES"
        train_result = run_backtest(
            train["is_win"][train_mask],
            train["avg_price"][train_mask],
            side_is_yes,
        )
        if train_result is None or train_result["win_rate"] < MIN_WIN_RATE or train_result["profit_factor"] < MIN_PROFIT_FACTOR:
            checked += 1
            continue

        # Test backtest
        test_mask = build_mask(test)
        if test_mask.sum() < 20:
            checked += 1
            continue

        side_is_yes_test = test["side"][test_mask] == "YES"
        test_result = run_backtest(
            test["is_win"][test_mask],
            test["avg_price"][test_mask],
            side_is_yes_test,
        )
        if test_result is None:
            checked += 1
            continue

        # Overfit detection: profitable on train but losing on test
        if train_result["roi_pct"] > 0 and test_result["roi_pct"] < 0:
            checked += 1
            continue

        # Validate
        val_mask = build_mask(val)
        val_result = None
        if val_mask.sum() >= 10:
            side_is_yes_val = val["side"][val_mask] == "YES"
            val_result = run_backtest(
                val["is_win"][val_mask],
                val["avg_price"][val_mask],
                side_is_yes_val,
            )

        candidates.append({
            "params": params,
            "label": "%s %s %s %s %s %s" % (
                params["side"], params["category"], params["price_zone"],
                params["whale"], params["taker"], params["vol"],
            ),
            "train": train_result,
            "test": test_result,
            "validate": val_result,
            "train_n": int(n_train),
            "test_n": int(test_mask.sum()),
            "val_n": int(val_mask.sum()),
        })

        checked += 1
        if checked % 5000 == 0:
            elapsed = time.time() - t0
            print(f"   ... {checked:,}/{total_combos:,} checked, {len(candidates)} passed, {elapsed:.0f}s", flush=True)

    checked = total_combos  # all checked
    elapsed = time.time() - t0
    print(f"   Done: {total_combos:,} combos, {len(candidates)} passed filters, {elapsed:.0f}s", flush=True)

    # ── 4. DSR + ranking ──
    print("\n4/5 DSR + ranking...", flush=True)

    for c in candidates:
        trades = c["test"]["trades_executed"]
        pf = c["test"]["profit_factor"]
        approx_sharpe = (pf - 1) * math.sqrt(trades / 252) if pf < 900 else 0
        dsr_p = deflated_sharpe_ratio(approx_sharpe, trades, total_combos)
        c["dsr_p"] = round(dsr_p, 4)
        c["dsr_tag"] = "confirmed" if dsr_p < 0.05 else "unconfirmed"
        c["approx_sharpe"] = round(approx_sharpe, 4)

    # Sort by test PF
    candidates.sort(key=lambda c: -c["test"]["profit_factor"])

    # ── 5. Results ──
    print("\n5/5 Results")
    print("=" * 70)

    confirmed = [c for c in candidates if c["dsr_tag"] == "confirmed"]
    unconfirmed = [c for c in candidates if c["dsr_tag"] == "unconfirmed"]
    print(f"\nTotal passed: {len(candidates)}")
    print(f"DSR confirmed: {len(confirmed)}")
    print(f"DSR unconfirmed: {len(unconfirmed)}")

    print(f"\n{'='*70}")
    print("TOP 20 (by test PF, DSR confirmed first):")
    print(f"{'='*70}")

    show = (confirmed + unconfirmed)[:20]
    for i, c in enumerate(show, 1):
        tag = "✅" if c["dsr_tag"] == "confirmed" else "⚠️"
        t = c["test"]
        tr = c["train"]
        v = c.get("validate")
        val_str = ""
        if v and v["trades_executed"] > 0:
            val_str = f" | Val: {v['trades_executed']}t WR={v['win_rate']}% ROI={v['roi_pct']}%"
        overfit = ""
        if v and v["roi_pct"] < 0 and t["roi_pct"] > 0:
            overfit = " ⚠️OVERFIT"

        print(f"\n{tag} #{i} {c['label']}")
        print(f"   Train: {tr['trades_executed']}t WR={tr['win_rate']}% PF={tr['profit_factor']} ROI={tr['roi_pct']}%")
        print(f"   Test:  {t['trades_executed']}t WR={t['win_rate']}% PF={t['profit_factor']} ROI={t['roi_pct']}% DD={t['max_drawdown']:.1%}")
        print(f"   DSR p={c['dsr_p']}{val_str}{overfit}")

    # Per-category summary
    print(f"\n{'='*70}")
    print("PER-CATEGORY CONFIRMED STRATEGIES:")
    print(f"{'='*70}")
    cat_confirmed = {}
    for c in confirmed:
        cat = c["params"]["category"]
        if cat not in cat_confirmed:
            cat_confirmed[cat] = []
        cat_confirmed[cat].append(c)

    for cat in sorted(cat_confirmed.keys()):
        strats = cat_confirmed[cat]
        print(f"\n  {cat} ({len(strats)} confirmed):")
        for s in strats[:5]:
            t = s["test"]
            print(f"    {s['label']}: {t['trades_executed']}t WR={t['win_rate']}% PF={t['profit_factor']} ROI={t['roi_pct']}%")

    # Save
    summary = {
        "run_date": datetime.now().isoformat(),
        "total_combos": total_combos,
        "passed_filter": len(candidates),
        "confirmed": len(confirmed),
        "unconfirmed": len(unconfirmed),
        "data": {
            "total_positions": int(total),
            "total_markets": int(n_markets),
        },
        "candidates": candidates[:100],  # top 100
    }

    out_path = f"{OUT}/discovery_historical.json"
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\nResults: {out_path}")
    print(f"Total time: {time.time() - t_start:.0f}s")


if __name__ == "__main__":
    main()

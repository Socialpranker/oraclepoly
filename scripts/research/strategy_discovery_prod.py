"""Strategy Discovery на прод-снапшотах.

Grid search по комбинациям: category × side × div_bin × horizon_bin × whale_presence.
Данные: data/prod_oracle_snapshots.parquet (367K resolved, 2026-02-24→2026-04-10).
Primary flow — на stored oracle_score (уже посчитан продом).

Вывод: top-20 стратегий с edge ≥ +2pp vs best baseline и N ≥ 500.
Формат: JSON + markdown таблица в stdout.

Использование:
    source .venv/bin/activate
    python scripts/research/strategy_discovery_prod.py                  # stdout
    python scripts/research/strategy_discovery_prod.py --out results    # + JSON/MD в docs/research/
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
from scipy.stats import norm

from app.services.fee_calculator import estimate_trading_fee

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

ROOT = Path(__file__).resolve().parent.parent.parent
PROD_PQ = ROOT / "data" / "prod_oracle_snapshots.parquet"
OUTPUT_DIR = ROOT / "docs" / "research"

# ---------------------------------------------------------------------------
# Grid dimensions
# ---------------------------------------------------------------------------

CATEGORIES = ["Crypto", "Politics", "Sports", "Tech & AI", "Culture", "World & Economy"]
SIDES = ["YES", "NO"]
DIV_BINS = {
    "3-5%": (0.03, 0.05),
    "5-8%": (0.05, 0.08),
    "8-15%": (0.08, 0.15),
}
HORIZON_BINS = {
    "<7d": (0, 7),
    "7-30d": (7, 30),
    ">30d": (30, 9999),
}
WHALE_PRESENCE = [True, False]

# Quality filters
MIN_TRADES = 500
MIN_TEST_TRADES = 100  # Минимум трейдов на test set (защита от noise)
EDGE_THRESHOLD_PP = 2.0  # +2pp vs best baseline
WALK_FORWARD_RATIO = 0.80
DSR_SIGNIFICANCE = 0.05

# Backtest params
INITIAL_BANK = 10_000.0
STAKE = 10.0


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------


def load_prod_snapshots() -> pd.DataFrame:
    """Load prod_oracle_snapshots.parquet via DuckDB."""
    if not PROD_PQ.exists():
        print(f"Файл не найден: {PROD_PQ}", file=sys.stderr)
        sys.exit(1)

    df = duckdb.sql(f"""
        SELECT *,
            -- Derived: side based on oracle vs market
            CASE WHEN oracle_score > market_price THEN 'YES' ELSE 'NO' END AS side,
            -- Derived: entry_price = market_price at snapshot time
            market_price AS entry_price,
            -- Derived: oracle_score_at_entry
            oracle_score AS oracle_score_at_entry,
            -- Derived: win
            CASE
                WHEN oracle_score > market_price AND outcome = 'yes' THEN 1
                WHEN oracle_score <= market_price AND outcome = 'no' THEN 1
                ELSE 0
            END AS win,
            -- Derived: horizon_days
            CASE
                WHEN end_date IS NOT NULL AND created_at IS NOT NULL
                THEN GREATEST(0, EXTRACT(EPOCH FROM (end_date - created_at)) / 86400.0)
                ELSE NULL
            END AS horizon_days,
            -- Derived: has_whale (whale_tx_count > 0 OR whale_contribution_raw != 0)
            CASE WHEN whale_tx_count > 0 OR whale_contribution_raw != 0 THEN true ELSE false END AS has_whale
        FROM '{PROD_PQ}'
        WHERE outcome IS NOT NULL
    """).fetchdf()

    print(f"Загружено {len(df):,} resolved снапшотов из {PROD_PQ.name}")
    return df


# ---------------------------------------------------------------------------
# Precompute arrays (adapted from backtest_v2)
# ---------------------------------------------------------------------------


def precompute_arrays(df: pd.DataFrame) -> dict:
    """Numpy arrays for fast backtest. Matches backtest_v2.precompute_arrays logic."""
    is_yes = (df["side"] == "YES").values
    entry_price = df["entry_price"].values.astype(np.float64)
    oracle_score = df["oracle_score_at_entry"].values.astype(np.float64)
    win = df["win"].values.astype(np.int8)

    effective_entry = np.where(is_yes, entry_price, 1.0 - entry_price)
    effective_entry = np.maximum(effective_entry, 0.01)

    p = np.where(is_yes, oracle_score, 1.0 - oracle_score)
    b = 1.0 / effective_entry - 1.0

    kelly_raw = np.where(b > 0, (p * b - (1.0 - p)) / b, 0.0)
    payout = 1.0 / effective_entry - 1.0

    valid = (kelly_raw > 0) & (entry_price > 0.01) & (entry_price < 0.99) & (~np.isnan(oracle_score))

    dates = df["created_at"].values
    categories = df["category"].values if "category" in df.columns else np.full(len(df), "", dtype=object)

    return {
        "kelly_raw": kelly_raw,
        "payout": payout,
        "win": win,
        "valid": valid,
        "dates": dates,
        "entry_price": entry_price,
        "categories": categories,
        "n": len(df),
    }


# ---------------------------------------------------------------------------
# Entry backtest (from backtest_v2)
# ---------------------------------------------------------------------------


def run_entry_backtest(
    arrays: dict,
    initial_bank: float = INITIAL_BANK,
    stake: float = STAKE,
    filter_mask: np.ndarray | None = None,
) -> dict:
    """Entry backtest with fixed stake. Returns metrics dict."""
    payout = arrays["payout"]
    win = arrays["win"]
    valid = arrays["valid"].copy()
    entry_price = arrays["entry_price"]
    categories = arrays["categories"]
    n = arrays["n"]

    if filter_mask is not None:
        valid = valid & filter_mask

    bank = initial_bank
    peak = initial_bank
    max_drawdown = 0.0
    wins = 0
    trades_executed = 0
    total_won = 0.0
    total_lost = 0.0

    for i in range(n):
        if not valid[i]:
            continue
        if bank < stake:
            break

        fee = estimate_trading_fee(stake, entry_price[i], str(categories[i]))

        if win[i]:
            profit = stake * payout[i] - fee
            bank += profit
            wins += 1
            total_won += profit
        else:
            loss = stake + fee
            bank -= loss
            total_lost += loss

        trades_executed += 1
        peak = max(peak, bank)
        dd = (peak - bank) / peak if peak > 0 else 0.0
        max_drawdown = max(max_drawdown, dd)

    roi_pct = (bank - initial_bank) / initial_bank * 100.0 if initial_bank > 0 else 0.0
    win_rate = (wins / trades_executed * 100.0) if trades_executed > 0 else 0.0
    profit_factor = (total_won / total_lost) if total_lost > 0 else float("inf")

    total_pnl = total_won - total_lost
    roi_per_trade = (total_pnl / trades_executed / stake * 100.0) if trades_executed > 0 else 0.0

    return {
        "roi_pct": round(roi_pct, 2),
        "profit_factor": round(profit_factor, 3),
        "max_drawdown": round(max_drawdown, 4),
        "final_equity": round(bank, 2),
        "trades_executed": trades_executed,
        "win_rate": round(win_rate, 1),
        "avg_win": round(total_won / wins, 2) if wins > 0 else 0.0,
        "avg_loss": round(total_lost / (trades_executed - wins), 2) if (trades_executed - wins) > 0 else 0.0,
        "roi_per_trade": round(roi_per_trade, 2),
    }


# ---------------------------------------------------------------------------
# Walk-forward split
# ---------------------------------------------------------------------------


def walk_forward_split(dates: np.ndarray, ratio: float = WALK_FORWARD_RATIO) -> tuple[np.ndarray, np.ndarray]:
    """Split by date: train (first ratio%) / test (last 1-ratio%)."""
    unique_dates = np.unique(dates)
    split_idx = int(len(unique_dates) * ratio)
    split_date = unique_dates[split_idx]
    return dates < split_date, dates >= split_date


# ---------------------------------------------------------------------------
# Baselines
# ---------------------------------------------------------------------------


def compute_baselines(df: pd.DataFrame, arrays: dict, train_mask: np.ndarray, test_mask: np.ndarray) -> dict:
    """Compute per-category baselines: always-NO, market_follower (oracle side).

    always-NO: всегда ставим NO по market_price, win = outcome=='no'.
    market_follower: ставим по oracle side, kelly filter активен (backtest_v2 логика).

    ROI per trade (roi_per_trade) — нормализованная метрика для сравнения edge,
    т.к. raw ROI зависит от bank size vs total volume.

    Returns: {category: {"always_no": {...}, "market_follower": {...}}}
    """
    baselines = {}
    categories = df["category"].unique()

    for cat in categories:
        cat_mask = df["category"].values == cat
        baselines[cat] = {}

        # --- always-NO baseline ---
        no_win = (df["outcome"].values == "no")
        no_entry = 1.0 - df["market_price"].values.astype(np.float64)
        no_entry = np.maximum(no_entry, 0.01)
        no_valid = (no_entry > 0.01) & (no_entry < 0.99) & cat_mask

        no_payout = 1.0 / no_entry - 1.0
        no_arrays = {
            "payout": no_payout,
            "win": no_win.astype(np.int8),
            "valid": no_valid,
            "entry_price": no_entry,
            "categories": arrays["categories"],
            "n": arrays["n"],
        }

        no_full = _stat_backtest(no_arrays, no_valid)
        no_test = _stat_backtest(no_arrays, no_valid & test_mask)
        baselines[cat]["always_no"] = {"full": no_full, "test": no_test}

        # --- market_follower baseline ---
        mf_full = run_entry_backtest(arrays, filter_mask=cat_mask)
        mf_test = run_entry_backtest(arrays, filter_mask=cat_mask & test_mask)
        baselines[cat]["market_follower"] = {"full": mf_full, "test": mf_test}

    return baselines


def _stat_backtest(arrays: dict, mask: np.ndarray) -> dict:
    """Statistical backtest: compute per-trade P&L without bank constraint.

    No bankruptcy — counts every valid trade. Returns roi_per_trade (avg P&L / stake * 100).
    """
    payout = arrays["payout"]
    win = arrays["win"]
    entry_price = arrays["entry_price"]
    categories = arrays["categories"]
    n = arrays["n"]

    wins = 0
    trades = 0
    total_pnl = 0.0

    for i in range(n):
        if not mask[i]:
            continue

        fee = estimate_trading_fee(STAKE, abs(entry_price[i]), str(categories[i]))

        if win[i]:
            pnl = STAKE * payout[i] - fee
            wins += 1
        else:
            pnl = -(STAKE + fee)

        total_pnl += pnl
        trades += 1

    wr = (wins / trades * 100.0) if trades > 0 else 0.0
    roi_per_trade = (total_pnl / trades / STAKE * 100.0) if trades > 0 else 0.0

    return {
        "trades_executed": trades,
        "win_rate": round(wr, 1),
        "roi_per_trade": round(roi_per_trade, 2),
        "total_pnl": round(total_pnl, 2),
    }


# ---------------------------------------------------------------------------
# Grid search
# ---------------------------------------------------------------------------


def generate_grid() -> list[dict]:
    """Generate all combos: category × side × div_bin × horizon_bin × whale_presence."""
    combos = []
    for cat in CATEGORIES:
        for side in SIDES:
            for div_name, (div_lo, div_hi) in DIV_BINS.items():
                for hor_name, (hor_lo, hor_hi) in HORIZON_BINS.items():
                    for whale in WHALE_PRESENCE:
                        combos.append({
                            "category": cat,
                            "side": side,
                            "div_bin": div_name,
                            "div_lo": div_lo,
                            "div_hi": div_hi,
                            "horizon_bin": hor_name,
                            "horizon_lo": hor_lo,
                            "horizon_hi": hor_hi,
                            "whale_presence": whale,
                        })
    return combos


def build_mask(df: pd.DataFrame, combo: dict) -> np.ndarray:
    """Build boolean mask from combo dict."""
    mask = np.ones(len(df), dtype=bool)

    # Category
    mask &= df["category"].values == combo["category"]

    # Side
    mask &= df["side"].values == combo["side"]

    # Divergence bin
    abs_div = np.abs(df["divergence"].values.astype(np.float64))
    mask &= (abs_div >= combo["div_lo"]) & (abs_div < combo["div_hi"])

    # Horizon bin
    if "horizon_days" in df.columns:
        hd = df["horizon_days"].fillna(-1).values.astype(np.float64)
        mask &= (hd >= combo["horizon_lo"]) & (hd < combo["horizon_hi"])

    # Whale presence
    has_whale = df["has_whale"].values
    if combo["whale_presence"]:
        mask &= has_whale
    else:
        mask &= ~has_whale

    return mask


def combo_label(combo: dict) -> str:
    """Human-readable label: 'NO Sports 5-8% 7-30d no_wh'."""
    wh = "wh" if combo["whale_presence"] else "no_wh"
    return f"{combo['side']} {combo['category']} {combo['div_bin']} {combo['horizon_bin']} {wh}"


# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio
# ---------------------------------------------------------------------------


def compute_dsr(observed_sharpe: float, n_trials: int, n_observations: int) -> float:
    """DSR p-value (Bailey & Lopez de Prado 2014)."""
    n_trials = max(n_trials, 2)
    n_obs = max(n_observations, 2)
    expected_max = norm.ppf(1 - 1 / n_trials) * math.sqrt(1 / n_obs)
    se = math.sqrt((1 + 0.25 * 0**2 + (3 - 3) / 4 * observed_sharpe**2) / n_obs)
    z = (observed_sharpe - expected_max) / se if se > 0 else 0.0
    return 1 - norm.cdf(z)


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def run_discovery() -> dict:
    """Main pipeline: load → baselines → grid → evaluate → filter → DSR → output."""

    # 1. Load data
    df = load_prod_snapshots()

    # Date range info
    date_min = df["created_at"].min()
    date_max = df["created_at"].max()
    date_range_days = (date_max - date_min).total_seconds() / 86400
    print(f"Период: {date_min:%Y-%m-%d} → {date_max:%Y-%m-%d} ({date_range_days:.0f} дней)")
    print(f"Категории: {dict(df['category'].value_counts())}")

    # 2. Precompute
    arrays = precompute_arrays(df)
    train_mask, test_mask = walk_forward_split(arrays["dates"], ratio=WALK_FORWARD_RATIO)
    train_n = train_mask.sum()
    test_n = test_mask.sum()
    print(f"Split: train {train_n:,} ({WALK_FORWARD_RATIO:.0%}) / test {test_n:,} ({1-WALK_FORWARD_RATIO:.0%})")

    # 3. Baselines
    print("\nВычисляю baselines...")
    baselines = compute_baselines(df, arrays, train_mask, test_mask)
    print("\nBaselines (test set, roi_per_trade = avg P&L per $10 stake):")
    for cat, bl in baselines.items():
        no = bl["always_no"]["test"]
        mf = bl["market_follower"]["test"]
        print(f"  {cat:20s} | always-NO: RPT {no['roi_per_trade']:>6.2f}% WR {no['win_rate']:.1f}% N={no['trades_executed']:>6,}"
              f" | oracle_follow: RPT {mf['roi_per_trade']:>6.2f}% WR {mf['win_rate']:.1f}% N={mf['trades_executed']:>6,}")

    # Best baseline roi_per_trade per category (test set)
    best_baseline: dict[str, float] = {}
    best_baseline_name: dict[str, str] = {}
    for cat, bl in baselines.items():
        no_rpt = bl["always_no"]["test"]["roi_per_trade"]
        mf_rpt = bl["market_follower"]["test"]["roi_per_trade"]
        if no_rpt >= mf_rpt:
            best_baseline[cat] = no_rpt
            best_baseline_name[cat] = "always_no"
        else:
            best_baseline[cat] = mf_rpt
            best_baseline_name[cat] = "market_follower"

    # 4. Grid search
    grid = generate_grid()
    total_combos = len(grid)
    print(f"\nGrid search: {total_combos} комбинаций...")

    candidates = []
    for combo in grid:
        mask = build_mask(df, combo)
        n_total = mask.sum()
        if n_total < MIN_TRADES:
            continue

        # Full backtest
        full = run_entry_backtest(arrays, filter_mask=mask)
        if full["trades_executed"] < MIN_TRADES:
            continue

        # Train/test
        train = run_entry_backtest(arrays, filter_mask=mask & train_mask)
        test = run_entry_backtest(arrays, filter_mask=mask & test_mask)

        # Test set minimum
        if test["trades_executed"] < MIN_TEST_TRADES:
            continue

        # Overfit detection: train ROI > 0 but test ROI < 0
        if train["roi_pct"] > 0 and test["roi_pct"] < 0:
            continue

        # Edge vs best baseline: roi_per_trade (normalized per-trade metric)
        cat = combo["category"]
        baseline_rpt = best_baseline.get(cat, 0.0)
        edge_pp = test["roi_per_trade"] - baseline_rpt

        candidates.append({
            "combo": combo,
            "label": combo_label(combo),
            "full": full,
            "train": train,
            "test": test,
            "n_total": int(n_total),
            "edge_pp": round(edge_pp, 2),
            "baseline_name": best_baseline_name.get(cat, "?"),
            "baseline_rpt": baseline_rpt,
        })

    print(f"Прошли базовый фильтр: {len(candidates)} из {total_combos}")

    # 5. DSR
    n_observations = int(max(date_range_days, 1))
    for c in candidates:
        trades = c["full"]["trades_executed"]
        pf = c["full"]["profit_factor"]
        approx_sharpe = (pf - 1) * math.sqrt(trades / 252)
        dsr_p = compute_dsr(approx_sharpe, total_combos, n_observations)
        c["dsr_p"] = round(dsr_p, 4)
        c["dsr_tag"] = "confirmed" if dsr_p < DSR_SIGNIFICANCE else "unconfirmed"
        c["approx_sharpe"] = round(approx_sharpe, 4)

    # 6. Filter: edge ≥ +2pp (roi_per_trade) AND N ≥ 500
    filtered = [c for c in candidates if c["edge_pp"] >= EDGE_THRESHOLD_PP and c["full"]["trades_executed"] >= MIN_TRADES]
    filtered.sort(key=lambda c: c["test"]["roi_per_trade"], reverse=True)

    print(f"С edge ≥ +{EDGE_THRESHOLD_PP}pp и N ≥ {MIN_TRADES}: {len(filtered)}")

    # Top-20
    top20 = filtered[:20]

    # Build summary
    summary = {
        "run_date": datetime.now(tz=timezone.utc).strftime("%Y-%m-%d"),
        "data_source": str(PROD_PQ.name),
        "data_range": f"{date_min:%Y-%m-%d} → {date_max:%Y-%m-%d}",
        "total_snapshots": len(df),
        "total_combos": total_combos,
        "passed_basic_filter": len(candidates),
        "passed_edge_filter": len(filtered),
        "walk_forward_ratio": WALK_FORWARD_RATIO,
        "min_trades": MIN_TRADES,
        "edge_threshold_pp": EDGE_THRESHOLD_PP,
        "baselines": {
            cat: {
                "best": best_baseline_name[cat],
                "best_test_rpt": best_baseline[cat],
                "always_no_test_rpt": bl["always_no"]["test"]["roi_per_trade"],
                "always_no_test_wr": bl["always_no"]["test"]["win_rate"],
                "always_no_test_n": bl["always_no"]["test"]["trades_executed"],
                "market_follower_test_rpt": bl["market_follower"]["test"]["roi_per_trade"],
                "market_follower_test_wr": bl["market_follower"]["test"]["win_rate"],
                "market_follower_test_n": bl["market_follower"]["test"]["trades_executed"],
            }
            for cat, bl in baselines.items()
        },
        "top_strategies": [
            {
                "rank": i + 1,
                "label": c["label"],
                "category": c["combo"]["category"],
                "side": c["combo"]["side"],
                "div_bin": c["combo"]["div_bin"],
                "horizon_bin": c["combo"]["horizon_bin"],
                "whale_presence": c["combo"]["whale_presence"],
                "n_total": c["n_total"],
                "full_roi_pct": c["full"]["roi_pct"],
                "full_rpt": c["full"]["roi_per_trade"],
                "full_wr": c["full"]["win_rate"],
                "full_pf": c["full"]["profit_factor"],
                "full_max_dd": c["full"]["max_drawdown"],
                "full_trades": c["full"]["trades_executed"],
                "train_roi_pct": c["train"]["roi_pct"],
                "train_rpt": c["train"]["roi_per_trade"],
                "train_wr": c["train"]["win_rate"],
                "test_roi_pct": c["test"]["roi_pct"],
                "test_rpt": c["test"]["roi_per_trade"],
                "test_wr": c["test"]["win_rate"],
                "test_pf": c["test"]["profit_factor"],
                "test_trades": c["test"]["trades_executed"],
                "edge_pp": c["edge_pp"],
                "baseline_name": c["baseline_name"],
                "baseline_rpt": c["baseline_rpt"],
                "dsr_p": c["dsr_p"],
                "dsr_tag": c["dsr_tag"],
                "approx_sharpe": c["approx_sharpe"],
            }
            for i, c in enumerate(top20)
        ],
    }

    return summary


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------


def format_markdown(summary: dict) -> str:
    """Format results as markdown table."""
    lines = [
        f"# Strategy Discovery — Prod Snapshots",
        f"",
        f"**Дата:** {summary['run_date']}  ",
        f"**Данные:** {summary['data_source']} ({summary['total_snapshots']:,} снапшотов, {summary['data_range']})  ",
        f"**Walk-forward:** {summary['walk_forward_ratio']:.0%} train / {1 - summary['walk_forward_ratio']:.0%} test  ",
        f"**Фильтры:** N ≥ {summary['min_trades']}, test N ≥ {MIN_TEST_TRADES}, edge ≥ +{summary['edge_threshold_pp']}pp vs best baseline  ",
        f"**Комбинаций:** {summary['total_combos']} → {summary['passed_basic_filter']} basic → {summary['passed_edge_filter']} edge filter",
        "",
        "## Baselines (test set)",
        "",
        "| Category | Best Baseline | RPT% | always-NO RPT% | always-NO WR% | oracle_follow RPT% | oracle_follow WR% |",
        "|----------|---------------|------|----------------|---------------|--------------------|--------------------|",
    ]

    for cat, bl in sorted(summary["baselines"].items(), key=lambda x: -x[1]["always_no_test_n"]):
        lines.append(
            f"| {cat} | {bl['best']} | {bl['best_test_rpt']:.2f}% | "
            f"{bl['always_no_test_rpt']:.2f}% | {bl['always_no_test_wr']:.1f}% | "
            f"{bl['market_follower_test_rpt']:.2f}% | {bl['market_follower_test_wr']:.1f}% |"
        )

    top = summary["top_strategies"]
    if not top:
        lines.extend(["", "## Top стратегии", "", "Стратегий с edge ≥ +2pp не найдено."])
        return "\n".join(lines)

    lines.extend([
        "",
        f"## Top-{len(top)} стратегий (edge ≥ +{summary['edge_threshold_pp']}pp по RPT)",
        "",
        "*RPT = ROI per trade (avg P&L / stake, нормализован). Edge = RPT стратегии − RPT лучшего baseline.*",
        "",
        "| # | Стратегия | N | Full RPT% | Full WR% | Full PF | Test RPT% | Test WR% | Test PF | Edge pp | DSR |",
        "|---|-----------|---|-----------|----------|---------|-----------|----------|---------|---------|-----|",
    ])

    for s in top:
        dsr = "✅" if s["dsr_tag"] == "confirmed" else f"p={s['dsr_p']:.3f}"
        lines.append(
            f"| {s['rank']} | {s['label']} | {s['full_trades']:,} | "
            f"{s['full_rpt']:.2f}% | {s['full_wr']:.1f}% | {s['full_pf']:.2f} | "
            f"{s['test_rpt']:.2f}% | {s['test_wr']:.1f}% | {s['test_pf']:.2f} | "
            f"+{s['edge_pp']:.1f} | {dsr} |"
        )

    # Detail cards for top-5
    lines.extend(["", "## Детали top-5", ""])
    for s in top[:5]:
        lines.extend([
            f"### #{s['rank']} {s['label']}",
            f"- **Category:** {s['category']}, **Side:** {s['side']}",
            f"- **Divergence:** {s['div_bin']}, **Horizon:** {s['horizon_bin']}, **Whale:** {'yes' if s['whale_presence'] else 'no'}",
            f"- **Full:** RPT {s['full_rpt']:.2f}%, ROI {s['full_roi_pct']:.1f}%, WR {s['full_wr']:.1f}%, PF {s['full_pf']:.2f}, MaxDD {s['full_max_dd']:.2%}, N={s['full_trades']:,}",
            f"- **Train:** RPT {s['train_rpt']:.2f}%, ROI {s['train_roi_pct']:.1f}%, WR {s['train_wr']:.1f}%",
            f"- **Test:** RPT {s['test_rpt']:.2f}%, ROI {s['test_roi_pct']:.1f}%, WR {s['test_wr']:.1f}%, PF {s['test_pf']:.2f}, N={s['test_trades']:,}",
            f"- **Edge:** +{s['edge_pp']:.1f}pp RPT vs {s['baseline_name']} (baseline RPT {s['baseline_rpt']:.2f}%)",
            f"- **DSR:** p={s['dsr_p']:.4f} ({s['dsr_tag']}), Sharpe≈{s['approx_sharpe']:.3f}",
            "",
        ])

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(description="Strategy Discovery на прод-снапшотах")
    parser.add_argument("--out", type=str, default=None, help="Сохранить JSON + MD в docs/research/")
    args = parser.parse_args()

    summary = run_discovery()

    # Markdown to stdout
    md = format_markdown(summary)
    print("\n" + "=" * 80)
    print(md)

    # Save files
    if args.out:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        run_date = summary["run_date"]

        json_path = OUTPUT_DIR / f"strategy_discovery_prod_{run_date}.json"
        with open(json_path, "w") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False, default=str)
        print(f"\nJSON: {json_path}")

        md_path = OUTPUT_DIR / f"strategy_discovery_prod_{run_date}.md"
        with open(md_path, "w") as f:
            f.write(md)
        print(f"MD:   {md_path}")


if __name__ == "__main__":
    main()

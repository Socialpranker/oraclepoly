"""Walk-forward backtest на историческом датасете Polymarket.

Берёт PnL-агрегаты из maker_pnl.parquet / taker_pnl.parquet,
обогащает market metadata из market_enriched.parquet,
и прогоняет walk-forward: train 2024, test 2025, validate 2026.

Для каждого «виртуального кита» (wallet с ≥30 resolved) вычисляет:
- Win rate, ROI, sector accuracy
- Сравнивает whale-следование vs contra стратегии
- Считает Deflated Sharpe Ratio (DSR)

Запуск:
  ssh root@<YOUR_SERVER_IP>
  python scripts/research/backtest_historical.py

  # Только определённая категория:
  python scripts/research/backtest_historical.py --category Crypto

  # Сохранить CSV:
  python scripts/research/backtest_historical.py --output /mnt/data/polymarket_dataset/backtest_results.csv
"""

import argparse
import math
import os
import sys
import time
from datetime import datetime

import duckdb
import numpy as np

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

DATA_DIR = "/mnt/data/polymarket_dataset"
MAKER_PNL = f"{DATA_DIR}/maker_pnl.parquet"
TAKER_PNL = f"{DATA_DIR}/taker_pnl.parquet"
MARKET_ENRICHED = f"{DATA_DIR}/market_enriched.parquet"

# Walk-forward периоды
PERIODS = {
    "train": ("2020-01-01", "2024-12-31"),
    "test": ("2025-01-01", "2025-12-31"),
    "validate": ("2026-01-01", "2026-12-31"),
}

# Минимальные требования для стратегии
MIN_POSITIONS = 30
MIN_WIN_RATE = 0.52
MIN_ROI = 5.0

# Kelly sizing
INITIAL_BANK = 10_000.0
FIXED_STAKE = 50.0


# ---------------------------------------------------------------------------
# Deflated Sharpe Ratio (Bailey & López de Prado, 2014)
# ---------------------------------------------------------------------------


def deflated_sharpe_ratio(
    sharpe: float,
    n_trades: int,
    n_strategies: int,
    skew: float = 0.0,
    kurtosis: float = 3.0,
) -> float:
    """DSR: вероятность что Sharpe > 0 не случайна при множественном тестировании.

    Returns: p-value (< 0.05 = значимый).
    """
    if n_trades < 10 or sharpe <= 0:
        return 1.0

    # Expected max Sharpe under null (Euler-Mascheroni correction)
    euler_gamma = 0.5772
    e_max_sharpe = (1.0 - euler_gamma) * (2.0 * math.log(n_strategies)) ** 0.5
    if n_strategies > 1:
        e_max_sharpe += euler_gamma * (2.0 * math.log(n_strategies)) ** -0.5

    # Standard error of Sharpe
    se = ((1 - skew * sharpe + (kurtosis - 1) / 4 * sharpe**2) / n_trades) ** 0.5
    if se <= 0:
        return 1.0

    # PSR: prob that SR > SR_benchmark
    from scipy.stats import norm

    z = (sharpe - e_max_sharpe) / se
    return 1.0 - norm.cdf(z)


# ---------------------------------------------------------------------------
# Core
# ---------------------------------------------------------------------------


def load_data(con: duckdb.DuckDBPyConnection, category: str | None = None) -> duckdb.DuckDBPyRelation:
    """Загрузить объединённый PnL dataset с market timestamps."""
    category_filter = ""
    if category:
        category_filter = f"AND m.category = '{category}'"

    # Объединяем maker + taker PnL, берём timestamp рынка из market_enriched
    query = f"""
        WITH combined AS (
            SELECT address, condition_id, category, is_win, realized_pnl, volume_usd,
                   trade_count, avg_price, direction, nonusdc_side
            FROM '{MAKER_PNL}'
            UNION ALL
            SELECT address, condition_id, category, is_win, realized_pnl, volume_usd,
                   trade_count, avg_price, direction, nonusdc_side
            FROM '{TAKER_PNL}'
        )
        SELECT
            c.*,
            m.end_date,
            m.created_at as market_created_at,
            m.question,
            m.winner
        FROM combined c
        JOIN '{MARKET_ENRICHED}' m ON c.condition_id = m.condition_id
        WHERE c.is_win IS NOT NULL
          AND m.end_date IS NOT NULL
          {category_filter}
        ORDER BY m.end_date
    """
    return con.execute(query).fetchdf()


def split_by_period(df, periods: dict) -> dict:
    """Разбить DataFrame по временным периодам."""
    import pandas as pd

    df["end_date"] = pd.to_datetime(df["end_date"], utc=True)
    splits = {}
    for name, (start, end) in periods.items():
        start_dt = pd.Timestamp(start, tz="UTC")
        end_dt = pd.Timestamp(end, tz="UTC")
        mask = (df["end_date"] >= start_dt) & (df["end_date"] <= end_dt)
        splits[name] = df[mask].copy()
    return splits


def compute_wallet_metrics(df) -> list[dict]:
    """Вычислить per-wallet метрики на заданном периоде."""
    if df.empty:
        return []

    grouped = df.groupby("address")
    results = []

    for address, group in grouped:
        n_positions = len(group)
        if n_positions < 5:
            continue

        wins = int(group["is_win"].sum())
        losses = n_positions - wins
        total_pnl = float(group["realized_pnl"].sum())
        total_invested = float(group["volume_usd"].sum())
        roi = (total_pnl / total_invested * 100) if total_invested > 0 else 0.0
        win_rate = wins / n_positions

        # Per-category breakdown
        cat_stats = {}
        for cat, cat_group in group.groupby("category"):
            cat_wins = int(cat_group["is_win"].sum())
            cat_total = len(cat_group)
            cat_stats[cat] = {
                "wins": cat_wins,
                "losses": cat_total - cat_wins,
                "wr": cat_wins / cat_total,
                "pnl": float(cat_group["realized_pnl"].sum()),
            }

        # Sharpe-like metric (PnL-based, per position)
        pnl_series = group["realized_pnl"].values.astype(np.float64)
        mean_pnl = np.mean(pnl_series)
        std_pnl = np.std(pnl_series)
        sharpe = (mean_pnl / std_pnl * np.sqrt(252)) if std_pnl > 0 else 0.0

        results.append({
            "address": address,
            "positions": n_positions,
            "wins": wins,
            "losses": losses,
            "win_rate": win_rate,
            "total_pnl": total_pnl,
            "total_invested": total_invested,
            "roi": roi,
            "sharpe": sharpe,
            "skew": float(np.nan_to_num(
                n_positions / ((n_positions - 1) * (n_positions - 2)) * np.sum(((pnl_series - mean_pnl) / max(std_pnl, 1e-9)) ** 3)
            )) if n_positions > 2 else 0.0,
            "category_stats": cat_stats,
        })

    return results


def run_follow_strategy(wallet_metrics_train: list[dict], df_test, min_positions: int, min_wr: float, min_roi: float) -> dict:
    """Стратегия follow: ставим вместе с wallets отобранными на train."""
    # Отбираем profitable wallets на train
    selected = [
        w for w in wallet_metrics_train
        if w["positions"] >= min_positions
        and w["win_rate"] >= min_wr
        and w["roi"] >= min_roi
    ]

    if not selected:
        return {"n_wallets": 0, "trades": 0}

    selected_addrs = {w["address"] for w in selected}

    # Прогон на test
    test_trades = df_test[df_test["address"].isin(selected_addrs)]
    if test_trades.empty:
        return {"n_wallets": len(selected), "trades": 0}

    bank = INITIAL_BANK
    peak = INITIAL_BANK
    max_dd = 0.0
    wins = 0
    total = 0
    total_won = 0.0
    total_lost = 0.0

    for _, row in test_trades.iterrows():
        if bank < FIXED_STAKE:
            break

        total += 1
        if row["is_win"]:
            # Estimate payout from avg_price
            avg_price = float(row["avg_price"]) if row["avg_price"] and row["avg_price"] > 0.01 else 0.5
            payout = FIXED_STAKE * (1.0 / avg_price - 1.0)
            bank += payout
            wins += 1
            total_won += payout
        else:
            bank -= FIXED_STAKE
            total_lost += FIXED_STAKE

        peak = max(peak, bank)
        dd = (peak - bank) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    wr = wins / total if total > 0 else 0.0
    roi = (bank - INITIAL_BANK) / INITIAL_BANK * 100
    pf = total_won / total_lost if total_lost > 0 else float("inf")

    return {
        "n_wallets": len(selected),
        "trades": total,
        "wins": wins,
        "win_rate": round(wr, 4),
        "roi": round(roi, 2),
        "pf": round(pf, 3),
        "max_dd": round(max_dd, 4),
        "final_equity": round(bank, 2),
    }


def run_contra_strategy(wallet_metrics_train: list[dict], df_test, threshold_positions: int = 30) -> dict:
    """Стратегия contra: фейд worst wallets (кто стабильно теряет)."""
    # Отбираем consistently losing wallets на train
    losers = [
        w for w in wallet_metrics_train
        if w["positions"] >= threshold_positions
        and w["win_rate"] < 0.45
        and w["roi"] < -10.0
    ]

    if not losers:
        return {"n_wallets": 0, "trades": 0}

    loser_addrs = {w["address"] for w in losers}

    # Contra: когда loser покупает YES → мы ставим NO (инвертируем is_win)
    test_trades = df_test[df_test["address"].isin(loser_addrs)]
    if test_trades.empty:
        return {"n_wallets": len(losers), "trades": 0}

    bank = INITIAL_BANK
    peak = INITIAL_BANK
    max_dd = 0.0
    wins = 0
    total = 0
    total_won = 0.0
    total_lost = 0.0

    for _, row in test_trades.iterrows():
        if bank < FIXED_STAKE:
            break

        total += 1
        # Инвертируем: проигрыш loser'a = наш выигрыш
        contra_win = not row["is_win"]
        if contra_win:
            avg_price = float(row["avg_price"]) if row["avg_price"] and row["avg_price"] > 0.01 else 0.5
            payout = FIXED_STAKE * (1.0 / (1.0 - avg_price) - 1.0)
            bank += payout
            wins += 1
            total_won += payout
        else:
            bank -= FIXED_STAKE
            total_lost += FIXED_STAKE

        peak = max(peak, bank)
        dd = (peak - bank) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    wr = wins / total if total > 0 else 0.0
    roi = (bank - INITIAL_BANK) / INITIAL_BANK * 100
    pf = total_won / total_lost if total_lost > 0 else float("inf")

    return {
        "n_wallets": len(losers),
        "trades": total,
        "wins": wins,
        "win_rate": round(wr, 4),
        "roi": round(roi, 2),
        "pf": round(pf, 3),
        "max_dd": round(max_dd, 4),
        "final_equity": round(bank, 2),
    }


def run_sector_specialist_strategy(wallet_metrics_train: list[dict], df_test, min_sector_wr: float = 0.60) -> dict:
    """Стратегия sector specialist: следуем за wallet только в его лучшей категории."""
    # Для каждого wallet находим его лучшую категорию на train
    specialist_map: dict[str, str] = {}  # address -> best_category
    for w in wallet_metrics_train:
        if w["positions"] < MIN_POSITIONS:
            continue
        best_cat = None
        best_wr = 0.0
        for cat, stats in w["category_stats"].items():
            cat_total = stats["wins"] + stats["losses"]
            if cat_total >= 10 and stats["wr"] >= min_sector_wr and stats["wr"] > best_wr:
                best_cat = cat
                best_wr = stats["wr"]
        if best_cat:
            specialist_map[w["address"]] = best_cat

    if not specialist_map:
        return {"n_wallets": 0, "trades": 0}

    bank = INITIAL_BANK
    peak = INITIAL_BANK
    max_dd = 0.0
    wins = 0
    total = 0
    total_won = 0.0
    total_lost = 0.0

    for _, row in df_test.iterrows():
        addr = row["address"]
        if addr not in specialist_map:
            continue
        if row["category"] != specialist_map[addr]:
            continue
        if bank < FIXED_STAKE:
            break

        total += 1
        if row["is_win"]:
            avg_price = float(row["avg_price"]) if row["avg_price"] and row["avg_price"] > 0.01 else 0.5
            payout = FIXED_STAKE * (1.0 / avg_price - 1.0)
            bank += payout
            wins += 1
            total_won += payout
        else:
            bank -= FIXED_STAKE
            total_lost += FIXED_STAKE

        peak = max(peak, bank)
        dd = (peak - bank) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)

    wr = wins / total if total > 0 else 0.0
    roi = (bank - INITIAL_BANK) / INITIAL_BANK * 100
    pf = total_won / total_lost if total_lost > 0 else float("inf")

    return {
        "n_wallets": len(specialist_map),
        "trades": total,
        "wins": wins,
        "win_rate": round(wr, 4),
        "roi": round(roi, 2),
        "pf": round(pf, 3),
        "max_dd": round(max_dd, 4),
        "final_equity": round(bank, 2),
    }


# ---------------------------------------------------------------------------
# Grid search: threshold combinations
# ---------------------------------------------------------------------------

FOLLOW_GRID = {
    "min_positions": [20, 30, 50],
    "min_wr": [0.50, 0.55, 0.60],
    "min_roi": [0.0, 5.0, 10.0, 20.0],
}


def grid_search_follow(wallet_metrics_train: list[dict], df_test) -> list[dict]:
    """Grid search по порогам follow strategy."""
    import itertools

    results = []
    keys = list(FOLLOW_GRID.keys())
    values = [FOLLOW_GRID[k] for k in keys]

    for combo in itertools.product(*values):
        params = dict(zip(keys, combo))
        r = run_follow_strategy(
            wallet_metrics_train, df_test,
            min_positions=params["min_positions"],
            min_wr=params["min_wr"],
            min_roi=params["min_roi"],
        )
        if r["trades"] > 0:
            results.append({**params, **r})

    results.sort(key=lambda x: -x.get("roi", 0))
    return results


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="Walk-forward backtest на исторических Polymarket данных")
    parser.add_argument("--category", type=str, default=None, help="Фильтр по категории (Crypto, Politics, Sports, ...)")
    parser.add_argument("--output", type=str, default=None, help="Сохранить результаты в CSV")
    args = parser.parse_args()

    for f in [MAKER_PNL, TAKER_PNL, MARKET_ENRICHED]:
        if not os.path.exists(f):
            print(f"ОШИБКА: не найден {f}")
            sys.exit(1)

    con = duckdb.connect()
    con.execute("SET memory_limit='2GB'")

    # ── 1. Загрузка ──
    print("=" * 80)
    print("WALK-FORWARD BACKTEST — Polymarket Historical Dataset")
    print(f"Дата: {datetime.now().isoformat()}")
    if args.category:
        print(f"Категория: {args.category}")
    print("=" * 80)

    t0 = time.time()
    print("\n1. Загрузка PnL данных...", flush=True)
    df = load_data(con, args.category)
    print(f"   {len(df):,} resolved positions, {df['address'].nunique():,} wallets", flush=True)

    # ── 2. Split по периодам ──
    print("\n2. Split по периодам:", flush=True)
    splits = split_by_period(df, PERIODS)
    for name, split_df in splits.items():
        n_wallets = split_df["address"].nunique() if not split_df.empty else 0
        print(f"   {name}: {len(split_df):,} positions, {n_wallets:,} wallets")

    if splits["train"].empty:
        print("\nОШИБКА: train period пуст. Нет данных за 2024.")
        sys.exit(1)

    # ── 3. Wallet метрики на train ──
    print("\n3. Wallet метрики (train period)...", flush=True)
    train_metrics = compute_wallet_metrics(splits["train"])
    print(f"   {len(train_metrics):,} wallets с ≥5 positions", flush=True)

    profitable = [w for w in train_metrics if w["roi"] > 0]
    strong = [w for w in train_metrics if w["win_rate"] >= MIN_WIN_RATE and w["roi"] >= MIN_ROI and w["positions"] >= MIN_POSITIONS]
    print(f"   Profitable: {len(profitable):,} ({100*len(profitable)/max(len(train_metrics),1):.1f}%)")
    print(f"   Strong (WR≥{MIN_WIN_RATE}, ROI≥{MIN_ROI}%, pos≥{MIN_POSITIONS}): {len(strong):,}")

    # Top 10 by ROI
    train_metrics.sort(key=lambda x: -x["roi"])
    print("\n   Top 10 by ROI (train):")
    print(f"   {'Address':<14} {'Pos':>5} {'WR':>6} {'ROI%':>8} {'PnL$':>12} {'Sharpe':>7}")
    for w in train_metrics[:10]:
        print(f"   {w['address'][:12]}.. {w['positions']:>5} {w['win_rate']:>5.1%} {w['roi']:>7.1f}% ${w['total_pnl']:>10,.0f} {w['sharpe']:>7.2f}")

    # ── 4. Walk-forward: Train → Test ──
    print("\n" + "=" * 80)
    print("4. WALK-FORWARD: Train (2024) → Test (2025)")
    print("=" * 80)

    # 4a. Follow strategy
    print("\n4a. Follow Strategy (grid search):", flush=True)
    follow_results = grid_search_follow(train_metrics, splits["test"])

    if follow_results:
        print("\n   Top 5 конфигураций (test period):")
        print(f"   {'min_pos':>7} {'min_wr':>6} {'min_roi':>7} {'wallets':>7} {'trades':>6} {'WR':>6} {'ROI%':>8} {'PF':>6} {'maxDD':>6}")
        for r in follow_results[:5]:
            print(
                f"   {r['min_positions']:>7} {r['min_wr']:>5.0%} {r['min_roi']:>6.0f}% "
                f"{r['n_wallets']:>7} {r['trades']:>6} {r['win_rate']:>5.1%} "
                f"{r['roi']:>7.1f}% {r['pf']:>6.2f} {r['max_dd']:>5.1%}"
            )

        # DSR для лучшей конфигурации
        best_follow = follow_results[0]
        if best_follow["trades"] > 10 and best_follow.get("roi", 0) > 0:
            # Approximate Sharpe from ROI and trades
            approx_sharpe = best_follow["roi"] / max(best_follow["max_dd"] * 100, 1.0)
            dsr_p = deflated_sharpe_ratio(approx_sharpe, best_follow["trades"], len(follow_results))
            print(f"\n   DSR p-value (best follow): {dsr_p:.4f} {'✓ SIGNIFICANT' if dsr_p < 0.05 else '✗ not significant'}")
    else:
        print("   Нет прибыльных конфигураций.")

    # 4b. Contra strategy
    print("\n4b. Contra Strategy (fade worst wallets):", flush=True)
    contra = run_contra_strategy(train_metrics, splits["test"])
    if contra["trades"] > 0:
        print(f"   Wallets: {contra['n_wallets']}, Trades: {contra['trades']}, "
              f"WR: {contra['win_rate']:.1%}, ROI: {contra['roi']:.1f}%, PF: {contra['pf']:.2f}")
    else:
        print("   Нет suitable losers для contra.")

    # 4c. Sector specialist
    print("\n4c. Sector Specialist Strategy:", flush=True)
    specialist = run_sector_specialist_strategy(train_metrics, splits["test"])
    if specialist["trades"] > 0:
        print(f"   Wallets: {specialist['n_wallets']}, Trades: {specialist['trades']}, "
              f"WR: {specialist['win_rate']:.1%}, ROI: {specialist['roi']:.1f}%, PF: {specialist['pf']:.2f}")
    else:
        print("   Недостаточно specialist wallets.")

    # ── 5. Validate: Test → Validate (2026) ──
    if not splits["validate"].empty:
        print("\n" + "=" * 80)
        print("5. VALIDATION: лучшие стратегии на 2026 данных")
        print("=" * 80)

        # Валидация follow — лучшая конфигурация с теста
        if follow_results:
            best = follow_results[0]
            val_follow = run_follow_strategy(
                train_metrics, splits["validate"],
                min_positions=best["min_positions"],
                min_wr=best["min_wr"],
                min_roi=best["min_roi"],
            )
            print("\n   Follow (best from test):")
            if val_follow["trades"] > 0:
                print(f"   Trades: {val_follow['trades']}, WR: {val_follow['win_rate']:.1%}, "
                      f"ROI: {val_follow['roi']:.1f}%, PF: {val_follow['pf']:.2f}")
                # Overfit detection
                if best["roi"] > 0 and val_follow["roi"] < 0:
                    print("   ⚠️  OVERFIT: прибыльна на test, убыточна на validate!")
                elif val_follow["roi"] > 0:
                    print("   ✓ Стабильна: прибыльна и на test, и на validate")
            else:
                print("   Нет trades на validate period.")

        # Валидация contra
        val_contra = run_contra_strategy(train_metrics, splits["validate"])
        print("\n   Contra:")
        if val_contra["trades"] > 0:
            print(f"   Trades: {val_contra['trades']}, WR: {val_contra['win_rate']:.1%}, "
                  f"ROI: {val_contra['roi']:.1f}%, PF: {val_contra['pf']:.2f}")
        else:
            print("   Нет trades на validate period.")

        # Валидация sector specialist
        val_specialist = run_sector_specialist_strategy(train_metrics, splits["validate"])
        print("\n   Sector Specialist:")
        if val_specialist["trades"] > 0:
            print(f"   Trades: {val_specialist['trades']}, WR: {val_specialist['win_rate']:.1%}, "
                  f"ROI: {val_specialist['roi']:.1f}%, PF: {val_specialist['pf']:.2f}")
        else:
            print("   Нет trades на validate period.")
    else:
        print("\n5. Validate period пуст (нет данных 2026). Пропускаем.")

    # ── 6. Per-category breakdown ──
    print("\n" + "=" * 80)
    print("6. PER-CATEGORY BREAKDOWN (train → test)")
    print("=" * 80)

    categories = sorted(splits["train"]["category"].unique())
    print(f"\n   {'Category':<15} {'Train WR':>8} {'Test WR':>8} {'Train wallets':>13} {'Test trades':>11} {'Test ROI':>8}")
    print(f"   {'-'*15} {'-'*8} {'-'*8} {'-'*13} {'-'*11} {'-'*8}")

    for cat in categories:
        cat_train = splits["train"][splits["train"]["category"] == cat]
        cat_test = splits["test"][splits["test"]["category"] == cat]

        if cat_train.empty:
            continue

        train_wr = cat_train["is_win"].mean()
        test_wr = cat_test["is_win"].mean() if not cat_test.empty else 0
        n_wallets = cat_train["address"].nunique()

        # Прогнать follow для этой категории
        cat_metrics = compute_wallet_metrics(cat_train)
        cat_follow = run_follow_strategy(cat_metrics, cat_test, MIN_POSITIONS, MIN_WIN_RATE, MIN_ROI)

        test_roi_str = f"{cat_follow['roi']:.1f}%" if cat_follow["trades"] > 0 else "N/A"

        print(f"   {cat:<15} {train_wr:>7.1%} {test_wr:>7.1%} {n_wallets:>13,} {cat_follow['trades']:>11} {test_roi_str:>8}")

    # ── 7. Stress periods ──
    print("\n" + "=" * 80)
    print("7. STRESS PERIODS")
    print("=" * 80)

    import pandas as pd

    stress_periods = {
        "US Elections 2024": ("2024-10-01", "2024-11-15"),
        "Post-Election 2024": ("2024-11-15", "2024-12-31"),
        "Q1 2025": ("2025-01-01", "2025-03-31"),
    }

    for name, (start, end) in stress_periods.items():
        start_dt = pd.Timestamp(start, tz="UTC")
        end_dt = pd.Timestamp(end, tz="UTC")
        mask = (df["end_date"] >= start_dt) & (df["end_date"] <= end_dt)
        period_df = df[mask]

        if period_df.empty:
            print(f"\n   {name}: нет данных")
            continue

        n_pos = len(period_df)
        n_wallets = period_df["address"].nunique()
        avg_wr = period_df["is_win"].mean()
        total_pnl = period_df["realized_pnl"].sum()

        # Follow на stress period
        stress_follow = run_follow_strategy(train_metrics, period_df, MIN_POSITIONS, MIN_WIN_RATE, MIN_ROI)

        print(f"\n   {name}:")
        print(f"   Positions: {n_pos:,}, Wallets: {n_wallets:,}, Avg WR: {avg_wr:.1%}, Total PnL: ${total_pnl:,.0f}")
        if stress_follow["trades"] > 0:
            print(f"   Follow: {stress_follow['trades']} trades, WR {stress_follow['win_rate']:.1%}, ROI {stress_follow['roi']:.1f}%")

    # ── Summary ──
    elapsed = time.time() - t0
    print(f"\n{'='*80}")
    print(f"Завершено за {elapsed:.0f}s")
    print(f"{'='*80}")

    # ── Save results ──
    if args.output:
        import csv as csv_mod

        rows = []
        if follow_results:
            for r in follow_results:
                rows.append({"strategy": "follow", **r})
        if contra["trades"] > 0:
            rows.append({"strategy": "contra", **contra})
        if specialist["trades"] > 0:
            rows.append({"strategy": "sector_specialist", **specialist})

        if rows:
            with open(args.output, "w", newline="") as f:
                writer = csv_mod.DictWriter(f, fieldnames=rows[0].keys())
                writer.writeheader()
                writer.writerows(rows)
            print(f"Результаты: {args.output}")


if __name__ == "__main__":
    main()

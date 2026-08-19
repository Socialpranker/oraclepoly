#!/usr/bin/env python3
"""Anti-smart-money detection — Phase 3.B step.

Цель: найти стабильно проигрывающие кошельки (n_trades ≥ 100, pnl < threshold,
loss-stability ≥ threshold) для использования как contra-signal в новой стратегии
fade_retail.

Логика:
1. Читает wallet_scores.parquet (existing, 340K wallets) как baseline.
2. Filters: n_trades ≥ 100 AND total_pnl ≤ -1000 USD.
3. Optional stability check: загружает trades этих кошельков из quant.parquet,
   разбивает на first/second half by time, считает correlation между losses
   в обеих половинах. corr > 0.5 → stable loser (не unlucky).
4. Output: losing_wallets.parquet с stable_loser flag.

Verdict для Phase 3:
- ≥1000 stable losers → fade signal достаточно силён, proceed Task 3.D
- <1000 → fade signal слишком слаб, abort

Usage:
    python3 scripts/research/anti_smart_money_analysis.py \
      --scores /mnt/data/polymarket_dataset/wallet_scores.parquet \
      --quant /mnt/data/hf_data/quant.parquet \
      --out-parquet /mnt/data/polymarket_dataset/losing_wallets.parquet \
      --out-md docs/research/2026-04-30-anti-smart-money.md
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd

# ────────────────────────────────────────────────────────────────────────────
# Constants
# ────────────────────────────────────────────────────────────────────────────

MIN_TRADES = 100
MIN_LOSS_USD = 1000.0  # минимальный убыток для рассмотрения
STABILITY_THRESHOLD = 0.5  # корреляция losses first/second half
PROMOTION_MIN_STABLE_LOSERS = 1000


# ────────────────────────────────────────────────────────────────────────────
# Filtering
# ────────────────────────────────────────────────────────────────────────────

def filter_loser_candidates(
    df_scores: pd.DataFrame,
    min_trades: int = MIN_TRADES,
    min_loss_usd: float = MIN_LOSS_USD,
) -> pd.DataFrame:
    """Subset wallets с n_trades ≥ min_trades AND total_pnl ≤ -min_loss_usd."""
    if "total_pnl" not in df_scores.columns:
        raise ValueError("wallet_scores.parquet missing 'total_pnl' column")
    if "total_trades" not in df_scores.columns:
        raise ValueError("wallet_scores.parquet missing 'total_trades' column")

    mask = (df_scores["total_trades"] >= min_trades) & (df_scores["total_pnl"] <= -min_loss_usd)
    return df_scores[mask].copy()


# ────────────────────────────────────────────────────────────────────────────
# Stability check
# ────────────────────────────────────────────────────────────────────────────

def compute_stability_for_wallet(
    con: duckdb.DuckDBPyConnection,
    wallet: str,
    quant_path: str,
) -> float | None:
    """Вернуть корреляцию между losses_per_market в first half vs second half времени.

    Returns None если не хватает trades для splitting.
    """
    # Достаём trades wallet'а как taker (для simplicity, основная активность retail = takers)
    rows = con.execute(
        f"""
        SELECT timestamp, condition_id, side, usd_amount, price
        FROM read_parquet('{quant_path}')
        WHERE taker = '{wallet}'
        ORDER BY timestamp
        """
    ).fetchall()

    if len(rows) < 50:
        return None

    df = pd.DataFrame(rows, columns=["timestamp", "condition_id", "side", "usd_amount", "price"])
    if len(df) < 50:
        return None

    # Split по медиане timestamp
    median_ts = df["timestamp"].median()
    df_first = df[df["timestamp"] < median_ts]
    df_second = df[df["timestamp"] >= median_ts]

    if len(df_first) < 25 or len(df_second) < 25:
        return None

    # Простейшая proxy для loss: usd_amount * (1 - price for YES side, price for NO side)
    # Без exact resolution outcome compute точный PnL невозможен per-trade, но
    # корреляция волатильности losses между двумя половинами — sufficient proxy.

    # На каждой половине: средний "loss proxy" = sum(usd_amount * proxy_loss)
    # proxy_loss[YES] = (1 - price), proxy_loss[NO] = price (предполагаем YES outcome random ~50%)
    df_first["loss_proxy"] = df_first["usd_amount"] * df_first["price"].apply(
        lambda p: max(p, 1 - p)
    )
    df_second["loss_proxy"] = df_second["usd_amount"] * df_second["price"].apply(
        lambda p: max(p, 1 - p)
    )

    # Корреляция между market-level losses в двух половинах
    first_by_mkt = df_first.groupby("condition_id")["loss_proxy"].sum()
    second_by_mkt = df_second.groupby("condition_id")["loss_proxy"].sum()
    common = first_by_mkt.index.intersection(second_by_mkt.index)

    if len(common) < 5:
        # Не overlap-ит markets между половинами — explicitly low corr
        return 0.0

    corr = first_by_mkt.loc[common].corr(second_by_mkt.loc[common])
    return float(corr) if pd.notna(corr) else 0.0


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--scores",
        default="/mnt/data/polymarket_dataset/wallet_scores.parquet",
        help="Path к wallet_scores.parquet (baseline)",
    )
    parser.add_argument(
        "--quant",
        default="/mnt/data/hf_data/quant.parquet",
        help="Path к quant.parquet для stability check",
    )
    parser.add_argument(
        "--out-parquet",
        default="/mnt/data/polymarket_dataset/losing_wallets.parquet",
        help="Output losing wallets parquet",
    )
    parser.add_argument(
        "--out-md",
        default="docs/research/2026-04-30-anti-smart-money.md",
        help="Output markdown report",
    )
    parser.add_argument(
        "--max-stability-checks",
        type=int,
        default=200,
        help="Max wallets для stability check (full может быть медленным)",
    )
    parser.add_argument(
        "--skip-stability",
        action="store_true",
        help="Пропустить stability check (быстро, но без stable_loser flag)",
    )
    args = parser.parse_args()

    if not os.path.exists(args.scores):
        print(f"ERROR: wallet_scores not found: {args.scores}")
        sys.exit(1)

    # Step 1: Filter candidates
    print(f"Loading {args.scores}...")
    df_scores = pd.read_parquet(args.scores)
    print(f"  Total wallets: {len(df_scores):,}")

    print(f"\nFiltering loser candidates (n_trades ≥ {MIN_TRADES}, pnl ≤ -${MIN_LOSS_USD:.0f})...")
    candidates = filter_loser_candidates(df_scores)
    print(f"  Candidates: {len(candidates):,}")

    if len(candidates) == 0:
        print("⚠️ No candidates — Phase 3.B abort")
        sys.exit(2)

    # Step 2: Stability check
    if not args.skip_stability and os.path.exists(args.quant):
        print(f"\nStability check on top-{args.max_stability_checks} candidates...")
        con = duckdb.connect()
        con.execute("SET memory_limit='2GB';")
        con.execute("SET threads=2;")

        # Сортируем по pnl (самые большие потери — самые "интересные")
        candidates_sorted = candidates.nsmallest(args.max_stability_checks, "total_pnl")
        stability_corrs = []
        for idx, row in candidates_sorted.iterrows():
            wallet = row["address"]
            corr = compute_stability_for_wallet(con, wallet, args.quant)
            stability_corrs.append(corr)
            if (len(stability_corrs)) % 50 == 0:
                print(f"  Checked {len(stability_corrs)}/{args.max_stability_checks}")

        candidates_sorted["stability_corr"] = stability_corrs
        candidates_sorted["stable_loser"] = candidates_sorted["stability_corr"].apply(
            lambda c: c is not None and c >= STABILITY_THRESHOLD
        )

        # Merge back: для остальных stability=None
        rest = candidates[~candidates["address"].isin(candidates_sorted["address"])].copy()
        rest["stability_corr"] = None
        rest["stable_loser"] = False

        result = pd.concat([candidates_sorted, rest], ignore_index=True)
    else:
        print("  Skipping stability check (--skip-stability or quant.parquet missing)")
        candidates["stability_corr"] = None
        candidates["stable_loser"] = False
        result = candidates

    # Step 3: Save parquet
    out_pq = Path(args.out_parquet)
    out_pq.parent.mkdir(parents=True, exist_ok=True)
    result.to_parquet(out_pq, compression="snappy", index=False)

    n_total = len(result)
    n_stable = int(result["stable_loser"].sum())

    # Step 4: Render report
    out_md = Path(args.out_md)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    lines = []
    lines.append(f"# Anti-Smart-Money Detection — {pd.Timestamp.now().date()}")
    lines.append("")
    lines.append(f"**Источник:** `{args.scores}` ({len(df_scores):,} total wallets)")
    lines.append("")
    lines.append("## Verdict")
    lines.append("")
    if n_stable >= PROMOTION_MIN_STABLE_LOSERS:
        lines.append(f"✅ **PROCEED** — {n_stable:,} stable losers найдено (≥{PROMOTION_MIN_STABLE_LOSERS}).")
        lines.append("Fade signal достаточно силён. Proceed to Task 3.D (fade_retail strategy).")
    else:
        lines.append(f"⚠️ **WEAK SIGNAL** — {n_stable:,} stable losers (нужно ≥{PROMOTION_MIN_STABLE_LOSERS}).")
        lines.append("Fade signal слабоват. Можно расширить max-stability-checks или ждать данные.")
    lines.append("")
    lines.append(f"- **Total candidates:** {n_total:,} (n_trades ≥ {MIN_TRADES}, pnl ≤ -${MIN_LOSS_USD:.0f})")
    lines.append(f"- **Stable losers:** {n_stable:,} (corr ≥ {STABILITY_THRESHOLD})")
    lines.append(f"- **Output:** `{out_pq}`")
    lines.append("")
    lines.append("## Top-10 by loss")
    lines.append("")
    lines.append("| Wallet | n_trades | total_pnl | win_rate | stability | stable? |")
    lines.append("|---|---|---|---|---|---|")
    for _, row in result.nsmallest(10, "total_pnl").iterrows():
        wallet_short = row["address"][:8] + "..." + row["address"][-6:]
        stab = f"{row['stability_corr']:.3f}" if pd.notna(row.get("stability_corr")) else "-"
        flag = "✅" if row["stable_loser"] else ""
        lines.append(
            f"| `{wallet_short}` | {int(row['total_trades'])} | "
            f"-${abs(row['total_pnl']):,.0f} | {row['win_rate']:.3f} | {stab} | {flag} |"
        )
    out_md.write_text("\n".join(lines))

    print(f"\nReport: {out_md}")
    print(f"Parquet: {out_pq}")
    print(f"Total candidates: {n_total:,}, Stable losers: {n_stable:,}")
    if n_stable < PROMOTION_MIN_STABLE_LOSERS:
        sys.exit(2)


if __name__ == "__main__":
    main()

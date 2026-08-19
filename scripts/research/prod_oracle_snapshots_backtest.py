"""Backtest реального прод-oracle на resolved oracle_snapshots.

В отличие от historical_pit_loader.py / historical_oracle_sanity_check.py,
которые реконструировали oracle_score из HF данных (урезанная модель из 4 факторов),
здесь мы работаем с **реальными** snapshot'ами из прод БД, где oracle_score
посчитан production pipeline в момент T со всеми активными факторами.

Цель: ответить на главный вопрос сессии — "работает ли прод-oracle как торговый
сигнал" без реконструкционных proxy'ев.

Данные:
    data/prod_oracle_snapshots.parquet (367,173 snapshot'а, 2026-02-24 → 2026-04-10)
    Источник: scripts/research/prod_oracle_snapshots_dump.py

⚠️ CAVEAT по factor coverage (2026-04-10):
- whale_contribution_raw: 100% ✓
- news_contribution_raw:  100% ✓
- sm_registry_raw:          3.7% 🔴
- sports_odds_raw:          0.1% 🔴
- retail/maker/taker:       ~0%  🔴
→ Это тест oracle_score в его ФАКТИЧЕСКОЙ прод-конфигурации (market + whale + news),
  не декларируемой из документации (10 факторов).
  См. memory/project_factor_coverage_broken.md.

Методология:
- Side = YES если oracle_score >= 0.5 иначе NO
- Win = (side == "YES" and outcome == "yes") or (side == "NO" and outcome == "no")
- PnL per $1 = (1/entry - 1) если win, -1 иначе, где entry = market_price если YES,
  иначе (1 - market_price)
- Baselines A0-A6 как в historical_oracle_sanity_check.py (market_follower, always_NO,
  always_YES, random, low_price_YES, anti_oracle)
- Дополнительно: срезы GLOBAL / Sports / Crypto / Politics (остальные категории <5%)

Использование:
    python3 scripts/research/prod_oracle_snapshots_backtest.py
"""

import argparse
import math
import random
from pathlib import Path

import duckdb

DEFAULT_PIT = Path(__file__).parent.parent.parent / "data" / "prod_oracle_snapshots.parquet"
DEFAULT_OUT = Path(__file__).parent.parent.parent / "docs/research/2026-04-10-prod-oracle-backtest.md"


def pnl_for_side(side: str, market_price: float, outcome_yes: bool) -> tuple[float, bool]:
    """PnL per $1 stake для бинарного рынка."""
    if side == "YES":
        win = outcome_yes
    else:
        win = not outcome_yes
    if not win:
        return -1.0, False
    entry = market_price if side == "YES" else (1.0 - market_price)
    entry = max(entry, 0.01)
    return (1.0 / entry) - 1.0, True


def compute_stats(pnls: list[float], wins: list[bool]) -> dict:
    n = len(pnls)
    if n == 0:
        return {"n": 0, "wr": 0.0, "roi": 0.0, "sharpe": 0.0, "median": 0.0}
    total = sum(pnls)
    roi = total / n
    wr = sum(wins) / n
    if n >= 2:
        mean = roi
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        std = math.sqrt(var)
        sharpe = roi / std if std > 0 else 0.0
    else:
        sharpe = 0.0
    sorted_pnls = sorted(pnls)
    median = sorted_pnls[n // 2]
    return {"n": n, "wr": wr, "roi": roi, "sharpe": sharpe, "median": median}


def evaluate_slice(slice_rows: list[dict], seed: int = 42) -> dict[str, dict]:
    """Посчитать oracle + baseline'ы на slice'е."""
    strategies: dict[str, dict] = {}

    # Oracle: side по oracle_score >= 0.5
    oracle_pnls, oracle_wins = [], []
    for r in slice_rows:
        mp = r["market_price"]
        outcome_yes = r["outcome"] == "yes"
        score = r["oracle_score"]
        side = "YES" if score >= 0.5 else "NO"
        pnl, win = pnl_for_side(side, mp, outcome_yes)
        oracle_pnls.append(pnl)
        oracle_wins.append(win)
    strategies["ORACLE_prod"] = compute_stats(oracle_pnls, oracle_wins)

    # Oracle с divergence-фильтром >= 0.03 (как реальные стратегии)
    div_pnls, div_wins = [], []
    for r in slice_rows:
        div = r.get("divergence") or 0.0
        if abs(div) < 0.03:
            continue
        mp = r["market_price"]
        outcome_yes = r["outcome"] == "yes"
        score = r["oracle_score"]
        side = "YES" if score >= 0.5 else "NO"
        pnl, win = pnl_for_side(side, mp, outcome_yes)
        div_pnls.append(pnl)
        div_wins.append(win)
    strategies["ORACLE_div_3pct"] = compute_stats(div_pnls, div_wins)

    # Baseline'ы
    rng = random.Random(seed)
    baselines = {
        "A0_market_follower": [],
        "A1_always_NO": [],
        "A2_always_YES": [],
        "A3_random_50_50": [],
        "A4_low_price_YES": [],
        "A6_anti_oracle": [],
    }
    baseline_wins = {k: [] for k in baselines}

    for r in slice_rows:
        mp = r["market_price"]
        outcome_yes = r["outcome"] == "yes"
        score = r["oracle_score"]
        oracle_side = "YES" if score >= 0.5 else "NO"

        sides = {
            "A0_market_follower": "YES" if mp > 0.5 else "NO",
            "A1_always_NO": "NO",
            "A2_always_YES": "YES",
            "A3_random_50_50": "YES" if rng.random() < 0.5 else "NO",
            "A4_low_price_YES": "YES" if mp < 0.5 else "NO",
            "A6_anti_oracle": "NO" if oracle_side == "YES" else "YES",
        }
        for name, side in sides.items():
            pnl, win = pnl_for_side(side, mp, outcome_yes)
            baselines[name].append(pnl)
            baseline_wins[name].append(win)

    for name in baselines:
        strategies[name] = compute_stats(baselines[name], baseline_wins[name])

    return strategies


def format_table(strategies: dict[str, dict]) -> list[str]:
    rows = [(name, s) for name, s in strategies.items()]
    rows.sort(key=lambda x: -x[1]["roi"])

    lines = []
    lines.append("| Strategy | N | WR | ROI | Sharpe/t | Median |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for name, s in rows:
        highlight = ""
        if name.startswith("ORACLE"):
            highlight = "**"
        lines.append(
            f"| {highlight}{name}{highlight} | {s['n']:,} | {s['wr']:.1%} | "
            f"{s['roi']:+.4f} | {s['sharpe']:+.2f} | {s['median']:+.4f} |"
        )
    return lines


def verdict_for_slice(strategies: dict[str, dict]) -> tuple[str, str]:
    """Вердикт: бьёт ли oracle best baseline на +2%?"""
    n = strategies["ORACLE_prod"]["n"]
    if n < 500:
        return "⚪", f"N={n} < 500, недостаточно данных"

    baseline_rois = [(name, strategies[name]["roi"]) for name in strategies if name.startswith("A")]
    best_name, best_roi = max(baseline_rois, key=lambda x: x[1])

    oracle_roi = strategies["ORACLE_prod"]["roi"]
    div_roi = strategies["ORACLE_div_3pct"]["roi"]

    oracle_edge = oracle_roi - best_roi
    div_edge = div_roi - best_roi

    results = []
    if oracle_edge >= 0.02:
        results.append(f"🟢 ORACLE_prod бьёт {best_name} на +{oracle_edge:.4f}")
    if div_edge >= 0.02:
        results.append(f"🟢 ORACLE_div_3pct бьёт {best_name} на +{div_edge:.4f}")

    if results:
        return "🟢", "; ".join(results)

    return (
        "⚪",
        f"ORACLE_prod ROI={oracle_roi:+.4f} (edge={oracle_edge:+.4f}), "
        f"div_3pct ROI={div_roi:+.4f} (edge={div_edge:+.4f}) "
        f"vs best {best_name}={best_roi:+.4f}",
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pit", type=str, default=str(DEFAULT_PIT))
    parser.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    args = parser.parse_args()

    print("=" * 70)
    print("PROD ORACLE_SNAPSHOTS BACKTEST")
    print("=" * 70)
    print(f"Source: {args.pit}")

    con = duckdb.connect()
    raw = con.execute(f"""
        SELECT
            market_id, category, oracle_score, market_price, outcome,
            divergence, divergence_signal, created_at, resolved_at
        FROM read_parquet('{args.pit}')
        WHERE market_price > 0.01 AND market_price < 0.99
    """).fetchall()

    cols = ["market_id", "category", "oracle_score", "market_price", "outcome",
            "divergence", "divergence_signal", "created_at", "resolved_at"]
    rows = [dict(zip(cols, r, strict=True)) for r in raw]

    # Конвертируем numeric → float (в oracle_snapshots они NUMERIC → Decimal)
    for row in rows:
        row["oracle_score"] = float(row["oracle_score"])
        row["market_price"] = float(row["market_price"])
        if row["divergence"] is not None:
            row["divergence"] = float(row["divergence"])

    print(f"\n[1/3] Loaded {len(rows):,} snapshots (excluding boundary markets)")

    # Срезы
    slices = {
        "GLOBAL": rows,
        "Sports": [r for r in rows if r["category"] == "Sports"],
        "Crypto": [r for r in rows if r["category"] == "Crypto"],
        "Politics": [r for r in rows if r["category"] == "Politics"],
        "Price_neutral_30_70": [r for r in rows if 0.30 <= r["market_price"] <= 0.70],
        "Strong_divergence": [r for r in rows if r.get("divergence") is not None and abs(r["divergence"]) >= 0.05],
    }

    print("\n[2/3] Evaluating baseline matrix per slice")
    for name, srows in slices.items():
        print(f"      {name:<25} {len(srows):>8,} rows")

    slice_results = {name: evaluate_slice(srows) for name, srows in slices.items()}

    # Markdown отчёт
    print(f"\n[3/3] Writing report → {args.out}")
    md = []
    md.append("# Prod Oracle Backtest — реальные snapshot'ы")
    md.append("")
    md.append(f"**Источник:** `{args.pit}`")
    md.append(f"**N snapshots:** {len(rows):,}")
    md.append("**Дата прогона:** 2026-04-10")
    md.append("**Скрипт:** `scripts/research/prod_oracle_snapshots_backtest.py`")
    md.append("")
    md.append("## ⚠️ Factor coverage caveat")
    md.append("")
    md.append("Прод oracle_score в этом сэмпле посчитан **фактически на 2 факторах** из 10:")
    md.append("- `whale_contribution_raw`: 100% ✓")
    md.append("- `news_contribution_raw`: 100% ✓")
    md.append("- `sm_registry_raw`: 3.7% 🔴")
    md.append("- `sports_odds_raw`: 0.1% 🔴")
    md.append("- `retail/maker/taker`: ~0% 🔴")
    md.append("")
    md.append("Это тест **реальной рабочей модели**, не декларируемой. См. `memory/project_factor_coverage_broken.md`.")
    md.append("")
    md.append("## Summary по срезам")
    md.append("")
    md.append("| Slice | N | ORACLE ROI | ORACLE_div ROI | Best baseline | Best ROI | Verdict |")
    md.append("|---|---:|---:|---:|---|---:|---|")
    for slice_name, strategies in slice_results.items():
        n = strategies["ORACLE_prod"]["n"]
        if n == 0:
            md.append(f"| {slice_name} | 0 | - | - | - | - | _empty_ |")
            continue
        oracle_roi = strategies["ORACLE_prod"]["roi"]
        div_roi = strategies["ORACLE_div_3pct"]["roi"]
        baseline_rois = [(name, strategies[name]["roi"]) for name in strategies if name.startswith("A")]
        best_name, best_roi = max(baseline_rois, key=lambda x: x[1])
        flag, _ = verdict_for_slice(strategies)
        md.append(
            f"| {slice_name} | {n:,} | {oracle_roi:+.4f} | {div_roi:+.4f} | "
            f"{best_name} | {best_roi:+.4f} | {flag} |"
        )
    md.append("")

    md.append("## Детали по срезам")
    md.append("")
    for slice_name, strategies in slice_results.items():
        md.append(f"### {slice_name}")
        md.append("")
        n = strategies["ORACLE_prod"]["n"]
        md.append(f"**N = {n:,}** snapshots")
        md.append("")
        if n == 0:
            md.append("_Empty slice_")
            md.append("")
            continue
        flag, text = verdict_for_slice(strategies)
        md.append(f"**Verdict:** {flag} {text}")
        md.append("")
        md.extend(format_table(strategies))
        md.append("")

    Path(args.out).parent.mkdir(exist_ok=True)
    with open(args.out, "w") as f:
        f.write("\n".join(md))

    # Console summary
    print("\n" + "=" * 70)
    print("QUICK SUMMARY")
    print("=" * 70)
    print(f"  {'Slice':<25} {'N':>8}  {'ORACLE':>9}  {'ORACLE_div':>11}  {'Best bl':>9}  {'Flag':>4}")
    print("  " + "-" * 78)
    for slice_name, strategies in slice_results.items():
        n = strategies["ORACLE_prod"]["n"]
        if n == 0:
            print(f"  {slice_name:<25} {'empty':>8}")
            continue
        oracle_roi = strategies["ORACLE_prod"]["roi"]
        div_roi = strategies["ORACLE_div_3pct"]["roi"]
        baseline_rois = [(name, strategies[name]["roi"]) for name in strategies if name.startswith("A")]
        _, best_roi = max(baseline_rois, key=lambda x: x[1])
        flag, _ = verdict_for_slice(strategies)
        print(
            f"  {slice_name:<25} {n:>8,}  {oracle_roi:>+9.4f}  {div_roi:>+11.4f}  "
            f"{best_roi:>+9.4f}  {flag}"
        )

    print(f"\nReport → {args.out}")


if __name__ == "__main__":
    main()

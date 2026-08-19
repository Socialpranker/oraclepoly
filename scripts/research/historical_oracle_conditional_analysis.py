"""Historical Oracle Conditional Analysis — есть ли у oracle edge хоть где-то?

После того как sanity check показал, что глобально ни prod, ни research oracle
не бьют простые baseline'ы, нужно ответить на более точный вопрос:

  "Существует ли slice данных, где oracle реально даёт edge?"

Мы разбиваем PIT dataset на срезы и в каждом срезе сравниваем oracle-модели
с baseline'ами. Если хоть в одном срезе oracle устойчиво бьёт best-baseline —
значит там есть сигнал, и его надо развивать. Если нет нигде — значит проблема
не в калибровке, а в отсутствии информации в текущих факторах.

Срезы:

  S1: price-neutral       market_price ∈ [0.30, 0.70]  (нет longshot бонуса)
  S2: price-strict        market_price ∈ [0.40, 0.60]  (ещё строже)
  S3: late-phase          phase == "late"              (horizon ≤3d)
  S4: neutral + late      S1 ∩ S3                       (самый честный тест)
  S5: volume_low          total_volume_usd ∈ [10K, 50K]
  S6: volume_mid          total_volume_usd ∈ [50K, 500K]
  S7: volume_high         total_volume_usd ≥ 500K
  S8: big divergence      |divergence| ≥ 0.05 (где стратегии реально входят)

Для каждого среза считаем:

  Стратегии:
    prod_oracle          исходные side_prod, prod_pnl, prod_win
    research_oracle      исходные side_research, research_pnl, research_win
    A0_market_follower   side = "YES" if mp > 0.5 else "NO"
    A1_always_NO
    A2_always_YES
    A3_random_50_50      seed=42, воспроизводимо
    A4_low_price_YES     side = "YES" if mp < 0.5 else "NO"
    A5_anti_research     inverse of side_research
    A6_anti_prod         inverse of side_prod

  Метрики: N, WR, ROI, Sharpe (per-trade), median PnL.

Вердикт на slice:
  - N < 500 → недостаточно данных
  - prod_roi > best_baseline_roi + 0.02 → 🟢 prod работает в slice'е
  - research_roi > best_baseline_roi + 0.02 → 🟢 research работает в slice'е
  - иначе → ⚪ нет edge

Запуск:
    python3 scripts/research/historical_oracle_conditional_analysis.py \
        --pit /mnt/data/polymarket_dataset/pit_dataset.parquet \
        --out /mnt/data/polymarket_dataset/analysis/conditional_analysis.md
"""

import argparse
import math
import os
import random

import duckdb

DEFAULT_PIT = "/mnt/data/polymarket_dataset/pit_dataset.parquet"
DEFAULT_OUT = "/mnt/data/polymarket_dataset/analysis/conditional_analysis.md"

MIN_N_FOR_VERDICT = 500
EDGE_THRESHOLD = 0.02  # oracle должен бить best baseline минимум на +2% ROI


# ────────────────────────────────────────────────────────────────────────────
# PnL helper
# ────────────────────────────────────────────────────────────────────────────

def pnl_for_side(side: str, market_price: float, winner_is_yes: bool) -> tuple[float, bool]:
    if side == "YES":
        win = winner_is_yes
    else:
        win = not winner_is_yes
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


# ────────────────────────────────────────────────────────────────────────────
# Slice definitions
# ────────────────────────────────────────────────────────────────────────────

def build_slices(rows: list[dict]) -> dict[str, list[dict]]:
    slices: dict[str, list[dict]] = {
        "GLOBAL": rows,
        "S1_price_neutral_30_70": [r for r in rows if 0.30 <= r["market_price"] <= 0.70],
        "S2_price_strict_40_60": [r for r in rows if 0.40 <= r["market_price"] <= 0.60],
        "S3_late_phase": [r for r in rows if r["phase"] == "late"],
        "S4_neutral_and_late": [
            r for r in rows if 0.30 <= r["market_price"] <= 0.70 and r["phase"] == "late"
        ],
        "S5_volume_low_10K_50K": [
            r for r in rows if 10_000 <= r["total_volume_usd"] < 50_000
        ],
        "S6_volume_mid_50K_500K": [
            r for r in rows if 50_000 <= r["total_volume_usd"] < 500_000
        ],
        "S7_volume_high_500K_plus": [
            r for r in rows if r["total_volume_usd"] >= 500_000
        ],
        "S8_big_divergence": [
            r for r in rows if abs(r["div_prod"]) >= 0.05 or abs(r["div_research"]) >= 0.05
        ],
    }
    return slices


# ────────────────────────────────────────────────────────────────────────────
# Strategy evaluation on a slice
# ────────────────────────────────────────────────────────────────────────────

def evaluate_slice(slice_rows: list[dict], seed: int = 42) -> dict[str, dict]:
    """Посчитать все стратегии на slice'е."""
    strategies = {}

    # Oracle модели (берём из исходных данных)
    prod_pnls = [r["prod_pnl"] for r in slice_rows]
    prod_wins = [r["prod_win"] for r in slice_rows]
    strategies["PROD_oracle"] = compute_stats(prod_pnls, prod_wins)

    research_pnls = [r["research_pnl"] for r in slice_rows]
    research_wins = [r["research_win"] for r in slice_rows]
    strategies["RESEARCH_oracle"] = compute_stats(research_pnls, research_wins)

    # Baseline'ы
    rng = random.Random(seed)
    baselines = {
        "A0_market_follower": [],
        "A1_always_NO": [],
        "A2_always_YES": [],
        "A3_random_50_50": [],
        "A4_low_price_YES": [],
        "A5_anti_research": [],
        "A6_anti_prod": [],
    }
    baseline_wins = {k: [] for k in baselines}

    for r in slice_rows:
        mp = r["market_price"]
        winner_yes = r["winner_is_yes"]

        sides = {
            "A0_market_follower": "YES" if mp > 0.5 else "NO",
            "A1_always_NO": "NO",
            "A2_always_YES": "YES",
            "A3_random_50_50": "YES" if rng.random() < 0.5 else "NO",
            "A4_low_price_YES": "YES" if mp < 0.5 else "NO",
            "A5_anti_research": "NO" if r["side_research"] == "YES" else "YES",
            "A6_anti_prod": "NO" if r["side_prod"] == "YES" else "YES",
        }
        for name, side in sides.items():
            pnl, win = pnl_for_side(side, mp, winner_yes)
            baselines[name].append(pnl)
            baseline_wins[name].append(win)

    for name in baselines:
        strategies[name] = compute_stats(baselines[name], baseline_wins[name])

    return strategies


# ────────────────────────────────────────────────────────────────────────────
# Verdict for a slice
# ────────────────────────────────────────────────────────────────────────────

def verdict_for_slice(slice_name: str, strategies: dict[str, dict]) -> tuple[str, str]:
    """Вернуть (emoji_flag, verdict_text)."""
    n = strategies["PROD_oracle"]["n"]
    if n < MIN_N_FOR_VERDICT:
        return "⚪", f"N={n} (< {MIN_N_FOR_VERDICT}, недостаточно данных)"

    baseline_names = [name for name in strategies if name.startswith("A")]
    baseline_rois = [(name, strategies[name]["roi"]) for name in baseline_names]
    best_baseline_name, best_baseline_roi = max(baseline_rois, key=lambda x: x[1])

    prod_roi = strategies["PROD_oracle"]["roi"]
    research_roi = strategies["RESEARCH_oracle"]["roi"]

    prod_edge = prod_roi - best_baseline_roi
    research_edge = research_roi - best_baseline_roi

    results = []
    if prod_edge >= EDGE_THRESHOLD:
        results.append(f"🟢 PROD бьёт best_baseline ({best_baseline_name}) на +{prod_edge:.4f}")
    if research_edge >= EDGE_THRESHOLD:
        results.append(f"🟢 RESEARCH бьёт best_baseline ({best_baseline_name}) на +{research_edge:.4f}")

    if results:
        return "🟢", "; ".join(results)

    return (
        "⚪",
        f"Ни prod (ROI={prod_roi:+.4f}, edge={prod_edge:+.4f}), "
        f"ни research (ROI={research_roi:+.4f}, edge={research_edge:+.4f}) "
        f"не бьют best_baseline={best_baseline_name} (ROI={best_baseline_roi:+.4f})",
    )


# ────────────────────────────────────────────────────────────────────────────
# Markdown report
# ────────────────────────────────────────────────────────────────────────────

def format_slice_section(slice_name: str, strategies: dict[str, dict]) -> list[str]:
    lines = []
    n = strategies["PROD_oracle"]["n"]
    lines.append(f"### {slice_name}")
    lines.append("")
    lines.append(f"**N = {n:,}** snapshots")
    lines.append("")
    if n == 0:
        lines.append("_Empty slice_")
        lines.append("")
        return lines

    flag, text = verdict_for_slice(slice_name, strategies)
    lines.append(f"**Verdict:** {flag} {text}")
    lines.append("")

    # Ranking table (sorted by ROI desc)
    rows = [(name, s) for name, s in strategies.items()]
    rows.sort(key=lambda x: -x[1]["roi"])

    lines.append("| Strategy | N | WR | ROI | Sharpe/t | Median |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for name, s in rows:
        highlight = ""
        if name in ("PROD_oracle", "RESEARCH_oracle"):
            highlight = "**"
        lines.append(
            f"| {highlight}{name}{highlight} | {s['n']:,} | {s['wr']:.1%} | "
            f"{s['roi']:+.4f} | {s['sharpe']:+.2f} | {s['median']:+.4f} |"
        )
    lines.append("")
    return lines


def format_report(slice_results: dict[str, dict[str, dict]]) -> str:
    lines = []
    lines.append("# Historical Oracle Conditional Analysis")
    lines.append("")
    lines.append("**Автор:** `scripts/research/historical_oracle_conditional_analysis.py`")
    lines.append("**Цель:** ответить на вопрос «есть ли у oracle edge хоть в одном срезе данных»")
    lines.append("")
    lines.append("**Контекст:** после sanity check стало ясно, что глобально ни prod, ни research")
    lines.append("oracle не бьют простые baseline'ы (longshot-эвристика A4 даёт лучший ROI).")
    lines.append("Этот анализ проверяет, существуют ли срезы, где oracle реально помогает.")
    lines.append("")
    lines.append("## Обозначения")
    lines.append("")
    lines.append("- **PROD_oracle** — oracle с весами из прод `calibration_configs`")
    lines.append("- **RESEARCH_oracle** — oracle с `DEFAULT_WEIGHTS` из `scoring.py`")
    lines.append("- **best_baseline** — лучший по ROI из {A0..A6}")
    lines.append("- 🟢 oracle бьёт best_baseline минимум на +2% ROI")
    lines.append("- ⚪ oracle не бьёт best_baseline / недостаточно данных")
    lines.append("")
    lines.append("## Baseline стратегии")
    lines.append("")
    lines.append("- **A0_market_follower** — `side = YES if market_price > 0.5 else NO`")
    lines.append("- **A1_always_NO** — `side = NO` (classic Polymarket NO-bias)")
    lines.append("- **A2_always_YES** — `side = YES`")
    lines.append("- **A3_random_50_50** — seed=42, воспроизводимо")
    lines.append("- **A4_low_price_YES** — `side = YES if market_price < 0.5 else NO` (longshot)")
    lines.append("- **A5_anti_research** — обратная сторона от RESEARCH")
    lines.append("- **A6_anti_prod** — обратная сторона от PROD")
    lines.append("")

    # Summary table
    lines.append("## Summary (все срезы)")
    lines.append("")
    lines.append("| Slice | N | PROD ROI | RESEARCH ROI | Best baseline | Best ROI | Verdict |")
    lines.append("|---|---:|---:|---:|---|---:|---|")
    for slice_name, strategies in slice_results.items():
        if strategies["PROD_oracle"]["n"] == 0:
            lines.append(f"| {slice_name} | 0 | - | - | - | - | _empty_ |")
            continue
        n = strategies["PROD_oracle"]["n"]
        prod_roi = strategies["PROD_oracle"]["roi"]
        research_roi = strategies["RESEARCH_oracle"]["roi"]
        baseline_rois = [
            (name, strategies[name]["roi"]) for name in strategies if name.startswith("A")
        ]
        best_name, best_roi = max(baseline_rois, key=lambda x: x[1])
        flag, _ = verdict_for_slice(slice_name, strategies)
        lines.append(
            f"| {slice_name} | {n:,} | {prod_roi:+.4f} | {research_roi:+.4f} | "
            f"{best_name} | {best_roi:+.4f} | {flag} |"
        )
    lines.append("")

    # Подробности по каждому слайсу
    lines.append("## Детали по срезам")
    lines.append("")
    for slice_name, strategies in slice_results.items():
        lines.extend(format_slice_section(slice_name, strategies))

    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pit", type=str, default=DEFAULT_PIT)
    parser.add_argument("--out", type=str, default=DEFAULT_OUT)
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    print(f"[1/4] Loading {args.pit}")
    con = duckdb.connect()
    raw = con.execute(
        f"""
        SELECT
            category, phase, horizon_days, market_price, total_volume_usd,
            div_prod, side_prod, prod_win, prod_pnl,
            div_research, side_research, research_win, research_pnl,
            winner_is_yes
        FROM read_parquet('{args.pit}')
        """
    ).fetchall()
    cols = [
        "category", "phase", "horizon_days", "market_price", "total_volume_usd",
        "div_prod", "side_prod", "prod_win", "prod_pnl",
        "div_research", "side_research", "research_win", "research_pnl",
        "winner_is_yes",
    ]
    rows = [dict(zip(cols, r, strict=True)) for r in raw]
    print(f"      {len(rows):,} rows loaded")

    print("[2/4] Building slices")
    slices = build_slices(rows)
    for name, s in slices.items():
        print(f"      {name:<30} {len(s):>7,} rows")

    print("[3/4] Evaluating strategies per slice")
    slice_results = {}
    for name, slice_rows in slices.items():
        slice_results[name] = evaluate_slice(slice_rows)

    print("[4/4] Writing report")
    report = format_report(slice_results)
    with open(args.out, "w") as f:
        f.write(report)
    print(f"      → {args.out}")

    # Печатаем summary на stdout для quick look
    print()
    print("=" * 70)
    print("QUICK SUMMARY")
    print("=" * 70)
    print(f"  {'Slice':<30} {'N':>7}  {'PROD':>9}  {'RESEARCH':>9}  {'Best bl':>9}  {'Flag':>4}")
    print("  " + "-" * 78)
    for slice_name, strategies in slice_results.items():
        n = strategies["PROD_oracle"]["n"]
        if n == 0:
            print(f"  {slice_name:<30} {'empty':>7}")
            continue
        prod_roi = strategies["PROD_oracle"]["roi"]
        research_roi = strategies["RESEARCH_oracle"]["roi"]
        baseline_rois = [
            (name, strategies[name]["roi"]) for name in strategies if name.startswith("A")
        ]
        _, best_roi = max(baseline_rois, key=lambda x: x[1])
        flag, _ = verdict_for_slice(slice_name, strategies)
        print(
            f"  {slice_name:<30} {n:>7,}  {prod_roi:>+9.4f}  {research_roi:>+9.4f}  "
            f"{best_roi:>+9.4f}  {flag}"
        )


if __name__ == "__main__":
    main()

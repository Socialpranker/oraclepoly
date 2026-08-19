"""Historical Edge Search — поиск любого работающего сигнала в PIT датасете.

Контекст: после sanity check и conditional analysis стало ясно, что urезанная
версия oracle_score не даёт edge ни в одном срезе данных. Перед тем как
усложнять модель (восстанавливать registry, sports, news), хочу ответить на
фундаментальный вопрос: **существует ли ВООБЩЕ торгуемый сигнал в этих данных,
помимо longshot bias (A4)?**

Этот скрипт тестирует 8 явных гипотез (H1-H8), зафиксированных ДО прогона —
чтобы избежать p-hacking. Каждая гипотеза отвечает на конкретный вопрос.

Гипотезы:

  H1_momentum_24h        Side = направление движения за 24h. Если цена выросла
                         → YES, упала → NO. Ожидание: работает на коротких horizon'ах.

  H2_late_drift          Same as H1, но применяется ТОЛЬКО на phase='late'.
                         Ожидание: в последние дни цена движется к победителю.

  H3_mean_reversion      Если цена отклонилась от 7d VWAP на ≥5%, ставь в
                         обратную сторону. Ожидание: работает на длинных horizon'ах.

  H4_volume_shock_fade   Если объём за 2h > 3× медианы часового объёма за 7d,
                         и флоу был в сторону X (maker buy vs sell) — ставь
                         ПРОТИВ X. Ожидание: retail-trap, fade the crowd.

  H5_whale_accumulation  Если whale_buy_ratio_2h > 0.6 и unique_takers_2h > 5,
                         ставь в сторону whale_buy — follow the whales.

  H6_crowd_wisdom        Если unique_takers_2h > 20 и taker_buy_ratio > 0.6,
                         ставь YES (follow the crowd). Противоположность H4.

  H7_early_flat          Если std_first_24h < 0.02 (рынок "залип" на старте),
                         то side = сторона, на которой рынок залип (rounded
                         market_price direction). Ожидание: скрытый consensus.

  H8_optimism_tax_direct Если market_price > 0.70 → NO. Если < 0.30 → NO.
                         В средней зоне — skip. Формализация "fade favorites".

Методика:
  1. Применяем H1-H8 к pit_dataset.parquet
  2. Сравниваем с A4 (low_price_YES), A1 (always_NO), A0 (market_follower)
  3. Применяем 4 slice'а: GLOBAL, S1_price_neutral, S3_late, S4_neutral_and_late
  4. Эвристика проходит фильтр если бьёт A4 хотя бы в ОДНОМ slice'е с N≥500
     и edge >+0.02.
  5. Сильная эвристика — проходит в ≥2 slice'ах.

Запуск:
    python3 scripts/research/historical_edge_search.py \
        --pit /mnt/data/polymarket_dataset/pit_dataset_v2.parquet \
        --out /mnt/data/polymarket_dataset/analysis/edge_search.md
"""

import argparse
import math
import os
import random

import duckdb

DEFAULT_PIT = "/mnt/data/polymarket_dataset/pit_dataset_v2.parquet"
DEFAULT_OUT = "/mnt/data/polymarket_dataset/analysis/edge_search.md"

MIN_N_FOR_VERDICT = 500
EDGE_THRESHOLD = 0.02


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
# Hypothesis side functions — каждая возвращает (side, skip_flag)
# skip_flag=True если гипотеза неприменима к этой строке (например, нет данных)
# ────────────────────────────────────────────────────────────────────────────

def h1_momentum_24h(r: dict) -> tuple[str | None, bool]:
    m = r.get("momentum_24h")
    if m is None:
        return None, True
    if m > 0.01:
        return "YES", False
    if m < -0.01:
        return "NO", False
    return None, True  # flat — skip


def h2_late_drift(r: dict) -> tuple[str | None, bool]:
    if r["phase"] != "late":
        return None, True
    m = r.get("momentum_24h")
    if m is None:
        return None, True
    if m > 0.01:
        return "YES", False
    if m < -0.01:
        return "NO", False
    return None, True


def h3_mean_reversion(r: dict) -> tuple[str | None, bool]:
    vwap = r.get("vwap_7d")
    if vwap is None:
        return None, True
    mp = r["market_price"]
    dev = mp - vwap
    if dev >= 0.05:
        return "NO", False  # цена выше vwap — ставим на reversion вниз → NO
    if dev <= -0.05:
        return "YES", False
    return None, True


def h4_volume_shock_fade(r: dict) -> tuple[str | None, bool]:
    vol_2h = r.get("volume_2h") or 0
    median_h = r.get("median_hourly_volume_7d")
    if not median_h or median_h <= 0:
        return None, True
    # vol_2h = сумма за 2h, сравниваем с 2 × median hourly
    if vol_2h < 3 * 2 * median_h:
        return None, True  # нет шока
    # Направление флоу: maker buy vs sell
    maker_buy = r.get("maker_raw", 0) or 0
    if maker_buy > 0.1:
        return "NO", False  # makers скупают YES → fade → NO
    if maker_buy < -0.1:
        return "YES", False
    return None, True


def h5_whale_accumulation(r: dict) -> tuple[str | None, bool]:
    whale_ratio = r.get("whale_buy_ratio_2h")
    if whale_ratio is None:
        return None, True
    unique_takers = r.get("unique_takers_2h") or 0
    if unique_takers < 5:
        return None, True
    if whale_ratio > 0.7:
        return "YES", False  # киты покупают YES
    if whale_ratio < 0.3:
        return "NO", False
    return None, True


def h6_crowd_wisdom(r: dict) -> tuple[str | None, bool]:
    unique_takers = r.get("unique_takers_2h") or 0
    if unique_takers < 20:
        return None, True
    # taker_buy_ratio: taker_raw содержит (buy-sell)/(buy+sell) — нормирован [-1,1]
    taker_raw = r.get("taker_raw", 0) or 0
    # taker_raw ∈ [-1, 1], переводим в ratio [0, 1]: (taker_raw + 1) / 2
    buy_ratio = (taker_raw + 1) / 2
    if buy_ratio > 0.6:
        return "YES", False
    if buy_ratio < 0.4:
        return "NO", False
    return None, True


def h7_early_flat(r: dict) -> tuple[str | None, bool]:
    std = r.get("std_first_24h")
    if std is None:
        return None, True
    if std >= 0.02:
        return None, True
    # Рынок залип — берём сторону "по рынку на момент T"
    mp = r["market_price"]
    if mp > 0.55:
        return "YES", False
    if mp < 0.45:
        return "NO", False
    return None, True


def h8_optimism_tax(r: dict) -> tuple[str | None, bool]:
    mp = r["market_price"]
    if mp > 0.70:
        return "NO", False  # fade overconfident favorite
    if mp < 0.30:
        return "NO", False  # fade overconfident longshot away from NO
    return None, True  # middle — skip


HYPOTHESES = {
    "H1_momentum_24h": h1_momentum_24h,
    "H2_late_drift": h2_late_drift,
    "H3_mean_reversion": h3_mean_reversion,
    "H4_volume_shock_fade": h4_volume_shock_fade,
    "H5_whale_accumulation": h5_whale_accumulation,
    "H6_crowd_wisdom": h6_crowd_wisdom,
    "H7_early_flat": h7_early_flat,
    "H8_optimism_tax": h8_optimism_tax,
}


# ────────────────────────────────────────────────────────────────────────────
# Baselines (из sanity check)
# ────────────────────────────────────────────────────────────────────────────

def baseline_a0(r: dict) -> str:
    return "YES" if r["market_price"] > 0.5 else "NO"


def baseline_a1(r: dict) -> str:
    return "NO"


def baseline_a2(r: dict) -> str:
    return "YES"


def baseline_a4(r: dict) -> str:
    return "YES" if r["market_price"] < 0.5 else "NO"


def baseline_random(r: dict, rng: random.Random) -> str:
    return "YES" if rng.random() < 0.5 else "NO"


# ────────────────────────────────────────────────────────────────────────────
# Slice definitions
# ────────────────────────────────────────────────────────────────────────────

def build_slices(rows: list[dict]) -> dict[str, list[dict]]:
    return {
        "GLOBAL": rows,
        "S1_price_neutral_30_70": [r for r in rows if 0.30 <= r["market_price"] <= 0.70],
        "S3_late_phase": [r for r in rows if r["phase"] == "late"],
        "S4_neutral_and_late": [
            r for r in rows if 0.30 <= r["market_price"] <= 0.70 and r["phase"] == "late"
        ],
    }


# ────────────────────────────────────────────────────────────────────────────
# Evaluation
# ────────────────────────────────────────────────────────────────────────────

def evaluate_hypothesis(rows: list[dict], hypothesis_fn) -> dict:
    pnls = []
    wins = []
    skipped = 0
    for r in rows:
        side, skip = hypothesis_fn(r)
        if skip or side is None:
            skipped += 1
            continue
        pnl, win = pnl_for_side(side, r["market_price"], r["winner_is_yes"])
        pnls.append(pnl)
        wins.append(win)
    stats = compute_stats(pnls, wins)
    stats["skipped"] = skipped
    stats["coverage"] = 1 - (skipped / len(rows)) if rows else 0
    return stats


def evaluate_baseline(rows: list[dict], baseline_fn, *extra_args) -> dict:
    pnls = []
    wins = []
    for r in rows:
        side = baseline_fn(r, *extra_args)
        pnl, win = pnl_for_side(side, r["market_price"], r["winner_is_yes"])
        pnls.append(pnl)
        wins.append(win)
    return compute_stats(pnls, wins)


# ────────────────────────────────────────────────────────────────────────────
# Report
# ────────────────────────────────────────────────────────────────────────────

def format_report(results: dict[str, dict[str, dict]]) -> str:
    lines = []
    lines.append("# Historical Edge Search — H1-H8")
    lines.append("")
    lines.append("**Автор:** `scripts/research/historical_edge_search.py`")
    lines.append("**Цель:** найти хотя бы одну эвристику, которая бьёт longshot baseline (A4)")
    lines.append("")
    lines.append("**Критерий прохождения фильтра:**")
    lines.append(f"- N ≥ {MIN_N_FOR_VERDICT} после применения эвристики")
    lines.append(f"- ROI − A4_roi ≥ +{EDGE_THRESHOLD:.2f}")
    lines.append("- Устойчивость = количество slice'ов, где условие выполнено")
    lines.append("")
    lines.append("## Гипотезы")
    lines.append("")
    lines.append("| Hypothesis | Условие | Side |")
    lines.append("|---|---|---|")
    lines.append("| H1_momentum_24h | momentum_24h > 0.01 или < -0.01 | YES / NO по направлению |")
    lines.append("| H2_late_drift | то же, только phase='late' | YES / NO |")
    lines.append("| H3_mean_reversion | ‖mp − vwap_7d‖ ≥ 0.05 | обратно отклонению |")
    lines.append("| H4_volume_shock_fade | vol_2h > 3× median*2 и maker направление | fade the maker flow |")
    lines.append("| H5_whale_accumulation | whale_buy_ratio > 0.7 + unique_takers ≥ 5 | follow whales |")
    lines.append("| H6_crowd_wisdom | unique_takers ≥ 20, taker buy ratio > 0.6 | follow crowd |")
    lines.append("| H7_early_flat | std_first_24h < 0.02 | по рынку |")
    lines.append("| H8_optimism_tax | mp > 0.70 или mp < 0.30 | всегда NO |")
    lines.append("")

    # Summary по всем slice'ам
    lines.append("## Summary — едет ли эвристика в slice'ах?")
    lines.append("")
    lines.append("Столбцы: coverage (доля строк где эвристика применима), N (сколько в итоге применено),")
    lines.append("ROI эвристики, A4_roi, edge (эвристика − A4), flag.")
    lines.append("")

    for slice_name, slice_results in results.items():
        lines.append(f"### {slice_name}")
        lines.append("")
        n_total = slice_results["__n_total"]
        a4 = slice_results["A4_baseline"]
        a1 = slice_results["A1_baseline"]
        a0 = slice_results["A0_baseline"]
        lines.append(f"**Total rows:** {n_total:,}")
        lines.append("")
        lines.append(
            f"**Baselines:** A4={a4['roi']:+.4f}  A1={a1['roi']:+.4f}  A0={a0['roi']:+.4f}"
        )
        lines.append("")
        lines.append("| Hypothesis | Coverage | N | WR | ROI | A4 | Edge vs A4 | Flag |")
        lines.append("|---|---:|---:|---:|---:|---:|---:|---|")

        h_items = [(k, v) for k, v in slice_results.items() if k.startswith("H")]
        h_items.sort(key=lambda kv: -kv[1]["roi"])

        for name, s in h_items:
            coverage = s.get("coverage", 0)
            n = s["n"]
            wr = s["wr"]
            roi = s["roi"]
            edge = roi - a4["roi"]
            if n < MIN_N_FOR_VERDICT:
                flag = "⚪"
            elif edge >= EDGE_THRESHOLD:
                flag = "🟢"
            elif edge <= -EDGE_THRESHOLD:
                flag = "🔴"
            else:
                flag = "➖"
            lines.append(
                f"| {name} | {coverage:.1%} | {n:,} | {wr:.1%} | {roi:+.4f} | "
                f"{a4['roi']:+.4f} | {edge:+.4f} | {flag} |"
            )
        lines.append("")

    # Финальная сводка — сколько раз каждая эвристика прошла фильтр
    lines.append("## Финальная сводка — количество успешных slice'ов")
    lines.append("")
    lines.append("| Hypothesis | 🟢 slices | 🔴 slices | ⚪ slices | Вердикт |")
    lines.append("|---|---:|---:|---:|---|")
    hypothesis_scores: dict[str, dict[str, int]] = {h: {"green": 0, "red": 0, "white": 0} for h in HYPOTHESES}
    for slice_name, slice_results in results.items():
        a4_roi = slice_results["A4_baseline"]["roi"]
        for h in HYPOTHESES:
            s = slice_results[h]
            if s["n"] < MIN_N_FOR_VERDICT:
                hypothesis_scores[h]["white"] += 1
                continue
            edge = s["roi"] - a4_roi
            if edge >= EDGE_THRESHOLD:
                hypothesis_scores[h]["green"] += 1
            elif edge <= -EDGE_THRESHOLD:
                hypothesis_scores[h]["red"] += 1

    for h, scores in hypothesis_scores.items():
        if scores["green"] >= 2:
            verdict = "🟢🟢 STRONG — edge в ≥2 slices"
        elif scores["green"] >= 1:
            verdict = "🟢 WEAK — edge только в 1 slice"
        elif scores["red"] >= 2:
            verdict = "🔴 ANTI-SIGNAL — хуже A4 в ≥2 slices"
        else:
            verdict = "⚪ neutral / no signal"
        lines.append(
            f"| {h} | {scores['green']} | {scores['red']} | {scores['white']} | {verdict} |"
        )
    lines.append("")
    return "\n".join(lines)


# ────────────────────────────────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pit", type=str, default=DEFAULT_PIT)
    parser.add_argument("--out", type=str, default=DEFAULT_OUT)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    os.makedirs(os.path.dirname(args.out), exist_ok=True)

    print(f"[1/3] Loading {args.pit}")
    con = duckdb.connect()
    raw = con.execute(
        f"""
        SELECT
            category, phase, horizon_days, market_price, total_volume_usd,
            whale_raw, maker_raw, taker_raw,
            side_prod, prod_win, prod_pnl,
            side_research, research_win, research_pnl,
            winner_is_yes,
            momentum_24h, price_24h_ago, vwap_7d, std_first_24h,
            whale_buy_ratio_2h, unique_takers_2h, volume_2h, median_hourly_volume_7d
        FROM read_parquet('{args.pit}')
        """
    ).fetchall()
    cols = [
        "category", "phase", "horizon_days", "market_price", "total_volume_usd",
        "whale_raw", "maker_raw", "taker_raw",
        "side_prod", "prod_win", "prod_pnl",
        "side_research", "research_win", "research_pnl",
        "winner_is_yes",
        "momentum_24h", "price_24h_ago", "vwap_7d", "std_first_24h",
        "whale_buy_ratio_2h", "unique_takers_2h", "volume_2h", "median_hourly_volume_7d",
    ]
    rows = [dict(zip(cols, r, strict=True)) for r in raw]
    print(f"      {len(rows):,} rows loaded")

    # Проверка что новые фичи на месте
    n_with_momentum = sum(1 for r in rows if r.get("momentum_24h") is not None)
    n_with_vwap = sum(1 for r in rows if r.get("vwap_7d") is not None)
    n_with_std = sum(1 for r in rows if r.get("std_first_24h") is not None)
    print("      Feature coverage:")
    print(f"        momentum_24h: {n_with_momentum:,} ({n_with_momentum/len(rows):.1%})")
    print(f"        vwap_7d:      {n_with_vwap:,} ({n_with_vwap/len(rows):.1%})")
    print(f"        std_first_24h:{n_with_std:,} ({n_with_std/len(rows):.1%})")

    print("[2/3] Evaluating hypotheses per slice")
    slices = build_slices(rows)

    results: dict[str, dict[str, dict]] = {}
    for slice_name, slice_rows in slices.items():
        slice_results: dict[str, dict] = {"__n_total": len(slice_rows)}
        # Baselines
        slice_results["A4_baseline"] = evaluate_baseline(slice_rows, baseline_a4)
        slice_results["A1_baseline"] = evaluate_baseline(slice_rows, baseline_a1)
        slice_results["A0_baseline"] = evaluate_baseline(slice_rows, baseline_a0)
        slice_results["A2_baseline"] = evaluate_baseline(slice_rows, baseline_a2)
        # Hypotheses
        for hname, hfn in HYPOTHESES.items():
            slice_results[hname] = evaluate_hypothesis(slice_rows, hfn)
        results[slice_name] = slice_results
        print(f"      {slice_name:<30} {len(slice_rows):,} rows → evaluated")

    print("[3/3] Writing report")
    report = format_report(results)
    with open(args.out, "w") as f:
        f.write(report)
    print(f"      → {args.out}")

    # Печатаем quick summary
    print()
    print("=" * 70)
    print("QUICK SUMMARY (GLOBAL slice)")
    print("=" * 70)
    global_res = results["GLOBAL"]
    a4_roi = global_res["A4_baseline"]["roi"]
    print(f"  A4 (baseline): ROI={a4_roi:+.4f}")
    print()
    print(f"  {'Hypothesis':<25} {'N':>7} {'Coverage':>9} {'ROI':>9} {'Edge vs A4':>12} {'Flag':>5}")
    print("  " + "-" * 72)
    h_items = [(k, v) for k, v in global_res.items() if k.startswith("H")]
    h_items.sort(key=lambda kv: -kv[1]["roi"])
    for name, s in h_items:
        edge = s["roi"] - a4_roi
        if s["n"] < MIN_N_FOR_VERDICT:
            flag = "⚪"
        elif edge >= EDGE_THRESHOLD:
            flag = "🟢"
        elif edge <= -EDGE_THRESHOLD:
            flag = "🔴"
        else:
            flag = "➖"
        coverage = s.get("coverage", 0)
        print(f"  {name:<25} {s['n']:>7,} {coverage:>8.1%} {s['roi']:>+9.4f} {edge:>+12.4f} {flag:>5}")


if __name__ == "__main__":
    main()

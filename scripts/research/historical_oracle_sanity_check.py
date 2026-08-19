"""Historical Oracle Sanity Check — валидация находки "research бьёт prod в 45×".

Гипотеза, которую надо отвергнуть:
  "Research edge — это просто longshot bias на top-by-volume сэмпле,
   а не реальный сигнал oracle_score."

Если гипотеза **не отвергается** — останавливаемся и пересматриваем методику.
Если **отвергается** — значит research действительно несёт информацию сверх baseline'ов.

Тестируемые baseline'ы:

  A0: market-follower          side = "YES" if market_price > 0.5 else "NO"
  A1: always-NO                side = "NO" (capture Polymarket NO-bias)
  A2: always-YES               side = "YES" (capture longshot бонус от высоких payoff)
  A3: random 50/50             side = random per row (воспроизводимо, seed=42)
  A4: low-price-YES            side = "YES" if market_price < 0.5 else "NO"
                               (противоположно A0 — чистая longshot логика)
  A5: anti-research            side = opposite to side_research per row
                               (если это даёт +ROI — research edge иллюзорен)
  A6: anti-prod                side = opposite to side_prod per row

Плюс — распределение сторон:
  какой % prod и research ставят на YES vs NO?
  если сильно расходятся — edge может быть в "случайном" выборе стороны, не в сигнале.

И ключевой метрический тест:
  если `|sharpe| > 5` у **любого** baseline → формула PnL/Sharpe бракованная
  ожидаемо: все baseline'ы должны давать ROI в диапазоне [-0.5, +0.6] на этом сэмпле.
"""

import argparse
import math
import random

import duckdb

DEFAULT_PIT = "/mnt/data/polymarket_dataset/pit_dataset.parquet"


def pnl_for_side(side: str, market_price: float, winner_is_yes: bool) -> tuple[float, bool]:
    """Посчитать PnL и win-flag для данной стороны.

    Returns: (pnl_per_dollar, win).
    """
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
        return {"n": 0, "wr": 0, "roi": 0, "sharpe": 0, "median_pnl": 0}
    total = sum(pnls)
    roi = total / n
    wr = sum(wins) / n
    # Sharpe (без √N scaling — честный per-trade)
    if n >= 2:
        mean = roi
        var = sum((p - mean) ** 2 for p in pnls) / (n - 1)
        std = math.sqrt(var)
        sharpe_per_trade = roi / std if std > 0 else 0.0
    else:
        sharpe_per_trade = 0.0
    sorted_pnls = sorted(pnls)
    median = sorted_pnls[n // 2]
    return {
        "n": n,
        "wr": wr,
        "roi": roi,
        "sharpe_per_trade": sharpe_per_trade,
        "median_pnl": median,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--pit", type=str, default=DEFAULT_PIT)
    parser.add_argument("--seed", type=int, default=42)
    args = parser.parse_args()

    print("=" * 70)
    print("HISTORICAL ORACLE SANITY CHECK")
    print("=" * 70)

    con = duckdb.connect()
    rows = con.execute(
        f"""
        SELECT
            category, phase, market_price,
            oracle_prod, div_prod, side_prod, prod_win, prod_pnl,
            oracle_research, div_research, side_research, research_win, research_pnl,
            winner_is_yes
        FROM read_parquet('{args.pit}')
        """
    ).fetchall()

    n_total = len(rows)
    print(f"\n[1/4] Loaded {n_total:,} PIT snapshots from {args.pit}")

    # ── Распределение сторон ──
    side_prod_count = {"YES": 0, "NO": 0}
    side_research_count = {"YES": 0, "NO": 0}
    winner_yes_count = 0
    mp_low_count = 0  # market_price < 0.5

    for r in rows:
        side_prod_count[r[5]] += 1
        side_research_count[r[10]] += 1
        if r[13]:
            winner_yes_count += 1
        if r[2] < 0.5:
            mp_low_count += 1

    print("\n[2/4] Side distributions:")
    print(f"  PROD:     YES={side_prod_count['YES']:,} ({side_prod_count['YES']/n_total:.1%})  "
          f"NO={side_prod_count['NO']:,} ({side_prod_count['NO']/n_total:.1%})")
    print(f"  RESEARCH: YES={side_research_count['YES']:,} ({side_research_count['YES']/n_total:.1%})  "
          f"NO={side_research_count['NO']:,} ({side_research_count['NO']/n_total:.1%})")
    print(f"  Outcomes: YES won={winner_yes_count:,} ({winner_yes_count/n_total:.1%})  "
          f"NO won={n_total-winner_yes_count:,} ({(n_total-winner_yes_count)/n_total:.1%})")
    print(f"  Market prices: price<0.5 = {mp_low_count:,} ({mp_low_count/n_total:.1%})  "
          f"price>=0.5 = {n_total-mp_low_count:,}")

    # ── Верификация: пересчёт prod/research PnL из первых принципов ──
    print("\n[3/4] Verification: recompute prod/research PnL from market_price + winner")

    prod_recomputed_pnls = []
    prod_recomputed_wins = []
    research_recomputed_pnls = []
    research_recomputed_wins = []

    for r in rows:
        mp = r[2]
        side_p = r[5]
        side_r = r[10]
        winner_yes = r[13]

        p_pnl, p_win = pnl_for_side(side_p, mp, winner_yes)
        r_pnl, r_win = pnl_for_side(side_r, mp, winner_yes)

        prod_recomputed_pnls.append(p_pnl)
        prod_recomputed_wins.append(p_win)
        research_recomputed_pnls.append(r_pnl)
        research_recomputed_wins.append(r_win)

    prod_recomputed = compute_stats(prod_recomputed_pnls, prod_recomputed_wins)
    research_recomputed = compute_stats(research_recomputed_pnls, research_recomputed_wins)

    # Сравним с prod_pnl/research_pnl в parquet
    prod_stored_pnls = [r[7] for r in rows]
    prod_stored_wins = [r[6] for r in rows]
    research_stored_pnls = [r[12] for r in rows]
    research_stored_wins = [r[11] for r in rows]
    prod_stored = compute_stats(prod_stored_pnls, prod_stored_wins)
    research_stored = compute_stats(research_stored_pnls, research_stored_wins)

    print(f"  PROD stored:      ROI={prod_stored['roi']:+.4f}  WR={prod_stored['wr']:.1%}  sharpe={prod_stored['sharpe_per_trade']:+.2f}")
    print(f"  PROD recomputed:  ROI={prod_recomputed['roi']:+.4f}  WR={prod_recomputed['wr']:.1%}  sharpe={prod_recomputed['sharpe_per_trade']:+.2f}")
    if abs(prod_stored['roi'] - prod_recomputed['roi']) > 1e-3:
        print("  ⚠️ DRIFT: stored != recomputed для prod!")

    print(f"  RESEARCH stored:     ROI={research_stored['roi']:+.4f}  WR={research_stored['wr']:.1%}  sharpe={research_stored['sharpe_per_trade']:+.2f}")
    print(f"  RESEARCH recomputed: ROI={research_recomputed['roi']:+.4f}  WR={research_recomputed['wr']:.1%}  sharpe={research_recomputed['sharpe_per_trade']:+.2f}")
    if abs(research_stored['roi'] - research_recomputed['roi']) > 1e-3:
        print("  ⚠️ DRIFT: stored != recomputed для research!")

    # ── Baseline'ы ──
    print("\n[4/4] Baseline comparison:")
    print()

    baselines: dict[str, dict] = {}
    rng = random.Random(args.seed)

    for r in rows:
        mp = r[2]
        winner_yes = r[13]
        side_p = r[5]
        side_r = r[10]

        # A0: market-follower
        side_a0 = "YES" if mp > 0.5 else "NO"
        # A1: always-NO
        side_a1 = "NO"
        # A2: always-YES
        side_a2 = "YES"
        # A3: random
        side_a3 = "YES" if rng.random() < 0.5 else "NO"
        # A4: low-price-YES (longshot capture)
        side_a4 = "YES" if mp < 0.5 else "NO"
        # A5: anti-research
        side_a5 = "NO" if side_r == "YES" else "YES"
        # A6: anti-prod
        side_a6 = "NO" if side_p == "YES" else "YES"

        for name, side in [
            ("A0_market_follower", side_a0),
            ("A1_always_NO", side_a1),
            ("A2_always_YES", side_a2),
            ("A3_random_50_50", side_a3),
            ("A4_low_price_YES", side_a4),
            ("A5_anti_research", side_a5),
            ("A6_anti_prod", side_a6),
        ]:
            if name not in baselines:
                baselines[name] = {"pnls": [], "wins": []}
            pnl, win = pnl_for_side(side, mp, winner_yes)
            baselines[name]["pnls"].append(pnl)
            baselines[name]["wins"].append(win)

    # Вычисляем + сортируем по ROI
    rows_out = []
    for name in sorted(baselines.keys()):
        s = compute_stats(baselines[name]["pnls"], baselines[name]["wins"])
        rows_out.append((name, s))

    # Плюс prod и research
    rows_out.append(("PROD_oracle", prod_stored))
    rows_out.append(("RESEARCH_oracle", research_stored))

    # Сортируем по ROI descending
    rows_out.sort(key=lambda x: -x[1]["roi"])

    print(f"  {'Strategy':<25} {'N':>6}  {'WR':>6}  {'ROI':>9}  {'Sharpe/t':>9}  {'Median':>9}")
    print(f"  {'-'*25} {'-'*6}  {'-'*6}  {'-'*9}  {'-'*9}  {'-'*9}")
    for name, s in rows_out:
        print(f"  {name:<25} {s['n']:>6,}  {s['wr']:>6.1%}  {s['roi']:>+9.4f}  {s['sharpe_per_trade']:>+9.2f}  {s['median_pnl']:>+9.4f}")

    # ── Verdict ──
    print()
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    research_roi = research_stored["roi"]
    best_baseline_roi = max(s["roi"] for _, s in rows_out if _.startswith("A"))
    best_baseline_name = max((n for n, _ in rows_out if n.startswith("A")),
                             key=lambda n: next(s for nn, s in rows_out if nn == n)["roi"])

    gap = research_roi - best_baseline_roi
    print(f"\nRESEARCH ROI:              {research_roi:+.4f}")
    print(f"BEST baseline:             {best_baseline_name} = {best_baseline_roi:+.4f}")
    print(f"Gap (research - baseline): {gap:+.4f}")

    if gap < 0.02:
        print("\n🔴 VERDICT: research НЕ бьёт лучший baseline (gap < 0.02)")
        print("   Гипотеза подтверждена — research edge иллюзорен.")
        print("   НЕ применять research-веса в прод.")
    elif gap < 0.10:
        print("\n🟡 VERDICT: research даёт marginal edge (gap 0.02-0.10)")
        print("   Возможен реальный edge, но слабый. Требуется:")
        print("   - Больше данных (полный прогон на 30K+ markets)")
        print("   - Нормализация категорий (избавиться от 'Other' корзины)")
        print("   - Live A/B тест перед применением")
    else:
        print(f"\n🟢 VERDICT: research даёт существенный edge (gap {gap:+.4f})")
        print("   Edge подтверждён после учёта baseline'ов.")
        print("   Можно готовить постепенный deploy.")

    # Проверка Sharpe
    if research_stored["sharpe_per_trade"] > 2.0:
        print(f"\n⚠️  Research per-trade Sharpe = {research_stored['sharpe_per_trade']:.2f} — подозрительно высоко")
        print("   Проверить: нет ли outlier'ов с гигантскими payoff")


if __name__ == "__main__":
    main()

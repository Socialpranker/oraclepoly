"""Oracle Deep Drill — attribution только на маркетах где факторы активны.

Проблема предыдущего анализа: 93% снапшотов оракл ≈ market_price (все факторы = 0).
Глобальный BSS +0.002 — это среднее по пустым и активным маркетам вместе.

Этот скрипт отвечает на настоящий вопрос:
  "На маркетах где whale/news/SM реально что-то сказали — оракл работает?"

Срезы:
  - whale_active:      whale_contribution_raw != 0
  - news_active:       news_contribution_raw != 0
  - sm_active:         sm_fresh_raw != 0 OR sm_registry_raw != 0
  - any_active:        хотя бы один фактор != 0
  - all_zero:          все факторы == 0 (pure market_price маркеты)

Для каждого среза:
  - BSS composite vs market_price
  - BSS composite vs pure market_price signal на тех же маркетах
  - IC
  - ROI при div >= 3% и 5%

Вопрос который закрываем:
  "Есть ли edge у оракла именно там где он должен работать?"
"""
from __future__ import annotations

import math
from pathlib import Path

import duckdb

PIT = Path(__file__).parent.parent.parent / "data" / "prod_oracle_snapshots.parquet"
OUT = Path(__file__).parent.parent.parent / "docs" / "research" / "2026-04-30-oracle-active-factor-deep.md"


def sigmoid(x: float) -> float:
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def brier(probs: list[float], outcomes: list[int]) -> float:
    n = len(probs)
    if n == 0:
        return float("nan")
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / n


def bss(model_bs: float, ref_bs: float) -> float:
    if ref_bs == 0 or math.isnan(ref_bs):
        return float("nan")
    return 1.0 - model_bs / ref_bs


def spearman(xs: list[float], ys: list[float]) -> float:
    n = len(xs)
    if n < 10:
        return float("nan")

    def rank(arr: list[float]) -> list[float]:
        si = sorted(range(n), key=lambda i: arr[i])
        r = [0.0] * n
        for rv, idx in enumerate(si, 1):
            r[idx] = float(rv)
        return r

    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((rx[i] - mx) * (ry[i] - my) for i in range(n))
    dx = math.sqrt(sum((r - mx) ** 2 for r in rx))
    dy = math.sqrt(sum((r - my) ** 2 for r in ry))
    return float("nan") if dx == 0 or dy == 0 else num / (dx * dy)


def roi_stats(oracle_scores: list[float], market_prices: list[float],
              outcomes: list[int], div_thr: float) -> dict:
    pnls, wins = [], []
    for score, mp, o in zip(oracle_scores, market_prices, outcomes):
        div = score - mp
        if abs(div) < div_thr:
            continue
        side = "YES" if div > 0 else "NO"
        if side == "YES":
            win = bool(o)
        else:
            win = not bool(o)
        entry = mp if side == "YES" else (1.0 - mp)
        entry = max(entry, 0.01)
        pnl = (1.0 / entry - 1.0) if win else -1.0
        pnls.append(pnl)
        wins.append(win)

    n = len(pnls)
    if n == 0:
        return {"n": 0, "wr": float("nan"), "roi": float("nan")}
    roi_v = sum(pnls) / n
    return {"n": n, "wr": sum(wins) / n, "roi": roi_v}


def analyze(label: str, rows: list[dict]) -> None:
    if len(rows) < 50:
        print(f"\n  {label}: n={len(rows)} — skip (too few)")
        return

    oracle = [float(r["oracle_score"]) for r in rows]
    mp = [float(r["market_price"]) for r in rows]
    outcomes = [int(r["outcome"] == "yes") for r in rows]
    categories = [r.get("category") or "?" for r in rows]

    bs_oracle = brier(oracle, outcomes)
    bs_market = brier(mp, outcomes)
    bss_val = bss(bs_oracle, bs_market)
    ic_val = spearman(oracle, outcomes)
    roi3 = roi_stats(oracle, mp, outcomes, 0.03)
    roi5 = roi_stats(oracle, mp, outcomes, 0.05)

    # Category breakdown
    cats: dict[str, dict] = {}
    for i, cat in enumerate(categories):
        if cat not in cats:
            cats[cat] = {"oracle": [], "mp": [], "outcomes": []}
        cats[cat]["oracle"].append(oracle[i])
        cats[cat]["mp"].append(mp[i])
        cats[cat]["outcomes"].append(outcomes[i])

    # Verdict
    if math.isnan(bss_val):
        verdict = "⚪ N/A"
    elif bss_val > 0.02 and abs(ic_val) > 0.05:
        verdict = "🟢 EDGE"
    elif bss_val > 0.005:
        verdict = "🟡 WEAK"
    elif bss_val < -0.02:
        verdict = "🔴 HURTS"
    else:
        verdict = "⚪ NOISE"

    print(f"\n{'='*60}")
    print(f"  {label}  (n={len(rows):,})")
    print(f"{'='*60}")
    print(f"  Brier oracle:   {bs_oracle:.6f}")
    print(f"  Brier market:   {bs_market:.6f}")
    print(f"  BSS:            {bss_val:+.6f}   {verdict}")
    print(f"  IC (Spearman):  {ic_val:+.4f}")
    print(f"  ROI @div≥3%:    {roi3['roi']:+.4f}  (n={roi3['n']:,}, WR={roi3['wr']:.1%})" if not math.isnan(roi3['roi']) else "  ROI @div≥3%:    N/A")
    print(f"  ROI @div≥5%:    {roi5['roi']:+.4f}  (n={roi5['n']:,}, WR={roi5['wr']:.1%})" if not math.isnan(roi5['roi']) else "  ROI @div≥5%:    N/A")

    # Per-category (only if > 100 rows)
    big_cats = sorted([(c, d) for c, d in cats.items() if len(d["oracle"]) >= 100],
                      key=lambda x: -len(x[1]["oracle"]))
    if big_cats:
        print(f"\n  Per-category:")
        print(f"  {'Cat':<15} {'N':>7}  {'BSS':>8}  {'IC':>8}  {'ROI3':>8}  Verdict")
        print(f"  {'-'*60}")
        for cat, d in big_cats:
            bs_o = brier(d["oracle"], d["outcomes"])
            bs_m = brier(d["mp"], d["outcomes"])
            b = bss(bs_o, bs_m)
            ic = spearman(d["oracle"], d["outcomes"])
            r3 = roi_stats(d["oracle"], d["mp"], d["outcomes"], 0.03)
            if math.isnan(b):
                v = "⚪"
            elif b > 0.02 and abs(ic) > 0.05:
                v = "🟢"
            elif b > 0.005:
                v = "🟡"
            elif b < -0.02:
                v = "🔴"
            else:
                v = "⚪"
            roi_s = f"{r3['roi']:+.4f}" if not math.isnan(r3["roi"]) else "   N/A"
            print(f"  {cat:<15} {len(d['oracle']):>7,}  {b:>+8.4f}  {ic:>+8.4f}  {roi_s:>8}  {v}")

    return {
        "label": label,
        "n": len(rows),
        "bss": bss_val,
        "ic": ic_val,
        "roi3": roi3,
        "roi5": roi5,
        "verdict": verdict,
    }


def main() -> None:
    print("=" * 60)
    print("ORACLE ACTIVE FACTOR DEEP DRILL")
    print("=" * 60)

    con = duckdb.connect()
    raw = con.execute(f"""
        SELECT
            oracle_score, market_price, outcome, category, divergence,
            whale_contribution_raw, news_contribution_raw,
            sm_fresh_raw, sm_registry_raw, sm_cluster_raw,
            maker_direction_raw, taker_imbalance_raw,
            oi_factor_raw, micro_factor_raw,
            created_at, resolved_at
        FROM read_parquet('{PIT}')
        WHERE market_price > 0.01 AND market_price < 0.99
          AND outcome IN ('yes', 'no')
    """).fetchall()

    cols = ["oracle_score", "market_price", "outcome", "category", "divergence",
            "whale_contribution_raw", "news_contribution_raw",
            "sm_fresh_raw", "sm_registry_raw", "sm_cluster_raw",
            "maker_direction_raw", "taker_imbalance_raw",
            "oi_factor_raw", "micro_factor_raw",
            "created_at", "resolved_at"]
    rows = [dict(zip(cols, r, strict=True)) for r in raw]
    print(f"Loaded: {len(rows):,} rows")

    def nz(r: dict, col: str) -> bool:
        v = r.get(col)
        return v is not None and float(v) != 0.0

    # Define slices
    whale_active = [r for r in rows if nz(r, "whale_contribution_raw")]
    news_active = [r for r in rows if nz(r, "news_contribution_raw")]
    sm_active = [r for r in rows if nz(r, "sm_fresh_raw") or nz(r, "sm_registry_raw")]
    any_active = [r for r in rows if
                  nz(r, "whale_contribution_raw") or nz(r, "news_contribution_raw") or
                  nz(r, "sm_fresh_raw") or nz(r, "sm_registry_raw")]
    all_zero = [r for r in rows if not (
        nz(r, "whale_contribution_raw") or nz(r, "news_contribution_raw") or
        nz(r, "sm_fresh_raw") or nz(r, "sm_registry_raw"))]
    micro_active = [r for r in rows if nz(r, "micro_factor_raw")]
    oi_active = [r for r in rows if nz(r, "oi_factor_raw")]
    high_div = [r for r in rows if r.get("divergence") is not None and abs(float(r["divergence"])) >= 0.05]

    print(f"\nSlice sizes:")
    print(f"  all rows:         {len(rows):>8,}")
    print(f"  whale_active:     {len(whale_active):>8,}  ({len(whale_active)/len(rows):.1%})")
    print(f"  news_active:      {len(news_active):>8,}  ({len(news_active)/len(rows):.1%})")
    print(f"  sm_active:        {len(sm_active):>8,}  ({len(sm_active)/len(rows):.1%})")
    print(f"  any_active:       {len(any_active):>8,}  ({len(any_active)/len(rows):.1%})")
    print(f"  all_zero:         {len(all_zero):>8,}  ({len(all_zero)/len(rows):.1%})")
    print(f"  micro_active:     {len(micro_active):>8,}  ({len(micro_active)/len(rows):.1%})")
    print(f"  oi_active:        {len(oi_active):>8,}  ({len(oi_active)/len(rows):.1%})")
    print(f"  high_div (≥5%):   {len(high_div):>8,}  ({len(high_div)/len(rows):.1%})")

    print("\n\n=== CORE QUESTION: does oracle work where it's supposed to? ===")

    analyze("GLOBAL (all snapshots)", rows)
    analyze("ALL_ZERO (pure market_price, no factors)", all_zero)
    analyze("ANY_ACTIVE (at least one factor fired)", any_active)
    analyze("WHALE_ACTIVE", whale_active)
    analyze("NEWS_ACTIVE", news_active)
    analyze("SM_ACTIVE", sm_active)
    analyze("MICRO_ACTIVE (24.6% coverage)", micro_active)
    analyze("OI_ACTIVE (93.9% coverage)", oi_active)
    analyze("HIGH_DIV ≥5%", high_div)

    print("\n\n=== DONE ===")
    print(f"Key question answered: see BSS values above.")
    print(f"  BSS > 0.02 + IC > 0.05 = oracle adds real edge in this slice")
    print(f"  BSS ≈ 0 = oracle = market_price in this slice (no edge, no harm)")
    print(f"  BSS < -0.02 = oracle hurts in this slice")


if __name__ == "__main__":
    main()

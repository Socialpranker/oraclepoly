"""Oracle Edge Attribution — какие компоненты оракла реально дают edge.

Отвечает на вопрос: "есть ли edge в оракле, и где именно он живёт?"

Метрики:
  1. Brier Score — calibration quality (ниже = лучше, baseline = market_price Brier)
  2. Brier Skill Score (BSS) = 1 - Brier(model)/Brier(baseline) > 0 → model лучше baseline
  3. Information Coefficient (IC) = Spearman(factor_raw, outcome_01) — ненулевой IC = фактор несёт сигнал
  4. ROI vs baselines — торговый edge через divergence >= threshold

Факторы:
  - COMPOSITE: oracle_score (полная модель)
  - WHALE: whale_contribution_raw → sigma → probability
  - NEWS: news_contribution_raw → sigma → probability
  - SM_FRESH: sm_fresh_raw → sigma → probability
  - SM_REGISTRY: sm_registry_raw (sparse, 3.7% coverage)
  - MAKER: maker_direction_raw
  - TAKER: taker_imbalance_raw
  - RETAIL: retail_factor_raw
  - MARKET_PRICE: baseline (рынок без нашей модели)

Срезы:
  - GLOBAL
  - Sports / Crypto / Politics / Other
  - Strong divergence (|div| >= 0.05)
  - Recent (last 30d от max resolved_at)

Использование:
    python3 scripts/research/oracle_edge_attribution.py --pit data/prod_oracle_snapshots.parquet
    python3 scripts/research/oracle_edge_attribution.py  # использует дефолтный путь
"""

from __future__ import annotations

import argparse
import math
from pathlib import Path

import duckdb

DEFAULT_PIT = Path(__file__).parent.parent.parent / "data" / "prod_oracle_snapshots.parquet"
DEFAULT_OUT = Path(__file__).parent.parent.parent / "docs" / "research" / "2026-04-30-oracle-edge-attribution.md"


# ---------------------------------------------------------------------------
# Математика
# ---------------------------------------------------------------------------

def sigmoid(x: float) -> float:
    """Log-odds → probability."""
    try:
        return 1.0 / (1.0 + math.exp(-x))
    except OverflowError:
        return 0.0 if x < 0 else 1.0


def brier_score(probs: list[float], outcomes: list[int]) -> float:
    """Brier Score = mean((p - o)^2). Ниже = лучше."""
    n = len(probs)
    if n == 0:
        return float("nan")
    return sum((p - o) ** 2 for p, o in zip(probs, outcomes)) / n


def brier_skill_score(model_bs: float, baseline_bs: float) -> float:
    """BSS > 0 → model лучше baseline. BSS = 1 → идеальная калибровка."""
    if baseline_bs == 0:
        return float("nan")
    return 1.0 - model_bs / baseline_bs


def spearman_ic(xs: list[float], ys: list[float]) -> float:
    """Spearman rank correlation (IC). Упрощённо — без ties correction."""
    n = len(xs)
    if n < 10:
        return float("nan")

    def rank(arr: list[float]) -> list[float]:
        sorted_idx = sorted(range(n), key=lambda i: arr[i])
        ranks = [0.0] * n
        for rank_val, idx in enumerate(sorted_idx, 1):
            ranks[idx] = float(rank_val)
        return ranks

    rx = rank(xs)
    ry = rank(ys)
    mean_rx = sum(rx) / n
    mean_ry = sum(ry) / n
    num = sum((rx[i] - mean_rx) * (ry[i] - mean_ry) for i in range(n))
    den_x = math.sqrt(sum((r - mean_rx) ** 2 for r in rx))
    den_y = math.sqrt(sum((r - mean_ry) ** 2 for r in ry))
    if den_x == 0 or den_y == 0:
        return float("nan")
    return num / (den_x * den_y)


def pnl_for_side(side: str, market_price: float, outcome_yes: bool) -> tuple[float, bool]:
    if side == "YES":
        win = outcome_yes
    else:
        win = not outcome_yes
    if not win:
        return -1.0, False
    entry = market_price if side == "YES" else (1.0 - market_price)
    entry = max(entry, 0.01)
    return (1.0 / entry) - 1.0, True


def compute_roi(probs: list[float], market_prices: list[float], outcomes: list[int],
                div_threshold: float = 0.03) -> dict:
    """ROI при торговле по divergence = prob - market_price."""
    pnls, wins, n_entries = [], [], 0
    for prob, mp, outcome_01 in zip(probs, market_prices, outcomes):
        div = prob - mp
        if abs(div) < div_threshold:
            continue
        n_entries += 1
        side = "YES" if div > 0 else "NO"
        pnl, win = pnl_for_side(side, mp, bool(outcome_01))
        pnls.append(pnl)
        wins.append(win)

    n = len(pnls)
    if n == 0:
        return {"n_entries": 0, "wr": float("nan"), "roi": float("nan"), "sharpe": float("nan")}

    roi = sum(pnls) / n
    wr = sum(wins) / n
    std = math.sqrt(sum((p - roi) ** 2 for p in pnls) / max(n - 1, 1))
    sharpe = roi / std if std > 0 else float("nan")
    return {"n_entries": n, "wr": wr, "roi": roi, "sharpe": sharpe}


# ---------------------------------------------------------------------------
# Загрузка данных
# ---------------------------------------------------------------------------

FACTOR_COLS = [
    "oracle_score", "market_price",
    "whale_contribution_raw", "news_contribution_raw",
    "sm_fresh_raw", "sm_cluster_raw", "sm_registry_raw",
    "maker_direction_raw", "taker_imbalance_raw", "retail_factor_raw",
    "oi_factor_raw", "concentration_factor_raw", "micro_factor_raw",
]


def load_rows(pit_path: str) -> list[dict]:
    con = duckdb.connect()
    cols = ", ".join(FACTOR_COLS)
    raw = con.execute(f"""
        SELECT
            {cols},
            outcome, category, divergence, created_at, resolved_at
        FROM read_parquet('{pit_path}')
        WHERE market_price > 0.01 AND market_price < 0.99
          AND outcome IN ('yes', 'no')
    """).fetchall()

    all_cols = FACTOR_COLS + ["outcome", "category", "divergence", "created_at", "resolved_at"]
    rows = []
    for r in raw:
        d = dict(zip(all_cols, r))
        # Normalize numeric types
        for col in FACTOR_COLS:
            if d[col] is not None:
                d[col] = float(d[col])
        if d["divergence"] is not None:
            d["divergence"] = float(d["divergence"])
        rows.append(d)
    return rows


# ---------------------------------------------------------------------------
# Factor analysis
# ---------------------------------------------------------------------------

# Факторы которые представляют log-odds → конвертируем через sigmoid в probability
LOG_ODDS_FACTORS = [
    "whale_contribution_raw",
    "news_contribution_raw",
    "sm_fresh_raw",
    "sm_cluster_raw",
    "sm_registry_raw",
    "maker_direction_raw",
    "taker_imbalance_raw",
    "retail_factor_raw",
    "oi_factor_raw",
    "concentration_factor_raw",
    "micro_factor_raw",
]

FACTOR_LABELS = {
    "oracle_score": "COMPOSITE (oracle_score)",
    "market_price": "BASELINE (market_price)",
    "whale_contribution_raw": "WHALE",
    "news_contribution_raw": "NEWS",
    "sm_fresh_raw": "SM_FRESH",
    "sm_cluster_raw": "SM_CLUSTER",
    "sm_registry_raw": "SM_REGISTRY",
    "maker_direction_raw": "MAKER_DIRECTION",
    "taker_imbalance_raw": "TAKER_IMBALANCE",
    "retail_factor_raw": "RETAIL",
    "oi_factor_raw": "OI_FACTOR",
    "concentration_factor_raw": "CONCENTRATION",
    "micro_factor_raw": "MICRO",
}


def analyze_factor(
    col: str,
    rows: list[dict],
    baseline_brier: float,
) -> dict:
    """Вернуть полный attribution dict для одного фактора."""
    valid = [(r[col], r["market_price"], int(r["outcome"] == "yes")) for r in rows if r[col] is not None]
    if len(valid) < 50:
        return {
            "col": col,
            "label": FACTOR_LABELS.get(col, col),
            "n": len(valid),
            "coverage": len(valid) / max(len(rows), 1),
            "skip": True,
            "skip_reason": f"n={len(valid)} < 50",
        }

    raw_vals, market_prices, outcomes = zip(*valid, strict=False)

    # Конвертируем в probability
    if col == "market_price" or col == "oracle_score":
        probs = list(raw_vals)
    else:
        # log-odds → sigmoid (центрируем если нужно)
        probs = [sigmoid(v) for v in raw_vals]

    # Метрики
    bs = brier_score(probs, list(outcomes))
    bss = brier_skill_score(bs, baseline_brier) if baseline_brier > 0 else float("nan")
    ic = spearman_ic(list(raw_vals), list(outcomes))
    roi_stats = compute_roi(probs, list(market_prices), list(outcomes), div_threshold=0.03)

    return {
        "col": col,
        "label": FACTOR_LABELS.get(col, col),
        "n": len(valid),
        "coverage": len(valid) / max(len(rows), 1),
        "skip": False,
        "brier": bs,
        "bss": bss,
        "ic": ic,
        "roi": roi_stats,
    }


def analyze_slice(rows: list[dict]) -> dict:
    """Полный attribution для slice."""
    if not rows:
        return {}

    outcomes = [int(r["outcome"] == "yes") for r in rows]
    market_prices = [r["market_price"] for r in rows]

    # Baseline Brier (market_price)
    baseline_brier = brier_score(market_prices, outcomes)

    results = {}
    for col in ["oracle_score", "market_price"] + LOG_ODDS_FACTORS:
        results[col] = analyze_factor(col, rows, baseline_brier)

    return results


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def fmt_pct(v: float, digits: int = 1) -> str:
    if math.isnan(v):
        return "N/A"
    return f"{v:.{digits}%}"


def fmt_f(v: float, digits: int = 4) -> str:
    if math.isnan(v):
        return "N/A"
    return f"{v:+.{digits}f}"


def render_factor_table(results: dict) -> list[str]:
    """Markdown таблица attribution для одного slice."""
    lines = []
    lines.append("| Factor | N | Coverage | Brier | BSS | IC (Spearman) | ROI@div3% | WR | Verdict |")
    lines.append("|---|---:|---:|---:|---:|---:|---:|---:|---|")

    # Baseline first
    baseline = results.get("market_price", {})
    baseline_brier = baseline.get("brier", float("nan"))

    # Sort by BSS descending (best first), skipped last
    ordered = []
    for col in ["oracle_score", "market_price"] + LOG_ODDS_FACTORS:
        if col in results:
            ordered.append(results[col])

    for r in ordered:
        if r.get("skip"):
            lines.append(
                f"| {r['label']} | {r['n']:,} | {fmt_pct(r['coverage'])} "
                f"| - | - | - | - | - | ⚪ {r.get('skip_reason','')} |"
            )
            continue

        # Verdict
        bss = r.get("bss", float("nan"))
        ic = r.get("ic", float("nan"))
        roi = r["roi"].get("roi", float("nan"))

        if r["col"] == "market_price":
            verdict = f"📌 baseline (Brier={r['brier']:.4f})"
        elif math.isnan(bss):
            verdict = "⚪ N/A"
        elif bss > 0.02 and abs(ic) > 0.05:
            verdict = "🟢 SIGNAL"
        elif bss > 0 and abs(ic) > 0.02:
            verdict = "🟡 WEAK"
        elif bss < -0.02:
            verdict = "🔴 HURTS"
        else:
            verdict = "⚪ NOISE"

        lines.append(
            f"| {r['label']} | {r['n']:,} | {fmt_pct(r['coverage'])} "
            f"| {fmt_f(r['brier'], 4)} | {fmt_f(bss, 4)} | {fmt_f(ic, 4)} "
            f"| {fmt_f(roi, 4)} | {fmt_pct(r['roi'].get('wr', float('nan')))} "
            f"| {verdict} |"
        )

    return lines


def render_summary_line(slice_name: str, results: dict) -> str:
    composite = results.get("oracle_score", {})
    baseline = results.get("market_price", {})
    if composite.get("skip") or baseline.get("skip"):
        return f"| {slice_name} | - | - | - | ⚪ insufficient data |"

    bss = composite.get("bss", float("nan"))
    ic = composite.get("ic", float("nan"))
    n = composite.get("n", 0)
    roi = composite["roi"].get("roi", float("nan"))

    if math.isnan(bss):
        flag = "⚪"
    elif bss > 0.02 and abs(ic) > 0.05:
        flag = "🟢 EDGE"
    elif bss > 0:
        flag = "🟡 WEAK"
    elif bss < -0.02:
        flag = "🔴 HURTS"
    else:
        flag = "⚪ NOISE"

    return (
        f"| {slice_name} | {n:,} | {fmt_f(bss, 4)} | {fmt_f(ic, 4)} "
        f"| {fmt_f(roi, 4)} | {flag} |"
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Oracle Edge Attribution")
    parser.add_argument("--pit", type=str, default=str(DEFAULT_PIT))
    parser.add_argument("--out", type=str, default=str(DEFAULT_OUT))
    args = parser.parse_args()

    print("=" * 70)
    print("ORACLE EDGE ATTRIBUTION")
    print("=" * 70)
    print(f"Source: {args.pit}")

    rows = load_rows(args.pit)
    print(f"Loaded: {len(rows):,} snapshots")

    if not rows:
        print("ERROR: no data loaded")
        return

    # Date range
    ts_vals = [r["resolved_at"] for r in rows if r["resolved_at"] is not None]
    max_ts = max(ts_vals) if ts_vals else None
    min_ts = min(ts_vals) if ts_vals else None
    print(f"Date range: {min_ts} → {max_ts}")

    # Factor coverage
    print("\nFactor coverage:")
    for col in ["oracle_score", "market_price"] + LOG_ODDS_FACTORS:
        n_valid = sum(1 for r in rows if r.get(col) is not None)
        n_nonzero = sum(1 for r in rows if r.get(col) not in (None, 0.0))
        print(f"  {col:<30} {n_valid:>7,} ({n_valid/len(rows):.1%}) nonzero={n_nonzero:>7,} ({n_nonzero/len(rows):.1%})")

    # Slices
    recent_cutoff = None
    if max_ts:
        try:
            from datetime import timedelta
            recent_cutoff = max_ts - timedelta(days=30)
        except Exception:
            pass

    slices: dict[str, list[dict]] = {
        "GLOBAL": rows,
        "Sports": [r for r in rows if r.get("category") == "Sports"],
        "Crypto": [r for r in rows if r.get("category") == "Crypto"],
        "Politics": [r for r in rows if r.get("category") == "Politics"],
        "Other": [r for r in rows if r.get("category") not in ("Sports", "Crypto", "Politics", None)],
        "Strong_div_5pct": [r for r in rows if r.get("divergence") is not None and abs(r["divergence"]) >= 0.05],
    }
    if recent_cutoff:
        slices["Recent_30d"] = [r for r in rows if r.get("resolved_at") and r["resolved_at"] >= recent_cutoff]

    print(f"\nSlices:")
    for name, srows in slices.items():
        print(f"  {name:<25} {len(srows):>8,} rows")

    # Attribution per slice
    print("\nComputing attribution per slice...")
    slice_results = {}
    for name, srows in slices.items():
        print(f"  {name}...")
        slice_results[name] = analyze_slice(srows)

    # Build markdown
    md: list[str] = []
    md.append("# Oracle Edge Attribution")
    md.append("")
    md.append(f"**Source:** `{args.pit}`")
    md.append(f"**N snapshots:** {len(rows):,}")
    md.append(f"**Date range:** {min_ts} → {max_ts}")
    md.append(f"**Run date:** 2026-04-30")
    md.append(f"**Script:** `scripts/research/oracle_edge_attribution.py`")
    md.append("")
    md.append("## Summary по срезам (composite oracle_score)")
    md.append("")
    md.append("| Slice | N | BSS | IC | ROI@div3% | Verdict |")
    md.append("|---|---:|---:|---:|---:|---|")
    for name in slices:
        md.append(render_summary_line(name, slice_results.get(name, {})))
    md.append("")
    md.append("> BSS > 0 = oracle лучше market_price как predictor.")
    md.append("> IC (Spearman) > 0.05 = фактор несёт информацию об outcome.")
    md.append("> ROI = PnL per $1 при торговле divergence ≥ 3%.")
    md.append("")

    # Factor attribution for GLOBAL
    md.append("## Per-factor attribution (GLOBAL)")
    md.append("")
    md.append("Каждый фактор изолированно: конвертируется в probability, считается Brier/BSS/IC/ROI.")
    md.append("Так видно какой компонент приносит информацию, а какой — шум или вред.")
    md.append("")
    global_results = slice_results.get("GLOBAL", {})
    md.extend(render_factor_table(global_results))
    md.append("")

    # Per-slice full detail
    md.append("## Детали по срезам")
    md.append("")
    for name, results in slice_results.items():
        md.append(f"### {name}")
        md.append("")
        n_total = len(slices[name])
        md.append(f"**N = {n_total:,}**")
        md.append("")
        if not results:
            md.append("_Empty slice_")
            md.append("")
            continue
        md.extend(render_factor_table(results))
        md.append("")

    # Console quick summary
    print("\n" + "=" * 70)
    print("COMPOSITE ORACLE ATTRIBUTION SUMMARY")
    print("=" * 70)
    print(f"  {'Slice':<25} {'N':>8}  {'BSS':>8}  {'IC':>8}  {'ROI':>8}  Verdict")
    print("  " + "-" * 70)
    for name, results in slice_results.items():
        comp = results.get("oracle_score", {})
        if not results or comp.get("skip"):
            print(f"  {name:<25} skip")
            continue
        bss = comp.get("bss", float("nan"))
        ic = comp.get("ic", float("nan"))
        roi = comp["roi"].get("roi", float("nan"))
        n = comp.get("n", 0)

        if math.isnan(bss):
            flag = "⚪"
        elif bss > 0.02 and abs(ic) > 0.05:
            flag = "🟢 EDGE"
        elif bss > 0:
            flag = "🟡 WEAK"
        elif bss < -0.02:
            flag = "🔴 HURTS"
        else:
            flag = "⚪ NOISE"

        bss_s = f"{bss:+.4f}" if not math.isnan(bss) else "  N/A"
        ic_s = f"{ic:+.4f}" if not math.isnan(ic) else "  N/A"
        roi_s = f"{roi:+.4f}" if not math.isnan(roi) else "  N/A"
        print(f"  {name:<25} {n:>8,}  {bss_s:>8}  {ic_s:>8}  {roi_s:>8}  {flag}")

    print("\nPer-factor (GLOBAL):")
    print(f"  {'Factor':<30} {'N':>8}  {'Cover':>7}  {'BSS':>8}  {'IC':>8}  {'ROI':>8}  Verdict")
    print("  " + "-" * 80)
    for col in ["oracle_score", "market_price"] + LOG_ODDS_FACTORS:
        r = global_results.get(col, {})
        if not r:
            continue
        if r.get("skip"):
            print(f"  {r['label']:<30} {r['n']:>8,}  {'skip':>7}  {r.get('skip_reason','')}")
            continue
        bss = r.get("bss", float("nan"))
        ic = r.get("ic", float("nan"))
        roi = r["roi"].get("roi", float("nan"))
        cov = r.get("coverage", 0.0)

        if r["col"] == "market_price":
            flag = "📌 baseline"
        elif math.isnan(bss):
            flag = "⚪"
        elif bss > 0.02 and abs(ic) > 0.05:
            flag = "🟢 SIGNAL"
        elif bss > 0 and abs(ic) > 0.02:
            flag = "🟡 WEAK"
        elif bss < -0.02:
            flag = "🔴 HURTS"
        else:
            flag = "⚪ NOISE"

        bss_s = f"{bss:+.4f}" if not math.isnan(bss) else "  N/A"
        ic_s = f"{ic:+.4f}" if not math.isnan(ic) else "  N/A"
        roi_s = f"{roi:+.4f}" if not math.isnan(roi) else "  N/A"
        print(f"  {r['label']:<30} {r['n']:>8,}  {cov:>7.1%}  {bss_s:>8}  {ic_s:>8}  {roi_s:>8}  {flag}")

    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w") as f:
        f.write("\n".join(md))
    print(f"\nReport → {args.out}")


if __name__ == "__main__":
    main()

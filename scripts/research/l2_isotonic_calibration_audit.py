"""L2 isotonic calibration audit — research-only.

Запуск ТОЛЬКО на проде:
    docker exec oraclepoly-backend python scripts/research/l2_isotonic_calibration_audit.py

Контекст 2026-05-19 (post-pipeline audit):
  L2 meta_labeler систематически переоценивает (avg predicted 0.62 vs actual 0.31
  в zone 0.50-0.85). Аналогично STANDALONE_V2_LGB: 0.479 vs 0.305.

  Гипотеза: monotone isotonic regression поверх raw predictions может elиminate
  bias без переобучения всей модели. Isotonic non-parametric, не overfit'ит
  на 7k samples, и сохраняет ordering raw predictions.

Datasets:
  A) pipeline_v2_shadow_predictions: 3,173 labeled (monolith_meta_p + outcome_int)
  B) standalone_v2_predictions:       4,008 labeled (predicted_p_yes + yes_won)

  Combined: ~7,181 samples с raw_p + binary outcome.

Method:
  - Chronological 80/20 split на каждом dataset отдельно (избегаем shift между ними)
  - sklearn.isotonic.IsotonicRegression на train.{raw_p, outcome}
  - Predict на holdout → сравнить calibration metrics:
      Brier, ECE, AUC, per-bin miscalibration
  - 3 baseline для сравнения:
      1) raw — без калибровки
      2) Platt (logistic) — параметрическая sigmoid calibration
      3) Isotonic — non-parametric monotone

Output: report в stdout, decision на retrain L2 или нет.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.services.database import close_db, fetch, init_db  # noqa: E402

# ─── Data loading ──────────────────────────────────────────────────────────

_FETCH_V2_SHADOW_SQL = """
SELECT
    monolith_meta_p AS raw_p,
    outcome_int     AS outcome,
    predicted_at    AS ts
FROM pipeline_v2_shadow_predictions
WHERE resolved = true AND outcome_int IS NOT NULL
ORDER BY predicted_at ASC
"""

_FETCH_STANDALONE_V2_SQL = """
SELECT
    predicted_p_yes AS raw_p,
    yes_won         AS outcome,
    inference_at    AS ts
FROM standalone_v2_predictions
WHERE yes_won IS NOT NULL
ORDER BY inference_at ASC
"""


# ─── Metrics ───────────────────────────────────────────────────────────────


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def ece(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> float:
    """Expected Calibration Error."""
    bins = np.linspace(0, 1, n_bins + 1)
    n = len(p)
    if n == 0:
        return 0.0
    total = 0.0
    for i in range(n_bins):
        mask = (p >= bins[i]) & (p < bins[i + 1]) if i < n_bins - 1 else (p >= bins[i]) & (p <= bins[i + 1])
        if mask.sum() < 1:
            continue
        total += (mask.sum() / n) * abs(p[mask].mean() - y[mask].mean())
    return float(total)


def auc(p: np.ndarray, y: np.ndarray) -> float:
    from sklearn.metrics import roc_auc_score

    if len(np.unique(y)) < 2:
        return 0.5
    return float(roc_auc_score(y, p))


def per_bin_report(p: np.ndarray, y: np.ndarray, name: str) -> None:
    """Print per-bin calibration table."""
    bins = [(0.0, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 0.85), (0.85, 1.01)]
    print(f"\n  Per-bin calibration: {name}")
    print(f"  {'bin':<14} {'n':>6} {'pred':>8} {'actual':>8} {'over':>8}")
    for lo, hi in bins:
        mask = (p >= lo) & (p < hi)
        if mask.sum() < 5:
            continue
        bin_p = p[mask].mean()
        bin_y = y[mask].mean()
        over = bin_p - bin_y
        print(f"  [{lo:.2f}, {hi:.2f}) {mask.sum():>6} {bin_p:>8.3f} {bin_y:>8.3f} {over:>+8.3f}")


# ─── Calibration methods ───────────────────────────────────────────────────


def fit_isotonic(p_train: np.ndarray, y_train: np.ndarray):
    from sklearn.isotonic import IsotonicRegression

    iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
    iso.fit(p_train, y_train)
    return iso


def fit_platt(p_train: np.ndarray, y_train: np.ndarray):
    """Platt scaling = logistic regression на 1 feature (logit(p))."""
    from sklearn.linear_model import LogisticRegression

    eps = 1e-6
    p_clipped = np.clip(p_train, eps, 1 - eps)
    logit = np.log(p_clipped / (1 - p_clipped)).reshape(-1, 1)
    lr = LogisticRegression()
    lr.fit(logit, y_train)
    return lr


def predict_platt(lr, p: np.ndarray) -> np.ndarray:
    eps = 1e-6
    p_clipped = np.clip(p, eps, 1 - eps)
    logit = np.log(p_clipped / (1 - p_clipped)).reshape(-1, 1)
    return lr.predict_proba(logit)[:, 1]


# ─── Main ──────────────────────────────────────────────────────────────────


async def main() -> None:
    await init_db()
    try:
        print("=" * 70)
        print("L2 ISOTONIC CALIBRATION AUDIT")
        print("=" * 70)

        # Загружаем оба датасета
        v2_rows = await fetch(_FETCH_V2_SHADOW_SQL)
        sv_rows = await fetch(_FETCH_STANDALONE_V2_SQL)

        v2_p = np.array([float(r["raw_p"]) for r in v2_rows], dtype=np.float64)
        v2_y = np.array([int(r["outcome"]) for r in v2_rows], dtype=np.int32)
        sv_p = np.array([float(r["raw_p"]) for r in sv_rows], dtype=np.float64)
        sv_y = np.array([int(r["outcome"]) for r in sv_rows], dtype=np.int32)

        print(f"\nv2_shadow_predictions:    n={len(v2_p):>5}  WR={v2_y.mean():.3f}")
        print(f"standalone_v2_predictions: n={len(sv_p):>5}  WR={sv_y.mean():.3f}")

        # Chronological split per source (избегаем data leakage между датасетами).
        # Combined в один pool — оба про raw_p → outcome, target тот же binary.
        v2_cut = int(len(v2_p) * 0.8)
        sv_cut = int(len(sv_p) * 0.8)

        p_train = np.concatenate([v2_p[:v2_cut], sv_p[:sv_cut]])
        y_train = np.concatenate([v2_y[:v2_cut], sv_y[:sv_cut]])
        p_test = np.concatenate([v2_p[v2_cut:], sv_p[sv_cut:]])
        y_test = np.concatenate([v2_y[v2_cut:], sv_y[sv_cut:]])

        print(f"\nTrain: n={len(p_train):>5}  WR={y_train.mean():.3f}")
        print(f"Test:  n={len(p_test):>5}  WR={y_test.mean():.3f}")
        print(f"Test mean raw_p: {p_test.mean():.3f}  vs actual WR: {y_test.mean():.3f}")

        # ─── Baseline 1: raw predictions (no calibration) ──────────────────
        print("\n" + "=" * 70)
        print("BASELINE 1: raw (no calibration)")
        print("=" * 70)
        b_raw = brier(p_test, y_test)
        e_raw = ece(p_test, y_test)
        a_raw = auc(p_test, y_test)
        print(f"  Brier:  {b_raw:.4f}")
        print(f"  ECE:    {e_raw:.4f}")
        print(f"  AUC:    {a_raw:.4f}")
        print(f"  Mean predicted: {p_test.mean():.3f}  vs actual: {y_test.mean():.3f}")
        print(f"  Overshoot:      {p_test.mean() - y_test.mean():+.3f}")
        per_bin_report(p_test, y_test, "raw")

        # ─── Method 1: Platt scaling (parametric sigmoid) ──────────────────
        print("\n" + "=" * 70)
        print("METHOD 1: Platt scaling (parametric sigmoid)")
        print("=" * 70)
        platt = fit_platt(p_train, y_train)
        p_platt = predict_platt(platt, p_test)
        b_p = brier(p_platt, y_test)
        e_p = ece(p_platt, y_test)
        a_p = auc(p_platt, y_test)
        print(f"  Brier:  {b_p:.4f}   ({(b_raw - b_p) / b_raw * 100:+.1f}% vs raw)")
        print(f"  ECE:    {e_p:.4f}   ({(e_raw - e_p) / max(e_raw, 1e-6) * 100:+.1f}% vs raw)")
        print(f"  AUC:    {a_p:.4f}")
        print(f"  Mean predicted: {p_platt.mean():.3f}  vs actual: {y_test.mean():.3f}")
        print(f"  Overshoot:      {p_platt.mean() - y_test.mean():+.3f}")
        per_bin_report(p_platt, y_test, "Platt")

        # ─── Method 2: Isotonic regression (non-parametric monotone) ───────
        print("\n" + "=" * 70)
        print("METHOD 2: Isotonic regression (non-parametric monotone)")
        print("=" * 70)
        iso = fit_isotonic(p_train, y_train)
        p_iso = np.asarray(iso.predict(p_test), dtype=np.float64)
        b_i = brier(p_iso, y_test)
        e_i = ece(p_iso, y_test)
        a_i = auc(p_iso, y_test)
        print(f"  Brier:  {b_i:.4f}   ({(b_raw - b_i) / b_raw * 100:+.1f}% vs raw)")
        print(f"  ECE:    {e_i:.4f}   ({(e_raw - e_i) / max(e_raw, 1e-6) * 100:+.1f}% vs raw)")
        print(f"  AUC:    {a_i:.4f}")
        print(f"  Mean predicted: {p_iso.mean():.3f}  vs actual: {y_test.mean():.3f}")
        print(f"  Overshoot:      {p_iso.mean() - y_test.mean():+.3f}")
        per_bin_report(p_iso, y_test, "Isotonic")

        # ─── Summary ──────────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("SUMMARY (lower Brier/ECE is better; AUC closer to 1.0 is better)")
        print("=" * 70)
        print(f"  {'Method':<12} {'Brier':>8} {'ECE':>8} {'AUC':>8} {'Overshoot':>10}")
        print(f"  {'raw':<12} {b_raw:>8.4f} {e_raw:>8.4f} {a_raw:>8.4f} {p_test.mean() - y_test.mean():>+10.3f}")
        print(f"  {'Platt':<12} {b_p:>8.4f} {e_p:>8.4f} {a_p:>8.4f} {p_platt.mean() - y_test.mean():>+10.3f}")
        print(f"  {'Isotonic':<12} {b_i:>8.4f} {e_i:>8.4f} {a_i:>8.4f} {p_iso.mean() - y_test.mean():>+10.3f}")

        # ─── Recommendation ────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("RECOMMENDATION")
        print("=" * 70)
        best_method = min(
            [("raw", b_raw), ("Platt", b_p), ("Isotonic", b_i)],
            key=lambda x: x[1],
        )
        print(f"  Best Brier: {best_method[0]} ({best_method[1]:.4f})")
        iso_improvement = (b_raw - b_i) / b_raw * 100
        if iso_improvement > 5:
            print("  >>> Isotonic ДАЁТ ≥5% improvement vs raw — стоит deploy в production")
        elif iso_improvement > 0:
            print(f"  >>> Isotonic чуть лучше ({iso_improvement:+.1f}%) — borderline, can try")
        else:
            print("  >>> Isotonic НЕ улучшает raw — текущий L1 уже не хуже, оставить как есть")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())

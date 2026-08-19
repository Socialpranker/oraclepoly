"""L1 isotonic calibration audit — research-only.

Запуск ТОЛЬКО на проде:
    docker exec oraclepoly-backend python scripts/research/l1_isotonic_calibration_audit.py

Контекст 2026-05-19: предыдущий audit (l2_isotonic) показал что isotonic regression
поверх L2 monolith_meta_p уменьшает Brier на 37%. Но монолит = L1 cal_p (по audit).
Проверяем: Isotonic поверх L1 cal_p (per-detector calibrated_p из ml_calibrators
+ ml_calibrators_lgb) на live detector_signals.

Метод:
  - Загрузить resolved detector_signals за последние 540 дней
  - Применить current L1 (LGB if exists, else Beta, else raw_p) — точно как inference
  - Chronological 80/20 holdout
  - Fit Isotonic на train cal_p → outcome
  - Сравнить metrics на holdout: raw, L1 cal_p, L1 cal_p + Isotonic, Platt
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from app.calibration.beta_calibrator import BetaCalibrator  # noqa: E402
from app.calibration.lgb_calibrator import LgbCalibrator  # noqa: E402
from app.calibration.lgb_features import build_features  # noqa: E402
from app.services.database import close_db, fetch, init_db  # noqa: E402

_FETCH_SIGNALS_SQL = """
SELECT
    ds.id,
    ds.detector,
    ds.confidence,
    ds.expected_edge_pct,
    ds.fair_price,
    ds.current_price,
    ds.horizon_sec,
    ds.side,
    ds.event_size,
    ds.outcome,
    ds.evidence,
    ds.generated_at,
    m.category,
    m.volume_24h,
    m.end_date,
    -- Tier 3.1 features (mirror inference SQL):
    CASE
        WHEN m.end_date IS NOT NULL
        THEN EXTRACT(epoch FROM (m.end_date - ds.generated_at)) / 3600.0
        ELSE NULL
    END AS hours_to_resolution_at_signal,
    rw.recent_wr_30d,
    rw.detector_recent_count_30d
FROM detector_signals ds
LEFT JOIN markets m ON m.polymarket_id = ds.market_id
LEFT JOIN LATERAL (
    SELECT
        AVG(CASE WHEN ds2.outcome = 'WIN' THEN 1.0 ELSE 0.0 END) AS recent_wr_30d,
        COUNT(*) AS detector_recent_count_30d
      FROM detector_signals ds2
     WHERE ds2.detector = ds.detector
       AND ds2.resolved = true
       AND ds2.outcome IN ('WIN', 'LOSS')
       AND ds2.generated_at < ds.generated_at
       AND ds2.generated_at >= ds.generated_at - interval '30 days'
) rw ON true
WHERE ds.resolved = true
  AND ds.outcome IN ('WIN', 'LOSS')
  AND ds.generated_at > now() - interval '540 days'
ORDER BY ds.generated_at ASC
"""


_FETCH_BETA_SQL = "SELECT detector, params FROM ml_calibrators"
_FETCH_LGB_SQL = "SELECT detector, model_payload FROM ml_calibrators_lgb WHERE is_active = true"


# ─── Metrics ───────────────────────────────────────────────────────────────


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def ece(p: np.ndarray, y: np.ndarray, n_bins: int = 10) -> float:
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
    bins = [(0.0, 0.30), (0.30, 0.50), (0.50, 0.70), (0.70, 0.85), (0.85, 1.01)]
    print(f"\n  Per-bin calibration: {name}")
    print(f"  {'bin':<14} {'n':>6} {'pred':>8} {'actual':>8} {'over':>8}")
    for lo, hi in bins:
        mask = (p >= lo) & (p < hi)
        if mask.sum() < 5:
            continue
        print(
            f"  [{lo:.2f}, {hi:.2f}) {mask.sum():>6} "
            f"{p[mask].mean():>8.3f} {y[mask].mean():>8.3f} "
            f"{p[mask].mean() - y[mask].mean():>+8.3f}"
        )


# ─── L1 inference ──────────────────────────────────────────────────────────


async def load_calibrators():
    beta_rows = await fetch(_FETCH_BETA_SQL)
    beta = {}
    for r in beta_rows:
        params = r["params"]
        if isinstance(params, str):
            params = json.loads(params)
        try:
            beta[r["detector"]] = BetaCalibrator.deserialize(params)
        except Exception:
            pass

    lgb_rows = await fetch(_FETCH_LGB_SQL)
    lgb = {}
    for r in lgb_rows:
        payload = r["model_payload"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        try:
            lgb[r["detector"]] = LgbCalibrator.deserialize(payload)
        except Exception:
            pass
    return beta, lgb


def l1_cal_p(row, beta_cals, lgb_cals) -> float:
    """Воспроизводим production inference path LGB → Beta → raw_p."""
    detector = row["detector"]
    raw_p = float(row["confidence"])

    # LGB
    lgb = lgb_cals.get(detector)
    if lgb is not None and lgb.params is not None:
        try:
            feats = build_features(dict(row))
            p = float(lgb.transform(feats))
            if 0.0 < p < 1.0:
                return p
        except Exception:
            pass

    # Beta
    beta = beta_cals.get(detector)
    if beta is not None and beta.params is not None:
        try:
            return float(beta.transform(raw_p))
        except Exception:
            pass

    return raw_p


# ─── Main ──────────────────────────────────────────────────────────────────


async def main() -> None:
    await init_db()
    try:
        print("=" * 70)
        print("L1 ISOTONIC CALIBRATION AUDIT (поверх current L1 cal_p)")
        print("=" * 70)

        beta_cals, lgb_cals = await load_calibrators()
        print(f"\nLoaded {len(beta_cals)} Beta + {len(lgb_cals)} LGB calibrators")

        rows = await fetch(_FETCH_SIGNALS_SQL)
        print(f"Fetched {len(rows)} labeled signals (540d)")

        if len(rows) < 1000:
            print("⚠ Too few signals — abort")
            return

        # Compute current L1 cal_p для каждого
        raw_p = np.array([float(r["confidence"]) for r in rows], dtype=np.float64)
        cal_p = np.array([l1_cal_p(r, beta_cals, lgb_cals) for r in rows], dtype=np.float64)
        y = np.array([1 if r["outcome"] == "WIN" else 0 for r in rows], dtype=np.int32)

        # Chronological split
        cut = int(len(rows) * 0.8)
        cal_train, cal_test = cal_p[:cut], cal_p[cut:]
        raw_test = raw_p[cut:]  # raw_p train не используется (Isotonic поверх cal_p)
        y_train, y_test = y[:cut], y[cut:]

        print(f"Train: n={len(cal_train)}  WR={y_train.mean():.3f}")
        print(f"Test:  n={len(cal_test)}   WR={y_test.mean():.3f}")
        print(f"Test mean cal_p: {cal_test.mean():.3f}")

        # ─── Baseline: current L1 cal_p (production) ───────────────────────
        print("\n" + "=" * 70)
        print("BASELINE: current L1 cal_p (production inference path)")
        print("=" * 70)
        b_l1 = brier(cal_test, y_test)
        e_l1 = ece(cal_test, y_test)
        a_l1 = auc(cal_test, y_test)
        print(f"  Brier:  {b_l1:.4f}")
        print(f"  ECE:    {e_l1:.4f}")
        print(f"  AUC:    {a_l1:.4f}")
        print(f"  Mean predicted: {cal_test.mean():.3f}  vs actual: {y_test.mean():.3f}")
        print(f"  Overshoot:      {cal_test.mean() - y_test.mean():+.3f}")
        per_bin_report(cal_test, y_test, "current L1")

        # ─── Reference: raw_p (no calibration at all) ──────────────────────
        print("\n" + "=" * 70)
        print("REFERENCE: raw_p (no calibration)")
        print("=" * 70)
        b_raw = brier(raw_test, y_test)
        e_raw = ece(raw_test, y_test)
        a_raw = auc(raw_test, y_test)
        print(f"  Brier:  {b_raw:.4f}")
        print(f"  ECE:    {e_raw:.4f}")
        print(f"  AUC:    {a_raw:.4f}")

        # ─── Method: Isotonic поверх current L1 ─────────────────────────────
        print("\n" + "=" * 70)
        print("METHOD: Isotonic regression поверх current L1 cal_p")
        print("=" * 70)
        from sklearn.isotonic import IsotonicRegression

        iso = IsotonicRegression(y_min=0.0, y_max=1.0, out_of_bounds="clip")
        iso.fit(cal_train, y_train)
        p_iso = np.asarray(iso.predict(cal_test), dtype=np.float64)

        b_iso = brier(p_iso, y_test)
        e_iso = ece(p_iso, y_test)
        a_iso = auc(p_iso, y_test)
        print(f"  Brier:  {b_iso:.4f}   ({(b_l1 - b_iso) / b_l1 * 100:+.1f}% vs L1)")
        print(f"  ECE:    {e_iso:.4f}   ({(e_l1 - e_iso) / max(e_l1, 1e-6) * 100:+.1f}% vs L1)")
        print(f"  AUC:    {a_iso:.4f}   ({a_iso - a_l1:+.4f} vs L1)")
        print(f"  Mean predicted: {p_iso.mean():.3f}  vs actual: {y_test.mean():.3f}")
        print(f"  Overshoot:      {p_iso.mean() - y_test.mean():+.3f}")
        per_bin_report(p_iso, y_test, "L1 + Isotonic")

        # ─── Summary ──────────────────────────────────────────────────────
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"  {'Method':<20} {'Brier':>8} {'ECE':>8} {'AUC':>8} {'Overshoot':>10}")
        print(f"  {'raw_p':<20} {b_raw:>8.4f} {e_raw:>8.4f} {a_raw:>8.4f} {raw_test.mean() - y_test.mean():>+10.3f}")
        print(f"  {'current L1':<20} {b_l1:>8.4f} {e_l1:>8.4f} {a_l1:>8.4f} {cal_test.mean() - y_test.mean():>+10.3f}")
        print(f"  {'L1 + Isotonic':<20} {b_iso:>8.4f} {e_iso:>8.4f} {a_iso:>8.4f} {p_iso.mean() - y_test.mean():>+10.3f}")

        improvement = (b_l1 - b_iso) / b_l1 * 100
        print(f"\n  Isotonic vs L1: {improvement:+.1f}% Brier improvement")
        if improvement > 5:
            print("  >>> Isotonic поверх L1 даёт ≥5% — embed в production")
        elif improvement > 0:
            print(f"  >>> Marginal ({improvement:+.1f}%) — нужны 2-4 недели больше данных")
        else:
            print("  >>> Isotonic не улучшает L1 — current L1 уже хорошо")
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())

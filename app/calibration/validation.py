"""ML validation harness — gate criteria для prevent D-MLCalibrator-style катастроф.

Источник: deep research ml-validation-methodology-2026-05.

Главные принципы (из R1):
1. **Per-segment AUC** обязателен для бинарных моделей с heavy-tailed price distribution.
   Aggregate AUC скрывает localized failures (D-MLCalibrator: global 0.94, per-band 0.52).
2. **Calibration > Discrimination** для trading. Нужна точная p, не только ranking.
3. **Brier decomposition** (Reliability − Resolution + Uncertainty) — лучшая single metric.
4. **Per-event bootstrap CI** — markets коррелированы внутри event.

HARD gate criteria (требуются до promote):
- Per-band AUC > 0.55 в каждом active price segment
- ECE < 0.05 на главном segment
- Brier Skill Score > 0.05 vs market price baseline
- Per-event bootstrap 95% CI lower bound on ROI > 0 (after fees + slippage)
- Calibration-in-the-large |slope - 1| < 0.15
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np

# ── HARD gate thresholds ─────────────────────────────────────────────────────

MIN_PER_BAND_AUC = 0.55
MAX_ECE = 0.05
MIN_BRIER_SKILL = 0.05
MAX_CALIBRATION_SLOPE_DEVIATION = 0.15
MIN_BAND_SIZE = 100  # bands с n < 100 считаются noise, не gate
DEFAULT_PRICE_BINS = (0.0, 0.10, 0.30, 0.50, 0.70, 0.85, 0.95, 1.0)


@dataclass
class SegmentMetrics:
    """Метрики per (price segment)."""

    segment: str
    n: int
    base_rate: float  # P(WIN) в segment
    auc: float | None  # None если у одного класса < 2 sample
    ece: float
    mean_pred: float
    mean_outcome: float


@dataclass
class BrierDecomposition:
    """Murphy's Brier decomposition.

    BS = Reliability − Resolution + Uncertainty
    - Reliability: насколько предсказания калиброваны (low = good)
    - Resolution: насколько модель разделяет outcomes (high = good)
    - Uncertainty: внутренняя неопределённость target (база, не зависит от модели)
    """

    brier_score: float
    reliability: float
    resolution: float
    uncertainty: float


@dataclass
class ValidationReport:
    """Полный validation report для ML модели."""

    name: str
    n_total: int
    n_events: int  # honest sample size
    overall_brier: float
    overall_auc: float | None
    brier_decomp: BrierDecomposition | None
    brier_skill_score: float  # vs market_price baseline
    calibration_slope: float
    calibration_intercept: float
    per_segment: list[SegmentMetrics]
    bootstrap_roi_ci: tuple[float, float] | None  # (lower, upper) at 95%
    failures: list[str] = field(default_factory=list)  # human-readable gate failures

    @property
    def passes_gate(self) -> bool:
        return len(self.failures) == 0


# ── Core metrics ─────────────────────────────────────────────────────────────


def auc_score(y_true: Sequence[int], y_pred: Sequence[float]) -> float | None:
    """ROC AUC. Возвращает None если у одного класса < 2 sample."""
    y = np.asarray(list(y_true), dtype=int)
    p = np.asarray(list(y_pred), dtype=float)
    if len(y) < 2:
        return None
    n_pos = int((y == 1).sum())
    n_neg = int((y == 0).sum())
    if n_pos < 2 or n_neg < 2:
        return None
    # Mann-Whitney U formulation: AUC = U / (n_pos × n_neg)
    order = np.argsort(p)
    ranks = np.empty_like(order, dtype=float)
    ranks[order] = np.arange(1, len(p) + 1, dtype=float)
    # Mid-rank для ties
    sorted_p = p[order]
    i = 0
    while i < len(sorted_p):
        j = i
        while j + 1 < len(sorted_p) and sorted_p[j + 1] == sorted_p[i]:
            j += 1
        if j > i:
            mid = (ranks[order[i]] + ranks[order[j]]) / 2
            for k in range(i, j + 1):
                ranks[order[k]] = mid
        i = j + 1
    sum_ranks_pos = float(ranks[y == 1].sum())
    u = sum_ranks_pos - n_pos * (n_pos + 1) / 2
    return u / (n_pos * n_neg)


def expected_calibration_error(
    y_true: Sequence[int],
    y_pred: Sequence[float],
    n_bins: int = 10,
) -> float:
    """ECE: weighted absolute deviation между mean(p) и mean(y) в каждом bin.

    Formula: sum(|bin_count|/n × |bin_pred - bin_acc|)
    """
    y = np.asarray(list(y_true), dtype=float)
    p = np.asarray(list(y_pred), dtype=float)
    if len(y) == 0:
        return 0.0
    # Equal-width bins on [0, 1]
    bins = np.linspace(0.0, 1.0, n_bins + 1)
    bins[-1] += 1e-9  # include 1.0
    bin_idx = np.digitize(p, bins) - 1
    bin_idx = np.clip(bin_idx, 0, n_bins - 1)
    n = len(p)
    ece = 0.0
    for b in range(n_bins):
        mask = bin_idx == b
        if not mask.any():
            continue
        bin_pred = p[mask].mean()
        bin_acc = y[mask].mean()
        ece += (mask.sum() / n) * abs(bin_pred - bin_acc)
    return float(ece)


def brier_decomposition(
    y_true: Sequence[int],
    y_pred: Sequence[float],
    n_bins: int = 10,
) -> BrierDecomposition:
    """Murphy 1973 decomposition.

    BS = (1/N) sum_k n_k (p_k - y_k)^2  − ... (полный вид через bins)
    """
    y = np.asarray(list(y_true), dtype=float)
    p = np.asarray(list(y_pred), dtype=float)
    n = len(y)
    if n == 0:
        return BrierDecomposition(0.0, 0.0, 0.0, 0.0)
    bs = float(np.mean((p - y) ** 2))
    base_rate = float(y.mean())
    uncertainty = base_rate * (1 - base_rate)

    bins = np.linspace(0.0, 1.0, n_bins + 1)
    bins[-1] += 1e-9
    bin_idx = np.digitize(p, bins) - 1
    bin_idx = np.clip(bin_idx, 0, n_bins - 1)

    reliability = 0.0
    resolution = 0.0
    for b in range(n_bins):
        mask = bin_idx == b
        nk = mask.sum()
        if nk == 0:
            continue
        pk = float(p[mask].mean())
        yk = float(y[mask].mean())
        reliability += (nk / n) * (pk - yk) ** 2
        resolution += (nk / n) * (yk - base_rate) ** 2

    return BrierDecomposition(
        brier_score=bs,
        reliability=reliability,
        resolution=resolution,
        uncertainty=uncertainty,
    )


def brier_skill_score(
    y_true: Sequence[int],
    y_pred: Sequence[float],
    y_baseline: Sequence[float],
) -> float:
    """BSS vs baseline (e.g. market price).

    BSS = 1 − BS_model / BS_baseline
    BSS > 0 → model лучше baseline.
    """
    y = np.asarray(list(y_true), dtype=float)
    p = np.asarray(list(y_pred), dtype=float)
    bp = np.asarray(list(y_baseline), dtype=float)
    if len(y) == 0:
        return 0.0
    bs_model = float(np.mean((p - y) ** 2))
    bs_base = float(np.mean((bp - y) ** 2))
    if bs_base <= 0:
        return 0.0
    return 1.0 - bs_model / bs_base


def calibration_intercept_slope(
    y_true: Sequence[int],
    y_pred: Sequence[float],
) -> tuple[float, float]:
    """Linear regression y ~ a + b*p. Идеально calibrated: a=0, b=1.

    Возвращает (intercept, slope). Использует logit trick для y in {0,1}:
    мы делаем простую linear regression в probability space (Steyerberg 2019 style).
    """
    y = np.asarray(list(y_true), dtype=float)
    p = np.asarray(list(y_pred), dtype=float)
    n = len(y)
    if n < 2:
        return (0.0, 1.0)
    # Avoid degenerate
    var_p = float(np.var(p))
    if var_p <= 1e-12:
        return (float(y.mean() - p.mean()), 1.0)
    cov = float(np.cov(p, y, ddof=0)[0, 1])
    slope = cov / var_p
    intercept = float(y.mean() - slope * p.mean())
    return (intercept, slope)


# ── Per-segment ──────────────────────────────────────────────────────────────


def segment_metrics(
    y_true: Sequence[int],
    y_pred: Sequence[float],
    segment_value: Sequence[float],
    bins: Sequence[float] = DEFAULT_PRICE_BINS,
) -> list[SegmentMetrics]:
    """Compute metrics per price-band segment."""
    y = np.asarray(list(y_true), dtype=int)
    p = np.asarray(list(y_pred), dtype=float)
    s = np.asarray(list(segment_value), dtype=float)
    n = len(y)
    if n == 0:
        return []

    edges = np.asarray(list(bins), dtype=float)
    if edges[-1] < 1.0:
        edges = np.concatenate([edges, [1.0 + 1e-9]])
    out: list[SegmentMetrics] = []
    for i in range(len(edges) - 1):
        lo, hi = edges[i], edges[i + 1]
        mask = (s >= lo) & (s < hi)
        nk = int(mask.sum())
        if nk == 0:
            continue
        y_seg = y[mask]
        p_seg = p[mask]
        out.append(
            SegmentMetrics(
                segment=f"[{lo:.2f}, {hi:.2f})",
                n=nk,
                base_rate=float(y_seg.mean()),
                auc=auc_score(y_seg, p_seg),
                ece=expected_calibration_error(y_seg, p_seg),
                mean_pred=float(p_seg.mean()),
                mean_outcome=float(y_seg.mean()),
            )
        )
    return out


# ── Bootstrap CI (cluster по event_id) ──────────────────────────────────────


def event_cluster_bootstrap_roi(
    event_ids: Sequence[str],
    roi_per_row: Sequence[float],
    n_resamples: int = 1000,
    confidence: float = 0.95,
    seed: int | None = None,
) -> tuple[float, float]:
    """Per-event cluster bootstrap для ROI confidence interval.

    Sample events with replacement (не rows!), compute mean ROI per resample.
    Возвращает (lower, upper) at confidence.
    """
    if not event_ids:
        return (0.0, 0.0)
    rng = np.random.default_rng(seed)
    rois = np.asarray(list(roi_per_row), dtype=float)
    events = np.asarray(list(event_ids))
    if len(events) != len(rois):
        raise ValueError("len mismatch event_ids vs roi_per_row")

    # Group rows by event
    unique_events, inverse = np.unique(events, return_inverse=True)
    n_events = len(unique_events)
    rows_per_event: list[np.ndarray] = [np.where(inverse == i)[0] for i in range(n_events)]
    event_mean_roi = np.array([rois[idx].mean() for idx in rows_per_event])

    resamples = np.empty(n_resamples, dtype=float)
    for r in range(n_resamples):
        sampled = rng.integers(0, n_events, size=n_events)
        resamples[r] = float(event_mean_roi[sampled].mean())

    alpha = 1.0 - confidence
    lower = float(np.percentile(resamples, alpha / 2 * 100))
    upper = float(np.percentile(resamples, (1 - alpha / 2) * 100))
    return (lower, upper)


# ── Full report + gate ──────────────────────────────────────────────────────


def validate_model(
    name: str,
    y_true: Sequence[int],
    y_pred: Sequence[float],
    market_price: Sequence[float],
    segment_value: Sequence[float],
    event_ids: Sequence[str] | None = None,
    roi_per_row: Sequence[float] | None = None,
    bins: Sequence[float] = DEFAULT_PRICE_BINS,
    bootstrap_seed: int | None = 42,
) -> ValidationReport:
    """Full validation report с HARD gate checks.

    Args:
        name: identifier модели для report
        y_true: per-row outcome (1=WIN, 0=LOSS)
        y_pred: per-row P[WIN] от модели
        market_price: per-row implied prob от market quote (используется как baseline для BSS)
        segment_value: per-row значение для бакетинга (обычно entry_price или yes_price)
        event_ids: per-row event_id для bootstrap CI (опционально, но настоятельно рекомендуется)
        roi_per_row: per-row realized ROI (требуется для bootstrap CI)
        bins: границы price segments
        bootstrap_seed: seed для per-event bootstrap CI. Дефолт 42 — CI
            воспроизводим при одинаковом входе (иначе нижняя граница гуляет
            вокруг нуля и флейкает gate `ci[0] > 0`). None — недетерминированный.
    """
    y = np.asarray(list(y_true), dtype=int)
    p = np.asarray(list(y_pred), dtype=float)
    bp = np.asarray(list(market_price), dtype=float)

    n_total = len(y)
    overall_brier = float(np.mean((p - y) ** 2)) if n_total > 0 else 0.0
    overall_auc = auc_score(y, p)
    decomp = brier_decomposition(y, p)
    bss = brier_skill_score(y, p, bp)
    intercept, slope = calibration_intercept_slope(y, p)
    per_seg = segment_metrics(y, p, segment_value, bins=bins)

    n_events = len(set(event_ids)) if event_ids else n_total
    boot_ci: tuple[float, float] | None = None
    if event_ids is not None and roi_per_row is not None:
        boot_ci = event_cluster_bootstrap_roi(event_ids, roi_per_row, seed=bootstrap_seed)

    failures: list[str] = []
    # Gate 1: per-band AUC > MIN_PER_BAND_AUC в каждом active segment
    for seg in per_seg:
        if seg.n < MIN_BAND_SIZE:
            continue
        if seg.auc is None:
            continue
        if seg.auc < MIN_PER_BAND_AUC:
            failures.append(f"per-band AUC {seg.auc:.3f} < {MIN_PER_BAND_AUC} в segment {seg.segment} (n={seg.n})")

    # Gate 2: ECE < MAX_ECE на overall
    overall_ece = expected_calibration_error(y, p)
    if overall_ece > MAX_ECE:
        failures.append(f"overall ECE {overall_ece:.3f} > {MAX_ECE}")

    # Gate 3: BSS > MIN_BRIER_SKILL
    if bss < MIN_BRIER_SKILL:
        failures.append(f"Brier Skill Score {bss:.3f} < {MIN_BRIER_SKILL}")

    # Gate 4: calibration slope close to 1.0
    if abs(slope - 1.0) > MAX_CALIBRATION_SLOPE_DEVIATION:
        failures.append(f"calibration slope {slope:.3f} deviates from 1.0 by > {MAX_CALIBRATION_SLOPE_DEVIATION}")

    # Gate 5: bootstrap CI lower > 0
    if boot_ci is not None and boot_ci[0] <= 0.0:
        failures.append(f"per-event bootstrap 95% CI lower {boot_ci[0]:.3f} <= 0 (ROI not significantly positive)")

    return ValidationReport(
        name=name,
        n_total=n_total,
        n_events=n_events,
        overall_brier=overall_brier,
        overall_auc=overall_auc,
        brier_decomp=decomp,
        brier_skill_score=bss,
        calibration_slope=slope,
        calibration_intercept=intercept,
        per_segment=per_seg,
        bootstrap_roi_ci=boot_ci,
        failures=failures,
    )


def format_report(report: ValidationReport) -> str:
    """Pretty-print отчёт для логов / CI output."""
    lines = []
    lines.append(f"=== Validation Report: {report.name} ===")
    lines.append(
        f"n_rows={report.n_total:,}  n_events={report.n_events:,}  AUC={report.overall_auc:.3f}"
        if report.overall_auc is not None
        else f"n_rows={report.n_total:,}  n_events={report.n_events:,}  AUC=N/A"
    )
    if report.brier_decomp is not None:
        d = report.brier_decomp
        lines.append(
            f"Brier={d.brier_score:.4f}  Reliability={d.reliability:.4f}  "
            f"Resolution={d.resolution:.4f}  Uncertainty={d.uncertainty:.4f}"
        )
    lines.append(
        f"BSS vs market price = {report.brier_skill_score:+.4f}  "
        f"slope={report.calibration_slope:.3f}  intercept={report.calibration_intercept:+.3f}"
    )
    if report.bootstrap_roi_ci is not None:
        lo, hi = report.bootstrap_roi_ci
        lines.append(f"Bootstrap 95% CI on ROI: [{lo:+.3f}, {hi:+.3f}]")

    lines.append("Per-segment:")
    lines.append(f"  {'segment':14s} {'n':>6s} {'base':>6s} {'AUC':>6s} {'ECE':>6s} {'mean_p':>7s} {'mean_y':>7s}")
    for seg in report.per_segment:
        auc_str = f"{seg.auc:.3f}" if seg.auc is not None else "N/A"
        lines.append(
            f"  {seg.segment:14s} {seg.n:>6,} {seg.base_rate:>5.1%} {auc_str:>6s} "
            f"{seg.ece:>5.3f} {seg.mean_pred:>6.3f} {seg.mean_outcome:>6.3f}"
        )

    lines.append("")
    if report.passes_gate:
        lines.append(">>> GATE: PASS")
    else:
        lines.append(">>> GATE: FAIL")
        for f in report.failures:
            lines.append(f"  - {f}")
    return "\n".join(lines)

# G18 Calibration Edge — Spec

**Status:** Spec (pre-implementation)
**Class:** NOT a detector — **multiplicative improvement on existing detectors**
**Owner:** owner
**Source:** [research/polymarket-orthogonal-strategies](../../research/polymarket-orthogonal-strategies/report.md) §4, [research/prediction-market-edge-sources](../../research/prediction-market-edge-sources/report.md) §6
**ADR:** [0001 Dual Model Paradigm](../adr/0001_dual_model_paradigm.md)

## Why this matters more than a new detector

Из [prediction-market-edge-sources](../../research/prediction-market-edge-sources/report.md) §6:

> Probability errors дают LINEAR degradation, sizing errors только QUADRATIC.
> Binding constraint — calibration pipeline accuracy.

И из orthogonal-strategies §4:

> Calibration Edge ловит рынки в категориях с доказанной неэффективностью (Sports, Culture), где другие стратегии не имеют преимущества.

**Hypothesis:** наши 24 существующих detector'а emit `confidence` и `fair_price` базируясь на canonical empirical WR ranges. Но эти ranges aggregated. Если разбить по `(category, price_bin)` — оракулу можно дать более точную **conditional probability**:

```
P(YES_won | detector, category, price_bin) = historical_WR_in_that_subset
```

И использовать это как **calibration overlay** на raw detector confidence.

## Mental model

Текущая модель:
```python
signal.confidence = 0.55  # detector's hardcoded WR estimate
signal.fair_price = 0.55  # implies P(YES) = 55%
```

Новая модель:
```python
# After detector emits raw signal:
calibrated_confidence = lookup_historical_wr(
    detector=signal.detector,
    category=market.category,
    price_bin=floor(signal.current_price * 10) / 10,  # 0.0, 0.1, ..., 0.9
)
signal.confidence = bayes_combine(signal.confidence, calibrated_confidence)
```

Bayes combine может быть простым weighted avg по historical sample size:
```python
def bayes_combine(prior, empirical, n_empirical, prior_weight=30):
    # Если у нас 30+ empirical observations — overrides prior
    if n_empirical < 30: return prior  # insufficient data
    return (prior_weight * prior + n_empirical * empirical) / (prior_weight + n_empirical)
```

## Что нужно построить

### 1. Calibration lookup table

```sql
CREATE TABLE detector_calibration_bins (
    detector TEXT NOT NULL,
    category TEXT NOT NULL,
    price_bin NUMERIC(3,1) NOT NULL,  -- 0.0, 0.1, ..., 0.9
    n_observations INTEGER NOT NULL,
    n_wins INTEGER NOT NULL,
    wr_observed NUMERIC(5,4),
    wilson_lower_95 NUMERIC(5,4),
    wilson_upper_95 NUMERIC(5,4),
    sample_window_start TIMESTAMPTZ,
    sample_window_end TIMESTAMPTZ,
    refit_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (detector, category, price_bin)
);
```

### 2. Refit job

`app/jobs/calibration_bins_refit.py` — weekly cron, считает actual WR per `(detector, category, price_bin)` за trailing 90 days resolved signals.

### 3. Calibration service extension

Существующий `app/services/calibration_service.py` already does 3-tier fallback (Redis → DB → hardcoded). Добавляем 4-th tier: **per-bin calibration**.

Logic:
```python
calibration = bin_lookup.get((detector, category, price_bin))
if calibration and calibration.n_observations >= 30:
    return bayes_combine(detector_prior, calibration.wr_observed, calibration.n_observations)
else:
    return detector_prior  # fallback to existing
```

### 4. Detector wiring

Все existing detectors уже emit `Signal` с `confidence`. Calibration overlay делается в `detector_signal_service.py` ПЕРЕД insert в БД. Detector сам не меняется — это middleware.

## Validation

**Backtest replay:**

Загрузить все resolved signals из `detector_signals` за последние 6 месяцев. Для каждого:
1. Compute calibrated_confidence от G18 vs raw_confidence
2. Compare Brier scores: `(predicted - actual)²`
3. Report delta per detector

**Pass criteria:**
- Overall Brier improvement ≥ 0.005 (significant но не overfit)
- НЕ ухудшает Brier на любом single detector
- Calibration plot (10 bins) показывает better calibration line

**Metrics:**
- `Brier_raw` vs `Brier_calibrated` per detector
- `Brier_raw` vs `Brier_calibrated` per category
- Wilson CI on improvement direction

## Implementation plan

### Phase 0 — Validation script (1 day)

```bash
python scripts/research/g18_calibration_replay.py \
    --start 2025-11-01 --end 2026-05-01 \
    --out docs/backtests/G18_calibration_edge.md
```

Output: per-detector before/after Brier comparison.

### Phase 1 — Migration + table (0.5 day, если validation passes)

`migrations/<ts>_create_detector_calibration_bins.sql`

### Phase 2 — Refit job (0.5 day)

`app/jobs/calibration_bins_refit.py` — weekly cron, populates table.

### Phase 3 — Calibration service extension (1 day)

Extend `CalibrationService` to use per-bin lookup. Add feature flag `CALIBRATION_BINS_ENABLED` для safety rollback.

### Phase 4 — Shadow validation (7 days)

С flag ON только на 1 detector (e.g. D44, который KEEPER), compare live signals quality.

### Phase 5 — Rollout

Поэтапно включить на все detectors. Не Live до 14-28d virtual validation.

## Risk

1. **Sparse bins:** некоторые `(detector × category × price_bin)` будут иметь n < 30. Mitigation: fallback to detector prior.
2. **Overfitting historical:** 90-day window может не отразить regime shift. Mitigation: refit weekly, monitor drift.
3. **Detector interaction:** меняя calibration одного detector, мы можем сместить ensemble behavior. Mitigation: feature flag для rollback.

## Expected impact

Не знаем quantitatively заранее, но из research:
- `Brier_improvement >= 0.005` = вероятно (calibration is well-known leverage)
- Best case: -0.02 Brier overall → 5-10% PnL improvement на всех detectors

**Это >5 новых detector'ов по EV даже в conservative case.**

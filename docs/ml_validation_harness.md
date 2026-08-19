# ML Validation Harness — gate для prevent D-MLCalibrator катастроф

**Module:** [app/calibration/validation.py](../app/calibration/validation.py)
**Tests:** [tests/test_validation.py](../tests/test_validation.py)
**Источник:** R1 deep research [ml-validation-methodology-2026-05](../06-desk-research/ml-validation-methodology-2026-05/2026-05-02.md)

## Зачем

D-MLCalibratorTopDecile (`memory/d_ml_calibrator_topdecile_2026-05-02.md`) показал
что **global AUC = 0.94** обманчив — оказалось что это artifact extreme zones
(yes_price < 0.10 или > 0.90). Per-band AUC в [0.40, 0.60] был **0.526** = random.
Top-decile ROI -18.3%, daily -$63.93.

Цель harness: до promote ЛЮБОЙ ML модели прогнать через 5 HARD gate criteria.
Без всех 5 PASS — модель не попадает в production.

## 5 HARD gate criteria

| # | Criterion | Threshold | Почему |
|---|---|---|---|
| 1 | **Per-band AUC** > 0.55 в каждом active price segment с n ≥ 100 | `MIN_PER_BAND_AUC = 0.55` | Защита от D-MLCalibrator-style aggregate iteration. Не доверяй global AUC |
| 2 | **Overall ECE** < 0.05 | `MAX_ECE = 0.05` | Calibration > Discrimination для trading (нужна точная p, не ranking) |
| 3 | **Brier Skill Score vs market price** > 0.05 | `MIN_BRIER_SKILL = 0.05` | Модель должна быть ЛУЧШЕ market — иначе зачем мы её используем? |
| 4 | **Calibration slope** \|slope - 1.0\| < 0.15 | `MAX_CALIBRATION_SLOPE_DEVIATION = 0.15` | Steyerberg 2019 — slope ≈ 1 значит модель "правильно агрессивна" |
| 5 | **Per-event bootstrap 95% CI on ROI** lower > 0 | derived | n_rows инфлируется per-event — реальный effect size в events |

Все 5 защищают от разных failure modes.

## Использование (минимальный пример)

```python
from app.calibration.validation import validate_model, format_report

report = validate_model(
    name="L2_LGB_v20260502",
    y_true=outcomes,            # [1, 0, 1, 1, ...]
    y_pred=model_predictions,    # [0.62, 0.18, 0.78, 0.71, ...]
    market_price=current_prices, # implied prob от market quote
    segment_value=current_prices, # обычно сегментируем по entry_price/yes_price
    event_ids=event_ids,          # для bootstrap CI (cluster по event)
    roi_per_row=realized_rois,   # для bootstrap ROI CI
)

print(format_report(report))

if not report.passes_gate:
    raise ValueError(f"Model failed gate: {report.failures}")

# Promote model to active...
```

## Custom segment bins

По умолчанию `DEFAULT_PRICE_BINS = (0.0, 0.10, 0.30, 0.50, 0.70, 0.85, 0.95, 1.0)`.
Они оптимизированы под Polymarket binary markets (sharp split на extreme zones).

Для ML на specific zone (e.g. D7b zone [0.85, 0.97]) можно сузить:

```python
report = validate_model(
    ...,
    bins=(0.85, 0.88, 0.92, 0.95, 0.97),  # 4 narrow bands в D7b region
)
```

## Per-event bootstrap CI

Markets коррелированы внутри event (memory `event_idempotency_research_2026-04-27`:
73% events имеют >1 market, avg 9.4). `n_rows` в 3-25× инфлирует confidence —
реальный effect size = `n_events`.

`event_cluster_bootstrap_roi()` делает stationary bootstrap **events** (не rows)
с replacement. Каждый resample = N_events events sampled, mean ROI считается
across all rows в выбранных events.

**Important:** roi_per_row должен быть net (после fees + slippage). Иначе CI
overoptimistic.

## Brier decomposition (Murphy 1973)

```python
from app.calibration.validation import brier_decomposition

d = brier_decomposition(y_true, y_pred)
print(f"Brier = {d.brier_score:.4f}")
print(f"  Reliability = {d.reliability:.4f}  (low = good calibration)")
print(f"  Resolution  = {d.resolution:.4f}   (high = good discrimination)")
print(f"  Uncertainty = {d.uncertainty:.4f}  (base rate variance — model-independent)")
```

**Identity:** `BS = Reliability − Resolution + Uncertainty`

Используется для diagnostic: если `Resolution` low — модель не разделяет outcomes
(подобно random). Если `Reliability` high — predictions miscalibrated даже если
ranking хороший.

## Когда применять gate

**Обязательно:**
- Перед promote ANY ML model в `ml_calibrators`, `ml_event_selector`, `ml_market_picker`, etc.
- В CI ML training jobs (`ml_*_fit.py`) — fail если gate не прошёл
- В weekly retrain job — если новая модель не лучше active по gate, оставить active

**Опционально:**
- В backtest scripts (`scripts/research/*.py`) — для сравнения hypothesis vs baseline
- В shadow stage analysis — после 2-4 недель

## Известные ограничения

- **Bootstrap CI требует `roi_per_row`**. Без него — gate 5 пропускается. Это
  допустимо для validation perfomance модели per se, но не для promote-decision.
- **Per-band AUC игнорирует bands с n < 100** (`MIN_BAND_SIZE`). Иначе единичные
  outliers создают false-fail. Для small datasets — снизить через kwarg.
- **Bins fixed**. Future: учитывать data-driven bin edges (quantile-based) для
  uneven distributions.

## Future extensions (не в scope F)

- **Purged k-fold cross-validation** (Lopez de Prado AFML Ch.7) — для time-series
  ML с overlapping labels. Текущий harness predполагает out-of-sample holdout.
- **Combinatorial Purged CV (CPCV)** — для L2 LightGBM stacker.
- **Deflated Sharpe** (Bailey & Lopez de Prado 2014) — для адресовать multiple
  hypothesis testing когда мы тестируем 6 моделей (M1-M6).

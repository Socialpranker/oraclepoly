---
detector: D42_event_driven_ml
verdict: SHADOW (fit pending — ml_features.parquet available, models не trained ещё)
backtest_date: null
canonical_source: /mnt/data/canonical/ml_features.parquet (55 features, 5 anchors)
data_source: LGBM Regressor predict delta_price = yes_price(target) - yes_price(anchor)
n_signals: null
backtest_days: null
roi_net_pct: null
wr_pct: null
daily_realistic_usd_at_5k: null
daily_realistic_usd_at_1k: null
honest_haircut_factor: 1.0
honest_daily_usd_at_5k: null
honest_daily_usd_at_1k: null
honest_haircut_reason: null
trades_per_day: null
adversarial_mirror_pass: null
adversarial_mirror_note: null
post_v2_split: null
---

# D42 Event-driven ML — Price Change Predictor

**Hypothesis (D3 из `prediction_markets_full_landscape.docx`):**
"Самый прибыльный тип ML на prediction markets сейчас."

Не предсказываем outcome (yes_won), а **изменение цены** за N часов:
- Target: `delta_price = yes_price(anchor_to) − yes_price(anchor_from)`
- Model: LightGBM Regressor

**Verdict:** ⚠ SHADOW — models не trained ещё, ждут fit на VPS.

## Архитектура

Per-window models (anchor_from → anchor_to):

| Window | Forward | Use case |
|---|---|---|
| 168_to_24 | 144h | Long-horizon trend signal |
| 72_to_24  | 48h  | Mid-horizon |
| 24_to_4   | 20h  | Short-horizon execution |

Train на `ml_features.parquet` (55 cols leak-free as_of anchor):
- yes_price_at_anchor, drift_72h, drift_7d
- volume_24h_pct, vol_72h_pct, realized_vol_24h/72h
- vpin_proxy, kyle_lambda, liquidity_quality
- whale_taker_volume_24h, retail_taker_avg_size_24h
- event_yes_sum, event_arb_distance_yes
- ~40 more

## Signal emission

```
if predicted_delta > +5pp:
    Signal(YES, ride up)
elif predicted_delta < -5pp:
    Signal(NO, ride down)
```

Filters:
- Mid-zone yes [0.15, 0.85] (extremes — exhaustion zone)
- |predicted_delta| ≥ 5pp (sub-threshold = no signal)

## Training procedure

Walk-forward split: first 80% by `_resolve_dt` = train, last 20% = holdout.

Metrics tracked:
- RMSE train + holdout
- Sign accuracy holdout (predicted direction vs actual)
- Sign accuracy на extreme predictions (|pred| ≥ 5pp)

## Risk

1. **Direction noise** — short horizons (24→4h, 20h) могут быть noise dominated.
   Mitigation: extreme predictions filter, calibrator weights.
2. **Distribution shift** — markets evolve, model trained на historical data.
   Mitigation: monthly re-fit (cron TBD).
3. **Sign accuracy != edge** — predicting direction != predicting magnitude.
   Mitigation: магнитуда delta используется как confidence proxy.

## Promotion criteria

- **Fit → Shadow** (после первого successful fit):
  - sign_accuracy_big_holdout ≥ 0.55 (better than random)
  - n_big_signals_holdout ≥ 100
  - RMSE_holdout / RMSE_train ratio < 1.5 (не overfitted)

- **Shadow → Virtual** (после ≥4 weeks shadow):
  - n_signals_live ≥ 50
  - Avg realized_roi_net ≥ +3%
  - Per-window breakdown показывает edge (особенно 24→4h для execution)

## Implementation

- Detector: [app/detectors/d42_event_driven_ml.py](../../app/detectors/d42_event_driven_ml.py)
- Fit script: [scripts/research/d42_event_driven_ml_fit.py](../../scripts/research/d42_event_driven_ml_fit.py)
- Models output: `/mnt/data/canonical/models/d42_event_driven_{window}_{timestamp}.pkl`
- Migration: `migrations/20260514153830_register_d42_event_driven_ml.sql`
- Tests: `tests/test_d42_event_driven_ml.py` (10 tests)

## References

- Source hypothesis: D3 event-driven ML, `prediction_markets_full_landscape.docx`
- Existing ML pipeline: standalone_v2_predictor.py (outcome prediction, different target)
- Feature kernel: `app/ml/feature_kernel.py` (canonical 55 features, leak-free)

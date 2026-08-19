---
detector: D41_momentum
verdict: SHADOW (backfill pending, no canonical numbers yet)
backtest_date: null
canonical_source: null
data_source: HF canonical trade_buckets (vwap_yes + buy/sell_volume + n_distinct_takers) rolling 3h
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

# D41 Momentum — Trend Following Detector

**Hypothesis (E3 из `prediction_markets_full_landscape.docx`):** Когда yes_price двигается ≥5pp за rolling 3h окно с volume confirmation (buy-heavy flow, broad taker participation), trend продолжается ещё 1-2h.

**Verdict:** ⚠ SHADOW — backfill done, но canonical numbers pending validation. Это первый **trend-following** detector в codebase (vs все остальные = fade).

## Стратегия

```
momentum_3h_pp = current_price - vwap_3h(taker_trades)
buy_share      = buy_vol_3h / (buy_vol_3h + sell_vol_3h)

if momentum_3h_pp > +5pp AND buy_share >= 0.60:
    Signal(YES, ride up)
elif momentum_3h_pp < -5pp AND buy_share <= 0.40:
    Signal(NO, ride down)
```

**Side decision intuition:**
- UP momentum + buy-heavy flow → aggressive taker pressure, continuation likely
- DOWN momentum + sell-heavy flow → distribution, continuation likely
- Mismatch (e.g. UP price + sell flow) → likely noise, не trade

## Filters

- **Mid-zone YES** [0.20, 0.80] — extreme zones обычно near-resolution exhaustion
- **TTR** [4, 720]h — нужно время для continuation, но не too long для noise
- **3h volume** ≥ $500 — фильтр шума
- **3h n_distinct_takers** ≥ 5 — broad participation, не один whale
- **Side bias** ≥ 60% — buy ИЛИ sell flow должен доминировать
- **Excluded:** Sports (pre-game/news momentum часто exhausts быстро)

## Empirical baseline (pre-validation)

- **WR estimate:** 0.55 (conservative)
- **Expected continuation:** half of observed move (5pp move → 2.5pp дополнительный gain)
- **Horizon:** 4h

## Risk acknowledgements

1. **Trend exhaustion** — momentum может закончиться раньше continuation окна. Mitigation: tight 4h horizon, не hold to resolution.
2. **News-driven spikes** — Sports category excluded, но Politics/Crypto могут также иметь news-driven moves. Mitigation: 3h rolling window сглаживает single bar spikes.
3. **Reversal risk** — после крупного move часто reversal. Mitigation: side bias filter (если momentum но opposite flow → reject).
4. **First trend-following detector** — все наши existing detectors = fade. Этот может корреллировать negatively с D5/D32/D33 (favorite fade). Mitigation: portfolio-level guard в L2/Layer A.

## Promotion criteria

- **Shadow → Virtual** (после ≥4 weeks live + backfill validation):
  - n_signals_backfill ≥ 100
  - Empirical WR ≥ 0.50 (lower bound на 95% CI)
  - Avg realized_roi_net ≥ +3% (positive after fees + slippage)
  - Per-category breakdown — не сосредоточен в одной category

- **Virtual → Live** (после 14 дней virtual): требует Layer A integration + corr check с fade detectors.

## Implementation

- Detector: [app/detectors/d41_momentum.py](../../app/detectors/d41_momentum.py)
- Scan job: [app/jobs/d41_scan.py](../../app/jobs/d41_scan.py) (cron every 10 min)
- Backfill: [scripts/research/backfill_detector_signals_hf.py](../../scripts/research/backfill_detector_signals_hf.py) с `needs_momentum_3h=True`
- Migration: `migrations/20260514150720_register_d41_momentum.sql`
- Tests: `tests/test_d41_momentum.py` (16 tests)

## References

- Source hypothesis: E3 momentum class, `prediction_markets_full_landscape.docx`
- Related fade detectors: D5/D30/D32/D33 (correlation check на promotion)

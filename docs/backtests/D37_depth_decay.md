---
detector: D37_depth_decay
verdict: SHADOW (no canonical backtest possible)
backtest_date: null
canonical_source: null
data_source: live forward-collection only (book_metrics требует CLOB orderbook snapshot)
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

# D37 DepthDecay — Phase 1 Shadow Detector

**Hypothesis (H1 из `research/polymarket-alpha-hypotheses-2026-05/2026-05-12.md`):** Depth падает ~6% per 10× reduction в TTR (canonical slope 0.55). В zones со slope >0.7 — спред расширяется быстрее ожидания. Maker edge per filled order: 4-8¢ в mid-zone, до 1300-1800 bps на extremes.

**Verdict:** ⚠ NO CANONICAL BACKTEST — детектор требует **live `book_metrics`** (CLOB orderbook snapshot), который не хранится в HF canonical layer. Forward-collect only.

## Архитектура

```
clob-book-sync (hourly :15)        →  oracle_snapshots (orderbook depth)
d36-d37-scan (every 5 min)         →  book_metrics через compute_obi_metrics
d37-scan                           →  detector_signals (DETECTOR_NAME='D37_depth_decay')
```

## Strategy

**Mean-reversion на microstructure**, не price prediction:

- Берём YES если `best_ask` на 1-2¢ выше mid → expect mean reversion to mid
- Берём NO если `best_bid` на 1-2¢ ниже mid → expect rebound

Exit: hold-to-resolution или закрытие через `position_manager`.

## Filters

- **Mid-zone price** [0.30, 0.85] (user 2026-05-13 review)
- **TTR window** [0.5, 12]h — depth decay активен при коротком TTR
- **Spread expansion** [3¢, 10¢] — fires когда spread ≥ 2-3× median (1-2¢ для mid-zone), capped < 10¢ чтобы не trigger в "toxic withdrawal"
- **Min total depth** $3,000 USD — нужна минимальная depth чтобы filled

## Empirical baseline

- **WR estimate:** 0.55 (conservative)
- **Per-fill edge:** 4-8¢ on mid-zone @ entry 0.50 = ~10-15% ROI per trade
- **Horizon:** 12h (hold to resolution if needed)

Source [S1]: arXiv 2026 — empirical depth decay на Polymarket.

## Risk acknowledgements

1. **Adverse selection** — если кто-то sweep всю side, spread расширяется потому что **они знают что-то**. Mitigation: spread cap 10¢ (toxic withdrawal cutoff).
2. **Hold to resolution risk** — если signal LOSS, full payout loss (-100% gross). Sizing должен быть 1% bankroll per signal.
3. **Maker rebate not modeled** — Polymarket maker rewards могут добавить 20-50bps если активен; backfill assume taker fees.
4. **Book metrics live-only** — нет canonical backtest, validation через ≥4 weeks shadow + manual review.

## Promotion criteria

- **Shadow → Virtual** (после ≥4 weeks):
  - n_signals ≥ 30
  - Empirical WR ≥ 0.50 (lower bound на confidence interval)
  - Avg realized_roi_net ≥ +5% (positive after fees + slippage)
  - Per-zone breakdown показывает edge в mid-zone (не tails)

- **Virtual → Live** (после 14 дней virtual): требует book_metrics consistency check + maker rebate audit.

## Implementation

- Detector: [app/detectors/d37_depth_decay.py](../../app/detectors/d37_depth_decay.py)
- Book metrics helper: [app/detectors/d36_obi_pressure.py](../../app/detectors/d36_obi_pressure.py) (shared `compute_obi_metrics`)
- Scan job: вероятно совмещён с D36 scan (TBD: проверить cron entry)
- Migration: register в detector_state TBD

## References

- Source hypothesis: `research/polymarket-alpha-hypotheses-2026-05/2026-05-12.md` H1
- Related detector: D36_obi_pressure (orderbook imbalance, complementary signal)

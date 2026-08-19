---
detector: D10b_election_ladder
verdict: PASS
backtest_date: 2026-05-02
canonical_source: scripts/research/d10_canonical.py + /tmp/d10b_refined.py
data_source: /mnt/data/canonical/markets_clean.parquet + price_snapshots.parquet
n_signals: 4233
backtest_days: 821
roi_net_pct: 65.0
wr_pct: 37.4
daily_realistic_usd_at_5k: 27.03
daily_realistic_usd_at_1k: 5.41
honest_haircut_factor: 0.5
honest_daily_usd_at_5k: 13.51
honest_daily_usd_at_1k: 2.70
honest_haircut_reason: production scan не имеет vol filter — ~36% signals в markets vol_24h < $1K (thin book risk). Также filter mismatch как у D7b.
trades_per_day: 5.2
adversarial_mirror_pass: false
adversarial_mirror_note: NO-side dominates (60% PnL). YES side weaker (15-21% WR). Категория exclusions per side.
post_v2_split: pass
---

# D10b ElectionLadder — Canonical Backtest

**Hypothesis:** В non-NegRisk multi-market events (mutex 2-6 markets) sum(yes_price) ≠ 1.0 из-за first-mover liquidity bias. Когда sum > 1.0 — fade NO на market с максимальным yes_price (overpriced). Когда sum < 1.0 — fade YES на market с минимальным yes_price (underpriced).

**Verdict:** ✅ PASS shadow-ready

## Per-(side, anchor_band) WR (canonical 821 days, 4,233 events, dev>=3%)

| side | anchor_band | n     | WR     | avg_cost |
|------|-------------|-------|--------|----------|
| NO   | 4h          | 2008  | 35.5%  | 0.276    |
| NO   | 12h         | 412   | 48.8%  | 0.336    |
| NO   | 24h         | 240   | 40.4%  | 0.315    |
| NO   | 48h         | 130   | 25.4%  | 0.276    |
| YES  | 4h          | 855   | 20.0%  | 0.172    |
| YES  | 12h         | 273   | 19.8%  | 0.246    |
| YES  | 24h         | 215   | 20.9%  | 0.229    |
| YES  | 48h         | 100   | 15.0%  | 0.165    |

## Filters

- **Event size:** 2-6 markets (mutex)
- **Non-NegRisk only** (D3 cross_mkt_arb уже covers NegRisk)
- **Deviation:** |sum(yes_price) - 1.0| ≥ 0.03
- **Window:** 4-48h before resolve
- **Categories excluded NO:** Crypto, Tech & AI (WR <20%)
- **Categories allowed YES only:** Tech & AI, Geopolitics, Other

## Honest expectations vs canonical

⚠️ **Important:** D10b canonical $27/day @ $5K — но production scan **не имеет vol filter** (нет `lifetime_volume_usd` в postgres). Из 58 первых production signals (2026-05-02): 36% в markets с vol_24h < $1K — fill rate ужасный.

Honest haircut: × 0.5 → realistic **$13.5/day @ $5K**, **$2.7/day @ $1K**.

**TODO:** добавить `volume_24h >= $1K` filter в `app/jobs/d10b_scan.py` после 7d наблюдения за fill rate.

## Differentiation

- D7b: single market, [0.85, 0.95] zone — D10b разные markets per event
- D3: NegRisk events — D10b explicitly non-NegRisk
- D10b торгует overpriced/underpriced **direction** в multi-leg structures

## Implementation

- Code: `app/detectors/d10b_election_ladder.py`
- Scan job: `app/jobs/d10b_scan.py` (cron `:15` hourly)
- Migration: `migrations/20260502153221_register_d10b_detector.sql`
- Tests: `tests/test_d10b_election_ladder.py` (18) + `tests/test_d10b_scan.py` (8)

## Promotion criteria

- **Shadow → Virtual** (после 14d): n_signals ≥ 100, NO-side WR per bucket matches canonical ±10pp
- **Virtual → Live** (после 28d virtual): virtual_pnl > $0 net of fees, fill rate ≥ 30% после vol filter добавления

## References

- Memory: `memory/d10b_production_2026-05-02.md`
- Prior backtest: `scripts/research/d10_canonical.py`
- D-NegRisk variants (failed): `memory/detectors_stage1_v2_2026-05-02.md`

---
detector: D_esports_favourite_fade
verdict: PASS
backtest_date: 2026-05-04
canonical_source: scripts/research/d_esports_favourite_fade_canonical.py
data_source: /mnt/data/canonical/ml_features_v2.parquet
n_signals: 504
backtest_days: 821
roi_net_pct: 58.7
wr_pct: 70.2
daily_realistic_usd_at_5k: 9.0
daily_realistic_usd_at_1k: 1.8
honest_haircut_factor: 0.5
honest_daily_usd_at_5k: 4.5
honest_daily_usd_at_1k: 0.9
honest_haircut_reason: production filter mismatch (lifetime feature aggregated from Polymarket trades API vs canonical price_min_asof) + post-V2 unverified
trades_per_day: 0.61
adversarial_mirror_pass: true
mirror_diff_pp: 111.4
adversarial_mirror_note: Strong asymmetric edge — YES mirror -52.7% vs NO +58.7% confirms favourite-fade направленность
post_v2_split: 2025 +58% / 2026 +60% — sustained, year_consistency 0.97
discovered_via: Auto Edge Scanner top-1 finding (id=528, scan 2026-05-03) → manual review (memory/d_top1_drawdown_review_2026-05-04.md) → clean re-research replacing drawdown proxy with price_min_asof
---

# D_ESPORTS_FAVOURITE_FADE — Canonical Backtest

**Hypothesis:** В Other category esports markets (LoL/Dota/CS/Valorant/CoD/HoK),
который хотя бы раз торговался near-zero за свою lifetime (`price_min_asof < 0.15`),
сейчас в mid zone [0.50, 0.70) на anchor 4h: **NO-side (favourite wins) имеет sustained edge**.

Logic: esports markets имеют intra-game volatility (одна команда сильно проигрывает →
price collapses к 0 → recovers если матч best-of). К anchor=4h до end_date market
restored к favourite price. Crowd over-prices remaining vibration risk, но favourite
реально wins в 70% случаев.

**Verdict:** PASS

## Filters

- **Category:** Other (Polymarket categorization)
- **Question regex (case-insensitive):** `\b(lol|league of legends|honor of kings|dota|cs:?go|counter-strike|valorant|cod|call of duty|overwatch|esports|game [0-9]|map [0-9]|bo[35]|hok)\b`
- **Zone:** YES `current_price` ∈ [0.50, 0.70)
- **Anchor:** 4h ONLY (band: hours_to_resolve ∈ [4, 8])
- **Trigger:** `price_min_asof < 0.15` (lifetime YES price хотя бы раз касалась near-zero)
- **Side:** NO

## Numbers

- n = 504 signals over 821 days = 0.61/day
- WR (NO): 70.2%
- avg_cost: $0.4094
- net ROI: +58.7%
- Mirror (YES): WR 29.8%, net ROI -52.7%
- Mirror diff: +111.4pp
- Daily PnL @ $5K (raw): +$9.00
- Daily PnL honest (×0.5): +$4.50
- Yearly honest: +$1,643

### Sub-zones

| Zone | n | WR | net_ROI |
|---|---|---|---|
| [0.50, 0.55) | 132 | 74.2% | +42.7% |
| [0.55, 0.60) | 144 | 70.1% | +50.3% |
| [0.60, 0.65) | 111 | 63.1% | +52.8% |
| [0.65, 0.70) | 95 | 69.5% | +93.4% |

### Year split

| Year | n | WR | net_ROI |
|---|---|---|---|
| 2025 | 133 | 69.2% | +58.2% |
| 2026 | 364 | 71.2% | +60.1% |

Year consistency 0.97 (positive ratio min/max).

### Monthly 2026 (regime stability)

| Month | n | WR | net_ROI |
|---|---|---|---|
| Jan | 143 | 67.1% | +51.6% |
| Feb | 157 | 74.5% | +66.5% |
| Mar | 64 | 71.9% | +63.1% |

No decay observed (in contrast to drawdown variant which dropped Mar 2026 -20pp).

### Liquidity sensitivity

| volume_asof floor | n | WR | net_ROI |
|---|---|---|---|
| ≥$0 | 504 | 70.2% | +58.7% |
| ≥$1K | 405 | 69.4% | +56.8% |
| ≥$5K | 287 | 71.8% | +61.9% |
| ≥$25K | 141 | 70.2% | +59.4% |
| ≥$100K | 59 | 74.6% | +68.8% |

Edge **растёт** при росте liquidity — NOT thin-market artifact.

### Slippage sensitivity

| Slippage | net_ROI |
|---|---|
| 2pp (canonical) | +58.7% |
| 3pp | +55.1% |
| 4pp | +51.6% |
| 5pp | +48.3% |

ROI остаётся положительным даже на 5pp slippage.

### Game type breakdown (n≥30 only)

| Game | n | WR | net_ROI |
|---|---|---|---|
| Dota | 102 | 76.5% | +74.2% |
| LoL | 96 | 64.6% | +55.4% |
| CS | 230 | 68.3% | +51.8% |
| Valorant | 64 | 67.2% | +55.9% |
| game N / map N / bo[35] | 514 | 68.7% | +56.1% |

## Discovery story

Auto Edge Scanner finding `max_drawdown_24h_pct > 0.9 mid Other 4h NO` (id=528,
n=652, ROI +59%, q=2.7e-41) был manually reviewed:

1. Data layer PIT-clean (no leakage, see `scripts/research/build_canonical_hf.py:227-228`).
2. Vol-floor robust (vol≥$25K WR 74%, vol≥$100K ROI +70%) — NOT thin-market artifact.
3. **82% markets имели pmin_24h<0.05** — drawdown was proxy для live esports volatility.
4. **58% signals (n=394/676) drove by esports keywords** with WR 78%, ROI +78%.

Replacing drawdown с esports keyword alone: edge collapses to ROI **-3%**.
Drawdown carried causal selection (high-vol intra-day) PLUS market-type proxy.

Sweep alternatives:
- `realized_vol_24h` weak substitute (rv>0.20 → n=110, too small)
- `price_range_asof > 0.5` PASS partial (n=803 ROI +15%, mdiff +37pp <50)
- **`price_min_asof < 0.15` clean PASS** (lifetime feature, easy to compute live)

## Production architecture

Detector reads `price_min_asof` per market via Polymarket data-api `/trades?market=<cid>`
endpoint (same pattern как D13/D14 для wallet quality). Lifetime min YES price = `min(trade.price)` across all returned trades (limit=5000 covers vast majority of esports markets).

**Pre-filter SQL:** `markets.question` ILIKE patterns reduce SQL output к ~10-50 markets/scan
before Polymarket API call. Scan job runs hourly.

**Per-event dedup:** outcomes коррелированы (best-of матчи имеют одну единственную outcome).
Best signal per event_id selected by rank = confidence × (1/current_price).

## Vs existing detectors

D_ESPORTS_FAVOURITE_FADE non-overlapping (Auto Edge Scanner overlap_pct = 0% по всем D3/D5/D7b/D10b/D13/D14):

- D7b: extreme zone fade [0.85, 0.97] (this is mid)
- D14: Crypto mid zone (this is Other esports)
- D13: extreme retail-only [0.85, 0.95]
- D10b: multi-leg arb (different signal type)

## Risks

- **Esports-specific seasonal:** major tournaments concentrate signals в Sep-Apr
- **Question regex maintenance:** новые игры (Marvel Rivals, etc.) — periodic regex audit
- **Lifetime feature production-side compute:** detector агрегирует min(price) over Polymarket
  trades API. На markets с >5000 trades (rare для esports) min может быть incomplete →
  honest haircut 0.5 учитывает this risk
- **Capacity small:** ~0.6 signals/day, $4.50/day honest — incremental
- **Esports liquidity grows:** в 2026+ esports markets могут стать deeper → edge может decay
- **March 2026 watch:** all monthly 2026 stable +50-67%, но small n (Mar n=64)

Generated by `scripts/research/d_esports_favourite_fade_canonical.py` 2026-05-04.

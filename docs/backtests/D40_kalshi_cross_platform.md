---
detector: D40_kalshi_cross_platform
verdict: SHADOW (no canonical backtest possible)
backtest_date: null
canonical_source: null
data_source: live forward-collection only (Kalshi не публикует историю)
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

# D40 KalshiCrossPlatform — Phase 1 Shadow Detector

**Hypothesis (A1 из `prediction_markets_full_landscape.docx`):** Цены на одно и то же событие на Polymarket vs Kalshi систематически расходятся (разные user bases, regulatory framework, информационные лаги). Two-leg arb: buy cheap YES + buy cheap NO = guaranteed profit при successful close.

**Verdict:** ⚠ NO CANONICAL BACKTEST — Kalshi не предоставляет публично исторические orderbook данные. Forward-collect only.

## Архитектура

```
kalshi-market-sync (30 min)        →  kalshi_markets
kalshi-polymarket-match (4h)       →  market_pair_matches (embeddings cosine ≥ 0.85)
kalshi-orderbook-sync (5 min)      →  kalshi_orderbook_snapshots (top of book для tracked pairs)
d40-scan (5 min)                   →  detector_signals (DETECTOR_NAME='D40_kalshi_cross_platform')
```

## Strategy

Two-leg arb on YES side:
- **Direction 1** (`poly_yes`): buy YES Polymarket (cheap) + buy NO Kalshi
- **Direction 2** (`kalshi_yes`): buy YES Kalshi (cheap) + buy NO Polymarket

Cost = `cheap_price × (1 + cheap_fee) + expensive_price × (1 + expensive_fee)`.

Net edge = `1.0 - total_cost`. Emit signal если `edge_pct ≥ MIN_NET_SPREAD_PCT` (default 3% из PRD decisions).

### Fee models

- **Polymarket:** `app/services/fee_calculator.py:estimate_trading_fee(amount, price, category)` — per-category dynamic.
- **Kalshi:** `7¢ × C × (1 - C)` per contract. Peak 1.75% at p=0.5, near zero в extreme zones.

## Filters

- **Phase 1 cohort:** Sports + Politics + Economics (mapped из Polymarket "World & Economy" → "Economics")
- **Min volume per side:** $5,000 USD
- **Auto-match threshold:** cosine similarity ≥ 0.85 (flagged 0.70-0.85 для manual TG review)
- **Inverse pairs (Phase 1):** SKIP — phase 2 поменяет ноги
- **Min net edge:** 3% (после fees, без slippage)

## Risk acknowledgements

1. **Auto-match false positives** (matcher pairs неправильные markets) → выбор ложного arb → loss. Mitigation: threshold 0.85, manual TG review для 0.70-0.85 зоны.
2. **Inverse markets** mismatched как direct → backwards arb. Mitigation: эвристика `sum(yes_a, yes_b) ≈ 1.0` detection.
3. **Slippage не учтён** в Phase 1 — на $100 stake на Kalshi book depth $500-5000 это 1-3pp. **Recommend только при net edge ≥ 5% если operator делает manual close-out.**
4. **Different resolution rules** — Polymarket UMA vs Kalshi CFTC. Phase 1 cohort выбран чтобы минимизировать risk divergence (Sports/Politics/Economics обычно резолвятся одинаково).
5. **Latency manual operator** — рынок может закрыть spread за минуты. PRD threshold 3% это компенсирует.

## Promotion criteria

- **Shadow → Virtual** (после ≥4 недель):
  - n_signals ≥ 50
  - ≥5 manual close-outs выполнены (operator placed обе ноги) с realized net profit > $0 в среднем
  - Auto-match FP rate < 10% (на manual TG review)
  - Per-category spread distribution соответствует ожиданиям (Sports ~1-3%, Politics ~2-4%)

- **Virtual → Live** (после 14 дней virtual): требует Phase 2 auto-execution с atomic two-leg placement (asyncio.gather).

## Implementation

- Detector: [app/detectors/d40_kalshi_cross_platform.py](../../app/detectors/d40_kalshi_cross_platform.py)
- Scan job: [app/jobs/d40_kalshi_cross_platform_scan.py](../../app/jobs/d40_kalshi_cross_platform_scan.py)
- Matcher: [app/matchers/kalshi_polymarket_matcher.py](../../app/matchers/kalshi_polymarket_matcher.py)
- Kalshi client: [app/services/kalshi_service.py](../../app/services/kalshi_service.py)
- Migration: `migrations/20260514130000_register_d40_kalshi_cross_platform.sql`
- Tests: `tests/test_d40_kalshi_cross_platform.py`, `tests/test_kalshi_polymarket_matcher.py`, `tests/test_kalshi_service.py`

## References

- PRD: [research/kalshi-integration-2026-05/prd.md](../../research/kalshi-integration-2026-05/prd.md)
- Source hypothesis: `prediction_markets_full_landscape.docx` (A1)
- Detailed plan: `arb_bot_scheme.docx`

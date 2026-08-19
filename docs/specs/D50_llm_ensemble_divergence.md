# D50 LLM Ensemble Divergence — Spec

**Status:** Spec (pre-backtest)
**Class:** Behavioral
**Owner:** owner
**Source:** [research/edge-synthesis-2026-05-24](../../research/edge-synthesis-2026-05-24/README.md) N7, [prediction-market-edge-sources §7]
**ADR:** [0001 Dual Model Paradigm](../adr/0001_dual_model_paradigm.md)

## Hypothesis

**Aggregated estimate from 3+ LLMs ≈ informed crowd belief**, better than Polymarket retail crowd на subset рынков.

Каждая LLM training data ≠ Polymarket retail crowd. Если 3 LLM (Claude, GPT-4o, Gemini) **независимо** дают similar probability estimate, и тот estimate **значительно расходится** с Polymarket price → likely Polymarket retail mispricing.

**Mental model:**
```
P_LLM_consensus = median(P_claude, P_gpt4o, P_gemini)
P_LLM_uncertainty = std(P_claude, P_gpt4o, P_gemini)

if P_LLM_uncertainty < 0.05 and |P_LLM_consensus - market_price| > 0.10:
    # LLMs agree, Polymarket disagrees → fade Polymarket
    edge = P_LLM_consensus - market_price
```

**Important caveat:** LLM training cutoff means it can't reason про события после cutoff. We need to **scope** to evergreen questions / well-documented events.

## Mental model — why this might work

1. **LLMs trained on broad web** — they reflect "expert consensus" view encoded in articles, papers, Wikipedia
2. **Polymarket retail crowd** — drawn to viral / emotional / hyped events, often disconnected from base rates
3. **3-LLM ensemble** filters out individual model biases (Claude tends optimistic on certain topics, GPT on others)
4. **High uncertainty** (`std > 0.10`) = LLMs themselves disagree → no signal
5. **Low uncertainty AND divergence from market** = signal

## Signal definition

**Entry trigger:**
1. Market category in whitelist: Politics, World&Economy, Tech&AI, Culture (NOT Sports — too dependent on specific game info; NOT Crypto strike — pure forecasting territory)
2. Market created ≥ 7 days ago (mature crowd consensus)
3. `volume_24h ≥ $2,000` (crowd has converged)
4. `dte_days ∈ [7, 180]` (not too imminent, not too distant)
5. LLM ensemble emits:
   - `P_LLM_consensus = median(P_claude, P_gpt4o, P_gemini)`
   - `P_LLM_uncertainty = std(...)`
6. Filters:
   - `P_LLM_uncertainty < 0.07` (models agree)
   - `|P_LLM_consensus - current_price| ≥ 0.10` (significant gap)
   - Side toward LLM: YES if `P_LLM_consensus > current_price`, else NO

## Cost / rate-limit considerations

3 LLM calls per market = expensive. **Don't scan all markets every hour.**

**Smart scan strategy:**
- Initial scan: all eligible markets once при first appearance в БД
- Re-evaluation: on `bookmaker_probability` change of ≥3pp OR on news event tagged to market
- Cache LLM responses for 24h (markets don't move that fast in this signal class)

**Cost estimate:**
- ~500 eligible markets active at any time
- Re-evaluation every 24h = 500 × 3 LLMs = 1500 calls/day
- At average ~$0.01/call (mix of Haiku/GPT-4o-mini/Gemini Flash): **$15/day max**, but with caching maybe $5/day

Budget verification before scaling.

## Confidence calculation

```python
divergence = abs(P_LLM_consensus - current_price)
agreement = 1 - P_LLM_uncertainty  # closer to 1 = better

base_conf = 0.50 + (divergence - 0.10) * (0.30 / 0.20)  # 0.50 → 0.80 as div 0.10 → 0.30
confidence = base_conf * agreement
confidence = min(confidence, 0.85)
```

## Backtest parameters

**Challenge:** LLMs have training cutoff, so we **can't simply replay** old markets and ask LLMs (they would already know outcome).

**Workaround:**
1. **Use LLM knowledge cutoff dates** — for Claude Sonnet 4.5 (cutoff Jan 2026), only test on markets created before Jan 2026 AND resolved before Aug 2026 (so LLM doesn't know outcome but knows context). For older markets, use older models with earlier cutoffs.

2. **Or, simpler:** start with **forward-looking shadow** only. Skip backtest, go straight to shadow with 14d window. Take longer to validate but no leakage.

**Decision:** start with **forward-looking shadow** (no backtest). Spec includes 30-day shadow window with strict promotion criteria.

**Pass criteria (shadow):**
- `n_signals ≥ 50` (30 days, ~1.5 signals/day)
- `wilson_lower_95(wr) > 0.55`
- Holds across at least 2 of {Politics, World&Economy, Tech&AI, Culture}

## Implementation plan

### Phase 0 — Cost validation (0.3 day)

Sample 50 markets, run 3-LLM ensemble. Measure cost per call. Decide if production scan economically viable.

### Phase 1 — LLM ensemble service (1.5 day)

`app/services/llm_ensemble_service.py` — async calls to 3 LLMs with structured output (JSON probability + reasoning).

Prompt template (TBD):
```
Question: {question}
Context: {category}, resolution_date={end_date}
Your task: estimate P(YES_resolves) as decimal 0.0-1.0.
Output JSON: {"probability": float, "reasoning": str, "confidence": str}
```

### Phase 2 — Detector + scan (1 day)

`app/detectors/d50_llm_ensemble.py` + `app/jobs/d50_scan.py` — cron daily, with cache check.

### Phase 3 — Migration (0.1 day)

Register `D50_llm_ensemble` in detector_state with `stage='shadow'`.

Add table for LLM responses cache:
```sql
CREATE TABLE llm_ensemble_cache (
    market_id TEXT NOT NULL,
    model TEXT NOT NULL,  -- 'claude', 'gpt4o', 'gemini'
    prob NUMERIC(5,4) NOT NULL,
    reasoning TEXT,
    fetched_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (market_id, model, fetched_at)
);
CREATE INDEX ON llm_ensemble_cache (market_id, fetched_at DESC);
```

### Phase 4 — Cron registration (0.1 day)

`scripts/cron_runner.py` + `crontab.conf` — `d50-scan` daily.

### Phase 5 — Shadow (30 days, not 14)

Longer because no backtest baseline. Need direct evidence of signal quality.

### Phase 6 — Virtual (14-28 days)

Same as standard.

## Risks

1. **LLM cost runaway:** if signal frequency higher than estimated, cost can spike. Mitigation: hard cap $X/day in scan job, alert if exceeded.

2. **LLMs converge to web consensus** which already reflects polls → no edge vs Polymarket which already prices polls. Mitigation: focus on markets where LLM **disagrees** with surface-level consensus (which Polymarket reflects).

3. **Training cutoff leak:** if we accidentally test post-cutoff outcomes, LLM "knows" answer. Mitigation: skip backtest, forward-looking only.

4. **LLM jailbreak / structured output failure:** model returns invalid JSON. Mitigation: retry with stricter prompt, fallback to "no signal" if 3 retries fail.

5. **Provider downtime:** one of 3 LLMs unavailable. Mitigation: signal valid only if all 3 responded.

## Sources

- [prediction-market-edge-sources §7] — LLM sentiment captures 45-50% return variation
- [Yahoo Finance, AlphaScope] — AI ensemble bots earning $2.2M / 2 months
- [research/edge-synthesis-2026-05-24 N7]

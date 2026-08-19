# D49 Vegas vs Polymarket Sports — Spec

**Status:** Spec (pre-backtest)
**Class:** Truth-Path
**Owner:** owner
**Source:** [research/edge-synthesis-2026-05-24](../../research/edge-synthesis-2026-05-24/README.md) N3, [prediction-market-edge-sources §5 + §4]
**ADR:** [0001 Dual Model Paradigm](../adr/0001_dual_model_paradigm.md)

## Hypothesis

**Vegas/bookmaker odds — structurally more accurate oracle для Sports**, чем Polymarket. Reasons:

1. Bookmakers оперируют **decades of data** на каждом team/league
2. Lines balanced by professional money flow (Pinnacle, Bookmaker.eu)
3. Bookmakers absorb large bets and balance — Polymarket retail-driven
4. На games-of-the-week / playoffs Polymarket retail gravitates к favorites сильнее чем Vegas

**Truth-Path claim:** для Sports event Vegas implied probability = better proxy for `P_resolution` чем Polymarket current_price.

```
P_resolution_sports ≈ Vegas_implied_prob (after vig removal)
edge = P_resolution - Polymarket_price
```

## Data we already have

`markets.bookmaker_probability` — поле в Postgres schema (см. `\d markets`). Уже заполняется через какой-то pipeline. Проверим **что именно** туда пишется.

```sql
SELECT category, COUNT(*), COUNT(bookmaker_probability) as with_book
FROM markets
WHERE is_active=true GROUP BY category;
```

If `bookmaker_probability` уже накоплено — отличный starting point.

## Signal definition

**Entry trigger:**
1. `category = 'Sports'`
2. `bookmaker_probability IS NOT NULL` AND `bookmaker_updated_at > now() - INTERVAL '6h'` (fresh line)
3. `|bookmaker_probability - current_price| ≥ 0.05` (5pp divergence threshold)
4. `volume_24h ≥ $1,000` (liquidity floor)
5. `dte ∈ [1h, 72h]` (sports resolve fast, Vegas lines most accurate close to game)
6. Side = direction toward Vegas:
   - If `bookmaker_probability > current_price`: side = YES (Polymarket undervalues favorite)
   - If `bookmaker_probability < current_price`: side = NO (Polymarket overvalues favorite)

**Subtype filter (important):**
- **Includes:** moneyline, spread, total points (binary or near-binary)
- **Excludes:** "Will Kyrie play 35+ minutes" (prop bets — Vegas not better oracle for props)

Need classifier — initial heuristic: question contains team names AND "win" / "beat" / "vs". More precise heuristic TBD.

## Confidence calculation

```python
divergence = abs(bookmaker_prob - current_price)
# Linear scale 0.05 → 0.20 divergence maps to confidence 0.55 → 0.90
confidence = min(0.55 + (divergence - 0.05) * (0.35 / 0.15), 0.90)

# Reduced if:
#   - Polymarket volume_24h > $50K (informed money also flowing) → 0.8x
#   - Time to game < 30 minutes (lines moving fast, lag) → 0.7x
```

## Backtest parameters

**Source:** `/mnt/data/canonical/markets_clean.parquet` + `price_snapshots.parquet` + ?? bookmaker history

**Critical question:** does canonical have bookmaker_probability snapshots over time, or только current state? Need to check.

If only current state — we backtest on **recent** resolved Sports markets (last 6 months where we have bookmaker_probability snapshots).

**Method:**
- Find resolved Sports markets where divergence threshold met at some snapshot
- Track entry → resolution outcome
- Compute Wilson lower CI on win rate

**Pass criteria:**
- `n_signals ≥ 200` (target 500+ for power)
- `wilson_lower_95(wr) > 0.55`
- Mean net PnL per trade > 1% post-fees
- Holds across NBA, NFL, MLB, NHL (no single-league overfitting)

## Implementation plan

### Phase 0 — Sanity check (0.2 day)

```bash
psql … -c "SELECT category, COUNT(*), AVG(bookmaker_probability IS NOT NULL::int) ...
FROM markets WHERE is_active=true OR resolved=true GROUP BY category;"
```

Verify bookmaker_probability coverage. If <50% of Sports — D49 has weak base.

### Phase 1 — Backtest script (1 day)

`scripts/research/d49_backtest.py` — using canonical + bookmaker_history table (если есть). Output `docs/backtests/D49_vegas_vs_polymarket_sports.md`.

### Phase 2 — Detector + scan (1 day)

`app/detectors/d49_vegas_vs_polymarket.py` + `app/jobs/d49_scan.py` — cron every 15 min during sports hours.

### Phase 3 — Migration (0.1 day)

Register in `detector_state` with `stage='shadow'`.

### Phase 4 — Shadow (7-14d)

Watch live signals. Vegas lines move; we want to confirm divergence is exploitable, not noise.

### Phase 5 — Virtual (14-28d)

Manual TG promote. Track fill rates — sports markets sometimes illiquid.

## Risks

1. **Bookmaker_probability staleness:** if pipeline only fetches Vegas line once a day, we'd be matching against stale data → false signals. Mitigation: hard filter `bookmaker_updated_at > now() - 6h`.

2. **Polymarket might be RIGHT sometimes:** insider info, weather, injury news. Vegas-Polymarket discrepancy might mean Polymarket has fresher info. Mitigation: cap confidence on high-volume Polymarket markets.

3. **Vegas line shopping:** different books quote different lines. `bookmaker_probability` aggregate may smooth over real edge points. Mitigation: cross-check with Pinnacle line directly (gold standard) если возможно в Phase 2+.

4. **Prop bet exclusion:** wrong classifier пропускает props → noise in signals. Mitigation: start with strict whitelist `(NBA, NFL, MLB) AND question contains "beat|defeat|win"`.

## Sources

- [research/edge-synthesis-2026-05-24](../../research/edge-synthesis-2026-05-24/README.md) N3
- [research/polymarket-orthogonal-strategies §3 (sentiment) + §4 (calibration edge)]
- ICE Polymarket Signals (BusinessWire 2026-02-11) — institutional Sports data flow

---
detector: D38_resolution_lag
verdict: SHADOW (no canonical backtest)
backtest_date: null
canonical_source: null
data_source: prod snapshot 2026-05-14 (oraclepoly-postgres on VPS)
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

# D38 ResolutionLag — Shadow Detector (No Canonical Backtest)

**Hypothesis (A3 из `prediction_markets_full_landscape.docx`):** Маркет, у которого `end_date` уже прошёл, но `markets.resolved=false` (наш cron `resolve_outcomes` работает раз в 4ч), часто имеет цену уже двинутую к одной из near-fait зон. Покупка в эту сторону до момента `resolved=true` даёт +5..15% gross за окно часов-сутки.

**Verdict:** ⚠️ NO CANONICAL BACKTEST — direct shadow deploy.

## Почему нет canonical backtest

HF `quant.parquet` (2022-11→2026-05) содержит trades и финальные `winner`, но **не содержит timestamp когда наш собственный `resolved=true` был выставлен**. Гипотеза A3 опирается именно на лаг между фактической резолюцией Polymarket Gamma и нашим cron — это **продукт нашей инфраструктуры**, его нельзя восстановить ретроспективно.

Альтернативные proxy backtest'ы (через "последние 24ч до конца торгов") меряют не A3, а late-stage fade — это уже делает D5. Поэтому D38 идёт сразу в shadow.

## Prod snapshot (2026-05-14, sanity check)

```sql
-- end_date < now() AND end_date > now() - 24h, is_active = true
active_pending  : 317 markets
edge_zone        :  25  (price ∈ [0.05-0.20] ∪ [0.80-0.95])
  yes_zone       :  10  ([0.85-0.95))
  no_zone        :  13  ([0.05-0.15))

stuck_total (lag > 24h) : 6588 markets  ← НЕ торгуем
```

**Ожидаемая частота:** ~30-50 сигналов/неделю (low-rate, high-precision).

## Filters

- **End date:** `end_date BETWEEN now() - 24h AND now()`
  - lag > 24h → stuck market (void/dispute), деньги залочиваются на недели
- **Zone:**
  - YES near-fait: `current_price ∈ [0.80, 0.95)` → buy YES
  - NO near-fait: `current_price ∈ [0.05, 0.20)` → buy NO
- **Volume:** `volume_24h >= $500` (для exit ликвидности)
- **Active:** `is_active = true AND resolved = false`

## Risk acknowledgements

1. **UMA dispute window не учитывается.** Polymarket резолвит через UMA optimistic oracle с 2h dispute period. В коде нет полей `uma_dispute_start`, `dispute_period_end`. Если оракул резолвнул "yes" но в течение 2ч кто-то disputed — outcome может flip → loss. **Mitigation:** confidence=0.95 (не 0.99) — закладывает 5pp на dispute/void/data lag, per-market 24h dedup, volume gate $500.
2. **Void markets.** Часть `stuck_total=6588` — это маркеты которые Polymarket резолвит как `void` (например, спортивные cancel'ы). Они не имеют winner и наш фильтр lag≤24h должен их исключить, но edge-case'ы возможны.
3. **current_price perversion.** После резолюции наш `market_sync.py` перезаписывает `current_price` в 0 или 1. Поэтому фильтр работает **только** на pre-resolved markets. После `resolved=true` мы уже не входим.

## Promotion criteria

- **Shadow → Virtual** (после 14 дней): n_signals ≥ 30, **WR ≥ 80%** в обеих зонах (yes_zone и no_zone), no_signal failure mode из void/dispute > 5%.
- **Virtual → Live** (после 14 дней virtual): virtual_pnl > $0 net of 2% fees + slippage, fill rate ≥ 60% на eligible signals.

## Implementation

- Detector: [app/detectors/d38_resolution_lag.py](../../app/detectors/d38_resolution_lag.py)
- Scan job: [app/jobs/d38_scan.py](../../app/jobs/d38_scan.py) (cron `*/5 * * * *`)
- Cron registry: [scripts/cron_runner.py](../../scripts/cron_runner.py) (`d38-scan`)
- Tests: [tests/test_d38_resolution_lag.py](../../tests/test_d38_resolution_lag.py)

## References

- Hypothesis source: `prediction_markets_full_landscape.docx` (A3 Resolution arb)
- Sanity check session: 2026-05-14

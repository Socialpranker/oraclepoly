---
detector: STANDALONE_V2_LGB
verdict: PASS
backtest_date: 2026-05-02
canonical_source: scripts/research/train_standalone_lgb_v2.py + standalone_v2_dry_runner.py
data_source: /mnt/data/canonical/ml_features.parquet (55 cols, 613K rows)
n_signals: 5316
backtest_days: 90
roi_net_pct: 31.0
wr_pct: 38.0
daily_realistic_usd_at_5k: 91.45
daily_realistic_usd_at_1k: 18.29
honest_haircut_factor: 0.35
honest_daily_usd_at_5k: 32.01
honest_daily_usd_at_1k: 6.40
honest_haircut_reason: production должен фетчить ml_features V2 LIVE per active market (snapshot price + trade flow + wallet flow as-of now). Ещё не реализовано — текущий cron только реплицирует backtest. Haircut 0.35 учитывает (а) feature staleness (snapshot per 1h vs anchor exact), (б) selection bias canonical markets vs live universe, (в) execution slippage 1% + fees 2%
trades_per_day: 59.1
adversarial_mirror_pass: true
adversarial_mirror_note: модель учится на бинарном yes_won без знания side — symmetric YES/NO выбор по EV (3264 YES + 4776 NO в EV>5% sample, оба прибыльны)
post_v2_split: pass (canonical 2024-2026 включает post-V2 era через 2026 Q1 markets)
---

# STANDALONE_V2_LGB — Canonical Backtest

**Hypothesis:** LightGBM на 55 leak-free per-anchor ml_features (price/volume/flow + wallet/microstructure/event) выдаёт калиброванный P(YES win) для ВСЕХ active markets, не только когда сработали детекторы D1-D6. Edge приходит от systematic favorite-longshot bias Polymarket в зоне 50-95% (4-5pp overprice фаворитов) + микроструктурный signal (whale activity, VPIN, кисель/ликвидность).

**Архитектура отличается от D1-D6:**
- D1-D6: pattern-match детекторы → emit signal → ML calibrate → EV size
- STANDALONE_V2_LGB: LGB сам — primary decision, читает per-(market, anchor) features

**Verdict:** ✅ PASS shadow-ready (НО см. honest_haircut_reason про live integration)

## Per-anchor metrics (out-of-time test 2025-Q4 + 2026-Q1)

| anchor | n_holdout | per-row Brier | per-event Brier | AUC weighted |
|--------|-----------|---------------|------------------|--------------|
| 4h | 50,800 | 0.164 | 0.143 | 0.808 |
| **24h** | **37,434** | **0.134** | **0.102** | **0.884** ★ production target |
| **72h** | **40,234** | **0.092** | **0.060** | **0.946** ★ best signal |
| 168h | 18,887 | 0.140 | 0.102 | 0.879 |
| 720h | 5,320 | 0.052 | 0.029 | 0.982 (n мало, overfit risk) |

Сравнение с baseline (honest_baseline `_073249`, per-event 0.207):
- anchor=24h V2: 0.102 → **2.0× лучше**
- anchor=72h V2: 0.060 → **3.5× лучше**

Сравнение с active L2 monolith `lgb_l2_v20260502_053135` (per-row 0.307 на detector_signals):
- anchor=24h V2 per-row 0.134 → **2.3× лучше** (но разные training distributions, не строго apples-to-apples)

## Dry trades simulation (anchor=24h, EV>10% gate)

Симуляция на 5,316 РЕЗОЛВНУТЫХ dry trades за last 90 days (из 23,226 placed, 18K ещё не резолвнулись):

| Метрика | Значение |
|---------|----------|
| n_trades placed | 23,226 |
| n_resolved | 5,316 |
| n_wins | 2,019 |
| WR | 38.0% |
| PnL net | **+$8,230** |
| Stake total | $26,580 |
| **ROI net** | **+31.0%** |
| YES side | 3,264 trades |
| NO side | 4,776 trades |

Stake $5/trade, slippage 1%, fees 2%.

## Per-EV-threshold breakdown (anchor=24h, sweet spot finding)

| EV порог | n_trades | WR | PnL | ROI | n_events |
|----------|----------|-----|-----|-----|----------|
| EV>5% | 8,040 | 43.4% | +$8,968 | +22.3% | 1,481 |
| **EV>10%** ★ chosen | **4,974** | **36.5%** | **+$9,022** | **+36.3%** | **1,085** |
| EV>20% | 2,427 | 28.3% | +$6,924 | +57.1% | 614 |
| EV>30% | 1,455 | 27.2% | +$6,530 | +89.8% | 388 |

EV>10% оптимум: высокая n + хороший ROI + покрытие 1085 events за 90d.

## Per-category breakdown (anchor=24h, 5316 resolved)

| Category | trades | WR | PnL net | ROI |
|----------|--------|-----|---------|-----|
| Other | 13,260 | 35.8% | +$3,693 | +25.9% |
| Geopolitics | 492 | 44.9% | +$960 | **+130.6%** |
| Weather | 2,256 | 40.1% | +$948 | +20.8% |
| Politics | 768 | 42.8% | +$817 | +41.6% |
| Crypto | 2,955 | 41.9% | +$715 | +25.9% |
| Tech & AI | 1,410 | 30.0% | +$477 | +53.0% |
| Finance | 486 | 48.0% | +$343 | **+91.4%** |
| Sports | 873 | 33.3% | +$179 | +26.6% |
| Culture | 462 | 39.0% | +$176 | +90.1% |
| Minute Markets | 264 | 30.0% | -$77 | -51.5% |

Только Minute Markets отрицательный (низкая n=264, exclusion candidate).

## Top features (LGB importance, anchor=24h, 51 cols total)

```
1. yes_price_at_anchor       714  (12.4% of total)
2. vwap_asof                 473
3. volume_asof               365
4. yes_price_72h             302
5. horizon_days              285
6. price_max_asof            283
7. price_min_asof            239
8. buy_ratio_72h_before      233
9. drift_72h_to_anchor       214
10. buy_ratio_24h_before     211
11. vol_24h_pct_asof         191
12. n_trades_asof            187
13. n_markets_in_event       187
14. price_range_asof         181
15. drift_7d_to_anchor       165
```

**12 из top-25 — новые Phase A+B+C фичи** (wallet/microstructure/event), importance budget: 60.9% old + 39.1% new.

## Files map (для воспроизводства)

| Слой | Файл |
|---|---|
| Canonical dataset build | `scripts/research/build_canonical_hf.py` + `build_ml_features.py` (55 cols) |
| Wallet registry | `scripts/research/build_wallet_registry.py` (2.44M wallets, top1%≥$107K) |
| Wallet flow per market | `scripts/research/build_wallet_flow_per_market.py` (13.3M rows) |
| Model training | `scripts/research/train_standalone_lgb_v2.py` (event-aware split + sample_weight=1/event_size) |
| Model artifacts | `/mnt/data/canonical/models/standalone_lgb_v2_anchor{4,24,72,168,720}h_*.pkl` |
| Shadow predictor cron | `scripts/research/standalone_v2_predictor.py` (hourly :00, MVP backtest mode — НЕ live) |
| Dry trades runner cron | `scripts/research/standalone_v2_dry_runner.py` (hourly :30, EV>10%, $5/trade) |
| API endpoints | `app/api/bot.py` `/v2/standalone/*` (4 endpoints — будут заменены на standard `/v2/detectors/STANDALONE_V2_LGB/*`) |

## Open questions для promotion to Virtual

1. **Live feature compute MISSING.** Текущий predictor только replays backtest. Для shadow stage нужно `compute_features_live.py` который берёт active markets и считает 51 фичу per-anchor as-of NOW. Без этого shadow=пусто.
2. **Integration с v2_execution.py** — модель не emit'ит signal в `detector_signals` table. Нужен adapter.
3. **Promotion gate Shadow→Virtual:** в production стандарт = n>=30 events + WR canonical match ±5pp. У нас n=5316 уже есть, но это backtest replay, не live shadow.

См. `docs/detector_lifecycle.md` для общих правил promotion.

## Honest re-audit 2026-05-02 на canonical HF + slippage

Скрипт: `scripts/research/standalone_v2_canonical_audit.py`. Holdout 20% events time-based, slippage 0/2/3/5pp.

**Выживают на slippage 5pp** (worst-case $100 stake на тонкой книге):

| Anchor | Category | n | WR | ROI@5pp |
|--------|----------|---|----|---------| 
| 4h     | Sports        | 446  | 71.5% | +35.1% |
| 4h     | Geopolitics   | 281  | 52.0% | +27.6% |
| 24h    | Geopolitics   | 248  | 46.8% | +39.3% |
| 72h    | Geopolitics   | 212  | 47.2% | +31.8% |
| 168h   | Geopolitics   | 208  | 50.5% | +27.0% |
| 720h   | Geopolitics   | 104  | 63.5% | +36.9% |
| 720h   | Politics      | 160  | 60.0% | +37.5% |

**Killed на 5pp:** Weather all anchors (−12 to −18%), Crypto 24/72h, Tech&AI 168h, Politics 4h.

**Critical findings:**
- **Per-row vs per-event mismatch.** На проде Geopolitics 24h: 147 markets / 38 events = 3.9× per-event. Real n events ≪ display.
- **Real slippage на $5 stake = 0.4pp** (измерил entry_price vs market_yes_price). При scale-up на $100 — ожидаем 1-3pp, не 5pp.
- **Real prod 7d numbers сильно выше canonical projection:** Other 24h $528/day, Geopolitics 24h $137/day. Возможно canonical holdout = `last 20% events by end_date` overrepresents stale events.

## Scale-up test 2026-05-02 (ACTIVE)

**Цель:** измерить slippage elasticity при $5 → $20 stake. Если linear scale → можно дальше до $100. Если sub-linear → знаем real ceiling.

**Per-bucket override** в `scripts/research/standalone_v2_dry_runner.py`:
```python
STAKE_OVERRIDE: dict[tuple[str, int], float] = {
    ("Other", 24): 20.0,
    ("Other", 72): 20.0,
    ("Geopolitics", 24): 20.0,
}
```
Активно с 2026-05-02 16:04 UTC. Старые записи $5 в БД сохранены (compare baseline). Новые dry_trades для этих 3 buckets идут с $20.

**Decision rule** через 2-3 дня:
- Linear: realized 7d PnL для override buckets ≈ 4× baseline → продолжать scale до $100
- Sub-linear: <2.5× baseline → slippage cost > linear, остановиться, измерять book depth напрямую
- Negative: <1× → kill scale-up, оставить $5 dry-only

После test обязательно revert override → standard $5 stake (или promote в proper config).

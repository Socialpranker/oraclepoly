# Legacy Strategies Audit — что переиспользовать как detectors

> Аудит 2026-04-30 после Phoenix refactor + Pipeline V2 launch.
> Контекст: 57 legacy strategies disabled, нужно извлечь паттерны для D7+.

## Top profitable patterns (n≥30, ROI > +1%)

| Strategy key | n | WR | PnL | ROI | Pattern |
|--------------|---|------|------|------|---------|
| `dyn_no_crypto_8-15_no_wh_z-1_vol_low` | 30 | 76.7% | $90 | 15.6% | Crypto NO, oracle z<0, div 8-15%, no whale, vol $1K-$10K |
| `dyn_no_sports_8-15_no_wh_z-1_p50-70_h_short_vol_micro` | 38 | 76.3% | $60 | 13.3% | Sports NO, mid-zone (50-70¢), oracle z<0, vol<$1K, horizon<7d |
| `dyn_no_crypto_3-5_no_wh_z-1` | 83 | 88.0% | $159 | 11.2% | Crypto NO, oracle z<0, div 3-5%, no whale |
| `politics_no_bias` | 83 | 57.8% | $99 | 10.6% | Politics NO, no whale, horizon<14d |
| `exp_no_midzone` | 31 | 45.2% | $35 | 8.9% | NO в mid-zone (entry 40-60¢) |
| `dyn_no_sports_5-8_wh_m+1_z-1` | 162 | 67.9% | $134 | 8.1% | Sports NO, +whale, oracle z<0, maker bullish, div 5-8% |
| `dyn_no_sports_8-15_no_wh_z-1_h_medium` | 130 | 72.3% | $71 | 6.7% | Sports NO, oracle z<0, div 8-15%, horizon 7-14d |
| `dyn_no_sports_8-15_no_wh_z-1_h_short_vol_micro` | 72 | 59.7% | $58 | 4.7% | Sports NO, oracle z<0, div 8-15%, horizon<7d, vol<$1K |
| `dyn_no_sports_8-15_no_wh_z-1` | 141 | 63.1% | $40 | 1.6% | Base Sports NO + oracle z<0 + div 8-15% |

## Главные паттерны (extracted)

### Pattern P1 — **Oracle Z-Score Bearish** (общий across crypto/sports)
**Логика:** когда `oracle_score < market_price` (oracle думает YES overpriced) и **oracle z-score < 0** (это устойчивый, не шумовой сигнал) → fade NO.

**Параметры:**
- side = NO
- oracle z-score < 0 (z-score from rolling 7-30d distribution per market)
- divergence (market_price − oracle_score) ∈ [0.03, 0.15]
- whale_tx_count = 0 (без whale shock)
- category ∈ {Sports, Crypto}

**WR на legacy data:** 63-88% (зависит от div_bin)
**Edge:** systematic + 2-3pp над raw fade-NO

→ **D7 candidate**

### Pattern P2 — **Micro-Volume Sports Mid-Zone**
**Логика:** small-volume Sports markets в mid-zone (50-70¢) с oracle z<0 = retail fade точка.

**Параметры:**
- category = Sports
- side = NO
- entry_price ∈ [0.50, 0.70]
- horizon_days < 7
- volume_24h < $1000
- oracle z-score < 0

**WR на legacy data:** 76% (n=38)

→ **D8 candidate** (overlap с D4 small-market fade, но с oracle z-score фильтром)

### Pattern P3 — **Politics NO Bias** (без oracle)
**Логика:** Politics markets имеют systematic NO bias (YES outcomes резолвятся реже).

**Параметры:**
- category = Politics
- side = NO
- whale_tx_count = 0
- horizon_days < 14

**WR на legacy data:** 58% (n=83), но cumulative ROI 10.6%

→ **Уже частично покрыто D5** (на extreme zones). Оставшаяся часть — **D9 candidate** для mid-zone (entry 0.30-0.85).

### Pattern P4 — **Whale Maker Bullish + Oracle Bearish** (contrarian whale)
**Логика:** когда whale buys YES (maker bullish) **И** oracle thinks YES overpriced (z<0) → contrarian fade NO выигрывает.

**Параметры:**
- category = Sports
- side = NO
- whale_tx_count > 0
- maker_direction (z-score) > +1 (whale strongly bullish)
- oracle z-score < 0
- divergence 5-8%

**WR на legacy data:** 68% (n=162)

→ **D10 candidate**: anti-smart-money detector, использует **whale × oracle disagreement**

## Что НЕ переиспользовать

### Categorically lossy (drop):
- `favorite_longshot_extreme` — −$402 PnL (entry 0.97-1.0 даёт inverse edge)
- `sniper` — −$182, использует oracle threshold как gate (без oracle сломан)
- `dyn_yes_*` стратегии — все в минусе (YES-bias не работает)
- `weather_arb` — слишком nichy
- `dyn_no_sports_8-15_no_wh_z-1_p50-70_h_short` — низкий ROI

### Уже покрыто existing detectors:
- `favorite_longshot_fade` ≈ **D5_long_horizon_extreme_fade**
- `favorite_no` ≈ **D4_small_market_fade** (high zone)

## Roadmap из этого аудита

1. **D7 (high priority)** — Pattern P1 (Oracle Z-Score Bearish) на 9K+ HF samples
2. **D8** — Pattern P2 (Micro-Volume Sports Mid-Zone)
3. **D9** — Pattern P3 (Politics NO mid-zone)
4. **D10** — Pattern P4 (Whale × Oracle disagreement)

Все 4 paterна используют **oracle как feature**, не как gate.
Это значит:
- oracle recalc cron нужен (раз в 6h хватает)
- oracle z-score нужно вычислять (rolling distribution per market)

## Memory update

Добавить `legacy_audit_2026-04-30.md` в `memory/MEMORY.md`.

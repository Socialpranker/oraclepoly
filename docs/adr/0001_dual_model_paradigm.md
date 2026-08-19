# ADR 0001 — Dual Model Paradigm: Behavioral × Truth-Path

**Status:** Proposed
**Date:** 2026-05-24
**Author:** owner
**Supersedes:** none
**Related:** [docs/detector_lifecycle.md](../detector_lifecycle.md), [research/edge-synthesis-2026-05-24/README.md](../../research/edge-synthesis-2026-05-24/README.md)

## Context

### Что мы делали раньше

OraclePoly строила Bayesian oracle, который пытался **предсказать вероятность исхода события лучше Polymarket**. Это было неявное допущение: если наш fair value точнее, edge = `fair - market`.

Эмпирически это **не работает на crypto strike рынках** (см. [research/kronos-crypto-strike-2026-05](../../research/kronos-crypto-strike-2026-05/README.md)): Polymarket уже эффективно отражает математически корректные probabilities. GBM/Heston проиграли CLOB по Brier 2.9× со статистической уверенностью P=1.0.

При этом существующие 24 работающих детектора OraclePoly **на самом деле не предсказывают истину** — они предсказывают:

1. **Crowd behavior** (D26 fade whale extreme, D13 retail confidence fade, D30/D31/D33/D34 favorite fades) — куда пойдёт market price через поведенческие сигналы
2. **Resolution mechanics** (D29 negrisk basket, D43 resolution dispute, D40/D45/D46 cross-venue, D3 cross-mkt arb) — структурную механику резолюции через гарантированные математические/правовые ограничения

Эта **двойная природа** работающих детекторов никогда не была формализована. Мы её зафиксировали в research-синтезе 2026-05-24.

### Почему это важно

1. **Mental model:** без явной парадигмы новые детекторы пишутся ad-hoc, основано на интуиции одного research'а
2. **Validation:** каждый класс детекторов требует **разной методологии валидации** (signal lead time vs hindsight resolution analysis)
3. **Data needs:** Behavioral нужны live wallet/social/sentiment feeds, Truth-Path нужны regulatory/resolution mechanics knowledge bases
4. **Hiring/scaling:** при росте команды новые люди должны понимать какой класс они строят

## Decision

**OraclePoly строит две комплементарные модели:**

### Behavioral Crowd Model

**Что предсказывает:** куда пойдёт market price в ближайшие N часов на основе сигнала, который **опережает crowd reaction**.

**Mental model:**
```
P_market(t+N) ≈ f_crowd(signals_at_t)
edge = sign(P_market(t+N) - P_market(t)) * conviction
```

**Сигналы:**
- Whale wallet movements (informed leads crowd)
- Vegas line shifts (bookmakers vs Polymarket lag)
- Polling drift (Politics)
- Twitter/Reddit sentiment shifts
- Round-number psychology
- News reaction timing (5-min overreaction window)
- Time-horizon bias к 50%
- Streaks → gambler's fallacy
- LLM ensemble divergence (crowd vs aggregate AI estimate)

**Существующие детекторы класса A:** D26, D13, D14, D5, D30, D31, D33, D34, D32, D7b, D_esports, D44 (partially)

### Truth-Path Resolution Model

**Что предсказывает:** реальную вероятность резолюции через **специфическую механику события**, которую crowd массово не моделирует.

**Mental model:**
```
P_resolution = f_mechanic(event_structure, oracle_rules, basket_constraints)
edge = P_resolution - P_market(t)
```

**Сигналы:**
- UMA dispute risk (исторические proposals + bond размеры)
- Negrisk basket constraints (sum-to-1, выпуклые комбинации)
- Cross-venue arbitrage (Polymarket vs Kalshi/Manifold)
- Settlement criteria parsing (wording vs headline)
- Regulatory deadline mechanics (FDA, CFTC, court timing)
- Vegas/bookmaker odds (для Sports — структурно более точный oracle)
- Dependent market chains (P(A∩B) consistency)
- Term-structure (BTC>X Sep vs Nov — log-time bias)
- Resolution lag patterns

**Существующие детекторы класса B:** D29, D43, D38, D40, D45, D46, D3, D10b

### Hybrid (класс C)

Где Behavioral встречается с Truth-Path — самые big EV signals:
- Insider trading detection (informed crowd + truth signal через wallet behavior)
- Whale category specialist (crowd indicator + domain truth)
- News momentum + UMA dispute (crowd reaction + resolution mechanic)

**Существующие детекторы класса C:** D41 momentum, D42 event-driven ML, D36 OBI, D37 depth decay, D44 late convergence

### Что НЕ строим (anti-decision)

1. **Pure forecasting models** (Kronos, GBM на price): доказательно проигрывают CLOB на efficient markets. Если стоит вопрос "наш forecast vs Polymarket forecast" — мы проигрываем.
2. **HFT latency arbitrage**: window 2.7s, 73% captured by sub-100ms bots. У нас нет инфраструктуры.
3. **Universal/category-agnostic detectors**: edge size зависит от категории (Finance 0.17pp gap, Entertainment 7.32pp gap). Любой новый детектор должен иметь **категориальный whitelist**.

## Consequences

### Положительные

1. **Чёткие критерии "стоит ли строить детектор":**
   - Это Behavioral или Truth-Path? Если ни то ни другое — отказываем
   - Какой сигнал опережает / что crowd не моделирует — должно быть явно
   - Категориальный whitelist — обязателен

2. **Разные validation methodology:**
   - Behavioral: signal lead time analysis на trade data, корреляция signal(t) → price(t+N)
   - Truth-Path: hindsight resolution analysis, был ли наш truth-path signal prescient

3. **Понятная roadmap:** существующая matrix `class A × class B × class C` показывает gaps. Видно где **новых детекторов больше всего возможно**:
   - Behavioral: Twitter sentiment lead, Polling drift, LLM ensemble — никем не делается
   - Truth-Path: regulatory deadline mechanics, dependent market chains — слабо покрыто

4. **Calibration becomes multiplicative leverage:** улучшение probability calibration существующих 24 детекторов через G18 даёт линейный rollback ROI на всех — больше чем 5 новых детекторов.

### Отрицательные / риски

1. **Парадигма может быть неполной:** возможны edges, которые не behavioral и не truth-path (например, **mechanical microstructure** — OBI/depth-decay). Решение: вводим класс C "Hybrid" для смешанных
2. **Граница A/B размытая:** D45 chainlink lead-lag — это behavioral (crowd lag) или truth-path (Chainlink = oracle truth)? Решение: каждый детектор должен иметь **primary class** в docstring + secondary tags
3. **Anti-pattern: пытаться втиснуть существующие в новую таксономию неестественно.** Решение: классификация делается **post-hoc**, не насильно. Существующие 24 уже классифицированы — это эмпирическая карта, не догма

## Implementation guidelines

### Для каждого нового детектора (D47+)

Docstring должен явно указывать:

```python
"""DXX_<name>.

Class: Behavioral | Truth-Path | Hybrid (primary class + optional secondary)
Mental model: <one sentence>
Edge mechanism: <what crowd misprices / what mechanic crowd doesn't model>
Predictive signal: <observable data input>
Source data: <where signal comes from>
Category whitelist: <Sports/Politics/Crypto/...>
Expected mispricing direction: <YES-overpriced / NO-overpriced>
Backtest hypothesis: <falsifiable statement on canonical data>
Research source: <paper / report / hypothesis ID>
"""
```

### Validation pipeline (использует существующий 4-stage)

См. [docs/detector_lifecycle.md](../detector_lifecycle.md). Никаких изменений в pipeline — только дополнительные **class-specific gates**:

**Class A (Behavioral) gates:**
- Backtest: Wilson lower CI > 0.55 на ≥200 signals **per category** (не aggregate)
- Shadow: signal lead time analysis — was signal_t < price_movement_t+N?
- Virtual: post-fee Sharpe > 1.0 на ≥100 trades

**Class B (Truth-Path) gates:**
- Backtest: hindsight resolution accuracy > baseline (CLOB) by 0.02 Brier on relevant subset
- Shadow: counterfactual analysis — какие markets были бы caught?
- Virtual: same as Class A

**Class C (Hybrid):** оба пайплайна параллельно, accept если **любой** прошёл

### Class-tag в БД

Добавить поле `detector_class` в `detector_state` table:

```sql
ALTER TABLE detector_state ADD COLUMN detector_class TEXT 
  CHECK (detector_class IN ('behavioral', 'truth_path', 'hybrid'));
```

(миграция при первой реализации detector класса C)

## Validation criteria (для самого ADR)

ADR будет считаться **успешным** если через 6 месяцев:

1. **≥4 новых детекторов** классов A/B/C построены и в Shadow или выше
2. **≥1 детектор Class A в Virtual или Live** — доказывает что behavioral signals работают
3. **≥1 детектор Class B в Virtual или Live** — доказывает что truth-path работают
4. **Существующие 24 детектора правильно классифицированы** — этот ADR не противоречит эмпирике

ADR будет **пересмотрен** если:
1. Через 6 месяцев нет ни одного нового Class A или Class B детектора в Virtual+
2. Появляется fundamental gap — есть работающий детектор, который не в A/B/C
3. Class A и Class B одинаково хорошо работают на одних и тех же сигналах (классификация бессмысленна)

## Alternatives considered

### Alt 1 — Не формализовать парадигму, продолжать ad-hoc

**Отвергнуто:** уже есть 24 детектора и 4 research'а. Без mental model следующие 24 детектора будут писаться вслепую. Дешевле сейчас.

### Alt 2 — Three model paradigm (add "Microstructure" как отдельный класс)

**Отвергнуто:** OBI/depth/VPIN — это поддиапазон Behavioral (микро-поведение order flow). Иначе мы будем плодить классы под каждый тип сигнала.

### Alt 3 — Сделать наоборот: pure forecasting + Kronos в production

**Отвергнуто:** см. Context — эмпирически проигрывает CLOB.

## Related research

- [research/edge-synthesis-2026-05-24/README.md](../../research/edge-synthesis-2026-05-24/README.md) — synthesis 4 existing research, 1748 строк
- [research/polymarket-alpha-hypotheses-2026-05](../../research/polymarket-alpha-hypotheses-2026-05/2026-05-12.md) — 10 testable hypotheses, источник G1-G10
- [research/prediction-market-edge-sources](../../research/prediction-market-edge-sources/report.md) — структурные edges, YES-bias 64pp
- [research/polymarket-orthogonal-strategies](../../research/polymarket-orthogonal-strategies/report.md) — 7 ортогональных стратегий
- [research/novye-torgovye-strategii-prediction-markets-polymarket](../../research/novye-torgovye-strategii-prediction-markets-polymarket/report.md) — 8 направлений
- [research/kronos-crypto-strike-2026-05/README.md](../../research/kronos-crypto-strike-2026-05/README.md) — negative result для pure forecasting

## Decision log

- 2026-05-24: Proposed by the owner during conversation. First batch of detectors selected: G1 (D47 mean-reversion), G18 (calibration edge), N3 (D49 Vegas line), N7 (D50 LLM ensemble). Promotion via parallel exploration loop.

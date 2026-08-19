# Oracle Engine — Bayesian Score

Ядро расчёта Oracle Score — Bayesian Log-Odds Framework.

## Ключевые файлы

| Компонент | Файл |
|-----------|------|
| Bayesian engine | `app/core/scoring.py` → `BayesianScoreCalculator` |
| Оркестратор | `app/handlers/oracle_calculator.py` → `recalculate_oracle_score()` |
| Калибровка (сервис) | `app/services/calibration_service.py` → 3-tier fallback: Redis → DB → hardcoded |
| Калибровка (оптимизация) | `app/jobs/calibration_grid_search.py` → Optuna search |
| Whale credibility | `app/handlers/whale_analytics.py` → P&L, ROI, timing_score, bankroll B_est |
| Параметры модели | `app/core/config.py` → `scoring_w_*`, `scoring_prior_*`, `scoring_credibility_gamma` |
| Hurst exponent | `app/core/hurst.py` → адаптация весов по фрактальной размерности |

## Whale Signal Chain

- **Kelly Fraction**: `f = min(S / B_est, 0.25)` — доля ставки от банкролла
- **Informed Beta Prior**: `C = (W+α)/(W+L+α+β)` — настраиваемый prior (α=1, β=1 по умолчанию, при α=5.5, β=4.5 → mean=0.55)
- **Temporal Discounting**: `discounted_record(W, L, γ=0.97)` — старые сделки теряют вес (γ^100 ≈ 0.048)
- **Expert Weight**: `multiplier = 0.5 + expert_weight` — модификатор силы сигнала [0.5, 1.5]
- **Timing Boost**: `C_adj = C + timing_score × scale` — credibility modifier по timing_score
- **Whale Bayes Factor**: `Λ = C_adj × expert_multiplier × (Z_whale - Z_mkt)` — вклад кита в log-odds
- **Liquidity Gating**: `Λ_gated = Λ × tanh(δ × depth / volume)` — коррекция на ликвидность (fallback 0.5×Λ)
- **Herding Discount**: кластеры китов (одно направление, <1ч) агрегируются как `√N × avg` вместо `N × avg`

## News Signal Chain

- **Power Law Decay**: `Λ(t) = Λ₀ / (1 + t/τ)^α` — степенной распад с длинным хвостом (α=0.52). Fallback exponential при α=0
- **Sentiment Direction**: `Λ_initial = engagement × max_lambda × sentiment`

## Smart Money (3 аддитивных терма)

- **Fresh Wallet**: новые кошельки с крупными ставками (w_fresh=0.6)
- **Cluster Coordination**: скоординированные кластеры кошельков (w_cluster=0.7)
- **Registry Match**: совпадение с реестром smart money (w_registry=0.3)

## Aggregation

```
Z_post = w_mkt×Z_mkt + w_whale×Σ(Λ_whale) + w_news×Σ(Λ_news)
       + w_fresh×Σ(SM_fresh) + w_cluster×Σ(SM_cluster) + w_registry×Σ(SM_registry)
P_oracle = sigmoid(Z_post / T)
```

- **Weights**: w_mkt=1.0, w_whale=0.3 (снижено 2026-03-15: counter-indicator), w_news=0.6, w_fresh=0.6, w_cluster=0.7, w_registry=0.3
- **Temperature**: T=1.0, калибруется через grid search
- **Divergence порог**: ±10% (underpriced/overpriced), magnitude: >15% strong, >10% moderate

## Calibration (Calibrator)

- **Beta Calibration** (Kull 2017): `P_cal = sigmoid(a × logit(p) + b)` — 2 параметра
- **Temperature Scaling**: `P_cal = sigmoid(Z / T)` — 1 параметр
- **Brier Decomposition** (Murphy): `BS = Reliability - Resolution + Uncertainty`
- **Grid Search**: `find_optimal_beta_params()`, `find_optimal_temperature()` — минимизация Log Loss

## Critical Fallbacks

| Компонент | При отказе | Деградация |
|-----------|-----------|-----------|
| WebSocket (microstructure) | Redis miss | `Λ_gated = Λ_whale × 0.5` |
| Bankroll (B_est) | Нет позиций на Polymarket | default 50K |
| Vector search (Qdrant) | Timeout | Нет semantic matching → пропуск |
| OpenAI embeddings | Timeout | Нет matching новостей к рынкам |

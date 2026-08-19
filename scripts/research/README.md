# Research Scripts

One-off аналитические и backtest-скрипты. **Не на кроне**, запускаются вручную для проверки гипотез.

## Каталог

### Strategy backtest & discovery

| Script | Purpose |
|---|---|
| `strategy_discovery_historical.py` | Grid search новых стратегий на 8.3M positions / 684K markets |
| `strategy_discovery_oracle_tournament.py` | Discovery + tournament на oracle-enriched данных |
| `strategy_discovery_prod.py` | Discovery на прод oracle_snapshots |
| `strategy_backtest_all.py` | Backtest всех стратегий на prod oracle_snapshots |
| `strategy_backtest_oracle.py` | Backtest со скорингом (research weights) на HF |
| `strategy_tournament.py` | Ранжирование стратегий с $500 банком |
| `strategy_volume_backtest.py` | Сколько ставок генерируют стратегии на исторических данных |

### Historical analysis

| Script | Purpose |
|---|---|
| `historical_pit_loader.py` | Point-in-time oracle scoring → `pit_dataset.parquet` |
| `historical_oracle_bin_analysis.py` | Где oracle даёт edge, где нет (бины) |
| `historical_oracle_conditional_analysis.py` | Conditional probability search |
| `historical_oracle_sanity_check.py` | Валидация "research бьёт prod 45×" |
| `historical_smart_money_registry.py` | Walk-forward smart money registry |
| `historical_edge_search.py` | Поиск любого edge в PIT датасете |
| `historical_category_analysis.py` | **Источник констант** в `app/core/scoring.py` (optimism_bias, vwap_deviation, taker_imbalance_corr) |

### Aggregations / HF data prep

| Script | Purpose |
|---|---|
| `aggregate_historical.py` | trades.parquet → market aggregates |
| `aggregate_phase2.py` | Phase 2: wallet-market aggregates + outcomes |
| `hf_bias_verification.py` | Верификация claims из deep research на HF |

### Calibration & filter ablation

| Script | Purpose |
|---|---|
| `calibrate_temperature_by_category.py` | Temperature scaling per category |
| `global_filters_ablation.py` | Ablation глобальных фильтров через full pipeline |
| `filter_audit.py` | Аудит всех фильтров на 6.8M historical positions |

### Production oracle snapshots research

| Script | Purpose |
|---|---|
| `prod_oracle_snapshots_dump.py` | Dump resolved oracle_snapshots → local parquet |
| `prod_oracle_snapshots_backtest.py` | Backtest реального прод-oracle на parquet dump |
| `backtest_stake_comparison.py` | Сравнение Kelly vs Fixed stake sizing |

### Microstructure

| Script | Purpose |
|---|---|
| `research_maker_vs_taker.py` | Maker vs taker liquidity research |

### Whale & cluster

| Script | Purpose |
|---|---|
| `backfill_whales.py` | Backfill whale profiles + smart money registry |
| `backtest_cluster_strategies.py` | Cluster strategies backtest (Kelly + daily limit) |
| `backtest_historical.py` | Walk-forward backtest на HF dataset |

### Manual research tools

| Script | Purpose |
|---|---|
| `dry_pipeline_reset.py` | Void open positions, reset bankroll → initial |

---

## Зависимости от данных

| Источник | Скрипты |
|---|---|
| `/mnt/data/polymarket_dataset/` (HF) | aggregate_*, historical_*, hf_bias_verification, backtest_historical |
| `/mnt/data/hf_data/quant.parquet` (568M trades) | historical_pit_loader |
| Production DB (через SSH) | strategy_discovery_prod, prod_oracle_snapshots_*, calibrate_temperature, backfill_whales |
| Local PIT dataset | strategy_*, filter_audit, global_filters_ablation, historical_oracle_* |

## Запуск на VPS

```bash
ssh root@<YOUR_SERVER_IP>
cd /root/oracle
nohup python3 scripts/research/<script>.py [args] > /var/log/oracle/<script>.log 2>&1 &
```

Для долгих скриптов (>10 минут) — обязательно `nohup` + лог в `/var/log/oracle/`.

## Pre-existing tech debt

В research-скриптах есть ruff-warnings (E712, F841, ambiguous variable names и т.д.). Pre-commit hook применяет ruff только к `^app/`, поэтому эти warnings не блокируют коммиты. Исправлять можно по мере касания, но не критично.

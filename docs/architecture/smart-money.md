# Smart Money Detection

## Ключевые файлы

Все в `app/handlers/smart_money/`:

| Компонент | Файл |
|-----------|------|
| Оркестратор | `engine.py` → `SmartMoneyEngine` |
| Fresh Wallet | `fresh_wallet_detector.py` → `FreshWalletDetector` |
| Clusters | `cluster_detector.py` → `ClusterDetector` |
| Registry | `registry.py` → `SmartMoneyRegistry` |
| Funding Chains | `funding_chain_analyzer.py` → `FundingChainAnalyzer` |
| Sectoral | `sectoral_scorer.py` → `SectoralScorer` |
| Scanner (cron) | `app/jobs/smart_money_scanner.py` |

## Архитектура

```
SmartMoneyEngine.analyze(market_id):
  ├── FreshWalletDetector → новые кошельки + крупные ставки → Bayes Factor (w=0.6)
  ├── ClusterDetector → скоординированные группы → Bayes Factor (w=0.7)
  ├── SmartMoneyRegistry → известные адреса → Bayes Factor (w=0.3)
  ├── FundingChainAnalyzer → цепочки финансирования между кошельками
  └── SectoralScorer → win rate по секторам/категориям
```

Три Bayes Factor аддитивно добавляются в `Z_post` в `scoring.py`.

## PostgreSQL таблицы

- `smart_money_wallets` — реестр (address, is_active, detection_source)
- `wallet_clusters` — кластеры (cluster_id, addresses, coordination_score)

## Cron Jobs (smart_money_scanner.py)

- `smart-money-registry-sync` — синхронизация реестра
- `smart-money-cluster-detection` — обнаружение кластеров
- `smart-money-funding-scan` — анализ цепочек
- `smart-money-sector-update` — обновление секторальных win rates

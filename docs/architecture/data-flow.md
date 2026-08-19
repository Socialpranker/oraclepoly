# Data Flow — Пайплайны и зависимости

## Главные пайплайны

### 1. Whale Transaction → Oracle Recalc (основной поток)

```
WebSocket (user channel, ≥$5K) → Redis queue: oracle/transaction.received
  → whale_watcher (idempотентность tx_hash+log_index)
  → ProxyWalletService (EOA↔proxy, bankroll B_est)
  → WhaleAnalyticsEngine (P&L, ROI, W/L, timing_score)
  → anomaly_detector (статистические аномалии)
  → oracle_calculator (параллельный fetch: market + txns + news + microstructure + smart_money)
  → BayesianScoreCalculator.calculate_full_score()
  → UPDATE markets SET oracle_score
  → alert_matcher (проверка user alert rules) → notification
  → [если ≥$25K] AI analysis (oracle_analyst)
```

### 2. News → Market Matching → Oracle Update

```
news_aggregate (cron 1h) → NewsAggregatorService (Google News RSS + NewsAPI)
  → PostgreSQL news (dedup by URL)
news_sync (cron 2h) → Firecrawl + Serper fallback → R2 archive + Qdrant embeddings
  → SemanticMatcher (vector search ≥0.85 → auto-valid, 0.7-0.85 → LLM cross-check)
  → temporal filtering (volume before/after news)
  → news_market_mappings → trigger oracle recalc
```

### 3. Smart Money Detection (cron: smart_money_scanner)

```
SmartMoneyEngine (оркестратор):
  → FreshWalletDetector (новые кошельки с крупными ставками)
  → ClusterDetector (скоординированные группы кошельков)
  → SmartMoneyRegistry (известные smart money адреса)
  → FundingChainAnalyzer (анализ цепочек финансирования)
  → SectoralScorer (секторальная оценка)
  → 3 Bayes Factors → oracle_calculator pipeline
```

## Service Dependencies

```
oracle_calculator ← BayesianScoreCalculator (scoring.py)
                  ← PolymarketService (market data)
                  ← PolymarketWebSocketService → Redis microstructure:{market_id}
                  ← WhaleAnalyticsEngine (bankroll B_est из позиций)
                  ← ProxyWalletService (EOA↔proxy маппинг)
                  ← SmartMoneyEngine (fresh wallets, clusters, registry, funding chains, sectoral)
                  ← VectorService/Qdrant (news embeddings)
                  ← PostgreSQL (transactions, news_market_mappings)

whale_watcher ← ProxyWalletService
              ← WhaleAnalyticsEngine
              ← PostgreSQL (transactions, whales)
              ← R2 (archive)

alert_matcher ← PostgreSQL (alert_rules, notifications)
              ← NotificationService (Telegram delivery)

anomaly_detector ← PostgreSQL (transactions, market history)
                 ← статистический анализ (z-score, volume spikes)

semantic_matcher ← VectorService (semantic search)
                 ← news_impact agent (sentiment LLM)
                 ← PostgreSQL (news_market_mappings)

All external services ← circuit_breaker.py (@polymarket_breaker, @firecrawl_breaker)
                      ← retry.py (@http_retry, exponential backoff)
```

## News Aggregation Fallbacks

| Источник | Роль | Деградация |
|----------|------|-----------|
| Google News RSS | Primary | Бесплатно, 1 req/sec |
| NewsAPI.org | Fallback | Free tier 100 req/day |
| Firecrawl | Full text scraping | circuit_breaker → Serper fallback |
| Serper | Full text fallback | Оба down → пропуск |

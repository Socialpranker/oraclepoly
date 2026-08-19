-- Migration: Performance Indexes
-- Date: 2026-02-12
-- Description: Adds composite indexes for frequent queries

-- ============================================
-- 1. TRANSACTIONS TABLE INDEXES
-- ============================================

-- Для oracle_calculator.py: запрос по market_id + created_at
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_transactions_market_created
ON transactions(market_id, created_at DESC);

-- Для whale_watcher.py: запрос по whale_id + created_at
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_transactions_whale_created
ON transactions(whale_id, created_at DESC);

-- NOTE: idx_transactions_tx_hash уже существует в 001_initial_schema.sql

-- ============================================
-- 2. NEWS_MARKET_MAPPINGS INDEXES
-- ============================================

-- Для semantic_matcher.py и oracle_calculator.py
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_news_mappings_market_created
ON news_market_mappings(market_id, created_at DESC);

-- Для фильтрации по confidence
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_news_mappings_confidence
ON news_market_mappings(confidence_score DESC)
WHERE confidence_score >= 0.85;

-- ============================================
-- 3. BLACK_SWAN_SCENARIOS INDEXES
-- ============================================

-- Для oracle_calculator.py
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_scenarios_market_active
ON black_swan_scenarios(market_id, is_active)
WHERE is_active = TRUE;

-- ============================================
-- 4. CONSENSUS_VOTES INDEXES
-- ============================================

-- Для подсчета голосов за день
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_votes_user_date_range
ON consensus_votes(user_id, created_at DESC);

-- Для rewarded votes
CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_votes_rewarded
ON consensus_votes(market_id, rewarded)
WHERE rewarded = FALSE;

-- ============================================
-- 5. ANALYZE TABLES
-- ============================================

ANALYZE transactions;
ANALYZE news_market_mappings;
ANALYZE black_swan_scenarios;
ANALYZE consensus_votes;

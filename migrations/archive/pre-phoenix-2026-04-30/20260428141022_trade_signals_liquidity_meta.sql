-- 20260428141022_trade_signals_liquidity_meta.sql
-- Description: Add liquidity_meta JSONB column to trade_signals.
--
-- Stores classification {tier, volume_24h, best_book_depth} computed at signal generation
-- time. Used for UI badges and post-mortem analytics. See plan
-- docs/superpowers/plans/2026-04-28-thin-book-gate.md.

ALTER TABLE trade_signals
    ADD COLUMN IF NOT EXISTS liquidity_meta JSONB NOT NULL DEFAULT '{}'::jsonb;

CREATE INDEX IF NOT EXISTS idx_trade_signals_liquidity_tier
    ON trade_signals ((liquidity_meta->>'tier'));

COMMENT ON COLUMN trade_signals.liquidity_meta IS
  'Liquidity classification at signal time: {tier: thin|low|ok|deep, volume_24h, best_book_depth}.';

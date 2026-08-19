-- Migration: 20260603120000_wallet_smart_money_lower_idx
-- Add functional index on LOWER(wallet) for case-insensitive lookup
-- Needed because wallet_smart_money stores EIP-55 checksum addresses (mixed case)
-- but trade API returns lowercase addresses.
-- Without this index, LOWER(wallet) = ANY(...) causes seq scan on 2.4M rows.

CREATE INDEX CONCURRENTLY IF NOT EXISTS idx_wallet_smart_money_lower_wallet
    ON wallet_smart_money (LOWER(wallet));

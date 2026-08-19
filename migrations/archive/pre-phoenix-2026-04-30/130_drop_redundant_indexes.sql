-- Migration 121: Drop redundant/wasteful indexes found during audit

-- idx_whales_address: redundant with UNIQUE constraint on whales.address
DROP INDEX IF EXISTS idx_whales_address;

-- idx_transactions_from_address: column never used in WHERE/ORDER BY
DROP INDEX IF EXISTS idx_transactions_from_address;

-- idx_transactions_to_address: column never used in WHERE/ORDER BY
DROP INDEX IF EXISTS idx_transactions_to_address;

-- idx_transactions_asset: column never used in WHERE/ORDER BY
DROP INDEX IF EXISTS idx_transactions_asset;

-- idx_votes_user_date_range: duplicates idx_consensus_votes_user_date (same columns)
DROP INDEX IF EXISTS idx_votes_user_date_range;

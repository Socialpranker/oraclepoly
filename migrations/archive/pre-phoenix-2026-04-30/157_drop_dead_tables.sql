-- Drop dead tables and columns identified by dead code audit (2026-03-10)

-- wallet_mappings: zero references in app/, ProxyWalletService never implemented
DROP TABLE IF EXISTS wallet_mappings CASCADE;

-- black_swan_scenarios: agent black_swan.py was deleted, no INSERT anywhere
-- Table is only SELECTed (always empty) and DELETEd
DROP TABLE IF EXISTS black_swan_scenarios CASCADE;

-- Dead columns
ALTER TABLE notifications DROP COLUMN IF EXISTS is_read;
ALTER TABLE users DROP COLUMN IF EXISTS discord_webhook_url;

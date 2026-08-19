-- 147_drop_political_tables.sql
-- Удаление таблиц Political Intelligence (замена Apify, удаление Congress/GDELT)

DROP TABLE IF EXISTS political_market_correlations CASCADE;
DROP TABLE IF EXISTS congress_lobbying CASCADE;
DROP TABLE IF EXISTS congress_votes CASCADE;
DROP TABLE IF EXISTS congress_trades CASCADE;
DROP TABLE IF EXISTS congress_members CASCADE;

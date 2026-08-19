-- 179: DOUBLE PRECISION → NUMERIC для финансовых колонок
--
-- DOUBLE PRECISION (IEEE 754 float) теряет точность:
--   0.1 + 0.2 = 0.30000000000000004
-- Для USDC сумм и цен используем NUMERIC (arbitrary precision).
--
-- Затронутые таблицы: paper_positions, live_paper_positions, simulation_daily_snapshots
-- Все три — аналитические (не hot path), безопасно ALTER TYPE.

-- paper_positions
ALTER TABLE paper_positions
    ALTER COLUMN entry_price TYPE NUMERIC(8,6),
    ALTER COLUMN oracle_score_at_entry TYPE NUMERIC(6,4),
    ALTER COLUMN divergence_at_entry TYPE NUMERIC(8,4),
    ALTER COLUMN stake TYPE NUMERIC(12,2),
    ALTER COLUMN pnl TYPE NUMERIC(12,4);

-- live_paper_positions
ALTER TABLE live_paper_positions
    ALTER COLUMN entry_price TYPE NUMERIC(8,6),
    ALTER COLUMN oracle_score_at_entry TYPE NUMERIC(6,4),
    ALTER COLUMN divergence_at_entry TYPE NUMERIC(8,4),
    ALTER COLUMN stake TYPE NUMERIC(12,2),
    ALTER COLUMN pnl TYPE NUMERIC(12,4);

-- simulation_daily_snapshots
ALTER TABLE simulation_daily_snapshots
    ALTER COLUMN initial_bank TYPE NUMERIC(14,2),
    ALTER COLUMN final_bank TYPE NUMERIC(14,2),
    ALTER COLUMN total_pnl TYPE NUMERIC(14,2),
    ALTER COLUMN max_drawdown TYPE NUMERIC(10,4);

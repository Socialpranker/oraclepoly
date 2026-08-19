-- Migration: restore_oracle_strategies
-- Восстановление 10 disabled стратегий, прошедших walk-forward OOS валидацию
-- на timeseries.parquet (227K snapshots, Feb 24 — Apr 15 2026).
--
-- Источник: research/validate_oracle_strategies.py
-- Полный отчёт: research/data/oracle_validation_2026-04-16.md
--
-- Критерии RESTORE:
--   - trades ≥ 400 (достаточно для статистической значимости)
--   - PF ≥ 1.20 (с запасом над break-even)
--   - WR ≥ 55%
--   - не дубликат по метрикам (фильтры реально разные)
--
-- Стратегии с bootstrap CI — наиболее надёжны (CI lower ≥ 1.0):
--   - sports_mid_horizon     PF=1.29 CI=[1.21, 1.37]
--   - sniper                 PF=1.28 CI=[1.20, 1.37]
--
-- Стратегии с высоким PF но без CI (brute-force из лога):
--   - dyn_no_sports_5-8_no_wh_z-1_p85-99   PF=3.04 WR=96.1% (heavy favorites + z<0)
--   - dyn_no_sports_8-15_no_wh_z-1         PF=1.29 WR=60.9% (основная Sports z<0)
--   - dyn_no_crypto_3-5_no_wh_z-1          PF=1.25 WR=84.4% (crypto contra)
--   - culture_no_baseline                  PF=2.19 WR=79.1% (code strategy)
--   - dyn_yes_sports_3-5_no_wh_z+1         PF=4.71 WR=97.3% (YES фавориты)
--   - dyn_no_sports_8-15_no_wh_z-1_p30-50  PF=1.16 (borderline — WATCH candidate)
--
-- ВАЖНО: pre-Apr-13 позиции удалены миграцией 20260415120000.
-- Новые dry-позиции начнут открываться со следующего cron-цикла strategy-positions.

BEGIN;

-- Bootstrap-validated (наиболее надёжные)
UPDATE strategy_state
SET stage = 'dry', disabled_reason = NULL, updated_at = NOW()
WHERE strategy_key IN (
    'sports_mid_horizon',
    'sniper'
) AND stage = 'disabled';

-- High PF (brute-force validated)
UPDATE strategy_state
SET stage = 'dry', disabled_reason = NULL, updated_at = NOW()
WHERE strategy_key IN (
    'dyn_no_sports_5-8_no_wh_z-1_p85-99',
    'dyn_no_sports_8-15_no_wh_z-1',
    'dyn_no_crypto_3-5_no_wh_z-1',
    'culture_no_baseline',
    'dyn_yes_sports_3-5_no_wh_z+1'
) AND stage = 'disabled';

-- Синхронизация bankroll
INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, stage)
SELECT strategy_key, 10000, 10000, 'dry'
FROM strategy_state
WHERE stage = 'dry' AND strategy_key IN (
    'sports_mid_horizon', 'sniper',
    'dyn_no_sports_5-8_no_wh_z-1_p85-99',
    'dyn_no_sports_8-15_no_wh_z-1',
    'dyn_no_crypto_3-5_no_wh_z-1',
    'culture_no_baseline',
    'dyn_yes_sports_3-5_no_wh_z+1'
)
ON CONFLICT (strategy_key) DO UPDATE
    SET stage = 'dry';

-- Финальное состояние: проверка
DO $$
DECLARE
    dry_count INT;
BEGIN
    SELECT COUNT(*) INTO dry_count FROM strategy_state WHERE stage = 'dry';
    RAISE NOTICE 'Total dry strategies after migration: %', dry_count;
END $$;

COMMIT;

-- 20260410154346_cleanup_zombie_paper_strategies.sql
-- Cleanup zombie strategy_state rows оставшихся в stage='paper' после paper→dry rename.
--
-- Context:
-- Миграция 20260405210000_seed_strategy_state.sql была применена ПОСЛЕ
-- 20260408163425_remove_disabled_strategies.sql (09:42:13 vs 09:40:07 UTC)
-- из-за порядка применения pending backlog на prod 2026-04-10.
-- Seed реинсертнула 12 стратегий с явным stage='paper', 4 из них были удалены
-- (baseline, sports_whale_yes, whale_contra, sports_yes_micro) — вернулись как zombie.
--
-- Python-файлы этих стратегий физически удалены (commits ea9a378, 8ec8093).
-- STRATEGY_REGISTRY их не содержит → код не может генерировать сигналы.
-- Bankroll для них тоже отсутствует → execution path не работает.
--
-- Action: DELETE zombie rows (не UPDATE paper→dry — стратегий больше нет).

DELETE FROM strategy_state
WHERE stage = 'paper'
  AND strategy_key IN ('baseline', 'sports_whale_yes', 'whale_contra', 'sports_yes_micro');

INSERT INTO strategy_audit (strategy_key, event, details)
SELECT
    key,
    'zombie_cleanup',
    jsonb_build_object(
        'reason', 'stage=paper after paper→dry rename, python file deleted',
        'migration', '20260410154346_cleanup_zombie_paper_strategies'
    )
FROM unnest(ARRAY['baseline', 'sports_whale_yes', 'whale_contra', 'sports_yes_micro']) AS key;

-- Disable narrow duplicate dry-strategies (2026-04-21)
--
-- Проблема:
--   Cross-strategy dedup (has_any_open_position в strategy_position_manager)
--   вместе с узкими подстратегиями discovery создавал тупик: широкая стратегия
--   всегда захватывала рынок первой, узкие subsets никогда не могли открыть
--   свою позицию и оставались с 0 ставок при 27-36 сигналов/день.
--
-- Решение:
--   Disable 44 узких стратегий без статистики. Оставить 13 keeper-стратегий:
--     - Все с dry_trades > 0 (уже собирают данные)
--     - По одной широкой keeper на группу (side × category × div_bin × z_sign)
--   В будущем узкие вариации будут оцениваться через strategy_virtual_positions
--   (shadow trading) без блокировки реальных позиций друг у друга.
--
-- После purge dry-линейка = 13 стратегий, одна на реальную торговую гипотезу.

UPDATE strategy_state
SET stage = 'disabled',
    disabled_reason = 'narrow_duplicate_2026_04_21: blocked by wider strategy in cross-dedup, no bets accumulated',
    updated_at = now()
WHERE stage = 'dry'
  AND COALESCE(dry_trades, 0) = 0
  AND strategy_key NOT IN (
    -- Keepers: широкие (один на группу)
    'dyn_no_crypto_5-8_no_wh_z-1_vol_low',
    'dyn_no_politics_8-15_no_wh_z-1_vol_micro',
    'dyn_no_crypto_8-15_no_wh_z-1_vol_low',
    'dyn_no_sports_5-8_no_wh_h_short',
    'dyn_no_sports_3-5_no_wh_h_sweet_spot_vol_micro',
    'dyn_yes_sports_8-15_no_wh_z+1_h_short_vol_micro',
    'dyn_no_politics_3-5_no_wh_z-1_p85-99'
    -- (Keepers с dry_trades > 0 автоматически исключены через COALESCE(dry_trades,0)=0)
  );

-- Sanity: после UPDATE должно остаться 15 dry стратегий:
--   13 dynamic (discovery/manual) + 2 code (sniper, politics_no_bias)
DO $$
DECLARE
    dry_count int;
    disabled_added int;
BEGIN
    SELECT COUNT(*) INTO dry_count FROM strategy_state WHERE stage = 'dry';
    IF dry_count != 15 THEN
        RAISE EXCEPTION 'Expected 15 dry strategies after purge (13 dynamic + 2 code), got %', dry_count;
    END IF;

    SELECT COUNT(*) INTO disabled_added FROM strategy_state
    WHERE disabled_reason LIKE 'narrow_duplicate_2026_04_21%';
    IF disabled_added != 44 THEN
        RAISE EXCEPTION 'Expected 44 strategies disabled, got %', disabled_added;
    END IF;
END $$;

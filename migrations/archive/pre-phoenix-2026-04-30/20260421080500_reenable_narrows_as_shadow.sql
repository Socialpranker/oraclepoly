-- Re-enable 44 ранее disabled узких стратегий как stage='shadow' (2026-04-21)
--
-- После миграции 20260421070000 узкие дубликаты были disabled, потому что не могли
-- открыть реальные позиции (проигрывали гонку за (market, side) широким стратегиям).
--
-- Теперь с появлением strategy_virtual_positions (20260421080000) они могут писать
-- shadow P&L независимо → переключаем их обратно в активный режим как 'shadow'.
--
-- Также меняем source='shadow_discovery' чтобы отличить от 'discovery' (real-trading).

UPDATE strategy_state
SET stage = 'shadow',
    source = 'shadow_discovery',
    disabled_reason = NULL,
    updated_at = now()
WHERE disabled_reason LIKE 'narrow_duplicate_2026_04_21%';

-- Sanity: после UPDATE должно быть 44 shadow + 13 dry + 2 code
DO $$
DECLARE
    shadow_count int;
    dry_count int;
BEGIN
    SELECT COUNT(*) INTO shadow_count FROM strategy_state WHERE stage = 'shadow';
    IF shadow_count != 44 THEN
        RAISE EXCEPTION 'Expected 44 shadow strategies, got %', shadow_count;
    END IF;

    SELECT COUNT(*) INTO dry_count FROM strategy_state WHERE stage = 'dry';
    IF dry_count != 15 THEN
        RAISE EXCEPTION 'Expected 15 dry strategies preserved, got %', dry_count;
    END IF;
END $$;

-- Триггеры для NOTIFY — real-time обновления через pg_notify.
-- Канал: table_changes
-- Payload: JSON с channel (имя таблицы), event (INSERT/UPDATE), data (строка).

-- Общая триггерная функция
CREATE OR REPLACE FUNCTION notify_table_change() RETURNS trigger AS $$
DECLARE
    payload TEXT;
BEGIN
    payload := json_build_object(
        'channel', TG_TABLE_NAME,
        'event', TG_OP,
        'data', row_to_json(NEW)
    )::text;

    -- pg_notify ограничен 8000 байт; при превышении обрезаем data
    IF length(payload) > 7500 THEN
        payload := json_build_object(
            'channel', TG_TABLE_NAME,
            'event', TG_OP,
            'data', json_build_object('id', NEW.id)
        )::text;
    END IF;

    PERFORM pg_notify('table_changes', payload);
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Триггер на markets
DROP TRIGGER IF EXISTS trg_markets_notify ON markets;
CREATE TRIGGER trg_markets_notify
    AFTER INSERT OR UPDATE ON markets
    FOR EACH ROW
    EXECUTE FUNCTION notify_table_change();

-- Триггер на transactions
DROP TRIGGER IF EXISTS trg_transactions_notify ON transactions;
CREATE TRIGGER trg_transactions_notify
    AFTER INSERT OR UPDATE ON transactions
    FOR EACH ROW
    EXECUTE FUNCTION notify_table_change();

-- Триггер на black_swan_scenarios
DROP TRIGGER IF EXISTS trg_black_swan_scenarios_notify ON black_swan_scenarios;
CREATE TRIGGER trg_black_swan_scenarios_notify
    AFTER INSERT OR UPDATE ON black_swan_scenarios
    FOR EACH ROW
    EXECUTE FUNCTION notify_table_change();

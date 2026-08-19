-- 20260804120155_notification_log.sql
-- Description: журнал отправок для time-based дедупа уведомлений (единый конверт, очередь 1).
--
-- Спека: docs/superpowers/specs/2026-08-04-notification-envelope-design.md §6.
-- Потребитель: app/services/notification_bus.py — _COOLDOWN_SQL (чтение) и
-- _MARK_SENT_SQL (запись через ON CONFLICT ... DO UPDATE).
--
-- Почему ключ именно такой: dedup_key = "{subject}|{kind}" и НЕ содержит ни текста
-- сообщения, ни календарного окна.
--   * текст исключён: цифры в сообщении плавают от прогона к прогону, и ключ,
--     зависящий от текста, сбрасывал бы cooldown при каждом изменении формулировки;
--   * календарное окно исключено: окно ВНУТРИ ключа обнуляет cooldown на своей
--     границе — ровно инцидент 2026-08-01 (PR #741), где дедуп advisory подавлял
--     0 сообщений из 17, потому что в хэш ключа входил календарный день.
-- Возраст записи проверяется time-based запросом (last_sent_at > now() - interval),
-- а не вычисляется из ключа.
--
-- Таблица растёт по числу РАЗЛИЧНЫХ (subject, kind), не по числу отправок:
-- ON CONFLICT DO UPDATE обновляет last_sent_at существующей строки. Для очереди 1
-- (13 источников × ~15 детекторов) это сотни строк, retention не требуется.

CREATE TABLE IF NOT EXISTS notification_log (
    dedup_key    text        PRIMARY KEY,
    last_sent_at timestamptz NOT NULL DEFAULT now()
);

-- Индекс под запросы «что отправлялось недавно» (наблюдаемость, разбор инцидентов).
-- NULLS LAST задан ЯВНО: в Postgres DESC по умолчанию даёт NULLS FIRST, и для
-- сортировки по времени это ставит строки без времени в начало выдачи.
CREATE INDEX IF NOT EXISTS idx_notification_log_sent_at
    ON notification_log (last_sent_at DESC NULLS LAST);

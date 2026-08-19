-- 20260804143241_notification_fyi_queue.sql
-- Description: очередь FYI-уведомлений, которую забирает Daily Brief (единый конверт, очередь 1).
--
-- Спека: docs/superpowers/specs/2026-08-04-notification-envelope-design.md §4 и §6.
--
-- Зачем таблица вообще. До неё notification_bus.publish() на Severity.FYI делал
-- `digested += 1` и ОТБРАСЫВАЛ уведомление: счётчик рос, сообщение исчезало,
-- потребителя не существовало нигде в коде. Спека §6 обещает «копится, забирает
-- Daily Brief» — этого механизма не было. Перевод источника в FYI без этой таблицы
-- означал бы тихую потерю сообщения — дословно риск №1 спеки §10 («джоба переведена
-- на шину, severity выставлен FYI, сообщение перестало приходить, и никто не заметил,
-- потому что оно приходило раз в месяц»). db_size_report шлёт 1 и 15 числа.
--
-- Почему не переиспользована notification_log. Там dedup_key — PRIMARY KEY, ровно
-- одна строка на (subject, kind), назначение — «когда последний раз отправляли».
-- Здесь нужен журнал НЕСКОЛЬКИХ несхлопываемых событий за сутки (три разных
-- детектора в одном virtual_promote_report — три строки), поэтому отдельная таблица
-- с surrogate id и без ON CONFLICT-схлопывания.
--
-- consumed_at вместо DELETE: дайджест помечает забранное, строки остаются для
-- разбора «почему в брифе было именно это».

CREATE TABLE IF NOT EXISTS notification_fyi_queue (
    id           bigserial   PRIMARY KEY,
    subject      text        NOT NULL,
    kind         text        NOT NULL,
    actor        text        NOT NULL,
    headline     text        NOT NULL,
    -- Готовая строка для дайджеста: рендер уже произошёл в шине. Дайджест не
    -- должен знать про Notification и повторно форматировать — граница §6
    -- («рендерер не знает про БД, шина не знает про HTML, джоба не знает
    -- ни про то, ни про другое»).
    digest_line  text        NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    consumed_at  timestamptz
);

-- Основной запрос дайджеста: «незабранное, старее — первым».
-- Частичный индекс по consumed_at IS NULL: очередь всегда почти пуста после
-- прогона брифа, а таблица растёт — сканировать нужно только хвост.
CREATE INDEX IF NOT EXISTS idx_notification_fyi_unconsumed
    ON notification_fyi_queue (created_at)
    WHERE consumed_at IS NULL;

-- Под разбор инцидентов «что забрал бриф такого-то числа».
-- NULLS LAST задан ЯВНО: в Postgres DESC по умолчанию даёт NULLS FIRST
-- (инцидент 2026-07-22), и незабранные строки (consumed_at IS NULL) вылезали бы
-- в начало выдачи «последнее забранное».
CREATE INDEX IF NOT EXISTS idx_notification_fyi_consumed_at
    ON notification_fyi_queue (consumed_at DESC NULLS LAST);

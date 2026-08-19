-- 177: Удаление мёртвых объектов (подтверждено grep по всему проекту)
--
-- user_watchlist: 0 ссылок в app/ и client/ (создана в 111, никогда не использовалась)
-- whales.avatar_url: 0 ссылок (whale API select'ит конкретные колонки без неё)
-- whales.is_anomaly: 0 ссылок + мёртвый индекс idx_whales_is_anomaly

DROP TABLE IF EXISTS user_watchlist CASCADE;

ALTER TABLE whales DROP COLUMN IF EXISTS avatar_url;
ALTER TABLE whales DROP COLUMN IF EXISTS is_anomaly;
DROP INDEX IF EXISTS idx_whales_is_anomaly;

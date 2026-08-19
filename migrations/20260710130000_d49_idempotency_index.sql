-- D49 strike-ladder idempotency: partial UNIQUE-индекс по (detector, tx_hash).
--
-- Проблема (техдолг-аудит 2026-07-10): DetectorSignalRepository.insert() делает
-- ON CONFLICT DO NOTHING без target-constraint → матчится только на PK id
-- (всегда свежий gen_random_uuid()), поэтому дедуп никогда не срабатывает.
-- D49 сканирует раз в 5 минут; при живом нарушении монотонности одна и та же
-- пара-нога (tx_hash = bucket_tx_hash(pair_key+leg, 4h-bucket)) вставлялась бы
-- ~12 раз за 4-часовое окно, раздувая detector_signals дублями и искажая
-- будущее обучение L1 Beta калибратора (shadow-цель детектора).
--
-- Фикс по образцу D1 (migrations/20260430141632_detector_signals_table.sql:47-49):
-- partial UNIQUE только для D49 → ON CONFLICT DO NOTHING начинает дедупить
-- по (detector, evidence->>'tx_hash') в пределах 4h-бакета.

CREATE UNIQUE INDEX IF NOT EXISTS uq_detector_signals_d49_tx
    ON public.detector_signals (detector, (evidence->>'tx_hash'))
    WHERE detector = 'D49_strike_ladder' AND evidence->>'tx_hash' IS NOT NULL;

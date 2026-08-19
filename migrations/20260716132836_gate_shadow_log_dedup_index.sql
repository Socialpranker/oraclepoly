-- 20260716132836_gate_shadow_log_dedup_index.sql
-- gate_shadow_log дедуп: UNIQUE-индекс по (detector, gate, detector_signal_id).
--
-- Проблема (virtual-improvement-аудит 2026-07-16): v2-execution гоняется раз в
-- минуту (crontab.conf), fetch_signals_for_active_pipeline() отдаёт сигнал пока
-- detector_signals.resolved=false — включая rejected по shadow-гейту (status
-- 'rejected' явно исключён из "активный ордер уже есть" в WHERE, см.
-- app/services/detector_signal_service.py). Каждый тик один и тот же rejected
-- сигнал снова проходит category_blacklist / temperature_blacklist /
-- category_health гейт → log_gate_shadow() → обычный INSERT без ON CONFLICT.
-- Прод: D42×Sports 6111 строк на 104 уникальных detector_signal_id (~59
-- дублей на сигнал — сигнал жил ~59 минут до резолва/выключения детектора).
-- Дубли искажали контрфактику: сырой COUNT(*) без DISTINCT почти привёл к
-- ложному выводу «блэклистить D42×Sports» (реальные сделки детектора в той
-- же категории были в плюсе).
--
-- Фикс по образцу D49 (migrations/20260710130000_d49_idempotency_index.sql):
-- UNIQUE-индекс → ON CONFLICT DO NOTHING в _INSERT_SQL начинает дедупить.
-- Не partial — таблица целиком append-only-по-намерению телеметрия, где
-- (detector, gate, detector_signal_id) всегда должен быть уникален.

CREATE UNIQUE INDEX IF NOT EXISTS uq_gate_shadow_log_detector_gate_signal
    ON public.gate_shadow_log (detector, gate, detector_signal_id);


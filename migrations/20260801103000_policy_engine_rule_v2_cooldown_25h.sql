-- Phase 2: policy engine v2 — cooldown_hours 6 → 25 (решение владельца 2026-08-01).
--
-- Причина: дедуп advisory чинился в два шага. Шаг 1 (коммит bc345fe4) сделал ключ
-- cooldown стабильным (name+verdict вместо config_hash с календарным днём внутри).
-- Но одного ключа мало: джоба суточная (cron 07:30), а окно было 6ч — оно физически
-- не дотягивается до предыдущего прогона, и дедуп продолжал подавлять 0 сообщений.
--
-- Контрфактика на 17 реальных исторических D3/D38-proposals (прод, 2026-08-01):
--   cooldown 6ч  → подавлено  0/17  (текущее поведение — спам)
--   cooldown 24ч → подавлено  8/17  (интервал между прогонами ровно 24.00ч —
--                                    окно попадает на границу и флапает по джиттеру)
--   cooldown 25ч → подавлено 12/17  ← выбрано: минимум, надёжно перекрывающий сутки
--
-- Остальные пороги без изменений — переносятся из v1 как есть.
--
-- Правки порогов = INSERT новой версии + деактивация старой, НЕ UPDATE params
-- (частичный уникальный индекс uq_policy_rules_active ON policy_rules (rule_key)
-- WHERE active гарантирует одну активную версию). Порядок обязателен: сначала
-- снимаем active с v1, иначе INSERT активной v2 нарушит индекс.

UPDATE policy_rules SET active = false
 WHERE rule_key = 'policy_engine' AND active;

INSERT INTO policy_rules (rule_key, version, params, active)
VALUES (
    'policy_engine',
    2,
    '{
        "drawdown_reduce_roi": -0.15,
        "drawdown_close_roi": -0.30,
        "drawdown_min_settled": 20,
        "drawdown_window_days": 30,
        "cooldown_hours": 25
    }'::jsonb,
    true
)
ON CONFLICT (rule_key, version) DO UPDATE
   SET params = EXCLUDED.params,
       active = true;

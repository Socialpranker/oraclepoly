-- 20260408163224_rename_paper_to_dry.sql
-- Переименовываем stage 'paper' → 'dry' в strategy_state.
-- 'paper' и 'dry' — одно и то же: симуляция с виртуальными деньгами.
-- 'dry' теперь единственный термин для этого режима.

UPDATE strategy_state SET stage = 'dry' WHERE stage = 'paper';


-- 20260705163719_add_outcome_proxy_to_detector_signals.sql
-- Description: Provenance-флаг proxy-резолва EXPIRED-сигналов по ценовому экстремуму.
--
-- Фикс №2 аудита virtual-стейджа (research/virtual-stage-audit-2026-07-05/):
-- 97% EXPIRED early-exit строк имели markets.current_price в резолюционном
-- экстремуме (≤0.02/≥0.98) — исход фактически предрешён, UMA-финализация
-- отстаёт, а сигналы выпадали из promote-статистики как roi=0.
--
-- detector_signals_resolve теперь пишет таким сигналам proxy WIN/LOSS с
-- outcome_proxy=true. Reconcile-фаза той же джобы перепроверяет proxy-строки,
-- когда markets.resolved становится true, и снимает флаг (или исправляет флип).
--
-- PG16: ADD COLUMN с constant DEFAULT — metadata-only, без rewrite таблицы.

ALTER TABLE public.detector_signals
    ADD COLUMN IF NOT EXISTS outcome_proxy boolean NOT NULL DEFAULT false;

-- Reconcile-выборка: только строки с активным proxy-флагом (их мало и они
-- временные — до реального резолва рынка). Частичный индекс держит выборку
-- дешёвой независимо от размера таблицы.
CREATE INDEX IF NOT EXISTS idx_detector_signals_outcome_proxy
    ON public.detector_signals (market_id)
    WHERE outcome_proxy = true;

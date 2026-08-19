-- 20260415120000_purge_pre_fix_dry_data.sql
-- Очистка dry-данных, открытых до 2026-04-13 (до фиксов скоринга/формулы).
--
-- Причина: до 13 апреля данные загрязнены:
--   - PnL формула NO-сторон сломана до 6 апреля (08d0fa1)
--   - News pipeline удалён 8 апреля (720ca93)
--   - Новые факторы (maker_direction, taker) добавлены 8 апреля (2ba6bfe)
--   - Microstructure factor удалён 10 апреля (5a46870)
--   - Price ticks удалён 10 апреля (72bf777)
--   - Pipeline починен 13 апреля (4ac3b36)
-- Данные нельзя сравнивать across time — очищаем, чтобы статистика считалась
-- только по чистым данным после всех фиксов.

BEGIN;

-- Дата отсечки: все позиции, ОТКРЫТЫЕ до 13 апреля 2026 00:00 UTC
-- Удаляем и closed, и open (open с мусорным entry oracle score бесполезны)

-- 1. Удалить ledger записи для позиций до отсечки (FK без CASCADE!)
DELETE FROM strategy_ledger
WHERE position_id IN (
    SELECT id FROM strategy_positions
    WHERE mode = 'dry'
      AND opened_at < '2026-04-13T00:00:00Z'
);

-- 2. Удалить сами позиции
DELETE FROM strategy_positions
WHERE mode = 'dry'
  AND opened_at < '2026-04-13T00:00:00Z';

-- 3. Обнулить dry_* метрики в strategy_state (пересчитаются при следующем strategy-monitor)
UPDATE strategy_state
SET dry_wr = NULL,
    dry_pnl = NULL,
    dry_trades = NULL,
    dry_pf = NULL,
    updated_at = NOW()
WHERE stage IN ('dry', 'disabled');

-- 4. Bankroll: НЕ трогаем initial_bankroll.
-- get_portfolio_bankroll() = initial + SUM(pnl) — после удаления старых позиций
-- SUM(pnl) автоматически пересчитается только по оставшимся (post-Apr-13).
-- Это корректно: bankroll "перезапускается" с текущего initial.

COMMIT;

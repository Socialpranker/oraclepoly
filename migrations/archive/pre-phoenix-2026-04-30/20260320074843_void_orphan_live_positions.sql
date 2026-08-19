-- Void orphan live_paper_positions для удалённой модели v2.4
-- v2.4 (Comments) удалена из MODELS_CONFIG, но 34 позиции остались в БД.
-- 20 open — никогда не получат новых сигналов, void чтобы не искажать статистику.

UPDATE live_paper_positions
SET status = 'void', pnl = 0, closed_at = now()
WHERE model = 'v2.4' AND status = 'open';

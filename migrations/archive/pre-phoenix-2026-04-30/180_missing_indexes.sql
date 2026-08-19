-- 180: Недостающие индексы (выявлены по анализу запросов в repositories)
--
-- Без CONCURRENTLY — совместимо с run_migration.py (implicit transaction).
-- На проде таблицы маленькие, блокировка будет мгновенной.

-- oracle_snapshots: WHERE outcome != 'pending' AND resolved_at >= $1
-- Запросы: signal attribution, error analysis, category breakdown, calibration — все фильтруют по resolved_at
CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_resolved
    ON oracle_snapshots(resolved_at DESC)
    WHERE outcome != 'pending';

-- live_paper_positions: WHERE status IN ('won','lost') AND closed_at >= $1
-- Запрос: daily P&L, models summary
CREATE INDEX IF NOT EXISTS idx_live_paper_status_closed
    ON live_paper_positions(status, closed_at DESC)
    WHERE status IN ('won', 'lost');

-- news: cleanup job — WHERE scraped_at < $1 AND id NOT IN (...)
CREATE INDEX IF NOT EXISTS idx_news_scraped_at
    ON news(scraped_at);

-- paper_positions: WHERE opened_at::date = CURRENT_DATE (daily spend)
-- Функциональный индекс — обычный индекс на opened_at не работает с ::date cast
CREATE INDEX IF NOT EXISTS idx_paper_positions_opened_date
    ON paper_positions((opened_at::date));

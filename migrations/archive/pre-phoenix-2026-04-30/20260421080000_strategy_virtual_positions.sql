-- Strategy Virtual Positions — shadow trading для узких стратегий (2026-04-21)
--
-- Цель: оценивать узкие discovery-подстратегии без блокировки реальных
-- позиций через has_any_open_position. Virtual pipeline:
--   1. Каждый matches-сигнал для shadow-стратегии → запись в эту таблицу
--   2. Cron strategy-virtual-resolve закрывает virtual позиции по resolved рынкам
--   3. strategy_validation для shadow читает статистику отсюда
--
-- Trade-offs (сознательные упрощения):
--   - Нет exit logic (trailing/TP) — virtual держится до resolve
--   - Нет slippage/fees — для относительного ranking это ок
--   - Outcome = из mv_market_resolved_outcome (SSoT)

CREATE TABLE IF NOT EXISTS strategy_virtual_positions (
    id              bigserial PRIMARY KEY,
    strategy_key    text NOT NULL REFERENCES strategy_state(strategy_key) ON DELETE CASCADE,
    market_id       uuid NOT NULL REFERENCES markets(id) ON DELETE CASCADE,
    event_id        uuid,
    side            text NOT NULL CHECK (side IN ('YES', 'NO')),
    entry_price     numeric(8,4) NOT NULL CHECK (entry_price > 0 AND entry_price < 1),
    signal_price    numeric(8,4),   -- raw YES price в момент сигнала
    oracle_score_at_entry numeric(6,4),
    divergence_at_entry   numeric(6,4),
    stake           numeric(10,2) NOT NULL DEFAULT 10.00,
    category        text,
    signal_source   text NOT NULL DEFAULT 'oracle',
    created_at      timestamptz NOT NULL DEFAULT now(),
    snapshot_date   date GENERATED ALWAYS AS ((created_at AT TIME ZONE 'UTC')::date) STORED,
    resolved_at     timestamptz,
    resolution_outcome text CHECK (resolution_outcome IN ('YES', 'NO', 'invalid')),
    pnl             numeric(10,2),
    status          text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'resolved', 'invalid'))
);

-- Уникальность: одна запись на (strategy, market, side, snapshot_date) —
-- защита от дубликатов при повторном запуске position_manager
CREATE UNIQUE INDEX IF NOT EXISTS ux_svp_daily_unique
    ON strategy_virtual_positions (strategy_key, market_id, side, snapshot_date);

-- Resolve cron: быстрый поиск открытых позиций по market_id
CREATE INDEX IF NOT EXISTS ix_svp_open_by_market
    ON strategy_virtual_positions (market_id, status) WHERE status = 'open';

-- Validation: статистика per стратегия
CREATE INDEX IF NOT EXISTS ix_svp_strategy_resolved
    ON strategy_virtual_positions (strategy_key, status, resolved_at DESC);

-- Добавляем 'shadow' в разрешённые stage для strategy_state
-- (stage text без CHECK на проде — просто документируем новое значение)
COMMENT ON TABLE strategy_virtual_positions IS
    'Shadow trading для узких discovery стратегий. Не связано с real bankroll.
     Stage=''shadow'' стратегии пишут сюда, минуя cross-strategy dedup.';

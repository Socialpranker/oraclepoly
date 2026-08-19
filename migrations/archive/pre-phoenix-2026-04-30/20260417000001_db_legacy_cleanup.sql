-- 20260417000001_db_legacy_cleanup.sql
-- Этап 1 чистки legacy: БД-слой.
--
-- Цели:
--   1. DROP таблицы которые должны были исчезнуть в migration 157 но снова появились.
--   2. DROP пустую materialized view top_analysts (refresh-функция уже удалена).
--   3. DROP gamification функции без callers в коде.
--   4. Синхронизировать триггер fn_create_bankroll_for_strategy с актуальным
--      _portfolio bankroll = $10000 (раньше захардкожено $500 — рассинхрон).
--
-- Все DROP идут под IF EXISTS — миграция идемпотентна.

BEGIN;

-- ═══════════════════════════════════════════════════════════════════════════
-- 1. Dead tables (повтор migration 157, которая не сработала или была обойдена)
-- ═══════════════════════════════════════════════════════════════════════════

-- wallet_mappings: ProxyWalletService так и не реализован, нет SELECT/INSERT в app/.
DROP TABLE IF EXISTS wallet_mappings CASCADE;

-- black_swan_scenarios: agent black_swan.py удалён, нет INSERT нигде в коде.
-- Триггер trg_black_swan_scenarios_notify уйдёт через CASCADE.
DROP TABLE IF EXISTS black_swan_scenarios CASCADE;

-- daily_challenge_claims: orphan от gamification, родительская daily_challenges
-- уже дропнута в migration 20260410171928_phase_e_gamification_and_cwi_cleanup.
DROP TABLE IF EXISTS daily_challenge_claims CASCADE;

-- ═══════════════════════════════════════════════════════════════════════════
-- 2. Dead materialized view
-- ═══════════════════════════════════════════════════════════════════════════

-- top_analysts: refresh_top_analysts() была удалена в migration 20260410171928,
-- view никогда не обновляется и не читается из app/.
DROP MATERIALIZED VIEW IF EXISTS top_analysts CASCADE;

-- ═══════════════════════════════════════════════════════════════════════════
-- 3. Dead PG functions без callers
-- ═══════════════════════════════════════════════════════════════════════════

-- Аудит показал что эти функции не имеют ни одного caller в app/, scripts/, client/.
-- Все они — наследие отменённых фич геймификации.

DROP FUNCTION IF EXISTS claim_daily_mission_reward(uuid, integer, integer);
DROP FUNCTION IF EXISTS check_achievements(uuid);
DROP FUNCTION IF EXISTS get_leaderboard_by_period(text, integer, integer);
DROP FUNCTION IF EXISTS get_category_leaderboard(text, text, integer, integer);

-- check_daily_challenges и claim_daily_challenge_bonus: phase_e migration пыталась
-- их дропнуть, но указала неверную сигнатуру — функции остались. Удаляем правильно.
DROP FUNCTION IF EXISTS check_daily_challenges(uuid, text, jsonb);
DROP FUNCTION IF EXISTS claim_daily_challenge_bonus(uuid);

-- ═══════════════════════════════════════════════════════════════════════════
-- 4. Bankroll trigger sync: $500 → $10000 (актуальный _portfolio default)
-- ═══════════════════════════════════════════════════════════════════════════

-- Контекст: миграция 20260416000003_wipe_dry_and_unify_bankroll.sql выставила
-- bankroll всех dry-стратегий до $10000, но триггер fn_create_bankroll_for_strategy
-- остался со старым $500. Это создавало рассинхрон при INSERT новых стратегий
-- (видно на exp_vwap_edge / exp_no_midzone / exp_temporal_sports — получили $500).
--
-- Фактически per-strategy bankroll НЕ используется для sizing (SSoT — _portfolio
-- через cron_quota_refresh), но рассинхрон путает чтение БД и mental model.

CREATE OR REPLACE FUNCTION fn_create_bankroll_for_strategy()
RETURNS TRIGGER AS $$
BEGIN
  IF NEW.stage IN ('dry', 'backtest') THEN
    INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
    VALUES (NEW.strategy_key, 10000.0, 10000.0, 250.0, 0.10)
    ON CONFLICT (strategy_key) DO NOTHING;
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Аналогично для fn_handle_bankroll_on_stage_change (live → paper откат)
CREATE OR REPLACE FUNCTION fn_handle_bankroll_on_stage_change()
RETURNS TRIGGER AS $$
BEGIN
  IF OLD.stage != 'live' AND NEW.stage = 'live' THEN
    UPDATE strategy_bankroll SET stage = 'live' WHERE strategy_key = NEW.strategy_key;
  END IF;
  IF OLD.stage = 'live' AND NEW.stage IN ('paper', 'backtest', 'dry') THEN
    INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate, stage)
    VALUES (NEW.strategy_key, 10000.0, 10000.0, 250.0, 0.10, 'dry')
    ON CONFLICT (strategy_key) DO UPDATE SET stage = 'dry';
  END IF;
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Backfill: уже созданные exp_* стратегии получили $500 — выровнять.
UPDATE strategy_bankroll
SET bankroll = 10000.0, initial_bankroll = 10000.0
WHERE strategy_key LIKE 'exp_%'
  AND initial_bankroll = 500.0;

-- Заодно: column default тоже синхронизируем для семантической чистоты.
ALTER TABLE strategy_bankroll
  ALTER COLUMN bankroll SET DEFAULT 10000,
  ALTER COLUMN initial_bankroll SET DEFAULT 10000;

COMMIT;

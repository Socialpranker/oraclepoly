-- ============================================================================
-- PHOENIX SCHEMA — Self-Trading Bot (2026-04-30)
-- ============================================================================
--
-- Snapshot полной схемы PostgreSQL после strip-down из B2C → self-trading bot.
-- Создан pg_dump --schema-only, постпроцесс: IF NOT EXISTS / OR REPLACE.
--
-- ИСТОРИЯ: 173 миграции (2025-2026) → архив migrations/archive/pre-phoenix-2026-04-30/
-- На проде (example.com): зарегистрирован в schema_migrations, не выполняется
--                            (вся схема уже существует).
-- На fresh dev: выполняется при первом run_migration.py — создаёт всю схему.
--
-- УДАЛЕНО при переходе:
--   - 13 user-facing таблиц (users, payments, subscriptions, notifications, votes...)
--   - 14 legacy функций (referrals, gamification, votes, credits, payments)
--   - schema auth (Supabase legacy)
--   - schema cleanup_backup_20260424_live
--   - notify_table_change trigger (WebSocket broadcast убран)
--
-- ОСТАЛОСЬ (38 таблиц):
--   Trading: execution_orders, strategy_*, transactions, whales, smart_money_*
--   Markets: markets, oracle_snapshots, market_signals, market_comments, retail_trades
--   ML: ml_models, ml_predictions, trade_features
--   Calibration: calibration_configs, scoring_configs, bin_reliability
--   News: news, news_market_mappings
--   Misc: events, feature_store, daily_benchmarks, weather_*, etl_runs
--
-- ============================================================================

--
-- PostgreSQL database dump
--


-- Dumped from database version 16.13
-- Dumped by pg_dump version 16.13

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--

CREATE SCHEMA public;


--
-- Name: fn_create_bankroll_for_strategy(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE OR REPLACE FUNCTION public.fn_create_bankroll_for_strategy() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
  IF NEW.stage IN ('dry', 'backtest') THEN
    INSERT INTO strategy_bankroll (strategy_key, bankroll, initial_bankroll, daily_limit, reinvest_rate)
    VALUES (NEW.strategy_key, 10000.0, 10000.0, 250.0, 0.10)
    ON CONFLICT (strategy_key) DO NOTHING;
  END IF;
  RETURN NEW;
END;
$$;


--
-- Name: fn_handle_bankroll_on_stage_change(); Type: FUNCTION; Schema: public; Owner: -
--

CREATE OR REPLACE FUNCTION public.fn_handle_bankroll_on_stage_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
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
$$;


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: bin_reliability; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.bin_reliability (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    bin_start numeric(3,2) NOT NULL,
    bin_end numeric(3,2) NOT NULL,
    bin_label character varying(10) NOT NULL,
    sample_count integer DEFAULT 0 NOT NULL,
    avg_predicted numeric(6,4),
    actual_win_rate numeric(6,4),
    gap numeric(6,4),
    abs_gap numeric(6,4),
    is_whitelisted boolean DEFAULT false NOT NULL,
    confidence_lower numeric(6,4),
    confidence_upper numeric(6,4),
    computed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: calibration_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.calibration_configs (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    category text DEFAULT 'global'::text NOT NULL,
    w_mkt numeric(6,4) DEFAULT 1.0 NOT NULL,
    w_whale numeric(6,4) DEFAULT 1.0 NOT NULL,
    w_news numeric(6,4) DEFAULT 0.8 NOT NULL,
    temperature numeric(6,4) DEFAULT 1.0 NOT NULL,
    is_active boolean DEFAULT false NOT NULL,
    source text DEFAULT 'manual'::text NOT NULL,
    brier_score_before numeric(8,6),
    brier_score_after numeric(8,6),
    log_loss_before numeric(8,6),
    log_loss_after numeric(8,6),
    sample_count integer DEFAULT 0,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    w_fresh numeric(6,4) DEFAULT 0.6,
    w_cluster numeric(6,4) DEFAULT 0.7,
    w_registry numeric(6,4) DEFAULT 0.3,
    w_oi numeric(6,4) DEFAULT 0.4,
    w_concentration numeric(6,4) DEFAULT 0.3,
    w_micro numeric(6,4) DEFAULT 0.35,
    beta_a numeric(6,4) DEFAULT 1.0,
    beta_b numeric(6,4) DEFAULT 0.0,
    calibration_method text DEFAULT 'temperature'::text,
    w_whale_contra numeric DEFAULT 0,
    w_retail numeric(6,4) DEFAULT 0.0,
    w_maker_direction numeric(6,4) DEFAULT 0.40,
    w_taker_imbalance numeric(6,4) DEFAULT 0.15,
    w_sports_odds numeric(6,4) DEFAULT 0.50
);


--
-- Name: daily_benchmarks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.daily_benchmarks (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    date date NOT NULL,
    total_resolved integer DEFAULT 0 NOT NULL,
    accuracy numeric(6,4) DEFAULT 0 NOT NULL,
    avg_brier_score numeric(6,4) DEFAULT 0 NOT NULL,
    pending_count integer DEFAULT 0 NOT NULL,
    total_snapshots integer DEFAULT 0 NOT NULL,
    accuracy_1d numeric(6,4),
    accuracy_3d numeric(6,4),
    accuracy_7d numeric(6,4),
    accuracy_14d numeric(6,4),
    accuracy_30d numeric(6,4),
    brier_1d numeric(6,4),
    brier_3d numeric(6,4),
    brier_7d numeric(6,4),
    brier_14d numeric(6,4),
    brier_30d numeric(6,4),
    resolved_1d integer DEFAULT 0,
    resolved_3d integer DEFAULT 0,
    resolved_7d integer DEFAULT 0,
    resolved_14d integer DEFAULT 0,
    resolved_30d integer DEFAULT 0,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    bss double precision,
    bss_7d double precision,
    bss_14d double precision,
    bss_30d double precision,
    murphy_reliability double precision,
    murphy_resolution double precision,
    avg_ttr_hours numeric(10,2),
    median_ttr_hours numeric(10,2),
    p75_ttr_hours numeric(10,2)
);


--
-- Name: etl_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.etl_runs (
    id bigint NOT NULL,
    job_name text NOT NULL,
    started_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    status text NOT NULL,
    rows_processed integer DEFAULT 0,
    rows_inserted integer DEFAULT 0,
    rows_updated integer DEFAULT 0,
    error_message text,
    metadata jsonb DEFAULT '{}'::jsonb,
    CONSTRAINT etl_runs_status_check CHECK ((status = ANY (ARRAY['running'::text, 'success'::text, 'failed'::text])))
);


--
-- Name: etl_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.etl_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: etl_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.etl_runs_id_seq OWNED BY public.etl_runs.id;


--
-- Name: event_arbitrage_scans; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.event_arbitrage_scans (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    event_id uuid NOT NULL,
    scanned_at timestamp with time zone DEFAULT now() NOT NULL,
    markets_count integer NOT NULL,
    sum_prices numeric(8,6) NOT NULL,
    deviation numeric(8,6) NOT NULL,
    max_spread numeric(8,6),
    outcomes_detail jsonb DEFAULT '[]'::jsonb NOT NULL,
    potential_profit_pct numeric(8,4)
);


--
-- Name: events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.events (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    polymarket_id text NOT NULL,
    title text NOT NULL,
    slug text,
    description text,
    category text DEFAULT 'World & Economy'::text NOT NULL,
    image_url text,
    volume numeric(20,6) DEFAULT 0,
    volume_24h numeric(20,6) DEFAULT 0,
    liquidity numeric(20,6) DEFAULT 0,
    start_date timestamp with time zone,
    end_date timestamp with time zone,
    is_active boolean DEFAULT true,
    markets_count integer DEFAULT 0,
    tags jsonb DEFAULT '[]'::jsonb,
    competitive numeric(5,4),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now()
);


--
-- Name: execution_orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.execution_orders (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    strategy_key text NOT NULL,
    market_id text NOT NULL,
    event_id text,
    side text NOT NULL,
    execution_mode text NOT NULL,
    signal_price numeric(8,4) NOT NULL,
    oracle_score numeric(6,4),
    divergence numeric(6,4),
    order_id text,
    order_type text NOT NULL,
    limit_price numeric(8,4),
    stake numeric(10,2) NOT NULL,
    fill_price numeric(8,4),
    filled_qty numeric(10,2),
    slippage numeric(6,4),
    fee_paid numeric(8,4),
    status text DEFAULT 'pending'::text NOT NULL,
    pnl numeric(10,2),
    outcome text,
    created_at timestamp with time zone DEFAULT now(),
    placed_at timestamp with time zone,
    filled_at timestamp with time zone,
    cancelled_at timestamp with time zone,
    settled_at timestamp with time zone,
    cancel_reason text,
    meta jsonb DEFAULT '{}'::jsonb
);


--
-- Name: feature_store; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.feature_store (
    id bigint NOT NULL,
    market_id text NOT NULL,
    condition_id text,
    event_id uuid,
    snapshot_at timestamp with time zone NOT NULL,
    side text NOT NULL,
    category text,
    question text,
    horizon_days numeric,
    market_price numeric,
    entry_price numeric,
    vwap_7d numeric,
    volume_24h numeric,
    maker_direction_raw numeric,
    taker_imbalance_raw numeric,
    whale_tx_count integer DEFAULT 0,
    whale_contribution_raw numeric,
    news_contribution_raw numeric,
    sm_fresh_raw numeric,
    sm_cluster_raw numeric,
    sm_registry_raw numeric,
    oi_factor_raw numeric,
    concentration_factor_raw numeric,
    micro_factor_raw numeric,
    oracle_score_prod numeric,
    oracle_score_experimental numeric,
    divergence_prod numeric,
    divergence_experimental numeric,
    z_posterior_prod numeric,
    z_posterior_experimental numeric,
    config_hash_prod text,
    config_hash_experimental text,
    components_available text[] DEFAULT '{}'::text[] NOT NULL,
    outcome text,
    win integer,
    resolved_at timestamp with time zone,
    source text NOT NULL,
    features_version text DEFAULT '1.0'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT feature_store_side_check CHECK ((side = ANY (ARRAY['YES'::text, 'NO'::text]))),
    CONSTRAINT feature_store_source_check CHECK ((source = ANY (ARRAY['hf'::text, 'live'::text, 'hf_enriched'::text]))),
    CONSTRAINT feature_store_win_check CHECK ((win = ANY (ARRAY[0, 1])))
);


--
-- Name: feature_store_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.feature_store_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: feature_store_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.feature_store_id_seq OWNED BY public.feature_store.id;


--
-- Name: funding_chains; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.funding_chains (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    target_address text NOT NULL,
    funder_address text NOT NULL,
    chain_depth integer DEFAULT 1,
    amount numeric DEFAULT 0,
    detected_at timestamp with time zone DEFAULT now()
);


--
-- Name: maker_direction_cache; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.maker_direction_cache (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    market_id uuid NOT NULL,
    condition_id text NOT NULL,
    maker_direction_ratio numeric(6,4) NOT NULL,
    depth_imbalance numeric(6,4),
    bid_depth_usd numeric(16,2),
    ask_depth_usd numeric(16,2),
    maker_count integer DEFAULT 0,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT maker_direction_ratio_range CHECK (((maker_direction_ratio >= '-1.0'::numeric) AND (maker_direction_ratio <= 1.0)))
);


--
-- Name: market_analyses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.market_analyses (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    market_id uuid NOT NULL,
    oracle_score_snapshot numeric(5,4),
    price_snapshot numeric(10,6),
    computed_analytics jsonb NOT NULL,
    scenarios jsonb,
    report jsonb,
    model_used text DEFAULT 'gpt-4o-mini'::text,
    generated_at timestamp with time zone DEFAULT now(),
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: market_comments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.market_comments (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    market_id uuid NOT NULL,
    comment_id text NOT NULL,
    author_address text,
    body text NOT NULL,
    is_holder boolean DEFAULT false,
    sentiment_score numeric(4,3),
    created_at timestamp with time zone NOT NULL,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: market_signals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.market_signals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    market_id uuid NOT NULL,
    condition_id text NOT NULL,
    taker_buy_volume_usd numeric(20,6) DEFAULT 0,
    taker_sell_volume_usd numeric(20,6) DEFAULT 0,
    taker_imbalance numeric(6,4),
    total_trade_count integer DEFAULT 0,
    whale_trade_count integer DEFAULT 0,
    avg_trade_size_usd numeric(16,2),
    data_source text DEFAULT 'historical'::text NOT NULL,
    period_start timestamp with time zone,
    period_end timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT taker_imbalance_range CHECK (((taker_imbalance >= '-1.0'::numeric) AND (taker_imbalance <= 1.0)))
);


--
-- Name: markets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.markets (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    polymarket_id text NOT NULL,
    question text NOT NULL,
    description text,
    category text,
    icon_url text,
    current_price numeric(10,6),
    oracle_score numeric(5,4),
    sentiment_score numeric(5,4),
    whale_confidence numeric(5,4),
    risk_score numeric(5,4),
    volume_24h numeric(20,6),
    end_date timestamp with time zone,
    is_active boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    horizon text DEFAULT 'mid'::text,
    clob_token_ids jsonb DEFAULT '[]'::jsonb,
    token_outcomes jsonb DEFAULT '{}'::jsonb,
    previous_oracle_score numeric(5,4),
    condition_id text,
    event_id uuid,
    slug text,
    score_decomposition jsonb,
    cwi_score numeric(5,4) DEFAULT NULL::numeric,
    cwi_regime text,
    resolved boolean DEFAULT false,
    winner text,
    bookmaker_probability numeric(5,4),
    bookmaker_updated_at timestamp with time zone,
    CONSTRAINT markets_horizon_check CHECK ((horizon = ANY (ARRAY['fast'::text, 'mid'::text, 'long'::text])))
);


--
-- Name: ml_models; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.ml_models (
    version text NOT NULL,
    feature_version text NOT NULL,
    trained_at timestamp with time zone DEFAULT now() NOT NULL,
    train_period_start timestamp with time zone NOT NULL,
    train_period_end timestamp with time zone NOT NULL,
    train_n integer NOT NULL,
    test_n integer NOT NULL,
    model_type text DEFAULT 'lightgbm'::text NOT NULL,
    hyperparams jsonb DEFAULT '{}'::jsonb NOT NULL,
    test_brier numeric(10,5),
    test_logloss numeric(10,5),
    test_auc numeric(8,4),
    test_ece numeric(10,5),
    test_no_roi_pct numeric(8,2),
    test_no_sharpe numeric(8,3),
    metrics_full jsonb,
    artifact_path text,
    feature_importance jsonb,
    is_active boolean DEFAULT false NOT NULL,
    deployed_at timestamp with time zone,
    retired_at timestamp with time zone,
    notes text
);


--
-- Name: ml_predictions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.ml_predictions (
    market_id uuid NOT NULL,
    snapshot_at timestamp with time zone NOT NULL,
    model_version text NOT NULL,
    p_yes numeric(8,4) NOT NULL,
    confidence numeric(8,4),
    contributors jsonb,
    market_price_at_snapshot numeric(8,4),
    divergence numeric(8,4),
    outcome smallint,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT ml_predictions_p_yes_range CHECK (((p_yes >= (0)::numeric) AND (p_yes <= (1)::numeric)))
);


--
-- Name: oracle_snapshots; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.oracle_snapshots (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    market_id uuid NOT NULL,
    oracle_score numeric(5,4) NOT NULL,
    sentiment_score numeric(5,4),
    whale_confidence numeric(5,4),
    risk_score numeric(5,4),
    news_impact_correction numeric(7,4) DEFAULT 0,
    market_price numeric(5,4) NOT NULL,
    volume_24h numeric,
    news_count integer DEFAULT 0,
    whale_tx_count integer DEFAULT 0,
    black_swan_count integer DEFAULT 0,
    divergence numeric(7,4),
    divergence_signal text,
    outcome text DEFAULT 'pending'::text,
    final_price numeric(5,4),
    resolved_at timestamp with time zone,
    prediction_correct boolean,
    brier_score numeric(7,4),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    whale_contribution_raw numeric(10,6),
    news_contribution_raw numeric(10,6),
    z_posterior numeric(10,6),
    oi_factor_raw numeric(10,6),
    concentration_factor_raw numeric(10,6),
    micro_factor_raw numeric(10,6),
    sm_fresh_raw numeric(10,6),
    sm_cluster_raw numeric(10,6),
    sm_registry_raw numeric(10,6),
    category text,
    end_date timestamp with time zone,
    sports_odds_raw numeric(7,4) DEFAULT 0,
    retail_factor_raw numeric(10,6) DEFAULT 0,
    maker_direction_raw numeric(5,4) DEFAULT 0,
    taker_imbalance_raw numeric(5,4) DEFAULT 0,
    CONSTRAINT oracle_snapshots_divergence_signal_check CHECK (((divergence_signal IS NULL) OR (divergence_signal = ANY (ARRAY['fair'::text, 'underpriced'::text, 'overpriced'::text, 'moderate_overpriced'::text, 'moderate_underpriced'::text, 'strong_overpriced'::text, 'strong_underpriced'::text])))),
    CONSTRAINT oracle_snapshots_outcome_check CHECK ((outcome = ANY (ARRAY['pending'::text, 'yes'::text, 'no'::text, 'void'::text])))
)
WITH (autovacuum_vacuum_scale_factor='0.02', autovacuum_vacuum_threshold='5000', autovacuum_analyze_scale_factor='0.05');


--
-- Name: mv_market_resolved_outcome; Type: MATERIALIZED VIEW; Schema: public; Owner: -
--

CREATE MATERIALIZED VIEW IF NOT EXISTS public.mv_market_resolved_outcome AS
 SELECT DISTINCT ON (market_id) market_id,
    outcome,
    resolved_at
   FROM public.oracle_snapshots
  WHERE (outcome <> 'pending'::text)
  ORDER BY market_id, resolved_at DESC
  WITH NO DATA;


--
-- Name: news; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.news (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    source text NOT NULL,
    title text NOT NULL,
    url text NOT NULL,
    summary text,
    sentiment_score numeric(5,4),
    r2_archive_key text,
    qdrant_point_id text,
    published_at timestamp with time zone,
    scraped_at timestamp with time zone DEFAULT now(),
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: news_market_mappings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.news_market_mappings (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    news_id uuid,
    market_id uuid,
    confidence_score numeric(5,4) NOT NULL,
    is_valid boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    impact_direction text,
    impact_magnitude numeric(5,4),
    probability_shift numeric(5,4),
    impact_confidence numeric(5,4),
    impact_reasoning text,
    key_factors text[],
    analyzed_at timestamp with time zone,
    temporal_weight numeric(4,2) DEFAULT 1.0,
    CONSTRAINT news_market_mappings_impact_direction_check CHECK ((impact_direction = ANY (ARRAY['YES'::text, 'NO'::text, 'NEUTRAL'::text])))
);


--
-- Name: objective_signals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.objective_signals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    market_id text NOT NULL,
    question text NOT NULL,
    category text DEFAULT 'weather'::text NOT NULL,
    city text,
    airport_station text,
    temp_threshold numeric(5,1),
    target_date date,
    market_price numeric(8,4) NOT NULL,
    meteo_prob numeric(8,4) NOT NULL,
    divergence numeric(8,4) NOT NULL,
    metar_temp_c numeric(5,1),
    forecast_temp_c numeric(5,1),
    resolution_source text,
    side text NOT NULL,
    stake numeric(8,2),
    enqueued boolean DEFAULT false NOT NULL,
    signal_date date DEFAULT CURRENT_DATE NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: oracle_recalc_timings; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.oracle_recalc_timings (
    id bigint NOT NULL,
    run_id uuid NOT NULL,
    market_id uuid,
    step text NOT NULL,
    duration_ms numeric(10,2) NOT NULL,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: oracle_recalc_timings_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.oracle_recalc_timings_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: oracle_recalc_timings_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.oracle_recalc_timings_id_seq OWNED BY public.oracle_recalc_timings.id;


--
-- Name: retail_trades; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.retail_trades (
    id bigint NOT NULL,
    trade_id text NOT NULL,
    condition_id text NOT NULL,
    maker text NOT NULL,
    taker text NOT NULL,
    side text NOT NULL,
    outcome text NOT NULL,
    price numeric(10,6) NOT NULL,
    amount numeric(20,6) NOT NULL,
    token_amount numeric(20,6),
    block_number bigint NOT NULL,
    block_timestamp timestamp with time zone NOT NULL,
    is_retail boolean DEFAULT true,
    created_at timestamp with time zone DEFAULT now(),
    CONSTRAINT retail_trades_outcome_check CHECK ((outcome = ANY (ARRAY['YES'::text, 'NO'::text]))),
    CONSTRAINT retail_trades_side_check CHECK ((side = ANY (ARRAY['buy'::text, 'sell'::text])))
);


--
-- Name: retail_trades_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.retail_trades_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: retail_trades_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.retail_trades_id_seq OWNED BY public.retail_trades.id;


--
-- Name: schema_migrations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.schema_migrations_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: scoring_configs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.scoring_configs (
    hash text NOT NULL,
    label text NOT NULL,
    weights jsonb NOT NULL,
    formula_flags jsonb DEFAULT '{}'::jsonb NOT NULL,
    calibration_source text DEFAULT 'global'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    promoted_to_prod_at timestamp with time zone,
    notes text
);


--
-- Name: smart_money_wallets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.smart_money_wallets (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    address text NOT NULL,
    label text,
    source text DEFAULT 'seed'::text,
    specialization text[] DEFAULT '{}'::text[],
    total_pnl numeric DEFAULT 0,
    is_active boolean DEFAULT true,
    last_synced_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    wallet_score integer DEFAULT 0,
    behavior_type text DEFAULT 'unknown'::text,
    wr_crypto numeric DEFAULT 0,
    wr_sports numeric DEFAULT 0,
    wr_politics numeric DEFAULT 0
);


--
-- Name: strategy_audit; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.strategy_audit (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    strategy_key text NOT NULL,
    event text NOT NULL,
    details jsonb,
    created_at timestamp with time zone DEFAULT now()
);


--
-- Name: strategy_bankroll; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.strategy_bankroll (
    strategy_key text NOT NULL,
    bankroll numeric(12,2) DEFAULT 10000 NOT NULL,
    initial_bankroll numeric(12,2) DEFAULT 10000 NOT NULL,
    total_withdrawn numeric(12,2) DEFAULT 0 NOT NULL,
    total_reinvested numeric(12,2) DEFAULT 0 NOT NULL,
    daily_limit numeric(10,2) DEFAULT 500 NOT NULL,
    reinvest_rate numeric(4,2) DEFAULT 0.10 NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    stage text DEFAULT 'dry'::text NOT NULL
);


--
-- Name: strategy_discovery_runs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.strategy_discovery_runs (
    id bigint NOT NULL,
    run_at timestamp with time zone DEFAULT now() NOT NULL,
    data_source text NOT NULL,
    fs_source text,
    total_combos integer NOT NULL,
    passed_filter integer NOT NULL,
    confirmed integer DEFAULT 0 NOT NULL,
    auto_registered integer DEFAULT 0 NOT NULL,
    data_window_from timestamp with time zone,
    data_window_to timestamp with time zone,
    rows_loaded integer,
    meta jsonb
);


--
-- Name: strategy_discovery_runs_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strategy_discovery_runs_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: strategy_discovery_runs_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strategy_discovery_runs_id_seq OWNED BY public.strategy_discovery_runs.id;


--
-- Name: strategy_ledger; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.strategy_ledger (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    strategy_key text NOT NULL,
    date date DEFAULT CURRENT_DATE NOT NULL,
    event_type text NOT NULL,
    position_id uuid,
    amount numeric(10,2) NOT NULL,
    balance_after numeric(12,2) NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: strategy_live_metrics; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.strategy_live_metrics (
    strategy_key text NOT NULL,
    live_resolved integer DEFAULT 0 NOT NULL,
    live_wins integer DEFAULT 0 NOT NULL,
    live_losses integer DEFAULT 0 NOT NULL,
    live_total_stake numeric(12,2) DEFAULT 0 NOT NULL,
    live_total_pnl numeric(12,2) DEFAULT 0 NOT NULL,
    live_gross_win numeric(12,2) DEFAULT 0 NOT NULL,
    live_gross_loss numeric(12,2) DEFAULT 0 NOT NULL,
    avg_win numeric(10,4),
    avg_loss numeric(10,4),
    wr_30d numeric(5,4),
    roi_30d numeric(6,4),
    drawdown_pct numeric(5,4) DEFAULT 0 NOT NULL,
    peak_pnl numeric(12,2) DEFAULT 0 NOT NULL,
    current_stake numeric(8,2) DEFAULT 5.0 NOT NULL,
    kelly_fraction numeric(6,4),
    safety_mult numeric(4,3) DEFAULT 0.250 NOT NULL,
    damping numeric(4,3) DEFAULT 0.0 NOT NULL,
    paused boolean DEFAULT false NOT NULL,
    paused_reason text,
    paused_at timestamp with time zone,
    dry_resolved_at_start integer DEFAULT 0 NOT NULL,
    dry_wr_at_start numeric(5,4),
    dry_roi_at_start numeric(6,4),
    last_settled_at timestamp with time zone,
    last_recomputed_at timestamp with time zone,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: strategy_positions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.strategy_positions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    strategy_key text NOT NULL,
    market_id uuid NOT NULL,
    event_id uuid,
    side text NOT NULL,
    entry_price numeric(8,4) NOT NULL,
    oracle_score_at_entry numeric(6,4),
    divergence_at_entry numeric(6,4),
    stake numeric(10,2) NOT NULL,
    category text,
    question text,
    meta jsonb DEFAULT '{}'::jsonb,
    status text DEFAULT 'open'::text NOT NULL,
    pnl numeric(10,2),
    opened_at timestamp with time zone DEFAULT now() NOT NULL,
    closed_at timestamp with time zone,
    snapshot_date date DEFAULT CURRENT_DATE NOT NULL,
    mode text DEFAULT 'dry'::text NOT NULL,
    signal_source text DEFAULT 'oracle'::text NOT NULL
);


--
-- Name: strategy_state; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.strategy_state (
    strategy_key text NOT NULL,
    stage text DEFAULT 'backtest'::text NOT NULL,
    promoted_at timestamp with time zone,
    disabled_reason text,
    backtest_wr numeric(5,2),
    backtest_pnl numeric(10,2),
    backtest_trades integer,
    backtest_pf numeric(6,2),
    backtest_max_dd numeric(5,2),
    backtest_at timestamp with time zone,
    dry_wr numeric(5,2),
    dry_pnl numeric(10,2),
    dry_trades integer,
    dry_pf numeric(6,2),
    live_wr numeric(5,2),
    live_pnl numeric(10,2),
    live_trades integer,
    updated_at timestamp with time zone DEFAULT now(),
    filters jsonb,
    source text DEFAULT 'code'::text,
    signal_source text DEFAULT 'oracle'::text NOT NULL,
    discovery_run_id bigint,
    discovery_metrics jsonb
);


--
-- Name: strategy_validations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.strategy_validations (
    id bigint NOT NULL,
    strategy_key text NOT NULL,
    validated_at timestamp with time zone DEFAULT now() NOT NULL,
    source text NOT NULL,
    total_bets integer NOT NULL,
    roi_pct numeric(8,2) NOT NULL,
    sharpe numeric(6,3),
    max_dd_pct numeric(6,2),
    win_rate numeric(5,2),
    ci_95_roi_low numeric(8,2),
    ci_95_roi_high numeric(8,2),
    components_used text[],
    verdict text NOT NULL,
    issues text[] DEFAULT '{}'::text[],
    raw_result jsonb
);


--
-- Name: strategy_validations_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strategy_validations_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: strategy_validations_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strategy_validations_id_seq OWNED BY public.strategy_validations.id;


--
-- Name: strategy_virtual_positions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.strategy_virtual_positions (
    id bigint NOT NULL,
    strategy_key text NOT NULL,
    market_id uuid NOT NULL,
    event_id uuid,
    side text NOT NULL,
    entry_price numeric(8,4) NOT NULL,
    signal_price numeric(8,4),
    oracle_score_at_entry numeric(6,4),
    divergence_at_entry numeric(6,4),
    stake numeric(10,2) DEFAULT 10.00 NOT NULL,
    category text,
    signal_source text DEFAULT 'oracle'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    snapshot_date date GENERATED ALWAYS AS (((created_at AT TIME ZONE 'UTC'::text))::date) STORED,
    resolved_at timestamp with time zone,
    resolution_outcome text,
    pnl numeric(10,2),
    status text DEFAULT 'open'::text NOT NULL,
    CONSTRAINT strategy_virtual_positions_entry_price_check CHECK (((entry_price > (0)::numeric) AND (entry_price < (1)::numeric))),
    CONSTRAINT strategy_virtual_positions_resolution_outcome_check CHECK ((resolution_outcome = ANY (ARRAY['YES'::text, 'NO'::text, 'invalid'::text]))),
    CONSTRAINT strategy_virtual_positions_side_check CHECK ((side = ANY (ARRAY['YES'::text, 'NO'::text]))),
    CONSTRAINT strategy_virtual_positions_status_check CHECK ((status = ANY (ARRAY['open'::text, 'resolved'::text, 'invalid'::text])))
);


--
-- Name: strategy_virtual_positions_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.strategy_virtual_positions_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: strategy_virtual_positions_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.strategy_virtual_positions_id_seq OWNED BY public.strategy_virtual_positions.id;


--
-- Name: trade_features; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.trade_features (
    market_id uuid NOT NULL,
    snapshot_at timestamp with time zone NOT NULL,
    market_price numeric(8,4) NOT NULL,
    volume_24h numeric(14,2),
    horizon_days numeric(8,2),
    category text,
    spread_bps numeric(10,2),
    book_depth_usd numeric(14,2),
    taker_imbalance_30m numeric(8,4),
    vpin_60m numeric(8,4),
    liquidity_decay_3h numeric(8,4),
    whale_buy_volume_24h numeric(14,2),
    whale_sell_volume_24h numeric(14,2),
    whale_unique_count_24h integer,
    sm_cluster_buy_pct numeric(8,4),
    sm_pre_resolution_drift numeric(8,4),
    event_market_count integer,
    event_total_volume numeric(14,2),
    outcome smallint,
    resolved_at timestamp with time zone,
    feature_version text DEFAULT 'v1'::text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    CONSTRAINT trade_features_outcome_chk CHECK (((outcome = ANY (ARRAY['-1'::integer, 0, 1])) OR (outcome IS NULL)))
);


--
-- Name: trade_signals; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.trade_signals (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    strategy_key text NOT NULL,
    market_id uuid NOT NULL,
    event_id uuid,
    side text NOT NULL,
    entry_price numeric(8,4) NOT NULL,
    oracle_score numeric(6,4) NOT NULL,
    market_price numeric(8,4) NOT NULL,
    divergence numeric(6,4) NOT NULL,
    ev numeric(8,4),
    whale_contribution_raw numeric(8,4),
    sm_registry_raw numeric(8,4),
    whale_tx_count integer,
    micro_factor_raw numeric(8,4),
    z_posterior numeric(8,4),
    news_contribution_raw numeric(8,4),
    volume_24h numeric(14,2),
    category text,
    end_date timestamp with time zone,
    tier integer,
    expected_accuracy numeric(6,4),
    question text,
    snapshot_date date DEFAULT CURRENT_DATE NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    signal_source text DEFAULT 'oracle'::text NOT NULL,
    liquidity_meta jsonb DEFAULT '{}'::jsonb NOT NULL
);


--
-- Name: transactions; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.transactions (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    tx_hash text NOT NULL,
    whale_id uuid,
    market_id uuid,
    direction text,
    outcome text,
    amount numeric(20,6) NOT NULL,
    r2_archive_key text,
    block_number bigint,
    block_timestamp timestamp with time zone,
    created_at timestamp with time zone DEFAULT now(),
    is_whale boolean DEFAULT false,
    log_index integer DEFAULT 0,
    from_address text,
    to_address text,
    asset text,
    price numeric(10,6),
    market_title text,
    market_slug text,
    timing_score numeric(5,4),
    is_arbitrage boolean DEFAULT false,
    is_taker boolean,
    CONSTRAINT transactions_direction_check CHECK ((direction = ANY (ARRAY['buy'::text, 'sell'::text])))
);


--
-- Name: wallet_clusters; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.wallet_clusters (
    id uuid DEFAULT public.uuid_generate_v4() NOT NULL,
    cluster_label text,
    detection_method text DEFAULT 'dbscan_temporal'::text,
    wallet_addresses text[] DEFAULT '{}'::text[],
    total_volume numeric DEFAULT 0,
    coordination_score numeric(5,4) DEFAULT 0,
    detected_at timestamp with time zone DEFAULT now(),
    bf_temporal numeric(8,4) DEFAULT 1.0,
    bf_entropy numeric(8,4) DEFAULT 1.0,
    bf_position numeric(8,4) DEFAULT 1.0,
    bf_graph numeric(8,4) DEFAULT 1.0,
    market_ids text[] DEFAULT '{}'::text[],
    is_active boolean DEFAULT true,
    category text DEFAULT 'default'::text,
    coordination_score_v2 numeric(5,4) DEFAULT 0,
    CONSTRAINT chk_coordination_score_v2_range CHECK (((coordination_score_v2 >= (0)::numeric) AND (coordination_score_v2 <= (1)::numeric)))
);


--
-- Name: whales; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE IF NOT EXISTS public.whales (
    id uuid DEFAULT gen_random_uuid() NOT NULL,
    address text NOT NULL,
    display_name text,
    avatar_url text,
    expert_weight numeric(5,4) DEFAULT 0.5,
    win_rate numeric(5,4) DEFAULT 0.5,
    total_volume numeric(20,6) DEFAULT 0,
    category_expertise jsonb DEFAULT '{}'::jsonb,
    is_anomaly boolean DEFAULT false,
    last_seen_at timestamp with time zone DEFAULT now(),
    created_at timestamp with time zone DEFAULT now(),
    updated_at timestamp with time zone DEFAULT now(),
    trading_pattern text DEFAULT 'unknown'::text,
    wallet_classification text DEFAULT 'unknown'::text,
    anomaly_reason text,
    pnl_usd numeric DEFAULT 0,
    roi_percent numeric DEFAULT 0,
    wins integer DEFAULT 0,
    losses integer DEFAULT 0,
    total_invested numeric DEFAULT 0,
    timing_score_avg numeric(5,4) DEFAULT 0,
    is_arbitrageur boolean DEFAULT false,
    arb_ratio numeric(5,4) DEFAULT 0,
    metrics_updated_at timestamp with time zone,
    insider_score numeric(5,4) DEFAULT 0,
    fresh_wallet_risk numeric(5,4) DEFAULT 0,
    cluster_id uuid,
    smart_money_wallet_id uuid,
    funding_depth integer DEFAULT 0,
    sector_win_rates jsonb DEFAULT '{}'::jsonb,
    avg_bet_size numeric DEFAULT 0,
    max_bet_size numeric DEFAULT 0,
    bet_count integer DEFAULT 0,
    whale_tier text DEFAULT 'shrimp'::text,
    ebq_score numeric(5,4),
    ebq_ci_low numeric(5,4),
    ebq_ci_high numeric(5,4),
    predictive_roi numeric(8,4),
    signal_strength numeric(6,4),
    category_ebq jsonb DEFAULT '{}'::jsonb,
    ebq_updated_at timestamp with time zone,
    whale_style text,
    style_confidence numeric,
    style_updated_at timestamp with time zone,
    market_impact_1h numeric,
    market_impact_6h numeric,
    market_impact_24h numeric,
    impact_sample_size integer,
    impact_updated_at timestamp with time zone
);


--
-- Name: etl_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.etl_runs ALTER COLUMN id SET DEFAULT nextval('public.etl_runs_id_seq'::regclass);


--
-- Name: feature_store id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feature_store ALTER COLUMN id SET DEFAULT nextval('public.feature_store_id_seq'::regclass);


--
-- Name: oracle_recalc_timings id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oracle_recalc_timings ALTER COLUMN id SET DEFAULT nextval('public.oracle_recalc_timings_id_seq'::regclass);


--
-- Name: retail_trades id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.retail_trades ALTER COLUMN id SET DEFAULT nextval('public.retail_trades_id_seq'::regclass);


--
-- Name: strategy_discovery_runs id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_discovery_runs ALTER COLUMN id SET DEFAULT nextval('public.strategy_discovery_runs_id_seq'::regclass);


--
-- Name: strategy_validations id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_validations ALTER COLUMN id SET DEFAULT nextval('public.strategy_validations_id_seq'::regclass);


--
-- Name: strategy_virtual_positions id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_virtual_positions ALTER COLUMN id SET DEFAULT nextval('public.strategy_virtual_positions_id_seq'::regclass);


--
-- Name: bin_reliability bin_reliability_bin_label_computed_at_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bin_reliability
    ADD CONSTRAINT bin_reliability_bin_label_computed_at_key UNIQUE (bin_label, computed_at);


--
-- Name: bin_reliability bin_reliability_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.bin_reliability
    ADD CONSTRAINT bin_reliability_pkey PRIMARY KEY (id);


--
-- Name: calibration_configs calibration_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.calibration_configs
    ADD CONSTRAINT calibration_configs_pkey PRIMARY KEY (id);


--
-- Name: daily_benchmarks daily_benchmarks_date_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.daily_benchmarks
    ADD CONSTRAINT daily_benchmarks_date_key UNIQUE (date);


--
-- Name: daily_benchmarks daily_benchmarks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.daily_benchmarks
    ADD CONSTRAINT daily_benchmarks_pkey PRIMARY KEY (id);


--
-- Name: etl_runs etl_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.etl_runs
    ADD CONSTRAINT etl_runs_pkey PRIMARY KEY (id);


--
-- Name: event_arbitrage_scans event_arbitrage_scans_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_arbitrage_scans
    ADD CONSTRAINT event_arbitrage_scans_pkey PRIMARY KEY (id);


--
-- Name: events events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_pkey PRIMARY KEY (id);


--
-- Name: events events_polymarket_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.events
    ADD CONSTRAINT events_polymarket_id_key UNIQUE (polymarket_id);


--
-- Name: execution_orders execution_orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.execution_orders
    ADD CONSTRAINT execution_orders_pkey PRIMARY KEY (id);


--
-- Name: feature_store feature_store_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feature_store
    ADD CONSTRAINT feature_store_pkey PRIMARY KEY (id);


--
-- Name: feature_store feature_store_unique; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.feature_store
    ADD CONSTRAINT feature_store_unique UNIQUE (market_id, snapshot_at, side, features_version);


--
-- Name: funding_chains funding_chains_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.funding_chains
    ADD CONSTRAINT funding_chains_pkey PRIMARY KEY (id);


--
-- Name: maker_direction_cache maker_direction_cache_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.maker_direction_cache
    ADD CONSTRAINT maker_direction_cache_pkey PRIMARY KEY (id);


--
-- Name: market_analyses market_analyses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_analyses
    ADD CONSTRAINT market_analyses_pkey PRIMARY KEY (id);


--
-- Name: market_comments market_comments_comment_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_comments
    ADD CONSTRAINT market_comments_comment_id_key UNIQUE (comment_id);


--
-- Name: market_comments market_comments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_comments
    ADD CONSTRAINT market_comments_pkey PRIMARY KEY (id);


--
-- Name: market_signals market_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_signals
    ADD CONSTRAINT market_signals_pkey PRIMARY KEY (id);


--
-- Name: markets markets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.markets
    ADD CONSTRAINT markets_pkey PRIMARY KEY (id);


--
-- Name: markets markets_polymarket_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.markets
    ADD CONSTRAINT markets_polymarket_id_key UNIQUE (polymarket_id);


--
-- Name: ml_models ml_models_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ml_models
    ADD CONSTRAINT ml_models_pkey PRIMARY KEY (version);


--
-- Name: ml_predictions ml_predictions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ml_predictions
    ADD CONSTRAINT ml_predictions_pkey PRIMARY KEY (market_id, snapshot_at, model_version);


--
-- Name: news_market_mappings news_market_mappings_news_id_market_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_market_mappings
    ADD CONSTRAINT news_market_mappings_news_id_market_id_key UNIQUE (news_id, market_id);


--
-- Name: news_market_mappings news_market_mappings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_market_mappings
    ADD CONSTRAINT news_market_mappings_pkey PRIMARY KEY (id);


--
-- Name: news news_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news
    ADD CONSTRAINT news_pkey PRIMARY KEY (id);


--
-- Name: news news_url_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news
    ADD CONSTRAINT news_url_key UNIQUE (url);


--
-- Name: objective_signals objective_signals_market_id_signal_date_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.objective_signals
    ADD CONSTRAINT objective_signals_market_id_signal_date_key UNIQUE (market_id, signal_date);


--
-- Name: objective_signals objective_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.objective_signals
    ADD CONSTRAINT objective_signals_pkey PRIMARY KEY (id);


--
-- Name: oracle_recalc_timings oracle_recalc_timings_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oracle_recalc_timings
    ADD CONSTRAINT oracle_recalc_timings_pkey PRIMARY KEY (id);


--
-- Name: oracle_snapshots oracle_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oracle_snapshots
    ADD CONSTRAINT oracle_snapshots_pkey PRIMARY KEY (id);


--
-- Name: retail_trades retail_trades_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.retail_trades
    ADD CONSTRAINT retail_trades_pkey PRIMARY KEY (id);


--
-- Name: retail_trades retail_trades_trade_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.retail_trades
    ADD CONSTRAINT retail_trades_trade_id_key UNIQUE (trade_id);


--
-- Name: scoring_configs scoring_configs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.scoring_configs
    ADD CONSTRAINT scoring_configs_pkey PRIMARY KEY (hash);


--
-- Name: smart_money_wallets smart_money_wallets_address_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.smart_money_wallets
    ADD CONSTRAINT smart_money_wallets_address_key UNIQUE (address);


--
-- Name: smart_money_wallets smart_money_wallets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.smart_money_wallets
    ADD CONSTRAINT smart_money_wallets_pkey PRIMARY KEY (id);


--
-- Name: strategy_audit strategy_audit_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_audit
    ADD CONSTRAINT strategy_audit_pkey PRIMARY KEY (id);


--
-- Name: strategy_bankroll strategy_bankroll_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_bankroll
    ADD CONSTRAINT strategy_bankroll_pkey PRIMARY KEY (strategy_key);


--
-- Name: strategy_discovery_runs strategy_discovery_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_discovery_runs
    ADD CONSTRAINT strategy_discovery_runs_pkey PRIMARY KEY (id);


--
-- Name: strategy_ledger strategy_ledger_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_ledger
    ADD CONSTRAINT strategy_ledger_pkey PRIMARY KEY (id);


--
-- Name: strategy_live_metrics strategy_live_metrics_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_live_metrics
    ADD CONSTRAINT strategy_live_metrics_pkey PRIMARY KEY (strategy_key);


--
-- Name: strategy_positions strategy_positions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_positions
    ADD CONSTRAINT strategy_positions_pkey PRIMARY KEY (id);


--
-- Name: strategy_positions strategy_positions_strategy_key_market_id_snapshot_date_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_positions
    ADD CONSTRAINT strategy_positions_strategy_key_market_id_snapshot_date_key UNIQUE (strategy_key, market_id, snapshot_date);


--
-- Name: strategy_state strategy_state_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_state
    ADD CONSTRAINT strategy_state_pkey PRIMARY KEY (strategy_key);


--
-- Name: strategy_validations strategy_validations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_validations
    ADD CONSTRAINT strategy_validations_pkey PRIMARY KEY (id);


--
-- Name: strategy_virtual_positions strategy_virtual_positions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_virtual_positions
    ADD CONSTRAINT strategy_virtual_positions_pkey PRIMARY KEY (id);


--
-- Name: trade_features trade_features_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_features
    ADD CONSTRAINT trade_features_pkey PRIMARY KEY (market_id, snapshot_at, feature_version);


--
-- Name: trade_signals trade_signals_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_signals
    ADD CONSTRAINT trade_signals_pkey PRIMARY KEY (id);


--
-- Name: trade_signals trade_signals_strategy_key_market_id_snapshot_date_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trade_signals
    ADD CONSTRAINT trade_signals_strategy_key_market_id_snapshot_date_key UNIQUE (strategy_key, market_id, snapshot_date);


--
-- Name: transactions transactions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT transactions_pkey PRIMARY KEY (id);


--
-- Name: transactions transactions_tx_hash_log_index_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT transactions_tx_hash_log_index_key UNIQUE (tx_hash, log_index);


--
-- Name: wallet_clusters uq_wallet_clusters_label; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_clusters
    ADD CONSTRAINT uq_wallet_clusters_label UNIQUE (cluster_label);


--
-- Name: wallet_clusters wallet_clusters_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.wallet_clusters
    ADD CONSTRAINT wallet_clusters_pkey PRIMARY KEY (id);


--
-- Name: whales whales_address_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.whales
    ADD CONSTRAINT whales_address_key UNIQUE (address);


--
-- Name: whales whales_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.whales
    ADD CONSTRAINT whales_pkey PRIMARY KEY (id);


--
-- Name: idx_arb_scans_deviation; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_arb_scans_deviation ON public.event_arbitrage_scans USING btree (abs(deviation)) WHERE (abs(deviation) > 0.02);


--
-- Name: idx_arb_scans_event; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_arb_scans_event ON public.event_arbitrage_scans USING btree (event_id);


--
-- Name: idx_arb_scans_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_arb_scans_time ON public.event_arbitrage_scans USING btree (scanned_at);


--
-- Name: idx_bin_reliability_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_bin_reliability_latest ON public.bin_reliability USING btree (computed_at DESC);


--
-- Name: idx_bin_reliability_whitelisted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_bin_reliability_whitelisted ON public.bin_reliability USING btree (is_whitelisted) WHERE (is_whitelisted = true);


--
-- Name: idx_calibration_configs_active_category; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS idx_calibration_configs_active_category ON public.calibration_configs USING btree (category) WHERE (is_active = true);


--
-- Name: idx_daily_benchmarks_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_daily_benchmarks_date ON public.daily_benchmarks USING btree (date DESC);


--
-- Name: idx_discovery_runs_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_discovery_runs_time ON public.strategy_discovery_runs USING btree (run_at DESC);


--
-- Name: idx_etl_runs_job_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_etl_runs_job_time ON public.etl_runs USING btree (job_name, started_at DESC);


--
-- Name: idx_etl_runs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_etl_runs_status ON public.etl_runs USING btree (status, started_at DESC);


--
-- Name: idx_events_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_events_active ON public.events USING btree (is_active) WHERE (is_active = true);


--
-- Name: idx_events_active_volume; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_events_active_volume ON public.events USING btree (is_active, volume_24h DESC);


--
-- Name: idx_events_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_events_category ON public.events USING btree (category);


--
-- Name: idx_events_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_events_slug ON public.events USING btree (slug);


--
-- Name: idx_events_volume; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_events_volume ON public.events USING btree (volume_24h DESC);


--
-- Name: idx_exec_orders_market; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_exec_orders_market ON public.execution_orders USING btree (market_id);


--
-- Name: idx_exec_orders_placed_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_exec_orders_placed_at ON public.execution_orders USING btree (placed_at) WHERE (status = 'placed'::text);


--
-- Name: idx_exec_orders_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_exec_orders_status ON public.execution_orders USING btree (status);


--
-- Name: idx_exec_orders_strategy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_exec_orders_strategy ON public.execution_orders USING btree (strategy_key);


--
-- Name: idx_fs_category_price; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_fs_category_price ON public.feature_store USING btree (category, market_price);


--
-- Name: idx_fs_live_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_fs_live_pending ON public.feature_store USING btree (snapshot_at DESC) WHERE ((source = 'live'::text) AND (outcome = 'pending'::text));


--
-- Name: idx_fs_resolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_fs_resolved ON public.feature_store USING btree (resolved_at DESC) WHERE (outcome = ANY (ARRAY['yes'::text, 'no'::text]));


--
-- Name: idx_fs_snapshot_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_fs_snapshot_at ON public.feature_store USING btree (snapshot_at DESC);


--
-- Name: idx_fs_source_outcome; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_fs_source_outcome ON public.feature_store USING btree (source, outcome);


--
-- Name: idx_funding_chains_funder; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_funding_chains_funder ON public.funding_chains USING btree (funder_address);


--
-- Name: idx_funding_chains_pair; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS idx_funding_chains_pair ON public.funding_chains USING btree (target_address, funder_address);


--
-- Name: idx_funding_chains_target; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_funding_chains_target ON public.funding_chains USING btree (target_address);


--
-- Name: idx_maker_direction_cache_fetched; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_maker_direction_cache_fetched ON public.maker_direction_cache USING btree (fetched_at);


--
-- Name: idx_maker_direction_cache_market; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS idx_maker_direction_cache_market ON public.maker_direction_cache USING btree (market_id);


--
-- Name: idx_market_analyses_latest; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_market_analyses_latest ON public.market_analyses USING btree (market_id, generated_at DESC);


--
-- Name: idx_market_comments_holder; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_market_comments_holder ON public.market_comments USING btree (market_id, is_holder) WHERE (is_holder = true);


--
-- Name: idx_market_comments_market; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_market_comments_market ON public.market_comments USING btree (market_id);


--
-- Name: idx_market_signals_market; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS idx_market_signals_market ON public.market_signals USING btree (market_id);


--
-- Name: idx_market_signals_updated; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_market_signals_updated ON public.market_signals USING btree (updated_at);


--
-- Name: idx_markets_active_category_volume; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_markets_active_category_volume ON public.markets USING btree (is_active, category, volume_24h DESC);


--
-- Name: idx_markets_active_volume; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_markets_active_volume ON public.markets USING btree (is_active, volume_24h DESC);


--
-- Name: idx_markets_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_markets_category ON public.markets USING btree (category);


--
-- Name: idx_markets_condition_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_markets_condition_id ON public.markets USING btree (condition_id);


--
-- Name: idx_markets_event_id; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_markets_event_id ON public.markets USING btree (event_id);


--
-- Name: idx_markets_horizon; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_markets_horizon ON public.markets USING btree (horizon);


--
-- Name: idx_markets_slug; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_markets_slug ON public.markets USING btree (slug) WHERE (slug IS NOT NULL);


--
-- Name: idx_ml_models_active_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS idx_ml_models_active_unique ON public.ml_models USING btree (feature_version) WHERE (is_active = true);


--
-- Name: idx_ml_models_trained_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_ml_models_trained_at ON public.ml_models USING btree (trained_at DESC);


--
-- Name: idx_ml_predictions_market_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_ml_predictions_market_recent ON public.ml_predictions USING btree (market_id, snapshot_at DESC);


--
-- Name: idx_ml_predictions_model_resolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_ml_predictions_model_resolved ON public.ml_predictions USING btree (model_version, outcome) WHERE (outcome IS NOT NULL);


--
-- Name: idx_mv_market_resolved_outcome_market_id; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS idx_mv_market_resolved_outcome_market_id ON public.mv_market_resolved_outcome USING btree (market_id);


--
-- Name: idx_news_mappings_confidence; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_news_mappings_confidence ON public.news_market_mappings USING btree (confidence_score DESC) WHERE (confidence_score >= 0.85);


--
-- Name: idx_news_mappings_market_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_news_mappings_market_created ON public.news_market_mappings USING btree (market_id, created_at DESC);


--
-- Name: idx_news_market_mappings_analyzed; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_news_market_mappings_analyzed ON public.news_market_mappings USING btree (market_id, analyzed_at DESC) WHERE (analyzed_at IS NOT NULL);


--
-- Name: idx_news_published_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_news_published_at ON public.news USING btree (published_at DESC);


--
-- Name: idx_news_scraped_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_news_scraped_at ON public.news USING btree (scraped_at);


--
-- Name: idx_news_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_news_source ON public.news USING btree (source);


--
-- Name: idx_objective_signals_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_objective_signals_category ON public.objective_signals USING btree (category);


--
-- Name: idx_objective_signals_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_objective_signals_date ON public.objective_signals USING btree (signal_date);


--
-- Name: idx_objective_signals_market; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_objective_signals_market ON public.objective_signals USING btree (market_id);


--
-- Name: idx_oracle_recalc_timings_run_step; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_oracle_recalc_timings_run_step ON public.oracle_recalc_timings USING btree (run_id, step);


--
-- Name: idx_oracle_recalc_timings_step_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_oracle_recalc_timings_step_created ON public.oracle_recalc_timings USING btree (step, created_at DESC);


--
-- Name: idx_oracle_snapshots_calibration; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_calibration ON public.oracle_snapshots USING btree (created_at) WHERE ((outcome <> 'pending'::text) AND (whale_contribution_raw IS NOT NULL) AND (news_contribution_raw IS NOT NULL));


--
-- Name: idx_oracle_snapshots_category; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_category ON public.oracle_snapshots USING btree (category) WHERE (category IS NOT NULL);


--
-- Name: idx_oracle_snapshots_created_brin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_created_brin ON public.oracle_snapshots USING brin (created_at) WITH (pages_per_range='64');


--
-- Name: idx_oracle_snapshots_maker_direction; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_maker_direction ON public.oracle_snapshots USING btree (maker_direction_raw) WHERE (maker_direction_raw <> (0)::numeric);


--
-- Name: idx_oracle_snapshots_market_resolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_market_resolved ON public.oracle_snapshots USING btree (market_id, resolved_at DESC) INCLUDE (outcome) WHERE (outcome = ANY (ARRAY['yes'::text, 'no'::text]));


--
-- Name: idx_oracle_snapshots_market_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_market_time ON public.oracle_snapshots USING btree (market_id, created_at);


--
-- Name: idx_oracle_snapshots_pending_market; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_pending_market ON public.oracle_snapshots USING btree (market_id, created_at DESC) WHERE (outcome = 'pending'::text);


--
-- Name: idx_oracle_snapshots_pending_recent; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_pending_recent ON public.oracle_snapshots USING btree (created_at DESC, market_id) WHERE (outcome = 'pending'::text);


--
-- Name: idx_oracle_snapshots_resolved_at_analytics; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_resolved_at_analytics ON public.oracle_snapshots USING btree (resolved_at DESC) WHERE (outcome <> 'pending'::text);


--
-- Name: idx_oracle_snapshots_vwap; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_oracle_snapshots_vwap ON public.oracle_snapshots USING btree (created_at DESC) INCLUDE (market_id, market_price) WHERE (market_price IS NOT NULL);


--
-- Name: idx_retail_trades_block; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_retail_trades_block ON public.retail_trades USING btree (block_number DESC);


--
-- Name: idx_retail_trades_condition_ts; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_retail_trades_condition_ts ON public.retail_trades USING btree (condition_id, block_timestamp DESC);


--
-- Name: idx_retail_trades_retail_signal; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_retail_trades_retail_signal ON public.retail_trades USING btree (condition_id, is_retail, side, block_timestamp DESC);


--
-- Name: idx_retail_trades_timestamp; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_retail_trades_timestamp ON public.retail_trades USING btree (block_timestamp DESC);


--
-- Name: idx_scoring_configs_label; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_scoring_configs_label ON public.scoring_configs USING btree (label);


--
-- Name: idx_scoring_configs_promoted; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_scoring_configs_promoted ON public.scoring_configs USING btree (promoted_to_prod_at DESC) WHERE (promoted_to_prod_at IS NOT NULL);


--
-- Name: idx_sl_strategy_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_sl_strategy_date ON public.strategy_ledger USING btree (strategy_key, date DESC);


--
-- Name: idx_slm_paused; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_slm_paused ON public.strategy_live_metrics USING btree (strategy_key) WHERE (paused = false);


--
-- Name: idx_smart_money_wallets_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_smart_money_wallets_active ON public.smart_money_wallets USING btree (is_active) WHERE (is_active = true);


--
-- Name: idx_smart_money_wallets_address; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_smart_money_wallets_address ON public.smart_money_wallets USING btree (address);


--
-- Name: idx_sp_market; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_sp_market ON public.strategy_positions USING btree (market_id);


--
-- Name: idx_sp_open; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_sp_open ON public.strategy_positions USING btree (status) WHERE (status = 'open'::text);


--
-- Name: idx_sp_signal_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_sp_signal_source ON public.strategy_positions USING btree (signal_source);


--
-- Name: idx_sp_strategy_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_sp_strategy_status ON public.strategy_positions USING btree (strategy_key, status);


--
-- Name: idx_strategy_audit_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_strategy_audit_created ON public.strategy_audit USING btree (created_at DESC);


--
-- Name: idx_strategy_audit_key; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_strategy_audit_key ON public.strategy_audit USING btree (strategy_key);


--
-- Name: idx_strategy_positions_mode; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_strategy_positions_mode ON public.strategy_positions USING btree (strategy_key, market_id, mode, status);


--
-- Name: idx_strategy_validations_key_time; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_strategy_validations_key_time ON public.strategy_validations USING btree (strategy_key, validated_at DESC);


--
-- Name: idx_trade_features_category_resolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_trade_features_category_resolved ON public.trade_features USING btree (category, resolved_at DESC) WHERE (outcome IS NOT NULL);


--
-- Name: idx_trade_features_pending; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_trade_features_pending ON public.trade_features USING btree (market_id) WHERE (outcome IS NULL);


--
-- Name: idx_trade_features_resolved_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_trade_features_resolved_at ON public.trade_features USING btree (resolved_at DESC) WHERE (outcome IS NOT NULL);


--
-- Name: idx_trade_signals_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_trade_signals_date ON public.trade_signals USING btree (snapshot_date);


--
-- Name: idx_trade_signals_liquidity_tier; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_trade_signals_liquidity_tier ON public.trade_signals USING btree (((liquidity_meta ->> 'tier'::text)));


--
-- Name: idx_trade_signals_market; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_trade_signals_market ON public.trade_signals USING btree (market_id);


--
-- Name: idx_trade_signals_source; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_trade_signals_source ON public.trade_signals USING btree (signal_source);


--
-- Name: idx_trade_signals_strategy; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_trade_signals_strategy ON public.trade_signals USING btree (strategy_key);


--
-- Name: idx_transactions_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_transactions_created_at ON public.transactions USING btree (created_at DESC);


--
-- Name: idx_transactions_from_address; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_transactions_from_address ON public.transactions USING btree (from_address) WHERE (from_address IS NOT NULL);


--
-- Name: idx_transactions_market_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_transactions_market_created ON public.transactions USING btree (market_id, created_at DESC);


--
-- Name: idx_transactions_market_nonarb; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_transactions_market_nonarb ON public.transactions USING btree (market_id, created_at DESC) WHERE (is_arbitrage = false);


--
-- Name: idx_transactions_whale_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_transactions_whale_created ON public.transactions USING btree (whale_id, created_at DESC);


--
-- Name: idx_wallet_clusters_active; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_wallet_clusters_active ON public.wallet_clusters USING btree (is_active) WHERE (is_active = true);


--
-- Name: idx_wallet_clusters_addresses_gin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_wallet_clusters_addresses_gin ON public.wallet_clusters USING gin (wallet_addresses);


--
-- Name: idx_wallet_clusters_detected; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_wallet_clusters_detected ON public.wallet_clusters USING btree (detected_at DESC);


--
-- Name: idx_whales_address_lower; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_whales_address_lower ON public.whales USING btree (lower(address));


--
-- Name: idx_whales_cluster; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_whales_cluster ON public.whales USING btree (cluster_id) WHERE (cluster_id IS NOT NULL);


--
-- Name: idx_whales_ebq_score; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_whales_ebq_score ON public.whales USING btree (ebq_score DESC NULLS LAST);


--
-- Name: idx_whales_insider_score; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_whales_insider_score ON public.whales USING btree (insider_score DESC) WHERE (insider_score > (0)::numeric);


--
-- Name: idx_whales_is_anomaly; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_whales_is_anomaly ON public.whales USING btree (is_anomaly);


--
-- Name: idx_whales_is_arbitrageur; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_whales_is_arbitrageur ON public.whales USING btree (is_arbitrageur) WHERE (is_arbitrageur = true);


--
-- Name: idx_whales_signal_strength; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_whales_signal_strength ON public.whales USING btree (signal_strength DESC NULLS LAST);


--
-- Name: idx_whales_tier; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS idx_whales_tier ON public.whales USING btree (whale_tier);


--
-- Name: ix_svp_open_by_market; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS ix_svp_open_by_market ON public.strategy_virtual_positions USING btree (market_id, status) WHERE (status = 'open'::text);


--
-- Name: ix_svp_strategy_resolved; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX IF NOT EXISTS ix_svp_strategy_resolved ON public.strategy_virtual_positions USING btree (strategy_key, status, resolved_at DESC);


--
-- Name: ux_svp_daily_unique; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX IF NOT EXISTS ux_svp_daily_unique ON public.strategy_virtual_positions USING btree (strategy_key, market_id, side, snapshot_date);


--
-- Name: strategy_state trg_bankroll_on_stage_change; Type: TRIGGER; Schema: public; Owner: -
--

CREATE OR REPLACE TRIGGER trg_bankroll_on_stage_change AFTER UPDATE OF stage ON public.strategy_state FOR EACH ROW EXECUTE FUNCTION public.fn_handle_bankroll_on_stage_change();


--
-- Name: strategy_state trg_create_bankroll_on_strategy_insert; Type: TRIGGER; Schema: public; Owner: -
--

CREATE OR REPLACE TRIGGER trg_create_bankroll_on_strategy_insert AFTER INSERT ON public.strategy_state FOR EACH ROW EXECUTE FUNCTION public.fn_create_bankroll_for_strategy();


--
-- Name: event_arbitrage_scans event_arbitrage_scans_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.event_arbitrage_scans
    ADD CONSTRAINT event_arbitrage_scans_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id);


--
-- Name: strategy_bankroll fk_bankroll_strategy_state; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_bankroll
    ADD CONSTRAINT fk_bankroll_strategy_state FOREIGN KEY (strategy_key) REFERENCES public.strategy_state(strategy_key) ON DELETE CASCADE;


--
-- Name: maker_direction_cache maker_direction_cache_market_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.maker_direction_cache
    ADD CONSTRAINT maker_direction_cache_market_id_fkey FOREIGN KEY (market_id) REFERENCES public.markets(id) ON DELETE CASCADE;


--
-- Name: market_analyses market_analyses_market_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_analyses
    ADD CONSTRAINT market_analyses_market_id_fkey FOREIGN KEY (market_id) REFERENCES public.markets(id) ON DELETE CASCADE;


--
-- Name: market_comments market_comments_market_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_comments
    ADD CONSTRAINT market_comments_market_id_fkey FOREIGN KEY (market_id) REFERENCES public.markets(id);


--
-- Name: market_signals market_signals_market_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.market_signals
    ADD CONSTRAINT market_signals_market_id_fkey FOREIGN KEY (market_id) REFERENCES public.markets(id) ON DELETE CASCADE;


--
-- Name: markets markets_event_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.markets
    ADD CONSTRAINT markets_event_id_fkey FOREIGN KEY (event_id) REFERENCES public.events(id) ON DELETE SET NULL;


--
-- Name: ml_predictions ml_predictions_model_version_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.ml_predictions
    ADD CONSTRAINT ml_predictions_model_version_fkey FOREIGN KEY (model_version) REFERENCES public.ml_models(version);


--
-- Name: news_market_mappings news_market_mappings_market_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_market_mappings
    ADD CONSTRAINT news_market_mappings_market_id_fkey FOREIGN KEY (market_id) REFERENCES public.markets(id) ON DELETE CASCADE;


--
-- Name: news_market_mappings news_market_mappings_news_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.news_market_mappings
    ADD CONSTRAINT news_market_mappings_news_id_fkey FOREIGN KEY (news_id) REFERENCES public.news(id) ON DELETE CASCADE;


--
-- Name: oracle_snapshots oracle_snapshots_market_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.oracle_snapshots
    ADD CONSTRAINT oracle_snapshots_market_id_fkey FOREIGN KEY (market_id) REFERENCES public.markets(id) ON DELETE CASCADE;


--
-- Name: strategy_ledger strategy_ledger_position_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_ledger
    ADD CONSTRAINT strategy_ledger_position_id_fkey FOREIGN KEY (position_id) REFERENCES public.strategy_positions(id);


--
-- Name: strategy_state strategy_state_discovery_run_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_state
    ADD CONSTRAINT strategy_state_discovery_run_id_fkey FOREIGN KEY (discovery_run_id) REFERENCES public.strategy_discovery_runs(id) ON DELETE SET NULL;


--
-- Name: strategy_validations strategy_validations_strategy_key_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_validations
    ADD CONSTRAINT strategy_validations_strategy_key_fkey FOREIGN KEY (strategy_key) REFERENCES public.strategy_state(strategy_key) ON DELETE CASCADE;


--
-- Name: strategy_virtual_positions strategy_virtual_positions_market_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_virtual_positions
    ADD CONSTRAINT strategy_virtual_positions_market_id_fkey FOREIGN KEY (market_id) REFERENCES public.markets(id) ON DELETE CASCADE;


--
-- Name: strategy_virtual_positions strategy_virtual_positions_strategy_key_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.strategy_virtual_positions
    ADD CONSTRAINT strategy_virtual_positions_strategy_key_fkey FOREIGN KEY (strategy_key) REFERENCES public.strategy_state(strategy_key) ON DELETE CASCADE;


--
-- Name: transactions transactions_whale_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.transactions
    ADD CONSTRAINT transactions_whale_id_fkey FOREIGN KEY (whale_id) REFERENCES public.whales(id);


--
-- Name: whales whales_cluster_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.whales
    ADD CONSTRAINT whales_cluster_id_fkey FOREIGN KEY (cluster_id) REFERENCES public.wallet_clusters(id) ON DELETE SET NULL;


--
-- Name: whales whales_smart_money_wallet_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.whales
    ADD CONSTRAINT whales_smart_money_wallet_id_fkey FOREIGN KEY (smart_money_wallet_id) REFERENCES public.smart_money_wallets(id) ON DELETE SET NULL;


--
-- Name: markets Allow public read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Allow public read" ON public.markets FOR SELECT USING (true);


--
-- Name: news Allow public read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Allow public read" ON public.news FOR SELECT USING (true);


--
-- Name: news_market_mappings Allow public read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Allow public read" ON public.news_market_mappings FOR SELECT USING (true);


--
-- Name: transactions Allow public read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Allow public read" ON public.transactions FOR SELECT USING (true);


--
-- Name: whales Allow public read; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY "Allow public read" ON public.whales FOR SELECT USING (true);


--
-- Name: events; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.events ENABLE ROW LEVEL SECURITY;

--
-- Name: events events_read_all; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY events_read_all ON public.events FOR SELECT USING (true);


--
-- Name: market_analyses; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.market_analyses ENABLE ROW LEVEL SECURITY;

--
-- Name: market_analyses market_analyses_read_all; Type: POLICY; Schema: public; Owner: -
--

CREATE POLICY market_analyses_read_all ON public.market_analyses FOR SELECT USING (true);


--
-- Name: markets; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.markets ENABLE ROW LEVEL SECURITY;

--
-- Name: news; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.news ENABLE ROW LEVEL SECURITY;

--
-- Name: news_market_mappings; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.news_market_mappings ENABLE ROW LEVEL SECURITY;

--
-- Name: transactions; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.transactions ENABLE ROW LEVEL SECURITY;

--
-- Name: whales; Type: ROW SECURITY; Schema: public; Owner: -
--

ALTER TABLE public.whales ENABLE ROW LEVEL SECURITY;

--
-- PostgreSQL database dump complete
--



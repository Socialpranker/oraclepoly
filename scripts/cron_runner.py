#!/usr/bin/env python3
"""
VPS Cron Runner — executes scheduled jobs by name.

Phoenix V2 (2026-04-30): post-cleanup minimal registry. Only Pipeline V2
+ core data ingestion + ML calibration + datasets + health monitoring.

Usage:
    python scripts/cron_runner.py <command>

Examples:
    python scripts/cron_runner.py trade-poll
    python scripts/cron_runner.py d7b-scan
    python scripts/cron_runner.py v2-execution
"""

import asyncio
import json
import logging
import sys
import time
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("cron_runner")

# Jobs с asyncio.gather (параллельные DB запросы) — нужен pool побольше
LARGE_POOL_JOBS: set[str] = set()

# Категории и описания для TG-алертов при падении
JOB_DESCRIPTIONS = {
    # Core data ingestion
    "trade-poll": ("🐋 Whale Feed", "Polling Polymarket WebSocket, запись whale TX в Redis queue"),
    "market-sync": ("📊 Data", "Синхронизация рынков из Polymarket API в БД"),
    "clob-book-sync": ("📉 Market Data", "CLOB taker flow → maker_direction_cache (hourly)"),
    "resolve-outcomes": ("✅ Pipeline", "Закрытие resolved markets, settlement позиций"),
    "reconcile-orphans": ("🔧 Ops", "Привязка market_id к orphan транзакциям"),
    # NegRisk metadata (для D3)
    "negrisk-sync": ("🎯 Detectors", "Обогащение events neg_risk/neg_risk_other (every 30 min)"),
    # Pipeline V2 detectors (d5/d7b/d10b/d30/d31/d32/d33/d_meta_consensus
    # сняты с registry 2026-05-20 по результатам audit — WR/ROI fail.
    # D43 killed 2026-06-09 — гипотеза dispute-fade опровергнута (0/228 WIN).
    "d3-scan": ("🎯 Detectors", "D3 CrossMktArb scan (every 10 min)"),
    "d13-scan": ("🎯 Detectors", "D13 RetailConfidenceFade scan (every 1h, :30)"),
    "d14-scan": ("🎯 Detectors", "D14 CryptoWinnersDistributeNO scan (every 1h, :40)"),
    "d26-scan": (
        "🎯 Detectors",
        "D26 FadeWhaleExtreme scan (fade FADE_WHALE pushes ≥$3K in extreme_low, every 5 min)",
    ),
    "d29-scan": (
        "🎯 Detectors",
        "D29 NegRisk Basket Arb (sum_yes>1.10, mathematically guaranteed, every 15 min)",
    ),
    "d38-scan": (
        "🎯 Detectors",
        "D38 Resolution Lag (end_date<now() last 24h + price 0.05-0.20 or 0.80-0.95, every 5 min)",
    ),
    "d42-scan": (
        "🎯 Detectors",
        "D42 EventDrivenML (LGBM Regressor price delta 168→24/72→24/24→4h, shadow only, every 30 min)",
    ),
    "d44-scan": (
        "🎯 Detectors",
        "D44 LateConvergenceCarry (DTE 2h-7d, price 0.96-0.98 YES / 0.02-0.04 NO, shadow only, every 30 min)",
    ),
    "d45-scan": (
        "🎯 Detectors",
        "D45 LeadLagCrossVenue (Polymarket↔Kalshi lead-lag, 5-30 min window, shadow only, every 5 min)",
    ),
    "d49-scan": (
        "🎯 Detectors",
        "D49 StrikeLadder (crypto above-$K ladder monotonicity arb, shadow only, every 5 min)",
    ),
    "manifold-scrape": (
        "🔗 Manifold",
        "Pull active BINARY Manifold markets + probability snapshots (every 15 min, public API, PDF §11)",
    ),
    "manifold-canonical-match": (
        "🔗 Manifold",
        "Embed Manifold + canonical_events, cosine match → venue_market_mappings (every 4h)",
    ),
    "polymarket-canonical-match": (
        "🔗 Polymarket",
        "Embed Polymarket markets + canonical_events, cosine match → venue_market_mappings (markets.id UUID, every 4h)",
    ),
    "canonical-event-generator": (
        "🔗 Canonical",
        "Auto-generate canonical_events from liquid Polymarket events (≥$50k) + exact polymarket mappings (every 6h)",
    ),
    "manifold-bets-scrape": (
        "🔗 Manifold",
        "Incremental pull individual bets for tracked Manifold markets (every 30 min, PDF §11 lead-lag)",
    ),
    # Kalshi integration Phase 1 (see research/kalshi-integration-2026-05/prd.md)
    "kalshi-market-sync": (
        "🔗 Kalshi",
        "Pull active Kalshi markets, upsert kalshi_markets (every 30 min)",
    ),
    "kalshi-polymarket-match": (
        "🔗 Kalshi",
        "Embed Polymarket+Kalshi markets, cosine match → market_pair_matches (every 4h)",
    ),
    "kalshi-orderbook-sync": (
        "🔗 Kalshi",
        "Pull Kalshi orderbooks for tracked pairs, write snapshots (every 5 min)",
    ),
    "d40-scan": (
        "🎯 Detectors",
        "D40 Kalshi Cross-Platform Arb scan (every 5 min)",
    ),
    # Daily price snapshot for D47 mean-reversion + rolling-window detectors
    "daily-price-snapshot": (
        "📸 Data",
        "Daily snapshot of every active market's yes_price (00:05 UTC daily)",
    ),
    # Intraday price snapshots для unresolved detector_signals (shadow TP/SL sim)
    "signal-price-snapshot-writer": (
        "📸 Data",
        "Capture yes_price per unresolved detector_signal (every 5 min) — shadow foundation",
    ),
    # G18 calibration edge — weekly refit of detector_calibration_bins
    "calibration-bins-refit": (
        "🎯 ML Calibration",
        "Refit per-(detector × category × price_bin) historical WR (Sunday 04:00 UTC)",
    ),
    # Live ML feature pipeline (Variant A — universal)
    "live-price-snapshot-writer": (
        "🧠 ML Pipeline",
        "Capture yes_price for active markets at anchor moments (every 5 min)",
    ),
    "live-trade-bucket-writer": (
        "🧠 ML Pipeline",
        "Aggregate Polymarket trades into hourly buckets per market (every 15 min)",
    ),
    "live-wallet-flow-writer": (
        "🧠 ML Pipeline",
        "Derive wallet flow per (market, hour) from live_trade_buckets (every 15 min)",
    ),
    "live-ml-features-builder": (
        "🧠 ML Pipeline",
        "Build 55-col live_ml_features matrix via feature_kernel (every 30 min)",
    ),
    "auto-edge-scan": ("🔬 Research", "Weekly automated edge discovery (Sun 03:00 UTC, host venv-research)"),
    "v2-execution": ("💰 Execution", "V2 execution: detector signals → intent → live placement (every 1 min)"),
    "v2-execution-resolve": ("💰 Execution", "V2 resolve: poll fills, stale-cancel maker, settle (every 1 min)"),
    "mtm-refresh": ("💰 Execution", "MtM refresh: update mtm_price/unrealized_pnl для open positions (every 1 min)"),
    "execution-reconcile": ("💰 Execution", "Phase 1 Stage 4: 60s wallet/DB reconciliation + 3-strike drift detection"),
    "update-detector-sizing": ("💰 Execution", "Daily fractional Kelly recompute per detector (06:00 UTC)"),
    "detector-signals-resolve": ("🎯 Detectors", "Заполнение outcome + realized_roi (hourly)"),
    "redeem": (
        "💰 Execution",
        "On-chain redeem resolved live-позиций через Polymarket Relayer (hourly, dry-run пока REDEEM_ENABLED=false)",
    ),
    "market-resolution-poll": (
        "💰 Execution",
        "Demand-driven резолв рынков с открытыми ордерами: точечный CLOB poll tokens[].winner (every 15 min)",
    ),
    "virtual-weekly-digest": (
        "💰 Execution",
        "Недельная TG-сводка WIN/LOSS/EXPIRED по virtual-ордерам (Sunday 09:00 UTC)",
    ),
    "category-health": (
        "💰 Execution",
        "Материализация blacklist-кандидатов detector×category в Redis для shadow-гейта (hourly)",
    ),
    "detector-slo-monitor": (
        "🎯 Detectors",
        "Daily SLO check: emission rate per detector vs baseline (06:30 UTC, alerts TG)",
    ),
    "edge-decay-monitor": (
        "📉 Portfolio Agent",
        "Daily Wilson/Sortino + CUSUM changepoint per detector/methodology class (07:00 UTC, alerts TG)",
    ),
    "adverse-selection-monitor": (
        "📉 Portfolio Agent",
        "Daily adverse selection scan per detector×category (07:15 UTC, throttled TG, alerts TG)",
    ),
    "improvement-cards-monitor": (
        "📉 Portfolio Agent",
        "Daily сборка Improvement Cards из аудитов, ранжирование по EV/час (07:30 UTC, без TG)",
    ),
    "improvement-brief-weekly": (
        "📉 Portfolio Agent",
        "Weekly TG-бриф: топ-3 Improvement Cards по EV/час с кнопками (пн 09:00 UTC)",
    ),
    "demote-alerts-monitor": (
        "🎯 Detectors",
        "Hourly check: virtual детекторы с drawdown ≥ $500 за 7d → alert TG (PR-C)",
    ),
    "execution-lag-monitor": (
        "💰 Execution",
        "Hourly: first-row p50/p95 лаг signal→order per source daemon/cron (Fix-2, latency-аудит №3)",
    ),
    "stuck-live-positions-monitor": (
        "💰 Execution",
        "Hourly: live-позиции открыты >24ч без резолва → alert TG (дедуп 6ч per order_id)",
    ),
    "d45-slo-monitor": (
        "🎯 Detectors",
        "D45 stream + freshness health check (hourly, alerts TG если Pyth/Binance disconnected)",
    ),
    "daily-summary": (
        "📊 Reports",
        "Daily virtual P&L + activity summary в TG (06:00 UTC = 09:00 МСК)",
    ),
    "virtual-promote-report": (
        "📊 Reports",
        "Virtual gate-статус по детекторам в TG (08:00 UTC = 11:00 МСК)",
    ),
    "detectors-honest-audit-report": (
        "📊 Reports",
        "Еженедельный honest prod-PnL термометр по живым детекторам за 30д в TG (вс 05:00 UTC)",
    ),
    "db-size-report": (
        "📊 Reports",
        "Bi-weekly размер БД + топ-таблицы в TG, warn при разрастании (1 и 15 числа 05:30 UTC)",
    ),
    "polymarket-api-health": (
        "🌐 External",
        "Hourly check 429/5xx error rate к Polymarket vs 7d baseline (alerts TG)",
    ),
    # ML calibration
    "ml-calibrator-fit": ("📐 Calibration", "L1 Beta calibrator fit per-detector (Sun 03:00 UTC)"),
    "ml-meta-labeler-fit": ("📐 Calibration", "L2 LightGBM meta-labeler fit + auto-promote (Sun 04:00 UTC)"),
    "model-drift-check": ("📐 Calibration", "Daily Brier degradation check L1+L2 (06:00 UTC)"),
    "refresh-mv-event-outcome": ("📐 Calibration", "Hourly REFRESH mv_event_outcome (Layer A ground truth)"),
    "ml-event-selector-fit": ("📐 Calibration", "Layer A (Event Selector) weekly fit (Sun 03:30 UTC)"),
    "ml-market-picker-fit": ("📐 Calibration", "Layer B (Market Picker) weekly fit + 3-gate promote (Sun 04:30 UTC)"),
    "ml-market-picker-ev-fit": (
        "📐 Calibration",
        "Layer B EV-regressor weekly fit (Shadow A/B vs binary) (Sun 04:35 UTC)",
    ),
    "pipeline-v2-shadow-predictor": (
        "🎯 Detectors",
        "Pipeline V2 shadow predictor — parallel mono vs v2 (every 10 min)",
    ),
    "pipeline-v2-outcome-sync": ("🎯 Detectors", "Sync resolved outcomes to shadow predictions (hourly :50)"),
    "pipeline-v2-comparison-report": ("📐 Calibration", "Daily mono vs v2 per-event Brier comparison (06:00 UTC)"),
    "pipeline-v2-drift-check": ("📐 Calibration", "Weekly Layer A/B drift vs training (Sun 06:30 UTC, post-cutover)"),
    "ml-accuracy-snapshot": ("📐 Calibration", "Weekly honest-cohort точность ML — 3 слоя + деньги (Sun 06:45 UTC)"),
    # Datasets
    "hf-upstream-check": ("📊 Datasets", "Проверка свежести HF quant.parquet (daily 06:00 UTC)"),
    # Health
    "wallet-health": ("💰 Execution", "Polymarket funder balance + allowance check"),
    "proxy-health": ("🌐 Infra", "IPRoyal proxy liveness + traffic check"),
    "db-monitor": ("🗄 Ops", "Мониторинг размеров ключевых таблиц БД + Telegram-алерт"),
    "site-availability": ("🌐 Infra", "Доступность example.com снаружи (фронт + API) + edge-алерт"),
    "site-availability": ("🌐 Infra", "Доступность example.com снаружи (фронт + API) + edge-алерт"),
    "cleanup": ("🗑 Ops", "Очистка устаревших данных (snapshots, logs)"),
    "vacuum-markets": ("🗑 Ops", "Weekly VACUUM FULL markets — возврат ОС TOAST-дырок от raw=NULL (Sun 04:00 UTC)"),
    "orderbook-snapshots-cleanup": (
        "🗑 Ops",
        "DELETE FROM orderbook_event_snapshots WHERE captured_at < now() - 14d (daily 06:35 UTC)",
    ),
    "refresh-tier2-mvs": (
        "📐 Analysis",
        "Refresh 5 tier-2 materialized views (hourly :50)",
    ),
    "refresh-market-calibration": (
        "📐 Analysis",
        "Refresh mv_market_calibration: калибровка рынка на нейтральной выборке (hourly)",
    ),
    "withdrawal-propose": (
        "💰 Bankroll",
        "Phase 1 Stage 7: monthly USDC withdrawal proposal (1-го 09:00 UTC, TG approval flow)",
    ),
    "ramp-check": (
        "💰 Bankroll",
        "Phase 3 Task 2: ramp-лесенка live_stake_cap_usd — daily-проверка ступени, TG approval flow (09:30 UTC)",
    ),
    "agent-daily-brief": (
        "🕵️ Portfolio Agent",
        "Phase 0 daily-бриф: прогоны 24ч + протухающие клеймы, без action-кнопок (06:50 UTC)",
    ),
    "agent-owners-report": (
        "🕵️ Portfolio Agent",
        "Phase 0 monthly owner's report: все agent_runs/claims + ручной cost ledger → research/ (1-го числа 07:00 UTC)",
    ),
    "dq-audit-daily": (
        "🕵️ Portfolio Agent",
        "Phase 2 C0: DQ-аудит (freshness/completeness/EXPIRED/RI) → GREEN|AMBER|RED, agent_runs kind='dq' (daily 06:45 UTC)",
    ),
    "policy-engine-daily": (
        "🕵️ Portfolio Agent",
        "Phase 2 C1: policy engine — вердикты §4.2 по DQ+мониторам+drawdown, agent_runs kind='proposal', shadow (daily 07:30 UTC)",
    ),
    "decision-reconcile": (
        "🕵️ Portfolio Agent",
        "Phase 2 C4: сверка ожиданий на due_date → reconciliation_result, без TG (daily 08:00 UTC)",
    ),
    "decision-retro-quarterly": (
        "🕵️ Portfolio Agent",
        "Phase 2 C4: quarterly-ретро журнала решений → TG-дайджест (1-го числа квартала 09:00 UTC)",
    ),
}

COMMANDS = {
    # Core data ingestion
    "trade-poll": ("app.jobs.trade_poll", "run_trade_poll"),
    "market-sync": ("app.jobs.market_sync", "run_market_sync"),
    "clob-book-sync": ("app.jobs.clob_book_sync", "run_clob_book_sync"),
    "resolve-outcomes": ("app.tasks.resolve_outcomes", "run_resolve_outcomes"),
    "market-resolution-poll": ("app.jobs.market_resolution_poller", "run_market_resolution_poll"),
    "reconcile-orphans": ("app.jobs.reconcile_orphans", "run_reconcile_orphans"),
    # NegRisk
    "negrisk-sync": ("app.jobs.negrisk_sync", "run_negrisk_sync"),
    # Pipeline V2 detectors (d5/d7b/d10b/d30/d31/d32/d33/d_meta_consensus
    # сняты с registry 2026-05-20 по результатам audit — WR/ROI fail.
    # D43 killed 2026-06-09 — гипотеза dispute-fade опровергнута (0/228 WIN).
    # D34/D41/D37/D10b/D_meta/D_esports/D46 killed 2026-06-20 — убыточны/пусты на live (см. detector_triage).
    "d3-scan": ("app.jobs.d3_scan", "run_d3_scan"),
    "d13-scan": ("app.jobs.d13_scan", "run_d13_scan"),
    "d14-scan": ("app.jobs.d14_scan", "run_d14_scan"),
    "d26-scan": ("app.jobs.d26_scan", "run_d26_scan"),
    "d29-scan": ("app.jobs.d29_scan", "run_d29_scan"),
    "d36-scan": ("app.jobs.d36_scan", "run_d36_scan"),
    "d38-scan": ("app.jobs.d38_scan", "run_d38_scan"),
    "d42-scan": ("app.jobs.d42_scan", "run_d42_scan"),
    "d44-scan": ("app.jobs.late_convergence_scan", "run_late_convergence_scan"),
    "d45-scan": ("app.jobs.d45_lead_lag_scan", "run_d45_scan"),
    "d49-scan": ("app.jobs.d49_scan", "run_d49_scan"),
    "manifold-scrape": ("app.jobs.manifold_scrape", "run_manifold_scrape"),
    "manifold-canonical-match": ("app.jobs.manifold_canonical_match", "run_manifold_canonical_match"),
    "polymarket-canonical-match": ("app.jobs.polymarket_canonical_match", "run_polymarket_canonical_match"),
    "canonical-event-generator": ("app.jobs.canonical_event_generator", "run_canonical_event_generator"),
    "manifold-bets-scrape": ("app.jobs.manifold_bets_scrape", "run_manifold_bets_scrape"),
    # Kalshi integration Phase 1
    "kalshi-market-sync": ("app.jobs.kalshi_market_sync", "run_kalshi_market_sync"),
    "kalshi-polymarket-match": ("app.jobs.kalshi_polymarket_match", "run_kalshi_polymarket_match"),
    "kalshi-orderbook-sync": ("app.jobs.kalshi_orderbook_sync", "run_kalshi_orderbook_sync"),
    "d40-scan": ("app.jobs.d40_kalshi_cross_platform_scan", "run_d40_scan"),
    # Daily price snapshot for D47 mean-reversion
    "daily-price-snapshot": (
        "app.jobs.daily_price_snapshot",
        "run_daily_price_snapshot",
    ),
    # Intraday price snapshots для unresolved detector_signals (shadow TP/SL sim)
    "signal-price-snapshot-writer": (
        "app.jobs.signal_price_snapshot_writer",
        "run_signal_price_snapshot_writer",
    ),
    # G18 calibration edge — weekly refit
    "calibration-bins-refit": (
        "app.jobs.calibration_bins_refit",
        "run_calibration_bins_refit",
    ),
    # Live ML feature pipeline
    "live-price-snapshot-writer": (
        "app.jobs.live_price_snapshot_writer",
        "run_live_price_snapshot_writer",
    ),
    "live-trade-bucket-writer": (
        "app.jobs.live_trade_bucket_writer",
        "run_live_trade_bucket_writer",
    ),
    "live-wallet-flow-writer": (
        "app.jobs.live_wallet_flow_writer",
        "run_live_wallet_flow_writer",
    ),
    "live-ml-features-builder": (
        "app.jobs.live_ml_features_builder",
        "run_live_ml_features_builder",
    ),
    "auto-edge-scan": ("app.jobs.auto_edge_scanner", "run_auto_edge_scanner"),
    "v2-execution": ("app.jobs.v2_execution", "run_v2_execution"),
    "v2-execution-resolve": ("app.jobs.v2_execution_resolve", "run_v2_execution_resolve"),
    "mtm-refresh": ("app.jobs.mtm_refresh", "run_mtm_refresh"),
    "execution-reconcile": ("app.jobs.execution_reconcile", "run_reconciliation"),
    "position-manager": ("app.jobs.position_manager", "run_position_manager"),
    "update-detector-sizing": ("app.core.sizing_controller", "run_update_detector_sizing"),
    "detector-signals-resolve": ("app.jobs.detector_signals_resolve", "run_detector_signals_resolve"),
    "redeem": ("app.jobs.redeem_job", "run_redeem"),
    "virtual-weekly-digest": ("app.jobs.virtual_weekly_digest", "run_virtual_weekly_digest"),
    "category-health": ("app.jobs.category_health", "run_category_health"),
    "detector-slo-monitor": ("app.jobs.detector_slo_monitor", "run_detector_slo_check"),
    "edge-decay-monitor": ("app.jobs.edge_decay_monitor", "run_edge_decay_monitor"),
    "adverse-selection-monitor": ("app.jobs.adverse_selection_monitor", "run_adverse_selection_monitor"),
    "improvement-cards-monitor": ("app.jobs.improvement_cards_monitor", "run_improvement_cards_monitor"),
    "dq-audit-daily": ("app.jobs.dq_audit_daily", "run_dq_audit_daily"),
    "policy-engine-daily": ("app.jobs.policy_engine_daily", "run_policy_engine_daily"),
    "decision-reconcile": ("app.jobs.decision_reconcile", "run_decision_reconcile"),
    "decision-retro-quarterly": ("app.jobs.decision_retro_quarterly", "run_decision_retro_quarterly"),
    "improvement-brief-weekly": ("app.jobs.improvement_brief_weekly", "run_improvement_brief_weekly"),
    "demote-alerts-monitor": ("app.jobs.demote_alerts_monitor", "run_demote_alerts_monitor"),
    "execution-lag-monitor": ("app.jobs.execution_lag_monitor", "run_execution_lag_monitor"),
    "stuck-live-positions-monitor": (
        "app.jobs.stuck_live_positions_monitor",
        "run_stuck_live_positions_monitor",
    ),
    "d45-slo-monitor": ("app.jobs.d45_slo_monitor", "run_d45_slo_check"),
    "daily-summary": ("app.jobs.daily_summary", "run_daily_summary"),
    "virtual-promote-report": ("app.jobs.virtual_promote_report", "run_virtual_promote_report"),
    "detectors-honest-audit-report": (
        "app.jobs.detectors_honest_audit_report",
        "run_detectors_honest_audit_report",
    ),
    "db-size-report": ("app.jobs.db_size_report", "run_db_size_report"),
    "polymarket-api-health": ("app.jobs.polymarket_api_health", "run_polymarket_api_health_check"),
    # ML calibration
    "ml-calibrator-fit": ("app.jobs.ml_calibrator_fit", "run_ml_calibrator_fit"),
    "ml-calibrator-lgb-fit": ("app.jobs.ml_calibrator_lgb_fit", "run_ml_calibrator_lgb_fit"),
    "ml-meta-labeler-fit": ("app.jobs.ml_meta_labeler_fit", "run_ml_meta_labeler_fit"),
    "model-drift-check": ("app.jobs.model_drift_check", "run_model_drift_check"),
    "refresh-mv-event-outcome": ("app.jobs.refresh_mv_event_outcome", "run_refresh_mv_event_outcome"),
    "ml-event-selector-fit": ("app.jobs.ml_event_selector_fit", "run_ml_event_selector_fit"),
    "ml-market-picker-fit": ("app.jobs.ml_market_picker_fit", "run_ml_market_picker_fit"),
    "ml-market-picker-ev-fit": ("app.jobs.ml_market_picker_ev_fit", "run_ml_market_picker_ev_fit"),
    "pipeline-v2-shadow-predictor": ("app.jobs.pipeline_v2_shadow_predictor", "run_pipeline_v2_shadow_predictor"),
    "pipeline-v2-outcome-sync": ("app.jobs.pipeline_v2_outcome_sync", "run_pipeline_v2_outcome_sync"),
    "pipeline-v2-comparison-report": (
        "app.jobs.pipeline_v2_comparison_report",
        "run_pipeline_v2_comparison_report",
    ),
    "pipeline-v2-drift-check": ("app.jobs.pipeline_v2_drift_check", "run_pipeline_v2_drift_check"),
    "ml-accuracy-snapshot": ("app.jobs.ml_accuracy_snapshot", "run_ml_accuracy_snapshot"),
    # Datasets
    "hf-upstream-check": ("app.jobs.hf_upstream_check", "run_hf_upstream_check"),
    # Health
    "wallet-health": ("app.jobs.wallet_health_check", "run_wallet_health_check"),
    "proxy-health": ("app.jobs.proxy_health_check", "run_proxy_health_check"),
    "db-monitor": ("app.jobs.db_size_monitor", "run_db_size_monitor"),
    "site-availability": ("app.jobs.site_availability_monitor", "run_site_availability_monitor"),
    "site-availability": ("app.jobs.site_availability_monitor", "run_site_availability_monitor"),
    "cleanup": ("app.jobs.cleanup", "run_storage_cleanup"),
    "vacuum-markets": ("app.jobs.vacuum_maintenance", "run_vacuum_maintenance"),
    "orderbook-snapshots-cleanup": (
        "app.jobs.orderbook_snapshots_cleanup",
        "run_orderbook_snapshots_cleanup",
    ),
    "refresh-tier2-mvs": ("app.jobs.refresh_tier2_mvs", "run_refresh_tier2_mvs"),
    "refresh-market-calibration": ("app.jobs.refresh_market_calibration", "run_refresh_market_calibration"),
    # Withdrawal flow (Phase 1 Stage 7) — monthly 1-го числа 09:00 UTC.
    # Default: WITHDRAWAL_ENABLED=false → run_propose_withdrawal returns "disabled".
    "withdrawal-propose": ("app.jobs.withdrawal_propose", "run_propose_withdrawal"),
    # Ramp-лесенка (Phase 3 Task 2, audit research-gap) — daily.
    "ramp-check": ("app.jobs.ramp_check", "run_ramp_check"),
    # Portfolio Analyst Agent Phase 0 (Task 10) — daily-бриф + monthly owner's report.
    "agent-daily-brief": ("app.jobs.agent_daily_brief", "run_agent_daily_brief"),
    "agent-owners-report": ("app.jobs.agent_owners_report", "run_agent_owners_report"),
}


async def run_job(command: str) -> dict:
    """Import and run a job function by command name.

    Инициализирует DB pool и Redis перед запуском,
    т.к. cron_runner работает вне FastAPI lifespan.
    """
    from app.core.cache import close_redis, init_redis
    from app.core.config import get_settings
    from app.services.database import close_db, init_db

    settings = get_settings()
    if command in LARGE_POOL_JOBS:
        await init_db(min_size=5, max_size=30)
    else:
        await init_db(min_size=1, max_size=3)
    await init_redis(settings.redis_url)

    try:
        # Lazy import
        from importlib import import_module

        module = import_module(COMMANDS[command][0])
        func = getattr(module, COMMANDS[command][1])

        return await func()
    finally:
        await close_db()
        await close_redis()


# Окно дедупликации фейлов: повторный фейл одного job в пределах этого окна
# = реальная проблема → алерт. Первый фейл за окно молчит (шумоподавление
# транзиентных таймаутов внешних API). 45 мин покрывает high-freq кроны
# (интервал ≤ ~20 мин); редкие кроны с бОльшим интервалом алертят сразу,
# т.к. метка протухает между запусками — для них это безопасно.
FAIL_WINDOW_SEC = 2700


async def _should_alert_for_failure(command: str) -> bool:
    """Решить, слать ли TG-алерт о фейле job (шумоподавление "2 фейла в окне").

    Атомарно ставит метку `cron:fail_seen:{command}` через SET NX EX:
    - метка только что создана (первый фейл за окно) → False (молчим);
    - метка уже была (повторный фейл в пределах окна) → True (алертим).

    Fail-open: при недоступности Redis возвращает True — лучше лишний алерт,
    чем проглоченная реальная авария.
    """
    try:
        from app.core.cache import get_redis

        redis = await get_redis()
        if redis is None:
            return True
        first_in_window = await redis.set(f"cron:fail_seen:{command}", "1", nx=True, ex=FAIL_WINDOW_SEC)
        return not first_in_window
    except Exception:
        return True


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print(__doc__)
        print("Available commands:")
        for cmd in sorted(COMMANDS):
            cat, desc = JOB_DESCRIPTIONS.get(cmd, ("⚙️", ""))
            print(f"  {cat} {cmd:30s} {desc}")
        sys.exit(0)

    command = sys.argv[1]

    if command not in COMMANDS:
        print(f"Unknown command: {command}")
        print(f"Available: {', '.join(sorted(COMMANDS))}")
        sys.exit(1)

    logger.info(f"Starting job: {command}")
    start = time.time()

    try:
        result = asyncio.run(run_job(command))
        elapsed = round(time.time() - start, 2)
        logger.info(f"Job {command} completed in {elapsed}s")
        print(json.dumps({"status": "ok", "command": command, "elapsed": elapsed, "result": result}, default=str))
        sys.exit(0)
    except Exception as e:
        elapsed = round(time.time() - start, 2)
        logger.exception(f"Job {command} failed after {elapsed}s")
        print(json.dumps({"status": "error", "command": command, "elapsed": elapsed, "error": str(e)}, default=str))
        # TG alert
        import traceback as _tb

        err_text = f"{type(e).__name__}: {e}".strip(": ")
        if not str(e):
            err_text = f"{type(e).__name__} (no message)\n" + _tb.format_exc()
        try:

            async def _alert():
                from app.core.cache import close_redis, init_redis
                from app.core.config import get_settings
                from app.services.database import close_db, init_db

                settings = get_settings()
                await init_db(min_size=1, max_size=1)
                await init_redis(settings.redis_url)
                try:
                    # Шумоподавление: первый фейл за окно молчит, алертим только
                    # на повторный в пределах FAIL_WINDOW_SEC. Решение вынесено в
                    # _should_alert_for_failure() (тестируемо, fail-open на Redis).
                    if not await _should_alert_for_failure(command):
                        logger.info(f"Job {command} failed — first in {FAIL_WINDOW_SEC}s window, suppressing alert")
                        return

                    from app.tasks.notifications import send_admin_message

                    category, description = JOB_DESCRIPTIONS.get(command, ("⚙️ Unknown", ""))
                    await send_admin_message(
                        f"❌ <b>Cron job failed: {command}</b>\n"
                        f"<b>Категория:</b> {category}\n"
                        f"<b>Что это:</b> {description}\n"
                        f"<b>Прошло секунд:</b> {elapsed}\n"
                        f"<b>Ошибка:</b> <code>{err_text[:1500]}</code>\n"
                        f"<i>(повторный фейл в окне {FAIL_WINDOW_SEC // 60} мин)</i>"
                    )
                finally:
                    await close_db()
                    await close_redis()

            asyncio.run(_alert())
        except Exception:
            pass

        sys.exit(1)


if __name__ == "__main__":
    main()

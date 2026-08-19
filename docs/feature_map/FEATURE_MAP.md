# OraclePoly Feature Map 🗺️

*Автоматически сгенерированная карта функционала и зависимостей проекта.*

## 🔥 Критичные компоненты (High Impact)
Эти файлы импортируются чаще всего. Изменения в них затрагивают значительную часть кодовой базы.

| Файл | От него зависят | Последнее изменение |
|---|---|---|
| `app/services/database.py` | **110** файлов | 2026-04-16 |
| `client/src/hooks/useBot.ts` | **50** файлов | 2026-07-02 |
| `app/core/config.py` | **48** файлов | 2026-07-05 |
| `app/core/cache.py` | **29** файлов | 2026-04-26 |
| `app/detectors/contract.py` | **28** файлов | 2026-04-30 |
| `client/src/utils/share.ts` | **19** файлов | 2026-02-19 |
| `app/detectors/token_resolver.py` | **17** файлов | 2026-06-10 |
| `app/detectors/__init__.py` | **15** файлов | 2026-06-20 |
| `app/tasks/notifications.py` | **13** файлов | 2026-05-21 |
| `app/api/telegram.py` | **13** файлов | 2026-07-02 |
| `app/repositories/detector_signal_repository.py` | **13** файлов | 2026-05-04 |
| `app/core/task_queue.py` | **11** файлов | 2026-03-28 |
| `app/services/detector_helpers.py` | **11** файлов | 2026-05-13 |
| `app/calibration/beta_calibrator.py` | **9** файлов | 2026-04-30 |
| `app/services/live_prices.py` | **8** файлов | 2026-07-04 |
| `app/services/polymarket_poller_service.py` | **8** файлов | 2026-07-03 |
| `app/services/fee_calculator.py` | **7** файлов | 2026-05-21 |
| `app/services/polymarket_service.py` | **7** файлов | 2026-04-29 |
| `app/core/retry.py` | **7** файлов | 2026-05-24 |
| `app/services/wallet_resolver.py` | **6** файлов | 2026-06-28 |

## 🧊 Забытый функционал (Stale Code)
Эти файлы давно не обновлялись (более 30 дней). Они либо гипер-стабильны, либо стали legacy/мертвым кодом.

| Файл | Последнее обновление | Суть последнего коммита |
|---|---|---|
| `app/core/__init__.py` | 2026-02-12 | 🚀 Initial commit: Autonomous OraclePoly with Docker & Innges... |
| `app/api/__init__.py` | 2026-02-12 | 🚀 Initial commit: Autonomous OraclePoly with Docker & Innges... |
| `app/jobs/__init__.py` | 2026-02-12 | 🚀 Initial commit: Autonomous OraclePoly with Docker & Innges... |
| `app/handlers/__init__.py` | 2026-02-12 | 🚀 Initial commit: Autonomous OraclePoly with Docker & Innges... |
| `app/services/__init__.py` | 2026-02-12 | 🚀 Initial commit: Autonomous OraclePoly with Docker & Innges... |
| `app/core/logging.py` | 2026-02-14 | feat: add Polymarket trade poller service with market resolu... |
| `client/src/components/ui/Badge.tsx` | 2026-02-15 | refactor(design): replace all hardcoded semantic colors with... |
| `client/src/main.tsx` | 2026-02-16 | feat: integrate Sentry error tracking (backend + frontend) |
| `client/src/utils/haptics.ts` | 2026-02-17 | feat: add PWA manifest and Telegram haptic feedback |
| `app/core/categories.py` | 2026-02-18 | fix: eliminate General fallback — all markets auto-classify ... |
| `client/src/utils/share.ts` | 2026-02-19 | polish: Profile tab round 2 — 13 visual/UX improvements |
| `app/core/parsing.py` | 2026-02-24 | refactor: устранить дублирование функционала (13 задач) (#12... |
| `app/core/validation.py` | 2026-02-24 | refactor: устранить дублирование функционала (13 задач) (#12... |
| `app/repositories/base.py` | 2026-02-24 | refactor: устранить дублирование функционала (13 задач) (#12... |
| `app/tasks/__init__.py` | 2026-02-26 | refactor: переименовать app/inngest/ → app/tasks/ (убрать le... |
| `client/src/lib/utils.ts` | 2026-03-10 | feat: UI редизайн — Mood Mode дизайн-система, shadcn/ui, эфф... |
| `client/src/components/ui/GlassCard.tsx` | 2026-03-10 | feat: UI редизайн — Mood Mode дизайн-система, shadcn/ui, эфф... |
| `client/src/components/ui/SemanticSkeleton.tsx` | 2026-03-10 | feat: UI редизайн — Mood Mode дизайн-система, shadcn/ui, эфф... |
| `app/core/circuit_breaker.py` | 2026-03-10 | feat: редизайн Admin Analytics + build fixes (#163) |
| `app/repositories/transaction_repository.py` | 2026-03-10 | feat: редизайн Admin Analytics + build fixes (#163) |

## 🎯 Ключевые Бизнес-Домены
Функционал, сгруппированный по смысловым сегментами.


### 🧠 Основная Формула (Oracle Score)
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/core/scoring_config_loader.py` | 2026-04-20 | 2026-06-14 | chore(scoring): w_maker_direction/w_taker_imbalanc... |
| `app/core/scoring.py` | 2026-02-12 | 2026-04-29 | chore(scripts): cleanup и реорганизация — research... |
| `app/core/scoring_pure.py` | 2026-04-20 | 2026-04-20 | feat(scoring): Phase 2 — Pure Oracle scoring + sha... |
| `app/services/shadow_scoring.py` | 2026-04-20 | 2026-04-20 | feat(scoring): Phase 2 — Pure Oracle scoring + sha... |
| `app/services/calibration_service.py` | 2026-02-23 | 2026-04-13 | feat(oracle): maker_direction coverage 3x + вес 0.... |
| `app/core/cluster_classifier.py` | 2026-03-25 | 2026-03-25 | fix: ClusterClassifier v3 — верифицированная looku... |
| `app/services/whale_segment_classifier.py` | 2026-03-14 | 2026-03-14 | feat: Whale Segment Simulation — 6 поведенческих с... |
| `app/services/whale_scoring.py` | 2026-03-13 | 2026-03-13 | refactor: добавить ValueError для unknown pipeline... |

### 🐋 Отслеживание Китов (Whale Tracking, backend-only)
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/handlers/whale_watcher.py` | 2026-02-12 | 2026-06-21 | feat(m2): whale_watcher ставит m2:whale_seen-флаг ... |
| `app/services/whale_sweep_service.py` | 2026-05-13 | 2026-05-14 | fix(whale-sweep): case-insensitive outcome + relax... |
| `app/handlers/whale_analytics.py` | 2026-02-23 | 2026-04-30 | Phase F: legacy code cleanup (-21,709 LOC, ~30% co... |
| `app/services/whale_style_classifier.py` | 2026-03-22 | 2026-03-22 | feat: whale style axis + market impact score + sco... |
| `app/core/whale_tiers.py` | 2026-03-12 | 2026-03-12 | feat: модуль 8-уровневых тиров китов с тестами |

### 🎯 Детекторы: Arbitrage & Basket
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/jobs/d29_scan.py` | 2026-05-13 | 2026-07-02 | fix: аудит дизайн-решений — латентность, риск-слой... |
| `app/jobs/d3_scan.py` | 2026-04-30 | 2026-07-02 | fix: аудит дизайн-решений — латентность, риск-слой... |
| `app/services/detector_metrics.py` | 2026-05-05 | 2026-07-02 | fix: аудит дизайн-решений — латентность, риск-слой... |
| `app/jobs/d36_scan.py` | 2026-05-13 | 2026-06-20 | chore(detectors): снести 7 убыточных детекторов (D... |
| `app/detectors/d36_obi_pressure.py` | 2026-05-13 | 2026-05-22 | refactor(detector_state): cleanup paused→shadow + ... |
| `app/detectors/d3_cross_mkt_arb.py` | 2026-04-30 | 2026-05-20 | feat(detectors): tighten D3 + D36 filters к sweet-... |
| `app/detectors/d29_negrisk_basket_arb.py` | 2026-05-13 | 2026-05-14 | fix(detectors): ruff-format reflow для current_pri... |
| `app/services/detector_helpers.py` | 2026-05-05 | 2026-05-13 | feat(D_meta_consensus): Composite Strategy Layer +... |
| `app/jobs/negrisk_sync.py` | 2026-04-30 | 2026-04-30 | feat(negrisk-sync): обогащение events neg_risk фла... |

### 🔀 Детекторы: Lead-Lag & Divergence
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/detectors/d42_event_driven_ml.py` | 2026-05-14 | 2026-06-21 | feat(d42): отрезать YES-плечо — эмитить только NO ... |
| `app/detectors/d45_chainlink_leadlag.py` | 2026-05-21 | 2026-06-10 | fix(detectors): token-side инверсия в D3 и d45_cha... |
| `app/jobs/d45_chainlink_leadlag_scan.py` | 2026-05-21 | 2026-06-10 | fix(detectors): token-side инверсия в D3 и d45_cha... |
| `app/jobs/d42_scan.py` | 2026-06-10 | 2026-06-10 | fix(detectors): token-side инверсия в D3 и d45_cha... |
| `app/detectors/d44_late_convergence_carry.py` | 2026-05-20 | 2026-06-03 | refactor(D44): убрать sports из CATEGORY_WHITELIST |
| `app/jobs/d45_slo_monitor.py` | 2026-05-21 | 2026-05-24 | fix(slo): три алёрта pipeline-aligned (D45 port + ... |
| `app/detectors/d45_lead_lag_cross_venue.py` | 2026-05-20 | 2026-05-22 | refactor(detector_state): cleanup paused→shadow + ... |
| `app/services/binance_stream_service.py` | 2026-05-21 | 2026-05-21 | feat(d45): defensive hardening — Binance backup WS... |
| `app/core/d45_loop.py` | 2026-05-21 | 2026-05-21 | feat(d45): Stage 3 — lifespan integration (Pyth st... |
| `app/services/pyth_history_service.py` | 2026-05-21 | 2026-05-21 | feat(d45): Stage 2 — cron scan job для chainlink l... |
| `app/services/pyth_stream_service.py` | 2026-05-21 | 2026-05-21 | feat(d45): Pyth Hermes SSE service для chainlink-r... |
| `app/jobs/d45_lead_lag_scan.py` | 2026-05-20 | 2026-05-20 | feat(detectors): D45 lead-lag cross-venue (Polymar... |
| `app/jobs/late_convergence_scan.py` | 2026-05-20 | 2026-05-20 | feat(detectors): D44 late_convergence_carry — Phas... |

### 🐢 Детекторы: Retail & Whale Behaviour
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/jobs/d13_scan.py` | 2026-05-03 | 2026-06-03 | fix: нормализовать wallet lookup через LOWER(walle... |
| `app/detectors/d14_crypto_winners_distribute_no.py` | 2026-05-03 | 2026-05-28 | fix(detectors): расширить окно D14 до [4,72]h — 4h... |
| `app/jobs/d14_scan.py` | 2026-05-03 | 2026-05-28 | fix(detectors): расширить окно D14 до [4,72]h — 4h... |
| `app/detectors/d38_resolution_lag.py` | 2026-05-14 | 2026-05-19 | feat(d38): tighten NO ceiling 0.20→0.15 (drop losi... |
| `app/detectors/d26_fade_whale_extreme.py` | 2026-05-13 | 2026-05-14 | fix(detectors): ruff-format reflow для current_pri... |
| `app/detectors/d13_retail_confidence_fade.py` | 2026-05-03 | 2026-05-14 | fix(detectors): ruff-format reflow для current_pri... |
| `app/jobs/d38_scan.py` | 2026-05-14 | 2026-05-14 | feat(D38): ResolutionLag detector — fade pre-resol... |
| `app/jobs/d26_scan.py` | 2026-05-13 | 2026-05-13 | feat(D_meta_consensus): Composite Strategy Layer +... |
| `app/jobs/auto_edge_scanner.py` | 2026-05-03 | 2026-05-03 | feat(research): Auto Edge Scanner — weekly hypothe... |

### 🌡️ M2 Liquidity Vacuum (Measurement Instrument)
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/detectors/m2_liquidity_vacuum.py` | 2026-06-21 | 2026-06-21 | fix(m2): коэрция event_id UUID→str в detect() — as... |
| `app/detectors/m2_vacuum_hook.py` | 2026-06-21 | 2026-06-21 | feat(m2): WSS-хук — depletion_check O(1) + fire-an... |

### 🇰 Kalshi Cross-Platform
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/jobs/kalshi_orderbook_sync.py` | 2026-05-14 | 2026-07-05 | fix: гигиена пайплайна данных — рекью WS-батча, ал... |
| `app/jobs/kalshi_market_sync.py` | 2026-05-14 | 2026-07-05 | fix: гигиена пайплайна данных — рекью WS-батча, ал... |
| `app/services/kalshi_service.py` | 2026-05-14 | 2026-07-05 | fix: гигиена пайплайна данных — рекью WS-батча, ал... |
| `app/jobs/d40_kalshi_cross_platform_scan.py` | 2026-05-14 | 2026-05-21 | feat(fees): maker rebate в fee_calculator + DRY pa... |
| `app/detectors/d40_kalshi_cross_platform.py` | 2026-05-14 | 2026-05-21 | feat(fees): подключить per-market fee_schedule во ... |
| `app/matchers/kalshi_polymarket_matcher.py` | 2026-05-14 | 2026-05-14 | feat(D40): Kalshi cross-platform arbitrage detecto... |
| `app/jobs/kalshi_polymarket_match.py` | 2026-05-14 | 2026-05-14 | feat(D40): Kalshi cross-platform arbitrage detecto... |

### 💰 Live Execution (Real Money)
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/jobs/v2_execution.py` | 2026-05-01 | 2026-07-05 | fix: гигиена пайплайна данных — рекью WS-батча, ал... |
| `app/jobs/v2_execution_resolve.py` | 2026-05-01 | 2026-07-05 | fix(settle): live-ордера settle только по резолву ... |
| `app/jobs/redeem_job.py` | 2026-07-05 | 2026-07-05 | feat: P&L-дайджест в redeem-уведомлении (#642) |
| `app/services/execution_safety.py` | 2026-05-01 | 2026-07-05 | fix(execution): decide_mode получает реальную глуб... |
| `app/jobs/mtm_refresh.py` | 2026-05-20 | 2026-07-04 | fix(ws): REST-посев книг позиций — live-цена для р... |
| `app/jobs/position_manager.py` | 2026-05-13 | 2026-07-03 | Live exit: устранение race condition, timeout-алер... |
| `app/services/live_exit.py` | 2026-07-02 | 2026-07-03 | Live exit: устранение race condition, timeout-алер... |
| `app/services/clob_order_lookup.py` | 2026-07-02 | 2026-07-02 | fix: аудит дизайн-решений — латентность, риск-слой... |
| `app/services/basket_execution.py` | 2026-07-02 | 2026-07-02 | fix: аудит дизайн-решений — латентность, риск-слой... |
| `app/jobs/execution_reconcile.py` | 2026-04-07 | 2026-06-29 | fix(risk): авто-снятие sticky kill_switch для tran... |
| `app/services/exit_policy_sweep.py` | 2026-05-29 | 2026-05-29 | feat(diagnostics): virtual-feedback reconciliation... |
| `app/jobs/withdrawal_propose.py` | 2026-05-21 | 2026-05-21 | feat(risk): withdrawal flow skeleton — propose + T... |
| `app/services/clob_client_protocol.py` | 2026-05-20 | 2026-05-20 | feat(risk): live drill + mock client + recovery sc... |
| `app/services/clob_factory.py` | 2026-05-20 | 2026-05-20 | feat(risk): live drill + mock client + recovery sc... |
| `app/jobs/wallet_health_check.py` | 2026-04-29 | 2026-05-13 | fix: устраняем bugs из cron логов |
| `app/jobs/reconcile_orphans.py` | 2026-03-12 | 2026-03-12 | fix: очистка данных китов и orphan-транзакций |

### 🚦 Risk / Gates система
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/services/gate_shadow_telemetry.py` | 2026-07-03 | 2026-07-03 | feat(execution): shadow-телеметрия для category_bl... |
| `app/services/price_staleness_telemetry.py` | 2026-07-03 | 2026-07-03 | feat(telemetry): shadow-замер расхождения db-цены ... |
| `app/core/detector_state.py` | 2026-05-01 | 2026-07-02 | fix: аудит дизайн-решений — латентность, риск-слой... |
| `app/services/reject_classify.py` | 2026-07-02 | 2026-07-02 | fix: аудит дизайн-решений — латентность, риск-слой... |
| `app/services/kill_switch_clear.py` | 2026-06-29 | 2026-07-02 | fix: аудит дизайн-решений — латентность, риск-слой... |
| `app/core/sizing_controller.py` | 2026-05-01 | 2026-06-20 | chore(detectors): снести 7 убыточных детекторов (D... |
| `app/services/promote_gates.py` | 2026-05-20 | 2026-06-04 | feat: G2 fill rate использует effective_fill_count... |
| `app/services/risk_engine.py` | 2026-03-11 | 2026-05-20 | feat(risk): per-bet 5% cap + min stake $25 floor (... |
| `app/core/horizon_limits.py` | 2026-05-03 | 2026-05-03 | feat: per-notional ROI + horizon cap 30d + D3/D10b... |
| `app/core/kelly.py` | 2026-03-24 | 2026-04-05 | cleanup: удалить неиспользуемую _FEE_RATE из kelly... |

### 🧪 ML Pipeline V2 (Two-Stage)
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/calibration/lgb_calibrator.py` | 2026-05-19 | 2026-07-05 | refactor(calibration): единый модуль fallback chai... |
| `app/calibration/fallback_chain.py` | 2026-07-05 | 2026-07-05 | refactor(calibration): единый модуль fallback chai... |
| `app/calibration/meta_labeler.py` | 2026-04-30 | 2026-06-20 | chore(detectors): снести 7 убыточных детекторов (D... |
| `app/calibration/market_picker.py` | 2026-05-01 | 2026-06-20 | chore(detectors): снести 7 убыточных детекторов (D... |
| `app/services/g18_bin_calibration.py` | 2026-05-25 | 2026-06-20 | chore(detectors): снести 7 убыточных детекторов (D... |
| `app/jobs/ml_market_picker_fit.py` | 2026-05-01 | 2026-06-15 | feat(ml-obs): наблюдаемость frozen-by-gate для L2/... |
| `app/jobs/ml_event_selector_fit.py` | 2026-05-01 | 2026-06-15 | feat(ml-obs): наблюдаемость frozen-by-gate для L2/... |
| `app/jobs/ml_meta_labeler_fit.py` | 2026-04-30 | 2026-06-15 | feat(ml-obs): наблюдаемость frozen-by-gate для L2/... |
| `app/jobs/model_drift_check.py` | 2026-04-30 | 2026-06-15 | feat(ml-obs): наблюдаемость frozen-by-gate для L2/... |
| `app/jobs/ml_honest_baseline_fit.py` | 2026-05-01 | 2026-06-14 | feat(ml): Tier 3.1 фичи в meta_labeler (hours_to_r... |
| `app/api/calibration.py` | 2026-02-23 | 2026-06-13 | fix(calibration): /by-category 500 — ABS(alias) в ... |
| `app/jobs/refresh_market_calibration.py` | 2026-06-13 | 2026-06-13 | feat(calibration): инфра честной калибровки рынка ... |
| `app/jobs/calibration_bins_refit.py` | 2026-05-25 | 2026-06-11 | G18: refit чистка killed + D37 в whitelist (shadow... |
| `app/jobs/ml_calibrator_lgb_fit.py` | 2026-05-19 | 2026-06-02 | fix(ml): group-aware split в LGB-калибраторе — уст... |
| `app/calibration/lgb_features.py` | 2026-05-19 | 2026-06-02 | feat(ml): per-anchor калибровка STANDALONE_V2_LGB ... |
| `app/jobs/ml_calibrator_fit.py` | 2026-04-30 | 2026-06-02 | feat(ml): per-anchor калибровка STANDALONE_V2_LGB ... |
| `app/jobs/ml_accuracy_snapshot.py` | 2026-06-02 | 2026-06-02 | feat(ml): инструмент измерения точности ML — 3 сло... |
| `app/calibration/ev_sizer.py` | 2026-04-30 | 2026-05-21 | feat(fees): подключить per-market fee_schedule во ... |
| `app/jobs/ml_market_picker_ev_fit.py` | 2026-05-01 | 2026-05-14 | fix(ml-ev-regressor): include backfilled events (s... |
| `app/jobs/refresh_mv_event_outcome.py` | 2026-05-01 | 2026-05-13 | fix: устраняем bugs из cron логов |
| `app/ml/feature_kernel.py` | 2026-05-05 | 2026-05-05 | fix(feature-kernel): Postgres column projection (y... |
| `app/jobs/live_ml_features_builder.py` | 2026-05-05 | 2026-05-05 | feat(ml): live_ml_features_builder + feature_parit... |
| `app/jobs/pipeline_v2_shadow_predictor.py` | 2026-05-01 | 2026-05-01 | V2 fee-aware sizing + EV-regressor (Shadow A/B) (#... |
| `app/jobs/pipeline_v2_drift_check.py` | 2026-05-01 | 2026-05-01 | feat(stages-5-5.5-6): shadow validation + drift mo... |
| `app/jobs/pipeline_v2_outcome_sync.py` | 2026-05-01 | 2026-05-01 | feat(stages-5-5.5-6): shadow validation + drift mo... |
| `app/jobs/pipeline_v2_comparison_report.py` | 2026-05-01 | 2026-05-01 | feat(stages-5-5.5-6): shadow validation + drift mo... |
| `app/calibration/event_selector.py` | 2026-05-01 | 2026-05-01 | feat(stage-2): Layer A Event Selector — модель + j... |
| `app/calibration/per_event_metrics.py` | 2026-05-01 | 2026-05-01 | feat(stage-1): honest baseline с GroupKFold + samp... |
| `app/calibration/event_aware_split.py` | 2026-05-01 | 2026-05-01 | feat(stage-1): honest baseline с GroupKFold + samp... |
| `app/calibration/beta_calibrator.py` | 2026-04-30 | 2026-04-30 | feat(calibration): detector_signals_resolve cron +... |

### 🪜 Detector Lifecycle & Promotion
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/services/detector_signal_service.py` | 2026-04-30 | 2026-07-05 | refactor(calibration): detector_signal_service на ... |
| `app/jobs/virtual_weekly_digest.py` | 2026-07-05 | 2026-07-05 | feat: TG-уведомления по сделкам (live per-trade + ... |
| `app/jobs/detector_signals_resolve.py` | 2026-04-30 | 2026-07-03 | fix(resolve): D3 horizon multiplier — не EXPIRE-ит... |
| `app/jobs/demote_alerts_monitor.py` | 2026-05-28 | 2026-07-02 | fix: аудит дизайн-решений — латентность, риск-слой... |
| `app/jobs/ramp_check.py` | 2026-07-02 | 2026-07-02 | fix: аудит дизайн-решений — латентность, риск-слой... |
| `app/jobs/detector_slo_monitor.py` | 2026-05-04 | 2026-06-20 | chore(detectors): снести 7 убыточных детекторов (D... |
| `app/jobs/detectors_honest_audit_report.py` | 2026-06-15 | 2026-06-19 | feat(monitoring): honest-audit — категорийный brea... |
| `app/jobs/virtual_promote_report.py` | 2026-06-03 | 2026-06-03 | fix: экранировать < как &lt; в blocker строках для... |
| `app/services/detector_stage_reconciliation.py` | 2026-05-29 | 2026-05-29 | fix: demote-алерт судит по signal-level realized_r... |

### 📢 Telegram Notifications
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/api/telegram.py` | 2026-02-12 | 2026-07-02 | fix: аудит дизайн-решений — латентность, риск-слой... |
| `app/tasks/notifications.py` | 2026-02-26 | 2026-05-21 | feat(risk): withdrawal flow skeleton — propose + T... |
| `app/core/telegram.py` | 2026-02-12 | 2026-03-28 | fix(security): комплексный security audit — 31 уяз... |

### 🧹 Data Pipeline Hygiene & Monitoring
| Файл | Создан | Обновлен | Суть последнего коммита |
|---|---|---|---|
| `app/jobs/db_size_monitor.py` | 2026-04-16 | 2026-07-03 | chore(monitoring): убрать пустую oracle_snapshots ... |
| `app/jobs/cleanup.py` | 2026-02-12 | 2026-07-03 | chore(telemetry): retention 30д для price_stalenes... |
| `app/jobs/polymarket_api_health.py` | 2026-05-24 | 2026-07-02 | fix: аудит дизайн-решений — латентность, риск-слой... |
| `app/jobs/vacuum_maintenance.py` | 2026-06-28 | 2026-06-28 | fix(ops): VACUUM FULL markets — лечение рецидива D... |
| `app/jobs/db_size_report.py` | 2026-06-15 | 2026-06-15 | feat(monitoring): месячные TG-напоминания — чеклис... |
| `app/jobs/canonical_event_generator.py` | 2026-06-05 | 2026-06-05 | feat(insights): автогенератор canonical_events из ... |
| `app/jobs/proxy_health_check.py` | 2026-04-05 | 2026-05-24 | fix(slo): три алёрта pipeline-aligned (D45 port + ... |
| `app/jobs/orderbook_snapshots_cleanup.py` | 2026-05-21 | 2026-05-21 | feat(data): tier-2 unique data — Gamma fields + Ma... |
| `app/jobs/hf_upstream_check.py` | 2026-04-20 | 2026-04-30 | Pipeline V2 roadmap: phases A-D (cleanup + dataset... |

## 📂 Подробная карта всех модулей

### `app/api`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `telegram.py` | 2026-02-12 | 2026-07-02 | 8 | **13** |
| `bot.py` | 2026-03-28 | 2026-07-02 | **14** | 0 |
| `tier2.py` | 2026-05-21 | 2026-06-15 | 2 | 0 |
| `calibration.py` | 2026-02-23 | 2026-06-13 | 2 | 0 |
| `d45.py` | 2026-05-21 | 2026-05-21 | 5 | 0 |
| `auth.py` | 2026-05-06 | 2026-05-11 | 3 | 0 |
| `__init__.py` | 2026-02-12 | 2026-02-12 | 0 | 1 |

### `app/backtest`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `schema.py` | 2026-05-20 | 2026-06-24 | 0 | 5 |
| `detector_adapter.py` | 2026-05-20 | 2026-06-20 | 3 | 1 |
| `data_loader.py` | 2026-05-20 | 2026-06-14 | 1 | 1 |
| `replay_engine.py` | 2026-05-20 | 2026-05-28 | 5 | 0 |
| `feature_builder.py` | 2026-05-21 | 2026-05-28 | 0 | 1 |
| `pre_filter.py` | 2026-05-25 | 2026-05-25 | 0 | 1 |
| `metrics.py` | 2026-05-20 | 2026-05-24 | 3 | 2 |
| `report.py` | 2026-05-20 | 2026-05-24 | 1 | 0 |
| `__init__.py` | 2026-05-20 | 2026-05-20 | 2 | 0 |

### `app/calibration`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `lgb_calibrator.py` | 2026-05-19 | 2026-07-05 | 0 | 2 |
| `fallback_chain.py` | 2026-07-05 | 2026-07-05 | 6 | 3 |
| `meta_labeler.py` | 2026-04-30 | 2026-06-20 | 1 | 5 |
| `market_picker.py` | 2026-05-01 | 2026-06-20 | 2 | 4 |
| `lgb_features.py` | 2026-05-19 | 2026-06-02 | 0 | 2 |
| `ev_sizer.py` | 2026-04-30 | 2026-05-21 | 2 | 2 |
| `validation.py` | 2026-05-02 | 2026-05-02 | 0 | 3 |
| `event_selector.py` | 2026-05-01 | 2026-05-01 | 0 | 4 |
| `per_event_metrics.py` | 2026-05-01 | 2026-05-01 | 0 | **6** |
| `event_aware_split.py` | 2026-05-01 | 2026-05-01 | 0 | 3 |
| `__init__.py` | 2026-04-30 | 2026-04-30 | 3 | 0 |
| `beta_calibrator.py` | 2026-04-30 | 2026-04-30 | 0 | **9** |

### `app/core`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `config.py` | 2026-02-12 | 2026-07-05 | 0 | **48** |
| `peak_bank.py` | 2026-05-20 | 2026-07-04 | 1 | 2 |
| `detector_state.py` | 2026-05-01 | 2026-07-02 | 1 | 3 |
| `sizing_controller.py` | 2026-05-01 | 2026-06-20 | 2 | 2 |
| `scoring_config_loader.py` | 2026-04-20 | 2026-06-14 | 2 | 1 |
| `embeddings.py` | 2026-02-12 | 2026-06-04 | 1 | 3 |
| `retry.py` | 2026-02-12 | 2026-05-24 | 1 | **7** |
| `d45_loop.py` | 2026-05-21 | 2026-05-21 | 2 | 1 |
| `signal_helpers.py` | 2026-04-13 | 2026-05-21 | 1 | 0 |
| `web_auth.py` | 2026-05-06 | 2026-05-21 | 0 | 2 |
| `auth.py` | 2026-02-16 | 2026-05-21 | 3 | 5 |
| `heartbeat.py` | 2026-05-20 | 2026-05-20 | 2 | 1 |
| `open_positions.py` | 2026-05-20 | 2026-05-20 | 2 | 1 |
| `bankroll_mtm.py` | 2026-05-20 | 2026-05-20 | 0 | 1 |
| `horizon_limits.py` | 2026-05-03 | 2026-05-03 | 0 | 1 |
| `slippage.py` | 2026-04-13 | 2026-05-02 | 0 | 5 |
| `scoring.py` | 2026-02-12 | 2026-04-29 | 3 | 2 |
| `liquidity.py` | 2026-04-28 | 2026-04-28 | 0 | 0 |
| `cache.py` | 2026-02-17 | 2026-04-26 | 0 | **29** |
| `entry_price.py` | 2026-04-13 | 2026-04-25 | 0 | 0 |
| `simulation_defaults.py` | 2026-03-19 | 2026-04-22 | 1 | 0 |
| `scoring_pure.py` | 2026-04-20 | 2026-04-20 | 1 | 1 |
| `signal_router.py` | 2026-04-13 | 2026-04-13 | 0 | 0 |
| `hf_bias.py` | 2026-04-13 | 2026-04-13 | 0 | 0 |
| `kelly.py` | 2026-03-24 | 2026-04-05 | 0 | 0 |
| `task_queue.py` | 2026-02-24 | 2026-03-28 | 1 | **11** |
| `telegram.py` | 2026-02-12 | 2026-03-28 | 1 | 1 |
| `cluster_classifier.py` | 2026-03-25 | 2026-03-25 | 0 | 0 |
| `bank_manager.py` | 2026-03-16 | 2026-03-16 | 0 | 0 |
| `constants.py` | 2026-03-15 | 2026-03-15 | 0 | 0 |
| `sanitizers.py` | 2026-02-26 | 2026-03-14 | 0 | 0 |
| `llm.py` | 2026-02-24 | 2026-03-14 | 1 | 0 |
| `rate_limiter.py` | 2026-02-12 | 2026-03-12 | 1 | 4 |
| `whale_tiers.py` | 2026-03-12 | 2026-03-12 | 0 | 1 |
| `circuit_breaker.py` | 2026-02-12 | 2026-03-10 | 0 | 3 |
| `parsing.py` | 2026-02-24 | 2026-02-24 | 0 | 0 |
| `validation.py` | 2026-02-24 | 2026-02-24 | 0 | 0 |
| `categories.py` | 2026-02-18 | 2026-02-18 | 0 | 1 |
| `logging.py` | 2026-02-12 | 2026-02-14 | 0 | 1 |
| `__init__.py` | 2026-02-12 | 2026-02-12 | 0 | 1 |

### `app/detectors`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `m2_liquidity_vacuum.py` | 2026-06-21 | 2026-06-21 | 2 | 0 |
| `m2_vacuum_hook.py` | 2026-06-21 | 2026-06-21 | 7 | 1 |
| `d42_event_driven_ml.py` | 2026-05-14 | 2026-06-21 | 2 | 0 |
| `__init__.py` | 2026-04-30 | 2026-06-20 | 2 | **15** |
| `d45_chainlink_leadlag.py` | 2026-05-21 | 2026-06-10 | 2 | 0 |
| `token_resolver.py` | 2026-05-04 | 2026-06-10 | 0 | **17** |
| `d44_late_convergence_carry.py` | 2026-05-20 | 2026-06-03 | 2 | 0 |
| `d14_crypto_winners_distribute_no.py` | 2026-05-03 | 2026-05-28 | 2 | 0 |
| `d45_lead_lag_cross_venue.py` | 2026-05-20 | 2026-05-22 | 2 | 0 |
| `d36_obi_pressure.py` | 2026-05-13 | 2026-05-22 | 2 | 1 |
| `d40_kalshi_cross_platform.py` | 2026-05-14 | 2026-05-21 | 2 | 1 |
| `d3_cross_mkt_arb.py` | 2026-04-30 | 2026-05-20 | 1 | 1 |
| `d38_resolution_lag.py` | 2026-05-14 | 2026-05-19 | 2 | 0 |
| `d26_fade_whale_extreme.py` | 2026-05-13 | 2026-05-14 | 2 | 0 |
| `d13_retail_confidence_fade.py` | 2026-05-03 | 2026-05-14 | 2 | 0 |
| `d29_negrisk_basket_arb.py` | 2026-05-13 | 2026-05-14 | 2 | 0 |
| `contract.py` | 2026-04-30 | 2026-04-30 | 0 | **28** |

### `app/handlers`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `whale_watcher.py` | 2026-02-12 | 2026-06-21 | 4 | 1 |
| `whale_analytics.py` | 2026-02-23 | 2026-04-30 | 3 | 2 |
| `__init__.py` | 2026-02-12 | 2026-02-12 | 0 | 0 |

### `app/jobs`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `kalshi_orderbook_sync.py` | 2026-05-14 | 2026-07-05 | 2 | 0 |
| `kalshi_market_sync.py` | 2026-05-14 | 2026-07-05 | 2 | 0 |
| `v2_execution.py` | 2026-05-01 | 2026-07-05 | **24** | 0 |
| `v2_execution_resolve.py` | 2026-05-01 | 2026-07-05 | **12** | 0 |
| `virtual_weekly_digest.py` | 2026-07-05 | 2026-07-05 | 3 | 0 |
| `redeem_job.py` | 2026-07-05 | 2026-07-05 | 5 | 0 |
| `mtm_refresh.py` | 2026-05-20 | 2026-07-04 | 6 | 0 |
| `detector_signals_resolve.py` | 2026-04-30 | 2026-07-03 | 3 | 2 |
| `live_price_snapshot_writer.py` | 2026-05-05 | 2026-07-03 | 6 | 0 |
| `db_size_monitor.py` | 2026-04-16 | 2026-07-03 | 3 | 0 |
| `cleanup.py` | 2026-02-12 | 2026-07-03 | 1 | 0 |
| `position_manager.py` | 2026-05-13 | 2026-07-03 | 8 | 2 |
| `demote_alerts_monitor.py` | 2026-05-28 | 2026-07-02 | 5 | 0 |
| `market_sync.py` | 2026-02-12 | 2026-07-02 | 6 | 2 |
| `d29_scan.py` | 2026-05-13 | 2026-07-02 | 6 | 0 |
| `d3_scan.py` | 2026-04-30 | 2026-07-02 | 7 | 0 |
| `ramp_check.py` | 2026-07-02 | 2026-07-02 | 3 | 0 |
| `polymarket_api_health.py` | 2026-05-24 | 2026-07-02 | 5 | 0 |
| `execution_reconcile.py` | 2026-04-07 | 2026-06-29 | 6 | 0 |
| `vacuum_maintenance.py` | 2026-06-28 | 2026-06-28 | 1 | 0 |
| `detector_slo_monitor.py` | 2026-05-04 | 2026-06-20 | 4 | 0 |
| `manifold_bets_scrape.py` | 2026-05-21 | 2026-06-20 | 2 | 0 |
| `d36_scan.py` | 2026-05-13 | 2026-06-20 | 8 | 0 |
| `daily_summary.py` | 2026-05-24 | 2026-06-19 | 4 | 0 |
| `detectors_honest_audit_report.py` | 2026-06-15 | 2026-06-19 | 3 | 1 |
| `db_size_report.py` | 2026-06-15 | 2026-06-15 | 3 | 0 |
| `ml_market_picker_fit.py` | 2026-05-01 | 2026-06-15 | 6 | 0 |
| `ml_event_selector_fit.py` | 2026-05-01 | 2026-06-15 | 4 | 0 |
| `ml_meta_labeler_fit.py` | 2026-04-30 | 2026-06-15 | 4 | 0 |
| `model_drift_check.py` | 2026-04-30 | 2026-06-15 | 4 | 0 |
| `ml_honest_baseline_fit.py` | 2026-05-01 | 2026-06-14 | 5 | 0 |
| `signal_price_snapshot_writer.py` | 2026-05-28 | 2026-06-14 | 1 | 0 |
| `refresh_market_calibration.py` | 2026-06-13 | 2026-06-13 | 1 | 0 |
| `calibration_bins_refit.py` | 2026-05-25 | 2026-06-11 | 1 | 0 |
| `d45_chainlink_leadlag_scan.py` | 2026-05-21 | 2026-06-10 | 8 | 1 |
| `d42_scan.py` | 2026-06-10 | 2026-06-10 | 5 | 0 |
| `canonical_event_generator.py` | 2026-06-05 | 2026-06-05 | 3 | 0 |
| `polymarket_canonical_match.py` | 2026-06-05 | 2026-06-05 | 3 | 0 |
| `virtual_promote_report.py` | 2026-06-03 | 2026-06-03 | 3 | 0 |
| `d13_scan.py` | 2026-05-03 | 2026-06-03 | 5 | 1 |
| `ml_calibrator_lgb_fit.py` | 2026-05-19 | 2026-06-02 | 4 | 0 |
| `ml_calibrator_fit.py` | 2026-04-30 | 2026-06-02 | 2 | 0 |
| `ml_accuracy_snapshot.py` | 2026-06-02 | 2026-06-02 | 1 | 0 |
| `d14_scan.py` | 2026-05-03 | 2026-05-28 | 6 | 0 |
| `daily_price_snapshot.py` | 2026-05-25 | 2026-05-28 | 1 | 0 |
| `live_trade_bucket_writer.py` | 2026-05-05 | 2026-05-25 | 2 | 0 |
| `live_wallet_flow_writer.py` | 2026-05-05 | 2026-05-25 | 2 | 0 |
| `d45_slo_monitor.py` | 2026-05-21 | 2026-05-24 | 2 | 0 |
| `proxy_health_check.py` | 2026-04-05 | 2026-05-24 | 3 | 0 |
| `withdrawal_propose.py` | 2026-05-21 | 2026-05-21 | 3 | 0 |
| `d40_kalshi_cross_platform_scan.py` | 2026-05-14 | 2026-05-21 | 7 | 0 |
| `refresh_tier2_mvs.py` | 2026-05-21 | 2026-05-21 | 1 | 0 |
| `orderbook_snapshots_cleanup.py` | 2026-05-21 | 2026-05-21 | 1 | 0 |
| `manifold_canonical_match.py` | 2026-05-20 | 2026-05-20 | 3 | 0 |
| `manifold_scrape.py` | 2026-05-20 | 2026-05-20 | 2 | 0 |
| `d45_lead_lag_scan.py` | 2026-05-20 | 2026-05-20 | 5 | 0 |
| `late_convergence_scan.py` | 2026-05-20 | 2026-05-20 | 5 | 0 |
| `ml_market_picker_ev_fit.py` | 2026-05-01 | 2026-05-14 | 4 | 0 |
| `d38_scan.py` | 2026-05-14 | 2026-05-14 | 5 | 0 |
| `kalshi_polymarket_match.py` | 2026-05-14 | 2026-05-14 | 3 | 0 |
| `d26_scan.py` | 2026-05-13 | 2026-05-13 | 5 | 0 |
| `refresh_mv_event_outcome.py` | 2026-05-01 | 2026-05-13 | 1 | 0 |
| `wallet_health_check.py` | 2026-04-29 | 2026-05-13 | 4 | 0 |
| `live_ml_features_builder.py` | 2026-05-05 | 2026-05-05 | 2 | 0 |
| `auto_edge_scanner.py` | 2026-05-03 | 2026-05-03 | 0 | 0 |
| `pipeline_v2_shadow_predictor.py` | 2026-05-01 | 2026-05-01 | 3 | 0 |
| `pipeline_v2_drift_check.py` | 2026-05-01 | 2026-05-01 | 2 | 0 |
| `pipeline_v2_outcome_sync.py` | 2026-05-01 | 2026-05-01 | 1 | 0 |
| `pipeline_v2_comparison_report.py` | 2026-05-01 | 2026-05-01 | 3 | 0 |
| `clob_book_sync.py` | 2026-04-10 | 2026-04-30 | 2 | 0 |
| `hf_upstream_check.py` | 2026-04-20 | 2026-04-30 | 0 | 0 |
| `negrisk_sync.py` | 2026-04-30 | 2026-04-30 | 2 | 1 |
| `trade_poll.py` | 2026-02-14 | 2026-04-02 | 4 | 0 |
| `reconcile_orphans.py` | 2026-03-12 | 2026-03-12 | 1 | 0 |
| `__init__.py` | 2026-02-12 | 2026-02-12 | 0 | 0 |

### `app/main.py`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `main.py` | 2026-02-12 | 2026-06-13 | **13** | 0 |

### `app/matchers`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `polymarket_canonical_matcher.py` | 2026-06-05 | 2026-06-05 | 1 | 2 |
| `manifold_canonical_matcher.py` | 2026-05-20 | 2026-05-20 | 0 | 2 |
| `__init__.py` | 2026-05-14 | 2026-05-14 | 0 | 0 |
| `kalshi_polymarket_matcher.py` | 2026-05-14 | 2026-05-14 | 0 | 1 |

### `app/ml`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `feature_kernel.py` | 2026-05-05 | 2026-05-05 | 0 | 2 |
| `__init__.py` | 2026-05-05 | 2026-05-05 | 0 | 0 |

### `app/models`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `__init__.py` | 2026-04-20 | 2026-04-20 | 0 | 0 |
| `feature_vector.py` | 2026-04-20 | 2026-04-20 | 0 | 4 |

### `app/repositories`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `cleanup_repository.py` | 2026-02-12 | 2026-07-03 | 1 | 1 |
| `canonical_event_repository.py` | 2026-05-20 | 2026-05-20 | 1 | 1 |
| `detector_signal_repository.py` | 2026-04-30 | 2026-05-04 | 2 | **13** |
| `oracle_snapshot_repository.py` | 2026-02-22 | 2026-04-30 | 2 | 1 |
| `trade_signal_repository.py` | 2026-04-05 | 2026-04-28 | 1 | 0 |
| `maker_direction_repository.py` | 2026-04-10 | 2026-04-10 | 1 | 1 |
| `calibration_repository.py` | 2026-02-23 | 2026-04-07 | 2 | 1 |
| `weather_repository.py` | 2026-04-06 | 2026-04-06 | 1 | 0 |
| `bin_reliability_repository.py` | 2026-03-19 | 2026-03-19 | 1 | 0 |
| `event_repository.py` | 2026-02-18 | 2026-03-14 | 1 | 1 |
| `transaction_repository.py` | 2026-02-12 | 2026-03-10 | 2 | 1 |
| `base.py` | 2026-02-24 | 2026-02-24 | 1 | 3 |

### `app/services`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `two_stage_inference.py` | 2026-05-01 | 2026-07-05 | 5 | 2 |
| `detector_signal_service.py` | 2026-04-30 | 2026-07-05 | 6 | 2 |
| `kalshi_service.py` | 2026-05-14 | 2026-07-05 | 4 | 2 |
| `polymarket_ws_service.py` | 2026-02-23 | 2026-07-05 | 9 | 5 |
| `execution_safety.py` | 2026-05-01 | 2026-07-05 | 8 | 4 |
| `live_prices.py` | 2026-07-02 | 2026-07-04 | 3 | **8** |
| `gate_shadow_telemetry.py` | 2026-07-03 | 2026-07-03 | 2 | 1 |
| `virtual_execution.py` | 2026-05-01 | 2026-07-03 | 3 | 2 |
| `polymarket_poller_service.py` | 2026-02-18 | 2026-07-03 | 6 | **8** |
| `price_staleness_telemetry.py` | 2026-07-03 | 2026-07-03 | 2 | 3 |
| `live_exit.py` | 2026-07-02 | 2026-07-03 | 4 | 4 |
| `shadow_simulation.py` | 2026-05-28 | 2026-07-02 | 1 | 1 |
| `clob_order_lookup.py` | 2026-07-02 | 2026-07-02 | 0 | 2 |
| `basket_execution.py` | 2026-07-02 | 2026-07-02 | 6 | 1 |
| `detector_metrics.py` | 2026-05-05 | 2026-07-02 | 4 | **6** |
| `reject_classify.py` | 2026-07-02 | 2026-07-02 | 0 | 1 |
| `kill_switch_clear.py` | 2026-06-29 | 2026-07-02 | 0 | 1 |
| `wallet_resolver.py` | 2026-04-29 | 2026-06-28 | 2 | **6** |
| `g18_bin_calibration.py` | 2026-05-25 | 2026-06-20 | 1 | 1 |
| `snapshot_recorder.py` | 2026-05-21 | 2026-06-08 | 5 | 3 |
| `promote_gates.py` | 2026-05-20 | 2026-06-04 | 3 | 1 |
| `detector_stage_reconciliation.py` | 2026-05-29 | 2026-05-29 | 1 | 0 |
| `exit_policy_sweep.py` | 2026-05-29 | 2026-05-29 | 1 | 0 |
| `polymarket_trades_pager.py` | 2026-05-25 | 2026-05-25 | 1 | 2 |
| `binance_stream_service.py` | 2026-05-21 | 2026-05-21 | 0 | 3 |
| `pyth_history_service.py` | 2026-05-21 | 2026-05-21 | 2 | 2 |
| `pyth_stream_service.py` | 2026-05-21 | 2026-05-21 | 0 | 4 |
| `jsonb_utils.py` | 2026-05-21 | 2026-05-21 | 0 | 5 |
| `fee_calculator.py` | 2026-03-11 | 2026-05-21 | 0 | **7** |
| `pnl_formulas.py` | 2026-05-05 | 2026-05-21 | 2 | 2 |
| `manifold_service.py` | 2026-05-20 | 2026-05-21 | 1 | 2 |
| `fake_clob_client.py` | 2026-05-20 | 2026-05-20 | 0 | 1 |
| `clob_client_protocol.py` | 2026-05-20 | 2026-05-20 | 0 | 1 |
| `clob_factory.py` | 2026-05-20 | 2026-05-20 | 4 | 0 |
| `risk_engine.py` | 2026-03-11 | 2026-05-20 | 0 | 2 |
| `settle_velocity_weighted.py` | 2026-05-20 | 2026-05-20 | 0 | 1 |
| `wilson_ci.py` | 2026-05-20 | 2026-05-20 | 0 | 1 |
| `whale_sweep_service.py` | 2026-05-13 | 2026-05-14 | 1 | 1 |
| `detector_helpers.py` | 2026-05-05 | 2026-05-13 | 3 | **11** |
| `taker_service.py` | 2026-03-19 | 2026-05-11 | 1 | 1 |
| `portfolios.py` | 2026-05-05 | 2026-05-05 | 1 | 3 |
| `backtest_metadata.py` | 2026-05-02 | 2026-05-03 | 0 | 2 |
| `polymarket_activity_service.py` | 2026-04-30 | 2026-04-30 | 2 | 0 |
| `polymarket_service.py` | 2026-02-12 | 2026-04-29 | 4 | **7** |
| `shadow_scoring.py` | 2026-04-20 | 2026-04-20 | 3 | 1 |
| `database.py` | 2026-02-23 | 2026-04-16 | 1 | **110** |
| `calibration_service.py` | 2026-02-23 | 2026-04-13 | 3 | 0 |
| `signal_enrichment.py` | 2026-04-05 | 2026-04-08 | 0 | 0 |
| `storage_service.py` | 2026-02-12 | 2026-04-08 | 1 | 0 |
| `thegraph_service.py` | 2026-04-02 | 2026-04-02 | 1 | 1 |
| `whale_style_classifier.py` | 2026-03-22 | 2026-03-22 | 0 | 0 |
| `whale_segment_classifier.py` | 2026-03-14 | 2026-03-14 | 0 | 0 |
| `whale_scoring.py` | 2026-03-13 | 2026-03-13 | 0 | 0 |
| `__init__.py` | 2026-02-12 | 2026-02-12 | 0 | 1 |

### `app/tasks`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `transaction.py` | 2026-02-26 | 2026-06-05 | 7 | 0 |
| `withdrawals.py` | 2026-05-21 | 2026-05-21 | 2 | 0 |
| `notifications.py` | 2026-02-26 | 2026-05-21 | 2 | **13** |
| `resolve_outcomes.py` | 2026-02-26 | 2026-05-13 | 5 | 2 |
| `__init__.py` | 2026-02-26 | 2026-02-26 | 0 | 0 |

### `client/src/App.tsx`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `App.tsx` | 2026-02-12 | 2026-05-21 | 3 | 1 |

### `client/src/components`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `LiveTerminal.tsx` | 2026-05-02 | 2026-07-05 | **11** | 1 |
| `SummaryStrip.tsx` | 2026-07-05 | 2026-07-05 | 2 | 1 |
| `OpenPositionsPanel.tsx` | 2026-05-03 | 2026-07-05 | 2 | 1 |
| `LiveCard.tsx` | 2026-05-03 | 2026-07-05 | 2 | 1 |
| `LiveExpand.tsx` | 2026-05-03 | 2026-07-05 | 2 | 1 |
| `EquityCurve.tsx` | 2026-05-03 | 2026-07-05 | 2 | 1 |
| `DiagnosticsBanner.tsx` | 2026-07-05 | 2026-07-05 | 2 | 1 |
| `primitives.tsx` | 2026-05-05 | 2026-05-30 | 2 | 1 |
| `VirtualCard.tsx` | 2026-05-03 | 2026-05-30 | 5 | 1 |
| `utils.ts` | 2026-05-03 | 2026-05-30 | 1 | 0 |
| `LiveReadinessMap.tsx` | 2026-05-03 | 2026-05-30 | 4 | 1 |
| `EditorialShell.tsx` | 2026-04-24 | 2026-05-21 | 1 | 1 |
| `AuthProvider.tsx` | 2026-02-19 | 2026-05-21 | 1 | 1 |
| `VirtualTerminal.tsx` | 2026-05-02 | 2026-05-18 | **11** | 1 |
| `DailySummaryBar.tsx` | 2026-05-18 | 2026-05-18 | 1 | 1 |
| `ShadowActivity.tsx` | 2026-05-02 | 2026-05-13 | 0 | 1 |
| `PortfolioRollupCard.tsx` | 2026-05-05 | 2026-05-05 | 1 | 1 |
| `PortfolioTabs.tsx` | 2026-05-05 | 2026-05-05 | 1 | 1 |
| `colors.ts` | 2026-05-05 | 2026-05-05 | 0 | 1 |
| `utils.ts` | 2026-05-03 | 2026-05-05 | 1 | 0 |
| `PromotionCard.tsx` | 2026-05-03 | 2026-05-05 | 3 | 1 |
| `StatusTerminal.tsx` | 2026-05-02 | 2026-05-05 | 8 | 1 |
| `Calendar.tsx` | 2026-05-02 | 2026-05-05 | 1 | 1 |
| `ShadowFunnel.tsx` | 2026-05-02 | 2026-05-05 | 1 | 0 |
| `SettleVelocityBar.tsx` | 2026-05-05 | 2026-05-05 | 1 | 2 |
| `AutoEdgeDashboard.tsx` | 2026-05-03 | 2026-05-03 | 0 | 1 |
| `PipelineV2Terminal.tsx` | 2026-04-30 | 2026-05-03 | 1 | 0 |
| `LifecycleProgress.tsx` | 2026-05-03 | 2026-05-03 | 2 | 4 |
| `StageHero.tsx` | 2026-05-03 | 2026-05-03 | 2 | 4 |
| `BacktestTerminal.tsx` | 2026-05-02 | 2026-05-03 | 6 | 1 |
| `PromotionExpand.tsx` | 2026-05-03 | 2026-05-03 | 1 | 1 |
| `BacktestExpand.tsx` | 2026-05-03 | 2026-05-03 | 1 | 1 |
| `BacktestCard.tsx` | 2026-05-03 | 2026-05-03 | 1 | 1 |
| `BacktestMap.tsx` | 2026-05-03 | 2026-05-03 | 3 | 1 |
| `VirtualExpand.tsx` | 2026-05-03 | 2026-05-03 | 1 | 1 |
| `utils.ts` | 2026-05-03 | 2026-05-03 | 1 | 0 |
| `VirtualEmptyState.tsx` | 2026-05-03 | 2026-05-03 | 2 | 2 |
| `ActivityToggle.tsx` | 2026-05-03 | 2026-05-03 | 2 | 1 |
| `PromotionMap.tsx` | 2026-05-03 | 2026-05-03 | 3 | 1 |
| `utils.ts` | 2026-05-03 | 2026-05-03 | 0 | 0 |
| `EmptyStageState.tsx` | 2026-05-03 | 2026-05-03 | 1 | 0 |
| `DetectorRow.tsx` | 2026-05-03 | 2026-05-03 | 2 | 0 |
| `StageBadge.tsx` | 2026-05-02 | 2026-05-02 | 0 | 2 |
| `DetectorLifecycleCard.tsx` | 2026-05-02 | 2026-05-02 | 2 | 1 |
| `DetailHero.tsx` | 2026-04-24 | 2026-04-24 | 1 | 1 |
| `DetailAnatomy.tsx` | 2026-04-24 | 2026-04-24 | 1 | 1 |
| `DetailPositions.tsx` | 2026-04-24 | 2026-04-24 | 1 | 1 |
| `DetailRecord.tsx` | 2026-04-24 | 2026-04-24 | 1 | 1 |
| `DetailTicker.tsx` | 2026-04-24 | 2026-04-24 | 1 | 1 |
| `DetailFooter.tsx` | 2026-04-24 | 2026-04-24 | 1 | 1 |
| `EditorialSkeleton.tsx` | 2026-04-24 | 2026-04-24 | 0 | 2 |
| `EditorialHistory.tsx` | 2026-04-24 | 2026-04-24 | 1 | 1 |
| `StatusBar.tsx` | 2026-03-28 | 2026-04-24 | 1 | 0 |
| `StrategyCard.tsx` | 2026-03-28 | 2026-04-14 | 1 | 0 |
| `HistoryDailySummary.tsx` | 2026-03-31 | 2026-04-07 | 1 | 1 |
| `HistoryLedger.tsx` | 2026-03-31 | 2026-03-31 | 1 | 1 |
| `AlertBanner.tsx` | 2026-03-28 | 2026-03-28 | 1 | 1 |
| `GlassCard.tsx` | 2026-02-12 | 2026-03-10 | 0 | 0 |
| `SemanticSkeleton.tsx` | 2026-03-10 | 2026-03-10 | 1 | 1 |
| `Badge.tsx` | 2026-02-12 | 2026-02-15 | 0 | 0 |

### `client/src/constants`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `signals.ts` | 2026-02-24 | 2026-03-10 | 0 | 0 |

### `client/src/hooks`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `useBot.ts` | 2026-03-28 | 2026-07-02 | 1 | **50** |
| `useTier2.ts` | 2026-05-21 | 2026-06-19 | 1 | 0 |
| `useAuth.ts` | 2026-02-12 | 2026-05-21 | 0 | 3 |
| `useViewport.ts` | 2026-05-03 | 2026-05-03 | 0 | 4 |
| `index.ts` | 2026-02-12 | 2026-04-10 | 0 | 0 |

### `client/src/lib`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `telegram.ts` | 2026-02-24 | 2026-05-21 | 0 | 0 |
| `queryClient.ts` | 2026-02-12 | 2026-05-11 | 0 | 1 |
| `utils.ts` | 2026-03-10 | 2026-03-10 | 0 | 0 |

### `client/src/main.tsx`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `main.tsx` | 2026-02-12 | 2026-02-16 | 3 | 0 |

### `client/src/pages`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `Tier2Insights.tsx` | 2026-05-21 | 2026-06-19 | 0 | 0 |
| `LoginPage.tsx` | 2026-05-06 | 2026-05-21 | 1 | 0 |
| `AutoEdgeResearch.tsx` | 2026-05-03 | 2026-05-03 | 1 | 0 |
| `StrategyDetail.tsx` | 2026-03-28 | 2026-05-02 | 8 | 0 |
| `BotDashboard.tsx` | 2026-03-28 | 2026-05-02 | 7 | 0 |
| `BotHistory.tsx` | 2026-03-31 | 2026-04-24 | 4 | 0 |

### `client/src/types`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `index.ts` | 2026-02-18 | 2026-04-21 | 0 | 0 |
| `lab.ts` | 2026-04-20 | 2026-04-20 | 0 | 0 |
| `telegram.d.ts` | 2026-02-17 | 2026-03-18 | 0 | 2 |

### `client/src/utils`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `share.ts` | 2026-02-18 | 2026-02-19 | 0 | **19** |
| `haptics.ts` | 2026-02-17 | 2026-02-17 | 0 | 0 |

### `client/vite.config.ts`
| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |
|---|---|---|---|---|
| `vite.config.ts` | 2026-02-12 | 2026-05-11 | 2 | 3 |
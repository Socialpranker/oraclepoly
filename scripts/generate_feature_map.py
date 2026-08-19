import ast
import collections
import os
import re
import subprocess
from datetime import UTC, datetime
from pathlib import Path


def get_file_git_data(file_path):
    """Возвращает расширенные git-данные для файла из единственного вызова git log.

    Один subprocess: git log --format="%H|%aI|%s" -- <file>
    Парсит: created, updated, msg, commit_count, version, version_display, _commit_dates.
    """
    result = {
        "created": None,
        "updated": None,
        "msg": "",
        "commit_count": 0,
        "version": 0.1,
        "version_display": "v0.1",
        "_commit_dates": [],
    }
    try:
        cmd = f'git log --format="%H|%aI|%s" -- "{file_path}"'
        output = subprocess.check_output(cmd, shell=True, text=True).strip()
        if not output:
            return result

        lines = output.splitlines()
        result["commit_count"] = len(lines)

        # Собираем даты для weekly aggregation
        commit_dates_iso = []

        # Последний коммит (первая строка) = newest
        first_line_parts = lines[0].split("|", 2)
        if len(first_line_parts) >= 3:
            result["updated"] = datetime.fromisoformat(first_line_parts[1].replace("Z", "+00:00"))
            result["msg"] = first_line_parts[2]
            commit_dates_iso.append(first_line_parts[1])

        # Первый коммит (последняя строка) = oldest
        last_line_parts = lines[-1].split("|", 2)
        if len(last_line_parts) >= 2:
            result["created"] = datetime.fromisoformat(last_line_parts[1].replace("Z", "+00:00"))

        # Собираем все даты из промежуточных строк
        for line in lines[1:]:
            parts = line.split("|", 2)
            if len(parts) >= 2:
                commit_dates_iso.append(parts[1])

        result["_commit_dates"] = commit_dates_iso

        # Hybrid version: start 0.1, feat: → +0.1, fix: или другое → +0.01
        version = 0.1
        # Идём от oldest к newest (reversed)
        for line in reversed(lines[:-1]):  # skip самый первый (oldest) — он уже учтён как 0.1
            parts = line.split("|", 2)
            if len(parts) >= 3:
                msg = parts[2].strip().lower()
                if msg.startswith("feat"):
                    version += 0.1
                else:
                    version += 0.01

        # Округляем до 1 знака
        version = round(version, 1)
        result["version"] = version
        result["version_display"] = f"v{version:.1f}"

    except Exception:
        pass

    return result


def get_file_lines(file_path):
    """Подсчитывает количество строк в файле."""
    try:
        with open(file_path, encoding="utf-8", errors="replace") as f:
            return sum(1 for _ in f)
    except Exception:
        return 0

def get_python_imports(file_path):
    """Extracts imported module names from a Python file."""
    imports = set()
    try:
        with open(file_path, encoding='utf-8') as f:
            tree = ast.parse(f.read(), filename=file_path)
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imports.add(alias.name)
            elif isinstance(node, ast.ImportFrom):
                if node.module:
                    imports.add(node.module)
    except Exception:
        pass
    return imports

def get_file_docstring(file_path):
    """Extracts docstring from python or initial block comment from JS/TS."""
    doc = ""
    try:
        with open(file_path, encoding='utf-8') as f:
            content = f.read()
            if file_path.endswith('.py'):
                try:
                    tree = ast.parse(content)
                    doc = ast.get_docstring(tree) or ""
                except Exception:
                    match = re.search(r'^[\'"]{3}([\s\S]*?)[\'"]{3}', content)
                    if match:
                        doc = match.group(1).strip()
            else:
                match = re.search(r'^\/\*\*([\s\S]*?)\*\/', content)
                if match:
                    lines = match.group(1).split('\n')
                    clean_lines = [re.sub(r'^\s*\*\s?', '', line) for line in lines]
                    doc = '\n'.join(clean_lines).strip()
    except Exception:
        pass
    return doc

def get_ts_imports(file_path):
    """Extracts imported module paths from a TS/JS file (very basic)."""
    imports = set()
    try:
        with open(file_path, encoding='utf-8') as f:
            content = f.read()
        matches = re.findall(r'import\s+.*?\s+from\s+[\'"](.*?)[\'"]', content)
        for match in matches:
            imports.add(match)
    except Exception:
        pass
    return imports

def map_python_import_to_file(module_name):
    """Maps a Python module name like 'app.services.x' to its file path."""
    parts = module_name.split('.')
    if parts[0] == 'app':
        rel_path = "/".join(parts) + ".py"
        abs_path = os.path.join(os.getcwd(), rel_path)
        if os.path.exists(abs_path):
            return rel_path

        rel_path_dir = "/".join(parts) + "/__init__.py"
        abs_path_dir = os.path.join(os.getcwd(), rel_path_dir)
        if os.path.exists(abs_path_dir):
            return rel_path_dir
    return None

def analyze_project():
    """Main analysis function."""
    app_files = list(Path('app').rglob('*.py'))

    client_files = []
    if os.path.exists('client'):
        client_files.extend([f for f in Path('client').rglob('*.ts') if 'node_modules' not in str(f)])
        client_files.extend([f for f in Path('client').rglob('*.tsx') if 'node_modules' not in str(f)])

    all_files = [str(f) for f in app_files + client_files if 'node_modules' not in str(f) and '.venv' not in str(f) and '__pycache__' not in str(f) and 'docs/visualizer' not in str(f)]

    file_data = {}
    incoming_deps = collections.defaultdict(list)
    outgoing_deps = collections.defaultdict(list)

    print(f"Analyzing {len(all_files)} files...")

    for idx, f in enumerate(all_files):
        if idx % 50 == 0 and idx > 0:
            print(f"Processed {idx}/{len(all_files)} files...")

        git_data = get_file_git_data(f)

        if f.endswith('.py'):
            imports = get_python_imports(f)
            for imp in imports:
                resolved = map_python_import_to_file(imp)
                if resolved and resolved in all_files:
                    outgoing_deps[f].append(resolved)
                    incoming_deps[resolved].append(f)
        else:
            imports = get_ts_imports(f)
            for imp in imports:
                base_name = os.path.basename(imp)
                # Fuzzy match for TS/React components
                for cf in client_files:
                    str_cf = str(cf)
                    if base_name in str_cf and (str_cf.endswith('.ts') or str_cf.endswith('.tsx')):
                        outgoing_deps[f].append(str_cf)
                        incoming_deps[str_cf].append(f)
                        break

        file_data[f] = {
            'created': git_data['created'],
            'updated': git_data['updated'],
            'msg': git_data['msg'],
            'commit_count': git_data['commit_count'],
            'version': git_data['version'],
            'version_display': git_data['version_display'],
            'lines': get_file_lines(f),
            '_commit_dates': git_data['_commit_dates'],
            'docstring': get_file_docstring(f),
        }

    return file_data, incoming_deps, outgoing_deps

def is_stale(date_updated):
    if not date_updated:
        return False
    # Ensure date_updated is timezone aware to compare with now
    now = datetime.now(UTC)
    if date_updated.tzinfo is None:
        date_updated = date_updated.replace(tzinfo=UTC)
    return (now - date_updated).days > 30

def generate_markdown(file_data, incoming_deps, outgoing_deps):
    report = []
    report.append("# OraclePoly Feature Map 🗺️")
    report.append("\n*Автоматически сгенерированная карта функционала и зависимостей проекта.*\n")

    # Critical components
    report.append("## 🔥 Критичные компоненты (High Impact)")
    report.append("Эти файлы импортируются чаще всего. Изменения в них затрагивают значительную часть кодовой базы.\n")
    report.append("| Файл | От него зависят | Последнее изменение |")
    report.append("|---|---|---|")

    sorted_by_impact = sorted(incoming_deps.items(), key=lambda x: len(x[1]), reverse=True)
    # top 20
    for f, deps in sorted_by_impact[:20]:
        if f not in file_data:
            continue
        updated = file_data[f]['updated']
        date_str = updated.strftime("%Y-%m-%d") if updated else "-"
        report.append(f"| `{f}` | **{len(deps)}** файлов | {date_str} |")

    # Stale components
    report.append("\n## 🧊 Забытый функционал (Stale Code)")
    report.append("Эти файлы давно не обновлялись (более 30 дней). Они либо гипер-стабильны, либо стали legacy/мертвым кодом.\n")
    report.append("| Файл | Последнее обновление | Суть последнего коммита |")
    report.append("|---|---|---|")

    stale_files = []
    for f, data in file_data.items():
        if is_stale(data['updated']):
            stale_files.append((f, data))

    # Sort by oldest updated first
    stale_files.sort(key=lambda x: x[1]['updated'] if x[1]['updated'] else datetime.max.replace(tzinfo=UTC))

    for f, data in stale_files[:20]:
        updated = data['updated']
        date_str = updated.strftime("%Y-%m-%d") if updated else "-"
        msg = data['msg'][:60] + "..." if len(data['msg']) > 60 else data['msg']
        report.append(f"| `{f}` | {date_str} | {msg} |")

    # Business Domains (Segmenting)
    report.append("\n## 🎯 Ключевые Бизнес-Домены")
    report.append("Функционал, сгруппированный по смысловым сегментами.\n")

    DOMAINS = {
        "🧠 Основная Формула (Oracle Score)": {
            "keywords": [
                "scoring.py", "calibration_service.py",
                "whale_scoring.py", "whale_segment_classifier.py",
                "cluster_classifier.py", "scoring_config_loader.py", "scoring_pure.py",
            ],
            "description_simple": "Центральная логика расчета вероятностей (Байесовский скоринг).",
            "description_complex": "BayesianScoreCalculator (log-odds) в app/core/scoring.py, чистая математика без сайд-эффектов. CalibrationService — 3-tier fallback калибровки (Redis → DB → hardcoded). WhaleScoring и WhaleSegmentClassifier дают вход в whale-based Bayes Factor."
        },
        "🐋 Отслеживание Китов (Whale Tracking, backend-only)": {
            "keywords": [
                "whale_watcher.py", "whale_analytics.py", "whale_tiers.py",
                "whale_sweep_service.py", "whale_style_classifier.py",
            ],
            "description_simple": "Backend-слежка за крупными кошельками и их классификация (без отдельного UI после cleanup).",
            "description_complex": "whale_watcher — WebSocket мониторинг крупных транзакций. whale_analytics и whale_tiers — тиринг и профили кошельков. whale_style_classifier классифицирует торговый стиль. Отдельный Whale Hub UI и API удалены при Phase D frontend cleanup — данные питают детекторы (D13/D26), не отображаются напрямую."
        },
        "🎯 Детекторы: Arbitrage & Basket": {
            "keywords": [
                "d3_scan.py", "d3_cross_mkt_arb.py",
                "d29_scan.py", "d29_negrisk_basket_arb.py",
                "d36_scan.py", "d36_obi_pressure.py",
                "negrisk_sync.py", "detector_helpers.py", "detector_metrics.py",
            ],
            "description_simple": "Детекторы структурного арбитража: neg-risk баскеты и стакан-давление.",
            "description_complex": "D3 ловит переоценённые neg-risk events (CrossMktArb), D29 эмитит basket-arb сигналы на NO-корзины (n=3..6 legs, отключён по умолчанию), D36 — microstructure OBI pressure по order book imbalance. Все эмитят через общий Signal-контракт в detector_signals, execution идёт через v2_execution."
        },
        "🔀 Детекторы: Lead-Lag & Divergence": {
            "keywords": [
                "d45_lead_lag_scan.py", "d45_lead_lag_cross_venue.py",
                "d45_chainlink_leadlag_scan.py", "d45_chainlink_leadlag.py",
                "d45_slo_monitor.py", "d45_loop.py",
                "late_convergence_scan.py", "d44_late_convergence_carry.py",
                "d42_scan.py", "d42_event_driven_ml.py",
                "pyth_history_service.py", "pyth_stream_service.py", "binance_stream_service.py",
            ],
            "description_simple": "Детекторы расхождения цены между источниками (crypto oracle vs Polymarket, cross-venue).",
            "description_complex": "D45 сравнивает Chainlink/Pyth/Binance котировки crypto-updown рынков с ценой Polymarket (basis divergence, окно резолюции секунды), D45 LeadLagCrossVenue ищет lag между площадками. D44 LateConvergenceCarry ловит сигналы на позднюю конвергенцию цены к резолву. D42 строит EventDrivenML сигнал из live_ml_features."
        },
        "🐢 Детекторы: Retail & Whale Behaviour": {
            "keywords": [
                "d13_scan.py", "d13_retail_confidence_fade.py",
                "d14_scan.py", "d14_crypto_winners_distribute_no.py",
                "d26_scan.py", "d26_fade_whale_extreme.py",
                "d38_scan.py", "d38_resolution_lag.py",
                "auto_edge_scanner.py",
            ],
            "description_simple": "Детекторы поведенческих паттернов retail/whale трейдеров.",
            "description_complex": "D13 RetailConfidenceFade и D26 FadeWhaleExtreme торгуют против чрезмерной уверенности толпы/китов. D14 CryptoWinnersDistributeNO ловит перекос в crypto-категориях. D38 ResolutionLag эксплуатирует задержку резолва рынка относительно факта."
        },
        "🌡️ M2 Liquidity Vacuum (Measurement Instrument)": {
            "keywords": [
                "m2_liquidity_vacuum.py", "m2_vacuum_hook.py",
            ],
            "description_simple": "Термометр-инструмент (не торговый детектор): измеряет опустошение стакана как предиктор движения цены.",
            "description_complex": "Shadow-only измерение: одностороннее вымывание bids/asks в order book коррелирует с forward-движением цены (T+30/60м). Realtime-хук на WSS hot-path с O(1) depletion-check и fire-and-forget записью событий; confidence/edge пока заглушки, исполнения ордеров нет."
        },
        "🇰 Kalshi Cross-Platform": {
            "keywords": [
                "d40_kalshi_cross_platform_scan.py", "d40_kalshi_cross_platform.py",
                "kalshi_market_sync.py", "kalshi_orderbook_sync.py",
                "kalshi_polymarket_match.py", "kalshi_polymarket_matcher.py",
                "kalshi_service.py",
            ],
            "description_simple": "Арбитражная детекция и синхронизация рынков между Polymarket и Kalshi.",
            "description_complex": "kalshi_market_sync/kalshi_orderbook_sync подтягивают рынки и стакан Kalshi; kalshi_polymarket_matcher сопоставляет эквивалентные рынки между площадками; D40 сканирует расхождение цены между Kalshi и Polymarket на сматченных парах и эмитит арбитражный сигнал каждые 5 минут."
        },
        "💰 Live Execution (Real Money)": {
            "keywords": [
                "v2_execution.py", "v2_execution_resolve.py",
                "execution_reconcile.py", "execution_safety.py",
                "redeem_job.py", "withdrawal_propose.py",
                "reconcile_orphans.py", "mtm_refresh.py",
                "position_manager.py", "wallet_health_check.py",
                "clob_client_protocol.py", "clob_factory.py", "clob_order_lookup.py",
                "basket_execution.py", "live_exit.py", "exit_policy_sweep.py",
            ],
            "description_simple": "Единый пайплайн от торгового сигнала до реального ордера на Polymarket и вывода прибыли.",
            "description_complex": "V2 Execution — единственный путь detector_signal → CLOB order → execution_orders (заменил legacy execution_job/strategy_position_manager). v2_execution_resolve подтверждает fills и settlement, execution_reconcile сверяет ордера с реальными транзакциями и держит 3-strike kill-switch на drift, redeem_job забирает выигрыш on-chain, withdrawal_propose формирует ежемесячный вывод прибыли."
        },
        "🚦 Risk / Gates система": {
            "keywords": [
                "risk_engine.py", "promote_gates.py",
                "gate_shadow_telemetry.py", "kill_switch_clear.py", "reject_classify.py",
                "price_staleness_telemetry.py", "detector_state.py",
                "kelly.py", "sizing_controller.py", "horizon_limits.py",
            ],
            "description_simple": "Система гейтов и лимитов, решающая допустить ли сигнал до реального ордера.",
            "description_complex": "detector_state хранит per-детектор стадию (shadow/virtual/live/paused) и live_stake_cap ramp-потолок; execution_safety.evaluate() — чистые check-функции (drift, book depth, spread) перед placement; promote_gates считает Wilson lower bound + adversarial gates для перехода в live; kill_switch_clear автоматически снимает sticky kill-switch для транзиентных причин; gate_shadow_telemetry и price_staleness_telemetry — non-invasive измерение потенциальных будущих гейтов без влияния на решения."
        },
        "🧪 ML Pipeline V2 (Two-Stage)": {
            "keywords": [
                "beta_calibrator.py", "lgb_calibrator.py", "lgb_features.py",
                "event_selector.py", "market_picker.py", "meta_labeler.py",
                "ev_sizer.py", "fallback_chain.py", "event_aware_split.py", "per_event_metrics.py",
                "feature_kernel.py", "live_ml_features_builder.py",
                "ml_calibrator_fit.py", "ml_calibrator_lgb_fit.py",
                "ml_event_selector_fit.py", "ml_market_picker_fit.py", "ml_market_picker_ev_fit.py",
                "ml_meta_labeler_fit.py", "ml_honest_baseline_fit.py", "ml_accuracy_snapshot.py",
                "calibration_bins_refit.py", "refresh_market_calibration.py", "refresh_mv_event_outcome.py",
                "pipeline_v2_shadow_predictor.py", "pipeline_v2_drift_check.py",
                "pipeline_v2_outcome_sync.py", "pipeline_v2_comparison_report.py",
                "model_drift_check.py", "calibration.py",
            ],
            "description_simple": "Двухступенчатый ML-пайплайн: калибровка детектора → выбор события → выбор рынка → EV-сайзинг.",
            "description_complex": "Stage 1 L1 Beta calibrator калибрует raw-confidence детектора per-detector; Layer A (ml_event_selector) отбирает перспективные события из mv_event_outcome; Layer B (ml_market_picker + ev_sizer) выбирает конкретный рынок/сторону и считает EV-based размер ставки; pipeline_v2_shadow_predictor гоняет monolith vs V2 путь параллельно, drift_check и comparison_report сравнивают Brier score для решения о cutover."
        },
        "🪜 Detector Lifecycle & Promotion": {
            "keywords": [
                "detector_signals_resolve.py", "detector_stage_reconciliation.py",
                "detector_signal_service.py", "detectors_honest_audit_report.py",
                "detector_slo_monitor.py", "demote_alerts_monitor.py",
                "virtual_promote_report.py", "virtual_weekly_digest.py", "ramp_check.py",
            ],
            "description_simple": "Жизненный цикл детектора: backtest → shadow → virtual → live, авто-promote/demote.",
            "description_complex": "detector_signals_resolve проставляет WIN/LOSS/EXPIRED исход каждому сигналу; detector_stage_reconciliation сверяет метрики между стадиями backtest/shadow/virtual; virtual_promote_report даёт ежедневный TG-отчёт по gate-статусу кандидатов на live; demote_alerts_monitor следит за drawdown уже живых и virtual детекторов; ramp_check enforces канареечный потолок ставки (live_stake_cap_usd) для только что промоутнутых детекторов."
        },
        "📢 Telegram Notifications": {
            "keywords": [
                "notifications.py", "telegram.py",
            ],
            "description_simple": "Отправка уведомлений и Mini App/webhook интеграция с Telegram.",
            "description_complex": "app/tasks/notifications.py::send_admin_message — центральная точка алертов для всех cron-джобов (execution_reconcile, redeem, health-checks). app/api/telegram.py содержит send_telegram_message/send_telegram_with_buttons и webhook для команд бота и Mini App. Per-trade уведомления по live-сделкам шлются из v2_execution_resolve при settle, weekly digest по virtual-сделкам — из virtual_weekly_digest.py."
        },
        "🧹 Data Pipeline Hygiene & Monitoring": {
            "keywords": [
                "db_size_monitor.py", "db_size_report.py", "hf_upstream_check.py",
                "canonical_event_generator.py", "vacuum_maintenance.py",
                "orderbook_snapshots_cleanup.py", "proxy_health_check.py",
                "polymarket_api_health.py", "cleanup.py",
            ],
            "description_simple": "Мониторинг здоровья данных, дисковой квоты и внешних API, плюс автоочистка legacy-данных.",
            "description_complex": "db_size_monitor/db_size_report следят за ростом БД и шлют TG-алерт при превышении порога; hf_upstream_check проверяет свежесть HuggingFace датасета для backtest; proxy_health_check и polymarket_api_health мониторят внешние зависимости (IPRoyal, Polymarket API error rate); vacuum_maintenance и orderbook_snapshots_cleanup управляют retention и физическим размером таблиц; canonical_event_generator автонаполняет canonical_events из живых Polymarket events."
        },
    }

    SCREENS = {
        "🤖 Bot Dashboard (единственный экран)": {
            "keywords": [
                "pages/BotDashboard.tsx", "components/bot/StrategyCard.tsx",
                "components/bot/StatusBar.tsx", "components/bot/AlertBanner.tsx",
                "components/bot/StageBadge.tsx",
                "useBot.ts", "bot.py",
            ],
            "description_simple": "Главный (и по факту единственный) экран: карточки детекторов по стадиям + статус-бар.",
            "description_complex": "После Phase D frontend cleanup весь старый multi-page UI (Whale Hub, Admin Analytics, Market Detail и т.д.) удалён. BotDashboard — единая точка входа: список StrategyCard (по одному на детектор) со StageBadge (backtest/shadow/virtual/live), общий StatusBar и AlertBanner. Данные — из app/api/bot.py (2600+ строк, самый крупный роутер проекта)."
        },
        "🪜 Detector Lifecycle Cards": {
            "keywords": [
                "components/bot/lifecycle/", "components/bot/detail/",
                "DetectorLifecycleCard.tsx", "BacktestTerminal.tsx", "VirtualTerminal.tsx",
                "LiveTerminal.tsx", "LifecycleProgress.tsx",
            ],
            "description_simple": "Раскрывающиеся карточки жизненного цикла детектора: backtest → shadow → virtual → live.",
            "description_complex": "DetectorLifecycleCard — раскрывающийся компонент с 4 под-состояниями (backtest/shadow/virtual/live), у каждого свой Terminal-компонент и Expand-панель (BacktestMap, PromotionMap, LiveReadinessMap, EquityCurve, OpenPositionsPanel). Питается от app/api/bot.py эндпоинтов /v2/lifecycle/*."
        },
        "📜 History & Activity": {
            "keywords": [
                "pages/BotHistory.tsx", "components/bot/history/",
                "HistoryLedger.tsx", "HistoryDailySummary.tsx",
                "components/bot/status/Calendar.tsx", "ShadowActivity.tsx", "StatusTerminal.tsx",
            ],
            "description_simple": "История сделок и активности по дням/календарю.",
            "description_complex": "BotHistory + EditorialHistory — журнал сделок (HistoryLedger) и дневные саммари (HistoryDailySummary). Calendar/ShadowActivity визуализируют активность детекторов по дням. Данные из /v2/activity, /v2/activity/day/{date}, /v2/activity/calendar."
        },
        "🔬 Auto-Edge Research & Tier2 Insights": {
            "keywords": [
                "pages/AutoEdgeResearch.tsx", "pages/Tier2Insights.tsx",
                "AutoEdgeDashboard.tsx", "useTier2.ts", "tier2.py",
            ],
            "description_simple": "Исследовательские дашборды: авто-обнаружение edge и Tier2-аналитика (UMA-споры, дивергенция Manifold).",
            "description_complex": "AutoEdgeResearch показывает discoveries от auto_edge_scanner (dismiss/promote в UI). Tier2Insights — витрина research-эндпоинтов: uma-disputes, manifold-divergence, whale-shocks, term-structure, cohort-stats, ml-accuracy."
        },
        "🧪 Pipeline V2 Shadow Funnel": {
            "keywords": [
                "components/bot/pipeline_v2/", "PipelineV2Terminal.tsx", "ShadowFunnel.tsx",
            ],
            "description_simple": "Визуализация воронки Two-Stage ML pipeline в shadow-режиме (monolith vs V2).",
            "description_complex": "ShadowFunnel показывает, сколько сигналов проходит через Layer A / Layer B / EV-gate в V2-пути параллельно с monolith-путём — для сравнения перед cutover."
        },
        "🔑 Auth & Telegram Mini App": {
            "keywords": [
                "LoginPage.tsx", "useAuth.ts", "auth.py",
            ],
            "description_simple": "Авторизация через Telegram Mini App.",
            "description_complex": "LoginPage + useAuth используют app/api/auth.py (/callback, /me, /logout) для авторизации владельца бота через Telegram initData."
        },
    }

    for domain_name, data in DOMAINS.items():
        keywords = data["keywords"]
        report.append(f"\n### {domain_name}")
        report.append("| Файл | Создан | Обновлен | Суть последнего коммита |")
        report.append("|---|---|---|---|")

        domain_files = []
        for f, fdata in file_data.items():
            for kw in keywords:
                if kw in f:
                    domain_files.append((f, fdata))
                    break # count file once per domain

        domain_files.sort(key=lambda x: x[1]['updated'] if x[1]['updated'] else datetime.min.replace(tzinfo=UTC), reverse=True)

        for f, fdata in domain_files:
            created = fdata['created']
            updated = fdata['updated']
            cd_str = created.strftime("%Y-%m-%d") if created else "-"
            ud_str = updated.strftime("%Y-%m-%d") if updated else "-"
            msg = fdata['msg'][:50] + "..." if len(fdata['msg']) > 50 else fdata['msg']
            report.append(f"| `{f}` | {cd_str} | {ud_str} | {msg} |")


    # Module map
    report.append("\n## 📂 Подробная карта всех модулей")

    subsystems = collections.defaultdict(list)
    for f in file_data.keys():
        parts = f.split('/')
        if len(parts) > 1:
            if parts[0] == 'app':
                sub = parts[0] + "/" + parts[1] # e.g. app/services
            elif parts[0] == 'client' and len(parts) > 2:
                sub = parts[0] + "/" + parts[1] + "/" + parts[2] # e.g. client/src/components
            else:
                sub = parts[0] + "/" + parts[1]
            subsystems[sub].append(f)
        else:
            subsystems['Root'].append(f)

    # Sort subsystems alphabetically
    for sub in sorted(subsystems.keys()):
        files = subsystems[sub]
        if not files:
            continue

        report.append(f"\n### `{sub}`")
        report.append("| Файл | Создан | Обновлен | Вызывает (Зависит от) | Вызывается (Влияет на) |")
        report.append("|---|---|---|---|---|")

        # Sort files by updated descending
        files.sort(key=lambda x: file_data[x]['updated'] if file_data[x]['updated'] else datetime.min.replace(tzinfo=UTC), reverse=True)

        for f in files:
            created = file_data[f]['created']
            updated = file_data[f]['updated']
            cd_str = created.strftime("%Y-%m-%d") if created else "-"
            ud_str = updated.strftime("%Y-%m-%d") if updated else "-"

            inc = len(incoming_deps.get(f, []))
            outg = len(outgoing_deps.get(f, []))

            bold_inc = f"**{inc}**" if inc > 5 else str(inc)
            bold_outg = f"**{outg}**" if outg > 10 else str(outg)

            report.append(f"| `{os.path.basename(f)}` | {cd_str} | {ud_str} | {bold_outg} | {bold_inc} |")

    return "\n".join(report), DOMAINS, SCREENS


def compute_weekly_commits(commit_dates_iso, num_weeks=8):
    """Группирует ISO-даты коммитов в недельные бакеты (isocalendar).

    Возвращает массив длиной num_weeks: [oldest_week, ..., current_week].
    """
    now = datetime.now(UTC)

    # Создаём бакеты для последних num_weeks недель
    week_counts = [0] * num_weeks

    for date_str in commit_dates_iso:
        try:
            dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))

            # Вычисляем разницу в неделях от текущей через дни
            delta_days = (now - dt).days
            weeks_ago = delta_days // 7

            if 0 <= weeks_ago < num_weeks:
                # index 0 = oldest, index num_weeks-1 = current
                idx = num_weeks - 1 - weeks_ago
                week_counts[idx] += 1
        except Exception:
            continue

    return week_counts


def compute_domain_stats(domains, file_data):
    """Вычисляет агрегированную статистику для каждого домена.

    Для каждого домена:
    - version: среднее версий файлов, округлённое до 0.1
    - total_commits: сумма commit_count
    - total_files: количество файлов
    - last_activity_days: дней с последнего обновления
    - status: "active" (<3d), "moderate" (3-7d), "stable" (>7d)
    - weekly_commits: массив коммитов за последние 8 недель
    """
    now = datetime.now(UTC)
    domain_stats = {}

    for domain_name, domain_data in domains.items():
        keywords = domain_data["keywords"]

        # Находим файлы домена
        matched_files = []
        for f, fdata in file_data.items():
            for kw in keywords:
                if kw in f:
                    matched_files.append((f, fdata))
                    break

        if not matched_files:
            domain_stats[domain_name] = {
                "version": 0.1,
                "total_commits": 0,
                "total_files": 0,
                "last_activity_days": 999,
                "status": "stable",
                "weekly_commits": [0] * 8,
            }
            continue

        # Агрегация
        versions = []
        total_commits = 0
        all_commit_dates = []
        max_updated = None

        for f, fdata in matched_files:
            versions.append(fdata.get("version", 0.1))
            total_commits += fdata.get("commit_count", 0)
            all_commit_dates.extend(fdata.get("_commit_dates", []))

            updated = fdata.get("updated")
            if updated:
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=UTC)
                if max_updated is None or updated > max_updated:
                    max_updated = updated

        avg_version = round(sum(versions) / len(versions), 1) if versions else 0.1

        if max_updated:
            last_activity_days = (now - max_updated).days
        else:
            last_activity_days = 999

        if last_activity_days < 3:
            status = "active"
        elif last_activity_days <= 7:
            status = "moderate"
        else:
            status = "stable"

        weekly_commits = compute_weekly_commits(all_commit_dates, num_weeks=8)

        domain_stats[domain_name] = {
            "version": avg_version,
            "total_commits": total_commits,
            "total_files": len(matched_files),
            "last_activity_days": last_activity_days,
            "status": status,
            "weekly_commits": weekly_commits,
        }

    return domain_stats


# ---------------------------------------------------------------------------
# Task 3: API endpoints extraction
# ---------------------------------------------------------------------------

def extract_router_prefixes():
    """Парсит app/main.py для вызовов include_router() и возвращает маппинг файл→префикс.

    Возвращает dict: {"app/api/module.py": "/api/prefix"}.
    Если prefix не указан — пустая строка.
    """
    prefixes = {}
    main_path = os.path.join(os.getcwd(), "app", "main.py")
    if not os.path.exists(main_path):
        return prefixes

    try:
        with open(main_path, encoding="utf-8") as f:
            content = f.read()
    except Exception:
        return prefixes

    # Находим все include_router() вызовы
    # Паттерн: app.include_router(module.router, prefix="/api/...", ...)
    # или app.include_router(module.router)  (без prefix)
    pattern = re.compile(
        r'app\.include_router\(\s*(\w+)\.router'
        r'(?:.*?prefix\s*=\s*["\']([^"\']*)["\'])?',
        re.DOTALL,
    )

    # Также парсим импорты, чтобы сопоставить module_name → файл
    # Паттерн: from app.api import (mod1, mod2, ...)  или  from app.api import mod
    import_pattern = re.compile(
        r'from\s+app\.api\s+import\s+\(([^)]+)\)', re.DOTALL
    )
    import_single = re.compile(r'from\s+app\.api\s+import\s+(\w+)')

    imported_modules = set()
    for m in import_pattern.finditer(content):
        names = [n.strip().rstrip(",") for n in m.group(1).split("\n")]
        imported_modules.update(n for n in names if n and not n.startswith("#"))
    for m in import_single.finditer(content):
        imported_modules.add(m.group(1))

    for m in pattern.finditer(content):
        module_name = m.group(1)  # e.g. "markets", "admin_db"
        prefix = m.group(2) or ""  # e.g. "/api/markets" or ""

        if module_name in imported_modules:
            file_path = f"app/api/{module_name}.py"
            prefixes[file_path] = prefix

    return prefixes


def extract_api_endpoints(file_data, outgoing_deps, prefixes):
    """Извлекает API endpoints из router-файлов.

    Для каждого router-файла:
    1. Regex по @router.(get|post|put|patch|delete)("path")
    2. Комбинирует prefix + path для полного endpoint
    3. handler_chain = outgoing_deps[router_file] (depth 1)
    4. Frontend hook matching: ищет prefix в содержимом hooks/*.ts
    5. version из file_data

    Возвращает list[dict].
    """
    endpoints = []

    # Предварительно загружаем содержимое всех hooks для поиска
    hooks_dir = os.path.join(os.getcwd(), "client", "src", "hooks")
    hook_files = {}  # filename → content
    if os.path.isdir(hooks_dir):
        for hook_file in os.listdir(hooks_dir):
            if hook_file.endswith(".ts") or hook_file.endswith(".tsx"):
                hook_path = os.path.join(hooks_dir, hook_file)
                try:
                    with open(hook_path, encoding="utf-8") as hf:
                        hook_files[f"client/src/hooks/{hook_file}"] = hf.read()
                except Exception:
                    pass

    # Также нужно учитывать router-файлы с собственным prefix в APIRouter()
    # (e.g. signals.py: router = APIRouter(prefix="/api/signals"))
    api_router_pattern = re.compile(
        r'APIRouter\([^)]*prefix\s*=\s*["\']([^"\']+)["\']'
    )

    # Обрабатываем все router-файлы
    all_router_files = set(prefixes.keys())

    # Добавляем файлы из app/api/, которые есть в file_data, но могли не попасть в prefixes
    for f in file_data:
        if f.startswith("app/api/") and f.endswith(".py") and "__init__" not in f:
            all_router_files.add(f)

    endpoint_pattern = re.compile(
        r'@router\.(get|post|put|patch|delete)\(\s*["\']([^"\']*)["\']'
    )

    for router_file in sorted(all_router_files):
        abs_path = os.path.join(os.getcwd(), router_file)
        if not os.path.exists(abs_path):
            continue

        try:
            with open(abs_path, encoding="utf-8") as rf:
                content = rf.read()
        except Exception:
            continue

        # Определяем prefix: сначала из main.py, потом из собственного APIRouter()
        prefix = prefixes.get(router_file, "")

        # Проверяем собственный prefix в APIRouter()
        own_prefix_match = api_router_pattern.search(content)
        if own_prefix_match:
            own_prefix = own_prefix_match.group(1)
            # Если main.py не задал prefix (пустая строка), используем собственный
            if not prefix:
                prefix = own_prefix

        # Извлекаем endpoints
        for em in endpoint_pattern.finditer(content):
            method = em.group(1).upper()
            path = em.group(2)

            # Полный путь
            full_path = prefix + path

            # Handler chain (depth 1) — зависимости router-файла
            handler_chain = list(outgoing_deps.get(router_file, []))

            # Frontend hook matching: ищем prefix в содержимом hooks
            frontend_hook = None
            if prefix:
                for hook_path, hook_content in hook_files.items():
                    if prefix in hook_content:
                        frontend_hook = hook_path
                        break

            # Version и updated из file_data
            fdata = file_data.get(router_file, {})
            version = fdata.get("version_display", "v0.1")
            updated = fdata.get("updated")
            # Конвертируем datetime в ISO строку
            updated_str = updated.isoformat() if updated else None

            endpoints.append({
                "method": method,
                "path": full_path,
                "router_file": router_file,
                "handler_chain": handler_chain,
                "frontend_hook": frontend_hook,
                "version": version,
                "updated": updated_str,
            })

    return endpoints


# ---------------------------------------------------------------------------
# Task 4: Risk zones + blind spots
# ---------------------------------------------------------------------------

COUPLING_PAIRS = [
    ["app/core/scoring.py", "app/core/config.py", "tests/test_oracle_calculator_bayesian.py"],
    ["app/handlers/whale_watcher.py", "app/tasks/transaction.py", "app/handlers/whale_analytics.py"],
    [
        "app/services/simulation_service.py",
        "app/services/risk_engine.py",
        "app/services/fee_calculator.py",
    ],
    ["app/handlers/oracle_calculator.py", "app/core/scoring.py", "app/services/calibration_service.py"],
    ["app/services/live_trading.py", "app/services/execution_engine.py", "app/services/risk_engine.py"],
    ["app/core/simulation_defaults.py", "client/src/components/analytics/simulationDefaults.ts"],
    ["app/services/strategy_service.py", "app/core/strategy_registry.py", "app/jobs/strategy_settle.py"],
]


def detect_risk_zones(file_data, incoming_deps):
    """Определяет зоны риска: high-churn coupled файлы и desync-пары.

    High-churn coupled: файлы с >5 callers, изменённые за последние 7 дней.
    Desync pairs: связанные группы, где newest и oldest разнятся на >3 дней.

    Возвращает dict: {"high_churn_coupled": [...], "desync_pairs": [...]}.
    """
    now = datetime.now(UTC)

    # --- High-churn coupled ---
    high_churn_coupled = []
    for f, callers in incoming_deps.items():
        if len(callers) <= 5:
            continue
        fdata = file_data.get(f)
        if not fdata or not fdata.get("updated"):
            continue
        updated = fdata["updated"]
        if updated.tzinfo is None:
            updated = updated.replace(tzinfo=UTC)
        days_ago = (now - updated).days
        if days_ago <= 7:
            high_churn_coupled.append({
                "file": f,
                "callers": len(callers),
                "days_ago": days_ago,
            })

    high_churn_coupled.sort(key=lambda x: x["callers"], reverse=True)

    # --- Desync pairs ---
    desync_pairs = []
    for group in COUPLING_PAIRS:
        # Собираем даты обновления для файлов группы
        dated_files = []
        for gf in group:
            fdata = file_data.get(gf)
            if fdata and fdata.get("updated"):
                updated = fdata["updated"]
                if updated.tzinfo is None:
                    updated = updated.replace(tzinfo=UTC)
                dated_files.append((gf, updated))

        if len(dated_files) < 2:
            continue

        # Находим newest и oldest
        dated_files.sort(key=lambda x: x[1], reverse=True)
        newest_file, newest_date = dated_files[0]

        for lagging_file, lagging_date in dated_files[1:]:
            days_apart = (newest_date - lagging_date).days
            if days_apart > 3:
                risk = "high" if days_apart > 7 else "warning"
                desync_pairs.append({
                    "files": [newest_file, lagging_file],
                    "days_apart": days_apart,
                    "risk": risk,
                })

    return {
        "high_churn_coupled": high_churn_coupled,
        "desync_pairs": desync_pairs,
    }


def detect_blind_spots(file_data, incoming_deps):
    """Определяет слепые зоны: файлы без docstring, без входящих зависимостей, без тестов.

    Возвращает dict: {"no_docstring": [...], "zero_incoming": [...], "no_tests": [...]}.
    """
    # Собираем имена тестовых файлов для сопоставления
    test_dir = os.path.join(os.getcwd(), "tests")
    test_file_names = set()
    if os.path.isdir(test_dir):
        for tf in os.listdir(test_dir):
            if tf.startswith("test_") and tf.endswith(".py"):
                test_file_names.add(tf)

    # Также проверяем тестовые файлы в file_data
    for f in file_data:
        basename = os.path.basename(f)
        if basename.startswith("test_") and basename.endswith(".py"):
            test_file_names.add(basename)

    no_docstring = []
    zero_incoming = []
    no_tests = []

    skip_names = {"__init__.py", "conftest.py"}
    skip_zero_incoming = {"__init__.py", "conftest.py", "config.py"}

    for f, fdata in file_data.items():
        basename = os.path.basename(f)

        # --- no_docstring: пропускаем __init__.py и тестовые файлы ---
        if basename not in skip_names and not basename.startswith("test_"):
            if f.endswith(".py"):
                docstring = fdata.get("docstring", "")
                if not docstring:
                    no_docstring.append(f)

        # --- zero_incoming: пропускаем main.py, __init__, config.py, conftest ---
        if basename not in skip_zero_incoming and basename != "main.py":
            callers = incoming_deps.get(f, [])
            if len(callers) == 0:
                zero_incoming.append(f)

        # --- no_tests: только app/handlers/ и app/core/, ищем test_* пару ---
        if (f.startswith("app/handlers/") or f.startswith("app/core/")) and f.endswith(".py"):
            if basename in skip_names:
                continue
            # Генерируем ожидаемое имя тестового файла
            # app/handlers/whale_watcher.py → test_whale_watcher.py
            stem = basename.replace(".py", "")
            expected_test = f"test_{stem}.py"
            if expected_test not in test_file_names:
                no_tests.append(f)

    no_docstring.sort()
    zero_incoming.sort()
    no_tests.sort()

    return {
        "no_docstring": no_docstring,
        "zero_incoming": zero_incoming,
        "no_tests": no_tests,
    }


if __name__ == "__main__":
    import time
    start = time.time()
    file_data, incoming_deps, outgoing_deps = analyze_project()
    report, DOMAINS, SCREENS = generate_markdown(file_data, incoming_deps, outgoing_deps)

    # Агрегация статистики по доменам (до очистки _commit_dates)
    domain_stats = compute_domain_stats(DOMAINS, file_data)

    # Task 3: API endpoints
    prefixes = extract_router_prefixes()
    api_endpoints = extract_api_endpoints(file_data, outgoing_deps, prefixes)
    print(f"  API endpoints extracted: {len(api_endpoints)}")

    # Task 4: Risk zones + blind spots
    risk_zones = detect_risk_zones(file_data, incoming_deps)
    blind_spots = detect_blind_spots(file_data, incoming_deps)
    print(f"  Risk zones: {len(risk_zones['high_churn_coupled'])} high-churn, {len(risk_zones['desync_pairs'])} desync")
    print(f"  Blind spots: {len(blind_spots['no_docstring'])} no-doc, {len(blind_spots['zero_incoming'])} zero-inc, {len(blind_spots['no_tests'])} no-tests")

    # Очистка временных _commit_dates из file_data перед сериализацией
    for f in file_data:
        file_data[f].pop("_commit_dates", None)

    os.makedirs('docs/feature_map', exist_ok=True)
    with open('docs/feature_map/FEATURE_MAP.md', 'w', encoding='utf-8') as f:
        f.write(report)

    # Also dump as JSON for the visualizer React App
    import json

    # We need to serialize datetimes
    def json_serial(obj):
        if isinstance(obj, datetime):
            return obj.isoformat()
        raise TypeError(f"Type {type(obj)} not serializable")

    json_data = {
        "file_data": file_data,
        "incoming_deps": incoming_deps,
        "outgoing_deps": outgoing_deps,
        "domains": DOMAINS,
        "screens": SCREENS,
        "domain_stats": domain_stats,
        "api_endpoints": api_endpoints,
        "risk_zones": risk_zones,
        "blind_spots": blind_spots,
    }

    os.makedirs('docs/visualizer/public', exist_ok=True)
    try:
        with open('docs/visualizer/public/feature_map_data.json', 'w', encoding='utf-8') as f:
            json.dump(json_data, f, default=json_serial, indent=2)
    except Exception as e:
        print(f"Could not save JSON: {e}")

    print(f"Done in {time.time() - start:.2f}s! Report saved to docs/feature_map/FEATURE_MAP.md")

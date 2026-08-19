"""
Oracle Backend - Core Configuration

Strict typing for all environment variables.
Uses pydantic-settings for validation.
"""

import os
from functools import lru_cache
from typing import Literal

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- PostgreSQL ---
    database_url: str = Field(
        description="PostgreSQL connection URL (обязательно в production)",
    )
    agent_database_url: str | None = Field(
        default=None, description="Read-only DSN для Portfolio Analyst Agent (Phase 0, least-privilege)"
    )

    # --- Cloudflare R2 (optional) ---
    r2_account_id: str = Field(default="", description="Cloudflare Account ID")
    r2_access_key_id: str = Field(default="", description="R2 Access Key")
    r2_secret_access_key: str = Field(default="", description="R2 Secret Key")
    r2_bucket_name: str = Field(default="oracle-data-lake", description="R2 Bucket Name")

    # --- Qdrant (optional) ---
    qdrant_url: str = Field(default="", description="Qdrant Cloud URL")
    qdrant_api_key: str = Field(default="", description="Qdrant API Key")

    # --- OpenAI ---
    openai_api_key: str = Field(default="", description="OpenAI API Key for Embeddings")

    # --- Firecrawl ---
    firecrawl_api_key: str = Field(default="", description="Firecrawl API Key for scraping")

    # --- Serper (Fallback) ---
    serper_api_key: str = Field(default="", description="Serper API Key for fallback scraping")

    # --- Telegram ---
    telegram_bot_token: str = Field(default="", description="Telegram Bot API Token")
    telegram_webhook_secret: str = Field(default="", description="Telegram Webhook Secret Token")
    jwt_secret_key: str = Field(
        default="",
        description="Secret для подписи JWT cookie web-auth (min 32 chars, обязателен в проде)",
    )

    # --- Polymarket ---
    polymarket_api_key: str = Field(default="", description="Polymarket API Key")
    polymarket_private_key: str = Field(default="", description="Polymarket Private Key (Optional for read)")
    polymarket_address: str = Field(default="", description="Polymarket Funder Address (POLY_PROXY/Safe)")
    polymarket_signature_type: int = Field(
        default=1,
        description="Polymarket signature_type: 0=EOA, 1=POLY_PROXY (Magic), 2=POLY_GNOSIS_SAFE (Browser Wallet)",
    )
    polymarket_host: str = Field(default="https://clob.polymarket.com", description="CLOB API Host")
    polymarket_chain_id: int = Field(default=137, description="Chain ID (137 for Polygon)")

    # --- The Graph ---
    thegraph_api_key: str = Field(default="", description="The Graph API Key (free tier: 100K req/мес)")
    polymarket_ws_url: str = Field(
        default="wss://ws-subscriptions-clob.polymarket.com/ws/market",
        description="Polymarket WebSocket Market Channel URL",
    )

    # --- Kalshi (Phase 1 MVP, see research/kalshi-integration-2026-05/prd.md) ---
    kalshi_api_key_id: str = Field(default="", description="Kalshi API Key ID (UUID)")
    kalshi_private_key_path: str = Field(
        default="/run/secrets/kalshi_rsa.pem",
        description="Path to RSA-PSS private key PEM (Docker secret mount)",
    )
    kalshi_api_base_url: str = Field(
        default="https://demo-api.kalshi.co/trade-api/v2",
        description="Kalshi REST API base URL (demo by default, switch to api.elections.kalshi.com после KYC)",
    )
    kalshi_ws_url: str = Field(
        default="wss://demo-api.kalshi.co/trade-api/ws/v2",
        description="Kalshi WebSocket URL (demo by default)",
    )
    kalshi_rate_limit_rps: float = Field(default=2.0, description="Self-imposed RPS cap (Kalshi не публикует лимит)")
    kalshi_timeout_sec: float = Field(default=10.0, description="HTTP timeout for Kalshi requests")

    # --- Redis ---
    redis_url: str = Field(
        default="redis://localhost:6379/0",
        description="Redis connection URL (include password: redis://:password@host:port/db)",
    )

    # --- Admin ---
    admin_api_key: str = Field(default="", description="Single admin API key (legacy, используй ADMIN_API_KEYS)")
    admin_api_keys: list[str] = Field(
        default=[],
        description="Список admin API ключей через запятую для zero-downtime ротации (ADMIN_API_KEYS=key1,key2)",
    )
    admin_telegram_ids: list[int] = Field(
        default=[],
        description="Telegram user IDs allowed admin access (set via ADMIN_TELEGRAM_IDS as JSON array)",
    )

    @property
    def active_admin_keys(self) -> list[str]:
        """Все активные admin-ключи (объединение admin_api_key + admin_api_keys)."""
        keys: list[str] = []
        if self.admin_api_key:
            keys.append(self.admin_api_key)
        keys.extend(k for k in self.admin_api_keys if k and k not in keys)
        return keys

    # --- IPRoyal Proxy ---
    iproyal_api_token: str = Field(default="", description="IPRoyal API Bearer token для мониторинга прокси")
    iproyal_traffic_alert_gb: float = Field(default=1.0, description="Порог остатка трафика IPRoyal (GB) для алерта")
    https_proxy: str = Field(default="", description="HTTP прокси URL для Polymarket запросов (IPRoyal)")
    proxy_expires_at: str = Field(default="", description="ISO datetime истечения прокси-сессии (для таймера алерта)")

    # --- CryptoBot (Crypto Pay) ---
    cryptopay_api_token: str = Field(default="", description="CryptoBot Crypto Pay API token")
    webhook_ip_whitelist: list[str] = Field(
        default=["149.154.160.0/20", "91.108.4.0/22"],
        description=(
            "IP/CIDR whitelist для входящих вебхуков (Telegram, платёжные). "
            "Пустой список = без ограничений (только в dev). "
            "Дефолт — официальные Telegram CIDR. "
            'Override: WEBHOOK_IP_WHITELIST=\'["149.154.160.0/20","91.108.4.0/22","..."]\''
        ),
    )

    # --- Withdrawal flow (Phase 1 Stage 7) ---
    # Semi-auto monthly USDC withdrawal на whitelisted destination.
    # `_polymarket_withdraw_usdc` пока NotImplementedError — реальные ставки
    # не отправляются. `withdrawal_enabled=False` дефолтом блокирует propose job.
    withdrawal_enabled: bool = Field(
        default=False,
        description="Master switch для withdrawal flow. False = propose cron skip.",
    )
    withdrawal_destination_address: str = Field(
        default="",
        description="Whitelisted USDC destination address (single, hardcoded).",
    )
    withdrawal_min_amount_usd: float = Field(
        default=20.0,
        description="Min viable withdrawal в USD (gas amortization).",
    )
    withdrawal_proposal_ttl_hours: int = Field(
        default=48,
        description="TG approval timeout в часах.",
    )
    withdrawal_max_pct_of_monthly_profit: float = Field(
        default=0.50,
        description="Доля monthly profit для proposal (document §12: 50% rule).",
    )

    # --- Sentry ---
    sentry_dsn: str | None = None

    # --- Cloudflare Workers AI ---
    cf_account_id: str = Field(default="", description="Cloudflare Account ID для Workers AI")
    cf_api_token: str = Field(default="", description="Cloudflare API Token для Workers AI")
    cf_model_chat: str = Field(
        default="@cf/meta/llama-3.1-8b-instruct",
        description="Cloudflare Workers AI модель для чата/анализа",
    )
    cf_model_embedding: str = Field(
        default="@cf/baai/bge-large-en-v1.5",
        description="Cloudflare Workers AI модель для эмбеддингов",
    )
    cf_embedding_dim: int = Field(default=1024, description="Размерность эмбеддингов CF BGE-large")

    # --- OpenRouter (OpenAI-совместимый шлюз, embeddings + chat) ---
    openrouter_api_key: str = Field(default="", description="OpenRouter API Key (sk-or-...)")
    openrouter_embedding_model: str = Field(
        default="openai/text-embedding-3-small",
        description="OpenRouter модель для эмбеддингов (slug provider/model)",
    )
    openrouter_embedding_dim: int = Field(
        default=1536, description="Размерность эмбеддингов OpenRouter-модели (3-small=1536)"
    )
    advisory_explainer_enabled: bool = Field(
        default=False,
        description="Рубильник advisory-explainer (Request review). False → LLM не вызывается даже при наличии ключа",
    )
    advisory_explainer_model: str = Field(
        default="anthropic/claude-haiku-4.5",
        description="OpenRouter-модель для advisory-explainer (Request review); сменить без деплоя",
    )
    advisory_explainer_max_tokens: int = Field(
        default=400, description="Max output tokens для короткого разбора advisory"
    )

    # --- LLM Provider ---
    llm_provider: str = Field(default="openai", description="LLM provider: openai | cloudflare | openrouter | local")
    llm_analysis_model: str = Field(default="gpt-4o-mini", description="Model for analysis generation")
    llm_base_url: str | None = Field(default=None, description="Custom base URL for OpenAI-compatible API")
    llm_temperature: float = Field(default=0.4, description="LLM temperature for analysis")
    llm_max_tokens: int = Field(default=1500, description="Max output tokens for analysis")

    # --- Bayesian Scoring ---
    scoring_f_max: float = Field(default=0.25, description="Kelly cap (Quarter-Kelly)")
    scoring_k_liquidity: float = Field(default=0.01, description="Liquidity gating tuning parameter")
    scoring_w_mkt: float = Field(default=1.0, description="Market prior weight")
    scoring_w_whale: float = Field(default=0.0, description="Whale linear weight (обнулён, заменён на w_whale_contra)")
    scoring_w_whale_contra: float = Field(
        default=-0.3, description="Whale contra-indicator interaction weight (fade the whale)"
    )
    scoring_expert_weight_enabled: bool = Field(default=True, description="Enable expert_weight in whale scoring")
    scoring_timing_boost_scale: float = Field(default=0.1, description="Timing score boost scale for credibility")
    scoring_w_fresh: float = Field(default=0.0, description="SM Fresh weight (обнулён: corr 0.02)")
    scoring_w_cluster: float = Field(default=0.0, description="SM Cluster weight (обнулён: 934 записи)")
    scoring_w_registry: float = Field(default=0.3, description="Smart Money: registry match weight (corr +0.62)")
    scoring_w_oi: float = Field(default=0.05, description="OI weight (снижен: corr -0.008 с outcome, фактически шум)")
    scoring_w_concentration: float = Field(default=0.0, description="Concentration weight (обнулён: ухудшает accuracy)")
    scoring_w_micro: float = Field(default=0.0, description="Microstructure: удалён 2026-04-10, всегда 0.0")
    scoring_w_sports_odds: float = Field(default=0.5, description="Sports Odds weight (bookmaker anchor)")
    scoring_w_retail: float = Field(
        default=-0.2, description="Retail contra-signal (calibrated 2026-04-08: fade taker flow)"
    )
    scoring_w_maker_direction: float = Field(
        default=0.5, description="Maker direction weight (taker flow direction, corr +0.76)"
    )
    scoring_w_taker_imbalance: float = Field(
        default=0.0, description="Taker imbalance weight (0.0 — нет live-данных, ждёт calibration)"
    )

    polygonscan_api_key: str = Field(default="", description="Polygonscan API key (free tier)")
    sports_odds_api_key: str = Field(default="", description="The Odds API key (free tier)")
    sports_odds_cache_ttl: int = Field(default=900, description="Sports odds Redis cache TTL (секунды)")
    scoring_prior_alpha: float = Field(default=5.5, description="Beta prior alpha (mean_winrate × strength)")
    scoring_prior_beta: float = Field(default=4.5, description="Beta prior beta ((1-mean_winrate) × strength)")
    scoring_credibility_gamma: float = Field(default=0.97, description="Temporal discount for W/L (1.0=off)")
    scoring_decay_alpha: float = Field(default=0.52, description="Power law alpha (0=exponential)")

    # --- Category-Specific Tuning (Deep Research 2026-04-08) ---
    # Оптимальный min_divergence по категориям (стартовые значения из research,
    # уточняются после historical_category_analysis.py на 615K market_aggregates)
    scoring_w_optimism_tax: float = Field(default=0.0, description="Optimism tax weight (0 = off до калибровки на VPS)")

    # --- Thresholds ---
    ai_analysis_threshold_usd: int = Field(default=25_000, description="Min USD for AI whale analysis")
    whale_alert_threshold_usd: int = Field(default=50_000, description="Min USD for whale alert notification")

    # --- Anomaly Detection ---
    anomaly_new_account_days: int = Field(default=7, description="Максимум дней для флага new_account")
    anomaly_single_market_max: int = Field(default=2, description="Максимум уникальных рынков для single_market")
    anomaly_single_bet_max_trades: int = Field(default=3, description="Максимум сделок для single_bet")
    anomaly_single_bet_min_volume: int = Field(default=10_000, description="Мин объём USD для single_bet")
    anomaly_concentration_threshold: float = Field(default=0.9, description="Порог концентрации объёма")
    anomaly_min_flags: int = Field(default=2, description="Мин кол-во флагов для is_anomalous")
    ws_score_noise_filter: float = Field(default=0.005, description="Мин delta oracle score для broadcast")
    ws_price_write_through_enabled: bool = Field(
        default=True,
        description="Write-through WS-midpoint → markets.current_price раз в 30с (audit топ-3: 15-минутный gamma-снапшот).",
    )

    # --- G18 Calibration Edge (per-bin historical WR lookup) ---
    # Включает per-(detector × category × price_bin) lookup в _calibrate() как
    # first tier перед LGB/Beta. Whitelist детекторов внутри
    # app/services/g18_bin_calibration.py (WHITELIST_DETECTORS).
    # См. docs/specs/G18_calibration_edge.md.
    calibration_bins_enabled: bool = Field(default=False, description="G18 calibration edge feature flag")

    # --- Execution (V2 pipeline) ---
    # Safety limits на stake/daily/balance смотри в app/services/execution_safety.py.
    # Per-detector live gate — detector_state.stage='live' в БД.
    execution_enabled: bool = Field(default=False, description="Kill switch: глобальное выключение live execution")
    live_max_inflight_orders: int = Field(
        default=3, description="Cap working placed-ордеров (анти-флуд, не held-экспозиция)"
    )
    # 10→20 2026-07-07 (human decision, аудит throughput): при hold ~3.8 сут позиции
    # копятся быстрее, чем резолвятся — кап 10 дал 118 reject за 3 дня и стал топ-лимитером
    # накопления live-статистики. Долларовый риск сверху держит max_portfolio_heat=0.40.
    live_max_open_positions: int = Field(
        default=20, description="Cap held filled-позиций (страховка поверх exposure-caps risk_engine)"
    )
    live_min_stake: float = Field(
        default=1.0, description="Live: min stake USD (Polymarket CLOB rejects < $1 maker orders)"
    )
    # Замер 2026-08-05 (spec 2026-08-05-entry-price-gate-taker-mode-design.md):
    # edge D3 живёт только на входах <0.5 — там +60.6% taker ROI net за 60д, устойчиво
    # 13/13 недель; выше порога минус (0.6-0.7 → −22.7%, 0.7-0.8 → −51.6%). При этом
    # maker наливают охотнее именно в убыточной полосе (fill-rate 17.9% против 13.2%) —
    # это adverse selection, поэтому гейт идёт в паре с принудительным taker'ом.
    # Per-detector: у D36 эффекта нет вовсе, у STANDALONE полоса <0.5 даёт лишь +1.9%.
    entry_price_gate_detectors: str = Field(
        default="D3_cross_mkt_arb",
        description="CSV детекторов с гейтом цены входа + принудительным taker-режимом. Пустая строка = выкл",
    )
    entry_price_gate_max: float = Field(
        default=0.50,
        description=(
            "Порог цены входа для entry_price_gate_detectors: сигналы с ценой >= порога "
            "отклоняются. НЕ оптимум — точки перегиба нет, ROI падает монотонно "
            "(0.44→+84.7%, 0.50→+56.6%, 0.60→+35.9%). Компромисс объёма и ROI"
        ),
    )
    live_auto_pause_enabled: bool = Field(
        default=False,
        description=(
            "Phase 2 (audit): demote_alerts_monitor auto-pause live-детекторов, провалившихся "
            "по live 7d экономике (mark_paused). Default False — promote human-only, demote "
            "обратим, но auto-action на реальных деньгах включает человек осознанно "
            "(False = alert-only, как было для virtual)."
        ),
    )
    live_exit_enabled: bool = Field(
        default=False,
        description=(
            "Phase 3 Task 1 (audit топ-1): position_manager прогоняет live-позиции TP/SL/"
            "max_hold и исполняет выход реальным SELL через close_live_position. Default "
            "False — до гейта ≥10 resolved live-ставок с net_pnl>=0 + явное решение владельца "
            "(см. docs/superpowers/plans/2026-07-02-audit-fixes-phase3.md). Аварийная "
            "TG-кнопка/endpoint close_live_position работают независимо от этого флага."
        ),
    )
    virtual_early_exit_enabled: bool = Field(
        default=False,
        description=(
            "Virtual TP/SL/mid_zone_exit early-exits в position_manager. Default False = "
            "hold-to-resolution — закреплённая политика (human decision 2026-07-07 по "
            "research/exit-trajectories-audit-2026-07-06 + virtual-stage-audit-2026-07-05: "
            "hold-контрфактика +127% vs as-is TP/SL, SL порезал 145 будущих winners). "
            "Сознательно отвязан от live_exit_enabled (гейт #648 висел на нём): включение "
            "live-exit НЕ должно молча реанимировать virtual TP/SL."
        ),
    )
    execution_daemon_enabled: bool = Field(
        default=False,
        description=(
            "Phase 3 Task 3 (audit топ-7): резидентный execution-демон (scripts/"
            "execution_daemon.py) забирает сигналы из Redis-очереди "
            "oracle:queue:execution_signals и исполняет их немедленно (секунды вместо "
            "минутного cron-тика). Default False — до гейта ≥10 resolved live-ставок с "
            "net_pnl>=0 + явное решение владельца. Демон-процесс запускается всегда (Docker "
            "service execution-daemon), но проверяет этот флаг в главном цикле перед "
            "каждым dequeue — при False только спит и не исполняет ничего. Cron "
            "v2-execution остаётся watchdog'ом независимо от этого флага."
        ),
    )
    d29_basket_enabled: bool = Field(
        default=False,
        description=(
            "Phase 3 Task 4 (audit research-gap): multi-leg basket-координатор для "
            "D29 (negrisk basket arb) — buy NO на ВСЕ рынки события, а не одну ногу. "
            "Default False — d29_scan остаётся выключенным (сам проверяет этот флаг "
            "в начале run_d29_scan) как и было. Когда True: d29_scan эмитит сигналы, "
            "а _process_signal (v2_execution.py) роутит D29-сигналы в "
            "app/services/basket_execution.py вместо обычного single-leg пути. "
            "Live-исполнение корзины дополнительно требует live_exit_enabled=true "
            "(unwind partial fill использует close_live_position)."
        ),
    )

    # --- Risk Engine (Phase 1 Stage 3) ---
    # См. docs/superpowers/plans/2026-05-risk-engine-hardening/03-bet-sizing-caps.md.
    # Документ §12: 'Max 5% капитала на one trade, Min stake $25 на $10k bankroll'.
    risk_max_per_bet_pct: float = Field(
        default=0.05,
        description="Top cap: max доля bankroll на одну ставку. 0.05 = 5%. 0 = disabled.",
    )
    risk_min_stake_usd: float = Field(
        default=25.0,
        description="Floor: блок если итоговая ставка < $25 (нерентабельна с fee×2 + slippage).",
    )

    # --- Reconciliation drift detection (Phase 1 Stage 4) ---
    # См. docs/superpowers/plans/2026-05-risk-engine-hardening/04-reconciliation-60s.md (Step 4.4).
    reconciliation_strike_threshold: int = Field(
        default=3,
        description="N подряд minute-failures до SET kill_switch. Plan §4: 'lучше слишком чувствительно'.",
    )
    reconciliation_strike_ttl_sec: int = Field(
        default=300,
        description="TTL strike counter в Redis. 4-я минута без breach → reset. 300=5min баланс между sensitive/noise.",
    )
    reconciliation_skip_when_execution_disabled: bool = Field(
        default=True,
        description="Skip reconciliation полностью если EXECUTION_ENABLED=false. Avoid TG spam при no live trades.",
    )

    # --- Risk Daemon (Phase 1 Stage 5) ---
    # См. docs/superpowers/plans/2026-05-risk-engine-hardening/05-risk-daemon-deadman.md.
    # Defense-in-depth: daemon hard-kill thresholds МЕНЕЕ строгие чем safety check —
    # safety check сначала срабатывает на soft thresholds → блок новых orders;
    # daemon ловит только если уже пропустили + позиции просели глубже.
    risk_daemon_heartbeat_interval_sec: int = Field(
        default=30, description="Daemon пишет bot:risk_daemon_alive каждые N секунд."
    )
    risk_daemon_check_interval_sec: int = Field(
        default=30, description="Daemon evaluate_and_kill цикл каждые N секунд."
    )
    risk_daemon_main_stale_threshold_sec: int = Field(
        default=300,
        description="Если bot:risk_heartbeat (main) старше N сек → main мёртв → alert.",
    )
    risk_daemon_daily_loss_hard_kill_pct: float = Field(
        default=0.05,
        description="Hard kill threshold: -5% bankroll. Safety check работает на -3% (soft). Defense-in-depth.",
    )
    risk_daemon_exposure_hard_cap_pct: float = Field(
        default=0.60,
        description="Hard cap: >60% bank deployed → kill. risk_engine hot-cap 40% — daemon catch overflow.",
    )
    risk_daemon_anomaly_orders_per_min: int = Field(default=10, description="N orders/min → suspected runaway loop.")
    risk_daemon_anomaly_rejections_per_min: int = Field(default=5, description="N rejections/min → suspected anomaly.")
    risk_daemon_skip_when_execution_disabled: bool = Field(
        default=True,
        description="Skip daemon checks если EXECUTION_ENABLED=false. Heartbeat пишется всё равно (мониторинг daemon health).",
    )
    kill_switch_auto_clear_enabled: bool = Field(
        default=True,
        description="Авто-снятие sticky kill_switch для транзиентных причин. Единый env-рубильник фичи.",
    )
    kill_switch_auto_clear_clean_cycles: int = Field(
        default=5,
        description="N подряд чистых циклов до авто-снятия транзиентного kill_switch (anomalies/exposure/drift). Persistent-фолты остаются sticky.",
    )

    # --- Live Drill / Mock Client (Phase 1 Stage 8) ---
    # См. docs/superpowers/plans/2026-05-risk-engine-hardening/08-live-drill.md
    # + research/live-drill-tabletop-2026-05/report.md.
    # Активирует FakeClobClient (drop-in replace py_clob_client_v2). Для drill /
    # CI / unit tests. НИКОГДА не для performance estimation strategy (rule #10:
    # manual promote). Активация через mock_seed гарантирует reproducible runs.
    execution_mock_mode: bool = Field(
        default=False,
        description="Drop-in replace py_clob_client_v2 с FakeClobClient (stateful in-memory).",
    )
    mock_seed: int | None = Field(
        default=None,
        description="Deterministic seed для reproducible drill runs. None=non-deterministic.",
    )
    mock_partial_rate: float = Field(
        default=0.3,
        description="Доля orders которые fill partially (10-90% original size).",
    )
    mock_failure_rate: float = Field(
        default=0.0,
        description="Доля API calls которые fail (5xx simulation, для failure injection drills).",
    )
    mock_latency_min_ms: int = Field(
        default=50,
        description="Минимальная simulated network latency. Реальный Polymarket API ~50-300ms.",
    )
    mock_latency_max_ms: int = Field(
        default=300,
        description="Максимальная simulated network latency.",
    )

    # --- Promote Gates (Phase 0 Stage 3) ---
    # Mandatory gates для shadow→virtual→live promote. См.
    # docs/superpowers/plans/2026-05-honest-baseline/03-mandatory-gates.md.
    promote_gate_min_n_terminal: int = Field(
        default=30,
        description="Минимум resolved (wins+losses) для promote eligibility. Документ §3.3.",
    )
    promote_gate_wilson_confidence: float = Field(
        default=0.95,
        description="Confidence level для Wilson lower bound в gate checks.",
    )
    promote_gate_wilson_threshold_fill: float = Field(
        default=0.50,
        description="Wilson 95% lower bound для fill rate должен быть ≥ этого. Документ §3.3.",
    )
    promote_gate_wilson_threshold_winrate: float = Field(
        default=0.50,
        description="Wilson 95% lower bound для win rate должен быть ≥ этого.",
    )

    polymarket_api_secret: str = Field(default="", description="Polymarket API Secret for User WS auth")
    polymarket_api_passphrase: str = Field(default="", description="Polymarket API Passphrase for User WS auth")
    polymarket_builder_key: str = Field(default="", description="Polymarket Builder API Key (UUID)")
    polymarket_builder_secret: str = Field(default="", description="Polymarket Builder API Secret (HMAC)")
    polymarket_builder_passphrase: str = Field(default="", description="Polymarket Builder API Passphrase")
    redeem_enabled: bool = Field(
        default=False, description="Включить боевой redeem live-позиций. Без этого — только dry-run отчёт."
    )
    polymarket_proxy: str = Field(
        default="", description="HTTP(S) proxy для Polymarket API (формат: http://user:pass@host:port)"
    )

    # --- Pure Scoring (Phase 2) ---
    scoring_pure_enabled: bool = Field(
        default=False,
        description="Shadow mode: call scoring_pure.score() alongside legacy and log diffs.",
    )
    scoring_pure_cutover: bool = Field(
        default=False,
        description="Cutover: use scoring_pure.score() as source of truth in live.",
    )

    # --- Pipeline Version (Stage 4 / Two-Stage ML Pipeline) ---
    pipeline_version: Literal["monolith", "v2_two_stage"] = Field(
        default="monolith",
        description=(
            "Inference path selector. 'monolith' = legacy L1+L2 (default). "
            "'v2_two_stage' = Layer A (Event Selector) + Layer B (Market Picker). "
            "Switched via .env on prod after Stage 5 cutover decision."
        ),
    )

    # --- D45 Chainlink Lead-Lag (Shadow mode crypto-updown detector) ---
    d45_chainlink_leadlag_enabled: bool = Field(
        default=False,
        description=(
            "Включить PythStreamService + 5s scan loop для D45 chainlink lead-lag. "
            "Shadow-mode only (никогда execution). См. app/detectors/d45_chainlink_leadlag.py"
        ),
    )
    d45_chainlink_leadlag_scan_interval_sec: float = Field(
        default=5.0,
        description="Период между scan'ами D45 chainlink lead-lag (sec)",
    )

    # --- M2 Liquidity Vacuum (measurement detector, shadow only) ---
    m2_vacuum_enabled: bool = Field(
        default=False,
        description=(
            "Включить M2 liquidity-vacuum realtime-хук в WSS. Measurement-only "
            "(shadow), никогда execution. См. app/detectors/m2_vacuum_hook.py"
        ),
    )

    # --- Phase 2 C1: Policy Engine (shadow-first, спека §2.6) ---
    policy_engine_enabled: bool = Field(
        default=False,
        description=(
            "False = shadow: 🟠 advisory молчит, 🟡 DQ и IPS-parse уходят всегда; True = advisory "
            "уходит полностью. Читается в policy_engine_daily.run_policy_engine_daily (C3a)."
        ),
    )

    # --- Weather Arbitrage ---
    weather_arb_enabled: bool = Field(default=False, description="Включить weather arbitrage scanner")
    weather_arb_divergence_threshold: float = Field(default=0.15, description="Мин divergence для сигнала (15%)")
    weather_arb_stake_usd: float = Field(default=5.0, description="Размер ставки в USD (консервативно)")
    weather_arb_max_signals_per_scan: int = Field(default=10, description="Макс сигналов за один скан")
    weatherapi_key: str = Field(default="", description="WeatherAPI.com API key (fallback для Open-Meteo)")

    # --- CORS ---
    cors_origins: list[str] = Field(
        default=["http://localhost:5173"],
        description="Allowed CORS origins (set via CORS_ORIGINS env var as JSON array)",
    )

    @property
    def r2_endpoint_url(self) -> str:
        """Generate R2 S3-compatible endpoint URL."""
        return f"https://{self.r2_account_id}.r2.cloudflarestorage.com"

    @field_validator("jwt_secret_key")
    @classmethod
    def _jwt_secret_strong_in_production(cls, v: str) -> str:
        """В production требуем JWT secret ≥32 символа.

        Пустой/слабый ключ → web-auth тривиально brute-force'ится.
        В dev/test (SENTRY_ENVIRONMENT != "production") разрешаем пустой.
        """
        if os.getenv("SENTRY_ENVIRONMENT", "production") == "production" and len(v) < 32:
            raise ValueError(
                "JWT_SECRET_KEY must be at least 32 chars in production "
                "(current: empty or too short — brute-force risk)"
            )
        return v

    @field_validator("cors_origins")
    @classmethod
    def _cors_no_wildcard_with_credentials(cls, v: list[str]) -> list[str]:
        """Запрет '*' в CORS — backend всегда поднимает с allow_credentials=True."""
        if "*" in v:
            raise ValueError("CORS_ORIGINS cannot include '*' — incompatible with allow_credentials=True")
        return v


@lru_cache
def get_settings() -> Settings:
    """Cached settings instance."""
    return Settings()

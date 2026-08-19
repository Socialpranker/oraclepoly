"""Vulture whitelist: suppress false positives for dynamically-used code."""

# --- FastAPI route handlers (called by the framework, not directly) ---
health_check  # noqa
get_markets  # noqa
sync_markets  # noqa
get_market_detail  # noqa
telegram_webhook  # noqa
webhook_info  # noqa
setup_webhook  # noqa
get_or_create_user  # noqa
unlock_market_insight  # noqa
cast_consensus_vote  # noqa
get_leaderboard  # noqa
get_user_votes  # noqa
check_user_vote  # noqa

# --- Pydantic models (used for request/response validation) ---
UserProfile  # noqa
UnlockMarketRequest  # noqa
UnlockMarketResponse  # noqa
VoteRequest  # noqa
VoteResponse  # noqa
LeaderboardEntry  # noqa
LeaderboardResponse  # noqa
TelegramUser  # noqa
TelegramChat  # noqa
TelegramMessage  # noqa
TelegramUpdate  # noqa
NewsMarketMatch  # noqa
ScrapedArticle  # noqa

# --- FastAPI Depends parameters (DI, used by framework) ---
admin  # noqa

# --- asyncpg listener callback parameters (required by asyncpg API) ---
conn  # noqa
pid  # noqa

# --- Protocol method stubs (interface signature, body is `...`) ---
trajectory  # noqa

# --- Cron job functions (called dynamically via cron_runner.py importlib) ---
run_storage_cleanup  # noqa
run_daily_analytics_report  # noqa
run_daily_benchmark  # noqa
run_news_pipeline  # noqa
run_oracle_recalculate_all  # noqa
run_registry_sync  # noqa
run_cluster_detection  # noqa
run_funding_chain_scan  # noqa
run_sector_win_rates_update  # noqa
run_subscription_renewal  # noqa
run_trade_poll  # noqa
run_market_sync  # noqa
run_leaderboard_refresh  # noqa
run_reward_distribution  # noqa
run_calibration_grid_search  # noqa
run_resolve_outcomes  # noqa
run_ml_market_picker_ev_fit  # noqa
websocket_endpoint  # noqa

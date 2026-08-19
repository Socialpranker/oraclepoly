/** Polymarket Event (parent of markets) */
export interface Event {
    id: string;
    polymarket_id: string;
    title: string;
    slug: string | null;
    description: string | null;
    category: string;
    image_url: string | null;
    volume: number;
    volume_24h: number;
    liquidity: number;
    start_date: string | null;
    end_date: string | null;
    is_active: boolean;
    markets_count: number;
    tags: Array<{ label: string }>;
    competitive: number | null;
    created_at: string;
    updated_at: string;
    markets?: Market[];
}

/** Market — canonical type used by all components */
export interface Market {
    id: string;
    polymarket_id: string;
    condition_id?: string;
    question: string;
    description?: string;
    category: string;
    icon_url: string | null;
    current_price: number | null;
    oracle_score: number | null;
    previous_oracle_score: number | null;
    sentiment_score: number | null;
    whale_confidence: number | null;
    risk_score: number | null;
    volume_24h: number | null;
    end_date: string | null;
    is_active: boolean;
    horizon?: string;
    event_id?: string | null;
    ai_hint?: string | null;
    time_to_resolution?: {
        days: number;
        hours: number;
        total_seconds: number;
    } | null;
    created_at?: string;
    updated_at?: string;
    score_decomposition?: ScoreDecomposition | null;
    latest_analysis?: MarketAnalysis | null;
    microstructure?: {
        spread: number | null;
        depth_yes: number | null;
        depth_no: number | null;
    } | null;
    liquidity_health?: 'healthy' | 'moderate' | 'illiquid' | null;
    momentum?: Momentum | null;
}

/** Score Decomposition — прозрачность Oracle Score */
export interface ScoreDecomposition {
    market_prior: number;
    whale_shift: number;
    smart_money_shift: number;
    fresh_wallet_shift: number;
    cluster_shift: number;
    registry_shift: number;
    oi_shift: number;
    concentration_shift: number;
    micro_shift: number;
    swan_impact: number;
    oracle_score: number;
    p_raw: number;
    top_whales: Array<{
        address: string;
        shift: number;
        direction: 'bullish' | 'bearish';
        roi_percent?: number;
        win_rate?: number;
    }>;
    confidence: {
        n_whales: number;
        n_holders: number;
        has_microstructure: boolean;
        has_oi: boolean;
    };
    updated_at: string | null;
}

/** Oracle Analysis (stored in market_analyses table) */
export interface MarketAnalysis {
    id: string;
    market_id: string;
    oracle_score_snapshot: number;
    price_snapshot: number;
    computed_analytics: {
        divergence: {
            oracle_vs_price: number;
            signal: 'underpriced' | 'overpriced' | 'fair';
            magnitude: 'strong' | 'moderate' | 'weak';
        };
        confidence: {
            data_quality: 'excellent' | 'good' | 'fair' | 'poor';
            signal_strength?: 'strong' | 'moderate' | 'weak';
            source_agreement?: {
                sentiment_whale: 'aligned' | 'divergent' | 'unknown';
                whale_price: 'aligned' | 'divergent' | 'unknown';
                };
            components_available?: number;
            n_whales?: number;
            n_scenarios?: number;
            whale_tx_count?: number;
            scenario_count?: number;
        };
        trend: {
            direction: 'rising' | 'falling' | 'stable' | 'unknown';
            delta: number;
        };
        risk?: {
            p_swan: number;
            risk_level: 'critical' | 'high' | 'medium' | 'low' | 'none';
        };
        bayesian?: {
            z_posterior: number;
            p_raw: number;
            p_swan: number;
            whale_contribution: number | null;
            market_prior: number;
        };
    };
    scenarios: Scenario[] | null;
    report: {
        summary: string;
        key_insight: string;
        watch_factors: string[];
    } | null;
    model_used: string | null;
    generated_at: string;
}

export interface Scenario {
    title: string;
    probability: number;
    narrative: string;
    catalyst: string;
    impact_on_score: string;
}

/** Уровень Order Book (bid/ask) */
export interface OrderBookLevel {
    price: number;
    size: number;
}

/** Order Book данные из /api/markets/{id}/orderbook */
export interface OrderBook {
    depth_yes: number;
    depth_no: number;
    spread: number | null;
    best_bid: number | null;
    best_ask: number | null;
    midpoint: number | null;
    health: 'healthy' | 'moderate' | 'illiquid' | null;
    levels: {
        bids: OrderBookLevel[];
        asks: OrderBookLevel[];
    };
    updated_at: string | null;
}

/** Momentum данные из market detail response */
export interface Momentum {
    velocity_10m: number;
    volume_spike: number;
    is_trending: boolean;
    trend_direction: 'up' | 'down' | 'flat';
}

/** Рынок, на котором кит активен (последние 7 дней) */
export interface WhaleActiveMarket {
    id: string;
    question: string;
    oracle_score: number | null;
    current_price: number | null;
    last_direction: string;
    last_trade_at: string;
}

/** Транзакция кита (для пагинированного списка) */
export interface WhaleTransaction {
    id: string;
    tx_hash: string;
    direction: string;
    outcome: string | null;
    amount: number;
    price: number | null;
    market_title: string | null;
    created_at: string;
}

/** Профиль кита в enriched transaction */
export interface TransactionWhaleProfile {
    id: string;
    address: string;
    win_rate: number;
    roi_percent: number;
    total_volume: number;
    expertise: string | null;
    pnl_usd: number;
    is_followed: boolean | null;
}

/** Model Health data (BSS + Murphy decomposition) */
export interface ModelHealthData {
  bss: number;
  bs_model: number | null;
  bs_market: number | null;
  murphy_reliability: number | null;
  murphy_resolution: number | null;
  murphy_uncertainty: number | null;
  phase_markers: { date: string; label: string }[];
}

/** Smart Money кошелёк из реестра */
export interface SmartMoneyWallet {
    address: string;
    label: string | null;
    specialization: string[];
    total_pnl: number;
    win_rate: number | null;
    roi_percent: number | null;
    whale_tier: string | null;
    avg_bet_size: number | null;
}

/** Anomaly detection результат */
export interface TransactionAnomaly {
    is_anomalous: boolean;
    flags: string[];
    account_age_days: number;
    unique_markets: number;
    total_trades: number;
    volume_concentration: number;
    risk_label: 'clean' | 'suspicious_new' | 'concentrated' | 'single_bet';
}



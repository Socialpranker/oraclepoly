export type BacktestSource = 'all' | 'hf' | 'live' | 'hf_enriched';
export type OracleMode = 'prod' | 'experimental' | 'none';
export type Sizing = 'fixed_10' | 'fixed_100' | 'kelly_quarter';
export type Side = 'YES' | 'NO';

export interface FeaturesFilter {
  category?: string[];
  price_range?: [number, number];
  horizon_days_min?: number;
  horizon_days_max?: number;
  side?: Side;
  whale_tx_count_min?: number;
  components_required?: string[];
}

export interface StrategyFilter {
  divergence_range?: [number, number];
  z_posterior_range?: [number, number];
  min_oracle_score?: number;
  max_oracle_score?: number;
}

export interface BacktestRequest {
  source: BacktestSource;
  window_from?: string;
  window_to?: string;
  features_filter?: FeaturesFilter;
  oracle_mode?: OracleMode;
  strategy?: StrategyFilter;
  sizing?: Sizing;
  walk_forward?: { enabled: boolean; train_ratio?: number };
  limit?: number;
}

export interface TradeRef {
  market_id: string;
  snapshot_at: string;
  category: string | null;
  market_price: number;
  oracle_score: number;
  pnl: number;
  won: boolean;
}

export interface EquityPoint {
  date: string;
  equity: number;
  bet_count: number;
}

export interface BacktestResult {
  total_bets: number;
  wins: number;
  losses: number;
  roi_pct: number;
  sharpe: number;
  max_dd_pct: number;
  win_rate: number;
  ci_95_roi: [number, number];
  components_used: string[];
  warnings: string[];
  equity_curve: EquityPoint[];
  top_wins: TradeRef[];
  top_losses: TradeRef[];
}

export interface ABMetricDelta {
  baseline: number;
  variant: number;
  delta: number;
  significant: boolean;
}

export interface ABResult {
  baseline: BacktestResult;
  variant: BacktestResult;
  roi_delta: ABMetricDelta;
  sharpe_delta: ABMetricDelta;
  win_rate_delta: ABMetricDelta;
}

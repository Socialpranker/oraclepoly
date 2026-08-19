import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { adminFetch } from '../lib/telegram';

// --- Типы ---

export interface BotAlert {
  level: 'error' | 'warning' | 'info';
  message: string;
}

export interface CircuitBreakers {
  drawdown_tripped: boolean;
  daily_loss_tripped: boolean;
  low_balance: boolean;
}

export interface BotStatus {
  status: 'live' | 'running' | 'stopped' | 'dry_run';
  execution_live: boolean;
  execution_enabled: boolean;
  execution_dry_run: boolean;
  last_cron_cycle: string | null;
  circuit_breakers: CircuitBreakers;
  alerts: BotAlert[];
}

export interface StrategyToday {
  bets: number;
  won: number;
  lost: number;
  pnl: number;
}

export interface StrategyAllTime {
  win_rate: number;
  profit_factor: number;
  total_pnl: number;
  total_bets: number;
}

export interface BotStrategy {
  key: string;
  enabled: boolean;
  bankroll: number;
  initial_bankroll: number;
  quota: number;
  roi_pct: number;
  open_positions: number;
  pyramid_positions: number;
  today: StrategyToday;
  all_time: StrategyAllTime;
  earned: number;
  total_withdrawn: number;
  total_reinvested: number;
  frozen: number;
  drawdown_pct: number;
  daily_spent: number;
  daily_limit: number;
  signal_source: 'oracle' | 'contra_bias' | 'market_baseline';
  resolved_count: number;
}

export interface BotStrategiesResponse {
  strategies: BotStrategy[];
  totals: {
    bankroll: number;
    initial_bankroll: number;
    roi_pct: number;
    open_positions: number;
    pyramid_positions: number;
    today_pnl: number;
    total_earned: number;
    total_withdrawn: number;
    total_reinvested: number;
    total_frozen: number;
    total_reserved: number;
    free_capital: number;
    portfolio_bankroll: number;
  };
}

export interface BotPosition {
  id: string;
  market_id: string;
  market_question: string | null;
  side: string;
  entry_price: number;
  stake: number;
  status: string;
  pnl?: number;
  opened_at: string;
  closed_at?: string;
  meta?: Record<string, unknown>;
}

export interface BotFill {
  id: string;
  strategy_key: string;
  market_id: string;
  market_question: string | null;
  side: string;
  entry_price: number;
  stake: number;
  pnl: number;
  status: string;
  opened_at: string;
  closed_at?: string;
}

export interface EquityPoint {
  date: string;
  value: number;
}

// --- Hooks ---

export function useBotStatus() {
  return useQuery<BotStatus>({
    queryKey: ['bot', 'status'],
    queryFn: () => adminFetch<BotStatus>('/api/bot/status'),
    staleTime: 30_000,
    refetchInterval: 30_000,
  });
}

// Legacy execution-mode (deprecated — use Stage). 'dry' === stage='virtual',
// 'live' === stage='live'.
export type BotMode = 'dry' | 'live';

// Detector lifecycle stage (4-stage architecture, см. docs/detector_lifecycle.md).
//   'backtest' — гипотеза проверена на canonical HF (нет в БД для prod)
//   'shadow'   — production cron эмитит signals, no money
//   'virtual'  — full pipeline через ML+sizing+safety, virtual_* execution_orders
//   'live'     — реальные ордера в Polymarket CLOB
export type Stage = 'backtest' | 'shadow' | 'virtual' | 'live' | 'paused';

// Maps URL ?stage= param ↔ legacy BotMode для backwards compat.
export function stageToMode(stage: Stage): BotMode {
  return stage === 'live' ? 'live' : 'dry';
}
export function modeToStage(mode: BotMode): Stage {
  return mode === 'live' ? 'live' : 'virtual';
}

// BotTab — теперь 4 таба верхнего уровня в /bot (по lifecycle stages).
// Legacy 'status'/'internals' — мигрировал в shadow tab (с раскрытием internals).
export type BotTab = 'backtest' | 'shadow' | 'virtual' | 'live';

// ===== Pipeline V2 hooks =====
export interface V2DetectorRecent14d {
  n_total: number;
  n_resolved: number;
  n_wins: number;
  n_losses: number;
  wr: number | null;
  roi_net_pct: number | null;
  trend_pp: number | null;
  verdict: 'GREEN' | 'YELLOW' | 'RED';
}

export interface V2DetectorLifecycle {
  stage: Stage;
  backtest: {
    available: boolean;
    verdict: 'PASS' | 'FAIL' | 'MARGINAL' | null;
    honest_daily_usd_at_1k: number | null;
    honest_daily_usd_at_5k: number | null;
    wr_pct: number | null;
    roi_net_pct: number | null;
  };
  shadow: { signals_per_day_7d: number };
  virtual: { pnl_usd_7d: number };
  live: { pnl_usd_7d: number };
}

export interface V2DetectorRow {
  detector: string;
  n_total: number;
  n_resolved: number;
  n_wins: number;
  n_losses: number;
  n_expired: number;
  avg_roi_pct: number | null;
  avg_confidence: number | null;
  last_emit_at: string | null;
  wr: number | null;
  recent_14d?: V2DetectorRecent14d;
  lifecycle?: V2DetectorLifecycle;
  calibrator: {
    fitted: boolean;
    n_train?: number;
    brier_raw?: number;
    brier_holdout?: number;
    fit_at?: string | null;
  };
}

export interface V2ModelInfo {
  active: {
    version: string;
    n_train: number;
    n_holdout: number;
    train_brier: number;
    holdout_brier: number;
    fit_at: string | null;
  } | null;
  history: Array<{
    version: string;
    holdout_brier: number;
    fit_at: string;
    is_active: boolean;
  }>;
}

export interface V2RecentSignal {
  signal_id: string;
  detector: string;
  market_id: string;
  side: 'YES' | 'NO';
  raw_p: number;
  calibrated_p: number;
  meta_p: number;
  entry_price: number;
  ev_pct: number;
  stake_usd: number;
  opened_at: string;
  resolved: boolean;
  outcome: string | null;
  realized_roi_pct: number | null;
  realized_pnl_usd: number | null;
  category: string | null;
}

export function useV2Detectors() {
  return useQuery<{ detectors: V2DetectorRow[] }>({
    queryKey: ['bot', 'v2', 'detectors'],
    queryFn: () => adminFetch('/api/bot/v2/detectors'),
    refetchInterval: 30_000,
  });
}

export function useV2Model() {
  return useQuery<V2ModelInfo>({
    queryKey: ['bot', 'v2', 'model'],
    queryFn: () => adminFetch('/api/bot/v2/model'),
    refetchInterval: 60_000,
  });
}

// Backtest metadata из docs/backtests/{detector}.md (parsed by API).
export interface BacktestSections {
  hypothesis?: string;
  filters?: Array<{ key: string; value: string }>;
  bucket_wr_table?: Array<Record<string, string | number>>;
  category_breakdown?: Array<Record<string, string | number>>;
  promotion_criteria?: Record<string, string>;
}

export interface DetectorBacktest {
  detector: string;
  available: boolean;
  verdict?: 'PASS' | 'FAIL' | 'MARGINAL';
  backtest_date?: string;
  n_signals?: number;
  backtest_days?: number;
  roi_net_pct?: number;
  wr_pct?: number;
  daily_realistic_usd_at_5k?: number;
  daily_realistic_usd_at_1k?: number;
  honest_haircut_factor?: number;
  honest_daily_usd_at_5k?: number;
  honest_daily_usd_at_1k?: number;
  honest_haircut_reason?: string;
  trades_per_day?: number;
  adversarial_mirror_pass?: boolean;
  adversarial_mirror_note?: string;
  post_v2_split?: string;
  body_markdown?: string;
  sections?: BacktestSections;
  message?: string;
}

export function useDetectorBacktest(detector: string | null) {
  return useQuery<DetectorBacktest>({
    queryKey: ['bot', 'v2', 'detector', detector, 'backtest'],
    queryFn: () =>
      adminFetch(`/api/bot/v2/detectors/${encodeURIComponent(detector!)}/backtest`),
    enabled: !!detector,
    staleTime: 5 * 60_000,
  });
}

export function useV2RecentSignals(limit = 50) {
  return useQuery<{ signals: V2RecentSignal[] }>({
    queryKey: ['bot', 'v2', 'recent-signals', limit],
    queryFn: () => adminFetch(`/api/bot/v2/recent-signals?limit=${limit}`),
    refetchInterval: 30_000,
  });
}

export interface V2ActivityDay {
  date: string;
  attempted: number;
  rejected: number;
  placed: number;
  settled: number;
  wins: number;
  losses: number;
  /** TP/SL/mid_zone_exit via position_manager (subset of settled). */
  early_exits: number;
  /** Take-profit subset of early_exits. */
  tp_exits: number;
  /** Stop-loss subset of early_exits. */
  sl_exits: number;
  /** Mid-zone exit (price ушёл из [0.30, 0.85]). */
  mid_zone_exits: number;
  pnl_net_usd: number;
  pnl_gross_usd: number;
  staked_usd: number;
  rejected_pnl_if_taken: number;
}

export interface V2Activity {
  summary: {
    days: number;
    total_attempted: number;
    total_rejected: number;
    total_placed: number;
    total_settled: number;
    total_wins: number;
    total_losses: number;
    total_early_exits: number;
    total_tp_exits: number;
    total_sl_exits: number;
    total_mid_zone_exits: number;
    total_pnl_net_usd: number;
    total_pnl_gross_usd: number;
    total_staked_usd: number;
    total_rejected_pnl_if_taken: number;
  };
  daily: V2ActivityDay[];
}

export function useV2Activity(days = 14) {
  return useQuery<V2Activity>({
    queryKey: ['bot', 'v2', 'activity', days],
    queryFn: () => adminFetch<V2Activity>(`/api/bot/v2/activity?days=${days}`),
    refetchInterval: 60_000,
  });
}

export interface V2ActivityDayItem {
  id: string;
  ts: string | null;
  detector: string | null;
  strategy_key: string | null;
  market_id: string;
  question: string | null;
  category: string | null;
  side: 'YES' | 'NO';
  stake_usd: number;
  signal_price: number;
  status: string;
  cancel_reason: string | null;
  rejection_reason: string | null;
  outcome: 'WIN' | 'LOSS' | null;
  net_pnl_usd: number | null;
}

export interface V2ActivityDayDetector {
  detector: string;
  placed: number;
  rejected: number;
  settled: number;
  wins: number;
  losses: number;
  wr: number | null;
  stake_usd: number;
  pnl_net_usd: number;
  roi_net_pct: number | null;
}

export interface V2ActivityDayRejection {
  reason: string;
  count: number;
}

export interface V2ActivityDayDetail {
  date: string;
  count: number;
  items: V2ActivityDayItem[];
  by_detector: V2ActivityDayDetector[];
  by_rejection: V2ActivityDayRejection[];
}

export function useV2ActivityDay(date: string | null) {
  return useQuery<V2ActivityDayDetail>({
    queryKey: ['bot', 'v2', 'activity', 'day', date],
    queryFn: () =>
      adminFetch<V2ActivityDayDetail>(`/api/bot/v2/activity/day/${date}`),
    enabled: !!date,
    staleTime: 60_000,
  });
}

export interface V2ActivityCalendarDay {
  date: string;
  placed: number;
  rejected: number;
  settled: number;
  wins: number;
  pnl_net_usd: number;
}

export interface V2ActivityCalendar {
  days: number;
  items: V2ActivityCalendarDay[];
}

export function useV2ActivityCalendar(days = 60) {
  return useQuery<V2ActivityCalendar>({
    queryKey: ['bot', 'v2', 'activity', 'calendar', days],
    queryFn: () =>
      adminFetch<V2ActivityCalendar>(`/api/bot/v2/activity/calendar?days=${days}`),
    refetchInterval: 5 * 60_000,
  });
}

export interface V2SignalRateRow {
  detector: string;
  total_signals: number;
  rate_per_hour: number;
  last_signal_at: string | null;
  minutes_since_last: number;
  is_stale: boolean;
}

export function useV2SignalRate(days = 7) {
  return useQuery<{ detectors: V2SignalRateRow[]; days: number }>({
    queryKey: ['bot', 'v2', 'signal-rate', days],
    queryFn: () => adminFetch(`/api/bot/v2/signal-rate?days=${days}`),
    refetchInterval: 30_000,
  });
}

export interface V2GateRow {
  detector: string;
  n_total: number;
  n_resolved: number;
  n_wins: number;
  n_losses: number;
  wr: number | null;
  pnl_usd: number;
  roi_pct: number | null;
  required_n: number;
  progress_pct: number;
  distinct_markets: number;
  distinct_events: number;
  distinct_days: number;
  state:
    | 'shadow_collecting'
    | 'shadow_watch'
    | 'gate_evaluating'
    | 'mini_live'
    | 'live_full'
    | 'disabled';
}

export function useV2GateStatus() {
  return useQuery<{ detectors: V2GateRow[] }>({
    queryKey: ['bot', 'v2', 'gate-status'],
    queryFn: () => adminFetch(`/api/bot/v2/gate-status`),
    refetchInterval: 60_000,
  });
}

// ===== Legacy V1 strategies (used by StrategyDetail/BotHistory pages) =====

export function useBotStrategies(mode: BotMode = 'dry') {
  return useQuery<BotStrategiesResponse>({
    queryKey: ['bot', 'strategies', mode],
    queryFn: () => adminFetch<BotStrategiesResponse>(`/api/bot/strategies?mode=${mode}`),
    staleTime: 60_000,
  });
}

export function useBotPositions(strategyKey: string, status: 'open' | 'all' = 'open') {
  return useQuery<{ positions: BotPosition[] }>({
    queryKey: ['bot', 'positions', strategyKey, status],
    queryFn: () =>
      adminFetch<{ positions: BotPosition[] }>(
        `/api/bot/strategies/${strategyKey}/positions?status=${status}`
      ),
    staleTime: 30_000,
  });
}

export function useBotRecentFills(limit = 10, mode: BotMode = 'dry') {
  return useQuery<{ fills: BotFill[] }>({
    queryKey: ['bot', 'recent-fills', limit, mode],
    queryFn: () => adminFetch<{ fills: BotFill[] }>(`/api/bot/recent-fills?limit=${limit}&mode=${mode}`),
    staleTime: 60_000,
  });
}

export function useBotEquity(days = 7, mode: BotMode = 'dry') {
  return useQuery<{ points: EquityPoint[] }>({
    queryKey: ['bot', 'equity', days, mode],
    queryFn: () => adminFetch<{ points: EquityPoint[] }>(`/api/bot/equity?days=${days}&mode=${mode}`),
    staleTime: 5 * 60_000,
  });
}

export interface DailyPnLPoint {
  date: string;
  bets_placed: number;
  bets_settled: number;
  total_stake: number;
  wins: number;
  losses: number;
  pnl: number;
  gross_win: number;
  gross_loss: number;
}

export interface DailyPnLResponse {
  mode: BotMode;
  days: number;
  points: DailyPnLPoint[];
  totals: {
    bets_placed: number;
    bets_settled: number;
    total_stake: number;
    wins: number;
    losses: number;
    pnl: number;
    gross_win: number;
    gross_loss: number;
    win_rate: number;
  };
}

export function useBotDailyPnL(days = 14, mode: BotMode = 'live') {
  return useQuery<DailyPnLResponse>({
    queryKey: ['bot', 'daily-pnl', days, mode],
    queryFn: () => adminFetch<DailyPnLResponse>(`/api/bot/daily-pnl?days=${days}&mode=${mode}`),
    staleTime: 60_000,
  });
}

export interface LiveMetricStrategy {
  strategy_key: string;
  live_resolved: number;
  live_wins: number;
  live_losses: number;
  live_wr: number | null;
  live_total_stake: number;
  live_total_pnl: number;
  avg_win: number | null;
  avg_loss: number | null;
  wr_30d: number | null;
  roi_30d: number | null;
  drawdown_pct: number;
  current_stake: number;
  kelly_fraction: number | null;
  safety_mult: number;
  damping: number;
  paused: boolean;
  paused_reason: string | null;
  paused_at: string | null;
  dry_resolved_at_start: number;
  dry_wr_at_start: number | null;
  dry_roi_at_start: number | null;
  last_settled_at: string | null;
  last_recomputed_at: string | null;
}

export function useBotLiveMetrics() {
  return useQuery<{ strategies: LiveMetricStrategy[] }>({
    queryKey: ['bot', 'live-metrics'],
    queryFn: () => adminFetch<{ strategies: LiveMetricStrategy[] }>('/api/bot/live-metrics'),
    staleTime: 60_000,
  });
}

export function useStrategyPause() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) =>
      adminFetch<{ strategy_key: string; paused: boolean }>(
        `/api/bot/strategies/${key}/pause`,
        { method: 'POST' },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bot'] });
    },
  });
}

export function useStrategyResume() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (key: string) =>
      adminFetch<{ strategy_key: string; paused: boolean }>(
        `/api/bot/strategies/${key}/resume`,
        { method: 'POST' },
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bot'] });
    },
  });
}

export function useToggleStrategy() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (strategyKey: string) =>
      adminFetch<{ strategy_key: string; enabled: boolean }>(
        `/api/bot/strategies/${strategyKey}/toggle`,
        { method: 'POST' }
      ),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['bot', 'strategies'] });
    },
  });
}

// --- History ---

export interface HistoryDailyEntry {
  date: string;
  events: number;
  wins: number;
  losses: number;
  net_pnl: number;
  frozen_released: number;
  strategies: Record<string, { wins: number; losses: number; pnl: number }>;
}

export interface HistoryLedgerEntry {
  date: string;
  strategy_key: string;
  event_type: string;
  question: string;
  category: string;
  side: string;
  stake: number;
  entry_price: number;
  pnl: number;
  balance_after: number;
}

export interface HistoryResponse {
  daily: HistoryDailyEntry[];
  ledger: HistoryLedgerEntry[];
  summary: {
    total_events: number;
    total_pnl: number;
    total_frozen_released: number;
    total_won: number;
    total_lost: number;
  };
}

export function useBotHistory(days = 14, strategyKey?: string, mode: BotMode = 'dry') {
  const params = new URLSearchParams({ days: String(days), mode });
  if (strategyKey) params.set('strategy_key', strategyKey);
  return useQuery<HistoryResponse>({
    queryKey: ['bot', 'history', days, strategyKey, mode],
    queryFn: () => adminFetch<HistoryResponse>(`/api/bot/history?${params}`),
    staleTime: 2 * 60_000,
  });
}

// ===== Ensemble opportunities (Internals tab) =====

export interface EnsemblePair {
  det_a: string;
  det_b: string;
  markets: number;
  wins: number;
  wr_pct: number;
  net_roi_pct: number;
}

export interface EnsembleOpportunities {
  pairs: EnsemblePair[];
}

export function useEnsembleOpportunities() {
  return useQuery<EnsembleOpportunities>({
    queryKey: ['bot', 'v2', 'ensemble', 'opportunities'],
    queryFn: () => adminFetch<EnsembleOpportunities>('/api/bot/v2/ensemble/opportunities'),
    staleTime: 5 * 60_000,
  });
}

// ===== Pre-Live Readiness =====

export interface ReadinessCriterion {
  value: number | null;
  threshold: number;
  pass: boolean;
  median_fill_rate?: number;
  note?: string;
}

export interface ReadinessBucket {
  bucket: string;
  detector: string;
  category: string;
  entry_zone: string;
  criteria: {
    n_events_30d: ReadinessCriterion;
    inflation_factor: ReadinessCriterion;
    drift_wr_pp: ReadinessCriterion;
    hf_stability_delta_wr_pp: ReadinessCriterion;
    capacity_ratio: ReadinessCriterion;
    settlement_e2e: ReadinessCriterion;
  };
  verdict: 'ready' | 'not_ready';
}

export interface ReadinessGateStatus {
  summary: {
    total_buckets: number;
    ready_buckets: number;
    next_actions: string[];
  };
  buckets: ReadinessBucket[];
}

export function useV2DryReadinessStatus() {
  return useQuery<ReadinessGateStatus>({
    queryKey: ['bot', 'v2', 'dry', 'gate_status'],
    queryFn: () => adminFetch<ReadinessGateStatus>('/api/bot/v2/dry/gate_status'),
    refetchInterval: 60_000,
  });
}

// ===== Lifecycle progress + drill-down (PR 1: 4-stage UI) =====

export interface LifecycleStageCount {
  total: number;
  label: string;
}

export interface LifecycleCounts {
  backtest: LifecycleStageCount;
  shadow: LifecycleStageCount;
  virtual: LifecycleStageCount;
  live: LifecycleStageCount;
}

export function useLifecycleCounts() {
  return useQuery<LifecycleCounts>({
    queryKey: ['bot', 'v2', 'lifecycle', 'counts'],
    queryFn: () => adminFetch<LifecycleCounts>('/api/bot/v2/lifecycle/counts'),
    refetchInterval: 60_000,
  });
}

export interface DetectorBucket {
  category: string;
  zone: string;
  n_signals_7d: number;
  n_resolved_7d: number;
  wr_pct: number | null;
  roi_net_pct: number | null;
  virtual_pnl_7d: number;
  live_pnl_7d: number;
}

export interface DetectorBucketsResponse {
  detector: string;
  window_days: number;
  buckets: DetectorBucket[];
}

export function useDetectorBuckets(detector: string | null) {
  return useQuery<DetectorBucketsResponse>({
    queryKey: ['bot', 'v2', 'detector', detector, 'buckets'],
    queryFn: () =>
      adminFetch<DetectorBucketsResponse>(
        `/api/bot/v2/detectors/${encodeURIComponent(detector!)}/buckets`,
      ),
    enabled: !!detector,
    staleTime: 60_000,
  });
}

export interface BacktestMapItem {
  detector: string;
  verdict: 'PASS' | 'FAIL' | 'MARGINAL' | null;
  honest_daily_usd_at_5k: number | null;
  honest_daily_usd_at_1k: number | null;
  honest_haircut_factor: number | null;
  trades_per_day: number | null;
  roi_net_pct: number | null;
  wr_pct: number | null;
  n_signals: number | null;
  backtest_days: number | null;
  stage: Stage | null;
}

export interface BacktestMapResponse {
  items: BacktestMapItem[];
}

export function useBacktestMap() {
  return useQuery<BacktestMapResponse>({
    queryKey: ['bot', 'v2', 'lifecycle', 'backtest_map'],
    queryFn: () => adminFetch<BacktestMapResponse>('/api/bot/v2/lifecycle/backtest_map'),
    staleTime: 5 * 60_000,
  });
}

export interface PromotionGate {
  passed: boolean | null;
  value: number | string | null;
  threshold?: number;
  threshold_pp?: number;
  delta?: number | null;
  canonical?: number | null;
  label: string;
}

export interface PromotionReadinessItem {
  detector: string;
  stage: Stage | null;
  shadow_7d: {
    total_signals: number;
    resolved: number;
    terminal: number;
    wins: number;
    distinct_events: number;
    distinct_markets: number;
    recent_wr_pct: number | null;
  };
  canonical: {
    verdict: 'PASS' | 'FAIL' | 'MARGINAL' | null;
    canonical_wr_pct: number | null;
    honest_haircut_factor: number | null;
    honest_daily_usd_at_5k: number | null;
  };
  gates: {
    n_events: PromotionGate;
    wr_match: PromotionGate;
    verdict: PromotionGate;
  };
  ready: boolean;
  gates_passed: number;
  gates_total: number;
  wr_delta_pp: number | null;
}

export interface PromotionReadinessResponse {
  detectors: PromotionReadinessItem[];
}

export function usePromotionReadiness() {
  return useQuery<PromotionReadinessResponse>({
    queryKey: ['bot', 'v2', 'lifecycle', 'promotion_readiness'],
    queryFn: () =>
      adminFetch<PromotionReadinessResponse>('/api/bot/v2/lifecycle/promotion_readiness'),
    refetchInterval: 60_000,
  });
}

export interface LiveReadinessGate {
  passed: boolean | null;
  value: number | string | null;
  threshold?: number;
  label: string;
}

// gates_v2 — honest signal-level gates (evaluate_promote_gates). РОВНО то, по
// чему судит TG promote virtual→live. Карточка/map должны судить по ним, не по
// order-level legacy `gates`. См. [[shadow-roi-virtual-pnl]].
//   n_min              — signal terminal ≥ 30
//   pnl_positive       — signal ROI > 0 AND Sortino > thr AND Wilson WR lower ≥ break-even
//   fill_rate          — Wilson fill lower ≥ 50%
//   adversarial_mirror — manual (not_evaluated → blocks)
//   days_accumulated   — ≥14d virtual
//   execution_enabled  — env flag
export interface LiveReadinessGateV2 {
  passed: boolean;
  value: number | string | boolean | null;
  label: string;
  threshold?: number;
  // pnl_positive
  sum_roi_net_pct?: number;
  sortino?: number | 'inf';
  sortino_threshold?: number;
  winrate_pct?: number | null;
  wilson_lower_winrate_pct?: number | null;
  wilson_upper_winrate_pct?: number | null;
  break_even_wr_pct?: number;
  break_even_wr_source?: 'payout_aware' | 'fallback_flat';
  // fill_rate
  wilson_lower_pct?: number | null;
  wilson_upper_pct?: number | null;
  threshold_pct?: number;
}

export type PromoteDecisionV2 =
  | 'READY'
  | 'INSUFFICIENT_DATA'
  | 'BLOCKED'
  | 'MANUAL_OVERRIDE_NEEDED';

export interface LiveReadinessGatesV2 {
  n_min: LiveReadinessGateV2;
  pnl_positive: LiveReadinessGateV2;
  fill_rate: LiveReadinessGateV2;
  adversarial_mirror: LiveReadinessGateV2;
  days_accumulated: LiveReadinessGateV2;
  execution_enabled: LiveReadinessGateV2;
}

export type PortfolioId = 'rules' | 'ml' | 'sentiment' | 'whale_copy' | 'external';

export interface LiveReadinessItem {
  detector: string;
  stage: Stage | null;
  portfolio_id: PortfolioId | null;  // Phase 4: assigned portfolio
  gate_overrides_applied: string[];  // Phase 4: e.g. ['adversarial_mirror']
  promoted_at: string | null;
  days_virtual: number;
  virtual_7d: {
    n_placed: number;
    n_filled: number;
    n_settled: number;
    fill_rate_pct: number | null;
    pnl_usd: number;
    // PnL split (см. инцидент 2026-05-18):
    //   pnl_resolved_usd — реально резолвнутые WIN/LOSS (правда).
    //   pnl_paper_usd    — EARLY_EXIT mark-to-market (может быть inflated в extreme zones).
    pnl_resolved_usd: number;
    pnl_paper_usd: number;
    // v4: signal-level honest economics (hold-to-resolution) — ПЕРВИЧНАЯ метрика
    // оценки бойца, по ней судит promote-гейт G1 (не order-level pnl_usd).
    realized_roi_net_pct?: number | null;
    sig_terminal?: number | null;
    wins: number;
    losses: number;
    early_exits: number;
    wr_pct: number | null;
  };
  canonical: {
    verdict: 'PASS' | 'FAIL' | 'MARGINAL' | null;
    honest_daily_usd_at_5k: number | null;
    projected_7d_usd: number | null;
  };
  // Legacy order-level gates (5) — backward-compat, paper-надутый pnl_usd.
  // НЕ использовать для gate-display: см. gates_v2 ниже.
  gates: {
    pnl_positive: LiveReadinessGate;
    fill_rate: LiveReadinessGate;
    adversarial_mirror: LiveReadinessGate;
    days_accumulated: LiveReadinessGate;
    execution_enabled: LiveReadinessGate;
  };
  ready: boolean;
  gates_passed: number;
  gates_total: number;
  // Honest signal-level gates (6) — SSoT, ровно по чему судит TG promote.
  // ready_v2 = decision == "READY" (все 6 pass). Карточка/map судят по ним.
  gates_v2: LiveReadinessGatesV2;
  gates_decision_v2: PromoteDecisionV2;
  ready_v2: boolean;
  gates_v2_passed: number;
  gates_v2_total: number;
  settle_velocity: SettleVelocity;
}

// Phase 5: Portfolio rollup для tabs/cards
export interface Portfolio {
  portfolio_id: PortfolioId;
  display_name: string;
  bankroll_usd: number;
  exposure_cap_usd: number;
  exposure_used_usd: number;
  exposure_remaining_usd: number;
  kelly_fraction: number;
  gate_overrides: Record<string, string>;
  n_detectors: number;
  enabled: boolean;
}

export interface PortfoliosResponse {
  portfolios: Portfolio[];
}

export function usePortfolios() {
  return useQuery<PortfoliosResponse>({
    queryKey: ['bot', 'v2', 'portfolios'],
    queryFn: () => adminFetch<PortfoliosResponse>('/api/bot/v2/portfolios'),
    refetchInterval: 60_000,
  });
}

export interface SettleVelocityBuckets {
  under_24h_pct: number;
  d1_3_pct: number;
  d3_7_pct: number;
  over_7d_pct: number;
}

export interface SettleVelocity {
  buckets: SettleVelocityBuckets | null;
  median_hours: number | null;
  n: number;
  window_days?: number;
}

export interface ShadowCandidate {
  detector: string;
  shadow_gates_passed: number;
  distinct_events_7d: number;
  recent_wr_pct: number;
  canonical_wr_pct: number;
  honest_daily_usd_at_5k: number | null;
}

export interface LiveReadinessResponse {
  detectors: LiveReadinessItem[];
  candidates_from_shadow: ShadowCandidate[];
  execution_enabled: boolean;
  settle_velocity_global: SettleVelocity;
}

export function useLiveReadiness() {
  return useQuery<LiveReadinessResponse>({
    queryKey: ['bot', 'v2', 'lifecycle', 'live_readiness'],
    queryFn: () =>
      adminFetch<LiveReadinessResponse>('/api/bot/v2/lifecycle/live_readiness'),
    refetchInterval: 60_000,
  });
}

// =============================================================================
// /v2/daily_summary — простая дневная сводка: placed/won/lost/net.
// Прицел — ответить на вопрос «сколько поставили, выиграли, проиграли».
// =============================================================================

export interface DailySummaryRow {
  day: string;  // YYYY-MM-DD
  bets: number;
  staked_usd: number;
  won_usd: number;
  lost_usd: number;   // negative
  net_pnl: number;
  n_settled: number;
}

export interface DailySummaryResponse {
  days: DailySummaryRow[];
  totals_14d: {
    bets: number;
    staked_usd: number;
    won_usd: number;
    lost_usd: number;
    net_pnl: number;
    n_settled: number;
  };
}

export function useDailySummary() {
  return useQuery<DailySummaryResponse>({
    queryKey: ['bot', 'v2', 'daily_summary'],
    queryFn: () => adminFetch<DailySummaryResponse>('/api/bot/v2/daily_summary'),
    refetchInterval: 60_000,
  });
}

// =============================================================================
// /v2/lifecycle/live_dashboard — Trading Floor (operational dashboard).
// См. plans/wiggly-nibbling-stardust.md (PR 1: Live).
// =============================================================================

export interface BankrollSnapshot {
  wallet_balance_usd: number;
  initial_bankroll_usd: number;
  drawdown_pct: number | null;
  kill_switch_tripped: boolean;
  kill_switch_reason?: string | null;
  wallet_source: 'wallet' | 'db' | 'initial_fallback';
}

export interface EquityCurvePoint {
  date: string;
  live_pnl_cum: number;
  live_pnl_day: number;
  virtual_baseline_cum: number;
  virtual_baseline_day: number;
}

export interface LiveAlert {
  level: 'info' | 'warning' | 'critical';
  code: string;
  message: string;
}

export interface OpenPosition {
  detector: string;
  market_id: string;
  side: string;
  entry_price: number | null;
  stake_usd: number;
  unrealized_pnl_usd: number;
  opened_at: string | null;
}

export interface OpenPositionsSummary {
  count: number;
  total_exposure_usd: number;
  exposure_pct_of_bankroll: number | null;
  unrealized_pnl_usd: number;
  list: OpenPosition[];
}

export interface FillQuality {
  n_placed: number;
  n_filled: number;
  fill_rate_pct: number | null;
  avg_slippage_usd: number | null;
  fee_burn_usd: number;
  fee_burn_pct_of_pnl: number | null;
}

export interface LiveDetectorItem {
  detector: string;
  live_7d: {
    n_placed: number;
    n_filled: number;
    wins: number;
    losses: number;
    pnl_usd: number;
    fill_rate_pct: number | null;
    slippage_usd: number;
    fees_usd: number;
  };
  virtual_7d_projection_usd: number | null;
  slippage_delta_usd: number | null;
}

export interface LiveDashboardResponse {
  bankroll: BankrollSnapshot;
  equity_curve_7d: EquityCurvePoint[];
  alerts: LiveAlert[];
  open_positions: OpenPositionsSummary;
  fill_quality_7d: FillQuality;
  detectors: LiveDetectorItem[];
  execution_enabled: boolean;
}

export function useLiveDashboard() {
  return useQuery<LiveDashboardResponse>({
    queryKey: ['bot', 'v2', 'lifecycle', 'live_dashboard'],
    queryFn: () =>
      adminFetch<LiveDashboardResponse>('/api/bot/v2/lifecycle/live_dashboard'),
    refetchInterval: 30_000,
  });
}

// ─── Auto Edge Scanner — research discoveries (PR #423) ────────────────────

export type AutoEdgeStatus =
  | 'novel'
  | 'subset'
  | 'partial_overlap'
  | 'promoted'
  | 'dismissed'
  | 'pending';
export type AutoEdgeVerdict = 'PASS' | 'MARGINAL' | 'FAIL';

export interface AutoEdgeDiscovery {
  id: number;
  scan_run_id: string;
  scan_date: string;
  feature: string;
  threshold_op: string;
  threshold_value: number;
  zone: string;
  zone_lo: number;
  zone_hi: number;
  anchor_hours: number;
  category: string | null;
  side: 'YES' | 'NO';
  n_signals: number;
  n_unique_markets: number;
  win_rate: number;
  avg_cost: number;
  net_roi: number;
  mirror_net_roi: number;
  mirror_diff_pp: number;
  year_2025_net_roi: number | null;
  year_2026_net_roi: number | null;
  year_consistency_score: number | null;
  daily_signals: number;
  daily_raw_pnl_at_5k: number;
  daily_honest_pnl_at_5k: number;
  p_value: number | null;
  bh_adjusted_p: number | null;
  rank_score: number;
  overlap_pct_max: number;
  status: AutoEdgeStatus;
  verdict: AutoEdgeVerdict;
  cell_signature: string;
  first_seen_scan_date: string;
  n_times_seen: number;
  notified_at: string | null;
  promoted_to: string | null;
  notes: string | null;
  created_at: string;
}

export interface AutoEdgeScanSummary {
  scan_run_id: string;
  scan_date: string;
  n_findings: number;
  n_novel: number;
  n_subset: number;
  n_partial: number;
  n_promoted: number;
  n_notified: number;
  max_rank_score: number | null;
  first_seen: string;
}

export function useAutoEdgeDiscoveries(status?: string, limit = 50) {
  const params = new URLSearchParams();
  if (status) params.set('status', status);
  params.set('limit', String(limit));
  return useQuery<{ discoveries: AutoEdgeDiscovery[] }>({
    queryKey: ['bot', 'v2', 'auto-edge', 'discoveries', status ?? '', limit],
    queryFn: () => adminFetch(`/api/bot/v2/auto-edge/discoveries?${params.toString()}`),
    refetchInterval: 60_000,
  });
}

export function useAutoEdgeScans(limit = 20) {
  return useQuery<{ scans: AutoEdgeScanSummary[] }>({
    queryKey: ['bot', 'v2', 'auto-edge', 'scans', limit],
    queryFn: () => adminFetch(`/api/bot/v2/auto-edge/scans?limit=${limit}`),
    refetchInterval: 60_000,
  });
}

export function useAutoEdgeDismiss() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (id: number) =>
      adminFetch<{ ok: boolean }>(`/api/bot/v2/auto-edge/discoveries/${id}/dismiss`, {
        method: 'POST',
      }),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['bot', 'v2', 'auto-edge'] }),
  });
}

export function useAutoEdgePromote() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ id, detectorName }: { id: number; detectorName: string }) =>
      adminFetch<{ ok: boolean }>(
        `/api/bot/v2/auto-edge/discoveries/${id}/promote?detector_name=${encodeURIComponent(detectorName)}`,
        { method: 'POST' },
      ),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['bot', 'v2', 'auto-edge'] }),
  });
}

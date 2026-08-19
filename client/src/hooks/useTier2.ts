import { useQuery } from '@tanstack/react-query';
import { adminFetch } from '../lib/telegram';

// --- Types ---

export interface UmaDispute {
  market_id: string;
  condition_id: string;
  question: string;
  category: string | null;
  current_status: string | null;
  dispute_count: number;
  proposed_count: number;
  status_changes: number;
  uma_bond: number | null;
  uma_reward: number | null;
  current_price: number | null;
  resolved: boolean;
  winner: string | null;
  end_date: string | null;
  updated_at: string;
}

export interface ManifoldDivergence {
  canonical_event_id: string;
  polymarket_market_id: string;
  polymarket_condition_id: string;
  polymarket_question: string;
  polymarket_yes_prob: number | null;
  polymarket_volume_24h: number | null;
  manifold_market_id: string;
  manifold_question: string;
  manifold_yes_prob: number | null;
  manifold_volume_24h: number | null;
  divergence_pp: number;
  abs_divergence_pp: number;
  last_manifold_bet: string | null;
  manifold_bets_7d: number | null;
  polymarket_updated_at: string;
  manifold_updated_at: string;
}

export interface WhaleShock {
  event_id: string;
  trigger_type: string;
  market_id: string;
  condition_id: string;
  question: string | null;
  category: string | null;
  event_start: string;
  event_end: string;
  window_span: string | null;
  n_snapshots: number;
  depth_at_start: number | null;
  depth_at_50s: number | null;
  depth_recovery_ratio: number | null;
  midpoint_at_start: number | null;
  midpoint_at_50s: number | null;
  price_impact_50s: number | null;
  spread_at_start: number | null;
  spread_at_50s: number | null;
  trigger_meta: Record<string, unknown> | null;
}

export interface CohortStats {
  category: string;
  is_cyom: boolean;
  is_restricted: boolean;
  is_neg_risk_aug: boolean;
  n_markets: number;
  n_resolved: number;
  n_yes_won: number;
  n_no_won: number;
  yes_win_rate: number | null;
  avg_current_price: number | null;
  median_current_price: number | null;
  avg_volume_24h: number | null;
  total_volume_24h: number | null;
}

export interface Tier2Summary {
  uma: {
    active_disputes: number;
    multi_disputed: number;
    total_with_uma: number;
  };
  manifold: {
    pairs_total: number;
    high_divergence_count: number;
    active_manifold_pairs: number;
  };
  whale_shocks: {
    events_total: number;
    events_24h: number;
    avg_recovery_ratio: number | null;
    p95_price_impact: number | null;
  };
}

export type ActionSource = 'uma' | 'manifold' | 'whale' | 'term';
export type ActionSeverity = 'high' | 'med' | 'low';

export interface ActionItem {
  source: ActionSource;
  severity: ActionSeverity;
  headline: string;
  market_question: string | null;
  condition_id: string | null;
  category: string | null;
  payload: Record<string, unknown>;
  event_at: string | null;
}

export interface ActionFeedResponse {
  items: ActionItem[];
  count: number;
  freshness: {
    uma_last: string | null;
    manifold_last: string | null;
    whale_last: string | null;
    term_last: string | null;
  };
}

export interface TermStructureItem {
  market_id: string;
  condition_id: string;
  category: string | null;
  question: string;
  yes_prob_current: number | null;
  one_hour_price_change: number | null;
  one_day_price_change: number | null;
  one_week_price_change: number | null;
  one_month_price_change: number | null;
  one_year_price_change: number | null;
  volume_24h: number | null;
  momentum_regime_1h: string | null;
  resolved: boolean;
  winner: string | null;
  outcome_signed: number;
  end_date: string | null;
  updated_at: string;
}

// --- Hooks ---

const STALE_TIME_MS = 60_000;
const REFETCH_INTERVAL_MS = 120_000;

export function useTier2Summary() {
  return useQuery<Tier2Summary>({
    queryKey: ['tier2', 'summary'],
    queryFn: () => adminFetch<Tier2Summary>('/api/tier2/summary'),
    staleTime: STALE_TIME_MS,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

export function useUmaDisputes(params?: { limit?: number; min_disputes?: number }) {
  const q = new URLSearchParams();
  if (params?.limit) q.set('limit', String(params.limit));
  if (params?.min_disputes !== undefined) q.set('min_disputes', String(params.min_disputes));
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return useQuery<{ items: UmaDispute[]; count: number }>({
    queryKey: ['tier2', 'uma-disputes', params],
    queryFn: () => adminFetch(`/api/tier2/uma-disputes${suffix}`),
    staleTime: STALE_TIME_MS,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

export function useManifoldDivergence(params?: { limit?: number; min_divergence_pp?: number }) {
  const q = new URLSearchParams();
  if (params?.limit) q.set('limit', String(params.limit));
  if (params?.min_divergence_pp !== undefined) q.set('min_divergence_pp', String(params.min_divergence_pp));
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return useQuery<{ items: ManifoldDivergence[]; count: number }>({
    queryKey: ['tier2', 'manifold-divergence', params],
    queryFn: () => adminFetch(`/api/tier2/manifold-divergence${suffix}`),
    staleTime: STALE_TIME_MS,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

export function useWhaleShocks(params?: { limit?: number; trigger_type?: string }) {
  const q = new URLSearchParams();
  if (params?.limit) q.set('limit', String(params.limit));
  if (params?.trigger_type) q.set('trigger_type', params.trigger_type);
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return useQuery<{ items: WhaleShock[]; count: number }>({
    queryKey: ['tier2', 'whale-shocks', params],
    queryFn: () => adminFetch(`/api/tier2/whale-shocks${suffix}`),
    staleTime: STALE_TIME_MS,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

export function useCohortStats() {
  return useQuery<{ items: CohortStats[]; count: number }>({
    queryKey: ['tier2', 'cohort-stats'],
    queryFn: () => adminFetch('/api/tier2/cohort-stats'),
    staleTime: 5 * 60_000,
    refetchInterval: 5 * 60_000,
  });
}

export function useActionFeed(params?: { limit?: number }) {
  const q = new URLSearchParams();
  if (params?.limit) q.set('limit', String(params.limit));
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return useQuery<ActionFeedResponse>({
    queryKey: ['tier2', 'action-feed', params],
    queryFn: () => adminFetch<ActionFeedResponse>(`/api/tier2/action-feed${suffix}`),
    staleTime: STALE_TIME_MS,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

export function useTermStructure(params?: { limit?: number; regime?: string }) {
  const q = new URLSearchParams();
  if (params?.limit) q.set('limit', String(params.limit));
  if (params?.regime) q.set('regime', params.regime);
  const suffix = q.toString() ? `?${q.toString()}` : '';
  return useQuery<{ items: TermStructureItem[]; count: number }>({
    queryKey: ['tier2', 'term-structure', params],
    queryFn: () => adminFetch(`/api/tier2/term-structure${suffix}`),
    staleTime: STALE_TIME_MS,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

// --- ML Accuracy (Diagnostics → ML Accuracy) ---

export interface MlAccuracyLayerRow {
  detector: string;
  scope: string; // anchor '24h'/'72h' или 'anchor:horizon'
  cohort_n: number | null;
  n_events: number | null;
  metrics: Record<string, number | null>;
}

export interface MlAccuracyMoneyRow {
  detector: string;
  category: string;
  avg_roi_net_pct: number | null;
  n_signals: number | null;
  n_events: number | null;
  reliability: string | null; // RELIABLE | WEAK | ARTIFACT
  event_concentration?: number;
  loo_avg_roi_pct?: number | null;
  side_no_share?: number;
  bootstrap_roi_lo?: number;
  bootstrap_roi_hi?: number;
  median_roi_net_pct?: number;
  win_rate?: number;
  artifact_reasons?: string[];
}

export interface MlModelStatus {
  label: string;
  active: boolean;
  version?: string;
  fit_at?: string | null;
  skip_reason?: string | null;
  state?: string;
}

export interface MlAccuracyData {
  layers: {
    honesty: MlAccuracyLayerRow[];
    selection: MlAccuracyLayerRow[];
    timing: MlAccuracyLayerRow[];
  };
  money: MlAccuracyMoneyRow[];
  freshness: { snapshot_at: string | null } | null;
  model_status?: MlModelStatus[];
}

export function useMlAccuracy() {
  return useQuery<MlAccuracyData>({
    queryKey: ['tier2', 'ml-accuracy'],
    queryFn: () => adminFetch<MlAccuracyData>('/api/tier2/ml-accuracy'),
    staleTime: STALE_TIME_MS,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

export interface HonestAuditRow {
  detector: string;
  category: string;
  n_dedup: number;
  per_day: number;
  per_day_1k: number;
  trend: string; // '📉' | '➡️' | '📈' | ''
  verdict: string; // '❌ KILL' | '⚠️ MARGINAL' | '✅ PROMOTE candidate' | '⏸️ INSUFFICIENT ...'
}

export interface HonestAuditData {
  days: number;
  rows: HonestAuditRow[];
}

export function useHonestAudit() {
  return useQuery<HonestAuditData>({
    queryKey: ['bot', 'honest-audit'],
    queryFn: () => adminFetch<HonestAuditData>('/api/bot/v2/honest-audit'),
    staleTime: STALE_TIME_MS,
    refetchInterval: REFETCH_INTERVAL_MS,
  });
}

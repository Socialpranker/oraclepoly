/**
 * Live Trading Floor — color/format helpers.
 * См. plans/wiggly-nibbling-stardust.md (PR 1).
 */

import type { LiveAlert } from '../../../../hooks/useBot';

export const STAGE_RED = '#e04444';

/** Bankroll health: green <2% drawdown, gold 2-5%, red >5%. */
export function bankrollColor(drawdownPct: number | null): string {
  if (drawdownPct === null) return 'var(--ed-ink-faint)';
  if (drawdownPct >= 5) return 'var(--ed-down)';
  if (drawdownPct >= 2) return 'var(--ed-accent)';
  return 'var(--ed-up)';
}

/** Alert level → color. */
export function alertColor(level: LiveAlert['level']): string {
  if (level === 'critical') return 'var(--ed-down)';
  if (level === 'warning') return 'var(--ed-accent)';
  return 'var(--ed-ink-dim)';
}

/** Alert level → icon. */
export function alertIcon(level: LiveAlert['level']): string {
  if (level === 'critical') return '!';
  if (level === 'warning') return '▲';
  return '·';
}

/** Fill rate gradient: red <30%, gold 30-50%, green ≥50%. */
export function fillRateColor(rate: number | null): string {
  if (rate === null) return 'var(--ed-ink-faint)';
  if (rate >= 50) return 'var(--ed-up)';
  if (rate >= 30) return 'var(--ed-accent)';
  return 'var(--ed-down)';
}

/** PnL color: green positive, red negative, faint zero. */
export function pnlColor(pnl: number): string {
  if (pnl > 0) return 'var(--ed-up)';
  if (pnl < 0) return 'var(--ed-down)';
  return 'var(--ed-ink-faint)';
}

/** Format $ with sign, fixed precision. */
export function fmtUsd(v: number, opts?: { sign?: boolean; decimals?: number }): string {
  const decimals = opts?.decimals ?? 2;
  const sign = opts?.sign && v >= 0 ? '+' : '';
  if (v < 0) return `−$${Math.abs(v).toFixed(decimals)}`;
  return `${sign}$${v.toFixed(decimals)}`;
}

/** Format date YYYY-MM-DD → "Apr 26". */
export function fmtShortDate(iso: string): string {
  const d = new Date(iso + 'T00:00:00Z');
  return d.toLocaleDateString('en-US', { month: 'short', day: 'numeric', timeZone: 'UTC' });
}

/** Slippage delta visual: positive = beating projection, negative = slippage eating edge. */
export function slippageDeltaColor(delta: number | null): string {
  if (delta === null) return 'var(--ed-ink-faint)';
  if (delta >= 0) return 'var(--ed-up)';
  if (delta >= -5) return 'var(--ed-accent)';
  return 'var(--ed-down)';
}

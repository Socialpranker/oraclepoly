/**
 * Backtest Mission Control — shared color/format helpers.
 * См. plans/wiggly-nibbling-stardust.md (aesthetic direction).
 */

export type Verdict = 'PASS' | 'FAIL' | 'MARGINAL' | null | undefined;

export const VERDICT_COLORS = {
  PASS: 'var(--ed-up)',           // #5fb97a green
  FAIL: 'var(--ed-down)',         // #d65a5a red
  MARGINAL: 'var(--ed-accent)',   // #c9a961 gold
  none: 'var(--ed-ink-faint)',    // silent grey for missing MD
} as const;

export function verdictColor(v: Verdict): string {
  if (!v) return VERDICT_COLORS.none;
  return VERDICT_COLORS[v] ?? VERDICT_COLORS.none;
}

export function verdictLabel(v: Verdict): string {
  if (!v) return '— no MD';
  return v;
}

/**
 * Haircut factor → gradient color (gold @ 1.0 → red @ 0.0).
 *
 * Чем больше haircut (ниже factor) — тем "горячее" предупреждение.
 * factor=0.5 — neutral, factor=1.0 — green, factor=0 — red.
 */
export function haircutColor(factor: number | null | undefined): string {
  if (factor == null) return 'var(--ed-ink-faint)';
  if (factor >= 0.7) return 'var(--ed-up)';
  if (factor >= 0.5) return 'var(--ed-accent)';
  return 'var(--ed-down)';
}

/** "Haircut ×0.4 (60% optimism cut)" */
export function haircutLabel(factor: number | null | undefined): string {
  if (factor == null) return '—';
  const cutPct = Math.round((1 - factor) * 100);
  return `×${factor.toFixed(2)} (${cutPct}% cut)`;
}

/** Format $123.45 with sign */
export function fmtUsd(v: number | null | undefined, opts: { sign?: boolean; decimals?: number } = {}): string {
  if (v == null) return '—';
  const decimals = opts.decimals ?? 2;
  const sign = opts.sign && v >= 0 ? '+' : '';
  return `${sign}$${v.toFixed(decimals)}`;
}

export function fmtPct(v: number | null | undefined, opts: { sign?: boolean; decimals?: number } = {}): string {
  if (v == null) return '—';
  const decimals = opts.decimals ?? 1;
  const sign = opts.sign && v >= 0 ? '+' : '';
  return `${sign}${v.toFixed(decimals)}%`;
}

/** Compact integer 7,904 → "7.9K", 821 → "821" */
export function fmtCompact(v: number | null | undefined): string {
  if (v == null) return '—';
  if (v >= 1000) return `${(v / 1000).toFixed(1)}K`;
  return String(v);
}

/** Quadrant label for BacktestMap based on (haircut_factor, daily_at_5k) */
export function mapQuadrant(haircut: number, daily: number): {
  label: string;
  color: string;
  icon: string;
} {
  if (haircut >= 0.5 && daily >= 10) {
    return { label: 'Promote candidate', color: 'var(--ed-up)', icon: '✓' };
  }
  if (haircut < 0.5 && daily >= 10) {
    return { label: 'Re-audit haircut', color: 'var(--ed-accent)', icon: '⚠' };
  }
  if (haircut >= 0.5 && daily < 10) {
    return { label: 'Marginal — verify', color: 'var(--ed-ink-dim)', icon: '·' };
  }
  return { label: 'Killed / weak', color: 'var(--ed-down)', icon: '✕' };
}

/** Detector readable label (D7b_no_favorite_fade → D7b NoFavoriteFade) */
export function detectorShort(name: string): string {
  if (!name.includes('_')) return name;
  const [first, ...rest] = name.split('_');
  return `${first} ${rest.map(s => s[0].toUpperCase() + s.slice(1)).join('')}`;
}

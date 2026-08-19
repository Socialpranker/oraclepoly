/**
 * Shadow Promotion Center — color/format helpers.
 * См. plans/wiggly-nibbling-stardust.md (Hybrid C plan).
 */

import type { PromotionReadinessItem } from '../../../../hooks/useBot';

/** Цвет ready badge на карточке — по числу passed gates. */
export function readyColor(passed: number, total: number): string {
  if (passed === total) return 'var(--ed-up)';
  if (passed >= total - 1) return 'var(--ed-accent)';
  if (passed > 0) return 'var(--ed-ink-dim)';
  return 'var(--ed-ink-faint)';
}

/** Label для ready badge: "✓ READY", "▲ 2/3 close", "○ 1/3 partial", "● collecting". */
export function readyLabel(item: PromotionReadinessItem): string {
  const { gates_passed, gates_total, shadow_7d } = item;
  if (gates_passed === gates_total) return '✓ READY';
  if (gates_passed === gates_total - 1) return `▲ ${gates_passed}/${gates_total} close`;
  if (gates_passed > 0) return `○ ${gates_passed}/${gates_total} partial`;
  if (shadow_7d.distinct_events === 0) return '· no signals';
  return '● collecting';
}

// gateColor moved to ../cards/colors — re-export для backwards compat.
export { gateColor } from '../cards/colors';

/** Δ WR pp → color: |Δ| ≤ 5 = gold/up, иначе red. */
export function wrDeltaColor(delta: number | null): string {
  if (delta === null) return 'var(--ed-ink-faint)';
  if (Math.abs(delta) <= 5) return 'var(--ed-up)';
  if (Math.abs(delta) <= 10) return 'var(--ed-accent)';
  return 'var(--ed-down)';
}

/** Сортировки. */
export type ShadowSortKey = 'gates' | 'honest' | 'wr' | 'events';
export type ShadowFilterKey = 'all' | 'ready' | 'collecting';

export function compareItems(a: PromotionReadinessItem, b: PromotionReadinessItem, sort: ShadowSortKey): number {
  if (sort === 'gates') {
    const diff = b.gates_passed - a.gates_passed;
    if (diff !== 0) return diff;
    return (b.canonical.honest_daily_usd_at_5k ?? 0) - (a.canonical.honest_daily_usd_at_5k ?? 0);
  }
  if (sort === 'honest') {
    return (b.canonical.honest_daily_usd_at_5k ?? -Infinity) - (a.canonical.honest_daily_usd_at_5k ?? -Infinity);
  }
  if (sort === 'wr') {
    return (b.shadow_7d.recent_wr_pct ?? -Infinity) - (a.shadow_7d.recent_wr_pct ?? -Infinity);
  }
  // events
  return b.shadow_7d.distinct_events - a.shadow_7d.distinct_events;
}

export function applyFilter(items: PromotionReadinessItem[], filter: ShadowFilterKey): PromotionReadinessItem[] {
  if (filter === 'ready') return items.filter((i) => i.ready);
  if (filter === 'collecting') return items.filter((i) => !i.ready && i.shadow_7d.distinct_events < 30);
  return items;
}

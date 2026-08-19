/**
 * Virtual Live Readiness Center — color/format helpers.
 * См. plans/wiggly-nibbling-stardust.md (Variant A).
 */

import type { LiveReadinessItem } from '../../../../hooks/useBot';

/** Цвет ready badge — по числу passed gates из total (signal-level gates_v2). */
export function liveReadyColor(passed: number, total: number): string {
  if (passed === total) return 'var(--ed-up)';
  if (passed >= total - 1) return 'var(--ed-accent)';
  if (passed >= total - 3) return 'var(--ed-ink-dim)';
  return 'var(--ed-ink-faint)';
}

/** Label по signal-level gates_v2 (ровно по чему судит TG promote). READY =
 * decision == "READY" (все 6). Иначе показываем decision + прогресс x/6. */
export function liveReadyLabel(item: LiveReadinessItem): string {
  const { gates_v2_passed, gates_v2_total, gates_decision_v2, virtual_7d, days_virtual } = item;
  if (gates_decision_v2 === 'READY') return '✓ READY';
  if (gates_decision_v2 === 'MANUAL_OVERRIDE_NEEDED')
    return `👁 ${gates_v2_passed}/${gates_v2_total} adv?`;
  if (gates_decision_v2 === 'INSUFFICIENT_DATA') {
    if (virtual_7d.n_placed === 0) return '· no orders 7d';
    return `● accumulating${days_virtual < 14 ? ` (${days_virtual}d)` : ''}`;
  }
  // BLOCKED
  if (gates_v2_passed === gates_v2_total - 1) return `▲ ${gates_v2_passed}/${gates_v2_total} close`;
  return `⛔ ${gates_v2_passed}/${gates_v2_total} blocked`;
}

/** Fill rate gradient: red <30%, gold 30-50%, green ≥50%. */
export function fillRateColor(rate: number | null): string {
  if (rate === null) return 'var(--ed-ink-faint)';
  if (rate >= 50) return 'var(--ed-up)';
  if (rate >= 30) return 'var(--ed-accent)';
  return 'var(--ed-down)';
}

// gateColor moved to ../cards/colors — re-export для backwards compat.
export { gateColor } from '../cards/colors';

/** Days virtual progress (0% at 0d, 50% at 14d, 100% at 28d). */
export function daysVirtualProgress(days: number): number {
  if (days >= 28) return 100;
  if (days >= 14) return 50 + ((days - 14) / 14) * 50;
  return (days / 14) * 50;
}

/** Sort/filter helpers. */
export type VirtualSortKey = 'gates' | 'pnl' | 'fill_rate' | 'days';
export type VirtualFilterKey = 'all' | 'ready' | 'accumulating';

export function compareItems(
  a: LiveReadinessItem,
  b: LiveReadinessItem,
  sort: VirtualSortKey,
): number {
  if (sort === 'gates') {
    const diff = b.gates_v2_passed - a.gates_v2_passed;
    if (diff !== 0) return diff;
    return b.virtual_7d.pnl_usd - a.virtual_7d.pnl_usd;
  }
  if (sort === 'pnl') return b.virtual_7d.pnl_usd - a.virtual_7d.pnl_usd;
  if (sort === 'fill_rate') {
    return (b.virtual_7d.fill_rate_pct ?? -Infinity) - (a.virtual_7d.fill_rate_pct ?? -Infinity);
  }
  return b.days_virtual - a.days_virtual;
}

export function applyFilter(items: LiveReadinessItem[], filter: VirtualFilterKey): LiveReadinessItem[] {
  if (filter === 'ready') return items.filter((i) => i.ready_v2);
  if (filter === 'accumulating') return items.filter((i) => !i.ready_v2 && i.days_virtual < 14);
  return items;
}

import type { PromotionReadinessItem } from '../../../../hooks/useBot';
import { readyColor, readyLabel, wrDeltaColor } from './utils';
import { fmtUsd, fmtPct } from '../backtest/utils';
import {
  CardShell,
  CardHeader,
  CardDetectorTitle,
  GateRow,
  CardFooter,
  MiniStat,
} from '../cards/primitives';

const FAINT = 'var(--ed-ink-faint)';
const RULE = 'var(--ed-rule)';

interface Props {
  item: PromotionReadinessItem;
  expanded: boolean;
  onToggle: () => void;
}

/**
 * PromotionCard — карточка детектора с visible 3 gates (Shadow → Virtual).
 *
 * Hero: "Recent WR vs canonical" с Δ
 * Per-gate detail: 3 rows
 * Stats footer: signals 7d · honest projection · haircut
 */
export function PromotionCard({ item, expanded, onToggle }: Props) {
  const color = readyColor(item.gates_passed, item.gates_total);
  const label = readyLabel(item);
  const hasBacktest = item.canonical.verdict != null;
  const recentWr = item.shadow_7d.recent_wr_pct;
  const canonicalWr = item.canonical.canonical_wr_pct;
  const delta = item.wr_delta_pp;

  return (
    <CardShell color={color} expanded={expanded} onToggle={onToggle}>
      <CardHeader
        label={label}
        color={color}
        gates={[
          item.gates.n_events.passed,
          item.gates.wr_match.passed,
          item.gates.verdict.passed,
        ]}
      />

      <CardDetectorTitle detector={item.detector} />

      {/* Hero: Recent WR vs canonical */}
      <div style={{ marginBottom: 18 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 4 }}>
          <div
            className="ed-display"
            style={{
              fontSize: 'clamp(24px, 6.5vw, 32px)',
              fontWeight: 300,
              fontStyle: 'italic',
              letterSpacing: '-0.02em',
              color: recentWr != null ? 'var(--ed-ink)' : FAINT,
              lineHeight: 1,
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            {recentWr != null ? `${recentWr.toFixed(1)}%` : '—'}
          </div>
          {delta != null && (
            <div
              className="ed-mono"
              style={{ fontSize: 13, color: wrDeltaColor(delta), fontWeight: 600, letterSpacing: '0.02em' }}
            >
              {delta >= 0 ? '+' : ''}
              {delta.toFixed(1)}pp
            </div>
          )}
        </div>
        <div className="ed-mono" style={{ fontSize: 10, color: FAINT, letterSpacing: '0.06em' }}>
          recent WR · canonical {canonicalWr != null ? `${canonicalWr.toFixed(1)}%` : '—'}
        </div>
      </div>

      {/* Per-gate detail */}
      <div style={{ marginBottom: 16, display: 'flex', flexDirection: 'column', gap: 8 }}>
        <GateRow
          num={1}
          passed={item.gates.n_events.passed}
          label={item.gates.n_events.label}
          value={`${item.gates.n_events.value}/${item.gates.n_events.threshold} events 7d`}
        />
        <GateRow
          num={2}
          passed={item.gates.wr_match.passed}
          label={item.gates.wr_match.label}
          value={
            item.gates.wr_match.value != null && item.gates.wr_match.canonical != null
              ? `Δ ${(item.gates.wr_match.delta ?? 0) >= 0 ? '+' : ''}${(item.gates.wr_match.delta ?? 0).toFixed(1)}pp (±5)`
              : 'no data'
          }
          disabled={!hasBacktest}
        />
        <GateRow
          num={3}
          passed={item.gates.verdict.passed}
          label={item.gates.verdict.label}
          value={item.gates.verdict.value ? String(item.gates.verdict.value) : 'no doc'}
        />
      </div>

      {/* Stats */}
      <div
        style={{
          paddingTop: 12,
          borderTop: `1px solid ${RULE}`,
          display: 'grid',
          gridTemplateColumns: 'repeat(3, 1fr)',
          gap: 10,
          fontSize: 11,
          fontFamily: 'var(--ed-mono)',
        }}
      >
        <MiniStat
          label="signals 7d"
          value={`${item.shadow_7d.total_signals} · ${item.shadow_7d.resolved} res`}
        />
        <MiniStat
          label="potential"
          value={fmtUsd(item.canonical.honest_daily_usd_at_5k, { sign: true })}
        />
        <MiniStat
          label="haircut"
          value={
            item.canonical.honest_haircut_factor != null
              ? `×${item.canonical.honest_haircut_factor.toFixed(2)}`
              : '—'
          }
        />
      </div>

      <CardFooter
        expanded={expanded}
        label={expanded ? 'collapse · per-bucket WR' : 'per-bucket WR vs canonical'}
      />
    </CardShell>
  );
}

// Suppress unused: fmtPct re-imported if needed by future GateRow renders
void fmtPct;

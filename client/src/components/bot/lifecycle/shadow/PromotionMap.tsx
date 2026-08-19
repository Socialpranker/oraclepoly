import { useMemo, useState } from 'react';
import type { PromotionReadinessItem } from '../../../../hooks/useBot';
import { readyColor } from './utils';
import { detectorShort, fmtUsd, fmtPct } from '../backtest/utils';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';
const STAGE = '#5b9dff';

interface Props {
  items: PromotionReadinessItem[];
  isLoading?: boolean;
  onDotClick?: (detector: string) => void;
}

const W = 1100;
const H = 360;
const PAD = { top: 56, right: 30, bottom: 56, left: 90 };
const PLOT_W = W - PAD.left - PAD.right;
const PLOT_H = H - PAD.top - PAD.bottom;

const LANES = [
  { x: 0, label: '0/3', sub: 'no signals', color: 'var(--ed-ink-faint)' },
  { x: 1, label: '1/3', sub: 'partial', color: 'var(--ed-ink-dim)' },
  { x: 2, label: '2/3', sub: 'close', color: 'var(--ed-accent)' },
  { x: 3, label: '3/3', sub: 'ready', color: 'var(--ed-up)' },
];

/**
 * PromotionMap — scatter всех детекторов по lanes (0/3 → 3/3 gates passed).
 *
 *   Y axis: honest_daily_usd_at_5k (потенциал из backtest, 0 если no MD)
 *   X axis: gates_passed (дискретно 0-3, jitter в lane для разделения dots)
 *   Dot size: sqrt(distinct_events_7d) × 4
 *   Dot color: ready/partial/no-signals
 *
 * Detector в lane "3/3" в верхнем углу = promote candidate сейчас.
 */
export function PromotionMap({ items, isLoading = false, onDotClick }: Props) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const yMaxRound = useMemo(() => {
    const ys = items
      .map((i) => i.canonical.honest_daily_usd_at_5k)
      .filter((v): v is number => v != null);
    if (ys.length === 0) return 50;
    return Math.ceil(Math.max(...ys) / 10) * 10 || 10;
  }, [items]);

  const points = useMemo(() => {
    if (items.length === 0) return [];
    // Group by lane → jitter horizontally to avoid overlap
    const byLane = new Map<number, PromotionReadinessItem[]>();
    for (const item of items) {
      const lane = item.gates_passed;
      if (!byLane.has(lane)) byLane.set(lane, []);
      byLane.get(lane)!.push(item);
    }

    const result: Array<{
      item: PromotionReadinessItem;
      x: number;
      y: number;
      size: number;
    }> = [];

    for (const [lane, lst] of byLane) {
      const baseX = PAD.left + (lane / 3) * PLOT_W;
      const laneWidth = PLOT_W / 3;
      lst.forEach((item, i) => {
        const yRaw = item.canonical.honest_daily_usd_at_5k ?? 0;
        const y = PAD.top + (1 - yRaw / yMaxRound) * PLOT_H;
        // Spread within lane horizontally if multiple
        const offset = lst.length > 1 ? ((i - (lst.length - 1) / 2) / Math.max(1, lst.length - 1)) * (laneWidth * 0.3) : 0;
        const x = baseX + offset;
        const sizeRaw = Math.max(item.shadow_7d.distinct_events, 1);
        const size = Math.max(6, Math.min(28, Math.sqrt(sizeRaw) * 4));
        result.push({ item, x, y, size });
      });
    }
    return result;
  }, [items, yMaxRound]);

  return (
    <section
      style={{
        position: 'relative',
        padding: '32px 24px 16px',
        borderTop: `1px solid ${RULE}`,
        borderBottom: `1px solid ${RULE}`,
        background: `linear-gradient(180deg, ${STAGE}05 0%, transparent 100%)`,
      }}
    >
      <div style={{ maxWidth: 1180, margin: '0 auto' }}>
        <header
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'baseline',
            marginBottom: 12,
            flexWrap: 'wrap',
            gap: 8,
          }}
        >
          <div>
            <div
              className="ed-kicker"
              style={{ color: STAGE, letterSpacing: '0.18em', fontSize: 10, marginBottom: 4 }}
            >
              👁 Promotion Map
            </div>
            <h2
              className="ed-display"
              style={{
                fontSize: 22,
                fontWeight: 400,
                margin: 0,
                letterSpacing: '-0.01em',
                color: 'var(--ed-ink)',
              }}
            >
              <em style={{ fontStyle: 'italic' }}>Who's ready</em> for Virtual?
            </h2>
          </div>
          <div className="ed-mono" style={{ color: FAINT, fontSize: 11, letterSpacing: '0.04em' }}>
            {items.length} detectors · 7d shadow window
          </div>
        </header>

        {isLoading ? (
          <div style={{ color: FAINT, fontSize: 12, padding: 40, textAlign: 'center' }}>
            loading promotion map…
          </div>
        ) : items.length === 0 ? (
          <div style={{ color: FAINT, fontSize: 12, padding: 40, textAlign: 'center' }}>
            Нет shadow детекторов в БД.
          </div>
        ) : (
          <>
            <svg
              viewBox={`0 0 ${W} ${H}`}
              style={{ width: '100%', height: 'auto', display: 'block' }}
              role="img"
              aria-label="Promotion readiness map: gates passed vs honest daily PnL"
            >
              {/* Lane vertical guides */}
              {LANES.map((lane) => {
                const x = PAD.left + (lane.x / 3) * PLOT_W;
                return (
                  <g key={lane.x}>
                    <line
                      x1={x}
                      y1={PAD.top}
                      x2={x}
                      y2={H - PAD.bottom}
                      stroke={RULE}
                      strokeDasharray="2 4"
                      opacity={lane.x === 3 ? 0.5 : 0.25}
                    />
                    <text
                      x={x}
                      y={PAD.top - 22}
                      textAnchor="middle"
                      fontSize={11}
                      fontFamily="var(--ed-mono)"
                      fill={lane.color}
                      style={{ letterSpacing: '0.08em', fontWeight: 600 }}
                    >
                      {lane.label}
                    </text>
                    <text
                      x={x}
                      y={PAD.top - 8}
                      textAnchor="middle"
                      fontSize={9}
                      fontFamily="var(--ed-mono)"
                      fill={FAINT}
                      style={{ letterSpacing: '0.14em', textTransform: 'uppercase' }}
                    >
                      {lane.sub}
                    </text>
                  </g>
                );
              })}

              {/* Y axis ticks */}
              {[0, 0.25, 0.5, 0.75, 1].map((frac) => {
                const y = PAD.top + (1 - frac) * PLOT_H;
                const value = yMaxRound * frac;
                return (
                  <g key={`y-${frac}`}>
                    <line x1={PAD.left - 4} y1={y} x2={PAD.left} y2={y} stroke={FAINT} />
                    <text
                      x={PAD.left - 8}
                      y={y + 4}
                      textAnchor="end"
                      fontSize={10}
                      fontFamily="var(--ed-mono)"
                      fill={FAINT}
                    >
                      ${value.toFixed(0)}
                    </text>
                  </g>
                );
              })}

              {/* Y label */}
              <text
                x={20}
                y={PAD.top + PLOT_H / 2}
                textAnchor="middle"
                fontSize={10}
                fontFamily="var(--ed-mono)"
                fill={DIM}
                transform={`rotate(-90 20 ${PAD.top + PLOT_H / 2})`}
                style={{ letterSpacing: '0.12em', textTransform: 'uppercase' }}
              >
                Honest $/day @ $5K
              </text>
              <text
                x={PAD.left + PLOT_W / 2}
                y={H - 12}
                textAnchor="middle"
                fontSize={10}
                fontFamily="var(--ed-mono)"
                fill={DIM}
                style={{ letterSpacing: '0.12em', textTransform: 'uppercase' }}
              >
                ← Gates passed (Shadow → Virtual readiness) →
              </text>

              {/* Dots */}
              {points.map((p, i) => {
                const isHover = hoverIdx === i;
                const color = readyColor(p.item.gates_passed, p.item.gates_total);
                return (
                  <g
                    key={p.item.detector}
                    onMouseEnter={() => setHoverIdx(i)}
                    onMouseLeave={() => setHoverIdx(null)}
                    onClick={() => onDotClick?.(p.item.detector)}
                    style={{ cursor: 'pointer' }}
                  >
                    {isHover && (
                      <circle cx={p.x} cy={p.y} r={p.size + 8} fill={color} opacity={0.18} />
                    )}
                    <circle
                      cx={p.x}
                      cy={p.y}
                      r={isHover ? p.size * 1.15 : p.size}
                      fill={color}
                      opacity={p.item.shadow_7d.distinct_events > 0 ? 0.85 : 0.35}
                      stroke="var(--ed-bg)"
                      strokeWidth={1.5}
                      style={{ transition: 'r 150ms ease' }}
                    />
                    <text
                      x={p.x}
                      y={p.y - p.size - 6}
                      textAnchor="middle"
                      fontSize={9}
                      fontFamily="var(--ed-mono)"
                      fill={isHover ? 'var(--ed-ink)' : DIM}
                      style={{
                        opacity: isHover || p.item.gates_passed === p.item.gates_total ? 1 : 0.6,
                        transition: 'opacity 150ms',
                      }}
                    >
                      {detectorShort(p.item.detector)}
                    </text>
                  </g>
                );
              })}
            </svg>

            {hoverIdx !== null && points[hoverIdx] && <Tooltip item={points[hoverIdx].item} />}
            <Legend />
          </>
        )}
      </div>
    </section>
  );
}

function Tooltip({ item }: { item: PromotionReadinessItem }) {
  const color = readyColor(item.gates_passed, item.gates_total);
  return (
    <div
      style={{
        marginTop: 12,
        padding: '12px 16px',
        border: `1px solid ${color}40`,
        background: 'rgba(255,255,255,0.02)',
        display: 'grid',
        gridTemplateColumns: 'auto 1fr auto',
        gap: 16,
        alignItems: 'center',
        fontSize: 12,
      }}
    >
      <div>
        <div className="ed-mono" style={{ fontSize: 13, color: 'var(--ed-ink)', fontWeight: 500 }}>
          {item.detector}
        </div>
        <div className="ed-kicker" style={{ fontSize: 9, letterSpacing: '0.18em', color, marginTop: 2 }}>
          {item.gates_passed}/{item.gates_total} gates passed
        </div>
      </div>
      <div style={{ display: 'flex', gap: 18, color: DIM, fontSize: 11, fontFamily: 'var(--ed-mono)' }}>
        <Stat label="events 7d" value={String(item.shadow_7d.distinct_events)} />
        <Stat
          label="recent WR"
          value={item.shadow_7d.recent_wr_pct != null ? `${item.shadow_7d.recent_wr_pct.toFixed(1)}%` : '—'}
        />
        <Stat
          label="canonical WR"
          value={item.canonical.canonical_wr_pct != null ? `${item.canonical.canonical_wr_pct.toFixed(1)}%` : '—'}
        />
        <Stat
          label="Δ WR"
          value={item.wr_delta_pp != null ? `${item.wr_delta_pp >= 0 ? '+' : ''}${item.wr_delta_pp.toFixed(1)}pp` : '—'}
        />
        <Stat label="potential" value={fmtUsd(item.canonical.honest_daily_usd_at_5k, { sign: true })} />
      </div>
      <div style={{ color: FAINT, fontSize: 10 }}>click → details ↓</div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ color: FAINT, fontSize: 9, letterSpacing: '0.12em', textTransform: 'uppercase' }}>
        {label}
      </div>
      <div style={{ color: 'var(--ed-ink)', fontWeight: 500, marginTop: 2 }}>{value}</div>
    </div>
  );
}

function Legend() {
  return (
    <div
      style={{
        marginTop: 14,
        display: 'flex',
        gap: 22,
        flexWrap: 'wrap',
        fontSize: 10,
        color: FAINT,
        fontFamily: 'var(--ed-mono)',
        letterSpacing: '0.04em',
      }}
    >
      <LegendDot color="var(--ed-up)" label="3/3 ready" />
      <LegendDot color="var(--ed-accent)" label="2/3 close" />
      <LegendDot color="var(--ed-ink-dim)" label="1/3 partial" />
      <LegendDot color={FAINT} label="0/3 collecting" />
      <span>· bigger dot = more shadow events 7d</span>
    </div>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <span style={{ width: 8, height: 8, borderRadius: '50%', background: color, display: 'inline-block' }} />
      {label}
    </span>
  );
}

// Format helper alias to match other components
function fmtPctAlias(v: number | null | undefined): string {
  return fmtPct(v);
}
void fmtPctAlias; // silence unused warning if not used elsewhere

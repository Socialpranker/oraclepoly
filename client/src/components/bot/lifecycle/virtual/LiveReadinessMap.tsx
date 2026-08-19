import { useMemo, useState } from 'react';
import type { LiveReadinessItem } from '../../../../hooks/useBot';
import { liveReadyColor } from './utils';
import { detectorShort, fmtUsd } from '../backtest/utils';
import { useIsMobile } from '../../../../hooks/useViewport';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';
const STAGE = '#e0b020';

interface Props {
  items: LiveReadinessItem[];
  isLoading?: boolean;
  onDotClick?: (detector: string) => void;
}

const W = 1100;
const H = 360;
const PAD = { top: 36, right: 30, bottom: 56, left: 90 };
const PLOT_W = W - PAD.left - PAD.right;
const PLOT_H = H - PAD.top - PAD.bottom;

/**
 * LiveReadinessMap — 2D scatter virtual детекторов.
 *
 *   X axis: fill_rate_pct (0% → 100%)
 *   Y axis: virtual PnL 7d (continuous, may be negative)
 *   Dot size: sqrt(n_placed_7d) × 4
 *   Dot color: liveReadyColor (gates passed)
 *
 * Quadrants:
 *   top-right    — Live ready (PnL+ AND fill_rate ≥ 50%)
 *   top-left     — Slipping (PnL+ but bad fills, может book thin)
 *   bottom-right — Thin book (good fills но losing money — kill candidate)
 *   bottom-left  — Killed candidate (всё плохо)
 */
export function LiveReadinessMap({ items, isLoading = false, onDotClick }: Props) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const isCompact = useIsMobile(700);

  const yRange = useMemo(() => {
    const pnls = items.map((i) => i.virtual_7d.pnl_usd);
    if (pnls.length === 0) return { min: -50, max: 100 };
    const min = Math.min(0, ...pnls);
    const max = Math.max(50, ...pnls);
    const padding = (max - min) * 0.1;
    return { min: Math.floor((min - padding) / 10) * 10, max: Math.ceil((max + padding) / 10) * 10 };
  }, [items]);

  const yToPlot = useMemo(
    () => (val: number) =>
      PAD.top + ((yRange.max - val) / (yRange.max - yRange.min || 1)) * PLOT_H,
    [yRange],
  );

  const xGuide = PAD.left + 0.5 * PLOT_W; // fill_rate = 50%
  const yGuide = yToPlot(0); // pnl = $0

  const points = useMemo(() => {
    return items
      .filter((i) => i.virtual_7d.fill_rate_pct != null)
      .map((item) => {
        const fr = item.virtual_7d.fill_rate_pct!;
        const pnl = item.virtual_7d.pnl_usd;
        const x = PAD.left + (fr / 100) * PLOT_W;
        const y = yToPlot(pnl);
        const size = Math.max(6, Math.min(28, Math.sqrt(Math.max(item.virtual_7d.n_placed, 1)) * 4));
        return { item, x, y, size };
      });
  }, [items, yToPlot]);

  return (
    <section
      style={{
        position: 'relative',
        padding: '32px 24px 16px',
        borderTop: `1px solid ${RULE}`,
        borderBottom: `1px solid ${RULE}`,
        background: `linear-gradient(180deg, ${STAGE}06 0%, transparent 100%)`,
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
            <div className="ed-kicker" style={{ color: STAGE, letterSpacing: '0.18em', fontSize: 10, marginBottom: 4 }}>
              💸 Live Readiness Map
            </div>
            <h2
              className="ed-display"
              style={{ fontSize: 22, fontWeight: 400, margin: 0, letterSpacing: '-0.01em', color: 'var(--ed-ink)' }}
            >
              <em style={{ fontStyle: 'italic' }}>Ready</em> for real money?
            </h2>
          </div>
          <div className="ed-mono" style={{ color: FAINT, fontSize: 11, letterSpacing: '0.04em' }}>
            {items.length} virtual detectors · 7d window
          </div>
        </header>

        {isLoading ? (
          <div style={{ color: FAINT, fontSize: 12, padding: 40, textAlign: 'center' }}>loading…</div>
        ) : points.length === 0 ? (
          <div style={{ color: FAINT, fontSize: 12, padding: 40, textAlign: 'center' }}>
            Нет virtual детекторов с fills 7d.
          </div>
        ) : (
          <>
            <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto', display: 'block' }}>
              {/* Quadrant guides */}
              <line x1={xGuide} y1={PAD.top} x2={xGuide} y2={H - PAD.bottom} stroke={RULE} strokeDasharray="3 4" />
              <line x1={PAD.left} y1={yGuide} x2={W - PAD.right} y2={yGuide} stroke={RULE} strokeDasharray="3 4" />

              {!isCompact && (
                <>
                  <QuadrantLabel x={W - PAD.right - 8} y={PAD.top + 14} anchor="end" label="Live ready" color="var(--ed-up)" />
                  <QuadrantLabel x={PAD.left + 8} y={PAD.top + 14} anchor="start" label="Slipping (good PnL, bad fills)" color="var(--ed-accent)" />
                  <QuadrantLabel x={W - PAD.right - 8} y={H - PAD.bottom - 8} anchor="end" label="Thin book / kill" color={DIM} />
                  <QuadrantLabel x={PAD.left + 8} y={H - PAD.bottom - 8} anchor="start" label="Killed candidate" color="var(--ed-down)" />
                </>
              )}

              {/* Y axis ticks */}
              {[0, 0.25, 0.5, 0.75, 1].map((frac) => {
                const value = yRange.min + frac * (yRange.max - yRange.min);
                const y = yToPlot(value);
                return (
                  <g key={`y-${frac}`}>
                    <line x1={PAD.left - 4} y1={y} x2={PAD.left} y2={y} stroke={FAINT} />
                    <text x={PAD.left - 8} y={y + 4} textAnchor="end" fontSize={10} fontFamily="var(--ed-mono)" fill={FAINT}>
                      ${value.toFixed(0)}
                    </text>
                  </g>
                );
              })}

              {/* X axis ticks */}
              {[0, 25, 50, 75, 100].map((pct) => {
                const x = PAD.left + (pct / 100) * PLOT_W;
                return (
                  <g key={`x-${pct}`}>
                    <line x1={x} y1={H - PAD.bottom} x2={x} y2={H - PAD.bottom + 4} stroke={FAINT} />
                    <text x={x} y={H - PAD.bottom + 18} textAnchor="middle" fontSize={10} fontFamily="var(--ed-mono)" fill={FAINT}>
                      {pct}%
                    </text>
                  </g>
                );
              })}

              <text
                x={PAD.left + PLOT_W / 2}
                y={H - 12}
                textAnchor="middle"
                fontSize={10}
                fontFamily="var(--ed-mono)"
                fill={DIM}
                style={{ letterSpacing: '0.12em', textTransform: 'uppercase' }}
              >
                Fill rate (≥50% threshold) →
              </text>
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
                Order PnL 7d (paper · color=gates_v2)
              </text>

              {/* Dots */}
              {points.map((p, i) => {
                const isHover = hoverIdx === i;
                const color = liveReadyColor(p.item.gates_v2_passed, p.item.gates_v2_total);
                return (
                  <g
                    key={p.item.detector}
                    onMouseEnter={() => setHoverIdx(i)}
                    onMouseLeave={() => setHoverIdx(null)}
                    onClick={() => onDotClick?.(p.item.detector)}
                    style={{ cursor: 'pointer' }}
                  >
                    {isHover && <circle cx={p.x} cy={p.y} r={p.size + 8} fill={color} opacity={0.18} />}
                    <circle
                      cx={p.x}
                      cy={p.y}
                      r={isHover ? p.size * 1.15 : p.size}
                      fill={color}
                      opacity={0.85}
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
                      style={{ opacity: isHover || p.item.ready_v2 ? 1 : 0.65, transition: 'opacity 150ms' }}
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

function QuadrantLabel({
  x,
  y,
  anchor,
  label,
  color,
}: {
  x: number;
  y: number;
  anchor: 'start' | 'middle' | 'end';
  label: string;
  color: string;
}) {
  return (
    <text
      x={x}
      y={y}
      textAnchor={anchor}
      fontSize={9}
      fontFamily="var(--ed-mono)"
      fill={color}
      style={{ letterSpacing: '0.14em', textTransform: 'uppercase', opacity: 0.7 }}
    >
      {label}
    </text>
  );
}

function Tooltip({ item }: { item: LiveReadinessItem }) {
  const color = liveReadyColor(item.gates_v2_passed, item.gates_v2_total);
  const fr = item.virtual_7d;
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
          {item.gates_v2_passed}/{item.gates_v2_total} gates · {item.days_virtual}d virtual
        </div>
      </div>
      <div style={{ display: 'flex', gap: 18, color: DIM, fontSize: 11, fontFamily: 'var(--ed-mono)' }}>
        <Stat label="placed/filled" value={`${fr.n_placed}/${fr.n_filled}`} />
        <Stat label="fill rate" value={fr.fill_rate_pct != null ? `${fr.fill_rate_pct.toFixed(1)}%` : '—'} />
        <Stat label="PnL 7d" value={fmtUsd(fr.pnl_usd, { sign: true })} />
        <Stat label="WR" value={fr.wr_pct != null ? `${fr.wr_pct.toFixed(1)}%` : '—'} />
        <Stat
          label="vs projected"
          value={
            item.canonical.projected_7d_usd != null
              ? `${((fr.pnl_usd / item.canonical.projected_7d_usd) * 100).toFixed(0)}%`
              : '—'
          }
        />
      </div>
      <div style={{ color: FAINT, fontSize: 10 }}>click → details ↓</div>
    </div>
  );
}

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ color: FAINT, fontSize: 9, letterSpacing: '0.12em', textTransform: 'uppercase' }}>{label}</div>
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
      <LegendDot color="var(--ed-up)" label="5/5 ready" />
      <LegendDot color="var(--ed-accent)" label="4/5 close" />
      <LegendDot color="var(--ed-ink-dim)" label="2-3/5 partial" />
      <LegendDot color={FAINT} label="≤1/5" />
      <span>· bigger dot = more virtual orders 7d</span>
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

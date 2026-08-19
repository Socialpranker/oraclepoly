import { useMemo, useState } from 'react';
import type { BacktestMapItem } from '../../../../hooks/useBot';
import { verdictColor, fmtUsd, fmtPct, detectorShort } from './utils';
import { useIsMobile } from '../../../../hooks/useViewport';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';
const STAGE = '#9b6cff';

interface Props {
  items: BacktestMapItem[];
  isLoading?: boolean;
  onDotClick?: (detector: string) => void;
}

const W = 1100;
const H = 360;
const PAD = { top: 36, right: 30, bottom: 56, left: 90 };
const PLOT_W = W - PAD.left - PAD.right;
const PLOT_H = H - PAD.top - PAD.bottom;

/**
 * BacktestMap — 2D scatter всех детекторов с MD-файлами.
 *
 *   Y axis: honest_daily_usd_at_5k (0 → max, log если spread велик)
 *   X axis: honest_haircut_factor (0 → 1)
 *   Dot size: sqrt(trades_per_day) × 4, clamp 6-32px
 *   Dot color: verdict (PASS/FAIL/MARGINAL/no-MD)
 *   Quadrant guides: haircut=0.5, daily=$10/day
 *
 * Pure SVG — без libraries. Hover показывает tooltip, click → onDotClick.
 *
 * Smaller viewport: SVG масштабируется через viewBox. Touch-friendly.
 */
export function BacktestMap({ items, isLoading = false, onDotClick }: Props) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  const isCompact = useIsMobile(700);

  const points = useMemo(() => {
    const filtered = items.filter(
      (i) => i.honest_haircut_factor != null && i.honest_daily_usd_at_5k != null,
    );
    if (filtered.length === 0) return [];

    const yMax = Math.max(...filtered.map((i) => i.honest_daily_usd_at_5k!));
    const yMaxRound = Math.ceil(yMax / 10) * 10 || 10;

    return filtered.map((item) => {
      const xRaw = item.honest_haircut_factor!;
      const yRaw = item.honest_daily_usd_at_5k!;
      const x = PAD.left + xRaw * PLOT_W;
      const y = PAD.top + (1 - yRaw / yMaxRound) * PLOT_H;
      const sizeRaw = item.trades_per_day ?? 1;
      const size = Math.max(6, Math.min(32, Math.sqrt(sizeRaw) * 4));
      return { item, x, y, size };
    });
  }, [items]);

  const yMaxRound = useMemo(() => {
    const ys = items
      .map((i) => i.honest_daily_usd_at_5k)
      .filter((v): v is number => v != null);
    if (ys.length === 0) return 100;
    return Math.ceil(Math.max(...ys) / 10) * 10 || 10;
  }, [items]);

  // Quadrant guide lines (haircut=0.5, daily=$10)
  const xGuide = PAD.left + 0.5 * PLOT_W;
  const yGuide = PAD.top + (1 - 10 / yMaxRound) * PLOT_H;

  return (
    <section
      style={{
        position: 'relative',
        padding: '32px 24px 16px',
        borderTop: `1px solid ${RULE}`,
        borderBottom: `1px solid ${RULE}`,
        background: 'linear-gradient(180deg, rgba(155,108,255,0.04) 0%, transparent 100%)',
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
              🧪 Backtest Map
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
              <em style={{ fontStyle: 'italic' }}>Decisions</em>, at a glance
            </h2>
          </div>
          <div
            className="ed-mono"
            style={{ color: FAINT, fontSize: 11, letterSpacing: '0.04em' }}
          >
            {items.length} detectors · canonical 821d
          </div>
        </header>

        {isLoading ? (
          <div style={{ color: FAINT, fontSize: 12, padding: 40, textAlign: 'center' }}>
            loading map…
          </div>
        ) : points.length === 0 ? (
          <div style={{ color: FAINT, fontSize: 12, padding: 40, textAlign: 'center' }}>
            Нет детекторов с canonical backtest. Добавь docs/backtests/{`{detector}`}.md.
          </div>
        ) : (
          <>
            <svg
              viewBox={`0 0 ${W} ${H}`}
              style={{ width: '100%', height: 'auto', display: 'block' }}
              role="img"
              aria-label="Backtest scatter map: honest daily PnL vs haircut factor"
            >
              {/* Quadrant guide lines */}
              <line
                x1={xGuide}
                y1={PAD.top}
                x2={xGuide}
                y2={H - PAD.bottom}
                stroke={RULE}
                strokeDasharray="3 4"
              />
              <line
                x1={PAD.left}
                y1={yGuide}
                x2={W - PAD.right}
                y2={yGuide}
                stroke={RULE}
                strokeDasharray="3 4"
              />

              {/* Quadrant labels (corner annotations). Hidden on mobile to reduce noise. */}
              {!isCompact && (
                <>
                  <QuadrantLabel
                    x={W - PAD.right - 8}
                    y={PAD.top + 14}
                    anchor="end"
                    label="Promote candidates"
                    color="var(--ed-up)"
                  />
                  <QuadrantLabel
                    x={PAD.left + 8}
                    y={PAD.top + 14}
                    anchor="start"
                    label="Re-audit haircut"
                    color="var(--ed-accent)"
                  />
                  <QuadrantLabel
                    x={W - PAD.right - 8}
                    y={H - PAD.bottom - 8}
                    anchor="end"
                    label="Marginal — verify"
                    color={DIM}
                  />
                  <QuadrantLabel
                    x={PAD.left + 8}
                    y={H - PAD.bottom - 8}
                    anchor="start"
                    label="Killed / weak"
                    color="var(--ed-down)"
                  />
                </>
              )}

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

              {/* X axis ticks */}
              {[0, 0.25, 0.5, 0.75, 1].map((frac) => {
                const x = PAD.left + frac * PLOT_W;
                return (
                  <g key={`x-${frac}`}>
                    <line x1={x} y1={H - PAD.bottom} x2={x} y2={H - PAD.bottom + 4} stroke={FAINT} />
                    <text
                      x={x}
                      y={H - PAD.bottom + 18}
                      textAnchor="middle"
                      fontSize={10}
                      fontFamily="var(--ed-mono)"
                      fill={FAINT}
                    >
                      ×{frac.toFixed(2)}
                    </text>
                  </g>
                );
              })}

              {/* Axis labels */}
              <text
                x={PAD.left + PLOT_W / 2}
                y={H - 12}
                textAnchor="middle"
                fontSize={10}
                fontFamily="var(--ed-mono)"
                fill={DIM}
                style={{ letterSpacing: '0.12em', textTransform: 'uppercase' }}
              >
                Honest haircut factor →
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
                Honest $/day @ $5K
              </text>

              {/* Dots */}
              {points.map((p, i) => {
                const isHover = hoverIdx === i;
                const color = verdictColor(p.item.verdict);
                return (
                  <g
                    key={p.item.detector}
                    onMouseEnter={() => setHoverIdx(i)}
                    onMouseLeave={() => setHoverIdx(null)}
                    onClick={() => onDotClick?.(p.item.detector)}
                    style={{ cursor: 'pointer' }}
                  >
                    {/* Outer ring если detector promoted дальше backtest */}
                    {p.item.stage && p.item.stage !== 'backtest' && (
                      <circle
                        cx={p.x}
                        cy={p.y}
                        r={p.size + 5}
                        fill="none"
                        stroke={stageColor(p.item.stage)}
                        strokeWidth={1}
                        strokeDasharray="2 3"
                        opacity={0.6}
                      />
                    )}
                    {/* Halo on hover */}
                    {isHover && (
                      <circle
                        cx={p.x}
                        cy={p.y}
                        r={p.size + 8}
                        fill={color}
                        opacity={0.18}
                      />
                    )}
                    <circle
                      cx={p.x}
                      cy={p.y}
                      r={isHover ? p.size * 1.15 : p.size}
                      fill={color}
                      opacity={p.item.verdict ? 0.85 : 0.35}
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
                        opacity: isHover || (p.item.verdict === 'PASS' && p.size > 12) ? 1 : 0.6,
                        transition: 'opacity 150ms',
                      }}
                    >
                      {detectorShort(p.item.detector)}
                    </text>
                  </g>
                );
              })}
            </svg>

            {/* Tooltip pinned below map (avoids overflow on mobile) */}
            {hoverIdx != null && points[hoverIdx] && (
              <Tooltip item={points[hoverIdx].item} />
            )}

            {/* Legend */}
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

function Tooltip({ item }: { item: BacktestMapItem }) {
  return (
    <div
      style={{
        marginTop: 12,
        padding: '12px 16px',
        border: `1px solid ${verdictColor(item.verdict)}40`,
        background: 'rgba(255,255,255,0.02)',
        display: 'grid',
        gridTemplateColumns: 'auto 1fr auto',
        gap: 16,
        alignItems: 'center',
        fontSize: 12,
      }}
    >
      <div>
        <div
          className="ed-mono"
          style={{
            fontSize: 13,
            color: 'var(--ed-ink)',
            fontWeight: 500,
          }}
        >
          {item.detector}
        </div>
        <div
          className="ed-kicker"
          style={{
            fontSize: 9,
            letterSpacing: '0.18em',
            color: verdictColor(item.verdict),
            marginTop: 2,
          }}
        >
          {item.verdict ?? '— no doc'}
          {item.stage && item.stage !== 'backtest' && ` · stage=${item.stage}`}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 18, color: DIM, fontSize: 11, fontFamily: 'var(--ed-mono)' }}>
        <Stat label="honest @ $5K" value={fmtUsd(item.honest_daily_usd_at_5k, { sign: true })} />
        <Stat label="haircut" value={item.honest_haircut_factor ? `×${item.honest_haircut_factor.toFixed(2)}` : '—'} />
        <Stat label="trades/d" value={item.trades_per_day?.toFixed(1) ?? '—'} />
        <Stat label="WR" value={fmtPct(item.wr_pct)} />
        <Stat label="ROI" value={fmtPct(item.roi_net_pct, { sign: true })} />
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
      <LegendDot color="var(--ed-up)" label="PASS" />
      <LegendDot color="var(--ed-accent)" label="MARGINAL" />
      <LegendDot color="var(--ed-down)" label="FAIL" />
      <LegendDot color={FAINT} label="no doc" />
      <span>· bigger dot = more trades/day</span>
      <span>· dashed ring = promoted past backtest</span>
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

function stageColor(stage: string): string {
  const colors: Record<string, string> = {
    shadow: '#5b9dff',
    virtual: '#e0b020',
    live: '#e04444',
    paused: 'var(--ed-ink-faint)',
  };
  return colors[stage] ?? 'var(--ed-ink-faint)';
}

import { useMemo, useState } from 'react';
import type { EquityCurvePoint } from '../../../../hooks/useBot';
import { fmtUsd, fmtShortDate, STAGE_RED } from './utils';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';
const VIRTUAL_GOLD = '#e0b020';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';

interface Props {
  data: EquityCurvePoint[];
}

const W = 1180;
const H = 380;
const PAD_TOP = 36;
const PAD_RIGHT = 56;
const PAD_BOTTOM = 56;
const PAD_LEFT = 64;

/**
 * EquityCurve — area chart cumulative live PnL за 7d с overlay virtual baseline.
 *
 * Differentiator стадии Live. Видно "идём по плану" (live ≈ virtual) или
 * "съедает slippage" (live < virtual).
 *
 * Hover → vertical tracker line с tooltip per-day breakdown.
 * Без mount-анимаций: компонент перерисовывается каждый поллинг (30s).
 */
export function EquityCurve({ data }: Props) {
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);

  const { liveLine, virtualLine, areaPath, yMin, yMax, xStep } = useMemo(() => {
    const allValues = data.flatMap((p) => [p.live_pnl_cum, p.virtual_baseline_cum, 0]);
    let yMin = Math.min(...allValues);
    let yMax = Math.max(...allValues);
    if (yMin === yMax) {
      yMin -= 10;
      yMax += 10;
    }
    const range = yMax - yMin;
    yMin -= range * 0.1;
    yMax += range * 0.1;

    const xStep = (W - PAD_LEFT - PAD_RIGHT) / Math.max(1, data.length - 1);

    const xAt = (i: number) => PAD_LEFT + i * xStep;
    const yAt = (v: number) => {
      if (yMax === yMin) return PAD_TOP + (H - PAD_TOP - PAD_BOTTOM) / 2;
      return PAD_TOP + ((yMax - v) / (yMax - yMin)) * (H - PAD_TOP - PAD_BOTTOM);
    };

    const liveLine = data
      .map((p, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i)} ${yAt(p.live_pnl_cum)}`)
      .join(' ');

    const virtualLine = data
      .map((p, i) => `${i === 0 ? 'M' : 'L'} ${xAt(i)} ${yAt(p.virtual_baseline_cum)}`)
      .join(' ');

    const baseY = yAt(0);
    const areaPath =
      `M ${xAt(0)} ${baseY} ` +
      data.map((p, i) => `L ${xAt(i)} ${yAt(p.live_pnl_cum)}`).join(' ') +
      ` L ${xAt(data.length - 1)} ${baseY} Z`;

    return { liveLine, virtualLine, areaPath, yMin, yMax, xStep };
  }, [data]);

  const xAt = (i: number) => PAD_LEFT + i * xStep;
  const yAt = (v: number) => {
    if (yMax === yMin) return PAD_TOP + (H - PAD_TOP - PAD_BOTTOM) / 2;
    return PAD_TOP + ((yMax - v) / (yMax - yMin)) * (H - PAD_TOP - PAD_BOTTOM);
  };

  const handleMove = (e: React.MouseEvent<SVGSVGElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const xRatio = (e.clientX - rect.left) / rect.width;
    const xPx = xRatio * W - PAD_LEFT;
    const idx = Math.round(xPx / xStep);
    setHoverIdx(Math.max(0, Math.min(data.length - 1, idx)));
  };

  if (data.length === 0) {
    return (
      <div
        style={{
          padding: '40px 22px',
          textAlign: 'center',
          color: FAINT,
          fontSize: 12,
          fontStyle: 'italic',
        }}
      >
        Нет данных для equity curve.
      </div>
    );
  }

  // Y axis ticks: 5 equal slots
  const ticks: number[] = [];
  for (let i = 0; i <= 4; i++) {
    ticks.push(yMin + ((yMax - yMin) * i) / 4);
  }

  const hover = hoverIdx !== null ? data[hoverIdx] : null;

  return (
    <div
      style={{
        position: 'relative',
        margin: '0 auto',
        maxWidth: 1180,
        padding: '0 22px',
        marginBottom: 28,
      }}
    >
      <div style={{ marginBottom: 14 }}>
        <div
          className="ed-kicker"
          style={{
            fontSize: 10,
            letterSpacing: '0.22em',
            color: STAGE_RED,
            fontWeight: 600,
            marginBottom: 6,
            textTransform: 'uppercase',
          }}
        >
          Equity curve · 7d cumulative
        </div>
        <div
          className="ed-display"
          style={{ fontSize: 13, color: DIM, fontStyle: 'italic', fontWeight: 300 }}
        >
          Live realized vs virtual baseline (что должно было быть)
        </div>
      </div>

      <svg
        viewBox={`0 0 ${W} ${H}`}
        style={{
          width: '100%',
          height: 'auto',
          background: 'rgba(224,68,68,0.025)',
          border: `1px solid ${STAGE_RED}30`,
          cursor: 'crosshair',
        }}
        onMouseMove={handleMove}
        onMouseLeave={() => setHoverIdx(null)}
      >
        <defs>
          <linearGradient id="liveGradient" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor={STAGE_RED} stopOpacity="0.45" />
            <stop offset="100%" stopColor={STAGE_RED} stopOpacity="0.02" />
          </linearGradient>
        </defs>

        {/* Y axis grid + ticks */}
        {ticks.map((v, i) => {
          const y = yAt(v);
          return (
            <g key={i}>
              <line
                x1={PAD_LEFT}
                x2={W - PAD_RIGHT}
                y1={y}
                y2={y}
                stroke={RULE}
                strokeDasharray={v === 0 ? '0' : '2 4'}
                strokeWidth={v === 0 ? 1 : 0.5}
                opacity={v === 0 ? 0.5 : 0.3}
              />
              <text
                x={PAD_LEFT - 10}
                y={y + 4}
                textAnchor="end"
                fontSize="10"
                fontFamily="var(--ed-mono)"
                fill={FAINT}
              >
                {v >= 0 ? '+' : '−'}${Math.abs(v).toFixed(0)}
              </text>
            </g>
          );
        })}

        {/* X axis dates */}
        {data.map((p, i) => (
          <g key={i}>
            <line
              x1={xAt(i)}
              x2={xAt(i)}
              y1={H - PAD_BOTTOM}
              y2={H - PAD_BOTTOM + 5}
              stroke={FAINT}
              strokeWidth={0.5}
            />
            <text
              x={xAt(i)}
              y={H - PAD_BOTTOM + 20}
              textAnchor="middle"
              fontSize="10"
              fontFamily="var(--ed-mono)"
              fill={FAINT}
              opacity={i === 0 || i === data.length - 1 || i === Math.floor(data.length / 2) ? 1 : 0.6}
            >
              {fmtShortDate(p.date)}
            </text>
          </g>
        ))}

        {/* Live area fill */}
        <path d={areaPath} fill="url(#liveGradient)" />

        {/* Virtual baseline (dashed gold) */}
        <path
          d={virtualLine}
          fill="none"
          stroke={VIRTUAL_GOLD}
          strokeWidth={1.5}
          strokeDasharray="4 4"
          opacity={0.85}
        />

        {/* Live line */}
        <path
          d={liveLine}
          fill="none"
          stroke={STAGE_RED}
          strokeWidth={2.4}
          strokeLinejoin="round"
          strokeLinecap="round"
        />

        {/* Dots */}
        {data.map((p, i) => (
          <circle
            key={i}
            cx={xAt(i)}
            cy={yAt(p.live_pnl_cum)}
            r={hoverIdx === i ? 5 : 3}
            fill={STAGE_RED}
            stroke="var(--ed-bg)"
            strokeWidth={1.5}
          />
        ))}

        {/* Hover line */}
        {hoverIdx !== null && (
          <line
            x1={xAt(hoverIdx)}
            x2={xAt(hoverIdx)}
            y1={PAD_TOP}
            y2={H - PAD_BOTTOM}
            stroke={STAGE_RED}
            strokeWidth={1}
            strokeDasharray="3 3"
            opacity={0.7}
          />
        )}

        {/* Quadrant: legend */}
        <g transform={`translate(${PAD_LEFT + 12}, ${PAD_TOP + 12})`}>
          <rect width={4} height={4} fill={STAGE_RED} />
          <text x={10} y={5} fontSize="10" fontFamily="var(--ed-mono)" fill={DIM}>
            live realized
          </text>
          <line x1={108} y1={3} x2={120} y2={3} stroke={VIRTUAL_GOLD} strokeWidth={1.5} strokeDasharray="3 3" />
          <text x={126} y={5} fontSize="10" fontFamily="var(--ed-mono)" fill={DIM}>
            virtual baseline
          </text>
        </g>
      </svg>

      {/* Hover tooltip */}
      {hover && (
        <div
          style={{
            marginTop: 10,
            padding: '10px 14px',
            background: 'rgba(255,255,255,0.04)',
            border: `1px solid ${STAGE_RED}40`,
            display: 'grid',
            gridTemplateColumns: 'repeat(4, 1fr)',
            gap: 14,
            fontSize: 11,
            fontFamily: 'var(--ed-mono)',
          }}
        >
          <div>
            <span style={{ color: FAINT, fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.16em' }}>
              Date
            </span>
            <div style={{ color: 'var(--ed-ink)', marginTop: 2 }}>{fmtShortDate(hover.date)}</div>
          </div>
          <div>
            <span style={{ color: FAINT, fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.16em' }}>
              Live cum
            </span>
            <div style={{ color: hover.live_pnl_cum >= 0 ? UP : DOWN, marginTop: 2 }}>
              {fmtUsd(hover.live_pnl_cum, { sign: true })}
            </div>
          </div>
          <div>
            <span style={{ color: FAINT, fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.16em' }}>
              Day Δ
            </span>
            <div style={{ color: hover.live_pnl_day >= 0 ? UP : DOWN, marginTop: 2 }}>
              {fmtUsd(hover.live_pnl_day, { sign: true })}
            </div>
          </div>
          <div>
            <span style={{ color: FAINT, fontSize: 9, textTransform: 'uppercase', letterSpacing: '0.16em' }}>
              Virtual
            </span>
            <div style={{ color: VIRTUAL_GOLD, marginTop: 2 }}>
              {fmtUsd(hover.virtual_baseline_cum, { sign: true })}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

import { useMemo } from 'react';
import type { BotStrategy, EquityPoint } from '../../../hooks/useBot';

interface Props {
  strategy: BotStrategy;
  equity: EquityPoint[];
  caseNumber: number;
  mode: 'dry' | 'live';
}

function buildSparkPath(points: EquityPoint[], w: number, h: number): { line: string; area: string } {
  if (points.length < 2) return { line: '', area: '' };
  const vals = points.map((p) => p.value);
  const min = Math.min(...vals);
  const max = Math.max(...vals);
  const range = max - min || 1;
  const step = w / (points.length - 1);
  const line = points
    .map((p, i) => {
      const x = i * step;
      const y = h - ((p.value - min) / range) * h;
      return `${i === 0 ? 'M' : 'L'}${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  return { line, area: `${line} L${w},${h} L0,${h} Z` };
}

function formatKey(key: string): string {
  return key
    .replace(/_/g, ' ')
    .replace(/\b([a-z])/g, (m) => m.toUpperCase());
}

export function DetailHero({ strategy, equity, caseNumber, mode }: Props) {
  const earned = strategy.earned ?? 0;
  const roiPct = strategy.roi_pct ?? 0;
  const winRate = strategy.all_time?.win_rate ?? 0;
  const profitFactor = strategy.all_time?.profit_factor ?? 0;

  const up = earned >= 0;
  const color = up ? 'var(--ed-up)' : 'var(--ed-down)';
  const paper = mode === 'live' ? 'Live' : 'Paper';

  const { line, area } = useMemo(() => buildSparkPath(equity, 720, 120), [equity]);

  const roiAbs = Math.abs(roiPct);
  const roiInt = Math.floor(roiAbs);
  const roiDec = (roiAbs - roiInt).toFixed(2).slice(2);

  return (
    <section className="relative pt-6 pb-8 px-5 ed-grain">
      <div className="ed-fade-up">
        {/* Masthead */}
        <div className="flex items-center justify-between mb-1">
          <span className="ed-kicker">
            {paper} · Case № {caseNumber.toString().padStart(2, '0')}
          </span>
          <div className="flex items-center gap-1.5">
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: 999,
                background: strategy.enabled ? 'var(--ed-up)' : 'var(--ed-ink-faint)',
                display: 'inline-block',
              }}
            />
            <span
              className="ed-mono"
              style={{
                fontSize: 9,
                letterSpacing: '0.12em',
                color: strategy.enabled ? 'var(--ed-up)' : 'var(--ed-ink-faint)',
              }}
            >
              {strategy.enabled ? 'ACTIVE' : 'DORMANT'}
            </span>
          </div>
        </div>

        {/* Strategy name as masthead */}
        <h1
          className="ed-display"
          style={{
            fontSize: 22,
            lineHeight: 1.1,
            fontWeight: 500,
            color: 'var(--ed-ink)',
            letterSpacing: '-0.01em',
            marginBottom: 18,
          }}
        >
          {formatKey(strategy.key)}
        </h1>

        {/* ROI hero with italic % drop-cap */}
        <div className="flex items-baseline gap-1 mt-1">
          <span
            className="ed-display"
            style={{ fontSize: 14, fontStyle: 'italic', color: 'var(--ed-ink-dim)', fontWeight: 300 }}
          >
            {up ? '+' : '−'}
          </span>
          <span
            className="ed-display"
            style={{
              fontSize: 84,
              lineHeight: 0.88,
              fontWeight: 500,
              color: 'var(--ed-ink)',
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            {roiInt}
          </span>
          <span
            className="ed-display"
            style={{
              fontSize: 36,
              lineHeight: 1,
              color: 'var(--ed-ink-dim)',
              fontWeight: 400,
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            .{roiDec}
          </span>
          <span
            className="ed-display"
            style={{
              fontSize: 36,
              lineHeight: 1,
              color,
              fontWeight: 400,
              fontStyle: 'italic',
              marginLeft: 4,
            }}
          >
            %
          </span>
        </div>

        {/* Subtitle row */}
        <div className="flex items-center gap-3 mt-3 flex-wrap">
          <span
            className="ed-mono"
            style={{ fontSize: 13, color, fontWeight: 500 }}
          >
            {up ? '+' : '−'}${Math.abs(earned).toLocaleString('en-US', { maximumFractionDigits: 0 })}
          </span>
          <span className="ed-kicker" style={{ fontSize: 9 }}>
            earned
          </span>
          <span style={{ color: 'var(--ed-ink-faint)' }}>·</span>
          <span
            className="ed-mono"
            style={{ fontSize: 13, color: 'var(--ed-ink)', fontWeight: 500 }}
          >
            {winRate.toFixed(1)}%
          </span>
          <span className="ed-kicker" style={{ fontSize: 9 }}>
            WR
          </span>
          <span style={{ color: 'var(--ed-ink-faint)' }}>·</span>
          <span
            className="ed-mono"
            style={{ fontSize: 13, color: 'var(--ed-ink)', fontWeight: 500 }}
          >
            {profitFactor.toFixed(2)}
          </span>
          <span className="ed-kicker" style={{ fontSize: 9 }}>
            PF
          </span>
          <div className="flex-1" />
          <span
            className="ed-mono"
            style={{ fontSize: 10, color: 'var(--ed-ink-faint)' }}
          >
            n {strategy.all_time.total_bets}
          </span>
        </div>
      </div>

      {/* Full-width equity curve, no crop */}
      {equity.length >= 2 && (
        <div
          className="mt-6 -mx-5 ed-fade-up"
          style={{ animationDelay: '200ms', animationDuration: '0.8s' }}
        >
          <svg
            viewBox="0 0 720 120"
            className="w-full"
            style={{ height: 120 }}
            preserveAspectRatio="none"
          >
            <defs>
              <linearGradient id={`ed-hero-fill-${strategy.key}`} x1="0" x2="0" y1="0" y2="1">
                <stop offset="0%" stopColor={color} stopOpacity="0.28" />
                <stop offset="100%" stopColor={color} stopOpacity="0" />
              </linearGradient>
              <pattern
                id="ed-grid-pattern"
                x="0"
                y="0"
                width="60"
                height="30"
                patternUnits="userSpaceOnUse"
              >
                <line x1="0" y1="0" x2="0" y2="30" stroke="var(--ed-rule)" strokeWidth="0.5" />
              </pattern>
            </defs>
            <rect width="720" height="120" fill="url(#ed-grid-pattern)" />
            <path d={area} fill={`url(#ed-hero-fill-${strategy.key})`} />
            <path
              d={line}
              fill="none"
              stroke={color}
              strokeWidth="1.5"
              strokeLinejoin="round"
              strokeLinecap="round"
            />
          </svg>
        </div>
      )}

      <hr className="ed-rule-strong absolute bottom-0 left-5 right-5" />
    </section>
  );
}

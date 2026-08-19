import type { SettleVelocity } from '../../../../hooks/useBot';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';

const COLOR_FAST = 'var(--ed-up)';
const COLOR_MID = '#7fc99c';
const COLOR_SLOW = 'var(--ed-stage, #e0b020)';
const COLOR_LATE = 'var(--ed-down)';

interface Props {
  velocity: SettleVelocity;
  variant?: 'compact' | 'full';
}

function fmtMedian(hours: number | null): string {
  if (hours == null) return '—';
  if (hours < 24) return `${hours.toFixed(1)}h`;
  return `${(hours / 24).toFixed(1)}d`;
}

export function SettleVelocityBar({ velocity, variant = 'compact' }: Props) {
  const { buckets, median_hours, n, window_days } = velocity;
  const isFull = variant === 'full';
  const titleSize = isFull ? 11 : 9;
  const barHeight = isFull ? 8 : 4;
  const labelSize = isFull ? 11 : 10;

  if (n === 0) {
    return (
      <div>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontSize: titleSize,
            letterSpacing: '0.16em',
            textTransform: 'uppercase',
            color: FAINT,
            marginBottom: 6,
            fontFamily: 'var(--ed-mono)',
          }}
        >
          <span>Settle velocity{window_days ? ` · ${window_days}d` : ''}</span>
          <span style={{ color: DIM }}>n=0 · нет settled</span>
        </div>
        <div style={{ height: barHeight, background: RULE, opacity: 0.4 }} />
      </div>
    );
  }

  if (!buckets) {
    return (
      <div>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontSize: titleSize,
            letterSpacing: '0.16em',
            textTransform: 'uppercase',
            color: FAINT,
            marginBottom: 6,
            fontFamily: 'var(--ed-mono)',
          }}
        >
          <span>Settle velocity{window_days ? ` · ${window_days}d` : ''}</span>
          <span style={{ color: DIM }}>n={n} · мало данных (нужно ≥5)</span>
        </div>
        <div style={{ height: barHeight, background: RULE, opacity: 0.4 }} />
      </div>
    );
  }

  const segments = [
    { pct: buckets.under_24h_pct, color: COLOR_FAST, label: '<24h' },
    { pct: buckets.d1_3_pct, color: COLOR_MID, label: '1-3d' },
    { pct: buckets.d3_7_pct, color: COLOR_SLOW, label: '3-7d' },
    { pct: buckets.over_7d_pct, color: COLOR_LATE, label: '>7d' },
  ];

  const tooltip = segments.map((s) => `${s.label}: ${s.pct.toFixed(1)}%`).join(' · ');

  return (
    <div>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          fontSize: titleSize,
          letterSpacing: '0.16em',
          textTransform: 'uppercase',
          color: FAINT,
          marginBottom: 6,
          fontFamily: 'var(--ed-mono)',
        }}
      >
        <span>Settle velocity{window_days ? ` · ${window_days}d` : ''}</span>
        <span style={{ color: DIM, fontWeight: 600 }}>
          median {fmtMedian(median_hours)} · n={n}
        </span>
      </div>
      <div
        style={{
          height: barHeight,
          background: RULE,
          position: 'relative',
          display: 'flex',
          overflow: 'hidden',
        }}
        title={tooltip}
      >
        {segments.map((s, idx) => (
          <div
            key={idx}
            style={{
              width: `${s.pct}%`,
              background: s.color,
              opacity: s.pct > 0 ? 0.85 : 0,
              transition: 'width 200ms ease',
            }}
          />
        ))}
      </div>
      {isFull && (
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontSize: labelSize,
            color: DIM,
            marginTop: 8,
            fontFamily: 'var(--ed-mono)',
            gap: 12,
            flexWrap: 'wrap',
          }}
        >
          {segments.map((s) => (
            <span key={s.label} style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
              <span
                style={{
                  display: 'inline-block',
                  width: 8,
                  height: 8,
                  background: s.color,
                  opacity: 0.85,
                }}
              />
              <span style={{ color: FAINT }}>{s.label}:</span>
              <span style={{ color: s.color, fontWeight: 600 }}>{s.pct.toFixed(1)}%</span>
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

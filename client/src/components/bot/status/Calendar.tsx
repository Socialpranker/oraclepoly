import { Fragment, useState } from 'react';
import { useV2ActivityCalendar } from '../../../hooks/useBot';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const ACCENT = 'var(--ed-accent)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';
const RULE = 'var(--ed-rule)';

type Mode = 'pnl' | 'placed';
type RangeOpt = 30 | 60 | 90;

/**
 * Calendar — heat-map активности бота по дням за 30/60/90 дней.
 *
 * Две метрики переключаются:
 *   - PnL net: green/red по знаку, intensity по abs(value)
 *   - Placed: accent intensity по числу размещённых
 *
 * Показывает сезонность работы бота, длинные «тихие» периоды, и резкие
 * всплески активности/убытков. Источник: useV2ActivityCalendar.
 */
export function Calendar() {
  const [days, setDays] = useState<RangeOpt>(60);
  const [mode, setMode] = useState<Mode>('pnl');
  const { data, isLoading } = useV2ActivityCalendar(days);

  const series = data?.items ?? [];

  // Maps for fast lookup
  const dayMap = new Map(series.map((d) => [d.date, d]));

  // Build grid: weeks × 7 days, top row Mon..Sun. Anchor: today.
  const today = new Date();
  today.setUTCHours(0, 0, 0, 0);
  const start = new Date(today);
  start.setUTCDate(start.getUTCDate() - (days - 1));
  // Round to Monday
  const startDow = (start.getUTCDay() + 6) % 7; // 0=Mon..6=Sun
  start.setUTCDate(start.getUTCDate() - startDow);

  const cells: { date: Date; key: string; inRange: boolean }[] = [];
  const cur = new Date(start);
  while (cur <= today) {
    const key = cur.toISOString().slice(0, 10);
    const startCheck = new Date(today);
    startCheck.setUTCDate(startCheck.getUTCDate() - (days - 1));
    cells.push({
      date: new Date(cur),
      key,
      inRange: cur >= startCheck,
    });
    cur.setUTCDate(cur.getUTCDate() + 1);
  }

  // Compute color scale
  const valueOf = (d: { placed: number; pnl_net_usd: number }) =>
    mode === 'pnl' ? d.pnl_net_usd : d.placed;
  const maxAbs = Math.max(
    ...series.map((d) => Math.abs(valueOf(d))),
    1,
  );

  // Aggregate stats for header
  const totalPnl = series.reduce((s, d) => s + d.pnl_net_usd, 0);
  const totalPlaced = series.reduce((s, d) => s + d.placed, 0);
  const totalSettled = series.reduce((s, d) => s + d.settled, 0);
  const activeDays = series.filter((d) => d.placed > 0).length;
  const greenDays = series.filter((d) => d.pnl_net_usd > 0.5).length;
  const redDays = series.filter((d) => d.pnl_net_usd < -0.5).length;
  const totalPnlColor = totalPnl > 0.5 ? UP : totalPnl < -0.5 ? DOWN : DIM;

  return (
    <section style={{ marginBottom: 24 }}>
      <div className="ed-kicker" style={{ color: FAINT, marginBottom: 4 }}>
        Календарь · {days}d
      </div>
      <div className="ed-mono" style={{ color: DIM, fontSize: 11, marginBottom: 14 }}>
        heat-map активности — глянь когда бот был занят и когда тихо
      </div>

      <div
        style={{
          background: 'var(--ed-bg-raised)',
          border: `1px solid ${RULE}`,
          padding: '14px 14px 12px',
        }}
      >
        {/* Top: stats + controls */}
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'flex-start',
            marginBottom: 16,
            flexWrap: 'wrap',
            gap: 12,
          }}
        >
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(3, auto)',
              gap: 16,
            }}
          >
            <Stat
              label="дней активно"
              value={`${activeDays}`}
              sub={`из ${days}`}
            />
            <Stat
              label="placed"
              value={totalPlaced.toLocaleString()}
              sub={`${totalSettled.toLocaleString()} resolved`}
            />
            <Stat
              label="PnL net"
              value={`${totalPnl >= 0 ? '+' : '−'}$${Math.abs(totalPnl).toFixed(2)}`}
              valueColor={totalPnlColor}
              sub={`${greenDays} зел · ${redDays} крас`}
            />
          </div>

          <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
            <Toggle
              options={[
                { value: 'pnl', label: 'PnL' },
                { value: 'placed', label: 'placed' },
              ]}
              value={mode}
              onChange={(v) => setMode(v as Mode)}
            />
            <Toggle
              options={[
                { value: '30', label: '30d' },
                { value: '60', label: '60d' },
                { value: '90', label: '90d' },
              ]}
              value={String(days)}
              onChange={(v) => setDays(Number(v) as RangeOpt)}
            />
          </div>
        </div>

        {isLoading && (
          <div
            className="ed-mono"
            style={{ color: FAINT, fontSize: 11, padding: '32px 0' }}
          >
            loading…
          </div>
        )}

        {!isLoading && (
          <div
            style={{
              display: 'flex',
              justifyContent: 'center',
              overflowX: 'auto',
            }}
          >
            <div style={{ minWidth: 'min-content' }}>
              {/* Day-of-week labels */}
              <div
                style={{
                  display: 'grid',
                  gridTemplateColumns: '24px repeat(7, 18px)',
                  gap: 3,
                  marginBottom: 4,
                }}
              >
                <div />
                {['пн', 'вт', 'ср', 'чт', 'пт', 'сб', 'вс'].map((d) => (
                  <div
                    key={d}
                    className="ed-mono"
                    style={{
                      fontSize: 9,
                      color: FAINT,
                      textAlign: 'center',
                      opacity: 0.6,
                    }}
                  >
                    {d}
                  </div>
                ))}
              </div>

              {/* Cells grid: rows = weeks, cols = days */}
              <CalendarGrid cells={cells} dayMap={dayMap} mode={mode} maxAbs={maxAbs} />
            </div>
          </div>
        )}

        {/* Legend */}
        <div
          style={{
            marginTop: 12,
            paddingTop: 10,
            borderTop: `1px solid ${RULE}`,
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 8,
            fontSize: 9,
            color: DIM,
          }}
          className="ed-mono"
        >
          <span>{mode === 'pnl' ? '−' : '0'}</span>
          <Gradient mode={mode} />
          <span>{mode === 'pnl' ? '+' : 'много'}</span>
        </div>
      </div>
    </section>
  );
}

function CalendarGrid({
  cells,
  dayMap,
  mode,
  maxAbs,
}: {
  cells: { date: Date; key: string; inRange: boolean }[];
  dayMap: Map<string, { placed: number; pnl_net_usd: number; settled: number; wins: number; rejected: number }>;
  mode: Mode;
  maxAbs: number;
}) {
  // Group cells into weeks (rows of 7)
  const weeks: typeof cells[] = [];
  for (let i = 0; i < cells.length; i += 7) {
    weeks.push(cells.slice(i, i + 7));
  }

  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: '24px repeat(7, 18px)',
        gridAutoRows: '18px',
        gap: 3,
      }}
    >
      {weeks.map((week, wi) => {
        const monday = week[0]?.date;
        const showLabel =
          wi === 0 || (monday && monday.getUTCDate() <= 7);
        return (
          <Fragment key={`w${wi}`}>
            <div
              className="ed-mono"
              style={{
                fontSize: 9,
                color: FAINT,
                opacity: 0.7,
                textAlign: 'right',
                lineHeight: '18px',
                paddingRight: 4,
              }}
            >
              {showLabel && monday
                ? new Intl.DateTimeFormat('ru', { month: 'short' })
                    .format(monday)
                    .replace('.', '')
                : ''}
            </div>
            {week.map((c) => {
              const data = dayMap.get(c.key);
              const value = data
                ? mode === 'pnl'
                  ? data.pnl_net_usd
                  : data.placed
                : 0;
              const intensity = Math.min(1, Math.abs(value) / maxAbs);
              const color = cellColor(mode, value, intensity, c.inRange);

              const tooltip = data
                ? `${c.key}: ${data.placed} placed, ${data.settled} resolved (${data.wins}W), ${
                    data.pnl_net_usd >= 0 ? '+' : '−'
                  }$${Math.abs(data.pnl_net_usd).toFixed(2)}`
                : `${c.key}: нет данных`;

              return (
                <div
                  key={c.key}
                  title={tooltip}
                  style={{
                    width: 18,
                    height: 18,
                    background: color,
                    border: c.inRange ? `1px solid ${RULE}` : 'none',
                    cursor: c.inRange ? 'help' : 'default',
                    transition: 'transform 0.1s ease',
                  }}
                  onMouseEnter={(e) => {
                    if (c.inRange && data) e.currentTarget.style.transform = 'scale(1.4)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.transform = 'scale(1)';
                  }}
                />
              );
            })}
          </Fragment>
        );
      })}
    </div>
  );
}

function cellColor(mode: Mode, value: number, intensity: number, inRange: boolean): string {
  if (!inRange) return 'transparent';
  if (mode === 'pnl') {
    if (value === 0) return 'var(--ed-bg)';
    const base = value > 0 ? UP : DOWN;
    return mixColor(base, intensity);
  }
  // placed
  if (value === 0) return 'var(--ed-bg)';
  return mixColor(ACCENT, intensity);
}

function mixColor(cssVar: string, alpha: number): string {
  // Используем CSS color-mix для прозрачности → quasi heat-map intensity.
  // alpha: 0..1; нижний bound 0.18 чтобы хоть что-то было видно.
  const a = Math.max(0.18, alpha);
  return `color-mix(in srgb, ${cssVar} ${(a * 100).toFixed(0)}%, var(--ed-bg))`;
}

function Stat({
  label,
  value,
  sub,
  valueColor = 'var(--ed-ink)',
}: {
  label: string;
  value: string;
  sub: string;
  valueColor?: string;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 1 }}>
      <span
        className="ed-kicker"
        style={{ fontSize: 8, color: FAINT, letterSpacing: '0.14em' }}
      >
        {label}
      </span>
      <span
        className="ed-mono"
        style={{
          fontSize: 14,
          fontWeight: 500,
          color: valueColor,
          fontVariantNumeric: 'tabular-nums',
          lineHeight: 1.1,
        }}
      >
        {value}
      </span>
      <span className="ed-mono" style={{ fontSize: 9, color: FAINT }}>
        {sub}
      </span>
    </div>
  );
}

function Toggle({
  options,
  value,
  onChange,
}: {
  options: { value: string; label: string }[];
  value: string;
  onChange: (v: string) => void;
}) {
  return (
    <div
      style={{
        display: 'flex',
        gap: 0,
        border: `1px solid ${RULE}`,
      }}
      className="ed-mono"
    >
      {options.map((opt) => {
        const active = opt.value === value;
        return (
          <button
            key={opt.value}
            onClick={() => onChange(opt.value)}
            style={{
              background: active ? ACCENT : 'transparent',
              color: active ? 'var(--ed-bg)' : FAINT,
              border: 'none',
              padding: '3px 8px',
              fontSize: 9,
              cursor: 'pointer',
              textTransform: 'uppercase',
              letterSpacing: '0.1em',
              fontFamily: 'var(--ed-mono)',
            }}
          >
            {opt.label}
          </button>
        );
      })}
    </div>
  );
}

function Gradient({ mode }: { mode: Mode }) {
  const stops =
    mode === 'pnl'
      ? `${DOWN}, var(--ed-bg), ${UP}`
      : `var(--ed-bg), ${ACCENT}`;
  return (
    <div
      style={{
        width: 80,
        height: 12,
        background: `linear-gradient(to right, ${stops})`,
        border: `1px solid ${RULE}`,
      }}
    />
  );
}

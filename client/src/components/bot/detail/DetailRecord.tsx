import type { HistoryDailyEntry, BotFill } from '../../../hooks/useBot';

interface Props {
  daily: HistoryDailyEntry[];
  fills: BotFill[];
}

interface Streak {
  side: 'win' | 'loss';
  length: number;
}

function computeStreak(fills: BotFill[]): Streak {
  if (fills.length === 0) return { side: 'win', length: 0 };
  const settled = fills.filter((f) => f.status === 'settled' || f.pnl !== 0);
  if (settled.length === 0) return { side: 'win', length: 0 };
  const sorted = [...settled].sort((a, b) => (b.closed_at || '').localeCompare(a.closed_at || ''));
  const first = sorted[0];
  const side: 'win' | 'loss' = first.pnl >= 0 ? 'win' : 'loss';
  let length = 0;
  for (const f of sorted) {
    const s: 'win' | 'loss' = f.pnl >= 0 ? 'win' : 'loss';
    if (s === side) length += 1;
    else break;
  }
  return { side, length };
}

export function DetailRecord({ daily, fills }: Props) {
  const streak = computeStreak(fills);

  const bestDay = daily.reduce<HistoryDailyEntry | null>(
    (acc, d) => (acc === null || d.net_pnl > acc.net_pnl ? d : acc),
    null,
  );
  const worstDay = daily.reduce<HistoryDailyEntry | null>(
    (acc, d) => (acc === null || d.net_pnl < acc.net_pnl ? d : acc),
    null,
  );

  const maxAbs = daily.reduce((acc, d) => Math.max(acc, Math.abs(d.net_pnl)), 1);

  return (
    <section className="relative px-5 py-7 ed-grain">
      <div className="flex items-center gap-2 mb-5">
        <span className="ed-kicker">The Record</span>
        <hr className="ed-rule flex-1" />
        <span className="ed-kicker" style={{ fontSize: 9 }}>
          last {daily.length}d
        </span>
      </div>

      {/* Streak headline */}
      {streak.length > 0 && (
        <div
          className="ed-drop-cap mb-6"
          style={{
            fontFamily: 'var(--ed-display)',
            fontSize: 18,
            lineHeight: 1.35,
            color: 'var(--ed-ink)',
            fontWeight: 400,
            letterSpacing: '-0.005em',
          }}
        >
          {streak.side === 'win' ? `${streak.length} побед подряд` : `${streak.length} поражения подряд`}
          <span
            className="ed-display"
            style={{
              color: streak.side === 'win' ? 'var(--ed-up)' : 'var(--ed-down)',
              fontStyle: 'italic',
              marginLeft: 6,
            }}
          >
            —
          </span>
          <span
            className="ed-body"
            style={{ fontSize: 12, color: 'var(--ed-ink-dim)', marginLeft: 4 }}
          >
            {streak.side === 'win'
              ? 'последняя серия на позитиве.'
              : 'стратегия находится под давлением.'}
          </span>
        </div>
      )}

      {/* Best/Worst side by side */}
      {bestDay && worstDay && bestDay.date !== worstDay.date && (
        <div className="grid grid-cols-2 gap-3 mb-6">
          <DayCard label="Лучший день" day={bestDay} tone="up" />
          <DayCard label="Худший день" day={worstDay} tone="down" />
        </div>
      )}

      {/* Per-day micro-bars (newspaper turn-line) */}
      <div>
        <div className="ed-kicker mb-3" style={{ fontSize: 9 }}>
          Turn-line · {daily.length}d PnL
        </div>
        <div className="flex items-end gap-1" style={{ height: 60 }}>
          {daily.map((d) => {
            const pct = Math.abs(d.net_pnl) / maxAbs;
            const up = d.net_pnl >= 0;
            return (
              <div
                key={d.date}
                className="flex-1 flex flex-col items-center justify-end"
                style={{ height: '100%' }}
              >
                <div
                  style={{
                    width: '100%',
                    height: `${Math.max(pct * 100, 2)}%`,
                    background: up ? 'var(--ed-up)' : 'var(--ed-down)',
                    opacity: 0.75,
                    transition: 'height 0.5s cubic-bezier(0.22, 1, 0.36, 1)',
                  }}
                />
              </div>
            );
          })}
        </div>
        <div className="flex justify-between mt-2">
          <span
            className="ed-mono"
            style={{ fontSize: 9, color: 'var(--ed-ink-faint)' }}
          >
            {daily[0]?.date.slice(5) ?? ''}
          </span>
          <span
            className="ed-mono"
            style={{ fontSize: 9, color: 'var(--ed-ink-faint)' }}
          >
            max ±${maxAbs.toFixed(0)}
          </span>
          <span
            className="ed-mono"
            style={{ fontSize: 9, color: 'var(--ed-ink-faint)' }}
          >
            {daily[daily.length - 1]?.date.slice(5) ?? ''}
          </span>
        </div>
      </div>

      <hr className="ed-rule-strong mt-7" />
    </section>
  );
}

function DayCard({
  label,
  day,
  tone,
}: {
  label: string;
  day: HistoryDailyEntry;
  tone: 'up' | 'down';
}) {
  const color = tone === 'up' ? 'var(--ed-up)' : 'var(--ed-down)';
  const sign = tone === 'up' ? '+' : '−';
  return (
    <div
      style={{
        borderTop: `1px solid ${tone === 'up' ? 'rgba(95,185,122,0.35)' : 'rgba(214,90,90,0.35)'}`,
        paddingTop: 10,
      }}
    >
      <div className="ed-kicker" style={{ fontSize: 9, marginBottom: 6 }}>
        {label}
      </div>
      <div
        className="ed-display"
        style={{
          fontSize: 24,
          lineHeight: 1,
          fontWeight: 500,
          color,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        {sign}${Math.abs(day.net_pnl).toFixed(0)}
      </div>
      <div
        className="ed-mono"
        style={{ fontSize: 9, color: 'var(--ed-ink-faint)', marginTop: 4 }}
      >
        {day.date.slice(5)} · {day.wins}w {day.losses}l
      </div>
    </div>
  );
}

import type { BotPosition } from '../../../hooks/useBot';

interface Props {
  positions: BotPosition[];
}

function truncate(s: string | null, n: number): string {
  if (!s) return '—';
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

export function DetailPositions({ positions }: Props) {
  if (positions.length === 0) {
    return (
      <section className="px-5 py-6">
        <div className="flex items-center gap-2 mb-3">
          <span className="ed-kicker">Open Positions</span>
          <hr className="ed-rule flex-1" />
          <span className="ed-kicker" style={{ fontSize: 9 }}>
            empty
          </span>
        </div>
        <p
          className="ed-display"
          style={{
            fontSize: 13,
            color: 'var(--ed-ink-faint)',
            fontStyle: 'italic',
            textAlign: 'center',
            padding: '10px 0',
          }}
        >
          никаких открытых позиций · кеш на холде
        </p>
        <hr className="ed-rule-strong mt-4" />
      </section>
    );
  }

  return (
    <section className="px-5 py-6">
      <div className="flex items-center gap-2 mb-4">
        <span className="ed-kicker">Open Positions</span>
        <hr className="ed-rule flex-1" />
        <span className="ed-kicker" style={{ fontSize: 9 }}>
          {positions.length} active
        </span>
      </div>
      <ul className="ed-rise" style={{ display: 'flex', flexDirection: 'column', gap: 0 }}>
        {positions.map((p, idx) => (
          <li
            key={p.id}
            style={{
              display: 'flex',
              alignItems: 'baseline',
              gap: 10,
              padding: '10px 0',
              borderTop: idx === 0 ? 'none' : '1px solid var(--ed-rule)',
            }}
          >
            <span
              className="ed-mono"
              style={{ fontSize: 10, color: 'var(--ed-ink-faint)', minWidth: 22 }}
            >
              {String(idx + 1).padStart(2, '0')}
            </span>
            <span
              className="ed-chip"
              data-tone={p.side.toUpperCase() === 'YES' ? 'up' : 'down'}
            >
              {p.side.toUpperCase()}
            </span>
            <span
              className="ed-body flex-1"
              style={{ fontSize: 12, color: 'var(--ed-ink)', lineHeight: 1.3 }}
            >
              {truncate(p.market_question, 60)}
            </span>
            <span
              className="ed-mono"
              style={{ fontSize: 11, color: 'var(--ed-ink-dim)' }}
            >
              @ {p.entry_price.toFixed(2)}
            </span>
            <span
              className="ed-mono"
              style={{ fontSize: 12, color: 'var(--ed-ink)', fontWeight: 500 }}
            >
              ${p.stake.toFixed(0)}
            </span>
          </li>
        ))}
      </ul>
      <hr className="ed-rule-strong mt-4" />
    </section>
  );
}

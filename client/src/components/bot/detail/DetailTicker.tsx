import type { BotFill } from '../../../hooks/useBot';

interface Props {
  fills: BotFill[];
}

function truncate(s: string | null, n: number): string {
  if (!s) return '—';
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  return `${d.getUTCHours().toString().padStart(2, '0')}:${d.getUTCMinutes().toString().padStart(2, '0')}`;
}

export function DetailTicker({ fills }: Props) {
  if (fills.length === 0) return null;

  const doubled = [...fills, ...fills];

  return (
    <section className="relative py-4 overflow-hidden ed-ticker-viewport">
      <div className="flex items-center gap-2 px-5 mb-2">
        <span className="ed-kicker">Tape</span>
        <hr className="ed-rule flex-1" />
        <span className="ed-kicker" style={{ fontSize: 9 }}>
          {fills.length} fills · hover to pause
        </span>
      </div>

      <div
        style={{
          maskImage: 'linear-gradient(90deg, transparent, #000 10%, #000 90%, transparent)',
          WebkitMaskImage: 'linear-gradient(90deg, transparent, #000 10%, #000 90%, transparent)',
          padding: '8px 0',
          borderTop: '1px solid var(--ed-rule)',
          borderBottom: '1px solid var(--ed-rule)',
        }}
      >
        <div className="ed-ticker-track">
          {doubled.map((f, i) => {
            const up = f.pnl >= 0;
            const color = up ? 'var(--ed-up)' : 'var(--ed-down)';
            return (
              <span
                key={`${f.id}-${i}`}
                className="inline-flex items-baseline gap-2 whitespace-nowrap"
              >
                <span
                  className="ed-mono"
                  style={{ fontSize: 10, color: 'var(--ed-ink-faint)' }}
                >
                  {formatTime(f.closed_at || f.opened_at)}
                </span>
                <span
                  className="ed-chip"
                  data-tone={f.side.toUpperCase() === 'YES' ? 'up' : 'down'}
                >
                  {f.side.toUpperCase()}
                </span>
                <span
                  className="ed-body"
                  style={{ fontSize: 11, color: 'var(--ed-ink)' }}
                >
                  {truncate(f.market_question, 46)}
                </span>
                <span
                  className="ed-mono"
                  style={{ fontSize: 12, color, fontWeight: 500 }}
                >
                  {up ? '+' : '−'}${Math.abs(f.pnl).toFixed(2)}
                </span>
                <span
                  className="ed-display"
                  style={{ fontSize: 14, color: 'var(--ed-ink-faint)', fontStyle: 'italic' }}
                >
                  ·
                </span>
              </span>
            );
          })}
        </div>
      </div>
    </section>
  );
}

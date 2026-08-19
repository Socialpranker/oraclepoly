import type { HistoryLedgerEntry } from '../../hooks/useBot';

function timeAgo(dateStr: string): string {
  const diff = Date.now() - new Date(dateStr).getTime();
  const mins = Math.floor(diff / 60000);
  if (mins < 60) return `${mins}m`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h`;
  const days = Math.floor(hours / 24);
  return `${days}d`;
}

export function HistoryLedger({ entries }: { entries: HistoryLedgerEntry[] }) {
  if (!entries.length) {
    return <div className="text-center text-sm text-[var(--color-text-dim)] py-8">Нет записей за период</div>;
  }

  return (
    <div className="px-4 space-y-1.5">
      {entries.map((e, i) => {
        const isWin = e.event_type === 'win';
        const pnlColor = e.pnl >= 0 ? '#00FF85' : '#FF3B30';
        const sign = e.pnl >= 0 ? '+' : '';

        return (
          <div
            key={`${e.date}-${i}`}
            className="rounded-lg px-3 py-2 flex items-center justify-between"
            style={{ background: 'rgba(255,255,255,0.04)' }}
          >
            <div className="flex-1 min-w-0 mr-3">
              <div className="text-[11px] text-[var(--color-text-primary)] truncate">
                {e.question || 'Unknown market'}
              </div>
              <div className="flex items-center gap-1.5 mt-0.5 text-[10px] text-[var(--color-text-dim)]">
                <span className="font-mono">{e.strategy_key}</span>
                <span>·</span>
                <span>{e.side}</span>
                <span>·</span>
                <span>${e.stake.toFixed(0)} @ {(e.entry_price * 100).toFixed(0)}¢</span>
                <span>·</span>
                <span>{e.category}</span>
              </div>
            </div>
            <div className="text-right flex-shrink-0">
              <div className="text-sm font-bold font-mono" style={{ color: pnlColor }}>
                {sign}${Math.abs(e.pnl).toFixed(0)}
              </div>
              <div className="text-[9px] text-[var(--color-text-dim)]">
                {isWin ? '✓' : '✗'} {e.date ? timeAgo(e.date) : ''}
              </div>
            </div>
          </div>
        );
      })}
    </div>
  );
}

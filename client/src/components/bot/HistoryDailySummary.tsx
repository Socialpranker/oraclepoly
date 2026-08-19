import { useState } from 'react';
import { ChevronDown, ChevronUp } from 'lucide-react';
import type { HistoryDailyEntry } from '../../hooks/useBot';

function formatPnl(val: number): string {
  const sign = val >= 0 ? '+' : '-';
  return `${sign}$${Math.abs(val).toLocaleString('en-US', { maximumFractionDigits: 0 })}`;
}

function DayRow({ day }: { day: HistoryDailyEntry }) {
  const [expanded, setExpanded] = useState(false);
  const pnlColor = day.net_pnl >= 0 ? '#00FF85' : '#FF3B30';
  const strats = Object.entries(day.strategies);

  return (
    <div
      className="rounded-xl mb-2"
      style={{ background: 'rgba(255,255,255,0.05)', border: '1px solid rgba(255,255,255,0.07)' }}
    >
      <button
        onClick={() => setExpanded(!expanded)}
        className="w-full px-3 py-2.5 flex items-center justify-between text-left"
      >
        <div className="flex items-center gap-3">
          <span className="text-xs font-mono text-[var(--color-text-secondary)]">{day.date}</span>
          <span className="text-xs">
            <span style={{ color: '#00FF85' }}>{day.wins}W</span>
            <span className="text-[var(--color-text-dim)] mx-0.5">/</span>
            <span style={{ color: '#FF3B30' }}>{day.losses}L</span>
          </span>
        </div>
        <div className="flex items-center gap-3">
          {day.frozen_released > 0 && (
            <span className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: 'rgba(0,255,133,0.1)', color: '#00FF85' }}>
              unfrozen ${day.frozen_released.toFixed(0)}
            </span>
          )}
          <span className="text-sm font-bold font-mono" style={{ color: pnlColor }}>
            {formatPnl(day.net_pnl)}
          </span>
          {strats.length > 0 && (
            expanded
              ? <ChevronUp className="w-3.5 h-3.5 text-[var(--color-text-dim)]" />
              : <ChevronDown className="w-3.5 h-3.5 text-[var(--color-text-dim)]" />
          )}
        </div>
      </button>

      {expanded && strats.length > 0 && (() => {
        const won = strats.reduce((sum, [, s]) => sum + (s.pnl > 0 ? s.pnl : 0), 0);
        const lost = strats.reduce((sum, [, s]) => sum + (s.pnl < 0 ? s.pnl : 0), 0);
        return (
          <div className="px-3 pb-2.5 border-t border-white/5">
            {strats.map(([key, s]) => (
              <div key={key} className="flex items-center justify-between py-1 text-[11px]">
                <span className="text-[var(--color-text-secondary)] font-mono">{key}</span>
                <div className="flex items-center gap-2">
                  <span>
                    <span style={{ color: '#00FF85' }}>{s.wins}W</span>
                    <span className="text-[var(--color-text-dim)]">/</span>
                    <span style={{ color: '#FF3B30' }}>{s.losses}L</span>
                  </span>
                  <span className="font-mono font-semibold" style={{ color: s.pnl >= 0 ? '#00FF85' : '#FF3B30' }}>
                    {formatPnl(s.pnl)}
                  </span>
                </div>
              </div>
            ))}
            <div className="grid grid-cols-3 gap-2 pt-2 mt-1.5 border-t border-white/5">
              <div className="text-center">
                <div className="text-[8px] uppercase tracking-wider text-[var(--color-text-dim)]">Won</div>
                <div className="text-xs font-bold font-mono" style={{ color: '#00FF85' }}>
                  +${Math.abs(won).toLocaleString('en-US', { maximumFractionDigits: 0 })}
                </div>
              </div>
              <div className="text-center">
                <div className="text-[8px] uppercase tracking-wider text-[var(--color-text-dim)]">Lost</div>
                <div className="text-xs font-bold font-mono" style={{ color: '#FF3B30' }}>
                  -${Math.abs(lost).toLocaleString('en-US', { maximumFractionDigits: 0 })}
                </div>
              </div>
              <div className="text-center">
                <div className="text-[8px] uppercase tracking-wider text-[var(--color-text-dim)]">Balance</div>
                <div className="text-xs font-bold font-mono" style={{ color: day.net_pnl >= 0 ? '#00FF85' : '#FF3B30' }}>
                  {formatPnl(day.net_pnl)}
                </div>
              </div>
            </div>
          </div>
        );
      })()}
    </div>
  );
}

export function HistoryDailySummary({ daily }: { daily: HistoryDailyEntry[] }) {
  if (!daily.length) {
    return <div className="text-center text-sm text-[var(--color-text-dim)] py-8">Нет данных за период</div>;
  }

  return (
    <div className="px-4">
      {daily.map((day) => (
        <DayRow key={day.date} day={day} />
      ))}
    </div>
  );
}

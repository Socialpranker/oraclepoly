import { useState } from 'react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { useBotHistory } from '../hooks/useBot';
import { HistoryDailySummary } from '../components/bot/HistoryDailySummary';
import { HistoryLedger } from '../components/bot/HistoryLedger';
import { EditorialHistory } from '../components/bot/history/EditorialHistory';

const PERIODS = [
  { label: '7d', days: 7 },
  { label: '14d', days: 14 },
  { label: '30d', days: 30 },
];

type Tab = 'summary' | 'ledger';

export function BotHistory() {
  const navigate = useNavigate();
  const { strategyKey } = useParams<{ strategyKey?: string }>();
  const [searchParams] = useSearchParams();
  const mode = (searchParams.get('mode') === 'live' ? 'live' : 'dry') as 'dry' | 'live';
  const [days, setDays] = useState(14);
  const [tab, setTab] = useState<Tab>('summary');
  const { data, isLoading } = useBotHistory(days, strategyKey, mode);

  // Live mode — editorial layout (не затрагивает per-strategy view)
  if (mode === 'live' && !strategyKey) {
    return <EditorialHistory />;
  }

  if (isLoading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="text-sm text-[var(--color-text-dim)]">Загрузка...</div>
      </div>
    );
  }

  const summary = data?.summary;
  const pnlColor = (summary?.total_pnl ?? 0) >= 0 ? '#00FF85' : '#FF3B30';

  return (
    <div className="min-h-screen pb-8" style={{ background: 'var(--color-bg-primary)' }}>
      {/* Header */}
      <div
        className="px-4 py-3 flex items-center gap-2"
        style={{ borderBottom: '1px solid rgba(255,255,255,0.06)' }}
      >
        <button
          onClick={() => navigate(strategyKey ? `/bot/${strategyKey}` : '/bot')}
          className="text-[#9E7AFF] text-sm flex items-center gap-1"
        >
          <ArrowLeft className="w-4 h-4" /> {strategyKey ?? 'Bot'}
        </button>
        <span className="text-base font-bold text-[var(--color-text-primary)] ml-2">
          {strategyKey ? `${strategyKey} · History` : 'History'}
        </span>
        <span
          className="ml-auto text-[10px] font-semibold px-2 py-0.5 rounded-md"
          style={{
            background: mode === 'live' ? 'rgba(0,255,133,0.12)' : 'rgba(255,106,61,0.12)',
            color: mode === 'live' ? '#00FF85' : '#FF6A3D',
          }}
        >
          {mode === 'live' ? 'Live' : 'Dry Run'}
        </span>
      </div>

      {/* Period selector */}
      <div className="px-4 py-3 flex items-center gap-2">
        {PERIODS.map((p) => (
          <button
            key={p.days}
            onClick={() => setDays(p.days)}
            className="px-3 py-1.5 rounded-lg text-xs font-semibold transition-colors"
            style={{
              background: days === p.days ? 'rgba(158,122,255,0.2)' : 'rgba(255,255,255,0.05)',
              color: days === p.days ? '#9E7AFF' : 'var(--color-text-secondary)',
              border: `1px solid ${days === p.days ? 'rgba(158,122,255,0.3)' : 'transparent'}`,
            }}
          >
            {p.label}
          </button>
        ))}
      </div>

      {/* Summary KPIs */}
      {summary && (
        <div className="px-4 pb-3 space-y-2">
          <div className="grid grid-cols-3 gap-2">
            <div className="rounded-xl p-3 text-center" style={{ background: 'rgba(0,255,133,0.06)' }}>
              <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-dim)]">Won</div>
              <div className="text-base font-bold font-mono" style={{ color: '#00FF85' }}>
                +${Math.abs(summary.total_won).toLocaleString('en-US', { maximumFractionDigits: 0 })}
              </div>
            </div>
            <div className="rounded-xl p-3 text-center" style={{ background: 'rgba(255,59,48,0.06)' }}>
              <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-dim)]">Lost</div>
              <div className="text-base font-bold font-mono" style={{ color: '#FF3B30' }}>
                -${Math.abs(summary.total_lost).toLocaleString('en-US', { maximumFractionDigits: 0 })}
              </div>
            </div>
            <div className="rounded-xl p-3 text-center" style={{ background: 'rgba(255,255,255,0.05)' }}>
              <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-dim)]">P&L</div>
              <div className="text-base font-bold font-mono" style={{ color: pnlColor }}>
                {summary.total_pnl >= 0 ? '+' : '-'}${Math.abs(summary.total_pnl).toLocaleString('en-US', { maximumFractionDigits: 0 })}
              </div>
            </div>
          </div>
          <div className="grid grid-cols-2 gap-2">
            <div className="rounded-xl p-2.5 text-center" style={{ background: 'rgba(255,255,255,0.05)' }}>
              <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-dim)]">Unfrozen</div>
              <div className="text-sm font-bold font-mono" style={{ color: '#00FF85' }}>
                ${summary.total_frozen_released.toLocaleString('en-US', { maximumFractionDigits: 0 })}
              </div>
            </div>
            <div className="rounded-xl p-2.5 text-center" style={{ background: 'rgba(255,255,255,0.05)' }}>
              <div className="text-[9px] uppercase tracking-wider text-[var(--color-text-dim)]">Events</div>
              <div className="text-sm font-bold font-mono text-[var(--color-text-primary)]">
                {summary.total_events}
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Tab switch */}
      <div className="px-4 pb-3 flex gap-1">
        <button
          onClick={() => setTab('summary')}
          className="flex-1 py-2 rounded-lg text-xs font-semibold"
          style={{
            background: tab === 'summary' ? 'rgba(158,122,255,0.15)' : 'rgba(255,255,255,0.05)',
            color: tab === 'summary' ? '#9E7AFF' : 'var(--color-text-dim)',
          }}
        >
          По дням
        </button>
        <button
          onClick={() => setTab('ledger')}
          className="flex-1 py-2 rounded-lg text-xs font-semibold"
          style={{
            background: tab === 'ledger' ? 'rgba(158,122,255,0.15)' : 'rgba(255,255,255,0.05)',
            color: tab === 'ledger' ? '#9E7AFF' : 'var(--color-text-dim)',
          }}
        >
          Все события
        </button>
      </div>

      {/* Content */}
      {data && tab === 'summary' && <HistoryDailySummary daily={data.daily} />}
      {data && tab === 'ledger' && <HistoryLedger entries={data.ledger} />}
    </div>
  );
}

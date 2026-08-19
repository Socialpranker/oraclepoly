import { useNavigate } from 'react-router-dom';
import { BarChart3, Bot, Telescope } from 'lucide-react';
import type { BotTab } from '../../../hooks/useBot';

interface Props {
  tab: BotTab;
  onTabChange: (t: BotTab) => void;
  children: React.ReactNode;
}

interface TabSpec {
  id: BotTab;
  label: string;
  emoji: string;
  color: string;
  title: string;
}

const TABS: TabSpec[] = [
  {
    id: 'backtest',
    label: 'Backtest',
    emoji: '🧪',
    color: '#9b6cff',
    title: 'Canonical predicted: гипотеза проверена на 821 днях истории',
  },
  {
    id: 'shadow',
    label: 'Shadow',
    emoji: '👁',
    color: '#5b9dff',
    title: 'Live signals в БД, без денег — производственная валидация',
  },
  {
    id: 'virtual',
    label: 'Virtual',
    emoji: '💸',
    color: '#e0b020',
    title: 'Paper trading через full pipeline (calibrate → size → safety)',
  },
  {
    id: 'live',
    label: 'Live',
    emoji: '🔴',
    color: '#e04444',
    title: 'Реальные ордера в Polymarket CLOB — настоящие $$$',
  },
];

/**
 * EditorialShell — общий каркас /bot.
 *
 * Lifecycle architecture (2026-05-02): 4 stages tabs.
 *   Backtest → Shadow → Virtual → Live
 * См. docs/detector_lifecycle.md.
 */
export function EditorialShell({ tab, onTabChange, children }: Props) {
  const navigate = useNavigate();

  return (
    <div
      className="editorial"
      style={{
        background: 'var(--ed-bg)',
        color: 'var(--ed-ink)',
        fontFamily: 'var(--ed-body)',
        minHeight: '100vh',
      }}
    >
      <div
        className="px-5 pt-4 pb-3"
        style={{ borderBottom: '1px solid var(--ed-rule-strong)' }}
      >
        <div className="flex items-center justify-between mb-3">
          <h1
            className="ed-display"
            style={{
              fontSize: 22,
              fontWeight: 500,
              color: 'var(--ed-ink)',
              letterSpacing: '-0.02em',
            }}
          >
            Oracle<span style={{ color: 'var(--ed-accent)' }}>.</span>
          </h1>

          <div className="flex gap-5">
            <button
              className="ed-kicker flex items-center gap-1.5"
              style={{ color: 'var(--ed-ink)' }}
            >
              <Bot size={11} />
              Bot
            </button>
            <button
              onClick={() => navigate('/analytics')}
              className="ed-kicker flex items-center gap-1.5"
              style={{ color: 'var(--ed-ink-faint)' }}
            >
              <BarChart3 size={11} />
              Analytics
            </button>
            <button
              onClick={() => navigate('/bot/research/tier2')}
              className="ed-kicker flex items-center gap-1.5"
              style={{ color: 'var(--ed-ink-faint)' }}
              title="Tier-2 unique data: UMA disputes, Manifold lead-lag, whale shocks, cohorts"
            >
              <Telescope size={11} />
              Insights
            </button>
          </div>
        </div>

        <div className="flex gap-5 flex-wrap">
          {TABS.map((spec) => {
            const active = tab === spec.id;
            return (
              <button
                key={spec.id}
                onClick={() => onTabChange(spec.id)}
                className="ed-kicker flex items-center gap-1.5"
                style={{
                  color: active ? spec.color : 'var(--ed-ink-faint)',
                  paddingBottom: 6,
                  borderBottom: active ? `2px solid ${spec.color}` : '1px solid transparent',
                  transition: 'all 150ms',
                  fontSize: 11,
                }}
                title={spec.title}
              >
                <span style={{ fontSize: 13 }}>{spec.emoji}</span>
                {spec.label}
              </button>
            );
          })}
        </div>
      </div>

      <div className="ed-rise">{children}</div>
    </div>
  );
}

import { useState } from 'react';
import { ShadowActivity } from '../../status/ShadowActivity';
import { Calendar } from '../../status/Calendar';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';
const STAGE = '#5b9dff';

type View = 'heatmap' | 'bars';

/**
 * ActivityToggle — wrapper над existing Calendar (heatmap) и ShadowActivity (bars).
 *
 * Default: heatmap (60d, лучше для seasonality + долгий window).
 * Toggle bars: 14d bar chart placed/rejected/settled с per-day expand drill-down.
 *
 * Не переписывает existing components — просто control wrapper.
 */
export function ActivityToggle() {
  const [view, setView] = useState<View>('heatmap');

  return (
    <section
      style={{
        maxWidth: 1180,
        margin: '0 auto',
        padding: '24px 22px',
      }}
    >
      <header
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'baseline',
          marginBottom: 16,
          paddingBottom: 8,
          borderBottom: `1px solid ${RULE}`,
        }}
      >
        <div>
          <div
            className="ed-kicker"
            style={{ fontSize: 10, letterSpacing: '0.18em', color: DIM, marginBottom: 4 }}
          >
            Daily activity
          </div>
          <p style={{ color: FAINT, fontSize: 11, margin: 0 }}>
            {view === 'heatmap'
              ? 'Calendar heat-map: PnL/placed по дням за 30/60/90d'
              : 'Bar chart: placed/rejected/settled per day за 14d'}
          </p>
        </div>
        <div style={{ display: 'inline-flex', gap: 6 }}>
          <ToggleBtn active={view === 'heatmap'} onClick={() => setView('heatmap')}>
            heatmap
          </ToggleBtn>
          <ToggleBtn active={view === 'bars'} onClick={() => setView('bars')}>
            bars
          </ToggleBtn>
        </div>
      </header>

      {view === 'heatmap' ? <Calendar /> : <ShadowActivity />}
    </section>
  );
}

function ToggleBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '4px 12px',
        background: active ? `${STAGE}15` : 'transparent',
        border: `1px solid ${active ? `${STAGE}50` : RULE}`,
        color: active ? STAGE : DIM,
        fontSize: 10,
        fontFamily: 'var(--ed-mono)',
        letterSpacing: '0.06em',
        cursor: 'pointer',
        transition: 'all 150ms',
      }}
    >
      {children}
    </button>
  );
}

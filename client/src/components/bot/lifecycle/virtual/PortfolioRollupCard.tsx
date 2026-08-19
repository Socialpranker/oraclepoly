import type { Portfolio } from '../../../../hooks/useBot';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';

interface Props {
  portfolio: Portfolio;
}

/**
 * PortfolioRollupCard — sticky card в top of selected portfolio view.
 * Phase 5: bankroll bar, used/remaining $, kelly %, gate-override chips, n_detectors.
 *
 * Показывается только когда selected !== 'all' (один portfolio выбран).
 */
export function PortfolioRollupCard({ portfolio }: Props) {
  const usedPct = portfolio.exposure_cap_usd > 0
    ? (portfolio.exposure_used_usd / portfolio.exposure_cap_usd) * 100
    : 0;
  const barColor = usedPct > 90 ? DOWN : usedPct > 70 ? 'var(--ed-accent)' : UP;
  const overrideEntries = Object.entries(portfolio.gate_overrides);

  return (
    <div
      style={{
        border: `1px solid ${RULE}`,
        padding: 18,
        marginBottom: 18,
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
        gap: 18,
        background: 'var(--ed-bg-soft, transparent)',
      }}
    >
      {/* Display name + n_detectors */}
      <div>
        <div className="ed-kicker" style={{ fontSize: 9, letterSpacing: '0.18em', color: FAINT, marginBottom: 6 }}>
          Portfolio
        </div>
        <div className="ed-display" style={{ fontSize: 20, color: 'var(--ed-ink)', letterSpacing: '-0.01em' }}>
          {portfolio.display_name}
        </div>
        <div style={{ fontSize: 10, color: DIM, fontFamily: 'var(--ed-mono)', marginTop: 4 }}>
          {portfolio.n_detectors} detector{portfolio.n_detectors !== 1 ? 's' : ''}
          {!portfolio.enabled && <span style={{ color: DOWN, marginLeft: 8 }}>· DISABLED</span>}
        </div>
      </div>

      {/* Exposure bar */}
      <div style={{ minWidth: 200 }}>
        <div className="ed-kicker" style={{ fontSize: 9, letterSpacing: '0.18em', color: FAINT, marginBottom: 6 }}>
          Exposure
        </div>
        <div style={{ fontSize: 14, color: 'var(--ed-ink)', fontFamily: 'var(--ed-mono)', marginBottom: 6 }}>
          ${portfolio.exposure_used_usd.toFixed(2)} / ${portfolio.exposure_cap_usd.toFixed(0)}
        </div>
        <div style={{ height: 4, background: RULE, position: 'relative', overflow: 'hidden' }}>
          <div
            style={{
              position: 'absolute',
              inset: 0,
              width: `${Math.min(100, usedPct)}%`,
              background: barColor,
              opacity: 0.85,
              transition: 'width 250ms',
            }}
          />
        </div>
        <div style={{ fontSize: 10, color: FAINT, fontFamily: 'var(--ed-mono)', marginTop: 4 }}>
          ${portfolio.exposure_remaining_usd.toFixed(2)} headroom
        </div>
      </div>

      {/* Bankroll + kelly */}
      <div>
        <div className="ed-kicker" style={{ fontSize: 9, letterSpacing: '0.18em', color: FAINT, marginBottom: 6 }}>
          Bankroll · Kelly
        </div>
        <div style={{ fontSize: 14, color: 'var(--ed-ink)', fontFamily: 'var(--ed-mono)' }}>
          ${portfolio.bankroll_usd.toFixed(0)} · ×{portfolio.kelly_fraction.toFixed(2)}
        </div>
        <div style={{ fontSize: 10, color: FAINT, fontFamily: 'var(--ed-mono)', marginTop: 4 }}>
          fractional
        </div>
      </div>

      {/* Gate overrides */}
      <div>
        <div className="ed-kicker" style={{ fontSize: 9, letterSpacing: '0.18em', color: FAINT, marginBottom: 6 }}>
          Gate overrides
        </div>
        {overrideEntries.length === 0 ? (
          <div style={{ fontSize: 11, color: FAINT, fontFamily: 'var(--ed-mono)' }}>none</div>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 4 }}>
            {overrideEntries.map(([gate, action]) => (
              <span
                key={gate}
                title={`${gate} → ${action}`}
                style={{
                  fontSize: 9,
                  fontFamily: 'var(--ed-mono)',
                  padding: '3px 6px',
                  border: `1px solid ${RULE}`,
                  background: 'transparent',
                  color: DIM,
                  letterSpacing: '0.04em',
                }}
              >
                {gate.replace(/_/g, '-')}:{action}
              </span>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

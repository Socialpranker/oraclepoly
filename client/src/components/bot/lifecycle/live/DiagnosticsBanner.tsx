import { useState } from 'react';
import type { LiveDashboardResponse } from '../../../../hooks/useBot';
import { alertColor, alertIcon, STAGE_RED } from './utils';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const DOWN = 'var(--ed-down)';

interface Props {
  data: LiveDashboardResponse;
}

/**
 * DiagnosticsBanner — операционная диагностика live-стейджа.
 *
 * Рендерится ТОЛЬКО при проблеме: execution выключен, kill switch tripped
 * или есть critical-алерты. В норме — ничего (диагностика не должна
 * конкурировать с P&L за внимание). Разворачивается в полный список
 * алертов + технические детали (wallet source, env-флаг).
 */
export function DiagnosticsBanner({ data }: Props) {
  const [expanded, setExpanded] = useState(false);

  const { bankroll, alerts, execution_enabled } = data;
  const criticalAlerts = alerts.filter((a) => a.level === 'critical');

  const issues: string[] = [];
  if (!execution_enabled) issues.push('EXECUTION_ENABLED=false');
  if (bankroll.kill_switch_tripped) {
    issues.push(`kill switch: ${bankroll.kill_switch_reason ?? 'no reason'}`);
  }
  for (const a of criticalAlerts) issues.push(a.code);

  if (issues.length === 0) return null;

  return (
    <section style={{ maxWidth: 1180, margin: '0 auto 28px', padding: '0 22px' }}>
      <div
        style={{
          border: `1px solid ${DOWN}`,
          background: 'rgba(224,68,68,0.08)',
        }}
      >
        <button
          onClick={() => setExpanded(!expanded)}
          style={{
            display: 'flex',
            width: '100%',
            justifyContent: 'space-between',
            alignItems: 'baseline',
            gap: 14,
            padding: '12px 16px',
            background: 'none',
            border: 'none',
            cursor: 'pointer',
            textAlign: 'left',
          }}
        >
          <span
            className="ed-mono"
            style={{ fontSize: 12, color: DOWN, fontWeight: 600, letterSpacing: '0.04em' }}
          >
            ! Trading floor degraded — {issues.join(' · ')}
          </span>
          <span className="ed-mono" style={{ fontSize: 10, color: DIM, whiteSpace: 'nowrap' }}>
            {expanded ? 'hide' : 'details'} {expanded ? '▴' : '▾'}
          </span>
        </button>

        {expanded && (
          <div style={{ padding: '0 16px 14px', borderTop: `1px solid ${STAGE_RED}30` }}>
            {!execution_enabled && (
              <DetailRow label="Execution">
                EXECUTION_ENABLED=false — ордера не идут в Polymarket CLOB даже для
                promoted-детекторов. Включается в .env проды + рестарт backend.
              </DetailRow>
            )}
            {bankroll.kill_switch_tripped && (
              <DetailRow label="Kill switch">
                TRIPPED: {bankroll.kill_switch_reason ?? 'no reason recorded'}
              </DetailRow>
            )}
            <DetailRow label="Wallet source">
              {bankroll.wallet_source === 'wallet'
                ? 'live wallet (USDC on-chain)'
                : bankroll.wallet_source === 'db'
                ? 'db cache — свежий баланс кошелька недоступен'
                : 'initial fallback — ни wallet, ни db'}
            </DetailRow>
            {alerts.length > 0 && (
              <DetailRow label={`Alerts · ${alerts.length}`}>
                <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'grid', gap: 6 }}>
                  {alerts.map((a, i) => (
                    <li key={i} style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
                      <span
                        className="ed-mono"
                        style={{ color: alertColor(a.level), fontWeight: 700, minWidth: 12, textAlign: 'center' }}
                      >
                        {alertIcon(a.level)}
                      </span>
                      <span>
                        <span
                          className="ed-mono"
                          style={{ fontSize: 9, color: alertColor(a.level), letterSpacing: '0.12em', marginRight: 8 }}
                        >
                          {a.code}
                        </span>
                        {a.message}
                      </span>
                    </li>
                  ))}
                </ul>
              </DetailRow>
            )}
          </div>
        )}
      </div>
    </section>
  );
}

function DetailRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '140px 1fr', gap: 12, padding: '10px 0' }}>
      <span
        className="ed-kicker"
        style={{ fontSize: 9, letterSpacing: '0.16em', textTransform: 'uppercase', color: FAINT }}
      >
        {label}
      </span>
      <div style={{ fontSize: 11, color: 'var(--ed-ink)', lineHeight: 1.5 }}>{children}</div>
    </div>
  );
}

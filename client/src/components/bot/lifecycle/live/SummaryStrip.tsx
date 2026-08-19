import { useState } from 'react';
import type { BankrollSnapshot, FillQuality, LiveAlert } from '../../../../hooks/useBot';
import { alertColor, alertIcon, bankrollColor, fillRateColor, fmtUsd } from './utils';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';

interface Props {
  bankroll: BankrollSnapshot;
  fillQuality: FillQuality;
  alerts: LiveAlert[];
}

/**
 * SummaryStrip — тихая строка операционных чисел под equity curve.
 *
 * Заменяет три отдельные панели (Bankroll/Alerts/FillQuality): одна строка,
 * без прогресс-баров и декора. Цвет появляется только когда метрика требует
 * внимания. Алерты — счётчик с раскрытием списка (critical-алерты дополнительно
 * поднимаются в DiagnosticsBanner).
 */
export function SummaryStrip({ bankroll, fillQuality, alerts }: Props) {
  const [alertsOpen, setAlertsOpen] = useState(false);

  const fr = fillQuality.fill_rate_pct;
  const warning = alerts.filter((a) => a.level === 'warning').length;
  const critical = alerts.filter((a) => a.level === 'critical').length;
  const alertsSummary =
    alerts.length === 0
      ? '✓ none'
      : [critical > 0 ? `${critical} critical` : null, warning > 0 ? `${warning} warning` : null]
          .filter(Boolean)
          .join(' · ') || `${alerts.length} info`;

  return (
    <section style={{ maxWidth: 1180, margin: '0 auto 28px', padding: '0 22px' }}>
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: '14px 36px',
          padding: '14px 0',
          borderTop: `1px solid ${RULE}`,
          borderBottom: `1px solid ${RULE}`,
        }}
      >
        <Cell
          label="Wallet"
          value={`$${bankroll.wallet_balance_usd.toFixed(2)}`}
          sub={`of $${bankroll.initial_bankroll_usd.toFixed(0)} initial`}
        />
        <Cell
          label="Drawdown"
          value={
            bankroll.drawdown_pct !== null
              ? `${bankroll.drawdown_pct >= 0 ? '−' : '+'}${Math.abs(bankroll.drawdown_pct).toFixed(2)}%`
              : '—'
          }
          sub="kill switch at 5%"
          valueColor={bankroll.drawdown_pct !== null ? bankrollColor(bankroll.drawdown_pct) : undefined}
        />
        <Cell
          label="Fill rate · 7d"
          value={fr !== null ? `${fr.toFixed(1)}%` : 'no orders'}
          sub={fr !== null ? `${fillQuality.n_filled}/${fillQuality.n_placed} orders · gate 50%` : 'last 7d'}
          valueColor={fr !== null ? fillRateColor(fr) : undefined}
        />
        <Cell
          label="Slippage · fees"
          value={`${fillQuality.avg_slippage_usd !== null ? fmtUsd(fillQuality.avg_slippage_usd) : '—'} · ${fmtUsd(fillQuality.fee_burn_usd)}`}
          sub={
            fillQuality.fee_burn_pct_of_pnl !== null
              ? `fees ${fillQuality.fee_burn_pct_of_pnl.toFixed(1)}% of PnL`
              : 'avg slip · fee burn'
          }
        />
        <Cell
          label="Alerts"
          value={alertsSummary}
          sub={alerts.length > 0 ? (alertsOpen ? 'hide ▴' : 'show ▾') : 'operating nominally'}
          valueColor={critical > 0 ? 'var(--ed-down)' : warning > 0 ? 'var(--ed-accent)' : undefined}
          onClick={alerts.length > 0 ? () => setAlertsOpen(!alertsOpen) : undefined}
        />
      </div>

      {alertsOpen && alerts.length > 0 && (
        <ul
          style={{
            listStyle: 'none',
            margin: 0,
            padding: '12px 0',
            borderBottom: `1px solid ${RULE}`,
            display: 'grid',
            gap: 8,
          }}
        >
          {alerts.map((a, i) => (
            <li key={i} style={{ display: 'flex', gap: 10, alignItems: 'baseline', fontSize: 11 }}>
              <span
                className="ed-mono"
                style={{ color: alertColor(a.level), fontWeight: 700, minWidth: 12, textAlign: 'center' }}
              >
                {alertIcon(a.level)}
              </span>
              <span
                className="ed-mono"
                style={{ fontSize: 9, color: alertColor(a.level), letterSpacing: '0.12em' }}
              >
                {a.code}
              </span>
              <span style={{ color: 'var(--ed-ink)', lineHeight: 1.4 }}>{a.message}</span>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}

function Cell({
  label,
  value,
  sub,
  valueColor,
  onClick,
}: {
  label: string;
  value: string;
  sub: string;
  valueColor?: string;
  onClick?: () => void;
}) {
  return (
    <div
      onClick={onClick}
      style={{ cursor: onClick ? 'pointer' : 'default', minWidth: 110 }}
    >
      <div
        className="ed-kicker"
        style={{
          fontSize: 9,
          letterSpacing: '0.16em',
          textTransform: 'uppercase',
          color: FAINT,
          marginBottom: 4,
        }}
      >
        {label}
      </div>
      <div className="ed-mono" style={{ fontSize: 14, color: valueColor ?? 'var(--ed-ink)', fontWeight: 500 }}>
        {value}
      </div>
      <div className="ed-mono" style={{ fontSize: 9, color: DIM, marginTop: 3, letterSpacing: '0.04em' }}>
        {sub}
      </div>
    </div>
  );
}

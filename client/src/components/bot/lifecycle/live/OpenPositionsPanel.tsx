import type { OpenPositionsSummary } from '../../../../hooks/useBot';
import { fmtUsd, pnlColor } from './utils';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';

interface Props {
  open: OpenPositionsSummary;
}

/**
 * OpenPositionsPanel — middle section. Total count + exposure %, mini list per-position
 * (top 5) с unrealized PnL color.
 */
export function OpenPositionsPanel({ open }: Props) {
  const exposureColor =
    open.exposure_pct_of_bankroll === null
      ? FAINT
      : open.exposure_pct_of_bankroll > 25
      ? 'var(--ed-down)'
      : open.exposure_pct_of_bankroll > 10
      ? 'var(--ed-accent)'
      : 'var(--ed-up)';

  const top = open.list.slice(0, 5);

  return (
    <section
      style={{
        margin: '0 auto 28px',
        maxWidth: 1180,
        padding: '0 22px',
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'baseline',
          marginBottom: 14,
          flexWrap: 'wrap',
          gap: 10,
        }}
      >
        <div>
          <div
            className="ed-kicker"
            style={{
              fontSize: 10,
              letterSpacing: '0.22em',
              color: DIM,
              fontWeight: 600,
              textTransform: 'uppercase',
              marginBottom: 4,
            }}
          >
            Open positions · {open.count}
          </div>
          <div className="ed-display" style={{ fontSize: 13, color: DIM, fontStyle: 'italic', fontWeight: 300 }}>
            Filled, не resolved (CLOB live + virtual)
          </div>
        </div>
        <div style={{ display: 'flex', gap: 24, alignItems: 'baseline' }}>
          <Stat
            label="Exposure"
            value={fmtUsd(open.total_exposure_usd)}
            sub={
              open.exposure_pct_of_bankroll !== null
                ? `${open.exposure_pct_of_bankroll.toFixed(1)}% of bankroll`
                : '—'
            }
            color={exposureColor}
          />
          <Stat
            label="Unrealized"
            value={fmtUsd(open.unrealized_pnl_usd, { sign: true })}
            sub="naive est."
            color={pnlColor(open.unrealized_pnl_usd)}
          />
        </div>
      </div>

      {open.count === 0 ? (
        <div
          style={{
            padding: '24px 22px',
            textAlign: 'center',
            border: `1px dashed ${RULE}`,
            color: FAINT,
            fontSize: 12,
            fontStyle: 'italic',
          }}
        >
          Нет open positions. Все ордера resolved или ничего не держим.
        </div>
      ) : (
        <div style={{ border: `1px solid ${RULE}`, overflowX: 'auto', WebkitOverflowScrolling: 'touch' }}>
          <table style={{ width: '100%', minWidth: 560, borderCollapse: 'collapse', fontSize: 11 }}>
            <thead>
              <tr style={{ borderBottom: `1px solid var(--ed-rule-strong)` }}>
                <th style={th}>Detector</th>
                <th style={th}>Side</th>
                <th style={thR}>Entry</th>
                <th style={thR}>Stake</th>
                <th style={thR}>Unrealized</th>
                <th style={th}>Opened</th>
              </tr>
            </thead>
            <tbody>
              {top.map((pos, i) => (
                <tr key={i} style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}>
                  <td style={td}>
                    <span className="ed-mono" style={{ fontSize: 10 }}>{pos.detector}</span>
                  </td>
                  <td style={td}>
                    <span
                      className="ed-mono"
                      style={{
                        fontSize: 10,
                        color: pos.side === 'NO' ? 'var(--ed-down)' : 'var(--ed-up)',
                        letterSpacing: '0.06em',
                      }}
                    >
                      {pos.side}
                    </span>
                  </td>
                  <td style={tdR}>
                    {pos.entry_price !== null ? `$${pos.entry_price.toFixed(3)}` : '—'}
                  </td>
                  <td style={tdR}>{fmtUsd(pos.stake_usd)}</td>
                  <td style={{ ...tdR, color: pnlColor(pos.unrealized_pnl_usd), fontWeight: 600 }}>
                    {fmtUsd(pos.unrealized_pnl_usd, { sign: true })}
                  </td>
                  <td style={{ ...td, color: FAINT, fontSize: 10 }}>
                    {pos.opened_at ? fmtRelative(pos.opened_at) : '—'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {open.count > 5 && (
        <div
          style={{
            marginTop: 8,
            fontSize: 10,
            color: FAINT,
            fontFamily: 'var(--ed-mono)',
            textAlign: 'right',
          }}
        >
          + {open.count - 5} more positions hidden
        </div>
      )}
    </section>
  );
}

function fmtRelative(iso: string): string {
  const d = new Date(iso);
  const diffMs = Date.now() - d.getTime();
  const mins = Math.floor(diffMs / 60_000);
  if (mins < 60) return `${mins}m ago`;
  const hours = Math.floor(mins / 60);
  if (hours < 24) return `${hours}h ago`;
  return `${Math.floor(hours / 24)}d ago`;
}

function Stat({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: string;
  sub: string;
  color: string;
}) {
  return (
    <div style={{ textAlign: 'right' }}>
      <div
        className="ed-kicker"
        style={{
          fontSize: 9,
          letterSpacing: '0.16em',
          color: FAINT,
          textTransform: 'uppercase',
          marginBottom: 3,
        }}
      >
        {label}
      </div>
      <div
        className="ed-display"
        style={{
          fontSize: 18,
          fontStyle: 'italic',
          fontWeight: 400,
          color,
        }}
      >
        {value}
      </div>
      <div
        className="ed-mono"
        style={{ fontSize: 9, color: FAINT, marginTop: 2, letterSpacing: '0.04em' }}
      >
        {sub}
      </div>
    </div>
  );
}

const th: React.CSSProperties = {
  textAlign: 'left',
  padding: '8px 12px',
  fontSize: 9,
  letterSpacing: '0.14em',
  textTransform: 'uppercase',
  color: FAINT,
  fontWeight: 400,
  fontFamily: 'var(--ed-mono)',
};
const thR: React.CSSProperties = { ...th, textAlign: 'right' };
const td: React.CSSProperties = { padding: '8px 12px', color: 'var(--ed-ink)' };
const tdR: React.CSSProperties = { ...td, textAlign: 'right', fontFamily: 'var(--ed-mono)' };

import { useDetectorBuckets } from '../../../../hooks/useBot';
import type { LiveDetectorItem, DetectorBucket } from '../../../../hooks/useBot';
import { fmtUsd, pnlColor, STAGE_RED } from './utils';

const FAINT = 'var(--ed-ink-faint)';
const RULE = 'var(--ed-rule)';
const RULE_STRONG = 'var(--ed-rule-strong)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';
const VIRTUAL_GOLD = '#e0b020';

interface Props {
  item: LiveDetectorItem;
}

/**
 * LiveExpand — drill-down per-bucket realized live vs virtual.
 *
 * Showing per-(category, zone): live_pnl_7d, virtual_pnl_7d, slippage delta,
 * WR. Cell color reflects whether live is matching/beating/lagging virtual.
 */
export function LiveExpand({ item }: Props) {
  const { data: bucketsData, isLoading } = useDetectorBuckets(item.detector);
  const buckets = bucketsData?.buckets ?? [];

  return (
    <div
      style={{
        padding: '28px 28px 36px',
        background: 'rgba(224,68,68,0.04)',
        borderTop: `1px solid ${STAGE_RED}30`,
      }}
    >
      <div style={{ maxWidth: 1180, margin: '0 auto' }}>
        <div
          className="ed-kicker"
          style={{
            fontSize: 10,
            letterSpacing: '0.2em',
            color: STAGE_RED,
            marginBottom: 14,
            fontWeight: 600,
            textTransform: 'uppercase',
          }}
        >
          Per-bucket realized · live vs virtual · 7d
        </div>

        {isLoading ? (
          <div style={{ color: FAINT, fontSize: 12, padding: 20 }}>loading buckets…</div>
        ) : buckets.length === 0 ? (
          <div style={{ color: FAINT, fontSize: 12, padding: 20, fontStyle: 'italic' }}>
            Нет fills за 7d. Bucket breakdown появится когда детектор начнёт fill'ить.
          </div>
        ) : (
          <>
            <div style={{ overflowX: 'auto', WebkitOverflowScrolling: 'touch', border: `1px solid ${RULE}` }}>
              <table style={{ width: '100%', minWidth: 720, borderCollapse: 'collapse', fontSize: 11 }}>
                <thead>
                  <tr style={{ borderBottom: `1px solid ${RULE_STRONG}` }}>
                    <th style={th}>Category</th>
                    <th style={th}>Zone</th>
                    <th style={thR}>Signals</th>
                    <th style={thR}>Resolved</th>
                    <th style={thR}>Virtual PnL</th>
                    <th style={thR}>Live PnL</th>
                    <th style={thR}>Δ live − virt</th>
                    <th style={thR}>WR</th>
                  </tr>
                </thead>
                <tbody>
                  {buckets.map((b, i) => (
                    <BucketRow key={i} bucket={b} />
                  ))}
                </tbody>
              </table>
            </div>

            <div
              style={{
                marginTop: 12,
                fontSize: 10,
                color: FAINT,
                fontFamily: 'var(--ed-mono)',
                lineHeight: 1.5,
              }}
            >
              Cell color: <span style={{ color: UP }}>green = live ≥ virtual</span>
              {' · '}
              <span style={{ color: VIRTUAL_GOLD }}>gold = positive but slipping</span>
              {' · '}
              <span style={{ color: DOWN }}>red = losing money in this bucket</span>
            </div>
          </>
        )}

      </div>
    </div>
  );
}

function BucketRow({ bucket }: { bucket: DetectorBucket }) {
  const live = bucket.live_pnl_7d;
  const virt = bucket.virtual_pnl_7d;
  const delta = live - virt;

  const cellColor =
    live < 0
      ? DOWN
      : virt === 0
      ? live > 0 ? UP : FAINT
      : live >= virt * 0.95
      ? UP
      : live > 0
      ? VIRTUAL_GOLD
      : DOWN;
  const cellBg = `${cellColor}10`;
  const wr = bucket.wr_pct;

  return (
    <tr style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}>
      <td style={td}>{bucket.category}</td>
      <td style={td}>
        <span className="ed-mono" style={{ fontSize: 10 }}>
          {bucket.zone}
        </span>
      </td>
      <td style={tdR}>{bucket.n_signals_7d}</td>
      <td style={tdR}>{bucket.n_resolved_7d}</td>
      <td style={{ ...tdR, color: VIRTUAL_GOLD }}>{fmtUsd(virt, { sign: true })}</td>
      <td style={{ ...tdR, color: cellColor, fontWeight: 600, background: cellBg }}>
        {fmtUsd(live, { sign: true })}
      </td>
      <td style={{ ...tdR, color: pnlColor(delta), fontWeight: 500 }}>
        {fmtUsd(delta, { sign: true })}
      </td>
      <td style={tdR}>{wr !== null ? `${wr.toFixed(1)}%` : '—'}</td>
    </tr>
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

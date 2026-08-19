import { useDetectorBuckets } from '../../../../hooks/useBot';
import type { LiveReadinessItem, DetectorBucket } from '../../../../hooks/useBot';

const FAINT = 'var(--ed-ink-faint)';
const RULE = 'var(--ed-rule)';
const RULE_STRONG = 'var(--ed-rule-strong)';
const STAGE = '#e0b020';
const UP = 'var(--ed-up)';
const ACCENT = 'var(--ed-accent)';
const DOWN = 'var(--ed-down)';

interface Props {
  item: LiveReadinessItem;
}

/**
 * VirtualExpand — drill-down per-bucket realized vs projected P&L.
 *
 * Shows per-(category, zone) virtual_pnl_7d с baseline projected:
 *   projected = canonical_daily × 7 × bucket_share
 *   bucket_share approximated как n_bucket_signals / total_signals
 *
 * Cell color:
 *   realized > projected (×1.0+) → green (outperforming)
 *   realized > 0 but < projected → gold (working but slower)
 *   realized < 0 → red (losing money)
 *
 * Если data sparse — fallback к raw realized P&L per bucket без comparison.
 */
export function VirtualExpand({ item }: Props) {
  const { data: bucketsData, isLoading } = useDetectorBuckets(item.detector);
  const buckets = bucketsData?.buckets ?? [];
  const projected7d = item.canonical.projected_7d_usd;

  // Total signals для bucket_share approximation
  const totalSignals = buckets.reduce((s, b) => s + b.n_signals_7d, 0);

  return (
    <div
      style={{
        padding: '28px 28px 36px',
        background: `${STAGE}05`,
        borderTop: `1px solid ${STAGE}30`,
      }}
    >
      <div style={{ maxWidth: 1180, margin: '0 auto' }}>
        <SectionHeader>Per-bucket realized P&L · 7d virtual vs projected</SectionHeader>

        {isLoading ? (
          <div style={{ color: FAINT, fontSize: 12, padding: 20 }}>loading buckets…</div>
        ) : buckets.length === 0 ? (
          <div style={{ color: FAINT, fontSize: 12, padding: 20 }}>
            Нет virtual orders за последние 7 дней. Bucket breakdown появится когда детектор начнёт fill'ить.
          </div>
        ) : (
          <>
            <div style={{ overflowX: 'auto', WebkitOverflowScrolling: 'touch', border: `1px solid ${RULE}` }}>
              <table style={{ width: '100%', minWidth: 640, borderCollapse: 'collapse', fontSize: 11 }}>
                <thead>
                  <tr style={{ borderBottom: `1px solid ${RULE_STRONG}` }}>
                    <th style={th}>Category</th>
                    <th style={th}>Zone</th>
                    <th style={thR}>Signals</th>
                    <th style={thR}>Resolved</th>
                    <th style={thR}>Realized PnL</th>
                    <th style={thR}>Projected</th>
                    <th style={thR}>Δ vs proj.</th>
                    <th style={thR}>WR</th>
                  </tr>
                </thead>
                <tbody>
                  {buckets.map((b, i) => (
                    <BucketRow
                      key={i}
                      bucket={b}
                      projected7d={projected7d}
                      totalSignals={totalSignals}
                    />
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
              Cell color: <span style={{ color: UP }}>green = outperforms backtest projection</span>
              {' · '}
              <span style={{ color: ACCENT }}>gold = positive but lagging</span>
              {' · '}
              <span style={{ color: DOWN }}>red = losing money in this bucket</span>
              {projected7d == null && (
                <div style={{ marginTop: 4 }}>
                  Projected baseline недоступен (нет canonical в backtest md). Показываем raw realized PnL.
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function BucketRow({
  bucket,
  projected7d,
  totalSignals,
}: {
  bucket: DetectorBucket;
  projected7d: number | null;
  totalSignals: number;
}) {
  const realized = bucket.virtual_pnl_7d;
  const bucketShare = totalSignals > 0 ? bucket.n_signals_7d / totalSignals : 0;
  const projected = projected7d != null ? projected7d * bucketShare : null;

  const cellColor = pickCellColor(realized, projected);
  const cellBg = `${cellColor}10`;
  const wins = bucket.wr_pct;

  let delta: number | null = null;
  if (projected != null && projected !== 0) {
    delta = realized - projected;
  }

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
      <td style={{ ...tdR, color: cellColor, fontWeight: 600, background: cellBg }}>
        {realized >= 0 ? '+' : ''}${realized.toFixed(2)}
      </td>
      <td style={tdR}>
        {projected != null ? `$${projected.toFixed(2)}` : '—'}
      </td>
      <td style={{ ...tdR, color: cellColor, fontWeight: 500 }}>
        {delta != null ? `${delta >= 0 ? '+' : ''}$${delta.toFixed(2)}` : '—'}
      </td>
      <td style={tdR}>{wins != null ? `${wins.toFixed(1)}%` : '—'}</td>
    </tr>
  );
}

function pickCellColor(realized: number, projected: number | null): string {
  if (realized < 0) return DOWN;
  if (projected == null) return realized > 0 ? UP : FAINT;
  if (realized >= projected * 0.95) return UP;
  if (realized > 0) return ACCENT;
  return DOWN;
}

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="ed-kicker"
      style={{
        fontSize: 10,
        letterSpacing: '0.2em',
        color: STAGE,
        marginBottom: 14,
        fontWeight: 600,
        textTransform: 'uppercase',
      }}
    >
      {children}
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

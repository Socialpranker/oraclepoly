import { useDetectorBuckets, useDetectorBacktest } from '../../../../hooks/useBot';

const FAINT = 'var(--ed-ink-faint)';
const RULE = 'var(--ed-rule)';
const RULE_STRONG = 'var(--ed-rule-strong)';
const STAGE = '#5b9dff';
const UP = 'var(--ed-up)';
const ACCENT = 'var(--ed-accent)';
const DOWN = 'var(--ed-down)';

interface Props {
  detector: string;
}

interface CanonicalBucket {
  anchor?: string;
  zone?: string;
  WR?: number;
  wr?: number;
  n?: number;
  net_ROI?: number;
  [key: string]: string | number | undefined;
}

/**
 * PromotionExpand — drill-down per-bucket WR vs canonical.
 *
 * Shows recent (7d) WR per (category, zone) bucket compared to canonical
 * backtest (если есть docs/backtests/{det}.md). Cell color = |Δ vs canonical|:
 *   ≤5pp → green (passes Gate 2 in this bucket)
 *   ≤10pp → gold
 *   >10pp → red
 *
 * Если backtest нет — показываем raw WR per bucket без comparison.
 */
export function PromotionExpand({ detector }: Props) {
  const { data: bucketsData, isLoading: bucketsLoading } = useDetectorBuckets(detector);
  const { data: backtest } = useDetectorBacktest(detector);

  const buckets = bucketsData?.buckets ?? [];
  const canonicalRows = (backtest?.sections?.bucket_wr_table ?? []) as unknown as CanonicalBucket[];

  // Build canonical WR lookup by (anchor, zone) — варьируется формат, поэтому fuzzy match
  // Backtest table рядов формат: {anchor, zone, n, WR, net_ROI}
  // Production buckets формат: {category, zone, n_signals_7d, n_resolved_7d, wr_pct, ...}
  // Сравнение по zone (anchor у нас не известен per recent signal)
  const canonicalByZone = new Map<string, number>();
  for (const row of canonicalRows) {
    const zone = String(row.zone ?? '').trim();
    const wr = typeof row.WR === 'number' ? row.WR : typeof row.wr === 'number' ? row.wr : null;
    if (zone && wr != null) {
      // Если несколько anchor для одной zone — усредняем
      const existing = canonicalByZone.get(zone);
      canonicalByZone.set(zone, existing != null ? (existing + wr) / 2 : wr);
    }
  }

  return (
    <div
      style={{
        padding: '28px 28px 36px',
        background: `${STAGE}05`,
        borderTop: `1px solid ${STAGE}30`,
      }}
    >
      <div style={{ maxWidth: 1180, margin: '0 auto' }}>
        <SectionHeader>Per-bucket Win Rate · 7d shadow vs canonical</SectionHeader>

        {bucketsLoading ? (
          <div style={{ color: FAINT, fontSize: 12, padding: 20 }}>loading buckets…</div>
        ) : buckets.length === 0 ? (
          <div style={{ color: FAINT, fontSize: 12, padding: 20 }}>
            Нет signals за последние 7 дней. Bucket breakdown появится когда детектор начнёт эмитить.
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
                    <th style={thR}>Recent WR</th>
                    <th style={thR}>Canonical WR</th>
                    <th style={thR}>Δ</th>
                    <th style={thR}>ROI net</th>
                  </tr>
                </thead>
                <tbody>
                  {buckets.map((b, i) => {
                    const canonicalWr = canonicalByZone.get(b.zone) ?? null;
                    const delta =
                      canonicalWr != null && b.wr_pct != null ? b.wr_pct - canonicalWr : null;
                    const deltaColor = pickDeltaColor(delta);
                    const cellBg = delta != null ? `${deltaColor}10` : 'transparent';
                    return (
                      <tr key={i} style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}>
                        <td style={td}>{b.category}</td>
                        <td style={td}>
                          <span className="ed-mono" style={{ fontSize: 10 }}>
                            {b.zone}
                          </span>
                        </td>
                        <td style={tdR}>{b.n_signals_7d}</td>
                        <td style={tdR}>{b.n_resolved_7d}</td>
                        <td style={{ ...tdR, background: cellBg, color: deltaColor, fontWeight: 600 }}>
                          {b.wr_pct != null ? `${b.wr_pct.toFixed(1)}%` : '—'}
                        </td>
                        <td style={tdR}>
                          {canonicalWr != null ? `${canonicalWr.toFixed(1)}%` : '—'}
                        </td>
                        <td style={{ ...tdR, color: deltaColor, fontWeight: 500 }}>
                          {delta != null
                            ? `${delta >= 0 ? '+' : ''}${delta.toFixed(1)}pp`
                            : '—'}
                        </td>
                        <td
                          style={{
                            ...tdR,
                            color:
                              b.roi_net_pct == null
                                ? FAINT
                                : b.roi_net_pct > 0
                                ? UP
                                : DOWN,
                          }}
                        >
                          {b.roi_net_pct != null
                            ? `${b.roi_net_pct >= 0 ? '+' : ''}${b.roi_net_pct.toFixed(1)}%`
                            : '—'}
                        </td>
                      </tr>
                    );
                  })}
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
              <div>
                Cell color: <span style={{ color: UP }}>green = ±5pp от canonical (Gate 2 PASS)</span>
                {' · '}
                <span style={{ color: ACCENT }}>gold = ±10pp</span>
                {' · '}
                <span style={{ color: DOWN }}>red = &gt;10pp drift</span>
              </div>
              {canonicalByZone.size === 0 && (
                <div style={{ marginTop: 4, color: FAINT }}>
                  Canonical comparison недоступен (нет bucket_wr_table в backtest md или не совпадает zone formatting).
                </div>
              )}
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function pickDeltaColor(delta: number | null): string {
  if (delta == null) return FAINT;
  const abs = Math.abs(delta);
  if (abs <= 5) return UP;
  if (abs <= 10) return ACCENT;
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
const td: React.CSSProperties = {
  padding: '8px 12px',
  color: 'var(--ed-ink)',
};
const tdR: React.CSSProperties = { ...td, textAlign: 'right', fontFamily: 'var(--ed-mono)' };

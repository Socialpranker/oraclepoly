import { useDetectorBacktest } from '../../../../hooks/useBot';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';
const RULE_STRONG = 'var(--ed-rule-strong)';
const STAGE = '#9b6cff';

interface Props {
  detector: string;
}

/**
 * BacktestExpand — раскрытая панель с full content backtest md.
 *
 * 4 секции (если данные есть):
 *   1. Hypothesis — большой читабельный параграф
 *   2. BUCKET_WR heatmap — anchor × zone matrix с color-coded WR cells
 *   3. Filters & promotion criteria — 2 column lists
 *   4. Honest haircut reason — quote-style блок
 *
 * Источник: useDetectorBacktest(detector) → /v2/detectors/{det}/backtest.sections
 */
export function BacktestExpand({ detector }: Props) {
  const { data, isLoading } = useDetectorBacktest(detector);

  if (isLoading) {
    return (
      <div style={{ padding: '32px 28px', color: FAINT, fontSize: 12 }}>
        Loading backtest details for {detector}…
      </div>
    );
  }

  if (!data || !data.available) {
    return (
      <div style={{ padding: '32px 28px', color: FAINT, fontSize: 12 }}>
        Backtest document not available for {detector}.
      </div>
    );
  }

  const sections = data.sections ?? {};

  return (
    <div
      style={{
        padding: '28px 28px 36px',
        background: 'rgba(155,108,255,0.04)',
        borderTop: `1px solid ${STAGE}30`,
      }}
    >
      <div
        style={{
          maxWidth: 1180,
          margin: '0 auto',
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 2fr) minmax(0, 1fr)',
          gap: 36,
        }}
      >
        {/* LEFT — main content */}
        <div>
          {sections.hypothesis && <HypothesisBlock text={sections.hypothesis} />}
          {sections.bucket_wr_table && (
            <BucketWRHeatmap rows={sections.bucket_wr_table} />
          )}
          {sections.category_breakdown && (
            <CategoryTable rows={sections.category_breakdown} />
          )}
        </div>

        {/* RIGHT — sidebar */}
        <div>
          {sections.filters && <FiltersList items={sections.filters} />}
          {sections.promotion_criteria && (
            <PromotionCriteria criteria={sections.promotion_criteria} />
          )}
          {data.honest_haircut_reason && (
            <HaircutReason reason={data.honest_haircut_reason} factor={data.honest_haircut_factor} />
          )}
        </div>
      </div>
    </div>
  );
}

function SectionHeader({ children }: { children: React.ReactNode }) {
  return (
    <div
      className="ed-kicker"
      style={{
        fontSize: 10,
        letterSpacing: '0.2em',
        color: STAGE,
        marginBottom: 10,
        fontWeight: 600,
        textTransform: 'uppercase',
      }}
    >
      {children}
    </div>
  );
}

function HypothesisBlock({ text }: { text: string }) {
  return (
    <section style={{ marginBottom: 32 }}>
      <SectionHeader>Hypothesis</SectionHeader>
      <p
        style={{
          fontSize: 14,
          lineHeight: 1.65,
          color: 'var(--ed-ink)',
          maxWidth: '65ch',
          margin: 0,
          fontFamily: 'var(--ed-body)',
        }}
      >
        {text}
      </p>
    </section>
  );
}

function BucketWRHeatmap({ rows }: { rows: Array<Record<string, string | number>> }) {
  if (rows.length === 0) return null;

  // Find numeric WR column for color scale
  const wrKey = Object.keys(rows[0]).find((k) => k.toLowerCase().includes('wr')) ?? 'WR';
  const roiKey = Object.keys(rows[0]).find((k) => k.toLowerCase().includes('roi')) ?? null;
  const wrValues = rows
    .map((r) => Number(r[wrKey]))
    .filter((v) => !Number.isNaN(v));
  const wrMin = Math.min(...wrValues);
  const wrMax = Math.max(...wrValues);

  const cols = Object.keys(rows[0]);

  return (
    <section style={{ marginBottom: 32 }}>
      <SectionHeader>Per-bucket Win Rate</SectionHeader>
      <div style={{ overflowX: 'auto', WebkitOverflowScrolling: 'touch', border: `1px solid ${RULE}` }}>
        <table style={{ width: '100%', minWidth: 600, borderCollapse: 'collapse', fontSize: 11 }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${RULE_STRONG}` }}>
              {cols.map((c) => (
                <th
                  key={c}
                  style={{
                    padding: '8px 12px',
                    textAlign: typeof rows[0][c] === 'number' ? 'right' : 'left',
                    fontSize: 9,
                    letterSpacing: '0.14em',
                    textTransform: 'uppercase',
                    color: FAINT,
                    fontWeight: 400,
                    fontFamily: 'var(--ed-mono)',
                  }}
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => {
              const wrVal = Number(row[wrKey]);
              const wrFrac = wrMax > wrMin ? (wrVal - wrMin) / (wrMax - wrMin) : 0.5;
              const wrBg = wrColor(wrFrac);
              return (
                <tr key={i} style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}>
                  {cols.map((c) => {
                    const val = row[c];
                    const isNumeric = typeof val === 'number';
                    const isWr = c === wrKey;
                    const isRoi = c === roiKey;
                    return (
                      <td
                        key={c}
                        style={{
                          padding: '8px 12px',
                          textAlign: isNumeric ? 'right' : 'left',
                          fontFamily: isNumeric ? 'var(--ed-mono)' : 'var(--ed-body)',
                          color: isWr
                            ? wrBg
                            : isRoi && typeof val === 'number'
                            ? val > 0
                              ? 'var(--ed-up)'
                              : 'var(--ed-down)'
                            : 'var(--ed-ink)',
                          background: isWr ? `${wrBg}10` : 'transparent',
                          fontWeight: isWr ? 600 : 400,
                        }}
                      >
                        {isNumeric
                          ? isRoi
                            ? `${(val as number) > 0 ? '+' : ''}${(val as number).toFixed(1)}%`
                            : isWr
                            ? `${(val as number).toFixed(1)}%`
                            : (val as number).toString()
                          : String(val)}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <div style={{ marginTop: 6, fontSize: 9, color: FAINT, fontFamily: 'var(--ed-mono)' }}>
        WR colored on {wrMin.toFixed(1)}% — {wrMax.toFixed(1)}% scale
      </div>
    </section>
  );
}

function wrColor(frac: number): string {
  // Frac 0..1 → red..green via gold midpoint
  if (frac < 0.33) return 'var(--ed-down)';
  if (frac < 0.66) return 'var(--ed-accent)';
  return 'var(--ed-up)';
}

function CategoryTable({ rows }: { rows: Array<Record<string, string | number>> }) {
  if (rows.length === 0) return null;
  const cols = Object.keys(rows[0]);

  return (
    <section style={{ marginBottom: 32 }}>
      <SectionHeader>Category breakdown</SectionHeader>
      <div style={{ overflowX: 'auto', WebkitOverflowScrolling: 'touch', border: `1px solid ${RULE}` }}>
        <table style={{ width: '100%', minWidth: 600, borderCollapse: 'collapse', fontSize: 11 }}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${RULE_STRONG}` }}>
              {cols.map((c) => (
                <th
                  key={c}
                  style={{
                    padding: '8px 12px',
                    textAlign: typeof rows[0][c] === 'number' ? 'right' : 'left',
                    fontSize: 9,
                    letterSpacing: '0.14em',
                    textTransform: 'uppercase',
                    color: FAINT,
                    fontWeight: 400,
                  }}
                >
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((row, i) => (
              <tr key={i} style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}>
                {cols.map((c) => (
                  <td
                    key={c}
                    style={{
                      padding: '6px 12px',
                      fontSize: 11,
                      textAlign: typeof row[c] === 'number' ? 'right' : 'left',
                      fontFamily: typeof row[c] === 'number' ? 'var(--ed-mono)' : 'var(--ed-body)',
                      color: 'var(--ed-ink)',
                    }}
                  >
                    {typeof row[c] === 'number' ? (row[c] as number).toFixed(1) : String(row[c])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function FiltersList({ items }: { items: Array<{ key: string; value: string }> }) {
  return (
    <section style={{ marginBottom: 24 }}>
      <SectionHeader>Filters</SectionHeader>
      <ul style={{ listStyle: 'none', padding: 0, margin: 0, fontSize: 12 }}>
        {items.map((f, i) => (
          <li
            key={i}
            style={{
              padding: '6px 0',
              borderBottom: `1px dashed ${RULE}`,
              display: 'flex',
              gap: 10,
              alignItems: 'baseline',
            }}
          >
            <span style={{ color: FAINT, fontFamily: 'var(--ed-mono)', fontSize: 10, minWidth: 80 }}>
              {f.key}
            </span>
            <span style={{ color: DIM, lineHeight: 1.5 }}>{f.value}</span>
          </li>
        ))}
      </ul>
    </section>
  );
}

function PromotionCriteria({ criteria }: { criteria: Record<string, string> }) {
  const labels: Record<string, string> = {
    shadow_to_virtual: 'Shadow → Virtual',
    virtual_to_live: 'Virtual → Live',
  };
  return (
    <section style={{ marginBottom: 24 }}>
      <SectionHeader>Promotion criteria</SectionHeader>
      <div style={{ fontSize: 12 }}>
        {Object.entries(criteria).map(([key, value]) => (
          <div key={key} style={{ marginBottom: 14 }}>
            <div
              className="ed-mono"
              style={{
                fontSize: 10,
                color: STAGE,
                marginBottom: 4,
                letterSpacing: '0.08em',
              }}
            >
              {labels[key] ?? key}
            </div>
            <div style={{ color: DIM, lineHeight: 1.5 }}>{value}</div>
          </div>
        ))}
      </div>
    </section>
  );
}

function HaircutReason({ reason, factor }: { reason: string; factor?: number }) {
  return (
    <section
      style={{
        padding: '14px 16px',
        border: `1px solid var(--ed-accent)40`,
        background: 'var(--ed-accent-soft)',
        marginTop: 8,
      }}
    >
      <div
        className="ed-kicker"
        style={{
          fontSize: 9,
          letterSpacing: '0.16em',
          color: 'var(--ed-accent)',
          marginBottom: 6,
        }}
      >
        ⚠ Honest haircut {factor != null && `×${factor.toFixed(2)}`}
      </div>
      <div style={{ fontSize: 11, color: DIM, lineHeight: 1.55 }}>{reason}</div>
    </section>
  );
}

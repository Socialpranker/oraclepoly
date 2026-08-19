import { useState } from 'react';
import {
  useAutoEdgeDiscoveries,
  useAutoEdgeScans,
  useAutoEdgeDismiss,
  useAutoEdgePromote,
  type AutoEdgeDiscovery,
  type AutoEdgeStatus,
} from '../../../hooks/useBot';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';

const STATUS_FILTERS: Array<{ key: string; label: string; statuses: AutoEdgeStatus[] }> = [
  { key: 'novel', label: 'Novel', statuses: ['novel'] },
  { key: 'partial', label: 'Partial', statuses: ['partial_overlap'] },
  { key: 'subset', label: 'Subset', statuses: ['subset'] },
  { key: 'promoted', label: 'Promoted', statuses: ['promoted'] },
  { key: 'all', label: 'All', statuses: [] },
];

const fmtPctSigned = (v: number) => `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%`;
const fmtNum = (v: number) => v.toLocaleString();
const fmtMoney = (v: number) => `${v >= 0 ? '+' : ''}$${v.toFixed(2)}`;
const fmtPVal = (v: number | null) => (v === null ? '—' : v < 0.001 ? v.toExponential(1) : v.toFixed(3));

function zoneShort(d: AutoEdgeDiscovery): string {
  return `${d.zone} [${d.zone_lo.toFixed(2)}, ${d.zone_hi.toFixed(2)})`;
}

function statusColor(s: AutoEdgeStatus): string {
  if (s === 'novel') return UP;
  if (s === 'subset') return FAINT;
  if (s === 'partial_overlap') return 'var(--ed-warn, #f59e0b)';
  if (s === 'promoted') return 'var(--ed-accent, #3b82f6)';
  if (s === 'dismissed') return DIM;
  return DIM;
}

function verdictColor(v: 'PASS' | 'MARGINAL' | 'FAIL'): string {
  if (v === 'PASS') return UP;
  if (v === 'MARGINAL') return 'var(--ed-warn, #f59e0b)';
  return DOWN;
}

const td: React.CSSProperties = {
  padding: '8px 10px',
  fontSize: '0.78rem',
  borderBottom: `1px solid ${FAINT}`,
  verticalAlign: 'top',
};
const tdRight: React.CSSProperties = { ...td, textAlign: 'right' };
const th: React.CSSProperties = {
  padding: '8px 10px',
  fontSize: '0.7rem',
  textTransform: 'uppercase',
  letterSpacing: '0.05em',
  color: DIM,
  textAlign: 'left',
  borderBottom: `1px solid ${FAINT}`,
};
const thRight: React.CSSProperties = { ...th, textAlign: 'right' };

function DiscoveryDrillDown({ d }: { d: AutoEdgeDiscovery }) {
  return (
    <tr>
      <td colSpan={9} style={{ padding: 0 }}>
        <div style={{ padding: '12px 16px', background: 'rgba(255,255,255,0.02)', fontSize: '0.78rem' }}>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(2, minmax(0, 1fr))', gap: 12 }}>
            <div>
              <div style={{ color: DIM, marginBottom: 4 }}>Cell</div>
              <code style={{ color: 'var(--ed-ink, #fff)' }}>
                {d.feature} {d.threshold_op} {d.threshold_value.toPrecision(4)}
              </code>
              <div style={{ color: DIM, marginTop: 8 }}>
                Zone {zoneShort(d)} · Anchor {d.anchor_hours}h · Cat {d.category ?? 'ALL'} · Side {d.side}
              </div>
            </div>
            <div>
              <div style={{ color: DIM, marginBottom: 4 }}>Year split</div>
              <div>
                2025: {d.year_2025_net_roi !== null ? fmtPctSigned(d.year_2025_net_roi) : '—'} ·{' '}
                2026: {d.year_2026_net_roi !== null ? fmtPctSigned(d.year_2026_net_roi) : '—'}{' '}
                {d.year_consistency_score !== null && (
                  <span style={{ color: DIM }}>(consistency {d.year_consistency_score.toFixed(2)})</span>
                )}
              </div>
              <div style={{ color: DIM, marginTop: 8 }}>
                p-value {fmtPVal(d.p_value)} · BH q-value {fmtPVal(d.bh_adjusted_p)}
              </div>
            </div>
          </div>
          <div style={{ marginTop: 12, color: DIM }}>
            n={fmtNum(d.n_signals)} signals across {fmtNum(d.n_unique_markets)} markets · avg_cost ${d.avg_cost.toFixed(4)} ·
            mirror diff {d.mirror_diff_pp >= 0 ? '+' : ''}{d.mirror_diff_pp.toFixed(1)}pp · overlap_max {(d.overlap_pct_max * 100).toFixed(0)}% ·
            seen {d.n_times_seen}× since {d.first_seen_scan_date}
          </div>
          <div style={{ marginTop: 8, color: DIM }}>
            Honest @5K: {fmtMoney(d.daily_honest_pnl_at_5k)}/d · raw: {fmtMoney(d.daily_raw_pnl_at_5k)}/d ·{' '}
            {d.daily_signals.toFixed(1)} signals/d
          </div>
          {d.promoted_to && (
            <div style={{ marginTop: 8, color: 'var(--ed-accent, #3b82f6)' }}>
              ✓ Promoted to <strong>{d.promoted_to}</strong>
            </div>
          )}
        </div>
      </td>
    </tr>
  );
}

function DiscoveryRow({ d }: { d: AutoEdgeDiscovery }) {
  const [expanded, setExpanded] = useState(false);
  const dismiss = useAutoEdgeDismiss();
  const promote = useAutoEdgePromote();

  return (
    <>
      <tr
        onClick={() => setExpanded((e) => !e)}
        style={{ cursor: 'pointer', background: expanded ? 'rgba(255,255,255,0.03)' : 'transparent' }}
      >
        <td style={{ ...td, width: 24, color: DIM }}>{expanded ? '▼' : '▶'}</td>
        <td style={td}>
          <code style={{ fontSize: '0.72rem' }}>{d.feature}</code>
          <span style={{ color: DIM, marginLeft: 4 }}>
            {d.threshold_op}
            {d.threshold_value.toPrecision(3)}
          </span>
        </td>
        <td style={td}>{d.zone}</td>
        <td style={tdRight}>{d.anchor_hours}h</td>
        <td style={td}>{d.category ?? 'ALL'}</td>
        <td style={td}>{d.side}</td>
        <td style={tdRight}>{fmtNum(d.n_signals)}</td>
        <td style={{ ...tdRight, color: d.net_roi >= 0 ? UP : DOWN, fontWeight: 600 }}>
          {fmtPctSigned(d.net_roi)}
        </td>
        <td style={tdRight}>{d.rank_score.toFixed(2)}</td>
      </tr>
      {expanded && <DiscoveryDrillDown d={d} />}
      {expanded && (
        <tr>
          <td colSpan={9} style={{ padding: '0 16px 12px', borderBottom: `1px solid ${FAINT}` }}>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <span
                style={{
                  padding: '2px 8px',
                  fontSize: '0.7rem',
                  borderRadius: 4,
                  background: 'rgba(255,255,255,0.05)',
                  color: statusColor(d.status),
                  textTransform: 'uppercase',
                  letterSpacing: '0.05em',
                }}
              >
                {d.status}
              </span>
              <span
                style={{
                  padding: '2px 8px',
                  fontSize: '0.7rem',
                  borderRadius: 4,
                  background: 'rgba(255,255,255,0.05)',
                  color: verdictColor(d.verdict),
                  textTransform: 'uppercase',
                }}
              >
                {d.verdict}
              </span>
              {d.status !== 'dismissed' && d.status !== 'promoted' && (
                <button
                  type="button"
                  disabled={dismiss.isPending}
                  onClick={(e) => {
                    e.stopPropagation();
                    if (window.confirm(`Dismiss discovery #${d.id}?`)) dismiss.mutate(d.id);
                  }}
                  style={{
                    padding: '4px 10px',
                    fontSize: '0.7rem',
                    background: 'transparent',
                    color: DIM,
                    border: `1px solid ${FAINT}`,
                    borderRadius: 4,
                    cursor: 'pointer',
                  }}
                >
                  Dismiss
                </button>
              )}
              {d.status === 'novel' && (
                <button
                  type="button"
                  disabled={promote.isPending}
                  onClick={(e) => {
                    e.stopPropagation();
                    const name = window.prompt(
                      'Promote to detector slug (e.g. D15_smart_money_taker_high):',
                    );
                    if (name && name.trim()) {
                      promote.mutate({ id: d.id, detectorName: name.trim() });
                    }
                  }}
                  style={{
                    padding: '4px 10px',
                    fontSize: '0.7rem',
                    background: 'var(--ed-accent, #3b82f6)',
                    color: '#fff',
                    border: 'none',
                    borderRadius: 4,
                    cursor: 'pointer',
                  }}
                >
                  Promote →
                </button>
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

export function AutoEdgeDashboard() {
  const [filter, setFilter] = useState<string>('novel');
  const activeFilter = STATUS_FILTERS.find((f) => f.key === filter) ?? STATUS_FILTERS[0];
  const statusParam = activeFilter.statuses.length === 0 ? undefined : activeFilter.statuses.join(',');

  const { data: discoveriesData, isLoading } = useAutoEdgeDiscoveries(statusParam, 100);
  const { data: scansData } = useAutoEdgeScans(8);

  const discoveries = discoveriesData?.discoveries ?? [];
  const scans = scansData?.scans ?? [];
  const lastScan = scans[0];

  return (
    <div style={{ padding: '16px', maxWidth: 1400, margin: '0 auto' }}>
      <div style={{ marginBottom: 16 }}>
        <h1 style={{ fontSize: '1.25rem', margin: 0, color: 'var(--ed-ink, #fff)' }}>
          🔬 Auto Edge Discoveries
        </h1>
        <p style={{ color: DIM, fontSize: '0.78rem', margin: '4px 0 0' }}>
          Weekly grid sweep on canonical ml_features_v2.parquet (Sun 03:00 UTC). BH FDR α=0.05.
        </p>
      </div>

      {lastScan && (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
            gap: 12,
            padding: '12px',
            border: `1px solid ${FAINT}`,
            borderRadius: 8,
            marginBottom: 16,
            fontSize: '0.78rem',
          }}
        >
          <div>
            <div style={{ color: DIM, fontSize: '0.7rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Last scan
            </div>
            <div style={{ fontWeight: 600, marginTop: 2 }}>{lastScan.scan_date}</div>
          </div>
          <div>
            <div style={{ color: DIM, fontSize: '0.7rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Findings
            </div>
            <div style={{ fontWeight: 600, marginTop: 2 }}>{lastScan.n_findings}</div>
          </div>
          <div>
            <div style={{ color: DIM, fontSize: '0.7rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Novel
            </div>
            <div style={{ fontWeight: 600, marginTop: 2, color: UP }}>{lastScan.n_novel}</div>
          </div>
          <div>
            <div style={{ color: DIM, fontSize: '0.7rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Subset
            </div>
            <div style={{ fontWeight: 600, marginTop: 2, color: FAINT }}>{lastScan.n_subset}</div>
          </div>
          <div>
            <div style={{ color: DIM, fontSize: '0.7rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Notified
            </div>
            <div style={{ fontWeight: 600, marginTop: 2 }}>{lastScan.n_notified}</div>
          </div>
          <div>
            <div style={{ color: DIM, fontSize: '0.7rem', textTransform: 'uppercase', letterSpacing: '0.05em' }}>
              Top rank
            </div>
            <div style={{ fontWeight: 600, marginTop: 2 }}>
              {lastScan.max_rank_score?.toFixed(2) ?? '—'}
            </div>
          </div>
        </div>
      )}

      <div style={{ display: 'flex', gap: 4, marginBottom: 12 }}>
        {STATUS_FILTERS.map((f) => (
          <button
            type="button"
            key={f.key}
            onClick={() => setFilter(f.key)}
            style={{
              padding: '6px 14px',
              fontSize: '0.78rem',
              background: filter === f.key ? 'var(--ed-accent, #3b82f6)' : 'transparent',
              color: filter === f.key ? '#fff' : DIM,
              border: `1px solid ${filter === f.key ? 'var(--ed-accent, #3b82f6)' : FAINT}`,
              borderRadius: 4,
              cursor: 'pointer',
            }}
          >
            {f.label}
          </button>
        ))}
      </div>

      <div style={{ overflowX: 'auto', border: `1px solid ${FAINT}`, borderRadius: 8 }}>
        <table style={{ width: '100%', borderCollapse: 'collapse' }}>
          <thead>
            <tr>
              <th style={{ ...th, width: 24 }} />
              <th style={th}>Feature</th>
              <th style={th}>Zone</th>
              <th style={thRight}>Anchor</th>
              <th style={th}>Category</th>
              <th style={th}>Side</th>
              <th style={thRight}>n</th>
              <th style={thRight}>net ROI</th>
              <th style={thRight}>rank</th>
            </tr>
          </thead>
          <tbody>
            {isLoading && (
              <tr>
                <td colSpan={9} style={{ ...td, textAlign: 'center', color: DIM, padding: 40 }}>
                  Loading…
                </td>
              </tr>
            )}
            {!isLoading && discoveries.length === 0 && (
              <tr>
                <td colSpan={9} style={{ ...td, textAlign: 'center', color: DIM, padding: 40 }}>
                  No discoveries match this filter.
                </td>
              </tr>
            )}
            {discoveries.map((d) => (
              <DiscoveryRow key={d.id} d={d} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

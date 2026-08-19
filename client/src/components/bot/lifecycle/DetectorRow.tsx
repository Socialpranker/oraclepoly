import { useState } from 'react';
import { useDetectorBuckets } from '../../../hooks/useBot';
import type { Stage, DetectorBucket } from '../../../hooks/useBot';
import StageBadge from '../StageBadge';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';

export interface DetectorRowMetric {
  label: string;
  value: string;
  color?: string;
  // Какое поле bucket'а использовать для drill-down правой колонки
  bucketField: 'wr_pct' | 'roi_net_pct' | 'virtual_pnl_7d' | 'live_pnl_7d' | 'n_signals_7d';
  bucketFormat?: (v: number | null) => string;
}

interface Props {
  detector: string;
  stage: Stage;
  metric: DetectorRowMetric;
  // Колонка-описание (необязательно — например "n_signals_7d")
  secondary?: { label: string; value: string };
  // Если drill-down не нужен (для пустых детекторов / не в stage) — отключить
  disableDrillDown?: boolean;
}

/**
 * DetectorRow — строка таблицы с inline expand/collapse drill-down.
 *
 * Клик раскрывает sub-table per-bucket (category × zone) — ленивая загрузка
 * через useDetectorBuckets(detector). См. /v2/detectors/{det}/buckets API.
 */
export function DetectorRow({
  detector,
  stage,
  metric,
  secondary,
  disableDrillDown = false,
}: Props) {
  const [expanded, setExpanded] = useState(false);
  const { data, isLoading } = useDetectorBuckets(expanded ? detector : null);

  const buckets = data?.buckets ?? [];

  return (
    <>
      <tr
        onClick={() => !disableDrillDown && setExpanded((e) => !e)}
        style={{
          borderBottom: '1px solid rgba(255,255,255,0.04)',
          cursor: disableDrillDown ? 'default' : 'pointer',
          background: expanded ? 'rgba(255,255,255,0.02)' : 'transparent',
        }}
      >
        <td style={{ padding: '10px 8px 10px 14px', width: 24, color: FAINT, fontSize: 11 }}>
          {disableDrillDown ? '' : expanded ? '▼' : '▶'}
        </td>
        <td style={td}>
          <span style={{ fontWeight: 500 }}>{detector}</span>
        </td>
        <td style={td}>
          <StageBadge stage={stage} size="xs" />
        </td>
        {secondary && (
          <td style={tdRight}>
            <span style={{ color: DIM, fontFamily: 'var(--ed-mono)', fontSize: 11 }}>
              {secondary.value}
            </span>
          </td>
        )}
        <td style={tdRight}>
          <span style={{ color: metric.color ?? 'var(--ed-ink)' }}>{metric.value}</span>
        </td>
      </tr>
      {expanded && (
        <tr>
          <td colSpan={secondary ? 5 : 4} style={{ padding: 0, background: 'rgba(0,0,0,0.18)' }}>
            <div style={{ padding: '12px 22px 16px 36px' }}>
              {isLoading ? (
                <div style={{ color: FAINT, fontSize: 11 }}>загрузка bucket'ов…</div>
              ) : buckets.length === 0 ? (
                <div style={{ color: FAINT, fontSize: 11 }}>
                  Нет signals за 7 дней. Bucket breakdown появится когда детектор начнёт эмитить.
                </div>
              ) : (
                <BucketTable buckets={buckets} highlightField={metric.bucketField} />
              )}
            </div>
          </td>
        </tr>
      )}
    </>
  );
}

function BucketTable({
  buckets,
  highlightField,
}: {
  buckets: DetectorBucket[];
  highlightField: DetectorRowMetric['bucketField'];
}) {
  return (
    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 11 }}>
      <thead>
        <tr style={{ borderBottom: `1px solid ${FAINT}` }}>
          <th style={subTh}>Category</th>
          <th style={subTh}>Zone</th>
          <th style={subThR}>Signals 7d</th>
          <th style={subThR}>Resolved</th>
          <th style={subThR}>WR%</th>
          <th style={subThR}>ROI net%</th>
          <th style={subThR}>Virtual $</th>
          <th style={subThR}>Live $</th>
        </tr>
      </thead>
      <tbody>
        {buckets.map((b) => (
          <tr
            key={`${b.category}-${b.zone}`}
            style={{ borderBottom: '1px solid rgba(255,255,255,0.03)' }}
          >
            <td style={subTd}>{b.category}</td>
            <td style={subTd}>
              <span className="ed-mono" style={{ fontSize: 10 }}>
                {b.zone}
              </span>
            </td>
            <td style={subTdR}>{b.n_signals_7d}</td>
            <td style={subTdR}>{b.n_resolved_7d}</td>
            <td style={subTdR}>{fmtPct(b.wr_pct)}</td>
            <td style={subTdR}>
              <span
                style={{
                  color:
                    b.roi_net_pct === null
                      ? FAINT
                      : b.roi_net_pct > 0
                      ? UP
                      : b.roi_net_pct < 0
                      ? DOWN
                      : DIM,
                  fontWeight: highlightField === 'roi_net_pct' ? 500 : 400,
                }}
              >
                {b.roi_net_pct !== null ? `${b.roi_net_pct >= 0 ? '+' : ''}${b.roi_net_pct.toFixed(1)}%` : '—'}
              </span>
            </td>
            <td style={subTdR}>
              <span style={{ color: b.virtual_pnl_7d > 0 ? UP : b.virtual_pnl_7d < 0 ? DOWN : FAINT }}>
                ${b.virtual_pnl_7d.toFixed(2)}
              </span>
            </td>
            <td style={subTdR}>
              <span style={{ color: b.live_pnl_7d > 0 ? UP : b.live_pnl_7d < 0 ? DOWN : FAINT }}>
                ${b.live_pnl_7d.toFixed(2)}
              </span>
            </td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function fmtPct(v: number | null): string {
  if (v === null) return '—';
  return `${v.toFixed(1)}%`;
}

const td: React.CSSProperties = { padding: '10px 8px', color: 'var(--ed-ink)', fontSize: 12 };
const tdRight: React.CSSProperties = { ...td, textAlign: 'right' };
const subTh: React.CSSProperties = {
  textAlign: 'left',
  padding: '6px 8px',
  color: FAINT,
  fontWeight: 400,
  fontSize: 9,
  letterSpacing: '0.12em',
  textTransform: 'uppercase',
};
const subThR: React.CSSProperties = { ...subTh, textAlign: 'right' };
const subTd: React.CSSProperties = { padding: '6px 8px', color: 'var(--ed-ink)' };
const subTdR: React.CSSProperties = { ...subTd, textAlign: 'right', fontFamily: 'var(--ed-mono)' };

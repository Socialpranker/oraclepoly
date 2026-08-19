import type { LiveMetricStrategy } from '../../../hooks/useBot';

interface Props {
  liveMetric?: LiveMetricStrategy;
  mode: 'dry' | 'live';
}

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return '—';
  const d = new Date(iso);
  const diff = Date.now() - d.getTime();
  if (Number.isNaN(diff)) return '—';
  const m = Math.floor(diff / 60_000);
  if (m < 1) return 'just now';
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

export function DetailFooter({ liveMetric, mode }: Props) {
  const editionLine = mode === 'live' ? 'Oracle Live Edition' : 'Oracle Paper Edition';
  const lastRecompute = liveMetric ? timeAgo(liveMetric.last_recomputed_at) : null;
  const lastSettled = liveMetric ? timeAgo(liveMetric.last_settled_at) : null;

  return (
    <footer className="px-5 pt-6 pb-10">
      <div className="flex items-center gap-2 mb-3">
        <span className="ed-kicker">Colophon</span>
        <hr className="ed-rule flex-1" />
      </div>
      <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1.5">
        <span
          className="ed-display"
          style={{ fontSize: 13, color: 'var(--ed-ink)', fontStyle: 'italic', fontWeight: 400 }}
        >
          {editionLine}
        </span>
        <span
          className="ed-mono"
          style={{ fontSize: 10, color: 'var(--ed-ink-faint)' }}
        >
          vol. 01
        </span>
        <div className="flex-1" />
        {lastRecompute && (
          <span
            className="ed-mono"
            style={{ fontSize: 10, color: 'var(--ed-ink-faint)' }}
          >
            recompute {lastRecompute}
          </span>
        )}
        {lastSettled && (
          <span
            className="ed-mono"
            style={{ fontSize: 10, color: 'var(--ed-ink-faint)' }}
          >
            · last fill {lastSettled}
          </span>
        )}
      </div>
    </footer>
  );
}

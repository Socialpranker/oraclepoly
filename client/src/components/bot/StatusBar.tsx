import type { BotStatus } from '../../hooks/useBot';

const STATUS_STYLES = {
  live: { bg: 'rgba(0,255,133,0.15)', color: '#00FF85', label: '● Live' },
  running: { bg: 'rgba(0,255,133,0.15)', color: '#00FF85', label: '● Running' },
  stopped: { bg: 'rgba(255,59,48,0.15)', color: '#FF3B30', label: '● Stopped' },
  dry_run: { bg: 'rgba(255,106,61,0.15)', color: '#FF6A3D', label: '● Dry Run' },
} as const;

function formatUptime(iso: string | null): string {
  if (!iso) return '\u2014';
  const diff = Date.now() - new Date(iso).getTime();
  const h = Math.floor(diff / 3_600_000);
  if (h < 24) return `${h}h`;
  const d = Math.floor(h / 24);
  return `${d}d ${h % 24}h`;
}

export function StatusBar({ status }: { status: BotStatus }) {
  const style = STATUS_STYLES[status.status];

  return (
    <div
      className="sticky top-0 z-40 px-4 py-3 flex items-center gap-2"
      style={{ background: 'rgba(11,13,16,0.95)', borderBottom: '1px solid rgba(255,255,255,0.06)', backdropFilter: 'blur(12px)' }}
    >
      <span
        className="text-xs font-semibold px-2.5 py-1 rounded-full"
        style={{ background: style.bg, color: style.color }}
      >
        {style.label}
      </span>
      <span className="text-xs text-[var(--color-text-dim)]">
        {formatUptime(status.last_cron_cycle)}
      </span>
    </div>
  );
}

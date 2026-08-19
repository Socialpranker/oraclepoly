import { useLifecycleCounts } from '../../../hooks/useBot';
import type { Stage } from '../../../hooks/useBot';
import { useIsMobile } from '../../../hooks/useViewport';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';

// Только stages у которых есть LifecycleCounts (paused исключён)
type LifecycleStage = Exclude<Stage, 'paused'>;

const STAGES: { id: LifecycleStage; emoji: string; label: string; color: string }[] = [
  { id: 'backtest', emoji: '🧪', label: 'Backtest', color: '#9b6cff' },
  { id: 'shadow',   emoji: '👁',  label: 'Shadow',   color: '#5b9dff' },
  { id: 'virtual',  emoji: '💸', label: 'Virtual',  color: '#e0b020' },
  { id: 'live',     emoji: '🔴', label: 'Live',     color: '#e04444' },
];

interface Props {
  current: Stage;
  onSelect: (stage: Stage) => void;
}

/**
 * LifecycleProgress — горизонтальный flow всех 4 stages с counts.
 *
 *   🧪 Backtest 3   →   👁 Shadow 6   →   💸 Virtual 0   →   🔴 Live 0
 *
 * Текущий stage подсвечен. Stage с count=0 окрашен faint — видно где работа
 * "зависла" (никого не promoted дальше). Клик переключает вкладку.
 *
 * Источник: useLifecycleCounts() → /v2/lifecycle/counts.
 * См. docs/detector_lifecycle.md.
 */
export function LifecycleProgress({ current, onSelect }: Props) {
  const { data, isLoading } = useLifecycleCounts();
  const isCompact = useIsMobile(600);

  return (
    <div
      style={{
        borderBottom: '1px solid var(--ed-rule)',
        background: 'rgba(255,255,255,0.01)',
      }}
    >
      <div
        style={{
          maxWidth: 1180,
          margin: '0 auto',
          padding: isCompact ? '10px 14px' : '14px 22px',
          display: 'flex',
          alignItems: 'center',
          gap: isCompact ? 2 : 4,
          flexWrap: 'nowrap',
          justifyContent: isCompact ? 'space-between' : 'flex-start',
          overflowX: 'auto',
        }}
      >
        {STAGES.map((s, i) => {
          const count = data?.[s.id].total ?? null;
          const isCurrent = current === s.id;
          const isEmpty = count === 0;
          const color = isCurrent ? s.color : isEmpty ? FAINT : DIM;
          const bg = isCurrent ? `${s.color}10` : 'transparent';
          const tooltip = data?.[s.id].label ?? '';

          return (
            <span
              key={s.id}
              style={{ display: 'inline-flex', alignItems: 'center', gap: isCompact ? 2 : 4, flexShrink: 0 }}
            >
              <button
                onClick={() => onSelect(s.id)}
                title={tooltip}
                style={{
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: isCompact ? 4 : 8,
                  padding: isCompact ? '6px 8px' : '6px 12px',
                  border: isCurrent ? `1px solid ${s.color}40` : '1px solid transparent',
                  background: bg,
                  color,
                  fontSize: isCompact ? 11 : 12,
                  letterSpacing: '0.04em',
                  cursor: 'pointer',
                  borderRadius: 2,
                  transition: 'all 150ms',
                  fontFamily: 'var(--ed-mono)',
                  minHeight: 32,
                }}
              >
                <span style={{ fontSize: isCompact ? 14 : 13 }}>{s.emoji}</span>
                {!isCompact && (
                  <span style={{ fontWeight: isCurrent ? 600 : 400 }}>{s.label}</span>
                )}
                <span
                  className="ed-mono"
                  style={{
                    fontSize: isCompact ? 11 : 11,
                    color: isCurrent ? s.color : isEmpty ? FAINT : 'var(--ed-ink)',
                    fontWeight: 600,
                    marginLeft: 2,
                    opacity: isLoading ? 0.4 : 1,
                  }}
                >
                  {count ?? '—'}
                </span>
              </button>
              {i < STAGES.length - 1 && (
                <span
                  style={{
                    color: FAINT,
                    fontSize: isCompact ? 11 : 14,
                    padding: isCompact ? '0 1px' : '0 4px',
                  }}
                >
                  →
                </span>
              )}
            </span>
          );
        })}
      </div>
    </div>
  );
}

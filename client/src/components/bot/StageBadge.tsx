/**
 * StageBadge — визуализация detector lifecycle stage.
 *
 * 4 stages из docs/detector_lifecycle.md:
 *   🧪 backtest   — purple — гипотеза проверена на canonical (нет в БД для prod детекторов)
 *   👁  shadow    — blue   — production cron эмитит signals в БД, no money
 *   💸 virtual    — yellow — full pipeline (calibrate→size→safety) → execution_orders virtual_*
 *   🔴 live       — red    — реальные ордера в Polymarket CLOB
 *   ⏸  paused    — grey   — детектор временно остановлен
 */

const FAINT = 'var(--ed-ink-faint)';

export type Stage = 'backtest' | 'shadow' | 'virtual' | 'live' | 'paused';

const STAGE_META: Record<Stage, { label: string; emoji: string; color: string; bg: string }> = {
  backtest: { label: 'backtest', emoji: '🧪', color: '#9b6cff', bg: 'rgba(155, 108, 255, 0.10)' },
  shadow:   { label: 'shadow',   emoji: '👁',  color: '#5b9dff', bg: 'rgba(91, 157, 255, 0.10)' },
  virtual:  { label: 'virtual',  emoji: '💸', color: '#e0b020', bg: 'rgba(224, 176, 32, 0.10)' },
  live:     { label: 'live',     emoji: '🔴', color: '#e04444', bg: 'rgba(224, 68, 68, 0.10)' },
  paused:   { label: 'paused',   emoji: '⏸',  color: FAINT,     bg: 'transparent' },
};

interface StageBadgeProps {
  stage: Stage;
  size?: 'xs' | 'sm' | 'md';
  withEmoji?: boolean;
}

export default function StageBadge({ stage, size = 'sm', withEmoji = true }: StageBadgeProps) {
  const meta = STAGE_META[stage] ?? STAGE_META.shadow;
  const fontSize = size === 'xs' ? 8 : size === 'sm' ? 10 : 12;
  const padding = size === 'xs' ? '2px 6px' : size === 'sm' ? '3px 8px' : '5px 11px';
  return (
    <span
      className="ed-mono"
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 5,
        fontSize,
        textTransform: 'uppercase',
        letterSpacing: '0.18em',
        padding,
        border: `1px solid ${meta.color}`,
        color: meta.color,
        background: meta.bg,
        whiteSpace: 'nowrap',
      }}
      title={`Detector lifecycle stage: ${stage}`}
    >
      {withEmoji && <span style={{ fontSize: fontSize * 1.1 }}>{meta.emoji}</span>}
      {meta.label}
    </span>
  );
}

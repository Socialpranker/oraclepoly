import type { Stage } from '../../../hooks/useBot';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';

const STAGE_META: Record<Stage, { emoji: string; label: string; color: string }> = {
  backtest: { emoji: '🧪', label: 'Backtest', color: '#9b6cff' },
  shadow:   { emoji: '👁',  label: 'Shadow',   color: '#5b9dff' },
  virtual:  { emoji: '💸', label: 'Virtual',  color: '#e0b020' },
  live:     { emoji: '🔴', label: 'Live',     color: '#e04444' },
  paused:   { emoji: '⏸',  label: 'Paused',   color: FAINT },
};

interface PromotionCriterion {
  text: string;
  met?: boolean | null;
}

interface Props {
  stage: Stage;
  title: string;
  description: string;
  criteria?: PromotionCriterion[];
  command?: string;
  warning?: string;
}

/**
 * EmptyStageState — большой плейсхолдер для пустого stage.
 *
 * Объясняет почему здесь пусто и что нужно сделать чтобы появились данные.
 * Promotion критерии из docs/detector_lifecycle.md.
 *
 * Use cases:
 *   Virtual: "0 promoted" + критерии Shadow→Virtual + TG команда
 *   Live: "0 live" + критерии Virtual→Live + EXECUTION_ENABLED warning
 */
export function EmptyStageState({
  stage,
  title,
  description,
  criteria = [],
  command,
  warning,
}: Props) {
  const meta = STAGE_META[stage];

  return (
    <div
      style={{
        maxWidth: 560,
        margin: '48px auto',
        padding: '40px 32px',
        border: `1px dashed ${meta.color}66`,
        background: `${meta.color}05`,
        textAlign: 'center',
      }}
    >
      <div style={{ fontSize: 40, marginBottom: 16, lineHeight: 1 }}>{meta.emoji}</div>

      <h3
        className="ed-display"
        style={{
          fontSize: 22,
          fontWeight: 500,
          color: 'var(--ed-ink)',
          marginBottom: 10,
          letterSpacing: '-0.01em',
        }}
      >
        {title}
      </h3>

      <p style={{ fontSize: 13, color: DIM, lineHeight: 1.55, marginBottom: 24 }}>
        {description}
      </p>

      {warning && (
        <div
          style={{
            padding: '10px 14px',
            border: '1px solid #e04444',
            background: 'rgba(224, 68, 68, 0.08)',
            color: '#e04444',
            fontSize: 11,
            fontFamily: 'var(--ed-mono)',
            marginBottom: 22,
            textAlign: 'left',
          }}
        >
          ⚠ {warning}
        </div>
      )}

      {criteria.length > 0 && (
        <div style={{ textAlign: 'left', marginBottom: command ? 22 : 0 }}>
          <div
            className="ed-kicker"
            style={{
              fontSize: 9,
              color: meta.color,
              letterSpacing: '0.18em',
              marginBottom: 10,
            }}
          >
            Promotion criteria
          </div>
          <ul
            style={{
              listStyle: 'none',
              padding: 0,
              margin: 0,
              fontSize: 12,
              fontFamily: 'var(--ed-mono)',
              color: DIM,
            }}
          >
            {criteria.map((c, i) => (
              <li
                key={i}
                style={{
                  paddingLeft: 18,
                  position: 'relative',
                  marginBottom: 6,
                  lineHeight: 1.5,
                }}
              >
                <span
                  style={{
                    position: 'absolute',
                    left: 0,
                    top: 0,
                    color: c.met === true ? 'var(--ed-up)' : c.met === false ? 'var(--ed-down)' : FAINT,
                    fontWeight: 600,
                  }}
                >
                  {c.met === true ? '✓' : c.met === false ? '✕' : '·'}
                </span>
                {c.text}
              </li>
            ))}
          </ul>
        </div>
      )}

      {command && (
        <div
          style={{
            padding: '10px 14px',
            background: 'rgba(255,255,255,0.04)',
            border: '1px solid var(--ed-rule)',
            fontFamily: 'var(--ed-mono)',
            fontSize: 12,
            color: 'var(--ed-ink)',
            display: 'inline-block',
          }}
        >
          {command}
        </div>
      )}
    </div>
  );
}

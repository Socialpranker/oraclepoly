import type { Stage } from '../../../hooks/useBot';
import { useIsMobile } from '../../../hooks/useViewport';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';

const STAGE_META: Record<Stage, { emoji: string; label: string; color: string }> = {
  backtest: { emoji: '🧪', label: 'Backtest', color: '#9b6cff' },
  shadow:   { emoji: '👁',  label: 'Shadow',   color: '#5b9dff' },
  virtual:  { emoji: '💸', label: 'Virtual',  color: '#e0b020' },
  live:     { emoji: '🔴', label: 'Live',     color: '#e04444' },
  paused:   { emoji: '⏸',  label: 'Paused',   color: FAINT },
};

export interface StageHeroKPI {
  label: string;
  value: string;
  sub?: string;
  valueColor?: string;
}

interface Props {
  stage: Stage;
  title: string;
  subtitle?: string;
  kpis?: StageHeroKPI[];
}

/**
 * StageHero — большая шапка вкладки lifecycle с цветом stage.
 *
 * Layout (desktop): grid 1.4fr 1fr — слева большой заголовок с stage colour,
 * справа 1-3 KPI карточки. Mobile (<700px) — column.
 *
 * Использует CSS variables из client/src/index.css и stage colors из
 * StageBadge.tsx (single source of truth).
 */
export function StageHero({ stage, title, subtitle, kpis = [] }: Props) {
  const meta = STAGE_META[stage];
  const isCompact = useIsMobile(700);

  return (
    <section
      className="ed-grain"
      style={{
        position: 'relative',
        padding: isCompact ? '22px 16px 20px' : '32px 24px 28px',
        borderBottom: '1px solid var(--ed-rule-strong)',
        background: `linear-gradient(180deg, ${meta.color}08 0%, transparent 100%)`,
      }}
    >
      <div
        style={{
          maxWidth: 1180,
          margin: '0 auto',
          display: 'grid',
          gridTemplateColumns: isCompact || kpis.length === 0 ? '1fr' : 'minmax(0, 1.4fr) minmax(0, 1fr)',
          gap: isCompact ? 22 : 56,
          alignItems: 'end',
        }}
      >
        <div>
          <div
            className="ed-kicker"
            style={{
              color: meta.color,
              marginBottom: 10,
              letterSpacing: '0.18em',
              fontSize: 11,
              display: 'inline-flex',
              gap: 6,
              alignItems: 'center',
            }}
          >
            <span style={{ fontSize: 13 }}>{meta.emoji}</span>
            {meta.label.toUpperCase()}
          </div>
          <h1
            className="ed-display"
            style={{
              fontSize: isCompact ? 'clamp(22px, 6.5vw, 30px)' : 'clamp(32px, 4.5vw, 52px)',
              fontWeight: 400,
              lineHeight: 1.05,
              letterSpacing: '-0.025em',
              color: 'var(--ed-ink)',
              margin: 0,
            }}
          >
            {title}
          </h1>
          {subtitle && (
            <p
              style={{
                marginTop: 14,
                maxWidth: 580,
                fontSize: isCompact ? 12 : 13,
                lineHeight: 1.55,
                color: DIM,
              }}
            >
              {subtitle}
            </p>
          )}
        </div>

        {kpis.length > 0 && (
          <div
            style={{
              display: 'grid',
              gridTemplateRows: `repeat(${kpis.length}, auto)`,
              gridTemplateColumns: isCompact && kpis.length > 1 ? 'repeat(2, 1fr)' : '1fr',
              gridAutoFlow: isCompact ? 'row' : undefined,
              gap: isCompact ? 10 : 16,
            }}
          >
            {kpis.map((kpi) => (
              <KPICard key={kpi.label} {...kpi} stageColor={meta.color} isCompact={isCompact} />
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function KPICard({
  label,
  value,
  sub,
  valueColor,
  stageColor,
  isCompact,
}: StageHeroKPI & { stageColor: string; isCompact: boolean }) {
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: isCompact ? '1fr' : 'minmax(0, 1fr) auto',
        alignItems: isCompact ? 'flex-start' : 'baseline',
        gap: isCompact ? 4 : 14,
        paddingBottom: isCompact ? 8 : 12,
        borderBottom: '1px dashed var(--ed-rule)',
      }}
    >
      <div>
        <div
          className="ed-kicker"
          style={{
            fontSize: 9,
            color: DIM,
            letterSpacing: '0.16em',
            marginBottom: 4,
          }}
        >
          {label}
        </div>
        {sub && (
          <div className="ed-mono" style={{ fontSize: 10, color: FAINT }}>
            {sub}
          </div>
        )}
      </div>
      <div
        className="ed-display"
        style={{
          fontSize: isCompact ? 'clamp(20px, 5vw, 26px)' : 32,
          fontWeight: 300,
          letterSpacing: '-0.02em',
          color: valueColor ?? stageColor,
          lineHeight: 0.95,
          fontVariantNumeric: 'tabular-nums',
          marginTop: isCompact ? 4 : 0,
        }}
      >
        {value}
      </div>
    </div>
  );
}

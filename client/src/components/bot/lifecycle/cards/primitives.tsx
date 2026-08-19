/**
 * Shared card primitives — reusable между PromotionCard и VirtualCard.
 *
 * Не делаем generic StageCard со slot-based render: карточки принципиально
 * разные (3 vs 5 gates, разные hero metrics, разный middle section).
 * Вместо — extract building blocks: shell/header/dots/row/mini.
 */
import type { ReactNode } from 'react';
import { detectorShort } from '../backtest/utils';
import { gateColor } from './colors';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';

/** Внешняя кнопка-shell с hover/expanded border + transition. */
export function CardShell({
  color,
  expanded,
  onToggle,
  children,
}: {
  color: string;
  expanded: boolean;
  onToggle: () => void;
  children: ReactNode;
}) {
  return (
    <button
      onClick={onToggle}
      style={{
        textAlign: 'left',
        padding: 0,
        width: '100%',
        background: 'transparent',
        border: `1px solid ${expanded ? color + '60' : RULE}`,
        cursor: 'pointer',
        transition: 'all 200ms',
        boxShadow: expanded ? `0 0 0 1px ${color}30` : 'none',
        display: 'block',
      }}
      onMouseEnter={(e) => {
        if (!expanded) e.currentTarget.style.borderColor = color + '40';
      }}
      onMouseLeave={(e) => {
        if (!expanded) e.currentTarget.style.borderColor = RULE;
      }}
    >
      <div style={{ padding: 'clamp(14px, 3vw, 18px) clamp(14px, 3vw, 20px) 16px' }}>
        {children}
      </div>
    </button>
  );
}

/** Header row: ready label слева + gate dots справа. */
export function CardHeader({
  label,
  color,
  gates,
}: {
  label: string;
  color: string;
  gates: Array<boolean | null>;
}) {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'baseline',
        marginBottom: 14,
      }}
    >
      <div className="ed-kicker" style={{ fontSize: 9, letterSpacing: '0.2em', color, fontWeight: 600 }}>
        {label}
      </div>
      <GateDots gates={gates} />
    </div>
  );
}

/** Detector ID + human-readable name. */
export function CardDetectorTitle({ detector }: { detector: string }) {
  return (
    <>
      <div className="ed-mono" style={{ fontSize: 12, color: DIM, marginBottom: 6 }}>
        {detector}
      </div>
      <div
        className="ed-display"
        style={{
          fontSize: 18,
          fontWeight: 400,
          color: 'var(--ed-ink)',
          marginBottom: 18,
          letterSpacing: '-0.01em',
        }}
      >
        {detectorShort(detector).split(' ').slice(1).join(' ') || detector}
      </div>
    </>
  );
}

/** N gate dots, animated stagger 80ms. */
export function GateDots({ gates }: { gates: Array<boolean | null> }) {
  return (
    <div style={{ display: 'inline-flex', gap: 5, alignItems: 'center' }}>
      {gates.map((g, i) => (
        <span
          key={i}
          title={`Gate ${i + 1}`}
          style={{
            width: 9,
            height: 9,
            borderRadius: '50%',
            background: g === true ? gateColor(true) : 'transparent',
            border: `1.5px solid ${gateColor(g)}`,
            display: 'inline-block',
            transition: 'all 250ms',
            transitionDelay: `${i * 80}ms`,
          }}
        />
      ))}
    </div>
  );
}

/** One-line gate row: ✓/✕ icon · G{n} label · value. */
export function GateRow({
  num,
  passed,
  label,
  value,
  disabled = false,
}: {
  num: number;
  passed: boolean | null;
  label: string;
  value: string;
  disabled?: boolean;
}) {
  const c = disabled ? FAINT : gateColor(passed);
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'baseline',
        gap: 8,
        fontSize: 11,
        fontFamily: 'var(--ed-mono)',
        opacity: disabled ? 0.5 : 1,
      }}
    >
      <span style={{ color: c, fontWeight: 600, fontSize: 10, minWidth: 14 }}>
        {passed === true ? '✓' : passed === false ? '✕' : '·'}
      </span>
      <span style={{ color: DIM, flex: 1, minWidth: 0, lineHeight: 1.4 }}>
        <span style={{ color: FAINT, marginRight: 6 }}>G{num}</span>
        {label}
      </span>
      <span style={{ color: c, fontWeight: 500, whiteSpace: 'nowrap', flexShrink: 0 }}>{value}</span>
    </div>
  );
}

/** Footer: "› expand · per-bucket WR" с rotate animation. */
export function CardFooter({ expanded, label }: { expanded: boolean; label: string }) {
  return (
    <div
      style={{
        marginTop: 14,
        paddingTop: 10,
        borderTop: `1px solid ${RULE}`,
        fontSize: 10,
        color: FAINT,
        fontFamily: 'var(--ed-mono)',
        letterSpacing: '0.16em',
        textTransform: 'uppercase',
        display: 'flex',
        alignItems: 'center',
        gap: 6,
      }}
    >
      <span
        style={{
          transition: 'transform 200ms',
          display: 'inline-block',
          transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)',
        }}
      >
        ›
      </span>
      {label}
    </div>
  );
}

/** Mini stat block (label uppercase + value). Used by PromotionCard footer stats. */
export function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div
        style={{
          color: FAINT,
          fontSize: 9,
          letterSpacing: '0.14em',
          textTransform: 'uppercase',
          marginBottom: 3,
        }}
      >
        {label}
      </div>
      <div style={{ color: 'var(--ed-ink)', fontWeight: 500 }}>{value}</div>
    </div>
  );
}

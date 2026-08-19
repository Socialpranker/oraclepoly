import type { BacktestMapItem } from '../../../../hooks/useBot';
import {
  verdictColor,
  verdictLabel,
  haircutColor,
  fmtUsd,
  fmtPct,
  fmtCompact,
  detectorShort,
} from './utils';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';
const RULE_STRONG = 'var(--ed-rule-strong)';

interface Props {
  item: BacktestMapItem;
  expanded: boolean;
  onToggle: () => void;
  hasDoc: boolean;
}

/**
 * BacktestCard — карточка детектора в Mission Control grid.
 *
 * Hero metric — `honest_daily_usd_at_5k` крупным Fraunces italic.
 * Haircut bar визуализирует optimism cut (красная зона = большой haircut).
 *
 * Если detector без MD-файла — серая карточка "no backtest document".
 *
 * Click toggles expand → BacktestExpand загружается через useDetectorBacktest.
 */
export function BacktestCard({ item, expanded, onToggle, hasDoc }: Props) {
  const verdict = item.verdict;
  const vColor = verdictColor(verdict);
  const haircut = item.honest_haircut_factor;
  const hColor = haircutColor(haircut);

  if (!hasDoc) {
    return (
      <div
        style={{
          padding: 18,
          border: `1px dashed ${RULE_STRONG}`,
          background: 'rgba(255,255,255,0.01)',
          color: FAINT,
          minHeight: 220,
          display: 'flex',
          flexDirection: 'column',
          justifyContent: 'space-between',
        }}
      >
        <div>
          <div
            className="ed-kicker"
            style={{ fontSize: 9, letterSpacing: '0.18em', color: FAINT, marginBottom: 8 }}
          >
            — no MD
          </div>
          <div
            className="ed-mono"
            style={{ fontSize: 13, color: DIM, fontWeight: 500, marginBottom: 6 }}
          >
            {item.detector}
          </div>
          <div style={{ fontSize: 11, color: FAINT, lineHeight: 1.5, fontStyle: 'italic' }}>
            Backtest document not authored. Add{' '}
            <code style={{ color: DIM }}>docs/backtests/{item.detector}.md</code> with
            verdict and metrics to enable this card.
          </div>
        </div>
        <div style={{ fontSize: 10, color: FAINT, marginTop: 16 }}>
          {item.stage ? `Currently in stage='${item.stage}'` : '(not registered in detector_state)'}
        </div>
      </div>
    );
  }

  const isPositive = (item.honest_daily_usd_at_5k ?? 0) > 0;
  const heroColor = isPositive ? vColor : 'var(--ed-down)';

  return (
    <button
      onClick={onToggle}
      style={{
        textAlign: 'left',
        padding: 0,
        width: '100%',
        background: 'transparent',
        border: `1px solid ${expanded ? vColor + '60' : RULE}`,
        cursor: 'pointer',
        transition: 'all 200ms',
        boxShadow: expanded ? `0 0 0 1px ${vColor}30` : 'none',
        display: 'block',
      }}
      onMouseEnter={(e) => {
        if (!expanded) e.currentTarget.style.borderColor = vColor + '40';
      }}
      onMouseLeave={(e) => {
        if (!expanded) e.currentTarget.style.borderColor = RULE;
      }}
    >
      <div style={{ padding: 'clamp(14px, 3vw, 18px) clamp(14px, 3vw, 20px) 16px' }}>
        {/* Verdict + name */}
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'baseline',
            marginBottom: 14,
          }}
        >
          <div
            className="ed-kicker"
            style={{ fontSize: 9, letterSpacing: '0.2em', color: vColor, fontWeight: 600 }}
          >
            {verdictLabel(verdict)}
          </div>
          {item.stage && item.stage !== 'backtest' && (
            <div
              className="ed-mono"
              style={{
                fontSize: 9,
                letterSpacing: '0.14em',
                textTransform: 'uppercase',
                color: stageColor(item.stage),
                opacity: 0.8,
              }}
            >
              {item.stage}
            </div>
          )}
        </div>

        <div
          className="ed-mono"
          style={{
            fontSize: 12,
            color: DIM,
            marginBottom: 6,
            letterSpacing: '0.02em',
          }}
        >
          {item.detector}
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
          {detectorShort(item.detector).split(' ').slice(1).join(' ') || item.detector}
        </div>

        {/* Hero number — honest @ $5K */}
        <div style={{ marginBottom: 4 }}>
          <div
            className="ed-display"
            style={{
              fontSize: 'clamp(28px, 7vw, 38px)',
              fontWeight: 300,
              fontStyle: 'italic',
              letterSpacing: '-0.025em',
              color: heroColor,
              lineHeight: 1,
              fontVariantNumeric: 'tabular-nums',
            }}
          >
            {fmtUsd(item.honest_daily_usd_at_5k, { sign: true })}
          </div>
          <div
            className="ed-mono"
            style={{
              fontSize: 10,
              color: FAINT,
              marginTop: 4,
              letterSpacing: '0.06em',
            }}
          >
            honest $/day @ $5K bankroll
          </div>
        </div>

        {/* Secondary — @ $1K */}
        <div
          style={{
            marginTop: 6,
            paddingTop: 10,
            borderTop: `1px dashed ${RULE}`,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'baseline',
            color: DIM,
            fontSize: 11,
            fontFamily: 'var(--ed-mono)',
          }}
        >
          <span style={{ color: FAINT, letterSpacing: '0.06em' }}>@ $1K</span>
          <span style={{ color: heroColor, fontWeight: 500 }}>
            {fmtUsd(item.honest_daily_usd_at_1k, { sign: true })}
          </span>
        </div>

        {/* Haircut bar */}
        {haircut != null && (
          <div style={{ marginTop: 18 }}>
            <div
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                fontSize: 9,
                letterSpacing: '0.16em',
                textTransform: 'uppercase',
                color: FAINT,
                marginBottom: 6,
                fontFamily: 'var(--ed-mono)',
              }}
            >
              <span>Honest haircut</span>
              <span style={{ color: hColor, fontWeight: 600 }}>
                ×{haircut.toFixed(2)} ({Math.round((1 - haircut) * 100)}% cut)
              </span>
            </div>
            <div
              style={{
                height: 4,
                background: RULE,
                position: 'relative',
                overflow: 'hidden',
              }}
            >
              <div
                style={{
                  position: 'absolute',
                  inset: 0,
                  width: `${haircut * 100}%`,
                  background: `linear-gradient(90deg, var(--ed-down) 0%, var(--ed-accent) 50%, var(--ed-up) 100%)`,
                  opacity: 0.85,
                }}
              />
            </div>
          </div>
        )}

        {/* Stats row */}
        <div
          style={{
            marginTop: 18,
            display: 'grid',
            gridTemplateColumns: 'repeat(3, 1fr)',
            gap: 10,
            fontSize: 11,
            fontFamily: 'var(--ed-mono)',
          }}
        >
          <Mini
            label="signals"
            value={`${fmtCompact(item.n_signals)} · ${item.backtest_days ?? '—'}d`}
          />
          <Mini label="WR · ROI net" value={`${fmtPct(item.wr_pct)} · ${fmtPct(item.roi_net_pct, { sign: true })}`} />
          <Mini label="trades/day" value={item.trades_per_day?.toFixed(1) ?? '—'} />
        </div>

        {/* Footer */}
        <div
          style={{
            marginTop: 16,
            paddingTop: 12,
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
          <span style={{ transition: 'transform 200ms', display: 'inline-block', transform: expanded ? 'rotate(90deg)' : 'rotate(0deg)' }}>›</span>
          {expanded ? 'collapse details' : 'expand details'}
        </div>
      </div>
    </button>
  );
}

function Mini({ label, value }: { label: string; value: string }) {
  return (
    <div>
      <div style={{ color: FAINT, fontSize: 9, letterSpacing: '0.14em', textTransform: 'uppercase', marginBottom: 3 }}>
        {label}
      </div>
      <div style={{ color: 'var(--ed-ink)', fontWeight: 500 }}>{value}</div>
    </div>
  );
}

function stageColor(stage: string): string {
  const colors: Record<string, string> = {
    shadow: '#5b9dff',
    virtual: '#e0b020',
    live: '#e04444',
    paused: 'var(--ed-ink-faint)',
    backtest: '#9b6cff',
  };
  return colors[stage] ?? 'var(--ed-ink-faint)';
}

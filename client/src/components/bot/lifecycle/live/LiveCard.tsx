import type { LiveDetectorItem } from '../../../../hooks/useBot';
import { fmtUsd, pnlColor, slippageDeltaColor, STAGE_RED } from './utils';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';

interface Props {
  item: LiveDetectorItem;
  onClick?: () => void;
  isExpanded?: boolean;
}

/**
 * LiveCard — per-detector card. Hero: live_pnl_7d Fraunces italic.
 * Δ vs virtual projection — тихой строкой в футере; детальный breakdown
 * (per-bucket live vs virtual) — в LiveExpand по клику.
 */
export function LiveCard({ item, onClick, isExpanded }: Props) {
  const { live_7d, slippage_delta_usd } = item;
  const heroPnlColor = pnlColor(live_7d.pnl_usd);

  return (
    <div
      onClick={onClick}
      style={{
        position: 'relative',
        padding: '20px 22px',
        background: isExpanded ? 'rgba(224,68,68,0.08)' : 'rgba(224,68,68,0.03)',
        border: `1px solid ${isExpanded ? STAGE_RED : RULE}`,
        cursor: onClick ? 'pointer' : 'default',
        transition: 'all 200ms ease-out',
        overflow: 'hidden',
      }}
      onMouseEnter={(e) => {
        if (onClick && !isExpanded) e.currentTarget.style.borderColor = `${STAGE_RED}80`;
      }}
      onMouseLeave={(e) => {
        if (onClick && !isExpanded) e.currentTarget.style.borderColor = RULE;
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 10 }}>
        <div className="ed-mono" style={{ fontSize: 12, color: 'var(--ed-ink)', fontWeight: 500 }}>
          {item.detector}
        </div>
        <div
          style={{
            fontSize: 9,
            color: STAGE_RED,
            fontFamily: 'var(--ed-mono)',
            letterSpacing: '0.18em',
            textTransform: 'uppercase',
            padding: '2px 6px',
            border: `1px solid ${STAGE_RED}60`,
            background: `${STAGE_RED}15`,
          }}
        >
          🔴 LIVE
        </div>
      </div>

      <div
        className="ed-display"
        style={{
          fontSize: 'clamp(28px, 4vw, 38px)',
          fontStyle: 'italic',
          fontWeight: 300,
          color: heroPnlColor,
          lineHeight: 1.05,
          letterSpacing: '-0.01em',
          marginBottom: 4,
        }}
      >
        {fmtUsd(live_7d.pnl_usd, { sign: true })}
      </div>
      <div
        className="ed-mono"
        style={{
          fontSize: 10,
          color: FAINT,
          letterSpacing: '0.06em',
          marginBottom: 16,
        }}
      >
        live realized · 7d
      </div>

      {/* Stats row */}
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(2, 1fr)',
          gap: 8,
          paddingTop: 12,
          borderTop: `1px solid ${RULE}`,
        }}
      >
        <Mini label="Filled" value={`${live_7d.n_filled}/${live_7d.n_placed}`} />
        <Mini
          label="Wins"
          value={`${live_7d.wins}/${live_7d.wins + live_7d.losses}`}
          color={live_7d.wins > live_7d.losses ? 'var(--ed-up)' : DIM}
        />
      </div>

      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          marginTop: 10,
          fontSize: 9,
          fontFamily: 'var(--ed-mono)',
          color: FAINT,
          letterSpacing: '0.04em',
        }}
      >
        <span>slippage {fmtUsd(live_7d.slippage_usd)} · fees {fmtUsd(live_7d.fees_usd)}</span>
        {slippage_delta_usd !== null && (
          <span style={{ color: slippageDeltaColor(slippage_delta_usd) }}>
            Δ vs virt {fmtUsd(slippage_delta_usd, { sign: true })}
          </span>
        )}
      </div>
    </div>
  );
}

function Mini({ label, value, color }: { label: string; value: string; color?: string }) {
  return (
    <div>
      <div
        style={{
          fontSize: 9,
          color: FAINT,
          letterSpacing: '0.12em',
          textTransform: 'uppercase',
          marginBottom: 2,
          fontFamily: 'var(--ed-mono)',
        }}
      >
        {label}
      </div>
      <div
        className="ed-mono"
        style={{ fontSize: 13, color: color ?? 'var(--ed-ink)', fontWeight: 500 }}
      >
        {value}
      </div>
    </div>
  );
}

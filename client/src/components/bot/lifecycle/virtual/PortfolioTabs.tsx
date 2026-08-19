import type { Portfolio, PortfolioId } from '../../../../hooks/useBot';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';
const STAGE = '#e0b020';

export type SelectedPortfolio = 'all' | PortfolioId;

interface Props {
  portfolios: Portfolio[];
  selected: SelectedPortfolio;
  onSelect: (id: SelectedPortfolio) => void;
  /** Per-portfolio detector counts (filtered live_readiness items). */
  countsByPortfolio: Record<string, number>;
  totalCount: number;
}

/**
 * PortfolioTabs — горизонтальные tabs для переключения portfolio в Virtual stage.
 * Phase 5 of portfolios refactor.
 *
 * Layout: [All N] [Rules N] [ML N] [Sentiment N] [Whale-copy N] [External N]
 * Empty portfolio (n_detectors=0) — показывается серым, всё равно clickable.
 */
export function PortfolioTabs({
  portfolios,
  selected,
  onSelect,
  countsByPortfolio,
  totalCount,
}: Props) {
  return (
    <div
      style={{
        display: 'flex',
        gap: 4,
        flexWrap: 'wrap',
        alignItems: 'baseline',
        padding: '8px 0 12px',
        borderBottom: `1px solid ${RULE}`,
        marginBottom: 18,
      }}
    >
      <Tab
        label="All"
        count={totalCount}
        active={selected === 'all'}
        onClick={() => onSelect('all')}
      />
      {portfolios.map((p) => (
        <Tab
          key={p.portfolio_id}
          label={p.display_name}
          count={countsByPortfolio[p.portfolio_id] ?? 0}
          active={selected === p.portfolio_id}
          onClick={() => onSelect(p.portfolio_id)}
          empty={p.n_detectors === 0}
        />
      ))}
    </div>
  );
}

function Tab({
  label,
  count,
  active,
  onClick,
  empty = false,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
  empty?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '8px 14px',
        background: active ? `${STAGE}15` : 'transparent',
        border: `1px solid ${active ? `${STAGE}50` : RULE}`,
        color: active ? STAGE : empty ? FAINT : DIM,
        fontSize: 11,
        fontFamily: 'var(--ed-mono)',
        letterSpacing: '0.04em',
        cursor: 'pointer',
        transition: 'all 150ms',
        minHeight: 34,
        display: 'inline-flex',
        alignItems: 'baseline',
        gap: 8,
      }}
    >
      <span>{label}</span>
      <span
        style={{
          fontSize: 9,
          color: active ? STAGE : FAINT,
          opacity: empty ? 0.4 : 0.8,
          fontWeight: 600,
        }}
      >
        {count}
      </span>
    </button>
  );
}

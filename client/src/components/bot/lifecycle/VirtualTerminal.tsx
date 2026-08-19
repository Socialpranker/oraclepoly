import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useLiveReadiness, usePortfolios } from '../../../hooks/useBot';
import type { Stage, BotTab } from '../../../hooks/useBot';
import { LifecycleProgress } from './LifecycleProgress';
import { StageHero } from './StageHero';
import { LiveReadinessMap } from './virtual/LiveReadinessMap';
import { VirtualCard } from './virtual/VirtualCard';
import { VirtualExpand } from './virtual/VirtualExpand';
import { VirtualEmptyState } from './virtual/VirtualEmptyState';
import { SettleVelocityBar } from './virtual/SettleVelocityBar';
import { DailySummaryBar } from './virtual/DailySummaryBar';
import { PortfolioTabs, type SelectedPortfolio } from './virtual/PortfolioTabs';
import { PortfolioRollupCard } from './virtual/PortfolioRollupCard';
import {
  applyFilter,
  compareItems,
  type VirtualSortKey,
  type VirtualFilterKey,
} from './virtual/utils';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';
const RULE = 'var(--ed-rule)';
const STAGE = '#e0b020';

/**
 * VirtualTerminal — Live Readiness Center (Variant A).
 *
 * Layout:
 *   1. LifecycleProgress (общий)
 *   2. StageHero "Live Readiness Center" (gold) с 3 KPI
 *   3. LiveReadinessMap — hero scatter (fill_rate × Virtual PnL 7d)
 *   4. Если 0 promoted → VirtualEmptyState с 6 gates + ready candidates
 *   5. Если есть virtual: Toolbar + VirtualCardsGrid + inline VirtualExpand
 *
 * См. plans/wiggly-nibbling-stardust.md.
 */
export function VirtualTerminal() {
  const [searchParams, setSearchParams] = useSearchParams();
  const { data, isLoading } = useLiveReadiness();
  const { data: portfoliosData } = usePortfolios();
  const [openDetector, setOpenDetector] = useState<string | null>(null);
  const [sort, setSort] = useState<VirtualSortKey>('gates');
  const [filter, setFilter] = useState<VirtualFilterKey>('all');

  // Phase 5: portfolio selection — persisted в URL searchParam `portfolio`.
  const portfolioParam = searchParams.get('portfolio') as SelectedPortfolio | null;
  const selectedPortfolio: SelectedPortfolio = portfolioParam ?? 'all';

  const switchStage = (s: Stage) => {
    setSearchParams((p) => {
      const next = new URLSearchParams(p);
      next.set('stage', s as BotTab);
      return next;
    }, { replace: true });
  };

  const switchPortfolio = (id: SelectedPortfolio) => {
    setSearchParams((p) => {
      const next = new URLSearchParams(p);
      if (id === 'all') {
        next.delete('portfolio');
      } else {
        next.set('portfolio', id);
      }
      return next;
    }, { replace: true });
  };

  const allItems = useMemo(() => data?.detectors ?? [], [data?.detectors]);
  const candidates = data?.candidates_from_shadow ?? [];
  const executionEnabled = data?.execution_enabled ?? false;
  const portfolios = portfoliosData?.portfolios ?? [];

  // Phase 5: filter items by selected portfolio FIRST (before sort/accumulating filter).
  const items = useMemo(() => {
    if (selectedPortfolio === 'all') return allItems;
    return allItems.filter((i) => i.portfolio_id === selectedPortfolio);
  }, [allItems, selectedPortfolio]);

  // Counts per portfolio (для tab badges) — based on full items list, не filtered.
  const countsByPortfolio = useMemo(() => {
    const c: Record<string, number> = {};
    for (const i of allItems) {
      const pid = i.portfolio_id ?? 'unknown';
      c[pid] = (c[pid] ?? 0) + 1;
    }
    return c;
  }, [allItems]);

  const selectedPortfolioObj =
    selectedPortfolio !== 'all'
      ? portfolios.find((p) => p.portfolio_id === selectedPortfolio)
      : null;

  const sortedAndFiltered = useMemo(() => {
    const filtered = applyFilter(items, filter);
    return [...filtered].sort((a, b) => compareItems(a, b, sort));
  }, [items, sort, filter]);

  const readyCount = items.filter((i) => i.ready).length;
  // Split PnL (см. инцидент 2026-05-18 — VOIDED_BUG + uq_open_market + mark-to-market):
  //   resolved = реальный realized PnL по WIN/LOSS (правда).
  //   paper    = mark-to-market на EARLY_EXIT (может быть inflated в extreme zones).
  const resolvedPnl = items.reduce((s, i) => s + i.virtual_7d.pnl_resolved_usd, 0);
  const paperPnl = items.reduce((s, i) => s + i.virtual_7d.pnl_paper_usd, 0);
  const resolvedColor = resolvedPnl > 0 ? UP : resolvedPnl < 0 ? DOWN : undefined;
  const paperColor = paperPnl > 0 ? UP : paperPnl < 0 ? DOWN : undefined;
  const totalResolved = items.reduce((s, i) => s + i.virtual_7d.wins + i.virtual_7d.losses, 0);
  const totalEarlyExits = items.reduce((s, i) => s + i.virtual_7d.early_exits, 0);

  const scrollToCard = (detector: string) => {
    const el = document.getElementById(`virtual-card-${detector}`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      setOpenDetector(detector);
    }
  };

  const openItem = openDetector ? items.find((i) => i.detector === openDetector) : null;

  return (
    <div>
      <LifecycleProgress current="virtual" onSelect={switchStage} />
      <StageHero
        stage="virtual"
        title="Live Readiness Center · Virtual → Live"
        subtitle="Готовы ли детекторы стрелять реальными деньгами на CLOB? Resolved PnL — realized по WIN/LOSS (правда). Paper PnL — mark-to-mid на early-exit, может быть завышен в extreme zones. Promote в live: 5/5 gates passed."
        kpis={[
          {
            label: 'Resolved PnL · 7d',
            value: `${resolvedPnl >= 0 ? '+' : '−'}$${Math.abs(resolvedPnl).toFixed(2)}`,
            sub: `WIN/LOSS, n=${totalResolved} · реальный edge`,
            valueColor: resolvedColor,
          },
          {
            label: 'Paper PnL · 7d',
            value: `${paperPnl >= 0 ? '+' : '−'}$${Math.abs(paperPnl).toFixed(2)}`,
            sub: `early-exit mark-to-mid, n=${totalEarlyExits}`,
            valueColor: paperColor,
          },
          {
            label: 'Ready to promote',
            value: `${readyCount}/${items.length}`,
            sub: '5/5 gates passed',
            valueColor: readyCount > 0 ? UP : DIM,
          },
        ]}
      />

      <DailySummaryBar />

      {data?.settle_velocity_global && (
        <div
          style={{
            maxWidth: 1180,
            margin: '0 auto',
            padding: '20px 22px 0',
          }}
        >
          <SettleVelocityBar velocity={data.settle_velocity_global} variant="full" />
        </div>
      )}

      {/* Phase 5: portfolio tabs */}
      {portfolios.length > 0 && (
        <div style={{ maxWidth: 1180, margin: '0 auto', padding: '8px 22px 0' }}>
          <PortfolioTabs
            portfolios={portfolios}
            selected={selectedPortfolio}
            onSelect={switchPortfolio}
            countsByPortfolio={countsByPortfolio}
            totalCount={allItems.length}
          />
          {selectedPortfolioObj && <PortfolioRollupCard portfolio={selectedPortfolioObj} />}
        </div>
      )}

      <LiveReadinessMap items={items} isLoading={isLoading} onDotClick={scrollToCard} />

      {items.length === 0 ? (
        <VirtualEmptyState candidates={candidates} executionEnabled={executionEnabled} />
      ) : (
        <div style={{ maxWidth: 1180, margin: '0 auto', padding: '24px 22px 56px' }}>
          <Toolbar
            count={sortedAndFiltered.length}
            total={items.length}
            sort={sort}
            onSortChange={setSort}
            filter={filter}
            onFilterChange={setFilter}
          />

          {sortedAndFiltered.length === 0 ? (
            <div style={{ color: FAINT, fontSize: 12, padding: 40, textAlign: 'center' }}>
              Нет детекторов под этим фильтром.
            </div>
          ) : (
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: 'repeat(auto-fit, minmax(320px, 1fr))',
                gap: 14,
                marginBottom: 36,
              }}
            >
              {sortedAndFiltered.map((item) => {
                const isOpen = openDetector === item.detector;
                return (
                  <div
                    key={item.detector}
                    id={`virtual-card-${item.detector}`}
                    style={{ scrollMarginTop: 100 }}
                  >
                    <VirtualCard
                      item={item}
                      expanded={isOpen}
                      onToggle={() => setOpenDetector(isOpen ? null : item.detector)}
                    />
                  </div>
                );
              })}
            </div>
          )}

          {openItem && (
            <div style={{ border: `1px solid ${STAGE}40`, marginBottom: 36 }}>
              <VirtualExpand item={openItem} />
            </div>
          )}

          <p style={{ color: FAINT, fontSize: 10, marginTop: 18, lineHeight: 1.6 }}>
            Promotion Virtual → Live только manual через TG (с warning о реальных $$$). UI показывает
            5 critriia + 6th manual decision. См. <code>docs/detector_lifecycle.md</code>.
          </p>
        </div>
      )}
    </div>
  );
}

function Toolbar({
  count,
  total,
  sort,
  onSortChange,
  filter,
  onFilterChange,
}: {
  count: number;
  total: number;
  sort: VirtualSortKey;
  onSortChange: (s: VirtualSortKey) => void;
  filter: VirtualFilterKey;
  onFilterChange: (f: VirtualFilterKey) => void;
}) {
  return (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'baseline',
        marginBottom: 18,
        paddingBottom: 12,
        borderBottom: `1px solid ${RULE}`,
        flexWrap: 'wrap',
        gap: 12,
        marginTop: 32,
      }}
    >
      <div className="ed-kicker" style={{ fontSize: 10, letterSpacing: '0.18em', color: DIM }}>
        Detectors · {count}{count !== total && ` / ${total}`}
      </div>
      <div style={{ display: 'flex', gap: 18, alignItems: 'baseline', flexWrap: 'wrap' }}>
        <ToolbarGroup label="Sort">
          <ToolbarBtn active={sort === 'gates'} onClick={() => onSortChange('gates')}>
            gates
          </ToolbarBtn>
          <ToolbarBtn active={sort === 'pnl'} onClick={() => onSortChange('pnl')}>
            PnL 7d
          </ToolbarBtn>
          <ToolbarBtn active={sort === 'fill_rate'} onClick={() => onSortChange('fill_rate')}>
            fill rate
          </ToolbarBtn>
          <ToolbarBtn active={sort === 'days'} onClick={() => onSortChange('days')}>
            days
          </ToolbarBtn>
        </ToolbarGroup>
        <ToolbarGroup label="Filter">
          <ToolbarBtn active={filter === 'all'} onClick={() => onFilterChange('all')}>
            all
          </ToolbarBtn>
          <ToolbarBtn active={filter === 'ready'} onClick={() => onFilterChange('ready')}>
            ready 5/5
          </ToolbarBtn>
          <ToolbarBtn active={filter === 'accumulating'} onClick={() => onFilterChange('accumulating')}>
            accumulating
          </ToolbarBtn>
        </ToolbarGroup>
      </div>
    </div>
  );
}

function ToolbarGroup({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div style={{ display: 'inline-flex', gap: 6, alignItems: 'baseline' }}>
      <span
        className="ed-kicker"
        style={{ fontSize: 9, color: FAINT, letterSpacing: '0.16em', marginRight: 4 }}
      >
        {label}
      </span>
      {children}
    </div>
  );
}

function ToolbarBtn({
  active,
  onClick,
  children,
}: {
  active: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      style={{
        padding: '6px 12px',
        background: active ? `${STAGE}15` : 'transparent',
        border: `1px solid ${active ? `${STAGE}50` : RULE}`,
        color: active ? STAGE : DIM,
        fontSize: 10,
        fontFamily: 'var(--ed-mono)',
        letterSpacing: '0.04em',
        cursor: 'pointer',
        transition: 'all 150ms',
        minHeight: 32,
      }}
    >
      {children}
    </button>
  );
}

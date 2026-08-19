import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import {
  usePromotionReadiness,
  useV2Activity,
} from '../../../hooks/useBot';
import type { Stage, BotTab } from '../../../hooks/useBot';
import { EditorialSkeleton } from '../editorial/EditorialSkeleton';
import { LifecycleProgress } from '../lifecycle/LifecycleProgress';
import { StageHero } from '../lifecycle/StageHero';
import { PromotionMap } from '../lifecycle/shadow/PromotionMap';
import { PromotionCard } from '../lifecycle/shadow/PromotionCard';
import { PromotionExpand } from '../lifecycle/shadow/PromotionExpand';
import { ActivityToggle } from '../lifecycle/shadow/ActivityToggle';
import {
  applyFilter,
  compareItems,
  type ShadowSortKey,
  type ShadowFilterKey,
} from '../lifecycle/shadow/utils';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';
const RULE = 'var(--ed-rule)';
const STAGE = '#5b9dff';

/**
 * StatusTerminal — Shadow stage Promotion Center (Hybrid C layout).
 *
 * Layout:
 *   1. LifecycleProgress (общий)
 *   2. StageHero "Shadow · Promotion Center" (blue) с 3 KPI
 *   3. PromotionMap — hero scatter (gates passed × honest $/day)
 *   4. ActivityToggle — heatmap/bars view (14d/60d/90d)
 *   5. Toolbar (Sort/Filter)
 *   6. PromotionCardsGrid — рich card per detector с visible 3 gates
 *   7. PromotionExpand — inline drill-down с per-bucket WR vs canonical
 *
 * См. plans/wiggly-nibbling-stardust.md (Hybrid C plan).
 */
export function StatusTerminal() {
  const [, setSearchParams] = useSearchParams();
  const { data: readiness, isLoading } = usePromotionReadiness();
  const { data: activity } = useV2Activity(14);
  const [openDetector, setOpenDetector] = useState<string | null>(null);
  const [sort, setSort] = useState<ShadowSortKey>('gates');
  const [filter, setFilter] = useState<ShadowFilterKey>('all');

  const switchStage = (s: Stage) => {
    setSearchParams((p) => {
      const next = new URLSearchParams(p);
      next.set('stage', s as BotTab);
      return next;
    }, { replace: true });
  };

  const items = useMemo(() => readiness?.detectors ?? [], [readiness?.detectors]);

  const sortedAndFiltered = useMemo(() => {
    const filtered = applyFilter(items, filter);
    return [...filtered].sort((a, b) => compareItems(a, b, sort));
  }, [items, sort, filter]);

  // KPI для StageHero
  const collectingCount = items.filter((i) => i.shadow_7d.distinct_events > 0).length;
  const readyCount = items.filter((i) => i.ready).length;
  const totalPnl = activity?.summary.total_pnl_net_usd ?? 0;
  const pnlColor = totalPnl > 0 ? UP : totalPnl < 0 ? DOWN : undefined;

  const scrollToCard = (detector: string) => {
    const el = document.getElementById(`shadow-card-${detector}`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      setOpenDetector(detector);
    }
  };

  if (isLoading) {
    return <EditorialSkeleton label="loading shadow" />;
  }

  return (
    <div>
      <LifecycleProgress current="shadow" onSelect={switchStage} />
      <StageHero
        stage="shadow"
        title="Promotion Center · Shadow → Virtual"
        subtitle="Кто из активных детекторов реально работает на live данных и готов к Virtual? Map ниже выводит всех в (gates passed × honest $/day) пространство — точки в lane 3/3 в правом-верхнем углу — promote-кандидаты сейчас."
        kpis={[
          {
            label: 'Detectors collecting',
            value: `${collectingCount}/${items.length}`,
            sub: 'эмитят signals 7d',
          },
          {
            label: 'Ready to promote',
            value: String(readyCount),
            sub: '3/3 gates passed',
            valueColor: readyCount > 0 ? UP : DIM,
          },
          {
            label: 'PnL if executed · 14d',
            value: `${totalPnl >= 0 ? '+' : '−'}$${Math.abs(totalPnl).toFixed(2)}`,
            sub: 'shadow trading на бумаге',
            valueColor: pnlColor,
          },
        ]}
      />

      <PromotionMap items={items} isLoading={isLoading} onDotClick={scrollToCard} />

      <ActivityToggle />

      <div style={{ maxWidth: 1180, margin: '0 auto', padding: '0 22px 56px' }}>
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
                  id={`shadow-card-${item.detector}`}
                  style={{ scrollMarginTop: 100 }}
                >
                  <PromotionCard
                    item={item}
                    expanded={isOpen}
                    onToggle={() => setOpenDetector(isOpen ? null : item.detector)}
                  />
                </div>
              );
            })}
          </div>
        )}

        {openDetector && (
          <div style={{ border: `1px solid ${STAGE}40`, marginBottom: 36 }}>
            <PromotionExpand detector={openDetector} />
          </div>
        )}

        <p style={{ color: FAINT, fontSize: 10, marginTop: 18, lineHeight: 1.6 }}>
          Promotion Shadow → Virtual только manual через TG callback (см.{' '}
          <code>docs/detector_lifecycle.md</code>). UI показывает critriia, не выполняет действие.
          Gate 1 = ≥30 distinct events 7d. Gate 2 = recent WR в ±5pp от canonical. Gate 3 =
          backtest verdict = PASS.
        </p>
      </div>
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
  sort: ShadowSortKey;
  onSortChange: (s: ShadowSortKey) => void;
  filter: ShadowFilterKey;
  onFilterChange: (f: ShadowFilterKey) => void;
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
          <ToolbarBtn active={sort === 'honest'} onClick={() => onSortChange('honest')}>
            honest $/d
          </ToolbarBtn>
          <ToolbarBtn active={sort === 'wr'} onClick={() => onSortChange('wr')}>
            recent WR
          </ToolbarBtn>
          <ToolbarBtn active={sort === 'events'} onClick={() => onSortChange('events')}>
            events 7d
          </ToolbarBtn>
        </ToolbarGroup>
        <ToolbarGroup label="Filter">
          <ToolbarBtn active={filter === 'all'} onClick={() => onFilterChange('all')}>
            all
          </ToolbarBtn>
          <ToolbarBtn active={filter === 'ready'} onClick={() => onFilterChange('ready')}>
            ready 3/3
          </ToolbarBtn>
          <ToolbarBtn active={filter === 'collecting'} onClick={() => onFilterChange('collecting')}>
            collecting
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

import { useMemo, useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useBacktestMap, useV2Detectors } from '../../../hooks/useBot';
import type { Stage, BotTab, BacktestMapItem } from '../../../hooks/useBot';
import { LifecycleProgress } from './LifecycleProgress';
import { StageHero } from './StageHero';
import { BacktestMap } from './backtest/BacktestMap';
import { BacktestCard } from './backtest/BacktestCard';
import { BacktestExpand } from './backtest/BacktestExpand';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const UP = 'var(--ed-up)';
const RULE = 'var(--ed-rule)';
const STAGE = '#9b6cff';

type SortKey = 'honest5k' | 'verdict' | 'trades';
type FilterKey = 'all' | 'pass_only' | 'with_doc';

/**
 * BacktestTerminal — Mission Control для решения "promote или нет".
 *
 * Layout (см. plans/wiggly-nibbling-stardust.md):
 *   1. LifecycleProgress (общий)
 *   2. StageHero (purple) с 3 KPI
 *   3. BacktestMap — hero scatter (haircut × honest $/day)
 *   4. Sort/filter toolbar
 *   5. DetectorsGrid — карточки + inline expand
 */
export function BacktestTerminal() {
  const [, setSearchParams] = useSearchParams();
  const { data: mapData, isLoading: mapLoading } = useBacktestMap();
  const { data: allDetectorsData } = useV2Detectors();
  const [openDetector, setOpenDetector] = useState<string | null>(null);
  const [sort, setSort] = useState<SortKey>('honest5k');
  const [filter, setFilter] = useState<FilterKey>('all');

  const switchStage = (s: Stage) => {
    setSearchParams((p) => {
      const next = new URLSearchParams(p);
      next.set('stage', s as BotTab);
      return next;
    }, { replace: true });
  };

  // Объединяем mapData (детекторы с MD-файлами) + all detectors из БД
  // (детекторы без MD показываем серыми карточками "no doc").
  const mergedItems = useMemo<BacktestMapItem[]>(() => {
    const mapItems = mapData?.items ?? [];
    const allDetectors = allDetectorsData?.detectors ?? [];
    const inMap = new Set(mapItems.map((i) => i.detector));
    const placeholders: BacktestMapItem[] = allDetectors
      .filter((d) => !inMap.has(d.detector))
      .map((d) => ({
        detector: d.detector,
        verdict: null,
        honest_daily_usd_at_5k: null,
        honest_daily_usd_at_1k: null,
        honest_haircut_factor: null,
        trades_per_day: null,
        roi_net_pct: null,
        wr_pct: null,
        n_signals: null,
        backtest_days: null,
        stage: (d.lifecycle?.stage ?? null) as Stage | null,
      }));
    return [...mapItems, ...placeholders];
  }, [mapData, allDetectorsData]);

  const sortedAndFiltered = useMemo(() => {
    let list = mergedItems;
    if (filter === 'pass_only') {
      list = list.filter((i) => i.verdict === 'PASS');
    } else if (filter === 'with_doc') {
      list = list.filter((i) => i.verdict != null);
    }
    return [...list].sort((a, b) => {
      if (sort === 'honest5k') {
        return (b.honest_daily_usd_at_5k ?? -Infinity) - (a.honest_daily_usd_at_5k ?? -Infinity);
      }
      if (sort === 'trades') {
        return (b.trades_per_day ?? 0) - (a.trades_per_day ?? 0);
      }
      // verdict: PASS > MARGINAL > FAIL > null
      const order: Record<string, number> = { PASS: 0, MARGINAL: 1, FAIL: 2 };
      const av = a.verdict ? order[a.verdict] ?? 3 : 3;
      const bv = b.verdict ? order[b.verdict] ?? 3 : 3;
      return av - bv;
    });
  }, [mergedItems, sort, filter]);

  const passCount = mergedItems.filter((i) => i.verdict === 'PASS').length;
  const totalDoc = mergedItems.filter((i) => i.verdict != null).length;
  const sumHonest = mergedItems.reduce(
    (s, i) => s + (i.honest_daily_usd_at_5k ?? 0),
    0,
  );

  const scrollToCard = (detector: string) => {
    const el = document.getElementById(`backtest-card-${detector}`);
    if (el) {
      el.scrollIntoView({ behavior: 'smooth', block: 'center' });
      setOpenDetector(detector);
    }
  };

  return (
    <div>
      <LifecycleProgress current="backtest" onSelect={switchStage} />
      <StageHero
        stage="backtest"
        title="Mission Control · canonical 821d"
        subtitle="Какой детектор promote-ить завтра, какой через неделю, какой убить? Map ниже выводит всех в (haircut × honest $/day) пространство — точки в правом-верхнем углу готовы к Shadow → Virtual."
        kpis={[
          {
            label: 'Detectors with backtest',
            value: `${totalDoc}/${mergedItems.length}`,
            sub: 'docs/backtests/{det}.md',
          },
          {
            label: 'PASS verdict',
            value: String(passCount),
            sub: 'прошли все critical gates',
            valueColor: passCount > 0 ? UP : DIM,
          },
          {
            label: 'Sum honest $/day @ $5K',
            value: `${sumHonest >= 0 ? '+' : '−'}$${Math.abs(sumHonest).toFixed(0)}`,
            sub: 'если запустить ВСЕ PASS',
            valueColor: sumHonest > 0 ? UP : DIM,
          },
        ]}
      />

      <BacktestMap items={mergedItems} isLoading={mapLoading} onDotClick={scrollToCard} />

      <div style={{ maxWidth: 1180, margin: '0 auto', padding: '24px 22px' }}>
        <Toolbar
          count={sortedAndFiltered.length}
          total={mergedItems.length}
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
              gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))',
              gap: 14,
              marginBottom: 36,
            }}
          >
            {sortedAndFiltered.map((item) => {
              const isOpen = openDetector === item.detector;
              const hasDoc = item.verdict != null;
              return (
                <div
                  key={item.detector}
                  id={`backtest-card-${item.detector}`}
                  style={{ scrollMarginTop: 100 }}
                >
                  <BacktestCard
                    item={item}
                    expanded={isOpen}
                    hasDoc={hasDoc}
                    onToggle={() => setOpenDetector(isOpen ? null : item.detector)}
                  />
                </div>
              );
            })}
          </div>
        )}

        {openDetector && (
          <div style={{ border: `1px solid ${STAGE}40`, marginBottom: 36 }}>
            <BacktestExpand detector={openDetector} />
          </div>
        )}

        <p style={{ color: FAINT, fontSize: 10, marginTop: 18, lineHeight: 1.6 }}>
          Honest $/day = canonical backtest × honest_haircut_factor. Haircut учитывает разницу
          фильтров (lifetime_volume_usd vs production volume_24h) и slippage realism.
          Source: <code>docs/backtests/{'{detector}'}.md</code>.
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
  sort: SortKey;
  onSortChange: (s: SortKey) => void;
  filter: FilterKey;
  onFilterChange: (f: FilterKey) => void;
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
      }}
    >
      <div
        className="ed-kicker"
        style={{ fontSize: 10, letterSpacing: '0.18em', color: DIM }}
      >
        Detectors · {count}{count !== total && ` / ${total}`}
      </div>
      <div style={{ display: 'flex', gap: 18, alignItems: 'baseline', flexWrap: 'wrap' }}>
        <ToolbarGroup label="Sort">
          <ToolbarBtn active={sort === 'honest5k'} onClick={() => onSortChange('honest5k')}>
            honest $/d
          </ToolbarBtn>
          <ToolbarBtn active={sort === 'verdict'} onClick={() => onSortChange('verdict')}>
            verdict
          </ToolbarBtn>
          <ToolbarBtn active={sort === 'trades'} onClick={() => onSortChange('trades')}>
            trades/d
          </ToolbarBtn>
        </ToolbarGroup>
        <ToolbarGroup label="Filter">
          <ToolbarBtn active={filter === 'all'} onClick={() => onFilterChange('all')}>
            all
          </ToolbarBtn>
          <ToolbarBtn active={filter === 'with_doc'} onClick={() => onFilterChange('with_doc')}>
            with doc
          </ToolbarBtn>
          <ToolbarBtn active={filter === 'pass_only'} onClick={() => onFilterChange('pass_only')}>
            PASS
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

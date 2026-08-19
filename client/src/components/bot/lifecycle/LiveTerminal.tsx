import { useState } from 'react';
import { useSearchParams } from 'react-router-dom';
import { useLiveDashboard, useLiveReadiness } from '../../../hooks/useBot';
import type { Stage, BotTab } from '../../../hooks/useBot';
import { LifecycleProgress } from './LifecycleProgress';
import { StageHero } from './StageHero';
import { DiagnosticsBanner } from './live/DiagnosticsBanner';
import { EquityCurve } from './live/EquityCurve';
import { SummaryStrip } from './live/SummaryStrip';
import { OpenPositionsPanel } from './live/OpenPositionsPanel';
import { LiveCard } from './live/LiveCard';
import { LiveExpand } from './live/LiveExpand';
import { VirtualEmptyState } from './virtual/VirtualEmptyState';
import { fmtUsd, pnlColor, STAGE_RED } from './live/utils';

const FAINT = 'var(--ed-ink-faint)';

/**
 * LiveTerminal — Live Trading Floor (operational dashboard).
 *
 * Иерархия: экран отвечает за 5 секунд на «зарабатывает ли бот и всё ли ок?».
 * Hero = 2 KPI (Live PnL 7d + Today PnL), диагностика — только при проблеме
 * (DiagnosticsBanner), остальное подчинено.
 */
export function LiveTerminal() {
  const [, setSearchParams] = useSearchParams();
  const { data, isLoading } = useLiveDashboard();
  const { data: readiness } = useLiveReadiness();
  const [expandedDetector, setExpandedDetector] = useState<string | null>(null);

  const switchStage = (s: Stage) => {
    setSearchParams(
      (p) => {
        const next = new URLSearchParams(p);
        next.set('stage', s as BotTab);
        return next;
      },
      { replace: true },
    );
  };

  if (isLoading) {
    return (
      <div>
        <LifecycleProgress current="live" onSelect={switchStage} />
        <div style={{ padding: '40px 22px', color: FAINT, fontSize: 12, textAlign: 'center' }}>
          Загрузка trading floor…
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div>
        <LifecycleProgress current="live" onSelect={switchStage} />
        <div style={{ padding: '40px 22px', color: FAINT, fontSize: 12, textAlign: 'center' }}>
          Нет данных live_dashboard. Проверь backend.
        </div>
      </div>
    );
  }

  const { bankroll, equity_curve_7d, alerts, open_positions, fill_quality_7d, detectors, execution_enabled } = data;
  const lastPoint = equity_curve_7d.length > 0 ? equity_curve_7d[equity_curve_7d.length - 1] : null;
  const todayPnl = lastPoint?.live_pnl_day ?? 0;
  const totalLivePnl = lastPoint?.live_pnl_cum ?? 0;
  const deltaVsVirtual = lastPoint !== null ? lastPoint.live_pnl_cum - lastPoint.virtual_baseline_cum : null;

  const shadowCandidates = readiness?.candidates_from_shadow ?? [];

  return (
    <div>
      <LifecycleProgress current="live" onSelect={switchStage} />

      <StageHero
        stage="live"
        title="Real Money · Trading Floor"
        subtitle="Финал pipeline: реальные ордера в Polymarket CLOB. Watch the curve, watch the alerts."
        kpis={[
          {
            label: 'Live PnL · 7d',
            value: fmtUsd(totalLivePnl, { sign: true }),
            sub:
              deltaVsVirtual !== null
                ? `Δ vs virtual ${fmtUsd(deltaVsVirtual, { sign: true })}`
                : 'cumulative real-money',
            valueColor: pnlColor(totalLivePnl),
          },
          {
            label: 'Today PnL',
            value: fmtUsd(todayPnl, { sign: true }),
            sub: 'last day',
            valueColor: pnlColor(todayPnl),
          },
        ]}
      />

      <DiagnosticsBanner data={data} />

      <EquityCurve data={equity_curve_7d} />

      <SummaryStrip bankroll={bankroll} fillQuality={fill_quality_7d} alerts={alerts} />

      <OpenPositionsPanel open={open_positions} />

      {/* Per-detector cards or empty state */}
      {detectors.length === 0 ? (
        <div style={{ marginTop: 8 }}>
          <VirtualEmptyState candidates={shadowCandidates} executionEnabled={execution_enabled} />
        </div>
      ) : (
        <section style={{ maxWidth: 1180, margin: '0 auto 56px', padding: '0 22px' }}>
          <div
            className="ed-kicker"
            style={{
              fontSize: 10,
              letterSpacing: '0.22em',
              color: STAGE_RED,
              fontWeight: 600,
              textTransform: 'uppercase',
              marginBottom: 14,
            }}
          >
            Per-detector live performance · 7d
          </div>
          <div
            style={{
              display: 'grid',
              gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))',
              gap: 18,
              marginBottom: 18,
            }}
          >
            {detectors.map((d) => (
              <LiveCard
                key={d.detector}
                item={d}
                isExpanded={expandedDetector === d.detector}
                onClick={() =>
                  setExpandedDetector(expandedDetector === d.detector ? null : d.detector)
                }
              />
            ))}
          </div>

          {expandedDetector !== null && (() => {
            const item = detectors.find((d) => d.detector === expandedDetector);
            return item ? <LiveExpand item={item} /> : null;
          })()}
        </section>
      )}
    </div>
  );
}

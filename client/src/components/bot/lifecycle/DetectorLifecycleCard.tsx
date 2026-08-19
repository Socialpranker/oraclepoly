import { useDetectorBacktest, useV2Detectors } from '../../../hooks/useBot';
import StageBadge from '../StageBadge';
import type { Stage } from '../../../hooks/useBot';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';

interface Props {
  detector: string;
}

/**
 * DetectorLifecycleCard — 4-stage lifecycle map для одного детектора.
 *
 *   📊 Backtest    Shadow         Virtual       Live
 *   $X/day         N signals/d   $Y PnL 7d    $Z PnL 7d
 *   PASS/FAIL      collected     paper exec    real $$$
 *
 * Текущий stage детектора подсвечивается яркой рамкой.
 * См. docs/detector_lifecycle.md.
 */
export function DetectorLifecycleCard({ detector }: Props) {
  const { data: detectorsData } = useV2Detectors();
  const { data: backtest } = useDetectorBacktest(detector);

  const detData = detectorsData?.detectors.find((d) => d.detector === detector);
  const lc = detData?.lifecycle;
  const currentStage: Stage = (lc?.stage as Stage) ?? 'shadow';

  return (
    <div
      style={{
        border: `1px solid ${FAINT}`,
        padding: 18,
        marginBottom: 20,
        background: 'rgba(255,255,255,0.01)',
      }}
    >
      <div className="flex items-center justify-between mb-3">
        <h3 className="ed-display" style={{ fontSize: 16, fontWeight: 500 }}>
          Lifecycle
        </h3>
        <StageBadge stage={currentStage} size="sm" />
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(4, 1fr)',
          gap: 12,
        }}
      >
        <StageColumn
          stage="backtest"
          isCurrent={false}
          highlight
          label="Backtest"
          subtitle={`canonical · ${backtest?.backtest_days ?? '—'}d hist`}
          metric={
            backtest?.honest_daily_usd_at_5k != null
              ? `$${backtest.honest_daily_usd_at_5k.toFixed(2)}/d @$5K`
              : '—'
          }
          status={backtest?.verdict ?? '—'}
          statusColor={
            backtest?.verdict === 'PASS' ? UP : backtest?.verdict === 'FAIL' ? DOWN : FAINT
          }
          note={backtest?.honest_haircut_reason ?? ''}
        />

        <StageColumn
          stage="shadow"
          isCurrent={currentStage === 'shadow'}
          label="Shadow"
          subtitle="signals в БД, no money"
          metric={
            lc?.shadow.signals_per_day_7d != null
              ? `${lc.shadow.signals_per_day_7d.toFixed(1)} signals/d`
              : '—'
          }
          status={
            currentStage === 'shadow'
              ? 'collecting'
              : currentStage === 'virtual' || currentStage === 'live'
              ? 'graduated'
              : '—'
          }
          statusColor={DIM}
          note="last 7d"
        />

        <StageColumn
          stage="virtual"
          isCurrent={currentStage === 'virtual'}
          label="Virtual"
          subtitle="paper exec, full pipeline"
          metric={
            lc?.virtual.pnl_usd_7d != null
              ? `$${lc.virtual.pnl_usd_7d.toFixed(2)} PnL 7d`
              : '—'
          }
          status={
            currentStage === 'virtual'
              ? 'active'
              : currentStage === 'live'
              ? 'graduated'
              : 'waiting'
          }
          statusColor={
            currentStage === 'virtual' || currentStage === 'live' ? DIM : FAINT
          }
          note={lc?.virtual.pnl_usd_7d != null && lc.virtual.pnl_usd_7d > 0 ? UP : ''}
        />

        <StageColumn
          stage="live"
          isCurrent={currentStage === 'live'}
          label="Live"
          subtitle="реальные ордера"
          metric={
            lc?.live.pnl_usd_7d != null
              ? `$${lc.live.pnl_usd_7d.toFixed(2)} PnL 7d`
              : '—'
          }
          status={currentStage === 'live' ? 'active' : 'disabled'}
          statusColor={currentStage === 'live' ? UP : FAINT}
          note=""
        />
      </div>

      {backtest?.honest_haircut_reason && (
        <p style={{ color: FAINT, fontSize: 10, marginTop: 14, lineHeight: 1.6 }}>
          ⓘ Honest haircut: {backtest.honest_haircut_reason}
        </p>
      )}
    </div>
  );
}

interface ColProps {
  stage: Stage;
  isCurrent: boolean;
  highlight?: boolean;
  label: string;
  subtitle: string;
  metric: string;
  status: string;
  statusColor: string;
  note: string;
}

function StageColumn({
  stage,
  isCurrent,
  highlight,
  label,
  subtitle,
  metric,
  status,
  statusColor,
}: ColProps) {
  const stageColors: Record<Stage, string> = {
    backtest: '#9b6cff',
    shadow: '#5b9dff',
    virtual: '#e0b020',
    live: '#e04444',
    paused: FAINT,
  };
  const accent = stageColors[stage];
  const bordered = isCurrent || highlight;
  return (
    <div
      style={{
        padding: 12,
        border: bordered ? `1px solid ${accent}` : `1px solid rgba(255,255,255,0.05)`,
        background: bordered ? `${accent}10` : 'transparent',
      }}
    >
      <div
        className="ed-mono"
        style={{
          fontSize: 9,
          textTransform: 'uppercase',
          letterSpacing: '0.16em',
          color: accent,
          marginBottom: 4,
        }}
      >
        {label}
        {isCurrent && ' · сейчас'}
      </div>
      <div style={{ fontSize: 10, color: FAINT, marginBottom: 8 }}>{subtitle}</div>
      <div
        style={{
          fontSize: 14,
          fontWeight: 500,
          color: 'var(--ed-ink)',
          marginBottom: 4,
        }}
      >
        {metric}
      </div>
      <div className="ed-mono" style={{ fontSize: 9, color: statusColor, letterSpacing: '0.10em', textTransform: 'uppercase' }}>
        {status}
      </div>
    </div>
  );
}

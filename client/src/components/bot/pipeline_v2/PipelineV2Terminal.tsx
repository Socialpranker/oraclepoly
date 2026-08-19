import {
  useEnsembleOpportunities,
  useV2Detectors,
  useV2GateStatus,
  useV2Model,
  useV2RecentSignals,
  useV2SignalRate,
  type EnsemblePair,
  type V2DetectorRow,
  type V2GateRow,
  type V2ModelInfo,
  type V2RecentSignal,
  type V2SignalRateRow,
} from '../../../hooks/useBot';
import { EditorialSkeleton } from '../editorial/EditorialSkeleton';
// ShadowFunnel + ShadowDailyPnL переехали в StatusTerminal (Status tab).
// Здесь — только dev-инсайты: модели, калибровка, recent signals.

const DETECTOR_LABELS: Record<string, string> = {
  D3_cross_mkt_arb: 'D3 cross arb',
  D5_long_horizon_extreme_fade: 'D5 long extreme',
  D7b_no_favorite_fade: 'D7b no favorite',
  D10b_election_ladder: 'D10b ladder',
};

const STATE_LABELS: Record<V2GateRow['state'], string> = {
  shadow_collecting: 'collecting',
  shadow_watch: 'watch',
  gate_evaluating: 'gate ready',
  mini_live: 'mini-live',
  live_full: 'live',
  disabled: 'disabled',
};

const STATE_COLORS: Record<V2GateRow['state'], string> = {
  shadow_collecting: 'var(--ed-ink-faint)',
  shadow_watch: 'var(--ed-ink-dim)',
  gate_evaluating: 'var(--ed-accent)',
  mini_live: 'var(--ed-accent)',
  live_full: 'var(--ed-accent)',
  disabled: 'var(--ed-down)',
};

const ACCENT = 'var(--ed-accent)';
const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';

/**
 * PipelineV2Terminal — фокус на моделях и калибрации.
 *
 * Day-to-day P&L переехал в Dry tab. Здесь — что внутри pipeline:
 *  - The Stack (active L2 modelmeta)
 *  - Signal flow (rate health)
 *  - Calibration quality (per-detector L1 Brier)
 *  - Ensemble opportunities (correlation + WR на парах)
 *  - Promotion gate
 *  - Recent signals (50 latest)
 */
export function PipelineV2Terminal() {
  const { data: detectorsData, isLoading: detLoading } = useV2Detectors();
  const { data: modelData } = useV2Model();
  const { data: signalsData } = useV2RecentSignals(50);
  const { data: signalRateData } = useV2SignalRate(7);
  const { data: gateData } = useV2GateStatus();
  const { data: ensembleData } = useEnsembleOpportunities();

  if (detLoading || !detectorsData) {
    return <EditorialSkeleton label="loading pipeline v2" />;
  }

  return (
    <div
      className="ed-rise"
      style={{ padding: '20px 22px 40px', maxWidth: 1180, margin: '0 auto' }}
    >
      <V2Stack model={modelData} />
      {signalRateData && <V2SignalRate rates={signalRateData.detectors} />}
      <V2CalibrationQuality detectors={detectorsData.detectors} />
      {ensembleData && ensembleData.pairs.length > 0 && (
        <V2EnsembleOpportunities pairs={ensembleData.pairs} />
      )}
      {gateData && <V2PromotionGate detectors={gateData.detectors} />}
      {signalsData && <V2SignalsTable signals={signalsData.signals} />}
    </div>
  );
}

// ============ The Stack — active L2 model snapshot ============

function V2Stack({ model }: { model: V2ModelInfo | undefined }) {
  const a = model?.active;
  const fittedAgo = a?.fit_at ? formatRelative(a.fit_at) : null;

  return (
    <section style={{ marginBottom: 24 }}>
      <div className="ed-kicker" style={{ color: FAINT, marginBottom: 8 }}>
        The Stack
      </div>
      <div
        className="ed-mono"
        style={{ color: DIM, fontSize: 12, marginBottom: 14 }}
      >
        detectors → L1 Beta calibrators → L2 LightGBM → Layer A → Layer B → EV-sizer
      </div>

      {!a ? (
        <div
          className="ed-mono"
          style={{
            color: FAINT,
            fontSize: 12,
            padding: '14px 0',
            opacity: 0.6,
          }}
        >
          No active L2 model · fit cron Sun 04:00 UTC
        </div>
      ) : (
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))',
            gap: 16,
            background: 'var(--ed-bg-raised)',
            border: '1px solid var(--ed-rule)',
            padding: '14px 16px',
          }}
        >
          <Stat label="active version" value={a.version} mono valueSize={11} />
          <Stat label="train n" value={a.n_train.toLocaleString()} />
          <Stat label="train Brier" value={a.train_brier.toFixed(4)} />
          <Stat
            label="holdout Brier"
            value={a.holdout_brier.toFixed(4)}
            color={
              a.holdout_brier < 0.22
                ? 'var(--ed-up)'
                : a.holdout_brier > 0.28
                ? 'var(--ed-down)'
                : 'var(--ed-ink)'
            }
          />
          <Stat label="promoted" value={fittedAgo ?? '—'} />
        </div>
      )}
    </section>
  );
}

// ============ Signal Rate (health) ============

function V2SignalRate({ rates }: { rates: V2SignalRateRow[] }) {
  if (!rates.length) return null;
  const stale = rates.filter((r) => r.is_stale).length;
  const healthy = rates.length - stale;

  return (
    <section style={{ marginBottom: 24 }}>
      <div className="ed-kicker" style={{ color: FAINT, marginBottom: 4 }}>
        Signal flow · 7d
      </div>
      <div
        className="ed-mono"
        style={{ color: DIM, fontSize: 11, marginBottom: 12 }}
      >
        {healthy}/{rates.length} detectors emitting
      </div>
      <div style={{ display: 'flex', gap: 18, flexWrap: 'wrap' }}>
        {rates.map((r) => {
          const lbl = DETECTOR_LABELS[r.detector] ?? r.detector;
          const ageHours = r.minutes_since_last / 60;
          const ageStr =
            ageHours < 1
              ? `${Math.round(r.minutes_since_last)}m`
              : `${ageHours.toFixed(1)}h`;
          return (
            <div key={r.detector} style={{ minWidth: 130 }}>
              <div
                className="ed-kicker"
                style={{ color: FAINT, fontSize: 9, marginBottom: 4 }}
              >
                {lbl}
              </div>
              <div
                className="ed-mono"
                style={{
                  fontSize: 18,
                  fontWeight: 500,
                  color: r.is_stale ? 'var(--ed-down)' : 'var(--ed-ink)',
                }}
              >
                {r.rate_per_hour.toFixed(1)}
                <span style={{ fontSize: 10, color: FAINT, marginLeft: 3 }}>
                  /h
                </span>
              </div>
              <div
                className="ed-mono"
                style={{
                  fontSize: 10,
                  color: r.is_stale
                    ? 'var(--ed-down)'
                    : ageHours < 2
                    ? 'var(--ed-ink-dim)'
                    : FAINT,
                }}
              >
                last {ageStr} ago
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ============ Calibration Quality ============

function V2CalibrationQuality({ detectors }: { detectors: V2DetectorRow[] }) {
  const hasAnyFitted = detectors.some((d) => d.calibrator.fitted);
  return (
    <section style={{ marginBottom: 24 }}>
      <div className="ed-kicker" style={{ color: FAINT, marginBottom: 4 }}>
        Calibration quality
      </div>
      <div
        className="ed-mono"
        style={{ color: DIM, fontSize: 11, marginBottom: 12 }}
      >
        per-detector L1 Beta · Brier holdout · raw → calibrated
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 70px 80px 80px 70px',
          rowGap: 4,
          columnGap: 10,
          alignItems: 'baseline',
        }}
      >
        <Header>detector</Header>
        <Header align="right">n train</Header>
        <Header align="right">Brier raw</Header>
        <Header align="right">Brier holdout</Header>
        <Header align="right">drift</Header>

        {detectors.map((d) => {
          const cal = d.calibrator;
          const lbl = DETECTOR_LABELS[d.detector] ?? d.detector;
          if (!cal.fitted) {
            return (
              <Row
                key={d.detector}
                cols={[
                  <span style={{ color: 'var(--ed-ink)' }}>{lbl}</span>,
                  <span style={{ color: FAINT }} className="ed-mono">
                    —
                  </span>,
                  <span style={{ color: FAINT }} className="ed-mono">
                    sparse
                  </span>,
                  <span style={{ color: FAINT }} className="ed-mono">
                    sparse
                  </span>,
                  <span style={{ color: FAINT }} className="ed-mono">
                    —
                  </span>,
                ]}
              />
            );
          }
          const drift = (cal.brier_raw ?? 0) - (cal.brier_holdout ?? 0);
          const holdoutColor =
            cal.brier_holdout != null && cal.brier_holdout < 0.20
              ? 'var(--ed-up)'
              : cal.brier_holdout != null && cal.brier_holdout > 0.25
              ? 'var(--ed-down)'
              : 'var(--ed-ink)';
          return (
            <Row
              key={d.detector}
              cols={[
                <span style={{ color: 'var(--ed-ink)' }}>{lbl}</span>,
                <span className="ed-mono" style={{ color: DIM }}>
                  {cal.n_train?.toLocaleString() ?? '—'}
                </span>,
                <span className="ed-mono" style={{ color: DIM }}>
                  {cal.brier_raw?.toFixed(3) ?? '—'}
                </span>,
                <span className="ed-mono" style={{ color: holdoutColor }}>
                  {cal.brier_holdout?.toFixed(3) ?? '—'}
                </span>,
                <span
                  className="ed-mono"
                  style={{
                    color:
                      Math.abs(drift) < 0.005
                        ? 'var(--ed-up)'
                        : Math.abs(drift) > 0.02
                        ? 'var(--ed-down)'
                        : DIM,
                  }}
                >
                  {drift >= 0 ? '+' : ''}
                  {drift.toFixed(3)}
                </span>,
              ]}
            />
          );
        })}
      </div>

      {!hasAnyFitted && (
        <div
          className="ed-mono"
          style={{ color: FAINT, fontSize: 11, marginTop: 8, opacity: 0.7 }}
        >
          No calibrators fitted yet · cron ml_calibrator_fit runs daily 03:00 UTC
        </div>
      )}
    </section>
  );
}

// ============ Ensemble Opportunities ============

function V2EnsembleOpportunities({ pairs }: { pairs: EnsemblePair[] }) {
  return (
    <section style={{ marginBottom: 24 }}>
      <div className="ed-kicker" style={{ color: FAINT, marginBottom: 4 }}>
        Ensemble opportunities
      </div>
      <div
        className="ed-mono"
        style={{ color: DIM, fontSize: 11, marginBottom: 12 }}
      >
        markets where 2+ detectors agree · historical co-occurrence
      </div>

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '1fr 90px 70px 90px',
          rowGap: 4,
          columnGap: 10,
          alignItems: 'baseline',
        }}
      >
        <Header>pair</Header>
        <Header align="right">markets</Header>
        <Header align="right">WR</Header>
        <Header align="right">net ROI</Header>

        {pairs.map((p) => {
          const a = DETECTOR_LABELS[p.det_a] ?? p.det_a;
          const b = DETECTOR_LABELS[p.det_b] ?? p.det_b;
          const wrColor =
            p.wr_pct >= 70
              ? 'var(--ed-up)'
              : p.wr_pct < 55
              ? 'var(--ed-down)'
              : 'var(--ed-ink)';
          const roiColor =
            p.net_roi_pct > 100
              ? 'var(--ed-accent)'
              : p.net_roi_pct > 0
              ? 'var(--ed-up)'
              : 'var(--ed-down)';
          return (
            <Row
              key={`${p.det_a}+${p.det_b}`}
              cols={[
                <span style={{ color: 'var(--ed-ink)' }}>
                  {a} <span style={{ color: FAINT }}>+</span> {b}
                </span>,
                <span className="ed-mono" style={{ color: DIM }}>
                  {p.markets.toLocaleString()}
                </span>,
                <span className="ed-mono" style={{ color: wrColor }}>
                  {p.wr_pct.toFixed(0)}%
                </span>,
                <span className="ed-mono" style={{ color: roiColor }}>
                  +{p.net_roi_pct.toFixed(0)}%
                </span>,
              ]}
            />
          );
        })}
      </div>
    </section>
  );
}

// ============ Promotion Gate ============

function V2PromotionGate({ detectors }: { detectors: V2GateRow[] }) {
  if (!detectors.length) return null;

  const ready = detectors.filter((d) => d.state === 'gate_evaluating').length;

  return (
    <section style={{ marginBottom: 24 }}>
      <div className="ed-kicker" style={{ color: FAINT, marginBottom: 4 }}>
        Promotion gate
      </div>
      <div
        className="ed-mono"
        style={{ color: DIM, fontSize: 11, marginBottom: 12 }}
      >
        {ready}/{detectors.length} ready · effective n = events
      </div>

      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 8,
        }}
      >
        {detectors.map((d) => {
          const lbl = DETECTOR_LABELS[d.detector] ?? d.detector;
          const stateColor = STATE_COLORS[d.state];
          const stateLbl = STATE_LABELS[d.state];
          const pct = Math.min(100, d.progress_pct);

          return (
            <div
              key={d.detector}
              style={{
                display: 'grid',
                gridTemplateColumns: '160px 1fr 80px 60px',
                alignItems: 'center',
                gap: 14,
              }}
            >
              <div>
                <div
                  className="ed-mono"
                  style={{ fontSize: 12, color: 'var(--ed-ink)' }}
                >
                  {lbl}
                </div>
                <div
                  className="ed-kicker"
                  style={{ fontSize: 9, color: stateColor, marginTop: 2 }}
                >
                  {stateLbl}
                </div>
              </div>

              <div>
                <div
                  style={{
                    position: 'relative',
                    height: 4,
                    background: 'var(--ed-rule)',
                    overflow: 'hidden',
                  }}
                >
                  <div
                    style={{
                      position: 'absolute',
                      top: 0,
                      left: 0,
                      bottom: 0,
                      width: `${pct}%`,
                      background: stateColor,
                      transition: 'width 0.3s',
                    }}
                  />
                </div>
                <div
                  className="ed-mono"
                  style={{ fontSize: 10, color: FAINT, marginTop: 4 }}
                >
                  {d.distinct_events} / {d.required_n} events ·{' '}
                  {d.distinct_markets} mkts · {d.distinct_days}d
                </div>
              </div>

              <div
                className="ed-mono"
                style={{
                  textAlign: 'right',
                  fontSize: 12,
                  color:
                    d.pnl_usd > 0.5
                      ? 'var(--ed-up)'
                      : d.pnl_usd < -0.5
                      ? 'var(--ed-down)'
                      : FAINT,
                }}
              >
                {d.pnl_usd >= 0 ? '+' : '−'}${Math.abs(d.pnl_usd).toFixed(0)}
              </div>

              <div
                className="ed-mono"
                style={{ textAlign: 'right', fontSize: 11, color: DIM }}
              >
                {d.wr != null ? `${(d.wr * 100).toFixed(0)}%` : '—'}
              </div>
            </div>
          );
        })}
      </div>
    </section>
  );
}

// ============ Recent Signals ============

function V2SignalsTable({ signals }: { signals: V2RecentSignal[] }) {
  if (!signals.length) {
    return (
      <section style={{ marginBottom: 24 }}>
        <div className="ed-kicker" style={{ color: FAINT, marginBottom: 8 }}>
          Recent signals
        </div>
        <div
          className="ed-mono"
          style={{ color: FAINT, fontSize: 11, opacity: 0.6 }}
        >
          No signals yet — pipeline runs every 5 min.
        </div>
      </section>
    );
  }

  return (
    <section style={{ marginBottom: 24 }}>
      <div className="ed-kicker" style={{ color: FAINT, marginBottom: 4 }}>
        Recent signals · {signals.length}
      </div>
      <div
        className="ed-mono"
        style={{ color: DIM, fontSize: 11, marginBottom: 12 }}
      >
        latest virtual & live placements
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table
          style={{
            width: '100%',
            fontSize: 11,
            borderCollapse: 'collapse',
          }}
          className="ed-mono"
        >
          <thead>
            <tr
              style={{
                borderBottom: '1px solid var(--ed-rule)',
                color: FAINT,
                fontSize: 9,
                textTransform: 'uppercase',
                letterSpacing: '0.06em',
              }}
            >
              <th style={{ textAlign: 'left', padding: '6px 6px' }}>time</th>
              <th style={{ textAlign: 'left', padding: '6px 6px' }}>det</th>
              <th style={{ textAlign: 'left', padding: '6px 6px' }}>cat</th>
              <th style={{ textAlign: 'right', padding: '6px 6px' }}>entry</th>
              <th style={{ textAlign: 'right', padding: '6px 6px' }}>meta_p</th>
              <th style={{ textAlign: 'right', padding: '6px 6px' }}>EV</th>
              <th style={{ textAlign: 'right', padding: '6px 6px' }}>stake</th>
              <th style={{ textAlign: 'center', padding: '6px 6px' }}>
                outcome
              </th>
              <th style={{ textAlign: 'right', padding: '6px 6px' }}>PnL</th>
            </tr>
          </thead>
          <tbody>
            {signals.map((s) => {
              const opened = new Date(s.opened_at).toISOString().slice(11, 16);
              const detShort = s.detector.split('_')[0];
              const outcomeColor =
                s.outcome === 'WIN'
                  ? 'var(--ed-up)'
                  : s.outcome === 'LOSS'
                  ? 'var(--ed-down)'
                  : FAINT;
              const pnlColor =
                s.realized_pnl_usd != null && s.realized_pnl_usd > 0
                  ? 'var(--ed-up)'
                  : s.realized_pnl_usd != null && s.realized_pnl_usd < 0
                  ? 'var(--ed-down)'
                  : FAINT;
              return (
                <tr
                  key={s.signal_id}
                  style={{
                    borderBottom: '1px solid var(--ed-rule)',
                  }}
                >
                  <td style={{ padding: '5px 6px', color: DIM }}>{opened}</td>
                  <td style={{ padding: '5px 6px' }}>{detShort}</td>
                  <td style={{ padding: '5px 6px', color: DIM }}>
                    {s.category ?? '—'}
                  </td>
                  <td style={{ textAlign: 'right', padding: '5px 6px' }}>
                    {s.entry_price.toFixed(3)}
                  </td>
                  <td
                    style={{
                      textAlign: 'right',
                      padding: '5px 6px',
                      fontWeight: 500,
                    }}
                  >
                    {s.meta_p.toFixed(3)}
                  </td>
                  <td
                    style={{
                      textAlign: 'right',
                      padding: '5px 6px',
                      color: s.ev_pct >= 0 ? ACCENT : FAINT,
                    }}
                  >
                    {s.ev_pct >= 0 ? '+' : ''}
                    {s.ev_pct.toFixed(0)}
                  </td>
                  <td
                    style={{
                      textAlign: 'right',
                      padding: '5px 6px',
                      color: DIM,
                    }}
                  >
                    ${s.stake_usd.toFixed(0)}
                  </td>
                  <td
                    style={{
                      textAlign: 'center',
                      padding: '5px 6px',
                      color: outcomeColor,
                      fontWeight: 500,
                    }}
                  >
                    {s.outcome ?? '—'}
                  </td>
                  <td
                    style={{
                      textAlign: 'right',
                      padding: '5px 6px',
                      color: pnlColor,
                    }}
                  >
                    {s.realized_pnl_usd != null
                      ? `${s.realized_pnl_usd >= 0 ? '+' : '−'}$${Math.abs(
                          s.realized_pnl_usd
                        ).toFixed(2)}`
                      : '—'}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

// ============ Helpers ============

function Stat({
  label,
  value,
  color = 'var(--ed-ink)',
  mono,
  valueSize = 14,
}: {
  label: string;
  value: string;
  color?: string;
  mono?: boolean;
  valueSize?: number;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      <span
        className="ed-kicker"
        style={{ color: FAINT, fontSize: 9 }}
      >
        {label}
      </span>
      <span
        className={mono ? 'ed-mono' : 'ed-mono'}
        style={{
          fontSize: valueSize,
          fontWeight: 500,
          color,
          wordBreak: mono ? 'break-all' : 'normal',
        }}
      >
        {value}
      </span>
    </div>
  );
}

function Header({
  children,
  align = 'left',
}: {
  children: React.ReactNode;
  align?: 'left' | 'right';
}) {
  return (
    <span
      className="ed-kicker"
      style={{ color: FAINT, fontSize: 9, textAlign: align }}
    >
      {children}
    </span>
  );
}

function Row({ cols }: { cols: React.ReactNode[] }) {
  return (
    <>
      {cols.map((c, i) => (
        <span
          key={i}
          className="ed-mono"
          style={{
            fontSize: 11,
            textAlign: i === 0 ? 'left' : 'right',
            paddingTop: 2,
            paddingBottom: 2,
            borderBottom: '1px solid var(--ed-rule)',
          }}
        >
          {c}
        </span>
      ))}
    </>
  );
}

function formatRelative(iso: string): string {
  const ms = Date.now() - new Date(iso).getTime();
  if (ms < 0) return 'just now';
  const h = ms / 3_600_000;
  if (h < 1) return `${Math.round(h * 60)}m ago`;
  if (h < 48) return `${Math.round(h)}h ago`;
  return `${Math.round(h / 24)}d ago`;
}

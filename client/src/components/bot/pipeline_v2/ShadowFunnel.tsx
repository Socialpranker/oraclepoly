import { useV2Activity, useV2SignalRate } from '../../../hooks/useBot';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const ACCENT = 'var(--ed-accent)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';

/**
 * ShadowFunnel — воронка пути сигнала через shadow за период (default 7d).
 *
 * Показывает обывателю:
 * 1. Сколько сигналов детекторы сгенерили
 * 2. Сколько прошли EV-gate (попали в shadow)
 * 3. Сколько уже резолвлены (WIN/LOSS)
 * 4. Какой суммарный net PnL (с fee+spread, реальный stake) получился
 *
 * Источник signals_emitted: useV2SignalRate (rate × hours).
 * Источник остального: useV2Activity.summary (real net PnL).
 */
export function ShadowFunnel({ days = 7 }: { days?: number }) {
  const { data: activity, isLoading: activityLoading } = useV2Activity(days);
  const { data: rate, isLoading: rateLoading } = useV2SignalRate(days);

  if (activityLoading || rateLoading) {
    return (
      <section style={{ marginBottom: 24 }}>
        <div className="ed-kicker" style={{ color: FAINT, marginBottom: 8 }}>
          Shadow funnel · {days}d
        </div>
        <div
          className="ed-mono"
          style={{ color: FAINT, fontSize: 11, padding: '20px 0', opacity: 0.6 }}
        >
          loading…
        </div>
      </section>
    );
  }

  const hours = days * 24;
  const totalEmitted = Math.round(
    (rate?.detectors ?? []).reduce((sum, d) => sum + d.rate_per_hour * hours, 0),
  );
  const sh = activity?.summary;
  const placed = sh?.total_placed ?? 0;
  const resolved = sh?.total_settled ?? 0;
  const wins = sh?.total_wins ?? 0;
  const losses = sh?.total_losses ?? 0;
  const pnl = sh?.total_pnl_net_usd ?? 0;
  const pnlGross = sh?.total_pnl_gross_usd ?? 0;
  const stakedUsd = sh?.total_staked_usd ?? 0;

  // Steps: emitted → ev_pass(=placed) → resolved → win/loss → pnl
  const passRate = totalEmitted > 0 ? (placed / totalEmitted) * 100 : 0;
  const resolveRate = placed > 0 ? (resolved / placed) * 100 : 0;
  const wr = wins + losses > 0 ? (wins / (wins + losses)) * 100 : null;

  // Bar widths normalized to widest stage
  const maxN = Math.max(totalEmitted, placed, resolved, 1);
  const barW = (n: number) => Math.max(2, (n / maxN) * 100);

  const pnlColor = pnl > 0.5 ? UP : pnl < -0.5 ? DOWN : FAINT;

  return (
    <section style={{ marginBottom: 24 }}>
      <div className="ed-kicker" style={{ color: FAINT, marginBottom: 4 }}>
        Shadow funnel · {days}d
      </div>
      <div className="ed-mono" style={{ color: DIM, fontSize: 11, marginBottom: 14 }}>
        путь сигнала: detector emit → EV gate → shadow → outcome → net PnL
      </div>

      <div
        style={{
          display: 'flex',
          flexDirection: 'column',
          gap: 4,
          background: 'var(--ed-bg-raised)',
          border: '1px solid var(--ed-rule)',
          padding: '18px 18px 16px',
        }}
      >
        <FunnelStage
          label="emitted"
          subLabel="raw сигналы детекторов"
          value={totalEmitted.toLocaleString()}
          width={barW(totalEmitted)}
          barColor={FAINT}
          showArrow
        />
        <FunnelStage
          label="ev gate pass"
          subLabel={`${passRate.toFixed(1)}% от emitted · попали в shadow`}
          value={placed.toLocaleString()}
          width={barW(placed)}
          barColor={DIM}
          showArrow
        />
        <FunnelStage
          label="resolved"
          subLabel={`${resolveRate.toFixed(1)}% от placed · outcome известен`}
          value={resolved.toLocaleString()}
          width={barW(resolved)}
          barColor={ACCENT}
          showArrow
        />
        <FunnelStage
          label="wins / losses"
          subLabel={wr != null ? `WR ${wr.toFixed(1)}%` : 'не хватает данных'}
          value={`${wins.toLocaleString()} / ${losses.toLocaleString()}`}
          width={barW(wins + losses)}
          splitWidth={wins + losses > 0 ? (wins / (wins + losses)) * barW(wins + losses) : 0}
          dual
        />

        <div
          style={{
            marginTop: 10,
            paddingTop: 12,
            borderTop: '1px solid var(--ed-rule)',
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'baseline',
          }}
        >
          <div>
            <div className="ed-kicker" style={{ color: FAINT, fontSize: 9 }}>
              PnL net · {days}d
            </div>
            <div
              className="ed-mono"
              style={{ fontSize: 11, color: DIM, marginTop: 2 }}
            >
              gross {pnlGross >= 0 ? '+' : '−'}${Math.abs(pnlGross).toFixed(2)} · staked $
              {stakedUsd.toFixed(0)}
            </div>
          </div>
          <div
            className="ed-display"
            style={{
              fontSize: 32,
              fontWeight: 400,
              letterSpacing: '-0.025em',
              color: pnlColor,
              lineHeight: 1,
            }}
          >
            {pnl >= 0 ? '+' : '−'}${Math.abs(pnl).toFixed(2)}
          </div>
        </div>
      </div>
    </section>
  );
}

function FunnelStage({
  label,
  subLabel,
  value,
  width,
  barColor = DIM,
  splitWidth = 0,
  dual = false,
  showArrow = false,
}: {
  label: string;
  subLabel: string;
  value: string;
  width: number;
  barColor?: string;
  splitWidth?: number;
  dual?: boolean;
  showArrow?: boolean;
}) {
  return (
    <>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: '120px 1fr 90px',
          alignItems: 'center',
          gap: 14,
        }}
      >
        <div>
          <div
            className="ed-kicker"
            style={{ fontSize: 9, color: FAINT, marginBottom: 2 }}
          >
            {label}
          </div>
          <div className="ed-mono" style={{ fontSize: 9, color: FAINT, opacity: 0.7 }}>
            {subLabel}
          </div>
        </div>
        <div
          style={{
            position: 'relative',
            height: 14,
            background: 'transparent',
          }}
        >
          {dual ? (
            <>
              <div
                style={{
                  position: 'absolute',
                  inset: 0,
                  width: `${width}%`,
                  background: DOWN,
                  opacity: 0.7,
                }}
              />
              <div
                style={{
                  position: 'absolute',
                  inset: 0,
                  width: `${splitWidth}%`,
                  background: UP,
                }}
              />
            </>
          ) : (
            <div
              style={{
                position: 'absolute',
                inset: 0,
                width: `${width}%`,
                background: barColor,
                transition: 'width 0.4s cubic-bezier(0.2, 0.8, 0.3, 1)',
              }}
            />
          )}
        </div>
        <div
          className="ed-mono"
          style={{
            textAlign: 'right',
            fontSize: 14,
            fontWeight: 500,
            color: 'var(--ed-ink)',
          }}
        >
          {value}
        </div>
      </div>
      {showArrow && (
        <div
          className="ed-mono"
          style={{
            fontSize: 10,
            color: FAINT,
            paddingLeft: 134,
            opacity: 0.5,
            lineHeight: 1,
          }}
        >
          ↓
        </div>
      )}
    </>
  );
}

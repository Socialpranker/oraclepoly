import type { BotStrategy, LiveMetricStrategy } from '../../../hooks/useBot';

interface Props {
  strategy: BotStrategy;
  liveMetric?: LiveMetricStrategy;
  mode: 'dry' | 'live';
}

function phaseFromResolved(n: number): { label: string; target: number } {
  if (n < 10) return { label: 'warmup', target: 10 };
  if (n < 30) return { label: 'probe', target: 30 };
  if (n < 100) return { label: 'scale', target: 100 };
  return { label: 'mature', target: n };
}

export function DetailAnatomy({ strategy, liveMetric, mode }: Props) {
  const isLive = mode === 'live' && liveMetric;
  const phase = isLive
    ? phaseFromResolved(liveMetric.live_resolved)
    : phaseFromResolved(strategy.resolved_count);

  const kelly = isLive ? liveMetric.kelly_fraction : null;
  const damping = isLive ? liveMetric.damping : null;
  const safety = isLive ? liveMetric.safety_mult : null;

  const dailySpent = strategy.daily_spent ?? 0;
  const dailyLimit = strategy.daily_limit ?? 0;
  const quotaPct = Math.min(100, (dailySpent / Math.max(1, dailyLimit)) * 100);

  return (
    <section className="relative px-5 py-7 ed-grain">
      {/* Vertical epigraph */}
      <span
        className="ed-pull absolute top-8"
        style={{ right: 10 }}
      >
        the anatomy · phase {phase.label}
      </span>

      <div className="flex items-center gap-2 mb-5">
        <span className="ed-kicker">The Anatomy</span>
        <hr className="ed-rule flex-1" />
        <span
          className="ed-chip"
          data-tone={isLive ? 'gold' : undefined}
          style={{ textTransform: 'uppercase' }}
        >
          {phase.label}
        </span>
      </div>

      {/* Asymmetric 3-column fact cards */}
      <div className="grid grid-cols-3 gap-3 ed-rise mr-8">
        <FactCard
          kicker="Kelly"
          value={kelly !== null && kelly !== undefined ? (kelly * 100).toFixed(1) : '—'}
          unit={kelly !== null && kelly !== undefined ? '%' : ''}
          caption={isLive ? 'live f*' : 'dry only'}
        />
        <FactCard
          kicker="Damping"
          value={damping !== null && damping !== undefined ? damping.toFixed(2) : '—'}
          caption={isLive ? 'uncertainty' : 'not live'}
        />
        <FactCard
          kicker="Safety"
          value={safety !== null && safety !== undefined ? safety.toFixed(2) : '—'}
          unit="×"
          caption={isLive ? 'phase mult' : 'not live'}
        />
      </div>

      {/* Bankroll + quota bar */}
      <div className="mt-7 mr-8">
        <div className="flex items-baseline justify-between mb-2">
          <span className="ed-kicker">Bankroll allocation</span>
          <span
            className="ed-mono"
            style={{ fontSize: 10, color: 'var(--ed-ink-faint)' }}
          >
            ${dailySpent.toFixed(0)} / ${dailyLimit.toFixed(0)} daily
          </span>
        </div>
        <div
          style={{
            height: 4,
            background: 'var(--ed-rule)',
            position: 'relative',
            overflow: 'hidden',
          }}
        >
          <div
            style={{
              position: 'absolute',
              inset: 0,
              width: `${quotaPct}%`,
              background:
                quotaPct > 85
                  ? 'var(--ed-down)'
                  : quotaPct > 60
                    ? 'var(--ed-accent)'
                    : 'var(--ed-up)',
              transition: 'width 0.6s cubic-bezier(0.22, 1, 0.36, 1)',
            }}
          />
        </div>
        <div className="flex justify-between mt-1">
          <span
            className="ed-mono"
            style={{ fontSize: 10, color: 'var(--ed-ink-faint)' }}
          >
            bankroll ${(strategy.bankroll ?? 0).toFixed(0)}
          </span>
          <span
            className="ed-mono"
            style={{ fontSize: 10, color: 'var(--ed-ink-faint)' }}
          >
            frozen ${(strategy.frozen ?? 0).toFixed(0)}
          </span>
        </div>
      </div>

      {/* Phase progress (to next threshold) */}
      <div className="mt-6 mr-8">
        <div className="flex items-baseline justify-between mb-2">
          <span className="ed-kicker">Progress to {phase.label === 'mature' ? 'mature' : 'next phase'}</span>
          <span
            className="ed-mono"
            style={{ fontSize: 10, color: 'var(--ed-ink-faint)' }}
          >
            {isLive ? liveMetric.live_resolved : strategy.resolved_count} / {phase.target}
          </span>
        </div>
        <div style={{ display: 'flex', gap: 3 }}>
          {Array.from({ length: Math.min(phase.target, 40) }).map((_, i) => {
            const resolved = isLive ? liveMetric.live_resolved : strategy.resolved_count;
            const cellsTotal = Math.min(phase.target, 40);
            const filledCells = Math.min(cellsTotal, Math.round((resolved / phase.target) * cellsTotal));
            const active = i < filledCells;
            return (
              <span
                key={i}
                style={{
                  flex: 1,
                  height: 10,
                  background: active
                    ? phase.label === 'mature'
                      ? 'var(--ed-up)'
                      : 'var(--ed-accent)'
                    : 'var(--ed-rule)',
                  opacity: active ? 0.85 : 1,
                }}
              />
            );
          })}
        </div>
      </div>

      <hr className="ed-rule-strong mt-8" />
    </section>
  );
}

function FactCard({
  kicker,
  value,
  unit,
  caption,
}: {
  kicker: string;
  value: string;
  unit?: string;
  caption: string;
}) {
  return (
    <div
      style={{
        borderTop: '1px solid var(--ed-rule-strong)',
        paddingTop: 10,
      }}
    >
      <div className="ed-kicker" style={{ fontSize: 9, marginBottom: 8 }}>
        {kicker}
      </div>
      <div className="flex items-baseline gap-0.5">
        <span
          className="ed-display"
          style={{
            fontSize: 32,
            lineHeight: 0.9,
            fontWeight: 500,
            color: 'var(--ed-ink)',
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {value}
        </span>
        {unit && (
          <span
            className="ed-display"
            style={{
              fontSize: 16,
              color: 'var(--ed-ink-dim)',
              fontStyle: 'italic',
              fontWeight: 400,
            }}
          >
            {unit}
          </span>
        )}
      </div>
      <div
        className="ed-mono"
        style={{ fontSize: 9, color: 'var(--ed-ink-faint)', marginTop: 4, letterSpacing: '0.04em' }}
      >
        {caption}
      </div>
    </div>
  );
}

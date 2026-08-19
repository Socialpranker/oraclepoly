import type { BotStrategy } from '../../hooks/useBot';

const SOURCE_LABELS: Record<string, { label: string; color: string }> = {
  oracle: { label: 'Oracle', color: '#9E7AFF' },
  contra_bias: { label: 'Contra', color: '#FF9A3D' },
  market_baseline: { label: 'Baseline', color: '#888' },
};

const SIGNIFICANCE_THRESHOLD = 30;

function SignificanceBar({ resolved }: { resolved: number }) {
  const pct = Math.min(resolved / SIGNIFICANCE_THRESHOLD, 1) * 100;
  const significant = resolved >= SIGNIFICANCE_THRESHOLD;

  return (
    <div className="mt-1.5">
      <div className="flex justify-between items-center mb-0.5">
        <span className="text-[9px] uppercase tracking-widest text-[var(--color-text-dim)]">
          Статзначимость
        </span>
        <span
          className="text-[9px]"
          style={{ color: significant ? '#00FF85' : 'var(--color-text-dim)' }}
        >
          {significant ? `✓ ${resolved} resolved` : `${resolved}/${SIGNIFICANCE_THRESHOLD} resolved`}
        </span>
      </div>
      <div className="h-0.5 bg-white/10 rounded-full overflow-hidden">
        <div
          className="h-full rounded-full transition-all"
          style={{
            width: `${pct}%`,
            backgroundColor: significant ? '#00FF85' : '#9E7AFF66',
          }}
        />
      </div>
    </div>
  );
}

export function StrategyCard({
  strategy,
  onClick,
}: {
  strategy: BotStrategy;
  onClick?: () => void;
}) {
  const isNeg = strategy.earned < 0;
  const hasDrawdown = strategy.drawdown_pct > 15;
  const src = SOURCE_LABELS[strategy.signal_source] ?? SOURCE_LABELS.oracle;

  return (
    <button
      onClick={onClick}
      className="w-full text-left rounded-xl p-3 bg-white/5 hover:bg-white/8 transition-colors"
    >
      {/* Header row */}
      <div className="flex justify-between items-start">
        <div className="flex items-center gap-2">
          <span className="text-sm font-medium">{strategy.key}</span>
          {hasDrawdown && (
            <span className="text-[10px]" style={{ color: '#FF6A3D' }}>
              ▼{strategy.drawdown_pct.toFixed(0)}%
            </span>
          )}
        </div>
        <span
          className="text-[10px] px-1.5 py-0.5 rounded-full font-medium"
          style={{
            backgroundColor: src.color + '22',
            color: src.color,
          }}
        >
          {src.label}
        </span>
      </div>

      {/* P&L + ROI + Quota */}
      <div className="flex gap-4 mt-1.5">
        <div>
          <div className="text-[10px] text-[var(--color-text-dim)]">P&L</div>
          <div
            className="text-base font-bold"
            style={{ color: isNeg ? '#FF3B30' : '#00FF85' }}
          >
            {isNeg ? '' : '+'}${strategy.earned.toFixed(2)}
          </div>
        </div>
        <div>
          <div className="text-[10px] text-[var(--color-text-dim)]">ROI</div>
          <div
            className="text-base font-bold"
            style={{ color: isNeg ? '#FF3B30' : '#00FF85' }}
          >
            {strategy.roi_pct > 0 ? '+' : ''}{strategy.roi_pct.toFixed(1)}%
          </div>
        </div>
        <div>
          <div className="text-[10px] text-[var(--color-text-dim)]">Quota</div>
          <div className="text-base font-bold">${strategy.quota.toFixed(0)}</div>
        </div>
      </div>

      {/* Stats row */}
      <div className="flex gap-3 mt-1 text-[10px] text-[var(--color-text-dim)]">
        <span>
          Open: {strategy.open_positions}
          {strategy.pyramid_positions > 0 && (
            <span className="ml-1 text-[9px] text-[var(--color-accent)] font-medium">
              ▲{strategy.pyramid_positions}
            </span>
          )}
        </span>
        <span>WR: {strategy.all_time.win_rate.toFixed(0)}%</span>
        <span>PF: {strategy.all_time.profit_factor.toFixed(2)}</span>
        <span>Bets: {strategy.all_time.total_bets}</span>
      </div>

      {/* Statistical significance */}
      <SignificanceBar resolved={strategy.resolved_count} />
    </button>
  );
}

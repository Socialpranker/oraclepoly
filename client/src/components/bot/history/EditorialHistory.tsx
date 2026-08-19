import { useMemo, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { ArrowLeft } from 'lucide-react';
import { useBotDailyPnL, useBotRecentFills } from '../../../hooks/useBot';
import type { DailyPnLPoint } from '../../../hooks/useBot';

const PERIODS = [
  { label: '7D', days: 7 },
  { label: '14D', days: 14 },
  { label: '30D', days: 30 },
  { label: '90D', days: 90 },
];

function formatDate(iso: string): string {
  const d = new Date(iso + 'T00:00:00Z');
  return d.toLocaleDateString('en-US', { day: '2-digit', month: 'short', timeZone: 'UTC' });
}

function buildEquityPath(points: DailyPnLPoint[], w: number, h: number, pad = 4) {
  if (points.length < 2) return { line: '', area: '' };
  const cum: number[] = [];
  let running = 0;
  for (const p of points) {
    running += p.pnl;
    cum.push(running);
  }
  const min = Math.min(0, ...cum);
  const max = Math.max(0, ...cum);
  const range = max - min || 1;
  const step = (w - pad * 2) / (points.length - 1);
  const coords = cum.map((v, i) => ({
    x: pad + i * step,
    y: pad + (h - pad * 2) * (1 - (v - min) / range),
  }));
  const line = coords.map((c, i) => `${i === 0 ? 'M' : 'L'}${c.x.toFixed(1)},${c.y.toFixed(1)}`).join(' ');
  const last = coords[coords.length - 1];
  const first = coords[0];
  const area = `${line} L${last.x.toFixed(1)},${h - pad} L${first.x.toFixed(1)},${h - pad} Z`;
  return { line, area, zeroY: pad + (h - pad * 2) * (1 - (0 - min) / range) };
}

export function EditorialHistory() {
  const navigate = useNavigate();
  const [days, setDays] = useState(14);
  const { data: daily, isLoading } = useBotDailyPnL(days, 'live');
  const { data: fills } = useBotRecentFills(50, 'live');

  const stats = useMemo(() => {
    if (!daily?.points.length) return null;
    const resolvedDays = daily.points.filter((p) => p.bets_settled > 0);
    const best = resolvedDays.length
      ? resolvedDays.reduce((acc, p) => (p.pnl > acc.pnl ? p : acc))
      : null;
    const worst = resolvedDays.length
      ? resolvedDays.reduce((acc, p) => (p.pnl < acc.pnl ? p : acc))
      : null;

    // Win/loss streak
    let streak = 0;
    const sortedFills = [...(fills?.fills ?? [])].sort((a, b) => {
      const ta = new Date(a.closed_at ?? a.opened_at).getTime();
      const tb = new Date(b.closed_at ?? b.opened_at).getTime();
      return tb - ta;
    });
    if (sortedFills.length) {
      const firstIsWin = sortedFills[0].pnl > 0;
      for (const f of sortedFills) {
        if ((f.pnl > 0) === firstIsWin) streak++;
        else break;
      }
      if (!firstIsWin) streak = -streak;
    }

    return { best, worst, streak };
  }, [daily, fills]);

  if (isLoading) {
    return (
      <div
        className="editorial min-h-screen flex items-center justify-center"
        style={{ background: 'var(--ed-bg)' }}
      >
        <span className="ed-kicker" style={{ color: 'var(--ed-ink-faint)' }}>
          Loading…
        </span>
      </div>
    );
  }

  const totals = daily?.totals;
  const up = (totals?.pnl ?? 0) >= 0;
  const equity = daily ? buildEquityPath(daily.points, 640, 160, 4) : { line: '', area: '', zeroY: 0 };

  return (
    <div
      className="editorial min-h-screen pb-10"
      style={{ background: 'var(--ed-bg)', color: 'var(--ed-ink)', fontFamily: 'var(--ed-body)' }}
    >
      {/* Header */}
      <div
        className="px-5 py-5 flex items-center gap-3"
        style={{ borderBottom: '1px solid var(--ed-rule-strong)' }}
      >
        <button
          onClick={() => navigate('/bot')}
          className="flex items-center gap-1"
          style={{ color: 'var(--ed-ink-dim)', fontSize: 12, fontFamily: 'var(--ed-body)' }}
        >
          <ArrowLeft size={14} />
          <span>Bot</span>
        </button>
        <div className="flex-1" />
        <span className="ed-kicker">Live · History</span>
      </div>

      {/* Hero total */}
      <section className="px-5 pt-6 pb-4">
        <div className="flex items-baseline gap-5 flex-wrap">
          <div>
            <div className="ed-kicker mb-1">Net over {days} days</div>
            <div
              className="ed-display"
              style={{
                fontSize: 48,
                fontWeight: 500,
                color: up ? 'var(--ed-up)' : 'var(--ed-down)',
                lineHeight: 1,
                fontVariantNumeric: 'tabular-nums',
              }}
            >
              {totals
                ? `${up ? '+' : '−'}$${Math.abs(totals.pnl).toFixed(2)}`
                : '—'}
            </div>
          </div>

          <div className="flex items-baseline gap-5">
            <MetricLarge
              label="Win rate"
              value={totals ? `${totals.win_rate.toFixed(0)}%` : '—'}
              sub={totals ? `${totals.wins}W/${totals.losses}L` : undefined}
            />
            <MetricLarge label="Bets" value={totals ? totals.bets_placed.toString() : '—'} />
            <MetricLarge
              label="Turnover"
              value={totals ? `$${totals.total_stake.toFixed(0)}` : '—'}
            />
          </div>
        </div>

        {/* Period switch */}
        <div className="flex gap-4 mt-5">
          {PERIODS.map((p) => (
            <button
              key={p.days}
              onClick={() => setDays(p.days)}
              className="ed-mono"
              style={{
                fontSize: 11,
                color: days === p.days ? 'var(--ed-accent)' : 'var(--ed-ink-faint)',
                paddingBottom: 4,
                borderBottom: days === p.days ? '1px solid var(--ed-accent)' : '1px solid transparent',
                letterSpacing: '0.1em',
              }}
            >
              {p.label}
            </button>
          ))}
        </div>
      </section>

      {/* Equity curve */}
      {daily && daily.points.length >= 2 && (
        <section className="px-5 py-5" style={{ borderTop: '1px solid var(--ed-rule)' }}>
          <div className="ed-kicker mb-3">Equity curve · cumulative PnL</div>
          <svg viewBox="0 0 640 160" className="w-full" style={{ height: 160 }} preserveAspectRatio="none">
            <defs>
              <linearGradient id="ed-eq-fill" x1="0" x2="0" y1="0" y2="1">
                <stop
                  offset="0%"
                  stopColor={up ? 'var(--ed-up)' : 'var(--ed-down)'}
                  stopOpacity="0.18"
                />
                <stop
                  offset="100%"
                  stopColor={up ? 'var(--ed-up)' : 'var(--ed-down)'}
                  stopOpacity="0"
                />
              </linearGradient>
            </defs>
            {equity.zeroY !== undefined && (
              <line
                x1="0"
                x2="640"
                y1={equity.zeroY}
                y2={equity.zeroY}
                stroke="var(--ed-rule)"
                strokeDasharray="2 3"
                strokeWidth="0.5"
              />
            )}
            <path d={equity.area} fill="url(#ed-eq-fill)" />
            <path
              d={equity.line}
              fill="none"
              stroke={up ? 'var(--ed-up)' : 'var(--ed-down)'}
              strokeWidth="1.4"
              strokeLinejoin="round"
            />
          </svg>
        </section>
      )}

      {/* Highlights */}
      {stats && (
        <section
          className="px-5 py-5 grid grid-cols-3 gap-4"
          style={{ borderTop: '1px solid var(--ed-rule)' }}
        >
          <Highlight
            label="Best day"
            value={stats.best ? `+$${stats.best.pnl.toFixed(2)}` : '—'}
            sub={stats.best ? formatDate(stats.best.date) : undefined}
            color="var(--ed-up)"
          />
          <Highlight
            label="Worst day"
            value={stats.worst ? `−$${Math.abs(stats.worst.pnl).toFixed(2)}` : '—'}
            sub={stats.worst ? formatDate(stats.worst.date) : undefined}
            color="var(--ed-down)"
          />
          <Highlight
            label="Streak"
            value={
              stats.streak === 0
                ? '—'
                : stats.streak > 0
                  ? `${stats.streak}W`
                  : `${Math.abs(stats.streak)}L`
            }
            color={stats.streak > 0 ? 'var(--ed-up)' : stats.streak < 0 ? 'var(--ed-down)' : undefined}
          />
        </section>
      )}

      {/* Daily breakdown */}
      {daily && daily.points.length > 0 && (
        <section style={{ borderTop: '1px solid var(--ed-rule)' }}>
          <div className="px-5 py-3 flex items-baseline justify-between">
            <span className="ed-kicker">By day</span>
            <span className="ed-mono" style={{ fontSize: 10, color: 'var(--ed-ink-faint)' }}>
              {daily.points.length} days
            </span>
          </div>
          <ul>
            {[...daily.points].reverse().map((p, i) => {
              const pnlColor =
                p.pnl > 0 ? 'var(--ed-up)' : p.pnl < 0 ? 'var(--ed-down)' : 'var(--ed-ink-dim)';
              return (
                <li
                  key={p.date}
                  className="px-5 py-2.5 flex items-baseline gap-3"
                  style={{ borderTop: i === 0 ? 'none' : '1px solid var(--ed-rule)' }}
                >
                  <span
                    className="ed-mono"
                    style={{ fontSize: 11, color: 'var(--ed-ink-dim)', minWidth: 54 }}
                  >
                    {formatDate(p.date)}
                  </span>
                  <span
                    className="ed-mono"
                    style={{ fontSize: 11, color: 'var(--ed-ink-faint)' }}
                  >
                    {p.bets_placed} bets
                  </span>
                  <span className="flex-1" />
                  <span
                    className="ed-mono"
                    style={{ fontSize: 11, color: 'var(--ed-ink-faint)' }}
                  >
                    {p.bets_settled > 0 ? `${p.wins}/${p.losses}` : '—'}
                  </span>
                  <span
                    className="ed-mono"
                    style={{ fontSize: 13, color: pnlColor, fontWeight: 500, minWidth: 78, textAlign: 'right' }}
                  >
                    {p.bets_settled > 0
                      ? `${p.pnl > 0 ? '+' : p.pnl < 0 ? '−' : '±'}$${Math.abs(p.pnl).toFixed(2)}`
                      : '—'}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {/* Trade log */}
      {fills?.fills && fills.fills.length > 0 && (
        <section style={{ borderTop: '1px solid var(--ed-rule)' }}>
          <div className="px-5 py-3 flex items-baseline justify-between">
            <span className="ed-kicker">Trade log</span>
            <span className="ed-mono" style={{ fontSize: 10, color: 'var(--ed-ink-faint)' }}>
              {fills.fills.length}
            </span>
          </div>
          <ul>
            {fills.fills.map((f, i) => {
              const pnl = f.pnl ?? 0;
              const color =
                pnl > 0 ? 'var(--ed-up)' : pnl < 0 ? 'var(--ed-down)' : 'var(--ed-ink-dim)';
              const t = new Date(f.closed_at ?? f.opened_at);
              return (
                <li
                  key={f.id}
                  className="px-5 py-2.5 flex items-start gap-3"
                  style={{ borderTop: i === 0 ? 'none' : '1px solid var(--ed-rule)' }}
                >
                  <div className="ed-mono" style={{ fontSize: 10, color: 'var(--ed-ink-faint)', minWidth: 88 }}>
                    <div>{t.toLocaleDateString('en-US', { day: '2-digit', month: 'short' })}</div>
                    <div>{t.toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', hour12: false })}</div>
                  </div>
                  <span
                    className="ed-mono uppercase"
                    style={{
                      fontSize: 9,
                      color: f.side === 'YES' ? 'var(--ed-up)' : 'var(--ed-down)',
                      letterSpacing: '0.15em',
                      minWidth: 26,
                      marginTop: 2,
                    }}
                  >
                    {f.side}
                  </span>
                  <div className="flex-1 min-w-0">
                    <div
                      className="ed-body truncate"
                      style={{ fontSize: 12, color: 'var(--ed-ink)' }}
                      title={f.market_question ?? undefined}
                    >
                      {f.market_question || <em>—</em>}
                    </div>
                    <div
                      className="ed-mono truncate"
                      style={{ fontSize: 10, color: 'var(--ed-ink-faint)', marginTop: 1 }}
                    >
                      {f.strategy_key} · ${f.stake.toFixed(2)} @ {f.entry_price.toFixed(2)}
                    </div>
                  </div>
                  <span
                    className="ed-mono"
                    style={{
                      fontSize: 13,
                      color,
                      fontWeight: 500,
                      minWidth: 78,
                      textAlign: 'right',
                    }}
                  >
                    {pnl > 0 ? '+' : pnl < 0 ? '−' : '±'}${Math.abs(pnl).toFixed(2)}
                  </span>
                </li>
              );
            })}
          </ul>
        </section>
      )}

      {(!fills?.fills || fills.fills.length === 0) && (
        <div
          className="px-5 py-12 text-center ed-body"
          style={{ color: 'var(--ed-ink-faint)', fontStyle: 'italic', fontSize: 12 }}
        >
          No live trades yet.
        </div>
      )}
    </div>
  );
}

function MetricLarge({ label, value, sub }: { label: string; value: string; sub?: string }) {
  return (
    <div>
      <div className="ed-kicker mb-1">{label}</div>
      <div className="flex items-baseline gap-1">
        <span
          className="ed-display"
          style={{
            fontSize: 24,
            fontWeight: 500,
            color: 'var(--ed-ink)',
            lineHeight: 1,
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {value}
        </span>
        {sub && (
          <span className="ed-mono" style={{ fontSize: 10, color: 'var(--ed-ink-faint)' }}>
            {sub}
          </span>
        )}
      </div>
    </div>
  );
}

function Highlight({
  label,
  value,
  sub,
  color,
}: {
  label: string;
  value: string;
  sub?: string;
  color?: string;
}) {
  return (
    <div>
      <div className="ed-kicker mb-1">{label}</div>
      <div
        className="ed-display"
        style={{
          fontSize: 22,
          fontWeight: 500,
          color: color ?? 'var(--ed-ink)',
          fontVariantNumeric: 'tabular-nums',
          lineHeight: 1,
        }}
      >
        {value}
      </div>
      {sub && (
        <div className="ed-mono" style={{ fontSize: 10, color: 'var(--ed-ink-faint)', marginTop: 2 }}>
          {sub}
        </div>
      )}
    </div>
  );
}

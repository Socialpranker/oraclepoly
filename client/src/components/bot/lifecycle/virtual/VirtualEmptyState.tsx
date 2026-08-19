import type { ShadowCandidate } from '../../../../hooks/useBot';
import { fmtUsd } from '../backtest/utils';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';
const STAGE = '#e0b020';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';

interface Props {
  candidates: ShadowCandidate[];
  executionEnabled: boolean;
}

interface GateRow {
  num: number;
  label: string;
  state: 'check' | 'cross' | 'manual' | null;
  hint?: string;
}

/**
 * VirtualEmptyState — большой плейсхолдер когда 0 promoted.
 *
 * Показывает:
 *   - 6 Virtual → Live gates (с current state для G5 EXECUTION_ENABLED)
 *   - Список ready candidates из Shadow (где 3/3 gates passed)
 *   - TG promotion command
 */
export function VirtualEmptyState({ candidates, executionEnabled }: Props) {
  const gates: GateRow[] = [
    { num: 1, label: 'Virtual PnL > $0 net 7d', state: null, hint: 'появляется после первых fills' },
    { num: 2, label: 'Fill rate ≥ 50% (filled / placed)', state: null, hint: 'book quality check' },
    { num: 3, label: 'Adversarial mirror check pass', state: null, hint: 'manual / n/a' },
    { num: 4, label: '14-28d virtual data accumulated', state: null, hint: 'soak period' },
    {
      num: 5,
      label: 'EXECUTION_ENABLED=true в .env проды',
      state: executionEnabled ? 'check' : 'cross',
      hint: executionEnabled ? '✓ enabled' : '✕ env flag = false',
    },
    { num: 6, label: 'Manual TG decision (warning о $$$)', state: 'manual', hint: 'human-in-the-loop' },
  ];

  return (
    <div style={{ maxWidth: 1180, margin: '0 auto', padding: '40px 22px 56px' }}>
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'minmax(0, 1.2fr) minmax(0, 1fr)',
          gap: 36,
          alignItems: 'start',
        }}
      >
        {/* LEFT — Virtual → Live gates checklist */}
        <section
          style={{
            padding: '36px 32px',
            border: `1px dashed ${STAGE}66`,
            background: `${STAGE}05`,
          }}
        >
          <div style={{ fontSize: 40, marginBottom: 16, lineHeight: 1 }}>💸</div>
          <h3
            className="ed-display"
            style={{
              fontSize: 22,
              fontWeight: 500,
              color: 'var(--ed-ink)',
              marginBottom: 10,
              letterSpacing: '-0.01em',
            }}
          >
            Virtual пусто · 0 detectors paper-trading
          </h3>
          <p style={{ fontSize: 13, color: DIM, lineHeight: 1.55, marginBottom: 24 }}>
            Никто из shadow детекторов ещё не promoted в virtual. Promotion открывает execution
            pipeline (calibrate → size → safety → virtual_placed). После 14-28 дней virtual data
            и pass всех 5 gates ниже — детектор готов к Live с реальными деньгами.
          </p>

          <div className="ed-kicker" style={{ fontSize: 10, letterSpacing: '0.2em', color: STAGE, marginBottom: 14, fontWeight: 600 }}>
            Virtual → Live promotion gates (6)
          </div>
          <ul style={{ listStyle: 'none', padding: 0, margin: 0 }}>
            {gates.map((g) => {
              const stateColor =
                g.state === 'check'
                  ? UP
                  : g.state === 'cross'
                  ? DOWN
                  : g.state === 'manual'
                  ? 'var(--ed-accent)'
                  : FAINT;
              const stateIcon =
                g.state === 'check' ? '✓' : g.state === 'cross' ? '✕' : g.state === 'manual' ? '◐' : '·';
              return (
                <li
                  key={g.num}
                  style={{
                    display: 'flex',
                    gap: 10,
                    padding: '10px 0',
                    borderBottom: `1px dashed ${RULE}`,
                    alignItems: 'baseline',
                    fontSize: 12,
                  }}
                >
                  <span style={{ color: stateColor, fontWeight: 600, minWidth: 14 }}>{stateIcon}</span>
                  <span style={{ color: DIM, fontSize: 9, fontFamily: 'var(--ed-mono)', letterSpacing: '0.14em', minWidth: 24 }}>
                    G{g.num}
                  </span>
                  <span style={{ color: 'var(--ed-ink)', flex: 1, lineHeight: 1.4 }}>{g.label}</span>
                  {g.hint && (
                    <span style={{ color: stateColor, fontSize: 10, fontFamily: 'var(--ed-mono)' }}>
                      {g.hint}
                    </span>
                  )}
                </li>
              );
            })}
          </ul>

          <div
            style={{
              marginTop: 22,
              padding: '10px 14px',
              background: 'rgba(255,255,255,0.04)',
              border: `1px solid ${RULE}`,
              fontFamily: 'var(--ed-mono)',
              fontSize: 12,
              color: 'var(--ed-ink)',
            }}
          >
            <div style={{ color: FAINT, fontSize: 9, letterSpacing: '0.18em', marginBottom: 4, textTransform: 'uppercase' }}>
              TG promotion command
            </div>
            promote:{candidates[0]?.detector ?? 'D7b_no_favorite_fade'} → virtual
          </div>
        </section>

        {/* RIGHT — Shadow candidates ready to promote */}
        <section>
          <div
            className="ed-kicker"
            style={{ fontSize: 10, letterSpacing: '0.2em', color: STAGE, marginBottom: 12, fontWeight: 600 }}
          >
            Ready to promote NOW
          </div>
          <h4
            className="ed-display"
            style={{
              fontSize: 16,
              fontWeight: 400,
              color: 'var(--ed-ink)',
              marginBottom: 14,
              fontStyle: 'italic',
            }}
          >
            Shadow detectors с 3/3 gates passed
          </h4>

          {candidates.length === 0 ? (
            <div
              style={{
                padding: '20px 16px',
                border: `1px dashed ${RULE}`,
                color: FAINT,
                fontSize: 12,
                lineHeight: 1.5,
                fontStyle: 'italic',
              }}
            >
              No detectors at 3/3 Shadow gates. Wait for Shadow data:
              <ul style={{ marginTop: 8, paddingLeft: 18, color: DIM }}>
                <li>≥30 distinct events 7d</li>
                <li>WR ±5pp от canonical</li>
                <li>backtest verdict = PASS</li>
              </ul>
            </div>
          ) : (
            <ul style={{ listStyle: 'none', padding: 0, margin: 0, display: 'flex', flexDirection: 'column', gap: 8 }}>
              {candidates.map((c) => (
                <li
                  key={c.detector}
                  style={{
                    padding: '12px 16px',
                    border: `1px solid ${UP}40`,
                    background: `${UP}06`,
                    display: 'grid',
                    gridTemplateColumns: '1fr auto',
                    gap: 12,
                    alignItems: 'baseline',
                  }}
                >
                  <div>
                    <div className="ed-mono" style={{ fontSize: 12, color: 'var(--ed-ink)', fontWeight: 500 }}>
                      {c.detector}
                    </div>
                    <div className="ed-mono" style={{ fontSize: 10, color: FAINT, marginTop: 4 }}>
                      {c.distinct_events_7d} events 7d · WR {c.recent_wr_pct.toFixed(1)}% (canonical {c.canonical_wr_pct.toFixed(1)}%)
                    </div>
                  </div>
                  <div style={{ textAlign: 'right' }}>
                    <div
                      className="ed-display"
                      style={{
                        fontSize: 18,
                        fontStyle: 'italic',
                        color: UP,
                        lineHeight: 1,
                        fontWeight: 300,
                      }}
                    >
                      {fmtUsd(c.honest_daily_usd_at_5k, { sign: true })}
                    </div>
                    <div className="ed-mono" style={{ fontSize: 9, color: FAINT, marginTop: 2, letterSpacing: '0.04em' }}>
                      honest /day @ $5K
                    </div>
                  </div>
                </li>
              ))}
            </ul>
          )}

          <p style={{ fontSize: 10, color: FAINT, marginTop: 16, lineHeight: 1.55 }}>
            Promote Shadow → Virtual только manual через TG callback. Откроет execution pipeline для
            этого детектора (calibrate → size → safety gate → virtual_placed orders).
          </p>
        </section>
      </div>
    </div>
  );
}

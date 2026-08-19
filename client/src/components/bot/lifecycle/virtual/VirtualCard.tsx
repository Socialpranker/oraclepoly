import type { LiveReadinessGateV2, LiveReadinessItem } from '../../../../hooks/useBot';
import { liveReadyColor, liveReadyLabel, fillRateColor, daysVirtualProgress } from './utils';
import { fmtUsd } from '../backtest/utils';
import { SettleVelocityBar } from './SettleVelocityBar';
import { CardShell, CardHeader, CardDetectorTitle, GateRow, CardFooter } from '../cards/primitives';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';

interface Props {
  item: LiveReadinessItem;
  expanded: boolean;
  onToggle: () => void;
}

/** G2 (pnl_positive) value: signal ROI% + Sortino + Wilson WR vs break-even.
 * Это money-гейт — показываем всю тройку, по которой реально судит promote. */
function pnlGateValue(gate: LiveReadinessGateV2): string {
  const roi = typeof gate.value === 'number' ? gate.value : (gate.sum_roi_net_pct ?? null);
  const roiStr = roi != null ? `${roi >= 0 ? '+' : ''}${roi.toFixed(1)}%` : '—';
  const sortinoStr =
    gate.sortino === 'inf' ? '∞' : typeof gate.sortino === 'number' ? gate.sortino.toFixed(2) : '—';
  const wrStr =
    gate.wilson_lower_winrate_pct != null && gate.break_even_wr_pct != null
      ? ` · WR↓${gate.wilson_lower_winrate_pct.toFixed(0)}/${gate.break_even_wr_pct.toFixed(0)}`
      : '';
  return `${roiStr} · S${sortinoStr}${wrStr}`;
}

/** G3 (fill_rate) value: Wilson lower% vs threshold (или "no orders"). */
function fillGateValue(gate: LiveReadinessGateV2): string {
  if (gate.value == null) return 'no orders';
  const lower = gate.wilson_lower_pct;
  const thr = gate.threshold_pct ?? 50;
  if (lower == null) return typeof gate.value === 'number' ? `${gate.value.toFixed(1)}%` : '—';
  return `↓${lower.toFixed(1)}% / ${thr.toFixed(0)}%`;
}

/** G4 (adversarial_mirror) value: pass / fail / not eval'd. */
function advMirrorValue(value: LiveReadinessGateV2['value']): string {
  if (value === 'not_evaluated') return 'not eval’d';
  if (value === 'pass') return 'pass';
  if (value === 'fail') return 'fail';
  return String(value ?? 'n/a');
}

/**
 * VirtualCard — карточка с visible 6 gate dots + Fill rate bar + days progress (Virtual → Live).
 *
 * Hero: signal ROI 7d (Fraunces italic) vs projected.
 * Bars: Fill rate (50% threshold), Days virtual (14d threshold), Settle velocity.
 * Per-gate detail: 6 rows из gates_v2 (signal-level, ровно по чему судит TG promote).
 *
 * ВАЖНО: gate-display (dots + строки + badge) судит по item.gates_v2, НЕ по legacy
 * item.gates (order-level pnl_usd, paper-надут). Человек и сервер — одна метрика.
 * См. [[shadow-roi-virtual-pnl]], PR #532 follow-up.
 */
export function VirtualCard({ item, expanded, onToggle }: Props) {
  const g = item.gates_v2;
  const color = liveReadyColor(item.gates_v2_passed, item.gates_v2_total);
  const label = liveReadyLabel(item);
  const v7 = item.virtual_7d;
  const pnl = v7.pnl_usd;
  // v4: hero = honest signal-level realized ROI (hold-to-resolution), gate судит по ней.
  // pnl_usd (order-level total) надут paper EARLY_EXIT → ушёл во вторичную строку.
  const realizedRoi = v7.realized_roi_net_pct ?? null;
  const heroColor = realizedRoi == null ? DIM : realizedRoi > 0 ? UP : realizedRoi < 0 ? DOWN : DIM;
  const projected7d = item.canonical.projected_7d_usd;
  const alphaRatio = projected7d != null && projected7d > 0 ? pnl / projected7d : null;

  return (
    <CardShell color={color} expanded={expanded} onToggle={onToggle}>
      <CardHeader
        label={label}
        color={color}
        gates={[
          g.n_min.passed,
          g.pnl_positive.passed,
          g.fill_rate.passed,
          g.adversarial_mirror.passed,
          g.days_accumulated.passed,
          g.execution_enabled.passed,
        ]}
      />

      {/* Phase 5: portfolio badge + override chips */}
      {item.portfolio_id && (
        <div style={{ display: 'flex', gap: 6, marginBottom: 8, flexWrap: 'wrap' }}>
          <span
            style={{
              fontSize: 9,
              fontFamily: 'var(--ed-mono)',
              padding: '2px 6px',
              border: `1px solid ${RULE}`,
              color: DIM,
              letterSpacing: '0.06em',
              textTransform: 'uppercase',
            }}
          >
            {item.portfolio_id}
          </span>
          {item.gate_overrides_applied.map((g) => (
            <span
              key={g}
              title={`portfolio override: ${g}`}
              style={{
                fontSize: 9,
                fontFamily: 'var(--ed-mono)',
                padding: '2px 6px',
                border: `1px dashed ${RULE}`,
                color: FAINT,
                letterSpacing: '0.04em',
              }}
            >
              skip:{g.replace(/_/g, '-')}
            </span>
          ))}
        </div>
      )}

      <CardDetectorTitle detector={item.detector} />

      {/* Hero: Virtual PnL (total = resolved + paper) */}
      <div style={{ marginBottom: 4 }}>
        <div
          className="ed-display"
          style={{
            fontSize: 'clamp(28px, 7vw, 36px)',
            fontWeight: 300,
            fontStyle: 'italic',
            letterSpacing: '-0.02em',
            color: heroColor,
            lineHeight: 1,
            fontVariantNumeric: 'tabular-nums',
          }}
        >
          {realizedRoi == null ? '—' : `${realizedRoi >= 0 ? '+' : ''}${realizedRoi.toFixed(1)}%`}
        </div>
        <div className="ed-mono" style={{ fontSize: 10, color: FAINT, marginTop: 4, letterSpacing: '0.06em' }}>
          signal ROI · hold-to-resolution{v7.sig_terminal != null ? ` · n=${v7.sig_terminal}` : ''} (gate metric)
        </div>
        {/* Вторично: order-level PnL split (paper EARLY_EXIT может быть завышен). */}
        {(v7.wins + v7.losses > 0 || v7.early_exits > 0) && (
          <div
            className="ed-mono"
            style={{
              display: 'flex',
              gap: 12,
              fontSize: 10,
              marginTop: 6,
              letterSpacing: '0.04em',
            }}
          >
            <span title="Order-level total PnL (resolved + paper) — вторичная диагностика, не gate-метрика">
              <span style={{ color: FAINT }}>order pnl </span>
              <span style={{ color: pnl >= 0 ? UP : DOWN, fontWeight: 500 }}>{fmtUsd(pnl, { sign: true })}</span>
            </span>
            <span title="Early-exit mark-to-mid (может быть завышен)">
              <span style={{ color: FAINT }}>paper </span>
              <span style={{ color: v7.pnl_paper_usd >= 0 ? UP : DOWN, fontWeight: 500 }}>
                {fmtUsd(v7.pnl_paper_usd, { sign: true })}
              </span>
              <span style={{ color: FAINT }}> · n={v7.early_exits}</span>
            </span>
          </div>
        )}
      </div>

      {/* Comparison */}
      {projected7d != null && (
        <div
          style={{
            marginTop: 8,
            paddingTop: 10,
            borderTop: `1px dashed ${RULE}`,
            display: 'flex',
            justifyContent: 'space-between',
            alignItems: 'baseline',
            fontSize: 11,
            fontFamily: 'var(--ed-mono)',
          }}
        >
          <span style={{ color: FAINT, letterSpacing: '0.06em' }}>vs projected</span>
          <span>
            <span style={{ color: DIM }}>{fmtUsd(projected7d, { sign: true })}</span>
            {alphaRatio != null && (
              <span style={{ color: alphaRatio >= 1 ? UP : alphaRatio >= 0 ? DIM : DOWN, marginLeft: 8, fontWeight: 600 }}>
                ×{alphaRatio.toFixed(2)}
              </span>
            )}
          </span>
        </div>
      )}

      {/* Fill rate bar */}
      {v7.fill_rate_pct != null && (
        <div style={{ marginTop: 18 }}>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              fontSize: 9,
              letterSpacing: '0.16em',
              textTransform: 'uppercase',
              color: FAINT,
              marginBottom: 6,
              fontFamily: 'var(--ed-mono)',
            }}
          >
            <span>Fill rate</span>
            <span style={{ color: fillRateColor(v7.fill_rate_pct), fontWeight: 600 }}>
              {v7.fill_rate_pct.toFixed(1)}% · {v7.n_filled}/{v7.n_placed}
            </span>
          </div>
          <div style={{ height: 4, background: RULE, position: 'relative', overflow: 'hidden' }}>
            <div
              style={{
                position: 'absolute',
                inset: 0,
                width: `${Math.min(100, v7.fill_rate_pct)}%`,
                background: `linear-gradient(90deg, var(--ed-down) 0%, var(--ed-accent) 50%, var(--ed-up) 100%)`,
                opacity: 0.85,
              }}
            />
            <div
              style={{
                position: 'absolute',
                left: '50%',
                top: -2,
                bottom: -2,
                width: 1,
                background: 'var(--ed-ink-dim)',
                opacity: 0.6,
              }}
              title="Gate 2 threshold: 50%"
            />
          </div>
        </div>
      )}

      {/* Days virtual progress */}
      <div style={{ marginTop: 14 }}>
        <div
          style={{
            display: 'flex',
            justifyContent: 'space-between',
            fontSize: 9,
            letterSpacing: '0.16em',
            textTransform: 'uppercase',
            color: FAINT,
            marginBottom: 6,
            fontFamily: 'var(--ed-mono)',
          }}
        >
          <span>Days virtual</span>
          <span style={{ color: item.days_virtual >= 14 ? UP : DIM, fontWeight: 600 }}>
            {item.days_virtual}d / 14-28d window
          </span>
        </div>
        <div style={{ height: 4, background: RULE, position: 'relative', overflow: 'hidden' }}>
          <div
            style={{
              position: 'absolute',
              inset: 0,
              width: `${daysVirtualProgress(item.days_virtual)}%`,
              background: item.days_virtual >= 14 ? UP : 'var(--ed-accent)',
              opacity: 0.7,
            }}
          />
          <div
            style={{
              position: 'absolute',
              left: '50%',
              top: -2,
              bottom: -2,
              width: 1,
              background: 'var(--ed-ink-dim)',
              opacity: 0.6,
            }}
            title="Gate 4 threshold: 14d"
          />
        </div>
      </div>

      {/* Settle velocity */}
      <div style={{ marginTop: 18 }}>
        <SettleVelocityBar velocity={item.settle_velocity} variant="compact" />
      </div>

      {/* Per-gate detail — signal-level gates_v2 (ровно по чему судит TG promote). */}
      <div style={{ marginTop: 16, display: 'flex', flexDirection: 'column', gap: 6 }}>
        <GateRow
          num={1}
          passed={g.n_min.passed}
          label="Sample N ≥ 30 terminal"
          value={`${g.n_min.value} / ${g.n_min.threshold ?? 30}`}
        />
        <GateRow
          num={2}
          passed={g.pnl_positive.passed}
          label="Signal ROI · Sortino · WR"
          value={pnlGateValue(g.pnl_positive)}
        />
        <GateRow
          num={3}
          passed={g.fill_rate.passed}
          label="Wilson fill lower ≥ 50%"
          value={fillGateValue(g.fill_rate)}
        />
        <GateRow
          num={4}
          passed={g.adversarial_mirror.passed}
          label="Adversarial mirror (manual)"
          value={advMirrorValue(g.adversarial_mirror.value)}
        />
        <GateRow
          num={5}
          passed={g.days_accumulated.passed}
          label="≥14d virtual data"
          value={`${g.days_accumulated.value}d / ${g.days_accumulated.threshold ?? 14}d`}
        />
        <GateRow
          num={6}
          passed={g.execution_enabled.passed}
          label="EXECUTION_ENABLED"
          value={g.execution_enabled.value ? 'true' : 'false'}
        />
      </div>

      <CardFooter
        expanded={expanded}
        label={expanded ? 'collapse · per-bucket' : 'per-bucket realized vs projected'}
      />
    </CardShell>
  );
}

import { useState } from 'react';
import {
  useV2Activity,
  useV2ActivityDay,
  type V2ActivityDayDetector,
  type V2ActivityDayItem,
  type V2ActivityDayRejection,
} from '../../../hooks/useBot';

type DrillTab = 'orders' | 'detectors' | 'rejections';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const ACCENT = 'var(--ed-accent)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';
const RULE = 'var(--ed-rule)';

const DAYS = 14;

/**
 * ShadowActivity — «сколько бот делал в день» за 14 дней.
 *
 * Показывает per-day:
 *   - placed (cdr_run / virtual_filled): сколько ставок реально размещено
 *   - rejected: сколько EV-gate отбросил (тоже отображаем — это work)
 *   - wins / losses (если уже есть resolved)
 *   - daily PnL bar
 *
 * Источник: /api/bot/v2/activity (включает все execution_modes,
 * не только pipeline_v2_shadow_positions VIEW).
 */
export function ShadowActivity() {
  const { data, isLoading } = useV2Activity(DAYS);
  const [expandedDate, setExpandedDate] = useState<string | null>(null);

  if (isLoading) {
    return (
      <section style={{ marginBottom: 24 }}>
        <div className="ed-kicker" style={{ color: FAINT, marginBottom: 8 }}>
          Активность бота · {DAYS}d
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

  const summary = data?.summary;
  const daily = data?.daily ?? [];

  // Backfill missing days with zeros
  const dailyMap = new Map(daily.map((d) => [d.date.slice(0, 10), d]));
  const today = new Date();
  const series = [];
  for (let i = DAYS - 1; i >= 0; i--) {
    const d = new Date(today);
    d.setUTCDate(d.getUTCDate() - i);
    const key = d.toISOString().slice(0, 10);
    const e = dailyMap.get(key);
    series.push({
      date: key,
      label: `${String(d.getUTCMonth() + 1).padStart(2, '0')}/${String(d.getUTCDate()).padStart(2, '0')}`,
      isToday: i === 0,
      placed: e?.placed ?? 0,
      rejected: e?.rejected ?? 0,
      wins: e?.wins ?? 0,
      losses: e?.losses ?? 0,
      pnl: e?.pnl_net_usd ?? 0,
      pnlGross: e?.pnl_gross_usd ?? 0,
      rejectedPnl: e?.rejected_pnl_if_taken ?? 0,
    });
  }

  const maxPlaced = Math.max(...series.map((s) => s.placed + s.rejected), 1);
  const maxAbsPnl = Math.max(...series.map((s) => Math.abs(s.pnl)), 1);

  const totalPlaced = summary?.total_placed ?? 0;
  const totalRejected = summary?.total_rejected ?? 0;
  const totalSettled = summary?.total_settled ?? 0;
  const totalWins = summary?.total_wins ?? 0;
  const totalPnl = summary?.total_pnl_net_usd ?? 0;
  const totalPnlGross = summary?.total_pnl_gross_usd ?? 0;
  const totalRejectedPnl = summary?.total_rejected_pnl_if_taken ?? 0;
  const totalTpExits = summary?.total_tp_exits ?? 0;
  const totalSlExits = summary?.total_sl_exits ?? 0;
  const totalMidZoneExits = summary?.total_mid_zone_exits ?? 0;
  const totalEarlyExits = summary?.total_early_exits ?? 0;
  const wr = totalSettled > 0 ? (totalWins / totalSettled) * 100 : null;

  const pnlColor = totalPnl > 0.5 ? UP : totalPnl < -0.5 ? DOWN : DIM;
  // Edge gate counterfactual: отрицательная сумма = gate спас (убытки если бы взяли);
  // положительная = gate упустил прибыль.
  const edgeGateSaved = totalRejectedPnl < 0;
  const edgeGateColor = totalRejectedPnl < -0.5 ? UP : totalRejectedPnl > 0.5 ? DOWN : DIM;

  return (
    <section style={{ marginBottom: 24 }}>
      <div className="ed-kicker" style={{ color: FAINT, marginBottom: 4 }}>
        Активность бота · {DAYS}d
      </div>
      <div className="ed-mono" style={{ color: DIM, fontSize: 11, marginBottom: 14 }}>
        размещено vs отклонено EV-gate vs резолвлено · per day
      </div>

      <div
        style={{
          background: 'var(--ed-bg-raised)',
          border: `1px solid ${RULE}`,
          padding: '20px 20px 16px',
        }}
      >
        {/* Top row — 5 KPI summary (responsive: 5 cols desktop, 2 mobile) */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))',
            gap: 14,
            paddingBottom: 16,
            borderBottom: `1px solid ${RULE}`,
            marginBottom: 16,
          }}
        >
          <SummaryCell
            label="размещено"
            value={totalPlaced.toLocaleString()}
            sub={`${DAYS} дней`}
          />
          <SummaryCell
            label="отклонено EV-gate"
            value={totalRejected.toLocaleString()}
            sub={
              totalAttemptedRate(totalRejected, totalPlaced + totalRejected) +
              '% попыток'
            }
            valueColor={DIM}
          />
          <SummaryCell
            label="резолвлено"
            value={totalSettled.toLocaleString()}
            sub={
              totalEarlyExits > 0
                ? `${totalTpExits}TP / ${totalSlExits}SL / ${totalMidZoneExits}MZ`
                : wr != null ? `${wr.toFixed(1)}% wins` : 'ждём резолва'
            }
            valueColor={wr != null && wr > 50 ? UP : DIM}
          />
          <SummaryCell
            label="PnL · net"
            value={`${totalPnl >= 0 ? '+' : '−'}$${Math.abs(totalPnl).toFixed(2)}`}
            sub={`gross ${totalPnlGross >= 0 ? '+' : '−'}$${Math.abs(totalPnlGross).toFixed(2)}`}
            valueColor={pnlColor}
          />
          <SummaryCell
            label={edgeGateSaved ? 'edge gate спас' : 'edge gate упустил'}
            value={`${edgeGateSaved ? '+' : '−'}$${Math.abs(totalRejectedPnl).toFixed(2)}`}
            sub={
              totalRejectedPnl === 0
                ? 'rejected ещё не закрылись'
                : edgeGateSaved
                ? 'если бы взяли — потеряли бы'
                : 'если бы взяли — заработали бы'
            }
            valueColor={edgeGateColor}
          />
        </div>

        {/* Stacked bars: placed (accent) + rejected (faint) per day */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: `repeat(${series.length}, minmax(0, 1fr))`,
            gap: 3,
            height: 72,
            alignItems: 'end',
            marginBottom: 4,
          }}
        >
          {series.map((s) => {
            const total = s.placed + s.rejected;
            const placedPct = total > 0 ? (s.placed / maxPlaced) * 100 : 0;
            const rejectedPct = total > 0 ? (s.rejected / maxPlaced) * 100 : 0;
            const pnlBit =
              s.pnl !== 0
                ? `, net ${s.pnl >= 0 ? '+' : '−'}$${Math.abs(s.pnl).toFixed(2)} (gross ${s.pnlGross >= 0 ? '+' : '−'}$${Math.abs(s.pnlGross).toFixed(2)})`
                : '';
            const rejBit =
              s.rejectedPnl !== 0
                ? `, edge gate ${s.rejectedPnl < 0 ? 'спас' : 'упустил'} ${s.rejectedPnl < 0 ? '+' : '−'}$${Math.abs(s.rejectedPnl).toFixed(2)}`
                : '';
            const tooltip = `${s.label}: ${s.placed} placed, ${s.rejected} rejected, ${s.wins}W/${s.losses}L${pnlBit}${rejBit}`;
            const isExpanded = expandedDate === s.date;
            return (
              <div
                key={s.date}
                title={tooltip}
                onClick={() =>
                  setExpandedDate((prev) => (prev === s.date ? null : s.date))
                }
                style={{
                  display: 'flex',
                  flexDirection: 'column',
                  justifyContent: 'flex-end',
                  height: '100%',
                  cursor: 'pointer',
                  position: 'relative',
                  outline: isExpanded ? `1px solid ${ACCENT}` : 'none',
                  outlineOffset: 2,
                }}
              >
                {rejectedPct > 0 && (
                  <div
                    style={{
                      width: '100%',
                      height: `${rejectedPct}%`,
                      background: FAINT,
                      opacity: 0.5,
                      transition: 'height 0.3s',
                    }}
                  />
                )}
                {placedPct > 0 && (
                  <div
                    style={{
                      width: '100%',
                      height: `${placedPct}%`,
                      background: s.isToday ? ACCENT : DIM,
                      transition: 'height 0.3s',
                    }}
                  />
                )}
              </div>
            );
          })}
        </div>

        {/* Day labels */}
        <div
          style={{
            display: 'grid',
            gridTemplateColumns: `repeat(${series.length}, minmax(0, 1fr))`,
            gap: 3,
          }}
        >
          {series.map((s) => (
            <div
              key={s.date}
              className="ed-mono"
              style={{
                fontSize: 8,
                color: s.isToday ? 'var(--ed-ink)' : FAINT,
                textAlign: 'center',
                opacity: s.isToday ? 1 : 0.7,
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                whiteSpace: 'nowrap',
              }}
            >
              {s.label}
            </div>
          ))}
        </div>

        {/* PnL bars (only resolved days) */}
        {series.some((s) => s.pnl !== 0) && (
          <>
            <div
              className="ed-kicker"
              style={{ fontSize: 9, color: FAINT, marginTop: 16, marginBottom: 4 }}
            >
              daily PnL · резолв
            </div>
            <div
              style={{
                display: 'grid',
                gridTemplateColumns: `repeat(${series.length}, 1fr)`,
                gap: 4,
                height: 36,
                alignItems: 'center',
                position: 'relative',
              }}
            >
              {/* zero baseline */}
              <div
                style={{
                  position: 'absolute',
                  left: 0,
                  right: 0,
                  top: '50%',
                  height: 1,
                  background: RULE,
                  zIndex: 0,
                }}
              />
              {series.map((s) => {
                if (s.pnl === 0) {
                  return <div key={s.date} />;
                }
                const heightPct = (Math.abs(s.pnl) / maxAbsPnl) * 50;
                const isUp = s.pnl > 0;
                const color = isUp ? UP : DOWN;
                return (
                  <div
                    key={s.date}
                    style={{
                      position: 'relative',
                      height: '100%',
                    }}
                  >
                    <div
                      style={{
                        position: 'absolute',
                        left: 0,
                        right: 0,
                        [isUp ? 'bottom' : 'top']: '50%',
                        height: `${heightPct}%`,
                        background: color,
                        opacity: 0.85,
                      }}
                    />
                  </div>
                );
              })}
            </div>
          </>
        )}

        {/* Legend */}
        <div
          style={{
            display: 'flex',
            gap: 18,
            marginTop: 16,
            paddingTop: 12,
            borderTop: `1px solid ${RULE}`,
            fontSize: 10,
            color: DIM,
            flexWrap: 'wrap',
          }}
          className="ed-mono"
        >
          <Legend color={ACCENT} label="placed (today)" />
          <Legend color={DIM} label="placed" />
          <Legend color={FAINT} label="rejected" />
          <Legend color={UP} label="WIN PnL" />
          <Legend color={DOWN} label="LOSS PnL" />
        </div>
        <div
          className="ed-mono"
          style={{
            marginTop: 8,
            fontSize: 10,
            color: FAINT,
            opacity: 0.8,
          }}
        >
          ↳ клик по столбцу — посмотреть какие именно ставки были в этот день
        </div>
      </div>

      {expandedDate && <DayDrillDown date={expandedDate} onClose={() => setExpandedDate(null)} />}
    </section>
  );
}

function DayDrillDown({ date, onClose }: { date: string; onClose: () => void }) {
  const { data, isLoading } = useV2ActivityDay(date);
  const [tab, setTab] = useState<DrillTab>('detectors');

  const detectors = data?.by_detector ?? [];
  const rejections = data?.by_rejection ?? [];
  const items = data?.items ?? [];

  return (
    <div
      style={{
        marginTop: 12,
        background: 'var(--ed-bg-raised)',
        border: `1px solid ${RULE}`,
        padding: '16px 20px',
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'baseline',
          marginBottom: 12,
        }}
      >
        <div className="ed-kicker" style={{ color: FAINT, fontSize: 10, letterSpacing: '0.18em' }}>
          {date} · {data?.count ?? 0} ордеров
        </div>
        <button
          onClick={onClose}
          className="ed-mono"
          style={{
            background: 'transparent',
            border: 'none',
            color: FAINT,
            fontSize: 11,
            cursor: 'pointer',
          }}
        >
          закрыть ✕
        </button>
      </div>

      {/* Tab strip */}
      <div
        style={{
          display: 'flex',
          gap: 0,
          marginBottom: 14,
          borderBottom: `1px solid ${RULE}`,
        }}
      >
        <DrillTabButton
          active={tab === 'detectors'}
          onClick={() => setTab('detectors')}
          label={`по детекторам (${detectors.length})`}
        />
        <DrillTabButton
          active={tab === 'orders'}
          onClick={() => setTab('orders')}
          label={`по ордерам (${items.length})`}
        />
        {rejections.length > 0 && (
          <DrillTabButton
            active={tab === 'rejections'}
            onClick={() => setTab('rejections')}
            label={`reject reasons (${rejections.length})`}
          />
        )}
      </div>

      {isLoading && (
        <div className="ed-mono" style={{ color: FAINT, fontSize: 11, padding: '12px 0' }}>
          loading…
        </div>
      )}

      {!isLoading && data && data.count === 0 && (
        <div className="ed-mono" style={{ color: FAINT, fontSize: 11, padding: '12px 0' }}>
          ничего не было
        </div>
      )}

      {!isLoading && tab === 'detectors' && detectors.length > 0 && (
        <DetectorBreakdownTable detectors={detectors} />
      )}

      {!isLoading && tab === 'orders' && items.length > 0 && (
        <OrdersTable items={items} count={data?.count ?? 0} />
      )}

      {!isLoading && tab === 'rejections' && rejections.length > 0 && (
        <RejectionsTable rejections={rejections} />
      )}
    </div>
  );
}

function DrillTabButton({
  active,
  onClick,
  label,
}: {
  active: boolean;
  onClick: () => void;
  label: string;
}) {
  return (
    <button
      onClick={onClick}
      className="ed-mono"
      style={{
        background: 'transparent',
        border: 'none',
        borderBottom: active ? `2px solid ${ACCENT}` : '2px solid transparent',
        color: active ? 'var(--ed-ink)' : FAINT,
        fontSize: 11,
        padding: '6px 14px',
        cursor: 'pointer',
        textTransform: 'uppercase',
        letterSpacing: '0.12em',
        marginBottom: -1,
      }}
    >
      {label}
    </button>
  );
}

function DetectorBreakdownTable({ detectors }: { detectors: V2ActivityDayDetector[] }) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table
        className="ed-mono"
        style={{
          width: '100%',
          borderCollapse: 'collapse',
          fontSize: 11,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        <thead>
          <tr style={{ color: FAINT, textAlign: 'left' }}>
            <Th>детектор</Th>
            <Th align="right">placed</Th>
            <Th align="right">rejected</Th>
            <Th align="right">резолв</Th>
            <Th align="right">W / L</Th>
            <Th align="right">WR</Th>
            <Th align="right">stake</Th>
            <Th align="right">PnL net</Th>
            <Th align="right">ROI</Th>
          </tr>
        </thead>
        <tbody>
          {detectors.map((d) => {
            const pnlColor = d.pnl_net_usd > 0.5 ? UP : d.pnl_net_usd < -0.5 ? DOWN : DIM;
            const roiColor =
              d.roi_net_pct == null ? DIM : d.roi_net_pct > 1 ? UP : d.roi_net_pct < -1 ? DOWN : DIM;
            return (
              <tr key={d.detector} style={{ borderTop: `1px solid ${RULE}` }}>
                <Td color="var(--ed-ink)">{d.detector}</Td>
                <Td align="right">{d.placed.toLocaleString()}</Td>
                <Td align="right" color={FAINT}>
                  {d.rejected > 0 ? d.rejected.toLocaleString() : '—'}
                </Td>
                <Td align="right">{d.settled > 0 ? d.settled.toLocaleString() : '—'}</Td>
                <Td align="right">
                  {d.settled > 0 ? `${d.wins} / ${d.losses}` : '—'}
                </Td>
                <Td align="right" color={d.wr != null && d.wr > 0.5 ? UP : DIM}>
                  {d.wr != null ? `${(d.wr * 100).toFixed(0)}%` : '—'}
                </Td>
                <Td align="right">${d.stake_usd.toFixed(2)}</Td>
                <Td align="right" color={pnlColor}>
                  {d.settled > 0
                    ? `${d.pnl_net_usd >= 0 ? '+' : '−'}$${Math.abs(d.pnl_net_usd).toFixed(2)}`
                    : '—'}
                </Td>
                <Td align="right" color={roiColor}>
                  {d.roi_net_pct != null
                    ? `${d.roi_net_pct >= 0 ? '+' : '−'}${Math.abs(d.roi_net_pct).toFixed(1)}%`
                    : '—'}
                </Td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function RejectionsTable({ rejections }: { rejections: V2ActivityDayRejection[] }) {
  const total = rejections.reduce((s, r) => s + r.count, 0);
  return (
    <div style={{ overflowX: 'auto' }}>
      <table
        className="ed-mono"
        style={{
          width: '100%',
          borderCollapse: 'collapse',
          fontSize: 11,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        <thead>
          <tr style={{ color: FAINT, textAlign: 'left' }}>
            <Th>причина</Th>
            <Th align="right">count</Th>
            <Th align="right">% от всех reject</Th>
          </tr>
        </thead>
        <tbody>
          {rejections.map((r) => (
            <tr key={r.reason} style={{ borderTop: `1px solid ${RULE}` }}>
              <Td color="var(--ed-ink)">{r.reason}</Td>
              <Td align="right">{r.count.toLocaleString()}</Td>
              <Td align="right" color={DIM}>
                {((r.count / total) * 100).toFixed(0)}%
              </Td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function OrdersTable({ items, count }: { items: V2ActivityDayItem[]; count: number }) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table
        className="ed-mono"
        style={{
          width: '100%',
          borderCollapse: 'collapse',
          fontSize: 11,
          fontVariantNumeric: 'tabular-nums',
        }}
      >
        <thead>
          <tr style={{ color: FAINT, textAlign: 'left' }}>
            <Th>статус</Th>
            <Th>детектор</Th>
            <Th>сторона</Th>
            <Th align="right">stake</Th>
            <Th align="right">price</Th>
            <Th>market</Th>
            <Th align="right">PnL net</Th>
          </tr>
        </thead>
        <tbody>
          {items.slice(0, 200).map((it) => (
            <DrillRow key={it.id} item={it} />
          ))}
        </tbody>
      </table>
      {items.length > 200 && (
        <div
          className="ed-mono"
          style={{ color: FAINT, fontSize: 10, padding: '8px 0', opacity: 0.7 }}
        >
          показано 200 из {count}
        </div>
      )}
    </div>
  );
}

function DrillRow({ item }: { item: V2ActivityDayItem }) {
  const isReject = item.status === 'rejected';
  const isWin = item.outcome === 'WIN';
  const isLoss = item.outcome === 'LOSS';
  const pnlColor = isWin ? UP : isLoss ? DOWN : DIM;
  const statusColor = isReject ? FAINT : isWin ? UP : isLoss ? DOWN : DIM;

  const reason = item.rejection_reason ?? item.cancel_reason;
  const statusLabel = isReject
    ? `rejected${reason ? ` (${truncate(reason, 24)})` : ''}`
    : item.outcome ?? item.status;

  const detLabel = item.detector || item.strategy_key || '—';

  return (
    <tr style={{ borderTop: `1px solid ${RULE}` }}>
      <Td color={statusColor}>{statusLabel}</Td>
      <Td color={DIM}>{detLabel}</Td>
      <Td>{item.side}</Td>
      <Td align="right">${item.stake_usd.toFixed(2)}</Td>
      <Td align="right">{item.signal_price.toFixed(3)}</Td>
      <Td color={DIM} title={item.question ?? item.market_id}>
        {item.question ? truncate(item.question, 36) : item.market_id.slice(0, 12) + '…'}
      </Td>
      <Td align="right" color={pnlColor}>
        {item.net_pnl_usd != null
          ? `${item.net_pnl_usd >= 0 ? '+' : '−'}$${Math.abs(item.net_pnl_usd).toFixed(2)}`
          : '—'}
      </Td>
    </tr>
  );
}

function Th({ children, align = 'left' }: { children: React.ReactNode; align?: 'left' | 'right' }) {
  return (
    <th
      className="ed-kicker"
      style={{
        textAlign: align,
        padding: '6px 10px 6px 0',
        fontSize: 9,
        letterSpacing: '0.16em',
        fontWeight: 400,
      }}
    >
      {children}
    </th>
  );
}

function Td({
  children,
  align = 'left',
  color = 'var(--ed-ink)',
  title,
}: {
  children: React.ReactNode;
  align?: 'left' | 'right';
  color?: string;
  title?: string;
}) {
  return (
    <td
      title={title}
      style={{
        textAlign: align,
        padding: '6px 10px 6px 0',
        color,
        whiteSpace: 'nowrap',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}
    >
      {children}
    </td>
  );
}

function truncate(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

function SummaryCell({
  label,
  value,
  sub,
  valueColor = 'var(--ed-ink)',
}: {
  label: string;
  value: string;
  sub: string;
  valueColor?: string;
}) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
      <span
        className="ed-kicker"
        style={{ fontSize: 9, color: FAINT, letterSpacing: '0.16em' }}
      >
        {label}
      </span>
      <span
        className="ed-display"
        style={{
          fontSize: 22,
          fontWeight: 400,
          letterSpacing: '-0.02em',
          color: valueColor,
          lineHeight: 1.05,
          fontVariantNumeric: 'tabular-nums',
          wordBreak: 'break-word',
        }}
      >
        {value}
      </span>
      <span className="ed-mono" style={{ fontSize: 10, color: FAINT }}>
        {sub}
      </span>
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <span
        style={{
          display: 'inline-block',
          width: 10,
          height: 10,
          background: color,
        }}
      />
      {label}
    </span>
  );
}

function totalAttemptedRate(part: number, total: number): string {
  if (total === 0) return '0';
  return ((part / total) * 100).toFixed(0);
}

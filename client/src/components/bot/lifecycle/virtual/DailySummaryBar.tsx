import { useDailySummary } from '../../../../hooks/useBot';

const FAINT = 'var(--ed-ink-faint)';
const DIM = 'var(--ed-ink-dim)';
const RULE = 'var(--ed-rule)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';

/**
 * DailySummaryBar — простой ответ на вопрос:
 *   «сколько поставили, сколько выиграли, сколько проиграли».
 *
 * Layout: текущий день большой блок + последние 7 дней мини-таблица.
 * Использует /api/bot/v2/daily_summary.
 */
export function DailySummaryBar() {
  const { data, isLoading } = useDailySummary();

  if (isLoading) {
    return (
      <div style={shellStyle}>
        <div className="ed-mono" style={{ color: FAINT, fontSize: 11 }}>
          loading daily summary…
        </div>
      </div>
    );
  }

  const days = data?.days ?? [];
  const today = days[0];

  return (
    <div style={shellStyle}>
      <div className="ed-mono" style={labelStyle}>
        DAILY SUMMARY · последние 14 дней
      </div>

      {today ? (
        <div style={todayGrid}>
          <Stat label="Сегодня поставлено" value={`${today.bets}`} sub={`на $${fmt(today.staked_usd)}`} />
          <Stat
            label="Выиграно"
            value={`+$${fmt(today.won_usd)}`}
            sub="net of fees"
            color={UP}
          />
          <Stat
            label="Проиграно"
            value={`−$${fmt(Math.abs(today.lost_usd))}`}
            sub="net of fees"
            color={DOWN}
          />
          <Stat
            label="Итог дня"
            value={`${today.net_pnl >= 0 ? '+' : '−'}$${fmt(Math.abs(today.net_pnl))}`}
            sub={`${today.n_settled} settled`}
            color={today.net_pnl >= 0 ? UP : DOWN}
          />
        </div>
      ) : (
        <div className="ed-mono" style={{ color: FAINT, fontSize: 11 }}>
          Сегодня ставок ещё не было.
        </div>
      )}

      {days.length > 1 && (
        <table style={tableStyle}>
          <thead>
            <tr style={{ borderBottom: `1px solid ${RULE}` }}>
              <th style={th}>День</th>
              <th style={thR}>Ставок</th>
              <th style={thR}>Поставлено</th>
              <th style={thR}>Выиграно</th>
              <th style={thR}>Проиграно</th>
              <th style={thR}>Итог</th>
            </tr>
          </thead>
          <tbody>
            {days.slice(1, 8).map((d) => (
              <tr key={d.day} style={{ borderBottom: `1px dashed ${RULE}` }}>
                <td style={td}>{d.day}</td>
                <td style={tdR}>{d.bets}</td>
                <td style={tdR}>${fmt(d.staked_usd)}</td>
                <td style={{ ...tdR, color: d.won_usd > 0 ? UP : DIM }}>
                  +${fmt(d.won_usd)}
                </td>
                <td style={{ ...tdR, color: d.lost_usd < 0 ? DOWN : DIM }}>
                  −${fmt(Math.abs(d.lost_usd))}
                </td>
                <td
                  style={{
                    ...tdR,
                    color: d.net_pnl > 0 ? UP : d.net_pnl < 0 ? DOWN : DIM,
                    fontWeight: 600,
                  }}
                >
                  {d.net_pnl >= 0 ? '+' : '−'}${fmt(Math.abs(d.net_pnl))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      {data?.totals_14d && (
        <div className="ed-mono" style={totalsStyle}>
          <span style={{ color: FAINT }}>14d total: </span>
          <span style={{ color: DIM }}>
            {data.totals_14d.bets} ставок · ${fmt(data.totals_14d.staked_usd)} stake
          </span>
          <span style={{ marginLeft: 12, color: UP }}>
            +${fmt(data.totals_14d.won_usd)}
          </span>
          <span style={{ marginLeft: 8, color: DOWN }}>
            −${fmt(Math.abs(data.totals_14d.lost_usd))}
          </span>
          <span
            style={{
              marginLeft: 12,
              color: data.totals_14d.net_pnl > 0 ? UP : DOWN,
              fontWeight: 600,
            }}
          >
            net {data.totals_14d.net_pnl >= 0 ? '+' : '−'}$
            {fmt(Math.abs(data.totals_14d.net_pnl))}
          </span>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div>
      <div className="ed-mono" style={{ fontSize: 10, color: FAINT, letterSpacing: '0.08em', textTransform: 'uppercase' }}>
        {label}
      </div>
      <div
        className="ed-display"
        style={{
          fontSize: 'clamp(22px, 5vw, 30px)',
          fontWeight: 300,
          fontStyle: 'italic',
          color: color ?? 'var(--ed-ink)',
          fontVariantNumeric: 'tabular-nums',
          letterSpacing: '-0.01em',
          marginTop: 2,
        }}
      >
        {value}
      </div>
      {sub && (
        <div className="ed-mono" style={{ fontSize: 10, color: DIM, marginTop: 2 }}>
          {sub}
        </div>
      )}
    </div>
  );
}

const fmt = (n: number) =>
  n >= 1000 ? n.toLocaleString('en-US', { maximumFractionDigits: 0 }) : n.toFixed(2);

const shellStyle: React.CSSProperties = {
  maxWidth: 1180,
  margin: '0 auto',
  padding: '22px 22px 18px',
  borderTop: `1px solid ${RULE}`,
};

const labelStyle: React.CSSProperties = {
  fontSize: 10,
  color: FAINT,
  letterSpacing: '0.12em',
  textTransform: 'uppercase',
  marginBottom: 14,
};

const todayGrid: React.CSSProperties = {
  display: 'grid',
  gridTemplateColumns: 'repeat(auto-fit, minmax(180px, 1fr))',
  gap: 24,
  marginBottom: 22,
};

const tableStyle: React.CSSProperties = {
  width: '100%',
  borderCollapse: 'collapse',
  fontSize: 11,
  fontFamily: 'var(--ed-mono)',
};

const th: React.CSSProperties = {
  textAlign: 'left',
  padding: '6px 8px',
  color: FAINT,
  fontWeight: 400,
  letterSpacing: '0.08em',
  textTransform: 'uppercase',
  fontSize: 9,
};

const thR: React.CSSProperties = { ...th, textAlign: 'right' };

const td: React.CSSProperties = {
  padding: '6px 8px',
  color: DIM,
  fontVariantNumeric: 'tabular-nums',
};

const tdR: React.CSSProperties = { ...td, textAlign: 'right' };

const totalsStyle: React.CSSProperties = {
  marginTop: 14,
  fontSize: 11,
  letterSpacing: '0.04em',
};

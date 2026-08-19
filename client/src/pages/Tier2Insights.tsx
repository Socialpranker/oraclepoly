import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  useTier2Summary,
  useActionFeed,
  useUmaDisputes,
  useManifoldDivergence,
  useWhaleShocks,
  useCohortStats,
  useTermStructure,
  useMlAccuracy,
  useHonestAudit,
  type ActionItem,
  type ActionSeverity,
  type ActionSource,
  type UmaDispute,
  type ManifoldDivergence,
  type WhaleShock,
  type CohortStats,
  type TermStructureItem,
  type MlAccuracyLayerRow,
  type MlAccuracyMoneyRow,
  type MlModelStatus,
  type HonestAuditRow,
} from '../hooks/useTier2';

/**
 * Tier2Insights — hybrid hunting + research dashboard.
 *
 * Цель: за 10 секунд понять, есть ли actionable edge прямо сейчас,
 * а потом — если нужно — провалиться в research-секции.
 *
 * Источники: 5 materialized views + /action-feed UNION endpoint.
 * Refresh hourly (cron refresh-tier2-mvs).
 */

const ACCENT = 'var(--ed-accent)';
const ACCENT_SOFT = 'var(--ed-accent-soft)';
const INK = 'var(--ed-ink)';
const DIM = 'var(--ed-ink-dim)';
const FAINT = 'var(--ed-ink-faint)';
const RULE = 'var(--ed-rule)';
const RULE_STRONG = 'var(--ed-rule-strong)';
const UP = 'var(--ed-up)';
const DOWN = 'var(--ed-down)';
const BG_RAISED = 'var(--ed-bg-raised)';

const SOURCE_META: Record<ActionSource, { label: string; glyph: string; color: string }> = {
  uma: { label: 'UMA', glyph: '⚖', color: '#b4876a' },
  manifold: { label: 'Manifold', glyph: '⇆', color: '#5b9dff' },
  whale: { label: 'Whale', glyph: '◆', color: '#9b6cff' },
  term: { label: 'Term-1h', glyph: '↗', color: '#c9a961' },
};

const SEVERITY_META: Record<ActionSeverity, { label: string; color: string; bg: string }> = {
  high: { label: 'HIGH', color: '#d65a5a', bg: 'rgba(214, 90, 90, 0.12)' },
  med: { label: 'MED', color: '#c9a961', bg: 'rgba(201, 169, 97, 0.12)' },
  low: { label: 'LOW', color: '#8a8680', bg: 'rgba(138, 134, 128, 0.10)' },
};

export function Tier2Insights() {
  return (
    <div
      className="editorial"
      style={{
        minHeight: '100vh',
        background: 'var(--ed-bg)',
        color: INK,
        fontFamily: 'var(--ed-body)',
      }}
    >
      <PageHeader />
      <main style={{ padding: '28px 22px 60px', maxWidth: 1400, margin: '0 auto' }}>
        <Section
          id="action-feed"
          eyebrow="Hunting"
          title="Action feed"
          subtitle="Все актуальные сигналы из 4 каналов, ранжированы по severity и recency."
        >
          <ActionFeed />
        </Section>

        <Section
          id="kpi"
          eyebrow="State"
          title="Канальное здоровье"
          subtitle="Что сейчас активно. Цвет = уровень внимания (зелёный = ок, золотой = есть сигналы, красный = много горячего)."
        >
          <KPIStrip />
        </Section>

        <Section
          id="manifold"
          eyebrow="Channel · cross-venue"
          title="Manifold ↔ Polymarket lead-lag"
          subtitle="Гипотеза: Manifold обычно двигается раньше на 4–12 часов. Точки далеко от диагонали = potential edge."
        >
          <ManifoldScatter />
        </Section>

        <Section
          id="uma"
          eyebrow="Channel · resolution risk"
          title="UMA dispute timeline"
          subtitle="≥2 disputes ⇒ unreliable resolution. Фильтр по статусу: proposed/disputed первыми."
        >
          <UmaBlock />
        </Section>

        <Section
          id="whale"
          eyebrow="Channel · microstructure"
          title="Whale shock recovery"
          subtitle="Reaction orderbook'а на trade ≥$50k. Плохое восстановление (≤70%) — fragile depth, momentum tail."
        >
          <WhaleBlock />
        </Section>

        <Section
          id="term"
          eyebrow="Channel · momentum"
          title="Term-structure breakouts"
          subtitle="Markets с резким движением за час. Регимы strong_up/down часто продолжают inertia."
        >
          <TermBlock />
        </Section>

        <Section
          id="cohorts"
          eyebrow="Channel · cohort skew"
          title="Cohort win-rates"
          subtitle="cyom + restricted + neg_risk_aug × category. Поиск систематической дешевизны или дороговизны."
        >
          <CohortBlock />
        </Section>

        <Section
          id="ml-accuracy"
          eyebrow="Diagnostics · model accuracy"
          title="ML Accuracy — куда · когда · деньги"
          subtitle="Honest-cohort точность (дедуп + event-health). Слой 1: честность вероятности (Brier vs рынок). Слой 2: качество отбора. Деньги: ROI после fees с флагом RELIABLE/ARTIFACT."
        >
          <MlAccuracyBlock />
        </Section>

        <Section
          id="honest-audit"
          eyebrow="Diagnostics · forward-валидация"
          title="Honest Audit — детекторы на живых резолвах"
          subtitle="Forward-only термометр: honest PnL по (детектор × категория), вердикт KILL/MARGINAL/PROMOTE, тренд деградации 📉/➡️/📈 по половинам окна. SSoT — еженедельный TG-аудит."
        >
          <HonestAuditBlock />
        </Section>
      </main>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Header
// ─────────────────────────────────────────────────────────────

function PageHeader() {
  const feed = useActionFeed({ limit: 1 });
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = setInterval(() => setNow(Date.now()), 30_000);
    return () => clearInterval(id);
  }, []);
  const refreshLag = useMemo(() => {
    const last = feed.data?.freshness.uma_last;
    if (!last) return null;
    return Math.max(0, (now - new Date(last).getTime()) / 60_000);
  }, [feed.data, now]);

  const stale = refreshLag !== null && refreshLag > 120;
  const freshnessLabel = refreshLag === null
    ? '—'
    : refreshLag < 60
      ? `${Math.round(refreshLag)}m ago`
      : `${(refreshLag / 60).toFixed(1)}h ago`;

  return (
    <div
      style={{
        padding: '14px 22px',
        borderBottom: `1px solid ${RULE_STRONG}`,
        display: 'flex',
        alignItems: 'baseline',
        gap: 16,
        flexWrap: 'wrap',
      }}
    >
      <Link
        to="/bot"
        style={{
          color: DIM,
          textDecoration: 'none',
          fontSize: 11,
          letterSpacing: '0.16em',
          textTransform: 'uppercase',
        }}
        className="ed-kicker"
      >
        ← Bot
      </Link>
      <span style={{ color: FAINT }}>/</span>
      <h1
        className="ed-display"
        style={{ fontSize: 26, fontWeight: 500, letterSpacing: '-0.02em', color: INK, margin: 0 }}
      >
        Tier-2<span style={{ color: ACCENT }}>.</span>{' '}
        <em style={{ fontStyle: 'italic', fontWeight: 300, color: DIM, fontSize: 18 }}>
          unique-data insights
        </em>
      </h1>
      <div style={{ flex: 1 }} />
      <div
        className="ed-kicker"
        style={{
          fontSize: 10,
          letterSpacing: '0.18em',
          textTransform: 'uppercase',
          color: stale ? '#d65a5a' : DIM,
          display: 'flex',
          alignItems: 'center',
          gap: 8,
        }}
      >
        <span
          style={{
            width: 7,
            height: 7,
            borderRadius: '50%',
            background: stale ? '#d65a5a' : UP,
            boxShadow: stale ? '0 0 6px #d65a5a' : `0 0 6px ${UP}`,
          }}
        />
        MV refresh · {freshnessLabel}
        {stale && <span style={{ marginLeft: 6, color: '#d65a5a' }}>STALE</span>}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Section wrapper
// ─────────────────────────────────────────────────────────────

function Section({
  id,
  eyebrow,
  title,
  subtitle,
  children,
}: {
  id: string;
  eyebrow: string;
  title: string;
  subtitle: string;
  children: React.ReactNode;
}) {
  return (
    <section id={id} style={{ marginBottom: 56, scrollMarginTop: 80 }}>
      <header style={{ marginBottom: 18 }}>
        <div
          className="ed-kicker"
          style={{
            fontSize: 10,
            letterSpacing: '0.22em',
            color: ACCENT,
            textTransform: 'uppercase',
            fontWeight: 600,
            marginBottom: 8,
          }}
        >
          {eyebrow}
        </div>
        <h2
          className="ed-display"
          style={{
            fontSize: 22,
            fontWeight: 500,
            letterSpacing: '-0.015em',
            color: INK,
            margin: 0,
            marginBottom: 6,
          }}
        >
          {title}
        </h2>
        <p
          className="ed-display"
          style={{
            fontSize: 13,
            fontStyle: 'italic',
            fontWeight: 300,
            color: DIM,
            margin: 0,
            maxWidth: 720,
            lineHeight: 1.5,
          }}
        >
          {subtitle}
        </p>
      </header>
      {children}
    </section>
  );
}

// ─────────────────────────────────────────────────────────────
// Action feed
// ─────────────────────────────────────────────────────────────

function ActionFeed() {
  const { data, isLoading, error } = useActionFeed({ limit: 12 });
  const [filter, setFilter] = useState<ActionSource | 'all'>('all');

  if (isLoading) return <Loading />;
  if (error) return <ErrorBanner err={error} />;
  if (!data?.items.length) return <Empty msg="Сигналов нет. Все каналы тихие — это нормально." />;

  const filtered = filter === 'all' ? data.items : data.items.filter((it) => it.source === filter);

  return (
    <div>
      <FilterRow
        active={filter}
        onChange={setFilter}
        counts={data.items.reduce(
          (acc, it) => {
            acc[it.source] = (acc[it.source] || 0) + 1;
            return acc;
          },
          {} as Record<string, number>,
        )}
      />
      <div
        style={{
          display: 'grid',
          gridTemplateColumns: 'repeat(auto-fit, minmax(340px, 1fr))',
          gap: 14,
          marginTop: 14,
        }}
      >
        {filtered.map((item, i) => (
          <ActionCard key={`${item.source}-${item.condition_id ?? i}`} item={item} />
        ))}
      </div>
    </div>
  );
}

function FilterRow({
  active,
  onChange,
  counts,
}: {
  active: ActionSource | 'all';
  onChange: (s: ActionSource | 'all') => void;
  counts: Record<string, number>;
}) {
  const total = Object.values(counts).reduce((s, n) => s + n, 0);
  const items: Array<{ key: ActionSource | 'all'; label: string; count: number }> = [
    { key: 'all', label: 'All', count: total },
    { key: 'uma', label: 'UMA', count: counts.uma || 0 },
    { key: 'manifold', label: 'Manifold', count: counts.manifold || 0 },
    { key: 'whale', label: 'Whale', count: counts.whale || 0 },
    { key: 'term', label: 'Term-1h', count: counts.term || 0 },
  ];

  return (
    <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
      {items.map((it) => {
        const isActive = active === it.key;
        return (
          <button
            key={it.key}
            type="button"
            onClick={() => onChange(it.key)}
            className="ed-kicker"
            style={{
              padding: '5px 11px',
              fontSize: 10,
              letterSpacing: '0.16em',
              textTransform: 'uppercase',
              background: isActive ? ACCENT_SOFT : 'transparent',
              border: `1px solid ${isActive ? ACCENT : RULE_STRONG}`,
              color: isActive ? ACCENT : DIM,
              cursor: 'pointer',
              borderRadius: 2,
              transition: 'all 150ms',
            }}
          >
            {it.label} <span style={{ color: FAINT, marginLeft: 4 }}>{it.count}</span>
          </button>
        );
      })}
    </div>
  );
}

function ActionCard({ item }: { item: ActionItem }) {
  const sm = SOURCE_META[item.source];
  const sev = SEVERITY_META[item.severity];
  const polyUrl = item.condition_id ? `https://polymarket.com/market/${item.condition_id}` : null;

  return (
    <article
      style={{
        background: BG_RAISED,
        border: `1px solid ${RULE_STRONG}`,
        borderLeft: `3px solid ${sm.color}`,
        padding: '12px 14px',
        borderRadius: 2,
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, justifyContent: 'space-between' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <span style={{ color: sm.color, fontSize: 14 }}>{sm.glyph}</span>
          <span
            className="ed-kicker"
            style={{
              fontSize: 9,
              letterSpacing: '0.20em',
              textTransform: 'uppercase',
              color: sm.color,
              fontWeight: 600,
            }}
          >
            {sm.label}
          </span>
          {item.category && (
            <span
              className="ed-kicker"
              style={{
                fontSize: 9,
                letterSpacing: '0.16em',
                textTransform: 'uppercase',
                color: FAINT,
              }}
            >
              · {item.category}
            </span>
          )}
        </div>
        <span
          className="ed-kicker"
          style={{
            fontSize: 9,
            fontWeight: 700,
            letterSpacing: '0.18em',
            padding: '2px 7px',
            background: sev.bg,
            color: sev.color,
            borderRadius: 2,
          }}
        >
          {sev.label}
        </span>
      </div>

      <div
        className="ed-display"
        style={{
          fontSize: 14,
          fontStyle: 'italic',
          fontWeight: 400,
          color: INK,
          letterSpacing: '-0.005em',
          lineHeight: 1.35,
        }}
      >
        {item.headline}
      </div>

      <div
        style={{
          fontSize: 12,
          color: DIM,
          lineHeight: 1.4,
        }}
      >
        {truncate(item.market_question ?? '—', 110)}
      </div>

      <ActionPayload item={item} />

      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          alignItems: 'center',
          marginTop: 4,
          paddingTop: 8,
          borderTop: `1px solid ${RULE}`,
        }}
      >
        <span
          className="ed-kicker"
          style={{ fontSize: 9, letterSpacing: '0.16em', color: FAINT, textTransform: 'uppercase' }}
        >
          {item.event_at ? formatRelativeTime(item.event_at) : '—'}
        </span>
        {polyUrl && (
          <a
            href={polyUrl}
            target="_blank"
            rel="noreferrer"
            className="ed-kicker"
            style={{
              fontSize: 9,
              letterSpacing: '0.18em',
              textTransform: 'uppercase',
              color: ACCENT,
              textDecoration: 'none',
            }}
          >
            Open ↗
          </a>
        )}
      </div>
    </article>
  );
}

function ActionPayload({ item }: { item: ActionItem }) {
  const p = item.payload as Record<string, unknown>;
  if (item.source === 'manifold') {
    const poly = num(p.poly_yes);
    const mf = num(p.mf_yes);
    const div = num(p.divergence_pp);
    const bets = num(p.mf_bets_7d);
    return (
      <PayloadGrid
        rows={[
          ['Poly YES', poly !== null ? `${(poly * 100).toFixed(1)}%` : '—', null],
          ['MF YES', mf !== null ? `${(mf * 100).toFixed(1)}%` : '—', null],
          [
            'Δ',
            div !== null ? `${div > 0 ? '+' : ''}${(div * 100).toFixed(1)}pp` : '—',
            div !== null ? (div > 0 ? UP : DOWN) : null,
          ],
          ['MF bets 7d', bets !== null ? String(bets) : '—', null],
        ]}
      />
    );
  }
  if (item.source === 'uma') {
    return (
      <PayloadGrid
        rows={[
          ['Status', strOr(p.status, '—'), null],
          ['Disputes', String(num(p.dispute_count) ?? '—'), null],
          ['Bond', formatUsd(num(p.bond)), null],
          ['Current px', priceOr(p.current_price), null],
        ]}
      />
    );
  }
  if (item.source === 'whale') {
    const rec = num(p.recovery_ratio);
    const impact = num(p.price_impact_50s);
    return (
      <PayloadGrid
        rows={[
          ['Trigger', strOr(p.trigger_type, '—'), null],
          [
            'Recovery 50s',
            rec !== null ? `${(rec * 100).toFixed(0)}%` : '—',
            rec !== null && rec < 0.7 ? DOWN : rec !== null ? UP : null,
          ],
          [
            'Δ price 50s',
            impact !== null ? `${impact > 0 ? '+' : ''}${(impact * 100).toFixed(2)}pp` : '—',
            impact !== null ? (impact >= 0 ? UP : DOWN) : null,
          ],
          ['Depth start', formatUsd(num(p.depth_at_start)), null],
        ]}
      />
    );
  }
  // term
  const ch1h = num(p.one_hour_change);
  const ch1d = num(p.one_day_change);
  const yes = num(p.yes_prob);
  const vol = num(p.volume_24h);
  return (
    <PayloadGrid
      rows={[
        ['YES', yes !== null ? `${(yes * 100).toFixed(1)}%` : '—', null],
        [
          '1h',
          ch1h !== null ? `${ch1h > 0 ? '+' : ''}${(ch1h * 100).toFixed(1)}pp` : '—',
          ch1h !== null ? (ch1h > 0 ? UP : DOWN) : null,
        ],
        [
          '24h',
          ch1d !== null ? `${ch1d > 0 ? '+' : ''}${(ch1d * 100).toFixed(1)}pp` : '—',
          ch1d !== null ? (ch1d > 0 ? UP : DOWN) : null,
        ],
        ['Vol 24h', formatUsd(vol), null],
      ]}
    />
  );
}

function PayloadGrid({ rows }: { rows: Array<[string, string, string | null]> }) {
  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: '4px 14px' }}>
      {rows.map(([label, value, color], i) => (
        <div key={i} style={{ display: 'flex', justifyContent: 'space-between', fontSize: 11 }}>
          <span
            className="ed-kicker"
            style={{ color: FAINT, letterSpacing: '0.12em', textTransform: 'uppercase', fontSize: 9 }}
          >
            {label}
          </span>
          <span
            style={{
              color: color ?? INK,
              fontFamily: 'var(--ed-mono)',
              fontWeight: 500,
            }}
          >
            {value}
          </span>
        </div>
      ))}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// KPI strip
// ─────────────────────────────────────────────────────────────

function KPIStrip() {
  const summary = useTier2Summary();
  if (summary.isLoading) return <Loading />;
  if (summary.error) return <ErrorBanner err={summary.error} />;
  if (!summary.data) return <Empty msg="No KPI data yet." />;

  const s = summary.data;
  return (
    <div
      style={{
        display: 'grid',
        gridTemplateColumns: 'repeat(auto-fit, minmax(240px, 1fr))',
        gap: 14,
      }}
    >
      <KPICard
        accent="#b4876a"
        title="UMA disputes"
        primary={s.uma.active_disputes}
        primaryLabel="active (proposed/disputed)"
        secondary={[
          ['multi-disputed', s.uma.multi_disputed],
          ['total with UMA history', s.uma.total_with_uma],
        ]}
        alertIf={s.uma.active_disputes > 0}
      />
      <KPICard
        accent="#5b9dff"
        title="Manifold pairs"
        primary={s.manifold.high_divergence_count}
        primaryLabel="divergence ≥10pp"
        secondary={[
          ['pairs tracked', s.manifold.pairs_total],
          ['active in 7d', s.manifold.active_manifold_pairs],
        ]}
        alertIf={s.manifold.high_divergence_count > 0}
      />
      <KPICard
        accent="#9b6cff"
        title="Whale shocks 24h"
        primary={s.whale_shocks.events_24h}
        primaryLabel="events (last 24h)"
        secondary={[
          [
            'avg recovery 50s',
            s.whale_shocks.avg_recovery_ratio !== null
              ? `${(s.whale_shocks.avg_recovery_ratio * 100).toFixed(0)}%`
              : '—',
          ],
          [
            'p95 Δprice',
            s.whale_shocks.p95_price_impact !== null
              ? `${(s.whale_shocks.p95_price_impact * 100).toFixed(2)}pp`
              : '—',
          ],
        ]}
        alertIf={s.whale_shocks.events_24h > 0}
      />
      <KPICard
        accent={ACCENT}
        title="Total events"
        primary={s.whale_shocks.events_total}
        primaryLabel="whale events all time"
        secondary={[
          ['UMA tracked', s.uma.total_with_uma],
          ['MF pairs', s.manifold.pairs_total],
        ]}
        alertIf={false}
      />
    </div>
  );
}

function KPICard({
  accent,
  title,
  primary,
  primaryLabel,
  secondary,
  alertIf,
}: {
  accent: string;
  title: string;
  primary: number;
  primaryLabel: string;
  secondary: Array<[string, number | string]>;
  alertIf: boolean;
}) {
  return (
    <div
      style={{
        background: BG_RAISED,
        border: `1px solid ${RULE_STRONG}`,
        padding: '14px 16px',
        borderRadius: 2,
        position: 'relative',
        overflow: 'hidden',
      }}
    >
      <div
        style={{
          position: 'absolute',
          left: 0,
          top: 0,
          bottom: 0,
          width: 3,
          background: accent,
          opacity: alertIf ? 1 : 0.4,
        }}
      />
      <div
        className="ed-kicker"
        style={{
          fontSize: 10,
          letterSpacing: '0.22em',
          textTransform: 'uppercase',
          color: accent,
          fontWeight: 600,
          marginBottom: 8,
        }}
      >
        {title}
      </div>
      <div
        className="ed-display"
        style={{
          fontSize: 36,
          fontWeight: 400,
          fontStyle: 'italic',
          color: alertIf ? accent : INK,
          letterSpacing: '-0.02em',
          lineHeight: 1,
        }}
      >
        {primary}
      </div>
      <div
        style={{
          fontSize: 11,
          color: DIM,
          marginTop: 4,
          marginBottom: 12,
        }}
      >
        {primaryLabel}
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {secondary.map(([label, value], i) => (
          <div
            key={i}
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              fontSize: 11,
              borderTop: i === 0 ? `1px solid ${RULE}` : 'none',
              paddingTop: i === 0 ? 8 : 0,
            }}
          >
            <span style={{ color: FAINT }}>{label}</span>
            <span style={{ color: INK, fontFamily: 'var(--ed-mono)' }}>{value}</span>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Manifold scatter
// ─────────────────────────────────────────────────────────────

function ManifoldScatter() {
  const { data, isLoading, error } = useManifoldDivergence({ limit: 200, min_divergence_pp: 0 });
  const [showAll, setShowAll] = useState(false);

  if (isLoading) return <Loading />;
  if (error) return <ErrorBanner err={error} />;
  if (!data?.items.length) return <Empty msg="Mapping пока пуст." />;

  const items = data.items.filter(
    (d) => d.polymarket_yes_prob !== null && d.manifold_yes_prob !== null,
  );

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <ManifoldSvg items={items} />
      <details
        open={showAll}
        onToggle={(e) => setShowAll((e.currentTarget as HTMLDetailsElement).open)}
        style={{ background: BG_RAISED, border: `1px solid ${RULE_STRONG}`, borderRadius: 2 }}
      >
        <summary
          className="ed-kicker"
          style={{
            padding: '10px 14px',
            cursor: 'pointer',
            fontSize: 10,
            letterSpacing: '0.18em',
            textTransform: 'uppercase',
            color: DIM,
            listStyle: 'none',
          }}
        >
          ▸ Drill-down table ({items.length})
        </summary>
        <ManifoldTable items={items} />
      </details>
    </div>
  );
}

function ManifoldSvg({ items }: { items: ManifoldDivergence[] }) {
  const W = 1000;
  const H = 480;
  const PAD = { top: 24, right: 24, bottom: 44, left: 56 };
  const [hover, setHover] = useState<ManifoldDivergence | null>(null);

  const xAt = (v: number) => PAD.left + v * (W - PAD.left - PAD.right);
  const yAt = (v: number) => H - PAD.bottom - v * (H - PAD.top - PAD.bottom);

  return (
    <div style={{ background: BG_RAISED, border: `1px solid ${RULE_STRONG}`, padding: 14, borderRadius: 2 }}>
      <svg
        viewBox={`0 0 ${W} ${H}`}
        style={{ width: '100%', height: 'auto' }}
        onMouseLeave={() => setHover(null)}
      >
        {/* Grid */}
        {[0, 0.25, 0.5, 0.75, 1].map((t) => (
          <g key={t}>
            <line x1={xAt(t)} x2={xAt(t)} y1={yAt(0)} y2={yAt(1)} stroke={RULE} strokeWidth={0.5} />
            <line x1={xAt(0)} x2={xAt(1)} y1={yAt(t)} y2={yAt(t)} stroke={RULE} strokeWidth={0.5} />
            <text
              x={xAt(t)}
              y={yAt(0) + 16}
              fontSize={10}
              fontFamily="var(--ed-mono)"
              fill={FAINT}
              textAnchor="middle"
            >
              {(t * 100).toFixed(0)}%
            </text>
            <text
              x={xAt(0) - 8}
              y={yAt(t) + 3}
              fontSize={10}
              fontFamily="var(--ed-mono)"
              fill={FAINT}
              textAnchor="end"
            >
              {(t * 100).toFixed(0)}%
            </text>
          </g>
        ))}

        {/* 45° diagonal — perfect agreement */}
        <line
          x1={xAt(0)}
          y1={yAt(0)}
          x2={xAt(1)}
          y2={yAt(1)}
          stroke={ACCENT}
          strokeWidth={1}
          strokeDasharray="4 4"
          opacity={0.6}
        />

        {/* ±10pp tolerance band */}
        <line
          x1={xAt(0.1)}
          y1={yAt(0)}
          x2={xAt(1)}
          y2={yAt(0.9)}
          stroke={ACCENT}
          strokeWidth={0.5}
          strokeDasharray="2 4"
          opacity={0.3}
        />
        <line
          x1={xAt(0)}
          y1={yAt(0.1)}
          x2={xAt(0.9)}
          y2={yAt(1)}
          stroke={ACCENT}
          strokeWidth={0.5}
          strokeDasharray="2 4"
          opacity={0.3}
        />

        {/* Axis labels */}
        <text
          x={(xAt(0) + xAt(1)) / 2}
          y={H - 8}
          fontSize={11}
          fontFamily="var(--ed-body)"
          fill={DIM}
          textAnchor="middle"
        >
          Polymarket YES probability →
        </text>
        <text
          x={16}
          y={(yAt(0) + yAt(1)) / 2}
          fontSize={11}
          fontFamily="var(--ed-body)"
          fill={DIM}
          textAnchor="middle"
          transform={`rotate(-90 16 ${(yAt(0) + yAt(1)) / 2})`}
        >
          Manifold YES probability →
        </text>

        {/* Dots */}
        {items.map((d, i) => {
          const x = xAt(d.polymarket_yes_prob ?? 0);
          const y = yAt(d.manifold_yes_prob ?? 0);
          const abs = d.abs_divergence_pp ?? 0;
          const color = abs >= 0.15 ? '#d65a5a' : abs >= 0.10 ? '#c9a961' : '#5b9dff';
          const r = abs >= 0.10 ? 5 : 3;
          const opacity = abs >= 0.10 ? 0.95 : 0.55;
          const active = hover?.canonical_event_id === d.canonical_event_id;
          return (
            <circle
              key={d.canonical_event_id ?? i}
              cx={x}
              cy={y}
              r={active ? r + 2 : r}
              fill={color}
              opacity={opacity}
              stroke={active ? INK : 'transparent'}
              strokeWidth={1.5}
              style={{ cursor: 'pointer' }}
              onMouseEnter={() => setHover(d)}
            />
          );
        })}
      </svg>

      {/* Hover details */}
      {hover ? (
        <div
          style={{
            marginTop: 10,
            padding: '10px 14px',
            background: 'rgba(255,255,255,0.04)',
            border: `1px solid ${RULE_STRONG}`,
            fontSize: 12,
          }}
        >
          <div
            className="ed-display"
            style={{ fontSize: 13, fontStyle: 'italic', color: INK, marginBottom: 6 }}
          >
            {truncate(hover.polymarket_question, 100)}
          </div>
          <div style={{ display: 'flex', gap: 18, color: DIM, fontFamily: 'var(--ed-mono)', fontSize: 11 }}>
            <span>
              Poly{' '}
              <span style={{ color: INK }}>
                {((hover.polymarket_yes_prob ?? 0) * 100).toFixed(1)}%
              </span>
            </span>
            <span>
              MF{' '}
              <span style={{ color: INK }}>
                {((hover.manifold_yes_prob ?? 0) * 100).toFixed(1)}%
              </span>
            </span>
            <span>
              Δ{' '}
              <span style={{ color: (hover.divergence_pp ?? 0) > 0 ? UP : DOWN }}>
                {(hover.divergence_pp ?? 0) > 0 ? '+' : ''}
                {((hover.divergence_pp ?? 0) * 100).toFixed(1)}pp
              </span>
            </span>
            <span>
              MF bets 7d <span style={{ color: INK }}>{hover.manifold_bets_7d ?? 0}</span>
            </span>
          </div>
        </div>
      ) : (
        <div
          style={{
            marginTop: 10,
            fontSize: 11,
            color: FAINT,
            display: 'flex',
            gap: 16,
            justifyContent: 'center',
          }}
        >
          <LegendDot color="#5b9dff" label="<10pp" />
          <LegendDot color="#c9a961" label="10–15pp" />
          <LegendDot color="#d65a5a" label="≥15pp" />
          <span>{items.length} pairs</span>
        </div>
      )}
    </div>
  );
}

function ManifoldTable({ items }: { items: ManifoldDivergence[] }) {
  return (
    <Table
      headers={['Question', 'Poly YES', 'MF YES', 'Δpp', 'MF bets 7d', 'Last MF bet']}
      rows={items.slice(0, 100).map((d) => [
        truncate(d.polymarket_question, 60),
        formatPercent(d.polymarket_yes_prob),
        formatPercent(d.manifold_yes_prob),
        d.divergence_pp !== null
          ? `${d.divergence_pp > 0 ? '+' : ''}${(d.divergence_pp * 100).toFixed(1)}`
          : '—',
        d.manifold_bets_7d ?? 0,
        d.last_manifold_bet ? formatRelativeTime(d.last_manifold_bet) : '—',
      ])}
    />
  );
}

// ─────────────────────────────────────────────────────────────
// UMA block
// ─────────────────────────────────────────────────────────────

function UmaBlock() {
  const { data, isLoading, error } = useUmaDisputes({ limit: 100, min_disputes: 0 });
  if (isLoading) return <Loading />;
  if (error) return <ErrorBanner err={error} />;
  if (!data?.items.length) return <Empty msg="Нет markets с UMA-историей. Подожди market-sync." />;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <UmaStatusBars items={data.items} />
      <details style={{ background: BG_RAISED, border: `1px solid ${RULE_STRONG}` }}>
        <summary
          className="ed-kicker"
          style={{
            padding: '10px 14px',
            cursor: 'pointer',
            fontSize: 10,
            letterSpacing: '0.18em',
            textTransform: 'uppercase',
            color: DIM,
            listStyle: 'none',
          }}
        >
          ▸ Drill-down table ({data.items.length})
        </summary>
        <Table
          headers={['Market', 'Cat', 'Status', '# Disputes', 'Bond', 'Price', 'Resolved', 'Winner', 'Updated']}
          rows={data.items.map((d) => [
            truncate(d.question, 55),
            d.category ?? '—',
            d.current_status ?? '—',
            d.dispute_count,
            d.uma_bond ? formatUsd(d.uma_bond) : '—',
            d.current_price !== null ? formatPercent(d.current_price) : '—',
            d.resolved ? '✓' : '—',
            d.winner ?? '—',
            formatRelativeTime(d.updated_at),
          ])}
        />
      </details>
    </div>
  );
}

function UmaStatusBars({ items }: { items: UmaDispute[] }) {
  const byCategory = useMemo(() => {
    const acc: Record<string, { proposed: number; disputed: number; resolved: number; other: number; total: number }> =
      {};
    for (const it of items) {
      const cat = it.category ?? 'unknown';
      if (!acc[cat]) acc[cat] = { proposed: 0, disputed: 0, resolved: 0, other: 0, total: 0 };
      acc[cat].total += 1;
      if (it.current_status === 'proposed') acc[cat].proposed += 1;
      else if (it.current_status === 'disputed') acc[cat].disputed += 1;
      else if (it.resolved) acc[cat].resolved += 1;
      else acc[cat].other += 1;
    }
    return Object.entries(acc)
      .sort(([, a], [, b]) => b.total - a.total)
      .slice(0, 12);
  }, [items]);

  const maxTotal = Math.max(1, ...byCategory.map(([, v]) => v.total));

  return (
    <div style={{ background: BG_RAISED, border: `1px solid ${RULE_STRONG}`, padding: 14, borderRadius: 2 }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          marginBottom: 10,
          alignItems: 'baseline',
        }}
      >
        <div
          className="ed-kicker"
          style={{ fontSize: 10, letterSpacing: '0.22em', color: DIM, textTransform: 'uppercase' }}
        >
          Markets per category × status (top 12)
        </div>
        <div style={{ display: 'flex', gap: 12, fontSize: 10, color: DIM }}>
          <Legend color="#d65a5a" label="disputed" />
          <Legend color="#c9a961" label="proposed" />
          <Legend color="#5fb97a" label="resolved" />
          <Legend color="#555250" label="other" />
        </div>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        {byCategory.map(([cat, v]) => (
          <div key={cat} style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            <div
              style={{
                width: 140,
                fontSize: 11,
                color: DIM,
                whiteSpace: 'nowrap',
                overflow: 'hidden',
                textOverflow: 'ellipsis',
                fontFamily: 'var(--ed-mono)',
              }}
              title={cat}
            >
              {cat}
            </div>
            <div style={{ flex: 1, height: 14, display: 'flex', background: 'rgba(255,255,255,0.02)' }}>
              <div
                style={{ background: '#d65a5a', width: `${(v.disputed / maxTotal) * 100}%`, height: '100%' }}
                title={`disputed: ${v.disputed}`}
              />
              <div
                style={{ background: '#c9a961', width: `${(v.proposed / maxTotal) * 100}%`, height: '100%' }}
                title={`proposed: ${v.proposed}`}
              />
              <div
                style={{ background: '#5fb97a', width: `${(v.resolved / maxTotal) * 100}%`, height: '100%' }}
                title={`resolved: ${v.resolved}`}
              />
              <div
                style={{ background: '#555250', width: `${(v.other / maxTotal) * 100}%`, height: '100%' }}
                title={`other: ${v.other}`}
              />
            </div>
            <div
              style={{
                width: 36,
                textAlign: 'right',
                fontFamily: 'var(--ed-mono)',
                fontSize: 11,
                color: INK,
              }}
            >
              {v.total}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Whale block
// ─────────────────────────────────────────────────────────────

function WhaleBlock() {
  const { data, isLoading, error } = useWhaleShocks({ limit: 200 });
  if (isLoading) return <Loading />;
  if (error) return <ErrorBanner err={error} />;
  if (!data?.items.length) return <Empty msg="Нет shock events. Жди whale trade ≥$50k." />;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <WhaleRecoveryHistogram items={data.items} />
      <details style={{ background: BG_RAISED, border: `1px solid ${RULE_STRONG}` }}>
        <summary
          className="ed-kicker"
          style={{
            padding: '10px 14px',
            cursor: 'pointer',
            fontSize: 10,
            letterSpacing: '0.18em',
            textTransform: 'uppercase',
            color: DIM,
            listStyle: 'none',
          }}
        >
          ▸ Drill-down events ({data.items.length})
        </summary>
        <Table
          headers={['Time', 'Trigger', 'Market', 'Recovery', 'Δprice 50s', 'Spread Δ']}
          rows={data.items.slice(0, 80).map((s) => [
            formatRelativeTime(s.event_start),
            s.trigger_type,
            truncate(s.question ?? s.condition_id, 40),
            s.depth_recovery_ratio !== null ? `${(s.depth_recovery_ratio * 100).toFixed(0)}%` : '—',
            s.price_impact_50s !== null
              ? `${s.price_impact_50s > 0 ? '+' : ''}${(s.price_impact_50s * 100).toFixed(2)}pp`
              : '—',
            s.spread_at_start !== null && s.spread_at_50s !== null
              ? `${s.spread_at_start.toFixed(4)} → ${s.spread_at_50s.toFixed(4)}`
              : '—',
          ])}
        />
      </details>
    </div>
  );
}

function WhaleRecoveryHistogram({ items }: { items: WhaleShock[] }) {
  const W = 1000;
  const H = 280;
  const PAD = { top: 20, right: 16, bottom: 36, left: 48 };

  const bins = useMemo(() => {
    // 10 bins from 0 to 1.2 (cap)
    const edges = [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0, 1.2];
    const counts = new Array(edges.length - 1).fill(0);
    for (const it of items) {
      const r = it.depth_recovery_ratio;
      if (r === null) continue;
      const clamped = Math.min(r, 1.19);
      const idx = edges.findIndex((e, i) => i < edges.length - 1 && clamped >= e && clamped < edges[i + 1]);
      if (idx >= 0) counts[idx] += 1;
    }
    return { edges, counts };
  }, [items]);

  const maxCount = Math.max(1, ...bins.counts);
  const xAt = (i: number) => PAD.left + (i / (bins.edges.length - 1)) * (W - PAD.left - PAD.right);
  const yAt = (v: number) => H - PAD.bottom - (v / maxCount) * (H - PAD.top - PAD.bottom);

  const median = useMemo(() => {
    const vals = items.map((i) => i.depth_recovery_ratio).filter((v): v is number => v !== null).sort((a, b) => a - b);
    return vals.length ? vals[Math.floor(vals.length / 2)] : null;
  }, [items]);

  return (
    <div style={{ background: BG_RAISED, border: `1px solid ${RULE_STRONG}`, padding: 14, borderRadius: 2 }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          marginBottom: 8,
          alignItems: 'baseline',
        }}
      >
        <div
          className="ed-kicker"
          style={{ fontSize: 10, letterSpacing: '0.22em', color: DIM, textTransform: 'uppercase' }}
        >
          Depth recovery ratio @ 50s
        </div>
        <div style={{ display: 'flex', gap: 16, fontSize: 11, color: DIM, fontFamily: 'var(--ed-mono)' }}>
          <span>
            Median <span style={{ color: INK }}>{median !== null ? `${(median * 100).toFixed(0)}%` : '—'}</span>
          </span>
          <span>
            Fragile (&lt;70%){' '}
            <span style={{ color: DOWN }}>
              {bins.counts.slice(0, 7).reduce((s, n) => s + n, 0)}
            </span>
          </span>
        </div>
      </div>
      <svg viewBox={`0 0 ${W} ${H}`} style={{ width: '100%', height: 'auto' }}>
        {/* Y grid */}
        {[0, 0.25, 0.5, 0.75, 1].map((t) => {
          const y = yAt(maxCount * t);
          return (
            <g key={t}>
              <line x1={PAD.left} x2={W - PAD.right} y1={y} y2={y} stroke={RULE} strokeWidth={0.5} />
              <text x={PAD.left - 6} y={y + 3} fontSize={10} fontFamily="var(--ed-mono)" fill={FAINT} textAnchor="end">
                {Math.round(maxCount * t)}
              </text>
            </g>
          );
        })}
        {/* 70% threshold */}
        <line
          x1={xAt(7)}
          x2={xAt(7)}
          y1={PAD.top}
          y2={H - PAD.bottom}
          stroke={DOWN}
          strokeWidth={1}
          strokeDasharray="4 3"
          opacity={0.6}
        />
        <text
          x={xAt(7) - 4}
          y={PAD.top + 12}
          fontSize={10}
          fontFamily="var(--ed-mono)"
          fill={DOWN}
          textAnchor="end"
        >
          70% threshold
        </text>
        {/* Bars */}
        {bins.counts.map((c, i) => {
          const x0 = xAt(i);
          const x1 = xAt(i + 1);
          const y = yAt(c);
          const color = i < 7 ? DOWN : UP;
          return (
            <g key={i}>
              <rect
                x={x0 + 2}
                y={y}
                width={x1 - x0 - 4}
                height={H - PAD.bottom - y}
                fill={color}
                opacity={0.7}
              />
              <text
                x={(x0 + x1) / 2}
                y={H - PAD.bottom + 16}
                fontSize={10}
                fontFamily="var(--ed-mono)"
                fill={FAINT}
                textAnchor="middle"
              >
                {Math.round(bins.edges[i] * 100)}
              </text>
            </g>
          );
        })}
        <text
          x={(PAD.left + W - PAD.right) / 2}
          y={H - 4}
          fontSize={11}
          fontFamily="var(--ed-body)"
          fill={DIM}
          textAnchor="middle"
        >
          recovery, % of pre-shock depth
        </text>
      </svg>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Term-structure block
// ─────────────────────────────────────────────────────────────

function TermBlock() {
  const { data, isLoading, error } = useTermStructure({ limit: 200 });
  if (isLoading) return <Loading />;
  if (error) return <ErrorBanner err={error} />;
  if (!data?.items.length) return <Empty msg="Нет term-structure data — подожди market-sync." />;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <TermRegimeChart items={data.items} />
      <details style={{ background: BG_RAISED, border: `1px solid ${RULE_STRONG}` }}>
        <summary
          className="ed-kicker"
          style={{
            padding: '10px 14px',
            cursor: 'pointer',
            fontSize: 10,
            letterSpacing: '0.18em',
            textTransform: 'uppercase',
            color: DIM,
            listStyle: 'none',
          }}
        >
          ▸ Drill-down ({data.items.length})
        </summary>
        <Table
          headers={['Market', 'Cat', 'YES', '1h', '24h', '7d', 'Vol 24h', 'Regime']}
          rows={data.items.slice(0, 100).map((m) => [
            truncate(m.question, 50),
            m.category ?? '—',
            formatPercent(m.yes_prob_current),
            formatSigned(m.one_hour_price_change),
            formatSigned(m.one_day_price_change),
            formatSigned(m.one_week_price_change),
            formatUsd(m.volume_24h),
            m.momentum_regime_1h ?? '—',
          ])}
        />
      </details>
    </div>
  );
}

function TermRegimeChart({ items }: { items: TermStructureItem[] }) {
  const regimes: Array<{ key: string; label: string; color: string }> = [
    { key: 'strong_down_1h', label: 'strong down', color: '#d65a5a' },
    { key: 'mild_down_1h', label: 'mild down', color: '#b4876a' },
    { key: 'flat', label: 'flat', color: '#555250' },
    { key: 'mild_up_1h', label: 'mild up', color: '#9bb872' },
    { key: 'strong_up_1h', label: 'strong up', color: '#5fb97a' },
  ];

  const counts = useMemo(() => {
    const acc: Record<string, number> = {};
    for (const it of items) {
      const r = it.momentum_regime_1h ?? 'flat';
      acc[r] = (acc[r] || 0) + 1;
    }
    return acc;
  }, [items]);

  const totalVolByRegime = useMemo(() => {
    const acc: Record<string, number> = {};
    for (const it of items) {
      const r = it.momentum_regime_1h ?? 'flat';
      acc[r] = (acc[r] || 0) + (it.volume_24h ?? 0);
    }
    return acc;
  }, [items]);

  const maxCount = Math.max(1, ...Object.values(counts));

  return (
    <div
      style={{
        background: BG_RAISED,
        border: `1px solid ${RULE_STRONG}`,
        padding: 14,
        borderRadius: 2,
      }}
    >
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          marginBottom: 12,
          alignItems: 'baseline',
        }}
      >
        <div
          className="ed-kicker"
          style={{ fontSize: 10, letterSpacing: '0.22em', color: DIM, textTransform: 'uppercase' }}
        >
          Markets per momentum regime (1h)
        </div>
        <div style={{ fontSize: 11, color: DIM, fontFamily: 'var(--ed-mono)' }}>
          Total <span style={{ color: INK }}>{items.length}</span>
        </div>
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 10 }}>
        {regimes.map((r) => {
          const c = counts[r.key] || 0;
          const vol = totalVolByRegime[r.key] || 0;
          const h = Math.max(4, (c / maxCount) * 140);
          return (
            <div key={r.key} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center' }}>
              <div
                className="ed-display"
                style={{
                  fontSize: 22,
                  fontStyle: 'italic',
                  fontWeight: 400,
                  color: c > 0 ? r.color : FAINT,
                  marginBottom: 6,
                }}
              >
                {c}
              </div>
              <div
                style={{
                  width: '70%',
                  height: h,
                  background: r.color,
                  opacity: c > 0 ? 0.8 : 0.15,
                  borderRadius: 2,
                  marginBottom: 8,
                }}
              />
              <div
                className="ed-kicker"
                style={{
                  fontSize: 10,
                  letterSpacing: '0.16em',
                  color: r.color,
                  textTransform: 'uppercase',
                  marginBottom: 4,
                }}
              >
                {r.label}
              </div>
              <div style={{ fontSize: 10, color: FAINT, fontFamily: 'var(--ed-mono)' }}>
                vol {formatUsd(vol)}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Cohort block
// ─────────────────────────────────────────────────────────────

function CohortBlock() {
  const { data, isLoading, error } = useCohortStats();
  if (isLoading) return <Loading />;
  if (error) return <ErrorBanner err={error} />;
  if (!data?.items.length) return <Empty msg="Cohort stats пусты." />;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      <CohortWinRateChart items={data.items} />
      <details style={{ background: BG_RAISED, border: `1px solid ${RULE_STRONG}` }}>
        <summary
          className="ed-kicker"
          style={{
            padding: '10px 14px',
            cursor: 'pointer',
            fontSize: 10,
            letterSpacing: '0.18em',
            textTransform: 'uppercase',
            color: DIM,
            listStyle: 'none',
          }}
        >
          ▸ Drill-down ({data.items.length})
        </summary>
        <Table
          headers={['Category', 'cyom', 'restricted', 'NR-aug', '# markets', '# resolved', 'YES wr', 'Vol 24h']}
          rows={data.items.map((c) => [
            c.category ?? '—',
            c.is_cyom ? '✓' : '—',
            c.is_restricted ? '✓' : '—',
            c.is_neg_risk_aug ? '✓' : '—',
            c.n_markets,
            c.n_resolved,
            c.yes_win_rate !== null ? `${(c.yes_win_rate * 100).toFixed(1)}%` : '—',
            formatUsd(c.total_volume_24h),
          ])}
        />
      </details>
    </div>
  );
}

function CohortWinRateChart({ items }: { items: CohortStats[] }) {
  // Группируем по cohort flag комбинации
  const groups = useMemo(() => {
    const acc: Record<
      string,
      { label: string; n_resolved: number; n_yes_won: number; total_volume: number }
    > = {};
    for (const it of items) {
      const key = `${it.is_cyom ? 'C' : '·'}${it.is_restricted ? 'R' : '·'}${it.is_neg_risk_aug ? 'N' : '·'}`;
      if (!acc[key]) acc[key] = { label: key, n_resolved: 0, n_yes_won: 0, total_volume: 0 };
      acc[key].n_resolved += it.n_resolved;
      acc[key].n_yes_won += it.n_yes_won;
      acc[key].total_volume += it.total_volume_24h ?? 0;
    }
    return Object.values(acc)
      .filter((g) => g.n_resolved >= 5)
      .map((g) => ({ ...g, yes_win_rate: g.n_yes_won / g.n_resolved }))
      .sort((a, b) => b.n_resolved - a.n_resolved);
  }, [items]);

  if (!groups.length) {
    return (
      <div style={{ background: BG_RAISED, border: `1px solid ${RULE_STRONG}`, padding: 14, color: DIM }}>
        Недостаточно resolved markets для cohort-аналитики (нужно ≥5 в группе).
      </div>
    );
  }

  return (
    <div style={{ background: BG_RAISED, border: `1px solid ${RULE_STRONG}`, padding: 14, borderRadius: 2 }}>
      <div
        style={{
          display: 'flex',
          justifyContent: 'space-between',
          marginBottom: 12,
          alignItems: 'baseline',
        }}
      >
        <div
          className="ed-kicker"
          style={{ fontSize: 10, letterSpacing: '0.22em', color: DIM, textTransform: 'uppercase' }}
        >
          YES win-rate by cohort (C=cyom · R=restricted · N=NR-aug)
        </div>
        <div style={{ fontSize: 11, color: FAINT, fontFamily: 'var(--ed-mono)' }}>
          baseline 50%
        </div>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        {groups.map((g) => {
          const delta = g.yes_win_rate - 0.5;
          const barW = Math.abs(delta) * 200; // 50pp range
          const color = delta > 0.02 ? UP : delta < -0.02 ? DOWN : FAINT;
          return (
            <div key={g.label} style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
              <div
                style={{
                  width: 60,
                  fontFamily: 'var(--ed-mono)',
                  fontSize: 12,
                  color: INK,
                  letterSpacing: '0.08em',
                }}
              >
                {g.label}
              </div>
              <div
                style={{
                  flex: 1,
                  display: 'flex',
                  justifyContent: 'center',
                  position: 'relative',
                  height: 20,
                  background: 'rgba(255,255,255,0.02)',
                }}
              >
                {/* center line at 50% */}
                <div
                  style={{
                    position: 'absolute',
                    left: '50%',
                    top: 0,
                    bottom: 0,
                    width: 1,
                    background: RULE_STRONG,
                  }}
                />
                <div
                  style={{
                    position: 'absolute',
                    height: '100%',
                    background: color,
                    opacity: 0.7,
                    width: barW,
                    [delta >= 0 ? 'left' : 'right']: '50%',
                  }}
                />
              </div>
              <div
                style={{
                  width: 64,
                  textAlign: 'right',
                  fontFamily: 'var(--ed-mono)',
                  fontSize: 12,
                  color,
                }}
              >
                {(g.yes_win_rate * 100).toFixed(1)}%
              </div>
              <div
                style={{
                  width: 50,
                  textAlign: 'right',
                  fontFamily: 'var(--ed-mono)',
                  fontSize: 11,
                  color: FAINT,
                }}
              >
                n={g.n_resolved}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// ML Accuracy block (Diagnostics)
// ─────────────────────────────────────────────────────────────

const FLAG_EMOJI: Record<string, string> = { RELIABLE: '✅', WEAK: '⚠️', ARTIFACT: '❌' };

function fmtNum(v: number | null | undefined, digits = 4): string {
  return v === null || v === undefined ? '—' : v.toFixed(digits);
}
function fmtPct(v: number | null | undefined): string {
  return v === null || v === undefined ? '—' : `${v >= 0 ? '+' : ''}${v.toFixed(1)}%`;
}

function honestyRows(rows: MlAccuracyLayerRow[]): (string | number)[][] {
  // anchor-метрики: edge над рынком, Brier, ECE, slope, AUC
  return [...rows]
    .sort((a, b) => a.scope.localeCompare(b.scope))
    .map((r) => {
      const m = r.metrics;
      const edge = m.edge_vs_market;
      const edgeStr = edge === null || edge === undefined ? '—' : `${edge >= 0 ? '+' : ''}${edge.toFixed(4)}`;
      return [
        r.scope,
        r.n_events ?? '—',
        fmtNum(m.per_event_brier),
        fmtNum(m.market_brier),
        edgeStr,
        fmtNum(m.ece),
        fmtNum(m.calibration_slope, 2),
        fmtNum(m.auc, 3),
      ];
    });
}

function selectionRows(rows: MlAccuracyLayerRow[]): (string | number)[][] {
  return [...rows]
    .sort((a, b) => a.scope.localeCompare(b.scope))
    .map((r) => {
      const m = r.metrics;
      return [
        r.scope,
        fmtNum(m.top1_per_event_accuracy, 3),
        fmtNum(m.precision_at_k, 3),
        fmtNum(m.precision_at_k_wilson_lo, 3),
        m.selection_lift === null || m.selection_lift === undefined ? '—' : m.selection_lift.toFixed(1),
        fmtNum(m.market_lift_brier, 3),
      ];
    });
}

function moneyRows(rows: MlAccuracyMoneyRow[]): (string | number)[][] {
  const order: Record<string, number> = { RELIABLE: 0, WEAK: 1, ARTIFACT: 2 };
  return [...rows]
    .sort((a, b) => {
      const fa = order[a.reliability ?? 'WEAK'] ?? 1;
      const fb = order[b.reliability ?? 'WEAK'] ?? 1;
      return fa - fb || (b.avg_roi_net_pct ?? 0) - (a.avg_roi_net_pct ?? 0);
    })
    .map((r) => [
      `${FLAG_EMOJI[r.reliability ?? ''] ?? ''} ${r.reliability ?? '—'}`,
      r.detector,
      r.category,
      r.n_signals ?? '—',
      r.n_events ?? '—',
      fmtPct(r.avg_roi_net_pct),
      r.event_concentration === undefined ? '—' : `${(r.event_concentration * 100).toFixed(0)}%`,
      r.loo_avg_roi_pct === null || r.loo_avg_roi_pct === undefined ? '—' : `${r.loo_avg_roi_pct >= 0 ? '+' : ''}${r.loo_avg_roi_pct.toFixed(1)}`,
      r.side_no_share === undefined ? '—' : `${(r.side_no_share * 100).toFixed(0)}%`,
    ]);
}

function ModelStatusBadges({ rows }: { rows?: MlModelStatus[] }) {
  // Date.now() в lazy-инициализаторе useState — единственное чистое место в render
  // (тот же паттерн, что freshnessLabel выше). Badge не тикает, разовый снимок ОК.
  const [now] = useState(() => Date.now());
  if (!rows || rows.length === 0) return null;
  return (
    <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8, marginBottom: 4 }}>
      {rows.map((m) => {
        const active = m.active;
        const ageH = m.fit_at ? Math.round((now - new Date(m.fit_at).getTime()) / 3_600_000) : null;
        // frozen (gate честно блокирует), не баг. stale = активна, но fit давно.
        const stale = active && ageH !== null && ageH > 24 * 8;
        const color = active ? (stale ? DOWN : UP) : DOWN;
        const glyph = active ? (stale ? '⚠' : '✓') : '🔒';
        const label = active
          ? stale ? 'STALE' : 'ACTIVE'
          : 'FROZEN (gate)';
        const ageStr = ageH === null ? '' : ageH > 48 ? ` · fit ${Math.round(ageH / 24)}d` : ` · fit ${ageH}h`;
        const title = m.skip_reason
          ? `${m.label}: ${m.skip_reason}`
          : active
            ? `${m.label}: активна${ageStr}`
            : `${m.label}: ${m.state ?? 'не промоутнута gate-ом (нестационарность, не поломка)'}`;
        return (
          <span
            key={m.label}
            title={title}
            style={{
              display: 'inline-flex', alignItems: 'center', gap: 5,
              fontSize: 11, padding: '3px 9px', borderRadius: 4,
              border: `1px solid ${color}`, color, whiteSpace: 'nowrap',
            }}
          >
            <span style={{ opacity: 0.85 }}>{glyph}</span>
            <span style={{ color: INK }}>{m.label}</span>
            <span>{label}{ageStr}</span>
          </span>
        );
      })}
    </div>
  );
}

function MlAccuracyBlock() {
  const { data, isLoading, error } = useMlAccuracy();
  if (isLoading) return <Loading />;
  if (error) return <ErrorBanner err={error} />;
  if (!data || !data.freshness?.snapshot_at) {
    return (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        <ModelStatusBadges rows={data?.model_status} />
        <Empty msg="Снимок точности ещё не построен (cron ml-accuracy-snapshot, Sun 06:45 UTC). Статус моделей выше: FROZEN = gate честно блокирует (нестационарность), не поломка." />
      </div>
    );
  }

  const honesty = data.layers.honesty ?? [];
  const selection = data.layers.selection ?? [];
  const money = data.money ?? [];
  const artifacts = money.filter((m) => m.reliability === 'ARTIFACT');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
      <ModelStatusBadges rows={data.model_status} />

      <div style={{ fontSize: 11, color: DIM, fontStyle: 'italic' }}>
        Снимок: {new Date(data.freshness.snapshot_at).toLocaleString()}
      </div>

      <div>
        <div className="ed-kicker" style={{ fontSize: 10, color: ACCENT, marginBottom: 6, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
          Слой 1 · честность вероятности (STANDALONE, per anchor)
        </div>
        <Table
          headers={['anchor', 'n_ev', 'event Brier', 'market Brier', 'edge', 'ECE', 'slope', 'AUC']}
          rows={honestyRows(honesty)}
        />
        <div style={{ fontSize: 10, color: DIM, marginTop: 6 }}>
          edge = market − model Brier (&gt;0 → бьёт рынок). slope≈1 = калибровано.
        </div>
      </div>

      <div>
        <div className="ed-kicker" style={{ fontSize: 10, color: ACCENT, marginBottom: 6, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
          Слой 2 · куда ставить (качество отбора)
        </div>
        <Table
          headers={['anchor', 'top1/event', 'precision@K', 'Wilson lo', 'lift', 'market-BSS']}
          rows={selectionRows(selection)}
        />
      </div>

      <div>
        <div className="ed-kicker" style={{ fontSize: 10, color: ACCENT, marginBottom: 6, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
          Деньги · ROI net по категориям (после fees)
        </div>
        <Table
          headers={['flag', 'detector', 'category', 'n_sig', 'n_ev', 'avg ROI', 'conc', 'LOO', 'NO%']}
          rows={moneyRows(money)}
        />
        {artifacts.length > 0 && (
          <div style={{ fontSize: 10, color: DOWN, marginTop: 8 }}>
            {artifacts.length} артефактов (концентрация / side-перекос / LOO-переворот) — не доверять цифрам.
          </div>
        )}
      </div>
    </div>
  );
}

function HonestAuditBlock() {
  const { data, isLoading, error } = useHonestAudit();
  if (isLoading) return <Loading />;
  if (error) return <ErrorBanner err={error} />;
  if (!data || data.rows.length === 0) {
    return <Empty msg="Нет свежих resolved-сигналов за окно (forward-валидация по живым detector_signals)." />;
  }
  const rows: (string | number)[][] = data.rows.map((r: HonestAuditRow) => [
    r.verdict,
    `${r.detector} [${r.category}]${r.trend ? ` ${r.trend}` : ''}`,
    r.n_dedup.toLocaleString(),
    `${r.per_day_1k >= 0 ? '+' : ''}${r.per_day_1k.toFixed(1)}`,
  ]);
  return (
    <div>
      <div className="ed-kicker" style={{ fontSize: 10, color: ACCENT, marginBottom: 6, letterSpacing: '0.18em', textTransform: 'uppercase' }}>
        forward-валидация · honest PnL по детектор×категория (за {data.days}д)
      </div>
      <Table headers={['вердикт', 'детектор [кат] тренд', 'n', '$/day @$1K']} rows={rows} />
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Shared atoms
// ─────────────────────────────────────────────────────────────

function Table({ headers, rows }: { headers: string[]; rows: (string | number)[][] }) {
  return (
    <div style={{ overflowX: 'auto' }}>
      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: 12 }}>
        <thead>
          <tr style={{ background: 'rgba(255,255,255,0.02)' }}>
            {headers.map((h) => (
              <th
                key={h}
                style={{
                  padding: '8px 12px',
                  textAlign: 'left',
                  color: FAINT,
                  fontWeight: 500,
                  borderTop: `1px solid ${RULE_STRONG}`,
                  borderBottom: `1px solid ${RULE_STRONG}`,
                  fontSize: 10,
                  letterSpacing: '0.18em',
                  textTransform: 'uppercase',
                }}
              >
                {h}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, i) => (
            <tr key={i} style={{ borderBottom: `1px solid ${RULE}` }}>
              {row.map((cell, j) => (
                <td
                  key={j}
                  style={{
                    padding: '6px 12px',
                    color: INK,
                    fontFamily: j === 0 ? 'var(--ed-body)' : 'var(--ed-mono)',
                  }}
                >
                  {cell}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      <span style={{ width: 10, height: 10, background: color, display: 'inline-block', borderRadius: 1 }} />
      {label}
    </span>
  );
}

function LegendDot({ color, label }: { color: string; label: string }) {
  return (
    <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4 }}>
      <span style={{ width: 8, height: 8, background: color, display: 'inline-block', borderRadius: '50%' }} />
      {label}
    </span>
  );
}

function Loading() {
  return <div style={{ padding: 24, color: DIM, fontStyle: 'italic' }}>Загружается…</div>;
}

function Empty({ msg }: { msg: string }) {
  return (
    <div style={{ padding: 24, color: DIM, fontStyle: 'italic', textAlign: 'center' }}>{msg}</div>
  );
}

function ErrorBanner({ err }: { err: unknown }) {
  const msg = err instanceof Error ? err.message : String(err);
  return (
    <div
      style={{
        padding: 14,
        background: 'rgba(214, 90, 90, 0.08)',
        border: '1px solid rgba(214, 90, 90, 0.3)',
        color: '#d65a5a',
        fontSize: 12,
      }}
    >
      Ошибка: {msg}
    </div>
  );
}

// ─────────────────────────────────────────────────────────────
// Formatters
// ─────────────────────────────────────────────────────────────

function truncate(s: string | null | undefined, n: number): string {
  if (!s) return '';
  return s.length > n ? `${s.slice(0, n)}…` : s;
}

function formatPercent(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  return `${(v * 100).toFixed(1)}%`;
}

function formatSigned(v: number | null | undefined): string {
  if (v === null || v === undefined) return '—';
  const pct = v * 100;
  return `${pct >= 0 ? '+' : ''}${pct.toFixed(1)}pp`;
}

function formatUsd(v: number | null | undefined): string {
  if (v === null || v === undefined || Number.isNaN(v)) return '—';
  if (Math.abs(v) >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (Math.abs(v) >= 1_000) return `$${(v / 1_000).toFixed(1)}k`;
  return `$${v.toFixed(0)}`;
}

function formatRelativeTime(iso: string | null | undefined): string {
  if (!iso) return '—';
  const ts = new Date(iso).getTime();
  if (Number.isNaN(ts)) return iso;
  const diffSec = (Date.now() - ts) / 1000;
  if (diffSec < 60) return `${Math.floor(diffSec)}s ago`;
  if (diffSec < 3600) return `${Math.floor(diffSec / 60)}m ago`;
  if (diffSec < 86400) return `${Math.floor(diffSec / 3600)}h ago`;
  return `${Math.floor(diffSec / 86400)}d ago`;
}

function num(v: unknown): number | null {
  if (v === null || v === undefined) return null;
  const n = typeof v === 'string' ? parseFloat(v) : (v as number);
  return Number.isFinite(n) ? n : null;
}

function strOr(v: unknown, fallback: string): string {
  return v === null || v === undefined || v === '' ? fallback : String(v);
}

function priceOr(v: unknown): string {
  const n = num(v);
  return n === null ? '—' : `${(n * 100).toFixed(1)}%`;
}

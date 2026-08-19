import { useMemo, useState } from 'react';
import { ArrowLeft, History, Pause, Play, Rocket, Undo2 } from 'lucide-react';
import { useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { DetailAnatomy } from '../components/bot/detail/DetailAnatomy';
import { DetailFooter } from '../components/bot/detail/DetailFooter';
import { DetailHero } from '../components/bot/detail/DetailHero';
import { DetailPositions } from '../components/bot/detail/DetailPositions';
import { DetailRecord } from '../components/bot/detail/DetailRecord';
import { DetailTicker } from '../components/bot/detail/DetailTicker';
import { DetectorLifecycleCard } from '../components/bot/lifecycle/DetectorLifecycleCard';
import {
  useBotEquity,
  useBotHistory,
  useBotLiveMetrics,
  useBotPositions,
  useBotRecentFills,
  useBotStrategies,
  useStrategyPause,
  useStrategyResume,
  useToggleStrategy,
} from '../hooks/useBot';
import type { BotMode } from '../hooks/useBot';

export function StrategyDetail() {
  const { strategyKey } = useParams<{ strategyKey: string }>();
  const navigate = useNavigate();
  const [searchParams] = useSearchParams();
  const mode: BotMode = searchParams.get('mode') === 'live' ? 'live' : 'dry';
  const [showFills, setShowFills] = useState(true);

  const { data: strategiesData, isLoading } = useBotStrategies(mode);
  const { data: equityData } = useBotEquity(30, mode);
  const { data: history } = useBotHistory(14, strategyKey, mode);
  const { data: fillsData } = useBotRecentFills(20, mode);
  const { data: liveMetricsData } = useBotLiveMetrics();
  const { data: positionsData } = useBotPositions(strategyKey || '', 'open');

  const toggleStrategy = useToggleStrategy();
  const pauseStrategy = useStrategyPause();
  const resumeStrategy = useStrategyResume();

  const strategies = useMemo(
    () => strategiesData?.strategies ?? [],
    [strategiesData],
  );
  const strategy = strategies.find((s) => s.key === strategyKey);
  const caseNumber = useMemo(() => {
    if (!strategyKey) return 1;
    const idx = strategies.findIndex((s) => s.key === strategyKey);
    return idx >= 0 ? idx + 1 : 1;
  }, [strategies, strategyKey]);

  const liveMetric = (liveMetricsData?.strategies ?? []).find(
    (s) => s.strategy_key === strategyKey,
  );
  const strategyFills = useMemo(
    () => (fillsData?.fills ?? []).filter((f) => f.strategy_key === strategyKey),
    [fillsData, strategyKey],
  );

  if (isLoading) {
    return (
      <div
        className="editorial min-h-screen flex items-center justify-center"
        style={{ background: 'var(--ed-bg)' }}
      >
        <span
          className="ed-display"
          style={{ fontSize: 14, fontStyle: 'italic', color: 'var(--ed-ink-faint)' }}
        >
          loading the archive…
        </span>
      </div>
    );
  }

  if (!strategy) {
    return (
      <div
        className="editorial min-h-screen px-6 py-8"
        style={{ background: 'var(--ed-bg)' }}
      >
        {strategyKey && (
          <div style={{ maxWidth: 900, margin: '0 auto', marginBottom: 30 }}>
            <DetectorLifecycleCard detector={strategyKey} />
          </div>
        )}
        <div style={{ textAlign: 'center' }}>
          <p
            className="ed-display"
            style={{
              fontSize: 18,
              fontStyle: 'italic',
              color: 'var(--ed-ink-dim)',
              marginBottom: 10,
            }}
          >
            нет live execution данных для {strategyKey}
          </p>
          <p
            style={{
              fontSize: 12,
              color: 'var(--ed-ink-faint)',
              marginBottom: 16,
            }}
          >
            Detector в shadow stage — эмитит signals но execution_orders ещё нет.
          </p>
          <button
            onClick={() => navigate('/bot')}
            className="ed-mono"
            style={{
              fontSize: 11,
              color: 'var(--ed-accent)',
              textTransform: 'uppercase',
              letterSpacing: '0.14em',
              background: 'none',
              border: 'none',
              cursor: 'pointer',
            }}
          >
            ← back to desk
          </button>
        </div>
      </div>
    );
  }

  const isLive = mode === 'live';
  const paused = liveMetric?.paused ?? false;

  const daily = history?.daily ?? [];
  const equity = equityData?.points ?? [];

  return (
    <div
      className="editorial min-h-screen"
      style={{
        background: 'var(--ed-bg)',
        color: 'var(--ed-ink)',
        fontFamily: 'var(--ed-body)',
      }}
    >
      {/* Top bar — back + strategy key breadcrumb */}
      <div
        className="sticky top-0 z-10 px-5 py-3 flex items-center justify-between ed-grain"
        style={{
          background: 'rgba(10, 10, 11, 0.85)',
          backdropFilter: 'blur(12px)',
          borderBottom: '1px solid var(--ed-rule)',
        }}
      >
        <button
          onClick={() => navigate(`/bot?mode=${mode}`)}
          className="flex items-center gap-1.5"
          style={{
            background: 'none',
            border: 'none',
            cursor: 'pointer',
            color: 'var(--ed-ink-dim)',
            padding: 0,
          }}
        >
          <ArrowLeft className="w-3.5 h-3.5" />
          <span
            className="ed-mono"
            style={{ fontSize: 10, letterSpacing: '0.14em', textTransform: 'uppercase' }}
          >
            Back
          </span>
        </button>
        <span
          className="ed-mono"
          style={{
            fontSize: 10,
            color: 'var(--ed-ink-faint)',
            letterSpacing: '0.14em',
            textTransform: 'uppercase',
          }}
        >
          {isLive ? 'Live' : 'Paper'}
        </span>
      </div>

      <main className="ed-fade-up">
        {strategyKey && (
          <div style={{ padding: '0 20px', marginTop: 20 }}>
            <DetectorLifecycleCard detector={strategyKey} />
          </div>
        )}

        <DetailHero
          strategy={strategy}
          equity={equity}
          caseNumber={caseNumber}
          mode={mode}
        />

        <DetailAnatomy strategy={strategy} liveMetric={liveMetric} mode={mode} />

        {daily.length > 0 && (
          <DetailRecord daily={daily} fills={strategyFills} />
        )}

        <DetailPositions positions={positionsData?.positions ?? []} />

        {showFills && strategyFills.length > 0 && (
          <DetailTicker fills={strategyFills} />
        )}
        {strategyFills.length === 0 && (
          <section className="px-5 py-4">
            <p
              className="ed-display"
              style={{
                fontSize: 12,
                color: 'var(--ed-ink-faint)',
                fontStyle: 'italic',
                textAlign: 'center',
              }}
            >
              лента пуста · пока что
            </p>
          </section>
        )}

        {/* Actions row */}
        <section className="px-5 py-6 flex flex-col gap-2">
          <div className="flex gap-2">
            <button
              onClick={() => navigate(`/bot/${strategy.key}/history?mode=${mode}`)}
              className="flex-1 flex items-center justify-center gap-1.5 py-2.5"
              style={{
                background: 'var(--ed-accent-soft)',
                border: '1px solid rgba(201, 169, 97, 0.28)',
                color: 'var(--ed-accent)',
                fontFamily: 'var(--ed-mono)',
                fontSize: 10,
                letterSpacing: '0.14em',
                textTransform: 'uppercase',
                cursor: 'pointer',
              }}
            >
              <History className="w-3 h-3" />
              Full archive
            </button>
            <button
              onClick={() => setShowFills((v) => !v)}
              className="px-4 py-2.5"
              style={{
                background: 'transparent',
                border: '1px solid var(--ed-rule-strong)',
                color: 'var(--ed-ink-dim)',
                fontFamily: 'var(--ed-mono)',
                fontSize: 10,
                letterSpacing: '0.14em',
                textTransform: 'uppercase',
                cursor: 'pointer',
              }}
            >
              {showFills ? 'hide tape' : 'show tape'}
            </button>
          </div>

          {/* Enable / Pause controls */}
          {isLive && liveMetric ? (
            <button
              onClick={() =>
                paused
                  ? resumeStrategy.mutate(strategy.key)
                  : pauseStrategy.mutate(strategy.key)
              }
              disabled={pauseStrategy.isPending || resumeStrategy.isPending}
              className="flex items-center justify-center gap-1.5 py-3"
              style={{
                background: paused ? 'var(--ed-up-soft)' : 'var(--ed-down-soft)',
                border: `1px solid ${paused ? 'rgba(95,185,122,0.35)' : 'rgba(214,90,90,0.35)'}`,
                color: paused ? 'var(--ed-up)' : 'var(--ed-down)',
                fontFamily: 'var(--ed-mono)',
                fontSize: 11,
                letterSpacing: '0.14em',
                textTransform: 'uppercase',
                fontWeight: 500,
                cursor: 'pointer',
              }}
            >
              {paused ? <Play className="w-3.5 h-3.5" /> : <Pause className="w-3.5 h-3.5" />}
              {paused ? 'resume live' : 'pause live'}
            </button>
          ) : (
            <button
              onClick={() => {
                const verb = strategy.enabled ? 'demote to paper' : 'promote to live';
                const msg = strategy.enabled
                  ? `Demote "${strategy.key}" back to PAPER? Live positions останутся открытыми до settle/stale, но новых не будет.`
                  : `Promote "${strategy.key}" to LIVE?\n\nНастоящие деньги. AKS начнёт со stake $8 (warmup). Auto-pause если WR_30d<45% или drawdown>25%.`;
                if (window.confirm(`${verb.toUpperCase()}\n\n${msg}`)) {
                  toggleStrategy.mutate(strategy.key);
                }
              }}
              disabled={toggleStrategy.isPending}
              className="flex items-center justify-center gap-1.5 py-3"
              style={{
                background: strategy.enabled ? 'var(--ed-down-soft)' : 'var(--ed-accent-soft)',
                border: `1px solid ${strategy.enabled ? 'rgba(214,90,90,0.35)' : 'rgba(201, 169, 97, 0.45)'}`,
                color: strategy.enabled ? 'var(--ed-down)' : 'var(--ed-accent)',
                fontFamily: 'var(--ed-mono)',
                fontSize: 11,
                letterSpacing: '0.14em',
                textTransform: 'uppercase',
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              {strategy.enabled ? (
                <>
                  <Undo2 className="w-3.5 h-3.5" />
                  demote to paper
                </>
              ) : (
                <>
                  <Rocket className="w-3.5 h-3.5" />
                  promote to live
                </>
              )}
            </button>
          )}
        </section>

        <DetailFooter liveMetric={liveMetric} mode={mode} />
      </main>
    </div>
  );
}

import { useState, useEffect } from 'react';
import { useSearchParams } from 'react-router-dom';
import { AlertBanner } from '../components/bot/AlertBanner';
import { EditorialShell } from '../components/bot/editorial/EditorialShell';
import { StatusTerminal } from '../components/bot/status/StatusTerminal';
import { BacktestTerminal } from '../components/bot/lifecycle/BacktestTerminal';
import { VirtualTerminal } from '../components/bot/lifecycle/VirtualTerminal';
import { LiveTerminal } from '../components/bot/lifecycle/LiveTerminal';
import { useBotStatus } from '../hooks/useBot';
import type { BotTab } from '../hooks/useBot';

/**
 * BotDashboard — root /bot.
 *
 * Lifecycle architecture (2026-05-02): 4 tabs соответствуют 4 stages.
 *   🧪 backtest — canonical predicted (docs/backtests/*.md)
 *   👁  shadow   — production signals (StatusTerminal)
 *   💸 virtual  — paper execution (VirtualTerminal)
 *   🔴 live     — реальные ордера (LiveTerminal)
 *
 * URL sync: ?stage=backtest|shadow|virtual|live
 * Backwards compat: ?mode=dry → stage=virtual; ?mode=live → stage=live.
 *
 * См. docs/detector_lifecycle.md.
 */

const VALID_TABS: BotTab[] = ['backtest', 'shadow', 'virtual', 'live'];

function resolveTabFromQuery(searchParams: URLSearchParams): BotTab {
  const stage = searchParams.get('stage');
  if (stage && VALID_TABS.includes(stage as BotTab)) {
    return stage as BotTab;
  }
  // Legacy ?mode=dry|live → stage alias
  const mode = searchParams.get('mode');
  if (mode === 'live') return 'live';
  if (mode === 'dry') return 'virtual';
  return 'shadow'; // default — production signals view
}

export function BotDashboard() {
  const [searchParams, setSearchParams] = useSearchParams();
  const [tab, setTab] = useState<BotTab>(resolveTabFromQuery(searchParams));
  const { data: status } = useBotStatus();

  useEffect(() => {
    setTab(resolveTabFromQuery(searchParams));
  }, [searchParams]);

  const handleTabChange = (next: BotTab) => {
    setTab(next);
    const params = new URLSearchParams(searchParams);
    params.delete('mode'); // remove legacy
    params.set('stage', next);
    setSearchParams(params, { replace: true });
  };

  let content: React.ReactNode;
  switch (tab) {
    case 'backtest':
      content = <BacktestTerminal />;
      break;
    case 'shadow':
      content = <StatusTerminal />;
      break;
    case 'virtual':
      content = <VirtualTerminal />;
      break;
    case 'live':
      content = <LiveTerminal />;
      break;
  }

  return (
    <EditorialShell tab={tab} onTabChange={handleTabChange}>
      {status?.alerts && status.alerts.length > 0 && <AlertBanner alerts={status.alerts} />}
      {content}
    </EditorialShell>
  );
}

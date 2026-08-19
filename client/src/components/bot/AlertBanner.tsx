import { AlertTriangle, XCircle, Info } from 'lucide-react';
import type { BotAlert } from '../../hooks/useBot';

const ALERT_STYLES = {
  error: { bg: 'rgba(255,59,48,0.12)', border: 'rgba(255,59,48,0.3)', color: '#FF3B30', Icon: XCircle },
  warning: { bg: 'rgba(255,106,61,0.12)', border: 'rgba(255,106,61,0.3)', color: '#FF6A3D', Icon: AlertTriangle },
  info: { bg: 'rgba(64,224,255,0.12)', border: 'rgba(64,224,255,0.3)', color: '#40E0FF', Icon: Info },
} as const;

export function AlertBanner({ alerts }: { alerts: BotAlert[] }) {
  if (!alerts.length) return null;

  return (
    <div className="flex flex-col">
      {alerts.map((alert, i) => {
        const style = ALERT_STYLES[alert.level];
        return (
          <div
            key={i}
            className="px-4 py-2 flex items-center gap-2 text-xs"
            style={{ background: style.bg, borderBottom: `1px solid ${style.border}` }}
          >
            <style.Icon className="w-3.5 h-3.5 flex-shrink-0" style={{ color: style.color }} />
            <span style={{ color: style.color }}>{alert.message}</span>
          </div>
        );
      })}
    </div>
  );
}

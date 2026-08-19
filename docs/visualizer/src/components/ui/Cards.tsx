import { useState } from 'react';
import { motion } from 'framer-motion';
import { FileCode, Calendar, GitCommit, ChevronDown } from 'lucide-react';
import type { FileData, DomainStats, RiskZones, BlindSpots } from '../../hooks/useFeatureMap';

export function Card({ children, className = "" }: { children: React.ReactNode, className?: string }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 20 }}
      animate={{ opacity: 1, y: 0 }}
      className={`glass-panel rounded-2xl p-6 ${className}`}
    >
      {children}
    </motion.div>
  );
}

export function StatCard({ title, value, subtitle, icon: Icon }: {
  title: string;
  value: string | number;
  subtitle?: string;
  icon: React.ComponentType<{ size?: number }>;
}) {
  return (
    <Card className="flex items-center gap-4 hover:border-primary/50 transition-colors bg-surface-light border-border">
      <div className="p-4 rounded-xl bg-primary/10 text-primary">
        <Icon size={24} />
      </div>
      <div>
        <p className="text-sm text-gray-400 font-medium">{title}</p>
        <p className="text-2xl font-bold bg-clip-text text-transparent bg-gradient-to-r from-white to-gray-400">
          {value}
        </p>
        {subtitle && <p className="text-xs text-primary/80 mt-1">{subtitle}</p>}
      </div>
    </Card>
  );
}

// Sidebar navigation item
export function SidebarItem({
  icon: Icon,
  label,
  isActive,
  onClick
}: {
  icon: React.ComponentType<{ size?: number }>;
  label: string;
  isActive: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`w-full flex items-center gap-3 px-4 py-2.5 rounded-lg text-sm font-medium transition-all duration-200
        ${isActive
          ? 'bg-primary/15 text-primary'
          : 'text-gray-400 hover:bg-white/5 hover:text-gray-200'
        }`}
    >
      <Icon size={18} />
      {label}
    </button>
  );
}

// Compact file table rows
export function FileRow({
  file,
  onClick
}: {
  file: FileData;
  onClick: (file: FileData) => void;
}) {
  const filename = file.id.split('/').pop() || file.id;
  const directory = file.id.split('/').slice(0, -1).join('/');

  return (
    <div
      onClick={() => onClick(file)}
      className="group grid grid-cols-[2fr_1fr_1fr_80px] gap-4 items-center px-4 py-3 border-b border-border hover:bg-white/5 cursor-pointer transition-colors text-sm"
    >
      {/* Name and Path */}
      <div className="flex items-center gap-3 overflow-hidden">
        <FileCode size={16} className="text-gray-500 flex-shrink-0 group-hover:text-primary transition-colors" />
        <div className="truncate">
          <p className="font-medium text-gray-200 truncate">{filename}</p>
          <p className="text-xs text-gray-500 truncate">{directory}</p>
        </div>
      </div>

      {/* Last Modified */}
      <div className="flex items-center gap-2 text-gray-400">
        <Calendar size={14} className="text-gray-500" />
        <span>{file.updated ? new Date(file.updated).toLocaleDateString() : 'N/A'}</span>
      </div>

      {/* Commit Info */}
      <div className="flex items-center gap-2 text-gray-400 truncate">
        <GitCommit size={14} className="text-gray-500 flex-shrink-0" />
        <span className="truncate">{file.msg || 'No history'}</span>
      </div>

      {/* Dependencies Badge */}
      <div className="justify-self-end">
        <span className="px-2 py-1 rounded bg-white/5 text-gray-400 text-xs font-mono border border-border">
          {file.incoming_deps?.length || 0} callers
        </span>
      </div>
    </div>
  );
}

// --- Domain Card ---
export function DomainCard({ name, stats, maxCommits, onClick }: {
  name: string;
  stats: DomainStats;
  maxCommits: number;
  onClick: () => void;
}) {
  const progress = maxCommits > 0 ? (stats.total_commits / maxCommits) * 100 : 0;
  const statusConfig: Record<DomainStats['status'], { label: string; color: string }> = {
    active: { label: 'Active', color: 'text-success' },
    moderate: { label: 'Moderate', color: 'text-warning' },
    stable: { label: 'Stable', color: 'text-gray-500' },
  };
  const { label: statusLabel, color: statusColor } = statusConfig[stats.status] || statusConfig.stable;

  return (
    <motion.div
      whileHover={{ scale: 1.02 }}
      onClick={onClick}
      className="glass-panel rounded-xl p-5 cursor-pointer hover:border-primary/40 transition-colors"
    >
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-semibold text-white truncate">{name}</h3>
        <span className="text-xs font-mono px-2 py-0.5 rounded bg-primary/10 text-primary">
          v{stats.version}
        </span>
      </div>
      <p className="text-xs text-gray-400 mb-3">
        {stats.total_files} files &middot; {stats.total_commits} commits
      </p>
      {/* Progress bar */}
      <div className="w-full h-1.5 bg-white/5 rounded-full overflow-hidden mb-3">
        <div
          className="h-full bg-primary/60 rounded-full transition-all"
          style={{ width: `${Math.min(progress, 100)}%` }}
        />
      </div>
      <span className={`text-xs font-medium ${statusColor}`}>{statusLabel}</span>
    </motion.div>
  );
}

// --- Heatmap ---
function stripDomainName(name: string): string {
  // Remove emoji (Unicode) and parenthetical text like "(Scoring)"
  return name
    .replace(/[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{FE00}-\u{FEFF}]/gu, '')
    .replace(/\s*\(.*?\)\s*/g, '')
    .trim();
}

function getHeatColor(count: number): string {
  if (count === 0) return 'bg-white/5';
  if (count <= 2) return 'bg-success/20';
  if (count <= 5) return 'bg-success/40';
  if (count <= 10) return 'bg-success/60';
  return 'bg-success/80';
}

export function Heatmap({ domainStats }: { domainStats: Record<string, DomainStats> }) {
  const domains = Object.entries(domainStats);

  return (
    <div className="space-y-2">
      {/* Column headers */}
      <div className="grid gap-2" style={{ gridTemplateColumns: '120px repeat(4, 1fr)' }}>
        <div />
        {['4w', '3w', '2w', '1w'].map(label => (
          <div key={label} className="text-xs text-gray-500 text-center">{label}</div>
        ))}
      </div>
      {/* Rows */}
      {domains.map(([name, stats]) => {
        const weeks = stats.weekly_commits.slice(-4);
        // Pad to 4 if shorter
        while (weeks.length < 4) weeks.unshift(0);
        return (
          <div key={name} className="grid gap-2 items-center" style={{ gridTemplateColumns: '120px repeat(4, 1fr)' }}>
            <span className="text-xs text-gray-300 truncate" title={name}>
              {stripDomainName(name)}
            </span>
            {weeks.map((count, i) => (
              <div
                key={i}
                className={`h-6 rounded ${getHeatColor(count)} flex items-center justify-center`}
                title={`${count} commits`}
              >
                {count > 0 && <span className="text-[10px] text-white/70">{count}</span>}
              </div>
            ))}
          </div>
        );
      })}
    </div>
  );
}

// --- TrendChart ---
export function TrendChart({ domainStats }: { domainStats: Record<string, DomainStats> }) {
  // Aggregate weekly_commits across all domains (8 weeks)
  const allDomains = Object.values(domainStats);
  const weeklyTotals: number[] = [];
  for (let i = 0; i < 8; i++) {
    let total = 0;
    for (const stats of allDomains) {
      const weeks = stats.weekly_commits;
      const idx = weeks.length - 8 + i;
      if (idx >= 0 && idx < weeks.length) {
        total += weeks[idx];
      }
    }
    weeklyTotals.push(total);
  }

  const maxVal = Math.max(...weeklyTotals, 1);

  return (
    <div className="flex flex-col h-full">
      <div className="flex items-end gap-1.5 flex-1 min-h-[120px]">
        {weeklyTotals.map((val, i) => (
          <div key={i} className="flex-1 flex flex-col items-center justify-end h-full">
            <motion.div
              initial={{ height: 0 }}
              animate={{ height: `${(val / maxVal) * 100}%` }}
              transition={{ delay: i * 0.05 }}
              className="w-full bg-primary/50 hover:bg-primary/70 rounded-t transition-colors min-h-[2px]"
              title={`${val} commits`}
            />
          </div>
        ))}
      </div>
      <div className="flex justify-between mt-2">
        <span className="text-xs text-gray-500">8w ago</span>
        <span className="text-xs text-gray-500">Now</span>
      </div>
    </div>
  );
}

// --- RiskTable ---
export function RiskTable({ riskZones }: { riskZones: RiskZones }) {
  const hasChurn = riskZones.high_churn_coupled.length > 0;
  const hasDesync = riskZones.desync_pairs.length > 0;

  if (!hasChurn && !hasDesync) {
    return (
      <div className="flex items-center justify-center py-8 text-gray-500 text-sm">
        No risk zones detected
      </div>
    );
  }

  return (
    <div className="space-y-4">
      {hasChurn && (
        <div className="space-y-2">
          <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">High Churn + Coupled</h4>
          {riskZones.high_churn_coupled.slice(0, 5).map((item, i) => (
            <div key={i} className="flex items-center gap-3 py-1.5 text-sm">
              <span className="w-2 h-2 rounded-full bg-danger flex-shrink-0" />
              <span className="text-gray-200 truncate font-mono text-xs">{item.file.split('/').pop()}</span>
              <span className="text-gray-500 text-xs ml-auto whitespace-nowrap">
                {item.callers} callers &middot; {item.days_ago}d ago
              </span>
            </div>
          ))}
        </div>
      )}
      {hasDesync && (
        <div className="space-y-2">
          <h4 className="text-xs font-semibold text-gray-500 uppercase tracking-wider">Desync Pairs</h4>
          {riskZones.desync_pairs.slice(0, 5).map((pair, i) => {
            const dotColor = pair.risk === 'high' ? 'bg-danger' : 'bg-warning';
            const fileNames = pair.files.map(f => f.split('/').pop()).join(' \u2194 ');
            return (
              <div key={i} className="flex items-center gap-3 py-1.5 text-sm">
                <span className={`w-2 h-2 rounded-full ${dotColor} flex-shrink-0`} />
                <span className="text-gray-200 truncate font-mono text-xs">{fileNames}</span>
                <span className="text-gray-500 text-xs ml-auto whitespace-nowrap">
                  {pair.days_apart}d apart
                </span>
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}

// --- BlindSpotList ---
function BlindSpotSection({ title, items, colorClass }: {
  title: string;
  items: string[];
  colorClass: string;
}) {
  const [open, setOpen] = useState(false);
  const maxShow = 8;
  const shown = items.slice(0, maxShow);
  const remaining = items.length - maxShow;

  return (
    <div className="border-b border-border last:border-b-0">
      <button
        onClick={() => setOpen(!open)}
        className="w-full flex items-center justify-between py-3 px-1 text-sm hover:bg-white/5 transition-colors rounded"
      >
        <div className="flex items-center gap-2">
          <ChevronDown size={14} className={`text-gray-500 transition-transform ${open ? 'rotate-0' : '-rotate-90'}`} />
          <span className={`font-medium ${colorClass}`}>{title}</span>
        </div>
        <span className="text-xs px-2 py-0.5 rounded bg-white/5 text-gray-400 border border-border font-mono">
          {items.length}
        </span>
      </button>
      {open && (
        <div className="pb-3 pl-6 space-y-1">
          {shown.map((item, i) => (
            <p key={i} className="text-xs text-gray-400 font-mono truncate">{item.split('/').pop()}</p>
          ))}
          {remaining > 0 && (
            <p className="text-xs text-gray-500 italic">+{remaining} more</p>
          )}
        </div>
      )}
    </div>
  );
}

export function BlindSpotList({ blindSpots }: { blindSpots: BlindSpots }) {
  return (
    <div className="divide-y-0">
      <BlindSpotSection title="No Docstring" items={blindSpots.no_docstring} colorClass="text-warning" />
      <BlindSpotSection title="Dead Code?" items={blindSpots.zero_incoming} colorClass="text-gray-400" />
      <BlindSpotSection title="No Tests" items={blindSpots.no_tests} colorClass="text-danger" />
    </div>
  );
}

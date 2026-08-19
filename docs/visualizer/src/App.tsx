import { useState } from 'react';
import {
  Network,
  FolderTree,
  AlertTriangle,
  LayoutTemplate,
  ChevronRight,
  Database,
  ShieldAlert,
  Wallet,
  Globe,
  Briefcase,
  Layers,
  Search,
  FileCode,
  Server
} from 'lucide-react';
import { useFeatureMap } from './hooks/useFeatureMap';
import type { FileData, FeatureMapData } from './hooks/useFeatureMap';
import { Card, StatCard, SidebarItem, FileRow, DomainCard, Heatmap, TrendChart, RiskTable, BlindSpotList } from './components/ui/Cards';
import { motion, AnimatePresence } from 'framer-motion';

// --- MODAL COMPONENT ---
function FileDetailsModal({ file, onClose, data, onNavigate }: {
  file: FileData,
  onClose: () => void,
  data: FeatureMapData,
  onNavigate: (fileId: string) => void
}) {
  const filename = file.id.split('/').pop() || file.id;
  const directory = file.id.split('/').slice(0, -1).join('/');

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <motion.div
        initial={{ opacity: 0, scale: 0.95 }}
        animate={{ opacity: 1, scale: 1 }}
        exit={{ opacity: 0, scale: 0.95 }}
        className="glass-panel w-full max-w-6xl max-h-[90vh] rounded-2xl border border-border shadow-2xl flex flex-col bg-surface overflow-hidden"
      >
        {/* Header */}
        <div className="flex items-center justify-between p-6 border-b border-border bg-surface-light">
          <div>
            <h2 className="text-2xl font-bold text-white flex items-center gap-3">
              <span className="text-primary">{filename}</span>
            </h2>
            <p className="text-gray-400 text-sm font-mono mt-1">{directory}</p>
          </div>
          <button onClick={onClose} className="p-2 hover:bg-white/10 rounded-lg text-gray-400 hover:text-white transition-colors">
            ✕
          </button>
        </div>

        <div className="flex-1 overflow-y-auto custom-scrollbar p-6">
          {/* Docstring area */}
          {file.docstring && (
            <div className="mb-8 p-4 bg-white/5 border border-border rounded-xl">
              <h3 className="text-sm font-semibold text-gray-300 mb-2 uppercase tracking-wider">File Documentation</h3>
              <pre className="text-sm text-gray-400 whitespace-pre-wrap font-sans leading-relaxed">{file.docstring}</pre>
            </div>
          )}

          {/* Butterfly View */}
          <div className="grid grid-cols-2 gap-8">
            {/* Left: Depends On (Outgoing) */}
            <div className="space-y-4">
              <h3 className="text-lg font-semibold text-gray-300 flex items-center gap-2">
                <span className="text-warning">←</span> Depends On (Imports)
                <span className="text-sm font-normal text-gray-500 bg-black/20 px-2 py-0.5 rounded-full">
                  {file.outgoing_deps?.length || 0}
                </span>
              </h3>
              <div className="space-y-2">
                {file.outgoing_deps && file.outgoing_deps.length > 0 ? (
                  file.outgoing_deps.map(dep => {
                    const depFile = data.file_data[dep];
                    if (!depFile) return null;
                    return (
                      <div key={dep} className="glass-panel rounded-lg p-3 hover:border-primary/50 cursor-pointer transition-colors" onClick={() => onNavigate(dep)}>
                        <p className="text-sm font-medium text-gray-200">{dep.split('/').pop()}</p>
                        <p className="text-xs text-gray-500 truncate">{dep}</p>
                      </div>
                    );
                  })
                ) : (
                  <p className="text-sm text-gray-500 italic p-4 text-center glass-panel rounded-lg">No internal dependencies found</p>
                )}
              </div>
            </div>

            {/* Right: Depended By (Incoming) */}
            <div className="space-y-4">
              <h3 className="text-lg font-semibold text-gray-300 flex items-center justify-end gap-2">
                <span className="text-sm font-normal text-gray-500 bg-black/20 px-2 py-0.5 rounded-full">
                  {file.incoming_deps?.length || 0}
                </span>
                Is Used By (Callers) <span className="text-success">→</span>
              </h3>
              <div className="space-y-2">
                {file.incoming_deps && file.incoming_deps.length > 0 ? (
                  file.incoming_deps.map(dep => {
                    const depFile = data.file_data[dep];
                    if (!depFile) return null;
                    return (
                      <div key={dep} className="glass-panel rounded-lg p-3 hover:border-primary/50 cursor-pointer transition-colors text-right" onClick={() => onNavigate(dep)}>
                        <p className="text-sm font-medium text-gray-200">{dep.split('/').pop()}</p>
                        <p className="text-xs text-gray-500 truncate">{dep}</p>
                      </div>
                    );
                  })
                ) : (
                  <p className="text-sm text-gray-500 italic p-4 text-center glass-panel rounded-lg">No callers found in analysis</p>
                )}
              </div>
            </div>
          </div>
        </div>
      </motion.div>
    </div>
  );
}

// --- Helper: method badge color ---
function getMethodColor(method: string): string {
  switch (method.toUpperCase()) {
    case 'GET': return 'bg-success/20 text-success';
    case 'POST': return 'bg-primary/20 text-primary';
    case 'PUT':
    case 'PATCH': return 'bg-warning/20 text-warning';
    case 'DELETE': return 'bg-danger/20 text-danger';
    default: return 'bg-white/10 text-gray-400';
  }
}

// --- Helper: check if updated within N days ---
function isRecentlyUpdated(updated: string | null, days: number): boolean {
  if (!updated) return false;
  const diff = Date.now() - new Date(updated).getTime();
  return diff < days * 24 * 60 * 60 * 1000;
}

// --- MAIN APP ---
export default function App() {
  const { data, loading, error } = useFeatureMap();
  const [selectedFile, setSelectedFile] = useState<FileData | null>(null);
  const [activeSection, setActiveSection] = useState<{ type: 'overview' | 'domain' | 'screen' | 'critical' | 'api', id: string }>({ type: 'overview', id: 'overview' });

  if (loading) return (
    <div className="min-h-screen bg-background flex items-center justify-center">
      <div className="w-16 h-16 border-4 border-primary/20 border-t-primary rounded-full animate-spin"></div>
    </div>
  );
  if (error || !data) return (
    <div className="min-h-screen bg-background flex items-center justify-center p-4 text-center text-danger">
      Failed to load feature map data. Check generator script.
    </div>
  );

  // Icon mapping helpers
  const getDomainIcon = (keyword: string) => {
    if (keyword.includes('score') || keyword.includes('oracle')) return <Database />;
    if (keyword.includes('whale')) return <Wallet />;
    if (keyword.includes('trade') || keyword.includes('sim')) return <Briefcase />;
    return <Layers />;
  };

  const getScreenIcon = (keyword: string) => {
    if (keyword.includes('home') || keyword.includes('dashboard')) return <LayoutTemplate />;
    if (keyword.includes('admin')) return <ShieldAlert />;
    if (keyword.includes('market')) return <Globe />;
    return <LayoutTemplate />;
  };

  // Compute max commits for DomainCard progress
  const domainEntries = Object.entries(data.domain_stats);
  const maxDomainCommits = domainEntries.reduce((max, [, s]) => Math.max(max, s.total_commits), 1);

  // Platform version = max domain version
  const platformVersion = domainEntries.reduce((max, [, s]) => Math.max(max, s.version), 0);

  // Content for the active Main Area
  const renderMainContent = () => {
    if (activeSection.type === 'overview') {
      return (
        <div className="p-8 space-y-8 animate-fadeIn overflow-y-auto h-full custom-scrollbar">
          <header>
            <h1 className="text-4xl font-bold tracking-tight text-white mb-2">System Overview</h1>
            <p className="text-gray-400 text-lg">High-level telemetry of the OraclePoly platform.</p>
          </header>

          {/* Row 1: Stats */}
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
            <StatCard title="Total Files" value={data.stats.total_files} icon={Network} />
            <StatCard title="Platform Version" value={`v${platformVersion}`} icon={FolderTree} />
            <StatCard title="High Coupling" value={data.critical_files.length} subtitle="Files with many callers" icon={AlertTriangle} />
            <StatCard title="Total Lines" value={data.stats.total_lines.toLocaleString()} icon={FileCode} />
          </div>

          {/* Row 2: Domain Cards */}
          {domainEntries.length > 0 && (
            <div>
              <h2 className="text-xl font-semibold text-white mb-4">Business Domains</h2>
              <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-3 gap-4">
                {domainEntries.map(([name, stats]) => (
                  <DomainCard
                    key={name}
                    name={name}
                    stats={stats}
                    maxCommits={maxDomainCommits}
                    onClick={() => setActiveSection({ type: 'domain', id: name })}
                  />
                ))}
              </div>
            </div>
          )}

          {/* Row 3: Charts */}
          {domainEntries.length > 0 && (
            <div className="grid grid-cols-1 xl:grid-cols-2 gap-8">
              <Card className="bg-surface-light border-border">
                <h2 className="text-lg font-semibold text-white mb-4">Activity Heatmap</h2>
                <Heatmap domainStats={data.domain_stats} />
              </Card>
              <Card className="bg-surface-light border-border">
                <h2 className="text-lg font-semibold text-white mb-4">Weekly Trend</h2>
                <TrendChart domainStats={data.domain_stats} />
              </Card>
            </div>
          )}

          {/* Row 4: Risk + Blind Spots */}
          <div className="grid grid-cols-1 xl:grid-cols-2 gap-8">
            <Card className="bg-surface-light border-border">
              <h2 className="text-lg font-semibold text-white mb-4 flex items-center gap-2">
                <AlertTriangle size={18} className="text-danger" /> Risk Zones
              </h2>
              <RiskTable riskZones={data.risk_zones} />
            </Card>
            <Card className="bg-surface-light border-border">
              <h2 className="text-lg font-semibold text-white mb-4">Blind Spots</h2>
              <BlindSpotList blindSpots={data.blind_spots} />
            </Card>
          </div>
        </div>
      );
    }

    if (activeSection.type === 'api') {
      const endpoints = data.api_endpoints;
      return (
        <div className="p-8 space-y-6 animate-fadeIn h-full flex flex-col">
          <header>
            <h1 className="text-3xl font-bold tracking-tight text-white mb-2 flex items-center gap-3">
              <Server className="text-primary" size={32} /> API Impact Map
            </h1>
            <p className="text-gray-400">Full endpoint registry with handler chains and frontend bindings.</p>
          </header>

          <Card className="flex-1 overflow-hidden flex flex-col bg-surface-light border-border p-0">
            <div className="grid grid-cols-[70px_1.5fr_1fr_1.5fr_1fr_60px] px-6 py-3 text-xs font-semibold uppercase tracking-wider text-gray-500 bg-black/20 border-b border-border">
              <div>Method</div>
              <div>Endpoint</div>
              <div>Router</div>
              <div>Handler Chain</div>
              <div>Frontend Hook</div>
              <div className="text-right">Ver</div>
            </div>
            <div className="flex-1 overflow-y-auto custom-scrollbar">
              {endpoints.length === 0 ? (
                <div className="p-12 text-center text-gray-500">
                  <Server size={48} className="mx-auto mb-4 opacity-20" />
                  <p>No API endpoints found in data.</p>
                </div>
              ) : (
                endpoints.map((ep, i) => {
                  const recent = isRecentlyUpdated(ep.updated, 3);
                  return (
                    <div
                      key={`${ep.method}-${ep.path}-${i}`}
                      onClick={() => {
                        const routerFile = data.file_data[ep.router_file];
                        if (routerFile) setSelectedFile(routerFile);
                      }}
                      className={`grid grid-cols-[70px_1.5fr_1fr_1.5fr_1fr_60px] items-center px-6 py-3 border-b border-border hover:bg-white/5 cursor-pointer transition-colors text-sm ${recent ? 'bg-primary/5' : ''}`}
                    >
                      <div>
                        <span className={`text-xs font-bold px-2 py-0.5 rounded ${getMethodColor(ep.method)}`}>
                          {ep.method}
                        </span>
                      </div>
                      <div className="text-gray-200 font-mono text-xs truncate" title={ep.path}>{ep.path}</div>
                      <div className="text-gray-400 text-xs truncate" title={ep.router_file}>{ep.router_file.split('/').pop()}</div>
                      <div className="text-gray-400 text-xs truncate" title={ep.handler_chain.join(' → ')}>
                        {ep.handler_chain.join(' \u2192 ')}
                      </div>
                      <div className={`text-xs truncate ${ep.frontend_hook ? 'text-primary' : 'text-gray-500'}`}>
                        {ep.frontend_hook || '\u2014'}
                      </div>
                      <div className="text-right text-xs text-gray-500 font-mono">{ep.version}</div>
                    </div>
                  );
                })
              )}
            </div>
          </Card>
        </div>
      );
    }

    if (activeSection.type === 'critical') {
      return (
        <div className="p-8 space-y-6 animate-fadeIn h-full flex flex-col">
          <header>
            <h1 className="text-3xl font-bold tracking-tight text-white mb-2 flex items-center gap-3">
              <AlertTriangle className="text-danger" size={32} /> High Criticality Hub
            </h1>
            <p className="text-gray-400">Files with the highest number of dependent modules. Changes here have high blast radius.</p>
          </header>

          <Card className="flex-1 overflow-hidden flex flex-col bg-surface-light border-border p-0">
            <div className="grid grid-cols-[2fr_1fr_1fr_80px] px-8 py-4 text-xs font-semibold uppercase tracking-wider text-gray-500 bg-black/20 border-b border-border">
              <div>File Path</div>
              <div>Last Modified</div>
              <div>Latest Commit</div>
              <div className="text-right">Callers</div>
            </div>
            <div className="flex-1 overflow-y-auto custom-scrollbar">
              <div className="px-4">
                {data.critical_files.map(({ file }) => {
                  const fileData = data.file_data[file];
                  if (!fileData) return null;
                  return (
                    <FileRow
                      key={file}
                      file={fileData}
                      onClick={setSelectedFile}
                    />
                  );
                })}
              </div>
            </div>
          </Card>
        </div>
      );
    }

    // Domain or Screen View
    const isDomain = activeSection.type === 'domain';
    const sectionData = isDomain ? data.domains[activeSection.id] : data.screens[activeSection.id];
    const title = activeSection.id;

    if (!sectionData) return null;

    // Filter files for this section
    const associatedFiles = Object.values(data.file_data).filter(f => {
      const isPathMatch = f.id.toLowerCase().includes(activeSection.id.toLowerCase());
      const isKeywordMatch = sectionData.keywords?.some(k => f.id.toLowerCase().includes(k.toLowerCase()));
      return isPathMatch || isKeywordMatch;
    });

    return (
      <div className="p-8 space-y-6 animate-fadeIn h-full flex flex-col">
        {/* Header & Descriptions */}
        <div className="space-y-4">
          <div className="flex items-center gap-2 text-sm text-primary mb-2">
            <span>{isDomain ? 'Business Domains' : 'UI Screens'}</span>
            <ChevronRight size={14} />
            <span className="text-white font-medium">{title}</span>
          </div>

          <div className="grid grid-cols-1 lg:grid-cols-3 gap-6">
            <div className="lg:col-span-1">
              <h1 className="text-3xl font-bold tracking-tight text-white mb-4">{title}</h1>
              {sectionData.description_simple && (
                <div className="p-4 bg-primary/10 border border-primary/20 rounded-xl mb-4">
                  <p className="text-primary font-medium">{sectionData.description_simple}</p>
                </div>
              )}
            </div>

            {sectionData.description_complex && (
              <div className="lg:col-span-2 glass-panel p-6 rounded-xl border-border bg-surface-light">
                <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-2">Architecture Context</h3>
                <p className="text-gray-300 text-sm leading-relaxed">{sectionData.description_complex}</p>
              </div>
            )}
          </div>
        </div>

        {/* File List */}
        <Card className="flex-1 flex flex-col overflow-hidden bg-surface-light border-border p-0 mt-4">
          <div className="px-6 py-4 border-b border-border bg-black/10 flex items-center justify-between">
            <h2 className="text-lg font-semibold text-white">Associated Files ({associatedFiles.length})</h2>
            <div className="relative">
              <Search className="absolute left-3 top-1/2 -translate-y-1/2 text-gray-500" size={16} />
              <input
                type="text"
                placeholder="Filter files..."
                className="pl-9 pr-4 py-1.5 bg-black/20 border border-border rounded-lg text-sm text-white focus:outline-none focus:border-primary w-64"
                disabled // Add state logic if search functionality is needed
              />
            </div>
          </div>

          <div className="flex-1 overflow-y-auto custom-scrollbar">
            <div className="px-2">
              {associatedFiles.length > 0 ? (
                associatedFiles.map(file => (
                  <FileRow
                    key={file.id}
                    file={file}
                    onClick={setSelectedFile}
                  />
                ))
              ) : (
                <div className="p-12 text-center text-gray-500">
                  <FileCode size={48} className="mx-auto mb-4 opacity-20" />
                  <p>No explicit files found strictly matching these keywords.</p>
                </div>
              )}
            </div>
          </div>
        </Card>
      </div>
    );
  };

  return (
    <div className="h-screen w-full flex bg-background text-gray-200 overflow-hidden font-sans">

      {/* SIDEBAR */}
      <div className="w-72 flex-shrink-0 bg-surface border-r border-border flex flex-col h-full z-10 shadow-xl">
        <div className="p-6 border-b border-border/50">
          <div className="flex items-center gap-3 text-primary">
            <div className="p-2 bg-primary/20 rounded-lg">
              <Network size={24} />
            </div>
            <h1 className="text-xl font-bold text-white tracking-tight">OraclePoly <span className="text-primary font-normal">Map</span></h1>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto custom-scrollbar p-4 space-y-6">
          <div className="space-y-1">
            <SidebarItem
              icon={LayoutTemplate}
              label="Overview"
              isActive={activeSection.type === 'overview'}
              onClick={() => setActiveSection({ type: 'overview', id: 'overview' })}
            />
          </div>

          <div>
            <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3 px-4">Business Domains</h3>
            <div className="space-y-1">
              {Object.keys(data.domains).map(domain => (
                <SidebarItem
                  key={domain}
                  icon={() => getDomainIcon(data.domains[domain].keywords?.[0] || '')}
                  label={domain}
                  isActive={activeSection.type === 'domain' && activeSection.id === domain}
                  onClick={() => setActiveSection({ type: 'domain', id: domain })}
                />
              ))}
            </div>
          </div>

          <div>
            <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3 px-4">UI Screens</h3>
            <div className="space-y-1">
              {data.screens && Object.keys(data.screens).map(screen => (
                <SidebarItem
                  key={screen}
                  icon={() => getScreenIcon(data.screens[screen].keywords?.[0] || '')}
                  label={screen}
                  isActive={activeSection.type === 'screen' && activeSection.id === screen}
                  onClick={() => setActiveSection({ type: 'screen', id: screen })}
                />
              ))}
            </div>
          </div>

          <div>
            <h3 className="text-xs font-semibold text-gray-500 uppercase tracking-wider mb-3 px-4">Analysis</h3>
            <div className="space-y-1">
              <SidebarItem
                icon={AlertTriangle}
                label="Critical Hotspots"
                isActive={activeSection.type === 'critical'}
                onClick={() => setActiveSection({ type: 'critical', id: 'critical' })}
              />
              <SidebarItem
                icon={Server}
                label="API Impact Map"
                isActive={activeSection.type === 'api'}
                onClick={() => setActiveSection({ type: 'api', id: 'api' })}
              />
            </div>
          </div>
        </div>

        {/* Footer info */}
        <div className="p-4 border-t border-border/50 text-xs text-gray-500 flex justify-between">
          <span>v2.0 Visualizer</span>
          <span>{data.stats.total_files} files mapped</span>
        </div>
      </div>

      {/* MAIN AREA */}
      <div className="flex-1 h-full overflow-hidden relative bg-gradient-to-br from-background to-surface">
        {renderMainContent()}
      </div>

      {/* MODALS */}
      <AnimatePresence>
        {selectedFile && (
          <FileDetailsModal
            file={selectedFile}
            data={data}
            onClose={() => setSelectedFile(null)}
            onNavigate={(id) => {
              const nextFile = data.file_data[id];
              if (nextFile) setSelectedFile(nextFile);
            }}
          />
        )}
      </AnimatePresence>
    </div>
  );
}

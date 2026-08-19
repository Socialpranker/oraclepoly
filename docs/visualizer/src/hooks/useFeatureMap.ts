import { useState, useEffect } from 'react'

export interface FileData {
  id: string;
  created: string | null;
  updated: string | null;
  msg: string;
  docstring?: string;
  incoming_deps: string[];
  outgoing_deps: string[];
  commit_count: number;
  version: number;
  version_display: string;
  lines: number;
}

export interface DomainInfo {
  keywords: string[];
  description_simple: string;
  description_complex: string;
}

export interface DomainStats {
  version: number;
  total_commits: number;
  total_files: number;
  last_activity_days: number;
  status: 'active' | 'moderate' | 'stable';
  weekly_commits: number[];
}

export interface ApiEndpoint {
  method: string;
  path: string;
  router_file: string;
  handler_chain: string[];
  frontend_hook: string | null;
  version: string;
  updated: string | null;
}

export interface RiskZones {
  high_churn_coupled: Array<{ file: string; callers: number; days_ago: number }>;
  desync_pairs: Array<{ files: string[]; days_apart: number; risk: 'high' | 'warning' }>;
}

export interface BlindSpots {
  no_docstring: string[];
  zero_incoming: string[];
  no_tests: string[];
}

export interface FeatureMapData {
  file_data: Record<string, FileData>;
  incoming_deps: Record<string, string[]>;
  outgoing_deps: Record<string, string[]>;
  domains: Record<string, DomainInfo>;
  screens: Record<string, DomainInfo>;
  stats: {
    total_files: number;
    total_lines: number;
  };
  critical_files: Array<{ file: string; deps: number }>;
  domain_stats: Record<string, DomainStats>;
  api_endpoints: ApiEndpoint[];
  risk_zones: RiskZones;
  blind_spots: BlindSpots;
}

export function useFeatureMap() {
  const [data, setData] = useState<FeatureMapData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    fetch('/feature_map_data.json')
      .then(res => {
        if (!res.ok) throw new Error('Failed to load map data');
        return res.json();
      })
      .then((raw: Record<string, unknown>) => {
        const fileData = (raw.file_data || {}) as Record<string, FileData>;
        const incomingDeps = (raw.incoming_deps || {}) as Record<string, string[]>;

        // Compute total lines from file_data entries
        const totalLines = Object.values(fileData).reduce(
          (sum, f) => sum + (f.lines || 0),
          0
        );

        // Post-process the raw JSON to match our React components' expectations
        const processedData: FeatureMapData = {
          ...raw as Omit<FeatureMapData, 'stats' | 'critical_files' | 'domain_stats' | 'api_endpoints' | 'risk_zones' | 'blind_spots'>,
          stats: {
            total_files: Object.keys(fileData).length,
            total_lines: totalLines,
          },
          critical_files: Object.entries(incomingDeps)
            .map(([file, deps]) => ({ file, deps: deps.length }))
            .sort((a, b) => b.deps - a.deps),
          domain_stats: (raw.domain_stats || {}) as Record<string, DomainStats>,
          api_endpoints: (raw.api_endpoints || []) as ApiEndpoint[],
          risk_zones: (raw.risk_zones || { high_churn_coupled: [], desync_pairs: [] }) as RiskZones,
          blind_spots: (raw.blind_spots || { no_docstring: [], zero_incoming: [], no_tests: [] }) as BlindSpots,
        };

        // Attach id, incoming_deps, and outgoing_deps directly to file_data objects
        Object.keys(processedData.file_data).forEach(id => {
          processedData.file_data[id] = {
            ...processedData.file_data[id],
            id,
            incoming_deps: processedData.incoming_deps[id] || [],
            outgoing_deps: processedData.outgoing_deps[id] || [],
            commit_count: processedData.file_data[id].commit_count || 0,
            version: processedData.file_data[id].version || 0,
            version_display: processedData.file_data[id].version_display || '',
            lines: processedData.file_data[id].lines || 0,
          };
        });

        setData(processedData);
      })
      .catch(err => setError(err.message))
      .finally(() => setLoading(false));
  }, []);

  return { data, loading, error };
}

import type { QueryKey } from './queryCacheCore';

export const MC_QUERY_PREFIX = ['mc'] as const;
export const MC_SERVERS_QUERY_PREFIX = [...MC_QUERY_PREFIX, 'servers'] as const;
export const MC_SETTINGS_QUERY_PREFIX = [...MC_QUERY_PREFIX, 'settings'] as const;
export const MC_TRENDS_QUERY_PREFIX = [...MC_QUERY_PREFIX, 'trends'] as const;

export const MEMORY_QUERY_PREFIX = ['memory'] as const;
export const MEMORY_OVERVIEW_QUERY_PREFIX = [...MEMORY_QUERY_PREFIX, 'overview'] as const;
export const MEMORY_LIST_QUERY_PREFIX = [...MEMORY_QUERY_PREFIX, 'list'] as const;
export const MEMORY_GRAPH_QUERY_PREFIX = [...MEMORY_QUERY_PREFIX, 'graph'] as const;

export const queryKeyPrefixes = Object.freeze({
  mc: MC_QUERY_PREFIX,
  mcServers: MC_SERVERS_QUERY_PREFIX,
  mcSettings: MC_SETTINGS_QUERY_PREFIX,
  mcTrends: MC_TRENDS_QUERY_PREFIX,
  memory: MEMORY_QUERY_PREFIX,
  memoryOverview: MEMORY_OVERVIEW_QUERY_PREFIX,
  memoryList: MEMORY_LIST_QUERY_PREFIX,
  memoryGraph: MEMORY_GRAPH_QUERY_PREFIX,
});

export interface MemoryListQueryParams {
  [key: string]: string | number | boolean | undefined;
  page: number;
  page_size: number;
  keyword?: string;
  session_id?: string;
  status: string;
  type: string;
  sort: string;
}

export function mcServers(groupId: string): QueryKey {
  return [...MC_SERVERS_QUERY_PREFIX, groupId] as const;
}

export function mcSettings(groupId: string): QueryKey {
  return [...MC_SETTINGS_QUERY_PREFIX, groupId] as const;
}

export function mcTrends(groupId: string, server: string | undefined, hours: number): QueryKey {
  return [...MC_TRENDS_QUERY_PREFIX, groupId, server ?? null, hours] as const;
}

export const memoryOverviewStats = [...MEMORY_OVERVIEW_QUERY_PREFIX, 'stats'] as const;
export const memoryOverviewBackups = [...MEMORY_OVERVIEW_QUERY_PREFIX, 'backups'] as const;

export function memoryList(filters: MemoryListQueryParams): QueryKey {
  return [
    ...MEMORY_LIST_QUERY_PREFIX,
    {
      page: filters.page,
      page_size: filters.page_size,
      keyword: filters.keyword ?? null,
      session_id: filters.session_id ?? null,
      status: filters.status,
      type: filters.type,
      sort: filters.sort,
    },
  ] as const;
}

export function memoryGraphOverview(
  session: string | null | undefined,
  persona: string | null | undefined,
): QueryKey {
  return [...MEMORY_GRAPH_QUERY_PREFIX, 'overview', session ?? null, persona ?? null] as const;
}

export const queryKeys = Object.freeze({
  mcServers,
  mcSettings,
  mcTrends,
  memoryOverviewStats,
  memoryOverviewBackups,
  memoryList,
  memoryGraphOverview,
});

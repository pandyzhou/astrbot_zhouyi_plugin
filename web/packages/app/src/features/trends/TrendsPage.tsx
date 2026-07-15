import { useEffect, type FormEvent } from 'react';
import { DataState, SelectField, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { apiClient } from '../../api/client';
import type { ServerRecord, ServersData, SettingsData, TrendsData } from '../../api/types';
import { formatNumber } from '../../format';
import { queryCache } from '../../store/queryCacheCore';
import { queryKeys } from '../../store/queryKeys';
import { useCachedQuery } from '../../store/useCachedQuery';
import { useWorkshopStore, type TrendFiltersState } from '../../store/workshopStore';
import { TrendChart } from './TrendChart';

function validateGroup<T extends { group_id: string }>(value: T, groupId: string, label: string): T {
  if (value.group_id !== groupId) throw new Error(`${label}响应不属于当前群组，请重试。`);
  return value;
}

function preserveTemporaryStatus(saved: ServerRecord, current: ServerRecord | undefined): ServerRecord {
  if (!current || saved.status !== 'unknown' || saved.host !== current.host) return saved;
  return {
    ...saved,
    status: current.status,
    version: current.version,
    latency: current.latency,
    players: current.players,
    icon: current.icon,
    queried_at: current.queried_at,
  };
}

function mergeSavedSnapshot(current: ServersData | undefined, incoming: ServersData): ServersData {
  if (!current || current.group_id !== incoming.group_id) return incoming;
  const currentById = new Map(current.servers.map((server) => [server.id, server]));
  return {
    ...incoming,
    last_manual_refresh_time: current.last_manual_refresh_time ?? incoming.last_manual_refresh_time,
    servers: incoming.servers.map((server) => preserveTemporaryStatus(server, currentById.get(server.id))),
  };
}

function errorMessage(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message || fallback : reason ? fallback : '';
}

export function TrendsPage() {
  const groupId = useWorkshopStore((state) => state.selectedGroupId);
  const serversKey = queryKeys.mcServers(groupId);
  const settingsKey = queryKeys.mcSettings(groupId);

  const serversQuery = useCachedQuery<ServersData>(serversKey, async () => {
    const incoming = validateGroup(await apiClient.servers(groupId), groupId, '服务器列表');
    return mergeSavedSnapshot(queryCache.peek<ServersData>(serversKey)?.data, incoming);
  });
  const settingsQuery = useCachedQuery<SettingsData>(settingsKey, async () => (
    validateGroup(await apiClient.settings(groupId), groupId, '运行配置')
  ));
  const serversData = serversQuery.data?.group_id === groupId ? serversQuery.data : undefined;
  const settingsData = settingsQuery.data?.group_id === groupId ? settingsQuery.data : undefined;

  const filters = useWorkshopStore((state) => state.trendFiltersByGroup[groupId]);
  const setTrendFilters = useWorkshopStore((state) => state.setTrendFilters);

  useEffect(() => {
    if (!settingsData || filters?.settingsReady) return;
    const defaultHours = settingsData.effective.default_trend_hours;
    setTrendFilters(groupId, {
      serverId: filters?.serverId ?? 'all',
      hours: filters?.hours ?? defaultHours,
      hoursInput: filters?.hoursInput ?? String(defaultHours),
      settingsReady: true,
    });
  }, [filters, groupId, setTrendFilters, settingsData]);

  const filtersReady = Boolean(filters?.settingsReady);
  const serverId = filtersReady ? filters.serverId : 'all';
  const hours = filtersReady ? filters.hours : 24;
  useEffect(() => {
    if (!filters?.settingsReady || !serversData || filters.serverId === 'all') return;
    if (serversData.servers.some((server) => server.id === filters.serverId)) return;
    setTrendFilters(groupId, { ...filters, serverId: 'all' });
  }, [filters, groupId, serversData, setTrendFilters]);

  function updateFilters(changes: Partial<TrendFiltersState>) {
    if (!filtersReady || !filters) return;
    setTrendFilters(groupId, { ...filters, ...changes });
  }

  const trendKey = queryKeys.mcTrends(groupId, serverId === 'all' ? undefined : serverId, hours);
  const trendsQuery = useCachedQuery<TrendsData>(trendKey, async () => (
    validateGroup(
      await apiClient.trends(groupId, serverId === 'all' ? undefined : serverId, hours),
      groupId,
      '趋势',
    )
  ), { enabled: filtersReady });
  const data = filtersReady && trendsQuery.data?.group_id === groupId ? trendsQuery.data : undefined;

  const serversError = errorMessage(serversQuery.error, '读取服务器失败');
  const settingsError = errorMessage(settingsQuery.error, '读取运行配置失败');
  const trendsError = errorMessage(trendsQuery.error, '读取趋势失败');

  function submitHours(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!filtersReady) return;
    const parsed = Number(filters.hoursInput);
    const next = Number.isFinite(parsed) ? Math.max(1, Math.min(168, Math.round(parsed))) : 24;
    updateFilters({ hoursInput: String(next), hours: next });
    if (next === hours) void trendsQuery.refresh().catch(() => undefined);
  }

  function useQuickHours(value: number) {
    if (!filtersReady) return;
    updateFilters({ hoursInput: String(value), hours: value });
  }

  function selectServer(value: string) {
    if (!filtersReady) return;
    updateFilters({ serverId: value });
  }

  const initialLoading = settingsQuery.isInitialLoading || (filtersReady && trendsQuery.isInitialLoading);
  const blockingError = !settingsData ? settingsError : !data ? trendsError : '';

  return (
    <div className="page-stack">
      <header className="page-heading">
        <div>
          <p className="eyebrow">Hourly Trends</p>
          <h1>在线趋势</h1>
        </div>
        <div className="page-actions">
          <button
            className="wf-button"
            type="button"
            disabled={!filtersReady || trendsQuery.isInitialLoading}
            onClick={() => void trendsQuery.refresh().catch(() => undefined)}
          >
            {trendsQuery.isRefreshing ? '刷新中…' : '刷新趋势'}
          </button>
        </div>
      </header>

      <WorkshopPanel title="趋势条件" description={`当前 group_id：${groupId}`}>
        <form className="trend-filters" onSubmit={submitHours}>
          <SelectField
            id="trend-server"
            label="服务器范围"
            options={[
              { value: 'all', label: '全部服务器（独立结果）' },
              ...(serversData?.servers ?? []).map((server) => ({ value: server.id, label: `${server.name} #${server.id}` })),
            ]}
            value={serverId}
            disabled={!filtersReady}
            onChange={selectServer}
          />
          <label className="wf-label">
            小时数（1–168）
            <input
              className="wf-input"
              type="number"
              min="1"
              max="168"
              step="1"
              inputMode="numeric"
              value={filtersReady ? filters.hoursInput : ''}
              disabled={!filtersReady}
              onChange={(event) => updateFilters({ hoursInput: event.target.value })}
            />
          </label>
          <button className="wf-button wf-button--primary" type="submit" disabled={!filtersReady}>查询趋势</button>
          <div className="quick-hours" role="group" aria-label="快捷小时数">
            {[24, 72, 168].map((value) => (
              <button className="wf-button wf-button--quiet" type="button" key={value} aria-pressed={filtersReady && hours === value} disabled={!filtersReady} onClick={() => useQuickHours(value)}>
                {value} 小时
              </button>
            ))}
          </div>
        </form>
      </WorkshopPanel>

      <p className="sampling-note">缺失采样表示该整点没有有效记录，不等于在线人数真实为 0。</p>
      {serversData && serversError ? <p className="inline-feedback inline-feedback--error" role="alert">{serversError}</p> : null}
      {!serversData && serversError ? <DataState state="error" title="读取服务器失败" message={serversError} action={<button className="wf-button" type="button" onClick={() => void serversQuery.refresh().catch(() => undefined)}>重新加载服务器</button>} /> : null}
      {settingsData && settingsError ? <p className="inline-feedback inline-feedback--error" role="alert">{settingsError}</p> : null}
      {data && trendsError ? <p className="inline-feedback inline-feedback--error" role="alert">{trendsError}</p> : null}
      {initialLoading ? <DataState state="loading" title="正在读取趋势" message={`查询最近 ${hours} 小时的整点采样。`} /> : null}
      {!initialLoading && blockingError ? (
        <DataState
          state="error"
          title={!settingsData ? '读取运行配置失败' : '读取趋势失败'}
          message={blockingError}
          action={(
            <button
              className="wf-button"
              type="button"
              onClick={() => void (!settingsData ? settingsQuery.refresh() : trendsQuery.refresh()).catch(() => undefined)}
            >
              重新加载
            </button>
          )}
        />
      ) : null}
      {!initialLoading && !blockingError && data && !data.results.length ? <DataState state="empty" title="没有趋势数据" message="当前条件未返回任何服务器结果。" /> : null}

      {data?.results.map((result) => (
        <WorkshopPanel
          className="trend-result"
          key={result.server.id}
          title={`${result.server.name} #${result.server.id}`}
          description={result.server.host}
        >
          <dl className="trend-summary">
            <div><dt>latest</dt><dd>{formatNumber(result.latest)}</dd></div>
            <div><dt>max</dt><dd>{formatNumber(result.max)}</dd></div>
            <div><dt>average</dt><dd>{formatNumber(result.average, 1)}</dd></div>
            <div><dt>count</dt><dd>{result.count}</dd></div>
          </dl>
          <TrendChart serverName={result.server.name} hours={data.hours} points={result.points} />
        </WorkshopPanel>
      ))}
    </div>
  );
}

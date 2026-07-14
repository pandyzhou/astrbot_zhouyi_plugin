import { useEffect, useRef, useState } from 'react';
import { ConfirmDialog, DataState, StatusBadge, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { apiClient } from '../../api/client';
import type { ServerRecord, ServersData, SettingsData } from '../../api/types';
import { formatTimestamp } from '../../format';
import { queryCache } from '../../store/queryCacheCore';
import { queryKeyPrefixes, queryKeys } from '../../store/queryKeys';
import { useCachedQuery } from '../../store/useCachedQuery';
import { useWorkshopStore } from '../../store/workshopStore';
import { ServerForm, type ServerFormValue } from './ServerForm';

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

export function ServersPage() {
  const groupId = useWorkshopStore((state) => state.selectedGroupId);
  const groupIdRef = useRef(groupId);
  const autoRefreshedGroupRef = useRef<string | null>(null);
  groupIdRef.current = groupId;

  const serversKey = queryKeys.mcServers(groupId);
  const settingsKey = queryKeys.mcSettings(groupId);
  const serversQuery = useCachedQuery<ServersData>(serversKey, async () => {
    const incoming = validateGroup(await apiClient.servers(groupId), groupId, '服务器列表');
    return mergeSavedSnapshot(queryCache.peek<ServersData>(serversKey)?.data, incoming);
  });
  const settingsQuery = useCachedQuery<SettingsData>(settingsKey, async () => (
    validateGroup(await apiClient.settings(groupId), groupId, '运行配置')
  ));
  const data = serversQuery.data?.group_id === groupId ? serversQuery.data : undefined;
  const settings = settingsQuery.data?.group_id === groupId ? settingsQuery.data : undefined;
  const queryError = serversQuery.error instanceof Error ? serversQuery.error.message : serversQuery.error ? '读取服务器失败' : '';
  const settingsError = settingsQuery.error instanceof Error ? settingsQuery.error.message : settingsQuery.error ? '读取自动刷新配置失败' : '';

  const [error, setError] = useState('');
  const [feedback, setFeedback] = useState('');
  const [busyKey, setBusyKey] = useState('');
  const [formMode, setFormMode] = useState<'add' | 'edit' | null>(null);
  const [editingServer, setEditingServer] = useState<ServerRecord | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ServerRecord | null>(null);

  useEffect(() => {
    autoRefreshedGroupRef.current = null;
    setError('');
    setFeedback('');
    setBusyKey('');
    setFormMode(null);
    setEditingServer(null);
    setDeleteTarget(null);
  }, [groupId]);

  function invalidateGroupTrends(targetGroupId = groupId) {
    queryCache.invalidate([...queryKeyPrefixes.mcTrends, targetGroupId]);
  }

  function updateServersCache(
    updater: (current: ServersData) => ServersData,
    targetGroupId = groupId,
  ) {
    const key = queryKeys.mcServers(targetGroupId);
    const current = queryCache.peek<ServersData>(key)?.data;
    if (!current || current.group_id !== targetGroupId) return;
    queryCache.set(key, updater(current));
  }

  function mergeServers(incoming: ServerRecord[], targetGroupId = groupId) {
    updateServersCache((current) => {
      const byId = new Map(incoming.map((server) => [server.id, server]));
      return {
        ...current,
        servers: current.servers.map((server) => {
          const next = byId.get(server.id);
          return next ? preserveTemporaryStatus(next, server) : server;
        }),
      };
    }, targetGroupId);
  }

  useEffect(() => {
    if (!data?.servers.length || !settings?.effective.auto_refresh_on_page_open) return;
    if (autoRefreshedGroupRef.current === groupId) return;
    autoRefreshedGroupRef.current = groupId;
    const requestedGroupId = groupId;
    setBusyKey('status:all');
    void apiClient.refreshStatus({ group_id: requestedGroupId })
      .then((result) => {
        validateGroup(result, requestedGroupId, '服务器状态');
        mergeServers(result.servers, requestedGroupId);
        invalidateGroupTrends(requestedGroupId);
      })
      .catch((reason: unknown) => {
        if (groupIdRef.current === requestedGroupId) {
          setError((reason as Error).message || '自动刷新服务器状态失败');
        }
      })
      .finally(() => {
        if (groupIdRef.current === requestedGroupId) setBusyKey('');
      });
  }, [data?.servers.length, groupId, settings?.effective.auto_refresh_on_page_open]);

  async function refreshStatus(serverId?: string) {
    const key = serverId ? `status:${serverId}` : 'status:all';
    if (busyKey) return;
    const requestedGroupId = groupId;
    setBusyKey(key);
    setError('');
    setFeedback('');
    try {
      const result = validateGroup(
        await apiClient.refreshStatus({ group_id: requestedGroupId, server_id: serverId }),
        requestedGroupId,
        '服务器状态',
      );
      mergeServers(result.servers, requestedGroupId);
      if (!serverId) {
        updateServersCache((current) => ({ ...current, last_manual_refresh_time: result.refreshed_at }), requestedGroupId);
      }
      invalidateGroupTrends(requestedGroupId);
      if (groupIdRef.current === requestedGroupId) {
        setFeedback(serverId ? '该服务器临时状态已刷新。' : '全部服务器临时状态已刷新。');
      }
    } catch (reason) {
      if (groupIdRef.current === requestedGroupId) setError((reason as Error).message || '刷新状态失败');
    } finally {
      if (groupIdRef.current === requestedGroupId) setBusyKey('');
    }
  }

  async function submitForm(value: ServerFormValue) {
    if (!formMode || busyKey) return;
    const requestedGroupId = groupId;
    setBusyKey('form');
    setError('');
    setFeedback('');
    try {
      if (formMode === 'add') {
        const result = await apiClient.addServer({ group_id: requestedGroupId, ...value });
        const targetKey = queryKeys.mcServers(requestedGroupId);
        const current = queryCache.peek<ServersData>(targetKey)?.data;
        if (current?.group_id === requestedGroupId) {
          queryCache.set(targetKey, { ...current, servers: [...current.servers, result.server] });
        } else {
          queryCache.set(targetKey, { group_id: requestedGroupId, servers: [result.server], last_manual_refresh_time: null });
        }
        invalidateGroupTrends(requestedGroupId);
        if (groupIdRef.current === requestedGroupId) setFeedback(`已添加服务器“${result.server.name}”。`);
      } else if (editingServer) {
        const result = await apiClient.updateServer({
          group_id: requestedGroupId,
          server_id: editingServer.id,
          name: value.name,
          host: value.host,
        });
        mergeServers([result.server], requestedGroupId);
        invalidateGroupTrends(requestedGroupId);
        if (groupIdRef.current === requestedGroupId) setFeedback(`已更新服务器“${result.server.name}”。`);
      }
      if (groupIdRef.current === requestedGroupId) {
        setFormMode(null);
        setEditingServer(null);
      }
    } catch (reason) {
      if (groupIdRef.current === requestedGroupId) setError((reason as Error).message || '保存服务器失败');
    } finally {
      if (groupIdRef.current === requestedGroupId) setBusyKey('');
    }
  }

  async function confirmDelete() {
    if (!deleteTarget || busyKey) return;
    const requestedGroupId = groupId;
    const target = deleteTarget;
    setBusyKey(`delete:${target.id}`);
    setError('');
    try {
      const deletedName = target.name;
      const result = await apiClient.deleteServer({ group_id: requestedGroupId, server_id: target.id });
      updateServersCache((current) => ({
        ...current,
        servers: current.servers.filter((server) => server.id !== result.deleted_server_id),
      }), requestedGroupId);
      invalidateGroupTrends(requestedGroupId);
      if (groupIdRef.current === requestedGroupId) {
        setDeleteTarget(null);
        setFeedback(result.trend_existed
          ? `已删除“${deletedName}”，并级联删除该服务器的趋势数据。`
          : `已删除“${deletedName}”；该服务器没有已保存的趋势数据。`);
      }
    } catch (reason) {
      if (groupIdRef.current === requestedGroupId) setError((reason as Error).message || '删除服务器失败');
    } finally {
      if (groupIdRef.current === requestedGroupId) setBusyKey('');
    }
  }

  return (
    <div className="page-stack">
      <header className="page-heading">
        <div>
          <p className="eyebrow">Server Workshop</p>
          <h1>服务器工坊</h1>
        </div>
        <div className="page-actions">
          <button className="wf-button" type="button" disabled={Boolean(busyKey)} onClick={() => { setEditingServer(null); setFormMode('add'); }}>
            添加服务器
          </button>
          <button className="wf-button wf-button--primary" type="button" disabled={Boolean(busyKey) || !data?.servers.length} onClick={() => void refreshStatus()}>
            {busyKey === 'status:all' ? '刷新中…' : '刷新全部状态'}
          </button>
        </div>
      </header>

      <section className="summary-grid" aria-label="服务器摘要">
        <div className="summary-card"><span>已保存服务器</span><strong>{data?.servers.length ?? 0}</strong></div>
        <div className="summary-card summary-card--wide"><span>最近一次手动刷新</span><strong>{formatTimestamp(data?.last_manual_refresh_time)}</strong></div>
      </section>

      {feedback ? <p className="inline-feedback" role="status">{feedback}</p> : null}
      {error ? <p className="inline-feedback inline-feedback--error" role="alert">{error}</p> : null}
      {data && queryError ? <p className="inline-feedback inline-feedback--error" role="alert">{queryError}</p> : null}
      {settings && settingsError ? <p className="inline-feedback inline-feedback--error" role="alert">{settingsError}</p> : null}
      {!settings && settingsError ? <DataState state="error" title="读取自动刷新配置失败" message={settingsError} action={<button className="wf-button" type="button" onClick={() => void settingsQuery.refresh().catch(() => undefined)}>重新加载配置</button>} /> : null}

      {formMode ? (
        <WorkshopPanel
          title={formMode === 'add' ? '新增服务器' : `编辑服务器 #${editingServer?.id ?? ''}`}
          description={formMode === 'add' ? '填写真实服务器名称与地址；force 仅在预查询失败时使用。' : '修改名称或地址后保存。'}
        >
          <ServerForm
            mode={formMode}
            server={editingServer}
            busy={busyKey === 'form'}
            onSubmit={(value) => void submitForm(value)}
            onCancel={() => { if (!busyKey) { setFormMode(null); setEditingServer(null); } }}
          />
        </WorkshopPanel>
      ) : null}

      <WorkshopPanel title="服务器列表" description={`当前 group_id：${groupId}`}>
        {serversQuery.isInitialLoading ? <DataState state="loading" title="正在读取服务器" /> : null}
        {!data && queryError ? (
          <DataState
            state="error"
            title="读取服务器失败"
            message={queryError}
            action={<button className="wf-button" type="button" onClick={() => void serversQuery.refresh().catch(() => undefined)}>重新加载</button>}
          />
        ) : null}
        {data && !data.servers.length ? <DataState state="empty" title="尚未保存服务器" message="使用“添加服务器”录入名称与地址。" /> : null}
        {data?.servers.length ? (
          <div className="server-list">
            {data.servers.map((server) => (
              <article className="server-row" key={server.id}>
                <div className="server-row-main">
                  <div className="server-identity">
                    <strong>{server.name} <span>#{server.id}</span></strong>
                    <code>{server.host}</code>
                  </div>
                  <div className="server-field"><span>临时状态</span><StatusBadge status={server.status} /></div>
                  <div className="server-field"><span>最后成功</span><time>{formatTimestamp(server.last_success_time)}</time></div>
                  <div className="row-actions">
                    <button className="wf-button wf-button--quiet" type="button" disabled={Boolean(busyKey)} onClick={() => void refreshStatus(server.id)}>
                      {busyKey === `status:${server.id}` ? '刷新中…' : '刷新'}
                    </button>
                    <button className="wf-button wf-button--quiet" type="button" disabled={Boolean(busyKey)} onClick={() => { setEditingServer(server); setFormMode('edit'); }}>
                      编辑
                    </button>
                    <button className="wf-button wf-button--danger" type="button" disabled={Boolean(busyKey)} onClick={() => setDeleteTarget(server)}>
                      删除
                    </button>
                  </div>
                </div>
                <details className="server-details">
                  <summary>展开查询与保存详情</summary>
                  <div className="server-detail-grid">
                    <div><span>版本</span><strong>{server.version ?? '暂无'}</strong></div>
                    <div><span>延迟</span><strong>{server.latency === null ? '暂无' : `${server.latency} ms`}</strong></div>
                    <div><span>玩家</span><strong>{server.players ? `${server.players.online} / ${server.players.max}` : '暂无'}</strong></div>
                    <div><span>创建时间</span><strong>{formatTimestamp(server.created_time)}</strong></div>
                    <div><span>最后失败</span><strong>{formatTimestamp(server.last_failed_time)}</strong></div>
                    <div><span>连续失败次数</span><strong>{server.failed_count}</strong></div>
                    <div><span>本次查询时间</span><strong>{formatTimestamp(server.queried_at)}</strong></div>
                    <div className="server-icon-cell">
                      <span>服务器图标</span>
                      {server.icon ? <img src={server.icon} alt={`${server.name} 的服务器图标`} /> : <strong>未提供</strong>}
                    </div>
                  </div>
                  <div className="player-sample">
                    <span>在线玩家列表</span>
                    {server.players?.sample.length ? (
                      <ul>{server.players.sample.map((player, index) => <li key={player.id ?? `${player.name}-${index}`}>{player.name}</li>)}</ul>
                    ) : <p>当前服务器中没有玩家</p>}
                  </div>
                </details>
              </article>
            ))}
          </div>
        ) : null}
      </WorkshopPanel>

      <ConfirmDialog
        open={Boolean(deleteTarget)}
        title="删除服务器"
        description={deleteTarget ? `确认删除“${deleteTarget.name}”（#${deleteTarget.id}）？此操作会级联删除该服务器的全部趋势数据。` : ''}
        confirmLabel="删除并级联清理趋势"
        danger
        busy={busyKey.startsWith('delete:')}
        onClose={() => { if (!busyKey) setDeleteTarget(null); }}
        onConfirm={() => void confirmDelete()}
      />
    </div>
  );
}

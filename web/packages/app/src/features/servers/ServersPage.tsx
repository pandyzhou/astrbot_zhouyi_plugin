import { useEffect, useState } from 'react';
import { ConfirmDialog, DataState, StatusBadge, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { apiClient } from '../../api/client';
import type { ServerRecord, ServersData } from '../../api/types';
import { formatTimestamp } from '../../format';
import { useWorkshopStore } from '../../store/workshopStore';
import { ServerForm, type ServerFormValue } from './ServerForm';

export function ServersPage() {
  const groupId = useWorkshopStore((state) => state.selectedGroupId);
  const [data, setData] = useState<ServersData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [feedback, setFeedback] = useState('');
  const [busyKey, setBusyKey] = useState('');
  const [formMode, setFormMode] = useState<'add' | 'edit' | null>(null);
  const [editingServer, setEditingServer] = useState<ServerRecord | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ServerRecord | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setData(null);
    setError('');
    setFeedback('');
    setFormMode(null);
    setEditingServer(null);
    setDeleteTarget(null);
    apiClient.servers(groupId, controller.signal)
      .then(setData)
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') setError((reason as Error).message || '读取服务器失败');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [groupId]);

  function mergeServers(incoming: ServerRecord[]) {
    setData((current) => {
      if (!current) return current;
      const byId = new Map(incoming.map((server) => [server.id, server]));
      return { ...current, servers: current.servers.map((server) => byId.get(server.id) ?? server) };
    });
  }

  async function refreshStatus(serverId?: string) {
    const key = serverId ? `status:${serverId}` : 'status:all';
    if (busyKey) return;
    setBusyKey(key);
    setError('');
    setFeedback('');
    try {
      const result = await apiClient.refreshStatus({ group_id: groupId, server_id: serverId });
      mergeServers(result.servers);
      if (!serverId) {
        setData((current) => current ? { ...current, last_manual_refresh_time: result.refreshed_at } : current);
      }
      setFeedback(serverId ? '该服务器临时状态已刷新。' : '全部服务器临时状态已刷新。');
    } catch (reason) {
      setError((reason as Error).message || '刷新状态失败');
    } finally {
      setBusyKey('');
    }
  }

  async function submitForm(value: ServerFormValue) {
    if (!formMode || busyKey) return;
    setBusyKey('form');
    setError('');
    setFeedback('');
    try {
      if (formMode === 'add') {
        const result = await apiClient.addServer({ group_id: groupId, ...value });
        setData((current) => current ? { ...current, servers: [...current.servers, result.server] } : current);
        setFeedback(`已添加服务器“${result.server.name}”。`);
      } else if (editingServer) {
        const result = await apiClient.updateServer({
          group_id: groupId,
          server_id: editingServer.id,
          name: value.name,
          host: value.host,
        });
        mergeServers([result.server]);
        setFeedback(`已更新服务器“${result.server.name}”。`);
      }
      setFormMode(null);
      setEditingServer(null);
    } catch (reason) {
      setError((reason as Error).message || '保存服务器失败');
    } finally {
      setBusyKey('');
    }
  }

  async function confirmDelete() {
    if (!deleteTarget || busyKey) return;
    setBusyKey(`delete:${deleteTarget.id}`);
    setError('');
    try {
      const deletedName = deleteTarget.name;
      const result = await apiClient.deleteServer({ group_id: groupId, server_id: deleteTarget.id });
      setData((current) => current ? {
        ...current,
        servers: current.servers.filter((server) => server.id !== result.deleted_server_id),
      } : current);
      setDeleteTarget(null);
      setFeedback(result.trend_existed
        ? `已删除“${deletedName}”，并级联删除该服务器的趋势数据。`
        : `已删除“${deletedName}”；该服务器没有已保存的趋势数据。`);
    } catch (reason) {
      setError((reason as Error).message || '删除服务器失败');
    } finally {
      setBusyKey('');
    }
  }

  return (
    <div className="page-stack">
      <header className="page-heading">
        <div>
          <p className="eyebrow">Server Workshop</p>
          <h1>服务器工坊</h1>
          <p>管理当前 group_id 保存的服务器，并按需查询临时在线状态。</p>
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
        {loading ? <DataState state="loading" title="正在读取服务器" /> : null}
        {!loading && !error && !data?.servers.length ? <DataState state="empty" title="尚未保存服务器" message="使用“添加服务器”录入名称与地址。" /> : null}
        {!loading && !error && data?.servers.length ? (
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
                    <span>玩家 sample（非完整名单）</span>
                    {server.players?.sample.length ? (
                      <ul>{server.players.sample.map((player, index) => <li key={player.id ?? `${player.name}-${index}`}>{player.name}</li>)}</ul>
                    ) : <p>本次查询未返回玩家 sample。</p>}
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

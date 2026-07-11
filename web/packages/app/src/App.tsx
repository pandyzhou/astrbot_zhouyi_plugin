import { useEffect, useState } from 'react';
import { DataState, SelectField, WorkshopShell } from '@pandyzhou/astrbot-mc-ui';
import { HashRouter, Navigate, NavLink, Route, Routes } from 'react-router-dom';
import { ApiClientError, apiClient } from './api/client';
import { ServersPage } from './features/servers/ServersPage';
import { TrendsPage } from './features/trends/TrendsPage';
import { initializeGroups, useWorkshopStore } from './store/workshopStore';

function AppContent() {
  const groups = useWorkshopStore((state) => state.groups);
  const selectedGroupId = useWorkshopStore((state) => state.selectedGroupId);
  const selectGroup = useWorkshopStore((state) => state.selectGroup);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<Error | null>(null);

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    apiClient.bootstrap(controller.signal)
      .then((data) => initializeGroups(data.groups, data.default_group_id))
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') {
          setError(reason instanceof Error ? reason : new Error('初始化失败'));
        }
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, []);

  const authRequired = !window.AstrBotPluginPage
    && error instanceof ApiClientError
    && error.code === 'AUTH_REQUIRED';
  const dashboardLoginUrl = `https://${window.location.hostname}:35015/#/auth/login`;

  const groupControl = (
    <SelectField
      className="wf-select-field--inline"
      id="global-group"
      label="群组 group_id"
      options={groups.map((group) => ({
        value: group.group_id,
        label: `${group.label}（${group.group_id}）`,
      }))}
      value={selectedGroupId}
      placeholder="暂无可用群组"
      disabled={loading || groups.length === 0}
      onChange={selectGroup}
    />
  );

  return (
    <WorkshopShell
      brand="Minecraft 方块工坊"
      groupControl={groupControl}
      navigation={(
        <>
          <NavLink to="/servers">服务器工坊</NavLink>
          <NavLink to="/trends">在线趋势</NavLink>
        </>
      )}
    >
      {loading ? <DataState state="loading" title="正在读取插件上下文" message="正在加载群组与服务器入口。" /> : null}
      {!loading && error ? (
        <DataState
          state="error"
          title={authRequired ? '需要登录 AstrBot' : '无法初始化管理界面'}
          message={error.message}
          action={authRequired ? (
            <div className="auth-required-actions">
              <a
                className="wf-button"
                href={dashboardLoginUrl}
                target="_blank"
                rel="noopener noreferrer"
              >
                前往 AstrBot 登录
              </a>
              <button className="wf-button" type="button" onClick={() => window.location.reload()}>
                登录后重新连接
              </button>
            </div>
          ) : undefined}
        />
      ) : null}
      {!loading && !error && !selectedGroupId ? (
        <DataState state="empty" title="没有可管理的群组" message="后端 bootstrap 未返回 group_id。" />
      ) : null}
      {!loading && !error && selectedGroupId ? (
        <Routes>
          <Route path="/servers" element={<ServersPage />} />
          <Route path="/trends" element={<TrendsPage />} />
          <Route path="*" element={<Navigate replace to="/servers" />} />
        </Routes>
      ) : null}
    </WorkshopShell>
  );
}

export function App() {
  return (
    <HashRouter>
      <AppContent />
    </HashRouter>
  );
}

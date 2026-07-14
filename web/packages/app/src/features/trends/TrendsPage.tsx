import { useEffect, useState, type FormEvent } from 'react';
import { DataState, SelectField, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { apiClient } from '../../api/client';
import type { ServerRecord, TrendsData } from '../../api/types';
import { formatNumber } from '../../format';
import { useWorkshopStore } from '../../store/workshopStore';
import { TrendChart } from './TrendChart';

export function TrendsPage() {
  const groupId = useWorkshopStore((state) => state.selectedGroupId);
  const [servers, setServers] = useState<ServerRecord[]>([]);
  const [serverId, setServerId] = useState('all');
  const [hours, setHours] = useState(24);
  const [hoursInput, setHoursInput] = useState('24');
  const [data, setData] = useState<TrendsData | null>(null);
  const [loading, setLoading] = useState(true);
  const [settingsReady, setSettingsReady] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    const controller = new AbortController();
    setServers([]);
    setServerId('all');
    setData(null);
    setError('');
    setSettingsReady(false);
    setLoading(true);
    Promise.all([
      apiClient.servers(groupId, controller.signal),
      apiClient.settings(groupId, controller.signal),
    ])
      .then(([serverResult, settings]) => {
        const defaultHours = settings.effective.default_trend_hours;
        setServers(serverResult.servers);
        setHours(defaultHours);
        setHoursInput(String(defaultHours));
        setSettingsReady(true);
      })
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') {
          setError((reason as Error).message || '读取服务器与运行配置失败');
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, [groupId]);

  useEffect(() => {
    if (!settingsReady) return undefined;
    const controller = new AbortController();
    setLoading(true);
    setError('');
    apiClient.trends(groupId, serverId === 'all' ? undefined : serverId, hours, controller.signal)
      .then(setData)
      .catch((reason: unknown) => {
        if ((reason as Error).name !== 'AbortError') setError((reason as Error).message || '读取趋势失败');
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [groupId, serverId, hours, settingsReady]);

  function submitHours(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const parsed = Number(hoursInput);
    const next = Number.isFinite(parsed) ? Math.max(1, Math.min(168, Math.round(parsed))) : 24;
    setHoursInput(String(next));
    setHours(next);
  }

  function useQuickHours(value: number) {
    setHoursInput(String(value));
    setHours(value);
  }

  return (
    <div className="page-stack">
      <header className="page-heading">
        <div>
          <p className="eyebrow">Hourly Trends</p>
          <h1>在线趋势</h1>
        </div>
      </header>

      <WorkshopPanel title="趋势条件" description={`当前 group_id：${groupId}`}>
        <form className="trend-filters" onSubmit={submitHours}>
          <SelectField
            id="trend-server"
            label="服务器范围"
            options={[
              { value: 'all', label: '全部服务器（独立结果）' },
              ...servers.map((server) => ({ value: server.id, label: `${server.name} #${server.id}` })),
            ]}
            value={serverId}
            disabled={loading}
            onChange={setServerId}
          />
          <label className="wf-label">
            小时数（1–168）
            <input className="wf-input" type="number" min="1" max="168" step="1" inputMode="numeric" value={hoursInput} onChange={(event) => setHoursInput(event.target.value)} />
          </label>
          <button className="wf-button wf-button--primary" type="submit" disabled={loading}>查询趋势</button>
          <div className="quick-hours" role="group" aria-label="快捷小时数">
            {[24, 72, 168].map((value) => (
              <button className="wf-button wf-button--quiet" type="button" key={value} aria-pressed={hours === value} disabled={loading} onClick={() => useQuickHours(value)}>
                {value} 小时
              </button>
            ))}
          </div>
        </form>
      </WorkshopPanel>

      <p className="sampling-note">缺失采样表示该整点没有有效记录，不等于在线人数真实为 0。</p>
      {error ? <p className="inline-feedback inline-feedback--error" role="alert">{error}</p> : null}
      {loading ? <DataState state="loading" title="正在读取趋势" message={`查询最近 ${hours} 小时的整点采样。`} /> : null}
      {!loading && !error && !data?.results.length ? <DataState state="empty" title="没有趋势数据" message="当前条件未返回任何服务器结果。" /> : null}

      {!loading && !error && data?.results.map((result) => (
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

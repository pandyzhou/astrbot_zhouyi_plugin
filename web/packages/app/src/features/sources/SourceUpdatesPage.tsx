import { useEffect, useState } from 'react';
import { DataState, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { apiClient } from '../../api/client';
import type { SourceUpdateItem, SourceUpdateStatus, SourceUpdatesData } from '../../api/types';
import { formatTimestamp } from '../../format';
import { useI18n } from '../../i18n';
import { queryKeys } from '../../store/queryKeys';
import { useCachedQuery } from '../../store/useCachedQuery';

const SOURCE_UPDATES_TTL = 300_000;

function safeExternalUrl(value: string | null): string | null {
  if (!value) return null;
  try {
    const url = new URL(value);
    return url.protocol === 'https:' ? url.href : null;
  } catch {
    return null;
  }
}

function displayValue(value: string | null | undefined) {
  return value || '暂无';
}

function SourceStatusChip({ status }: { status: SourceUpdateStatus }) {
  const { t } = useI18n();
  const labels: Record<SourceUpdateStatus, string> = {
    current: t('sourceStatusCurrent'),
    new_version: t('sourceStatusNewVersion'),
    new_commits: t('sourceStatusNewCommits'),
    changed: t('sourceStatusChanged'),
    unavailable: t('sourceStatusUnavailable'),
  };
  return <span className={`source-status-chip source-status-chip--${status.replace('_', '-')}`}>{labels[status]}</span>;
}

function SourceCard({ source }: { source: SourceUpdateItem }) {
  const repositoryUrl = safeExternalUrl(source.upstream.repository_url);
  const baselineCommitUrl = safeExternalUrl(
    source.baseline.repository && source.baseline.commit_sha
      ? `https://github.com/${source.baseline.repository}/commit/${source.baseline.commit_sha}`
      : null,
  );
  const commitUrl = safeExternalUrl(source.upstream.commit_url);

  return (
    <WorkshopPanel className="source-update-card" title={source.display_name} actions={<SourceStatusChip status={source.status} />}>
      <div className="source-role"><span>作用</span><strong>{source.role}</strong>{source.stale ? <span className="source-stale-chip">缓存数据</span> : null}</div>
      <div className="source-version-grid">
        <section>
          <h3>当前基线</h3>
          <dl>
            <div><dt>版本</dt><dd>{displayValue(source.baseline.version)}</dd></div>
            <div><dt>提交 SHA</dt><dd><code title={source.baseline.commit_sha ?? undefined}>{displayValue(source.baseline.commit_sha)}</code></dd></div>
            <div><dt>仓库</dt><dd><code>{displayValue(source.baseline.repository)}</code></dd></div>
            <div><dt>分支</dt><dd><code>{displayValue(source.baseline.branch)}</code></dd></div>
          </dl>
        </section>
        <section>
          <h3>上游状态</h3>
          <dl>
            <div><dt>版本</dt><dd>{displayValue(source.upstream.version)}</dd></div>
            <div><dt>提交 SHA</dt><dd><code title={source.upstream.commit_sha ?? undefined}>{displayValue(source.upstream.commit_sha)}</code></dd></div>
          </dl>
          <div className="source-latest-commit">
            <span>最新提交</span>
            <strong>{displayValue(source.upstream.commit_title)}</strong>
            <time>{formatTimestamp(source.upstream.committed_at)}</time>
          </div>
        </section>
      </div>
      {source.error ? <p className="inline-feedback inline-feedback--error source-error" role="alert">{source.error}</p> : null}
      {repositoryUrl || baselineCommitUrl || commitUrl ? (
        <div className="source-link-actions" aria-label={`${source.display_name} 外部链接`}>
          {repositoryUrl ? <a className="wf-button wf-button--quiet" href={repositoryUrl} target="_blank" rel="noopener noreferrer">查看上游仓库</a> : null}
          {baselineCommitUrl ? <a className="wf-button wf-button--quiet" href={baselineCommitUrl} target="_blank" rel="noopener noreferrer">查看基线提交</a> : null}
          {commitUrl ? <a className="wf-button wf-button--quiet" href={commitUrl} target="_blank" rel="noopener noreferrer">查看最新提交</a> : null}
        </div>
      ) : null}
    </WorkshopPanel>
  );
}

export function SourceUpdatesPage() {
  const { t } = useI18n();
  const sourceUpdatesQuery = useCachedQuery<SourceUpdatesData>(
    queryKeys.sourceUpdates,
    () => apiClient.sourceUpdates(),
    { ttl: SOURCE_UPDATES_TTL },
  );
  const [refreshing, setRefreshing] = useState(false);
  const [refreshError, setRefreshError] = useState('');
  const [currentTime, setCurrentTime] = useState(() => Date.now() / 1000);
  const data = sourceUpdatesQuery.data;
  const queryError = sourceUpdatesQuery.error instanceof Error
    ? sourceUpdatesQuery.error.message
    : sourceUpdatesQuery.error ? String(sourceUpdatesQuery.error) : '';
  const refreshBlocked = Boolean(data?.refresh_allowed_at && data.refresh_allowed_at > currentTime);

  useEffect(() => {
    if (!data?.refresh_allowed_at || data.refresh_allowed_at <= currentTime) return;
    const timer = window.setTimeout(
      () => setCurrentTime(Date.now() / 1000),
      Math.max(0, (data.refresh_allowed_at - currentTime) * 1000 + 50),
    );
    return () => window.clearTimeout(timer);
  }, [currentTime, data?.refresh_allowed_at]);

  const cacheStatus = data
    ? refreshBlocked
      ? `缓存或限流生效中，可再次检查时间：${formatTimestamp(data.refresh_allowed_at)}`
      : data.next_check_at && data.next_check_at > currentTime
        ? `缓存有效至 ${formatTimestamp(data.next_check_at)}，当前允许强制检查`
        : '缓存已到期，当前允许强制检查'
    : '等待首次检查';
  const rateLimitStatus = data?.rate_limit
    ? `请求配额：${data.rate_limit.remaining ?? '未知'} / ${data.rate_limit.limit ?? '未知'}；重置时间：${formatTimestamp(data.rate_limit.reset_at)}`
    : '未提供上游限流信息';

  async function forceRefresh() {
    if (refreshing || refreshBlocked) return;
    setRefreshing(true);
    setRefreshError('');
    try {
      const result = await apiClient.refreshSourceUpdates();
      sourceUpdatesQuery.setData(result);
    } catch (reason) {
      setRefreshError(reason instanceof Error ? reason.message : '强制检查来源更新失败');
    } finally {
      setRefreshing(false);
    }
  }

  return (
    <div className="page-stack">
      <header className="page-heading">
        <div><h1>{t('sourceUpdates')}</h1></div>
        <button className="wf-button wf-button--primary source-refresh-button" type="button" disabled={refreshing || refreshBlocked} onClick={() => void forceRefresh()}>
          {refreshing ? t('sourceRefreshing') : t('sourceForceRefresh')}
        </button>
      </header>

      <section className="source-update-meta" aria-label="来源更新检查状态">
        <article><span>最近检查时间</span><strong>{formatTimestamp(data?.checked_at)}</strong></article>
        <article><span>缓存与限流状态</span><strong>{cacheStatus}</strong><small>{rateLimitStatus}</small></article>
      </section>

      {refreshError ? <p className="inline-feedback inline-feedback--error" role="alert">{refreshError}</p> : null}
      {data && queryError ? <p className="inline-feedback inline-feedback--error" role="alert">{queryError}</p> : null}
      {sourceUpdatesQuery.isRefreshing ? <p className="inline-feedback" role="status">正在后台更新检查结果…</p> : null}

      {sourceUpdatesQuery.isInitialLoading ? <DataState state="loading" title={t('loading')} /> : null}
      {!data && queryError ? <DataState state="error" title={t('operationFailed')} message={queryError} action={<button className="wf-button" type="button" onClick={() => void sourceUpdatesQuery.refresh().catch(() => undefined)}>{t('retry')}</button>} /> : null}
      {data && !data.sources.length ? <DataState state="empty" title={t('empty')} /> : null}
      {data?.sources.length ? <div className="source-update-grid">{data.sources.map((source) => <SourceCard key={source.id} source={source} />)}</div> : null}
    </div>
  );
}

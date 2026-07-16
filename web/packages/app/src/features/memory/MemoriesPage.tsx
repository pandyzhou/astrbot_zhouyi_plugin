import { useCallback, useMemo, useRef, useState } from 'react';
import { DataState, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { memoryAdminClient } from '../../api/client';
import { useI18n } from '../../i18n';
import { queryCache } from '../../store/queryCacheCore';
import {
  MEMORY_CONFLICTS_QUERY_PREFIX,
  MEMORY_GRAPH_QUERY_PREFIX,
  MEMORY_LIST_QUERY_PREFIX,
  MEMORY_MAINTENANCE_QUERY_PREFIX,
  MEMORY_OBJECTS_QUERY_PREFIX,
  MEMORY_OVERVIEW_QUERY_PREFIX,
  queryKeys,
} from '../../store/queryKeys';
import { useCachedQuery } from '../../store/useCachedQuery';
import { IdentityMappingsPanel } from './IdentityMappingsPanel';
import { MemoryConflictPanel } from './MemoryConflictPanel';
import { MemoryDetailDrawer, formatMemoryTime } from './MemoryDetailDrawer';
import { MemoryMergeDialog } from './MemoryMergeDialog';
import { MemoryObjectEditor } from './MemoryObjectEditor';
import { buildMergePayload } from './memoryAdminState';
import type {
  MemoryMergePreviewData,
  MemoryObject,
  MemoryObjectDetailData,
  MemoryObjectFilters,
  MemoryObjectMutationInput,
  MemoryObjectsData,
} from './types';

type Workspace = 'objects' | 'conflicts' | 'identities';

function errorMessage(reason: unknown) {
  return reason instanceof Error ? reason.message : String(reason ?? '');
}

export function MemoriesPage() {
  const { t } = useI18n();
  const [workspace, setWorkspace] = useState<Workspace>('objects');
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [filters, setFilters] = useState({
    keyword: '',
    owner: '',
    scope: 'all',
    persona: '',
    status: 'all',
    type: '',
    conflict: 'all',
    index: 'all',
  });
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [detail, setDetail] = useState<MemoryObjectDetailData | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [creating, setCreating] = useState(false);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [feedbackError, setFeedbackError] = useState(false);
  const [mergeOpen, setMergeOpen] = useState(false);
  const [mergePreview, setMergePreview] = useState<MemoryMergePreviewData | null>(null);

  const identitiesQuery = useCachedQuery(
    queryKeys.memoryIdentities,
    () => memoryAdminClient.identities(),
    { ttl: 60_000 },
  );
  const maintenanceQuery = useCachedQuery(
    queryKeys.memoryMaintenance,
    () => memoryAdminClient.maintenance(),
    { ttl: 15_000 },
  );
  const owners = identitiesQuery.data?.owners ?? [];
  const queryableOwners = owners.filter((owner) => owner.status === 'active');
  const activeOwnerUserId = queryableOwners.some((owner) => owner.owner_user_id === filters.owner)
    ? filters.owner
    : queryableOwners[0]?.owner_user_id ?? '';

  const params = useMemo<MemoryObjectFilters>(() => ({
    page,
    page_size: pageSize,
    owner_user_id: activeOwnerUserId,
    keyword: filters.keyword || undefined,
    scope: filters.scope as MemoryObjectFilters['scope'],
    persona_id: filters.persona || undefined,
    status: filters.status as MemoryObjectFilters['status'],
    memory_type: filters.type || undefined,
    conflict: filters.conflict as MemoryObjectFilters['conflict'],
    index_status: filters.index as MemoryObjectFilters['index_status'],
    sort: 'updated_desc',
  }), [activeOwnerUserId, filters, page, pageSize]);
  const listKey = useMemo(() => queryKeys.memoryObjects(params), [params]);
  const paramsRef = useRef(params);
  const keyRef = useRef(listKey);
  paramsRef.current = params;
  keyRef.current = listKey;
  const listQuery = useCachedQuery<MemoryObjectsData>(
    listKey,
    () => memoryAdminClient.objects(params),
    { enabled: workspace === 'objects' && Boolean(activeOwnerUserId) },
  );
  const data = listQuery.data;
  const itemsById = useMemo(
    () => new Map((data?.items ?? []).map((item) => [item.memory_item_id, item])),
    [data],
  );
  const selectedItems = useMemo(
    () => [...selected]
      .map((id) => itemsById.get(id))
      .filter((item): item is MemoryObject => Boolean(item)),
    [itemsById, selected],
  );
  const allSelected = Boolean(data?.items.length)
    && Boolean(data?.items.every((item) => selected.has(item.memory_item_id)));

  const changeOwner = (ownerUserId: string) => {
    setFilters((value) => ({ ...value, owner: ownerUserId }));
    setPage(1);
    setSelected(new Set());
    setDetail(null);
    setMergeOpen(false);
    setMergePreview(null);
  };

  const invalidateAdmin = useCallback(() => {
    queryCache.invalidate(MEMORY_OBJECTS_QUERY_PREFIX);
    queryCache.invalidate(MEMORY_LIST_QUERY_PREFIX);
    queryCache.invalidate(MEMORY_CONFLICTS_QUERY_PREFIX);
    queryCache.invalidate(MEMORY_MAINTENANCE_QUERY_PREFIX);
    queryCache.invalidate(MEMORY_OVERVIEW_QUERY_PREFIX);
    queryCache.invalidate(MEMORY_GRAPH_QUERY_PREFIX);
  }, []);

  const refreshAfterMutation = useCallback(async () => {
    invalidateAdmin();
    setSelected(new Set());
    const refreshes: Array<Promise<unknown>> = [maintenanceQuery.refresh()];
    if (paramsRef.current.owner_user_id) {
      refreshes.push(queryCache.revalidate(
        keyRef.current,
        () => memoryAdminClient.objects(paramsRef.current),
      ));
    }
    await Promise.allSettled(refreshes);
  }, [invalidateAdmin, maintenanceQuery]);

  const runMutation = async (action: () => Promise<unknown>, success = '操作完成') => {
    setBusy(true);
    setFeedback('');
    try {
      await action();
      await refreshAfterMutation();
      setFeedbackError(false);
      setFeedback(success);
    } catch (reason) {
      setFeedbackError(true);
      setFeedback(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const openDetail = async (item: MemoryObject) => {
    setDetailLoading(true);
    setDetail(null);
    setFeedback('');
    try {
      setDetail(await memoryAdminClient.objectDetail(item.owner_user_id, item.memory_item_id));
    } catch (reason) {
      setFeedbackError(true);
      setFeedback(errorMessage(reason));
    } finally {
      setDetailLoading(false);
    }
  };

  const createObject = async (payload: MemoryObjectMutationInput) => {
    await runMutation(async () => {
      const created = await memoryAdminClient.createObject(payload);
      setDetail(created);
      setCreating(false);
    }, '记忆对象已创建');
  };

  const archiveSelected = () => {
    const ownerUserId = selectedItems[0]?.owner_user_id ?? activeOwnerUserId;
    return runMutation(() => memoryAdminClient.batchObjects({
      owner_user_id: ownerUserId,
      action: 'archive',
      items: selectedItems.map((item) => ({
        memory_item_id: item.memory_item_id,
        expected_version: item.version,
      })),
    }), '所选对象已归档');
  };

  const retryIndex = (items: MemoryObject[]) => {
    const ownerUserId = items[0]?.owner_user_id ?? activeOwnerUserId;
    return runMutation(() => memoryAdminClient.retryIndex({
      owner_user_id: ownerUserId,
      items: items.map((item) => ({
        memory_item_id: item.memory_item_id,
        expected_version: item.version,
      })),
    }), '索引重试已排队');
  };

  const previewMerge = async (survivorId: string) => {
    const survivor = selectedItems.find((item) => item.memory_item_id === survivorId);
    if (!survivor) return;
    const expected_versions = Object.fromEntries(
      selectedItems.map((item) => [item.memory_item_id, item.version]),
    );
    setBusy(true);
    setFeedback('');
    setMergePreview(null);
    try {
      setMergePreview(await memoryAdminClient.mergePreview({
        owner_user_id: survivor.owner_user_id,
        survivor_memory_item_id: survivorId,
        source_memory_item_ids: selectedItems
          .filter((item) => item.memory_item_id !== survivorId)
          .map((item) => item.memory_item_id),
        expected_versions,
      }));
    } catch (reason) {
      setFeedbackError(true);
      setFeedback(errorMessage(reason));
    } finally {
      setBusy(false);
    }
  };

  const confirmMerge = async (content: string, reason: string) => {
    if (!mergePreview) return;
    const payload = buildMergePayload(mergePreview, content, reason);
    await runMutation(async () => {
      const merged = await memoryAdminClient.merge(payload);
      setDetail(merged);
      setMergeOpen(false);
      setMergePreview(null);
    }, '对象已合并');
  };

  return (
    <div className="page-stack">
      <header className="page-heading">
        <div>
          <p className="eyebrow">MEMORY ADMIN</p>
          <h1>{t('memories')}</h1>
        </div>
        <div className="page-actions">
          <button
            className="wf-button"
            type="button"
            onClick={() => {
              if (workspace === 'objects') {
                const refreshes: Array<Promise<unknown>> = [
                  identitiesQuery.refresh(),
                  maintenanceQuery.refresh(),
                ];
                if (activeOwnerUserId) refreshes.push(listQuery.refresh());
                void Promise.allSettled(refreshes);
              } else {
                queryCache.invalidate(['memory', workspace]);
              }
            }}
          >
            {t('refresh')}
          </button>
          {workspace === 'objects' ? (
            <button
              className="wf-button wf-button--primary"
              type="button"
              onClick={() => setCreating((value) => !value)}
            >
              新建对象
            </button>
          ) : null}
        </div>
      </header>

      <nav className="memory-workspace-tabs" aria-label="记忆管理工作区">
        {([
          ['objects', '记忆对象'],
          ['conflicts', '冲突'],
          ['identities', '身份映射'],
        ] as const).map(([value, label]) => (
          <button
            key={value}
            type="button"
            aria-pressed={workspace === value}
            onClick={() => setWorkspace(value)}
          >
            {label}
          </button>
        ))}
      </nav>

      {feedback ? (
        <p
          className={`inline-feedback${feedbackError ? ' inline-feedback--error' : ''}`}
          role={feedbackError ? 'alert' : 'status'}
        >
          {feedback}
        </p>
      ) : null}

      {workspace === 'conflicts' ? (
        <MemoryConflictPanel
          owners={queryableOwners}
          ownerUserId={activeOwnerUserId}
          onOwnerChange={changeOwner}
        />
      ) : null}
      {workspace === 'identities' ? <IdentityMappingsPanel /> : null}

      {workspace === 'objects' ? (
        <>
          {creating ? (
            <WorkshopPanel title="新建记忆对象">
              <MemoryObjectEditor
                owners={queryableOwners.map((owner) => ({
                  owner_user_id: owner.owner_user_id,
                  display_name: owner.display_name,
                }))}
                submitLabel="创建对象"
                busy={busy}
                onCancel={() => setCreating(false)}
                onSubmit={(payload) => createObject(payload as MemoryObjectMutationInput)}
              />
            </WorkshopPanel>
          ) : null}

          <section className="maintenance-strip" aria-label="对象维护状态">
            <div>
              <span>迁移</span>
              <strong>{maintenanceQuery.data?.migration.state ?? '—'}</strong>
              <small>
                {maintenanceQuery.data?.migration.processed ?? 0}/
                {maintenanceQuery.data?.migration.total ?? 0}
              </small>
            </div>
            <div>
              <span>未解析 owner</span>
              <strong>{maintenanceQuery.data?.migration.unresolved_owner_count ?? 0}</strong>
            </div>
            <div>
              <span>来源覆盖</span>
              <strong>{Math.round((maintenanceQuery.data?.sources.coverage_ratio ?? 0) * 100)}%</strong>
            </div>
            <div>
              <span>索引修复</span>
              <strong>{maintenanceQuery.data?.index.needs_repair_count ?? 0}</strong>
            </div>
          </section>

          <WorkshopPanel title={t('filters')}>
            <form
              className="memory-object-filter-grid"
              onSubmit={(event) => {
                event.preventDefault();
                setPage(1);
              }}
            >
              <label className="wf-label">
                关键词
                <input
                  className="wf-input"
                  value={filters.keyword}
                  onChange={(event) => setFilters((value) => ({
                    ...value,
                    keyword: event.target.value,
                  }))}
                />
              </label>
              <label className="wf-label">
                Owner
                <select
                  className="wf-input"
                  value={activeOwnerUserId}
                  onChange={(event) => changeOwner(event.target.value)}
                  required
                >
                  <option value="" disabled>全部 owner（请选择具体 owner）</option>
                  {queryableOwners.map((owner) => (
                    <option key={owner.owner_user_id} value={owner.owner_user_id}>
                      {owner.display_name}
                    </option>
                  ))}
                </select>
              </label>
              <label className="wf-label">
                Scope
                <select
                  className="wf-input"
                  value={filters.scope}
                  onChange={(event) => {
                    setFilters((value) => ({ ...value, scope: event.target.value }));
                    setPage(1);
                  }}
                >
                  <option value="all">全部</option>
                  <option value="user">user</option>
                  <option value="persona">persona</option>
                  <option value="session">session</option>
                  <option value="public">public</option>
                  <option value="legacy_session">legacy_session</option>
                </select>
              </label>
              <label className="wf-label">
                Persona
                <input
                  className="wf-input"
                  value={filters.persona}
                  onChange={(event) => setFilters((value) => ({
                    ...value,
                    persona: event.target.value,
                  }))}
                />
              </label>
              <label className="wf-label">
                状态
                <select
                  className="wf-input"
                  value={filters.status}
                  onChange={(event) => {
                    setFilters((value) => ({ ...value, status: event.target.value }));
                    setPage(1);
                  }}
                >
                  <option value="all">全部</option>
                  <option value="active">active</option>
                  <option value="conflicted">conflicted</option>
                  <option value="archived">archived</option>
                  <option value="superseded">superseded</option>
                </select>
              </label>
              <label className="wf-label">
                类型
                <input
                  className="wf-input"
                  value={filters.type}
                  onChange={(event) => setFilters((value) => ({
                    ...value,
                    type: event.target.value,
                  }))}
                />
              </label>
              <label className="wf-label">
                冲突
                <select
                  className="wf-input"
                  value={filters.conflict}
                  onChange={(event) => {
                    setFilters((value) => ({ ...value, conflict: event.target.value }));
                    setPage(1);
                  }}
                >
                  <option value="all">全部</option>
                  <option value="yes">有冲突</option>
                  <option value="no">无冲突</option>
                </select>
              </label>
              <label className="wf-label">
                索引
                <select
                  className="wf-input"
                  value={filters.index}
                  onChange={(event) => {
                    setFilters((value) => ({ ...value, index: event.target.value }));
                    setPage(1);
                  }}
                >
                  <option value="all">全部</option>
                  <option value="synced">synced</option>
                  <option value="pending">pending</option>
                  <option value="needs_repair">needs_repair</option>
                  <option value="disabled">disabled</option>
                </select>
              </label>
              <button className="wf-button wf-button--primary" type="submit">搜索</button>
            </form>
          </WorkshopPanel>

          <div className="batch-toolbar">
            <label>
              <input
                aria-label="全选当前页"
                type="checkbox"
                checked={allSelected}
                onChange={(event) => setSelected(
                  event.target.checked
                    ? new Set(data?.items.map((item) => item.memory_item_id))
                    : new Set(),
                )}
              />{' '}
              全选当前页
            </label>
            <span>{selected.size} 项已选择</span>
            <button
              className="wf-button"
              type="button"
              disabled={busy || !selected.size}
              onClick={() => void archiveSelected().catch(() => undefined)}
            >
              批量归档
            </button>
            <button
              className="wf-button"
              type="button"
              disabled={busy || selected.size < 2}
              onClick={() => setMergeOpen(true)}
            >
              合并
            </button>
            <button
              className="wf-button"
              type="button"
              disabled={busy || !selected.size}
              onClick={() => void retryIndex(selectedItems).catch(() => undefined)}
            >
              索引重试
            </button>
          </div>

          {!activeOwnerUserId ? (
            <DataState
              state={identitiesQuery.isInitialLoading ? 'loading' : 'empty'}
              title={identitiesQuery.isInitialLoading ? '正在加载' : '请选择具体 owner'}
              message="对象列表不会执行跨 owner 查询"
            />
          ) : listQuery.isInitialLoading ? (
            <DataState state="loading" title="正在加载" message="记忆对象" />
          ) : !data && listQuery.error ? (
            <DataState state="error" title="加载失败" message={errorMessage(listQuery.error)} />
          ) : !data?.items.length ? (
            <DataState state="empty" title="暂无记忆对象" message="调整筛选条件或新建对象" />
          ) : (
            <div className="memory-object-list">
              {data.items.map((item) => (
                <article key={item.memory_item_id} className="memory-object-row">
                  <label className="memory-object-select">
                    <input
                      aria-label={`选择 ${item.memory_item_id}`}
                      type="checkbox"
                      checked={selected.has(item.memory_item_id)}
                      onChange={(event) => setSelected((current) => {
                        const next = new Set(current);
                        if (event.target.checked) next.add(item.memory_item_id);
                        else next.delete(item.memory_item_id);
                        return next;
                      })}
                    />
                  </label>
                  <button
                    className="memory-object-content"
                    type="button"
                    onClick={() => void openDetail(item)}
                  >
                    <span><code>{item.memory_item_id}</code><strong>{item.content}</strong></span>
                    <small>
                      {item.owner_display_name ?? item.owner_user_id} · {item.scope}
                      {item.persona_id ? ` · ${item.persona_id}` : ''}
                    </small>
                  </button>
                  <div className="memory-object-meta">
                    <span className="type-chip">{item.memory_type}</span>
                    <span className={`status-chip status-chip--${item.status}`}>{item.status}</span>
                    <span className={`status-chip status-chip--${item.index_status}`}>
                      {item.index_status}
                    </span>
                    <small>v{item.version} · {formatMemoryTime(item.updated_at)}</small>
                  </div>
                  <div className="memory-object-actions">
                    <button
                      className="wf-button"
                      type="button"
                      onClick={() => void openDetail(item)}
                    >
                      详情
                    </button>
                    {item.index_status === 'needs_repair' ? (
                      <button
                        className="wf-button"
                        type="button"
                        disabled={busy}
                        onClick={() => void retryIndex([item]).catch(() => undefined)}
                      >
                        重试索引
                      </button>
                    ) : null}
                  </div>
                </article>
              ))}
            </div>
          )}

          <nav className="pagination" aria-label="分页">
            <button
              className="wf-button"
              type="button"
              disabled={page <= 1 || listQuery.isInitialLoading}
              onClick={() => {
                setPage((value) => value - 1);
                setSelected(new Set());
              }}
            >
              上一页
            </button>
            <span>
              第 {page} 页 / 共 {Math.max(1, Math.ceil((data?.total ?? 0) / pageSize))} 页
            </span>
            <select
              aria-label="每页数量"
              className="wf-input"
              value={pageSize}
              onChange={(event) => {
                setPageSize(Number(event.target.value));
                setPage(1);
              }}
            >
              <option value="20">20</option>
              <option value="50">50</option>
              <option value="100">100</option>
            </select>
            <button
              className="wf-button"
              type="button"
              disabled={!data?.has_more || listQuery.isInitialLoading}
              onClick={() => {
                setPage((value) => value + 1);
                setSelected(new Set());
              }}
            >
              下一页
            </button>
          </nav>
        </>
      ) : null}

      {detail || detailLoading ? (
        <MemoryDetailDrawer
          objectDetail={detail}
          loading={detailLoading}
          allowEdit
          onClose={() => setDetail(null)}
          onObjectSaved={async (saved) => {
            setDetail(saved);
            await refreshAfterMutation();
          }}
        />
      ) : null}

      <MemoryMergeDialog
        open={mergeOpen}
        items={selectedItems}
        busy={busy}
        preview={mergePreview}
        onPreview={previewMerge}
        onPreviewInvalidated={() => setMergePreview(null)}
        onConfirm={confirmMerge}
        onClose={() => {
          if (!busy) {
            setMergeOpen(false);
            setMergePreview(null);
          }
        }}
      />
    </div>
  );
}

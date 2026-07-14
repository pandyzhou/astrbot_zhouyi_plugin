import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ConfirmDialog, DataState, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { memoryGet, memoryPost } from '../../api/client';
import { useI18n } from '../../i18n';
import { queryCache } from '../../store/queryCacheCore';
import { MEMORY_GRAPH_QUERY_PREFIX, MEMORY_LIST_QUERY_PREFIX, queryKeys, type MemoryListQueryParams } from '../../store/queryKeys';
import { useCachedQuery } from '../../store/useCachedQuery';
import { displayImportance, formatMemoryTime, MemoryDetailDrawer } from './MemoryDetailDrawer';
import type { MemoryDetail, MemoryItem, MemoryListData } from './types';

interface DeleteRequest {
  ids: number[];
  single?: MemoryDetail;
}

function errorMessage(reason: unknown) {
  return reason instanceof Error ? reason.message : String(reason ?? '');
}

export function MemoriesPage() {
  const { t } = useI18n();
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(20);
  const [keyword, setKeyword] = useState('');
  const [session, setSession] = useState('');
  const [status, setStatus] = useState('all');
  const [type, setType] = useState('all');
  const [sort, setSort] = useState('created_desc');
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [detail, setDetail] = useState<MemoryDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState('');
  const [feedbackError, setFeedbackError] = useState(false);
  const [deleteRequest, setDeleteRequest] = useState<DeleteRequest | null>(null);

  const listParams = useMemo<MemoryListQueryParams>(() => ({
    page,
    page_size: pageSize,
    keyword: keyword || undefined,
    session_id: session || undefined,
    status,
    type,
    sort,
  }), [keyword, page, pageSize, session, sort, status, type]);
  const listKey = useMemo(() => queryKeys.memoryList(listParams), [listParams]);
  const listParamsRef = useRef(listParams);
  const listKeyRef = useRef(listKey);
  listParamsRef.current = listParams;
  listKeyRef.current = listKey;

  const listQuery = useCachedQuery<MemoryListData>(
    listKey,
    () => memoryGet<MemoryListData>('memories', listParams),
  );
  const data = listQuery.data ?? null;
  const loading = listQuery.isInitialLoading;
  const error = listQuery.error;

  useEffect(() => {
    setSelected(new Set());
  }, [listKey]);

  const refreshAfterMutation = useCallback(async () => {
    queryCache.invalidate(MEMORY_LIST_QUERY_PREFIX);
    queryCache.invalidate(queryKeys.memoryOverviewStats);
    queryCache.invalidate(MEMORY_GRAPH_QUERY_PREFIX);
    setSelected(new Set());
    await queryCache.revalidate(
      listKeyRef.current,
      () => memoryGet<MemoryListData>('memories', listParamsRef.current),
    ).catch(() => undefined);
  }, []);

  const openDetail = async (item: Pick<MemoryItem, 'id'>) => {
    setDetail(null);
    setDetailLoading(true);
    setFeedback('');
    try {
      setDetail(await memoryGet<MemoryDetail>('memories/detail', { memory_id: item.id }));
    } catch (reason) {
      setFeedbackError(true);
      setFeedback((reason as Error).message);
      setDetailLoading(false);
    } finally {
      setDetailLoading(false);
    }
  };

  const archiveSelected = async () => {
    const ids = [...selected];
    if (!ids.length) return;
    setBusy(true);
    setFeedback('');
    try {
      await memoryPost('memories/batch-update', { memory_ids: ids, field: 'status', value: 'archived' }, undefined, `memory-archive:${ids.join(',')}`);
      await refreshAfterMutation();
      setFeedbackError(false);
      setFeedback(t('operationDone'));
    } catch (reason) {
      setFeedbackError(true);
      setFeedback((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const deleteMemories = async (request: DeleteRequest) => {
    setBusy(true);
    setFeedback('');
    try {
      await memoryPost('memories/batch-delete', { memory_ids: request.ids }, undefined, `memory-delete:${request.ids.join(',')}`);
      if (detail && request.ids.includes(detail.memory_id)) setDetail(null);
      await refreshAfterMutation();
      setFeedbackError(false);
      setFeedback(t('operationDone'));
      setDeleteRequest(null);
    } catch (reason) {
      setFeedbackError(true);
      setFeedback((reason as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const reloadDetail = async (memoryId: number) => {
    const [nextDetail] = await Promise.all([
      memoryGet<MemoryDetail>('memories/detail', { memory_id: memoryId }),
      refreshAfterMutation(),
    ]);
    setDetail(nextDetail);
    setFeedbackError(false);
    setFeedback(t('operationDone'));
  };

  const allSelected = useMemo(
    () => Boolean(data?.items.length) && data!.items.every((item) => selected.has(item.id)),
    [data, selected],
  );

  return (
    <div className="page-stack">
      <header className="page-heading">
        <div><p className="eyebrow">MEMORY INDEX</p><h1>{t('memories')}</h1></div>
        <button className="wf-button" type="button" disabled={loading} onClick={() => { void listQuery.refresh().catch(() => undefined); }}>{t('refresh')}</button>
      </header>
      <WorkshopPanel title={t('filters')}>
        <form className="memory-filter-grid" onSubmit={(event) => { event.preventDefault(); setPage(1); }}>
          <label className="wf-label">{t('keyword')}<input className="wf-input" value={keyword} onChange={(event) => setKeyword(event.target.value)} /></label>
          <label className="wf-label">{t('session')}<input className="wf-input" value={session} onChange={(event) => setSession(event.target.value)} /></label>
          <label className="wf-label">{t('status')}<select className="wf-input" value={status} onChange={(event) => { setStatus(event.target.value); setPage(1); }}><option value="all">{t('all')}</option><option value="active">{t('active')}</option><option value="archived">{t('archived')}</option><option value="deleted">{t('deleted')}</option></select></label>
          <label className="wf-label">{t('type')}<input className="wf-input" value={type} onChange={(event) => { setType(event.target.value || 'all'); setPage(1); }} placeholder="all / FACT" /></label>
          <label className="wf-label">{t('sort')}<select className="wf-input" value={sort} onChange={(event) => { setSort(event.target.value); setPage(1); }}><option value="created_desc">{t('created')} ↓</option><option value="updated_desc">{t('updated')} ↓</option><option value="importance_desc">{t('importance')} ↓</option><option value="importance_asc">{t('importance')} ↑</option><option value="type_asc">{t('type')} A–Z</option></select></label>
          <label className="wf-label">{t('pageSize')}<select className="wf-input" value={pageSize} onChange={(event) => { setPageSize(Number(event.target.value)); setPage(1); }}><option>20</option><option>50</option><option>100</option></select></label>
          <button className="wf-button wf-button--primary" type="submit" disabled={loading}>{t('search')}</button>
        </form>
      </WorkshopPanel>
      {feedback ? <p className={`inline-feedback${feedbackError ? ' inline-feedback--error' : ''}`} role={feedbackError ? 'alert' : 'status'}>{feedback}</p> : null}
      {data && error ? <p className="inline-feedback inline-feedback--error" role="alert">{errorMessage(error)}</p> : null}
      <div className="batch-toolbar">
        <label><input aria-label={t('selectAll')} type="checkbox" checked={allSelected} onChange={(event) => setSelected(event.target.checked ? new Set(data?.items.map((item) => item.id)) : new Set())} /> {t('selectAll')}</label>
        <span>{selected.size} {t('selected')}</span>
        <button className="wf-button" type="button" disabled={busy || !selected.size} onClick={() => void archiveSelected()}>{t('archive')}</button>
        <button className="wf-button wf-button--danger" type="button" disabled={busy || !selected.size} onClick={() => setDeleteRequest({ ids: [...selected] })}>{busy ? t('processing') : t('delete')}</button>
      </div>
      {loading ? <DataState state="loading" title={t('loading')} message={t('memoryIndex')} /> : !data && error ? <DataState state="error" title={t('operationFailed')} message={errorMessage(error)} action={<button className="wf-button" type="button" onClick={() => void listQuery.refresh().catch(() => undefined)}>{t('retry')}</button>} /> : !data?.items.length ? <DataState state="empty" title={t('empty')} message={t('memoryIndex')} /> : (
        <div className="memory-table-wrap">
          <table className="memory-table">
            <thead><tr><th aria-label={t('select')} /><th>ID</th><th>{t('content')}</th><th>{t('type')}</th><th>{t('importance')}</th><th>{t('status')}</th><th>{t('created')}</th></tr></thead>
            <tbody>{data.items.map((item) => (
              <tr key={item.id}>
                <td><input aria-label={`${t('select')} ${item.id}`} type="checkbox" checked={selected.has(item.id)} onChange={(event) => setSelected((current) => { const next = new Set(current); if (event.target.checked) next.add(item.id); else next.delete(item.id); return next; })} /></td>
                <td><button className="link-button" type="button" onClick={() => void openDetail(item)}>#{item.id}</button></td>
                <td><button className="memory-content-button" type="button" onClick={() => void openDetail(item)}>{item.text}</button></td>
                <td><span className="type-chip">{item.metadata?.memory_type ?? 'GENERAL'}</span></td>
                <td>{displayImportance(item.metadata?.importance).toFixed(1)}</td>
                <td><span className={`status-chip status-chip--${item.metadata?.status ?? 'active'}`}>{item.metadata?.status ?? 'active'}</span></td>
                <td>{formatMemoryTime(item.metadata?.create_time ?? item.created_at)}</td>
              </tr>
            ))}</tbody>
          </table>
        </div>
      )}
      <nav className="pagination" aria-label={t('pagination')}>
        <button className="wf-button" type="button" disabled={page <= 1 || loading} onClick={() => setPage((value) => value - 1)}>{t('previous')}</button>
        <span>{t('page')} {page} / {Math.max(1, Math.ceil((data?.total ?? 0) / pageSize))}</span>
        <button className="wf-button" type="button" disabled={!data?.has_more || loading} onClick={() => setPage((value) => value + 1)}>{t('next')}</button>
      </nav>
      {(detail || detailLoading) ? <MemoryDetailDrawer detail={detail} loading={detailLoading} allowEdit allowDelete deleting={busy && Boolean(deleteRequest?.single)} onClose={() => setDetail(null)} onSaved={reloadDetail} onRequestDelete={(value) => setDeleteRequest({ ids: [value.memory_id], single: value })} /> : null}
      <ConfirmDialog
        open={Boolean(deleteRequest)}
        title={deleteRequest?.single ? t('deleteMemoryTitle') : t('deleteSelectedTitle')}
        description={deleteRequest?.single ? `${t('deleteMemoryDescription')} #${deleteRequest.single.memory_id}` : `${t('deleteSelectedDescription')} ${deleteRequest?.ids.length ?? 0}`}
        confirmLabel={t('confirmDelete')}
        cancelLabel={t('cancel')}
        danger
        busy={busy}
        busyLabel={t('processing')}
        onClose={() => { if (!busy) setDeleteRequest(null); }}
        onConfirm={() => { if (deleteRequest) void deleteMemories(deleteRequest); }}
      />
    </div>
  );
}

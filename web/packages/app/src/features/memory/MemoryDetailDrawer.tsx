import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { DataState } from '@pandyzhou/astrbot-mc-ui';
import { ApiClientError, memoryAdminClient } from '../../api/client';
import { useI18n } from '../../i18n';
import { queryKeys } from '../../store/queryKeys';
import { useCachedQuery } from '../../store/useCachedQuery';
import { MemoryObjectEditor } from './MemoryObjectEditor';
import {
  preserveDraftOnRevisionConflict,
  type MemoryObjectDraft,
  type RevisionConflictState,
} from './memoryAdminState';
import { MemoryRevisionTimeline } from './MemoryRevisionTimeline';
import { MemorySourceMessages } from './MemorySourceMessages';
import type {
  MemoryDetail,
  MemoryObjectDetailData,
  MemoryObjectUpdateInput,
  MemoryRevision,
} from './types';

export function displayImportance(value: unknown) {
  const numeric = Number(value ?? 0.5);
  if (!Number.isFinite(numeric)) return 5;
  return Math.max(0, Math.min(10, numeric <= 1 ? numeric * 10 : numeric));
}

export function formatMemoryTime(value: unknown) {
  if (value === undefined || value === null || value === '') return '—';
  const numeric = Number(value);
  const date = Number.isFinite(numeric)
    ? new Date(numeric < 1e12 ? numeric * 1000 : numeric)
    : new Date(String(value));
  return Number.isNaN(date.valueOf()) ? String(value) : date.toLocaleString();
}

interface MemoryDetailDrawerProps {
  detail?: MemoryDetail | null;
  objectDetail?: MemoryObjectDetailData | null;
  loading?: boolean;
  allowEdit?: boolean;
  scoreBreakdown?: Record<string, number>;
  onClose: () => void;
  onObjectSaved?: (detail: MemoryObjectDetailData) => Promise<void> | void;
}

type ObjectTab = 'current' | 'revisions' | 'sources' | 'relations' | 'index';

const objectTabLabels: Record<ObjectTab, string> = {
  current: '当前内容',
  revisions: 'Revision 历史',
  sources: '来源消息',
  relations: '关系与冲突',
  index: '索引状态',
};

export function MemoryDetailDrawer({
  detail = null,
  objectDetail = null,
  loading = false,
  allowEdit = false,
  scoreBreakdown,
  onClose,
  onObjectSaved,
}: MemoryDetailDrawerProps) {
  const { t } = useI18n();
  const drawerRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const busyRef = useRef(false);
  const onCloseRef = useRef(onClose);
  const [tab, setTab] = useState<ObjectTab>('current');
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [selectedRevision, setSelectedRevision] = useState<number | undefined>();
  const [conflict, setConflict] = useState<RevisionConflictState | null>(null);
  const [currentObjectDetail, setCurrentObjectDetail] = useState<MemoryObjectDetailData | null>(objectDetail);
  const ownerUserId = currentObjectDetail?.item.owner_user_id ?? objectDetail?.item.owner_user_id ?? '';
  const objectId = currentObjectDetail?.item.memory_item_id ?? objectDetail?.item.memory_item_id ?? '';
  const revisionsQuery = useCachedQuery(
    queryKeys.memoryObjectRevisions(ownerUserId, objectId),
    () => memoryAdminClient.revisions(ownerUserId, objectId),
    { enabled: Boolean(ownerUserId && objectId) },
  );
  const sourcesQuery = useCachedQuery(
    queryKeys.memoryObjectSources(ownerUserId, objectId, selectedRevision),
    () => memoryAdminClient.sources(ownerUserId, objectId, selectedRevision),
    { enabled: Boolean(ownerUserId && objectId) },
  );
  const busy = saving;
  busyRef.current = busy;
  onCloseRef.current = onClose;

  useEffect(() => {
    setCurrentObjectDetail(objectDetail);
    setTab('current');
    setEditing(false);
    setError('');
    setConflict(null);
    setSelectedRevision(undefined);
  }, [objectDetail]);

  useEffect(() => {
    previousFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const previousBodyOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    const timer = window.setTimeout(() => closeButtonRef.current?.focus(), 0);
    const onKeyDown = (event: KeyboardEvent) => {
      const openDialog = document.querySelector<HTMLDialogElement>('dialog[open]');
      if (openDialog?.contains(document.activeElement)) return;
      if (event.key === 'Escape' && !busyRef.current) {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab' || !drawerRef.current?.contains(document.activeElement)) return;
      const focusable = [
        ...drawerRef.current.querySelectorAll<HTMLElement>(
          'button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), summary, [href], [tabindex]:not([tabindex="-1"])',
        ),
      ];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable.at(-1)!;
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener('keydown', onKeyDown);
    return () => {
      window.clearTimeout(timer);
      document.removeEventListener('keydown', onKeyDown);
      document.body.style.overflow = previousBodyOverflow;
      previousFocusRef.current?.focus();
    };
  }, []);

  const saveObject = async (payload: MemoryObjectUpdateInput, draft: MemoryObjectDraft) => {
    if (!currentObjectDetail) return;
    setSaving(true);
    setError('');
    try {
      const saved = await memoryAdminClient.updateObject(payload);
      setCurrentObjectDetail(saved);
      setEditing(false);
      setConflict(null);
      await Promise.allSettled([revisionsQuery.refresh(), sourcesQuery.refresh()]);
      await onObjectSaved?.(saved);
    } catch (reason) {
      if (
        reason instanceof ApiClientError
        && ['MEMORY_REVISION_CONFLICT', 'REVISION_CONFLICT', 'VERSION_CONFLICT'].includes(reason.code)
      ) {
        try {
          const current = currentObjectDetail.item;
          const latest = await memoryAdminClient.objectDetail(
            current.owner_user_id,
            current.memory_item_id,
          );
          setConflict(preserveDraftOnRevisionConflict(draft, current.version, latest.item));
          setError('对象已被其他操作更新。草稿已保留，请加载最新版本后重新审查并提交。');
        } catch (reloadError) {
          setError(reloadError instanceof Error ? reloadError.message : String(reloadError));
        }
      } else {
        setError(reason instanceof Error ? reason.message : String(reason));
      }
    } finally {
      setSaving(false);
    }
  };

  const object = currentObjectDetail?.item;
  const titleId = 'memory-detail-title';

  return createPortal(
    <div
      className="drawer-backdrop"
      onMouseDown={(event) => {
        if (event.target === event.currentTarget && !busy) onClose();
      }}
    >
      <section
        ref={drawerRef}
        className="memory-drawer"
        role="dialog"
        aria-modal="true"
        aria-labelledby="memory-detail-title"
        aria-busy={busy}
      >
        <header className="memory-drawer-header">
          <div className="memory-drawer-heading">
            <h2 id={titleId}>
              {object
                ? `记忆对象 ${object.memory_item_id}`
                : `${t('details')} ${detail ? `#${detail.memory_id}` : ''}`}
            </h2>
            {object ? (
              <dl className="memory-drawer-summary">
                <div><dt>Owner</dt><dd>{object.owner_display_name ?? object.owner_user_id}</dd></div>
                <div><dt>Scope</dt><dd>{object.scope}</dd></div>
                <div><dt>版本</dt><dd>v{object.version} / r{object.current_revision_no}</dd></div>
                <div>
                  <dt>{t('status')}</dt>
                  <dd><span className={`status-chip status-chip--${object.status}`}>{object.status}</span></dd>
                </div>
              </dl>
            ) : null}
          </div>
          <button
            ref={closeButtonRef}
            className="wf-button memory-drawer-close"
            type="button"
            disabled={busy}
            onClick={onClose}
          >
            {t('close')}
          </button>
        </header>

        <div className="memory-drawer-body">
          {loading || (!detail && !object) ? (
            <DataState state="loading" title={t('loading')} message={t('memoryDetail')} />
          ) : object && currentObjectDetail ? (
            <div className="detail-stack">
              {error ? (
                <p className="inline-feedback inline-feedback--error" role="alert">
                  {error}
                  {conflict ? (
                    <button
                      className="wf-button"
                      type="button"
                      onClick={() => {
                        setCurrentObjectDetail((current) => (
                          current ? { ...current, item: conflict.latest } : current
                        ));
                        setError('已加载最新版本，草稿仍保留。请重新审查后保存。');
                      }}
                    >
                      加载最新版本 v{conflict.latest.version}
                    </button>
                  ) : null}
                </p>
              ) : null}

              {editing ? (
                <MemoryObjectEditor
                  key={`${object.memory_item_id}:${object.version}`}
                  item={object}
                  preservedDraft={conflict?.draft ?? null}
                  submitLabel="保存新 revision"
                  busy={saving}
                  onCancel={() => setEditing(false)}
                  onSubmit={(payload, draft) => saveObject(payload as MemoryObjectUpdateInput, draft)}
                />
              ) : (
                <>
                  <nav className="memory-detail-tabs" aria-label="记忆对象详情标签">
                    {(Object.keys(objectTabLabels) as ObjectTab[]).map((value) => (
                      <button
                        key={value}
                        type="button"
                        aria-pressed={tab === value}
                        onClick={() => setTab(value)}
                      >
                        {objectTabLabels[value]}
                      </button>
                    ))}
                  </nav>

                  {tab === 'current' ? (
                    <div className="memory-detail-layout">
                      <article className="memory-detail-main">
                        <DetailSection title="当前内容">
                          <div className="memory-detail-content">{object.content}</div>
                        </DetailSection>
                        {object.structured_payload ? (
                          <DetailSection title="结构化内容">
                            <pre className="memory-json">
                              {JSON.stringify(object.structured_payload, null, 2)}
                            </pre>
                          </DetailSection>
                        ) : null}
                      </article>
                      <aside className="memory-detail-sidebar">
                        <DetailSection title={t('details')}>
                          <dl className="detail-grid">
                            <div><dt>稳定 ID</dt><dd>{object.memory_item_id}</dd></div>
                            <div><dt>Owner</dt><dd>{object.owner_user_id}</dd></div>
                            <div><dt>Scope</dt><dd>{object.scope}</dd></div>
                            <div><dt>{t('persona')}</dt><dd>{object.persona_id ?? '—'}</dd></div>
                            <div><dt>{t('session')}</dt><dd>{object.session_id ?? '—'}</dd></div>
                            <div><dt>{t('type')}</dt><dd>{object.memory_type}</dd></div>
                            <div><dt>{t('importance')}</dt><dd>{object.importance.toFixed(2)}</dd></div>
                            <div><dt>置信度</dt><dd>{object.confidence.toFixed(2)}</dd></div>
                            <div><dt>群聊安全</dt><dd>{object.group_safe ? '是' : '否'}</dd></div>
                            <div><dt>{t('updated')}</dt><dd>{formatMemoryTime(object.updated_at)}</dd></div>
                          </dl>
                        </DetailSection>
                      </aside>
                    </div>
                  ) : null}

                  {tab === 'revisions' ? (
                    revisionsQuery.isInitialLoading ? (
                      <DataState state="loading" title="正在加载" message="Revision 历史" />
                    ) : (
                      <MemoryRevisionTimeline
                        revisions={revisionsQuery.data ?? []}
                        selected={selectedRevision}
                        onSelect={(revision: MemoryRevision) => {
                          setSelectedRevision(revision.revision_no);
                          setTab('sources');
                        }}
                      />
                    )
                  ) : null}

                  {tab === 'sources' ? (
                    sourcesQuery.isInitialLoading ? (
                      <DataState state="loading" title="正在加载" message="来源消息" />
                    ) : (
                      <>
                        <div className="source-filter">
                          <span>{selectedRevision ? `Revision ${selectedRevision}` : '全部 revision'}</span>
                          {selectedRevision ? (
                            <button
                              className="wf-button"
                              type="button"
                              onClick={() => setSelectedRevision(undefined)}
                            >
                              查看全部
                            </button>
                          ) : null}
                        </div>
                        <MemorySourceMessages sources={sourcesQuery.data ?? []} />
                      </>
                    )
                  ) : null}

                  {tab === 'relations' ? (
                    <div className="relation-conflict-grid">
                      <DetailSection title="对象关系">
                        {currentObjectDetail.relations.length ? (
                          <ul className="relation-list">
                            {currentObjectDetail.relations.map((relation) => (
                              <li key={relation.relation_id}>
                                <span className="type-chip">{relation.relation_type}</span>
                                <code>{relation.target_memory_item_id}</code>
                                <p>{relation.target_content ?? '—'}</p>
                              </li>
                            ))}
                          </ul>
                        ) : <p className="muted">暂无关系</p>}
                      </DetailSection>
                      <DetailSection title="对象冲突">
                        {currentObjectDetail.conflicts.length ? (
                          <ul className="relation-list">
                            {currentObjectDetail.conflicts.map((item) => (
                              <li key={item.conflict_id}>
                                <span className={`status-chip status-chip--${item.severity}`}>{item.severity}</span>
                                <strong>{item.conflict_type}</strong>
                                <p>{item.left_item.content} / {item.right_item.content}</p>
                              </li>
                            ))}
                          </ul>
                        ) : <p className="muted">暂无冲突</p>}
                      </DetailSection>
                    </div>
                  ) : null}

                  {tab === 'index' ? (
                    <div className="index-status-panel">
                      <span className={`status-chip status-chip--${object.index_status}`}>
                        {object.index_status}
                      </span>
                      <dl className="detail-grid">
                        <div><dt>当前文档投影</dt><dd>{object.current_document_id ?? '—'}</dd></div>
                        <div><dt>来源数量</dt><dd>{object.source_count}</dd></div>
                        <div><dt>关系数量</dt><dd>{object.relation_count}</dd></div>
                        <div><dt>冲突数量</dt><dd>{object.conflict_count}</dd></div>
                      </dl>
                    </div>
                  ) : null}
                </>
              )}
            </div>
          ) : detail ? (
            <LegacyDetail detail={detail} scoreBreakdown={scoreBreakdown} />
          ) : null}
        </div>

        {object && allowEdit && !editing ? (
          <footer className="memory-drawer-actions">
            <button
              className="wf-button wf-button--primary"
              type="button"
              disabled={busy}
              onClick={() => setEditing(true)}
            >
              {t('edit')}
            </button>
          </footer>
        ) : null}
      </section>
    </div>,
    document.body,
  );
}

function LegacyDetail({
  detail,
  scoreBreakdown,
}: {
  detail: MemoryDetail;
  scoreBreakdown?: Record<string, number>;
}) {
  const { t } = useI18n();
  const metadata = detail.metadata ?? {};
  const history = detail.update_history ?? metadata.update_history ?? [];
  const graph = detail.graph_context;

  return (
    <div className="memory-detail-layout">
      <article className="memory-detail-main">
        <DetailSection title={t('content')}>
          <div className="memory-detail-content">{detail.text}</div>
        </DetailSection>
        {scoreBreakdown ? (
          <DetailSection title={t('scoreBreakdown')}>
            <dl className="score-breakdown">
              {Object.entries(scoreBreakdown).map(([key, value]) => (
                <div key={key}><dt>{key}</dt><dd>{Number(value).toFixed(6)}</dd></div>
              ))}
            </dl>
          </DetailSection>
        ) : null}
      </article>
      <aside className="memory-detail-sidebar">
        <DetailSection title={t('details')}>
          <dl className="detail-grid">
            <div><dt>{t('type')}</dt><dd>{detail.memory_type ?? metadata.memory_type ?? 'GENERAL'}</dd></div>
            <div><dt>{t('status')}</dt><dd>{detail.status ?? metadata.status ?? 'active'}</dd></div>
            <div><dt>{t('importance')}</dt><dd>{displayImportance(detail.importance ?? metadata.importance).toFixed(1)}</dd></div>
            <div><dt>{t('session')}</dt><dd>{detail.session_id ?? metadata.session_id ?? '—'}</dd></div>
            <div><dt>{t('persona')}</dt><dd>{detail.persona_id ?? metadata.persona_id ?? '—'}</dd></div>
            <div><dt>{t('created')}</dt><dd>{formatMemoryTime(detail.create_time ?? metadata.create_time ?? detail.created_at)}</dd></div>
          </dl>
        </DetailSection>
        <details className="memory-detail-disclosure">
          <summary>{t('updateHistory')}</summary>
          <div className="memory-detail-disclosure-content">
            {history.length ? (
              <ol className="history-list">
                {[...history].reverse().map((item, index) => (
                  <li key={`${String(item.timestamp)}-${index}`}>
                    <strong>{item.description || item.field || t('updated')}</strong>
                    <time>{formatMemoryTime(item.timestamp)}</time>
                    {item.description ? null : (
                      <p>{displayValue(item.old_value)} → {displayValue(item.new_value)}</p>
                    )}
                    {item.reason ? <small>{t('reason')}: {item.reason}</small> : null}
                  </li>
                ))}
              </ol>
            ) : <p className="muted">{t('empty')}</p>}
          </div>
        </details>
        <details className="memory-detail-disclosure">
          <summary>{t('graphContext')}</summary>
          <div className="memory-detail-disclosure-content">
            {!graph ? <p className="muted">{t('empty')}</p> : (
              <div className="graph-context">
                <p>
                  {graph.nodes.length} {t('nodes')} · {graph.edges.length} {t('edges')} · {graph.entries.length} {t('entries')}
                </p>
                {graph.entries.length ? (
                  <ul className="context-entry-list">
                    {graph.entries.map((entry) => (
                      <li key={entry.id}>
                        <strong>{entry.entry_type ?? t('entry')}</strong>
                        <span>{entry.content || '—'}</span>
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            )}
          </div>
        </details>
      </aside>
    </div>
  );
}

function displayValue(value: unknown) {
  if (value === undefined || value === null || value === '') return '—';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

function DetailSection({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="detail-section"><h3>{title}</h3>{children}</section>;
}

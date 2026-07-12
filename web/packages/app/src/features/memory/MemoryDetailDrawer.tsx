import { useEffect, useRef, useState } from 'react';
import { DataState } from '@pandyzhou/astrbot-mc-ui';
import { memoryPost } from '../../api/client';
import { useI18n } from '../../i18n';
import type { MemoryDetail } from './types';

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

function displayValue(value: unknown) {
  if (value === undefined || value === null || value === '') return '—';
  if (typeof value === 'string') return value;
  try {
    return JSON.stringify(value, null, 2);
  } catch {
    return String(value);
  }
}

interface MemoryDetailDrawerProps {
  detail: MemoryDetail | null;
  loading?: boolean;
  allowEdit?: boolean;
  allowDelete?: boolean;
  deleting?: boolean;
  scoreBreakdown?: Record<string, number>;
  onClose: () => void;
  onSaved?: (memoryId: number) => Promise<void> | void;
  onRequestDelete?: (detail: MemoryDetail) => void;
}

export function MemoryDetailDrawer({
  detail,
  loading = false,
  allowEdit = false,
  allowDelete = false,
  deleting = false,
  scoreBreakdown,
  onClose,
  onSaved,
  onRequestDelete,
}: MemoryDetailDrawerProps) {
  const { t } = useI18n();
  const drawerRef = useRef<HTMLElement>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const previousFocusRef = useRef<HTMLElement | null>(null);
  const busyRef = useRef(false);
  const onCloseRef = useRef(onClose);
  const [editing, setEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [content, setContent] = useState('');
  const [importance, setImportance] = useState('5');
  const [type, setType] = useState('GENERAL');
  const [status, setStatus] = useState('active');
  const [reason, setReason] = useState('');
  const busy = saving || deleting;
  busyRef.current = busy;
  onCloseRef.current = onClose;

  useEffect(() => {
    if (!detail) return;
    setEditing(false);
    setError('');
    setContent(detail.text ?? '');
    setImportance(displayImportance(detail.importance ?? detail.metadata?.importance).toFixed(1));
    setType(String(detail.memory_type ?? detail.metadata?.memory_type ?? 'GENERAL'));
    setStatus(String(detail.status ?? detail.metadata?.status ?? 'active'));
    setReason('');
  }, [detail]);

  useEffect(() => {
    previousFocusRef.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const focusTimer = window.setTimeout(() => closeButtonRef.current?.focus(), 0);
    const onKeyDown = (event: KeyboardEvent) => {
      const openDialog = document.querySelector<HTMLDialogElement>('dialog[open]');
      if (openDialog && openDialog.contains(document.activeElement)) return;
      if (event.key === 'Escape' && !busyRef.current) {
        event.preventDefault();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab' || !drawerRef.current || !drawerRef.current.contains(document.activeElement)) return;
      const focusable = [...drawerRef.current.querySelectorAll<HTMLElement>('button:not(:disabled), input:not(:disabled), select:not(:disabled), textarea:not(:disabled), [href], [tabindex]:not([tabindex="-1"])')];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
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
      window.clearTimeout(focusTimer);
      document.removeEventListener('keydown', onKeyDown);
      previousFocusRef.current?.focus();
    };
  }, []);

  const save = async () => {
    if (!detail) return;
    const memoryId = detail.memory_id;
    setSaving(true);
    setError('');
    try {
      if (Number(importance) !== displayImportance(detail.importance ?? detail.metadata?.importance)) {
        await memoryPost('memories/update', { memory_id: memoryId, field: 'importance', value: Number(importance), value_scale: 'display', reason }, undefined, `memory-importance:${memoryId}`);
      }
      if (type !== String(detail.memory_type ?? detail.metadata?.memory_type ?? 'GENERAL')) {
        await memoryPost('memories/update', { memory_id: memoryId, field: 'type', value: type, reason }, undefined, `memory-type:${memoryId}`);
      }
      if (status !== String(detail.status ?? detail.metadata?.status ?? 'active')) {
        await memoryPost('memories/update', { memory_id: memoryId, field: 'status', value: status, reason }, undefined, `memory-status:${memoryId}`);
      }
      let nextId = memoryId;
      if (content.trim() !== detail.text) {
        const result = await memoryPost<{ new_memory_id?: number }>('memories/update', { memory_id: memoryId, field: 'content', value: content.trim(), reason }, undefined, `memory-content:${memoryId}`);
        nextId = result.new_memory_id ?? memoryId;
      }
      setEditing(false);
      await onSaved?.(nextId);
    } catch (reasonValue) {
      setError((reasonValue as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const metadata = detail?.metadata ?? {};
  const keyFacts = detail?.key_facts ?? metadata.key_facts ?? [];
  const topics = detail?.topics ?? metadata.topics ?? [];
  const history = detail?.update_history ?? metadata.update_history ?? [];
  const graph = detail?.graph_context;

  return (
    <div className="drawer-backdrop" onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose(); }}>
      <aside ref={drawerRef} className="memory-drawer" role="dialog" aria-modal="true" aria-labelledby="memory-detail-title" aria-busy={busy}>
        <header>
          <h2 id="memory-detail-title">{t('details')} {detail ? `#${detail.memory_id}` : ''}</h2>
          <button ref={closeButtonRef} className="wf-button" type="button" disabled={busy} onClick={onClose}>{t('close')}</button>
        </header>
        {loading || !detail ? <DataState state="loading" title={t('loading')} message={t('memoryDetail')} /> : editing ? (
          <form className="drawer-form" onSubmit={(event) => { event.preventDefault(); void save(); }}>
            {error ? <p className="inline-feedback inline-feedback--error" role="alert">{error}</p> : null}
            <label className="wf-label">{t('content')}<textarea className="wf-input" rows={8} value={content} required onChange={(event) => setContent(event.target.value)} /></label>
            <label className="wf-label">{t('importance')} (0–10)<input className="wf-input" type="number" min="0" max="10" step="0.1" value={importance} onChange={(event) => setImportance(event.target.value)} /></label>
            <label className="wf-label">{t('type')}<input className="wf-input" value={type} onChange={(event) => setType(event.target.value)} /></label>
            <label className="wf-label">{t('status')}<select className="wf-input" value={status} onChange={(event) => setStatus(event.target.value)}><option value="active">active</option><option value="archived">archived</option><option value="deleted">deleted</option></select></label>
            <label className="wf-label">{t('reason')}<input className="wf-input" value={reason} onChange={(event) => setReason(event.target.value)} /></label>
            <div className="form-actions">
              <button className="wf-button" type="button" disabled={saving} onClick={() => setEditing(false)}>{t('cancel')}</button>
              <button className="wf-button wf-button--primary" disabled={saving || !content.trim()}>{saving ? t('saving') : t('save')}</button>
            </div>
          </form>
        ) : (
          <div className="detail-stack">
            {error ? <p className="inline-feedback inline-feedback--error" role="alert">{error}</p> : null}
            <p className="memory-detail-content">{detail.text}</p>
            <dl className="detail-grid">
              <div><dt>{t('type')}</dt><dd>{detail.memory_type ?? metadata.memory_type ?? 'GENERAL'}</dd></div>
              <div><dt>{t('status')}</dt><dd>{detail.status ?? metadata.status ?? 'active'}</dd></div>
              <div><dt>{t('importance')}</dt><dd>{displayImportance(detail.importance ?? metadata.importance).toFixed(1)}</dd></div>
              <div><dt>{t('session')}</dt><dd>{detail.session_id ?? metadata.session_id ?? '—'}</dd></div>
              <div><dt>{t('persona')}</dt><dd>{detail.persona_id ?? metadata.persona_id ?? '—'}</dd></div>
              <div><dt>{t('created')}</dt><dd>{formatMemoryTime(detail.create_time ?? metadata.create_time ?? detail.created_at)}</dd></div>
              <div><dt>{t('updated')}</dt><dd>{formatMemoryTime(detail.updated_at ?? metadata.updated_at)}</dd></div>
              <div><dt>{t('lastAccess')}</dt><dd>{formatMemoryTime(detail.last_access_time ?? metadata.last_access_time)}</dd></div>
            </dl>

            {scoreBreakdown && Object.keys(scoreBreakdown).length ? <DetailSection title={t('scoreBreakdown')}><dl className="score-breakdown">{Object.entries(scoreBreakdown).map(([key, value]) => <div key={key}><dt>{key}</dt><dd>{Number(value).toFixed(6)}</dd></div>)}</dl></DetailSection> : null}
            <DetailSection title={t('keyFacts')}><TagList items={keyFacts} empty={t('empty')} /></DetailSection>
            <DetailSection title={t('topics')}><TagList items={topics} empty={t('empty')} /></DetailSection>
            <DetailSection title={t('updateHistory')}>
              {history.length ? <ol className="history-list">{[...history].reverse().map((item, index) => <li key={`${String(item.timestamp)}-${index}`}><strong>{item.description || item.field || t('updated')}</strong><time>{formatMemoryTime(item.timestamp)}</time>{item.description ? null : <p>{displayValue(item.old_value)} → {displayValue(item.new_value)}</p>}{item.reason ? <small>{t('reason')}: {item.reason}</small> : null}</li>)}</ol> : <p className="muted">{t('empty')}</p>}
            </DetailSection>
            <DetailSection title={t('graphContext')}>
              {!graph ? <p className="muted">{t('empty')}</p> : <div className="graph-context"><p>{graph.nodes.length} {t('nodes')} · {graph.edges.length} {t('edges')} · {graph.entries.length} {t('entries')}</p><TagList items={graph.nodes.map((node) => node.label || `#${node.id}`)} empty={t('empty')} />{graph.entries.length ? <ul className="context-entry-list">{graph.entries.map((entry) => <li key={entry.id}><strong>{entry.entry_type ?? t('entry')}</strong><span>{entry.content || '—'}</span></li>)}</ul> : null}</div>}
            </DetailSection>
            {(allowEdit || allowDelete) ? <div className="form-actions drawer-danger-actions">
              {allowDelete ? <button className="wf-button wf-button--danger" type="button" disabled={busy} onClick={() => onRequestDelete?.(detail)}>{deleting ? t('deleting') : t('delete')}</button> : null}
              {allowEdit ? <button className="wf-button wf-button--primary" type="button" disabled={busy} onClick={() => setEditing(true)}>{t('edit')}</button> : null}
            </div> : null}
          </div>
        )}
      </aside>
    </div>
  );
}

function DetailSection({ title, children }: { title: string; children: React.ReactNode }) {
  return <section className="detail-section"><h3>{title}</h3>{children}</section>;
}

function TagList({ items, empty }: { items: string[]; empty: string }) {
  return items.length ? <ul className="tag-list">{items.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}</ul> : <p className="muted">{empty}</p>;
}

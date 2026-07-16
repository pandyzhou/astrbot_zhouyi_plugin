import { useState } from 'react';
import type { MemoryObjectDraft } from './memoryAdminState';
import { createPayloadFromDraft, draftFromObject, updatePayloadFromDraft, validateMemoryScope } from './memoryAdminState';
import type { MemoryObject, MemoryObjectMutationInput, MemoryObjectUpdateInput } from './types';

interface Props {
  item?: MemoryObject | null;
  owners?: Array<{ owner_user_id: string; display_name: string }>;
  submitLabel: string;
  busy?: boolean;
  preservedDraft?: MemoryObjectDraft | null;
  onCancel?: () => void;
  onSubmit: (payload: MemoryObjectMutationInput | MemoryObjectUpdateInput, draft: MemoryObjectDraft) => Promise<void> | void;
}

export function MemoryObjectEditor({ item, owners = [], submitLabel, busy = false, preservedDraft, onCancel, onSubmit }: Props) {
  const [draft, setDraft] = useState<MemoryObjectDraft>(() => preservedDraft ?? draftFromObject(item));
  const [error, setError] = useState('');
  const update = <K extends keyof MemoryObjectDraft>(key: K, value: MemoryObjectDraft[K]) => setDraft((current) => ({ ...current, [key]: value }));
  const submit = async () => {
    const validation = validateMemoryScope(draft, item);
    if (validation) { setError(validation); return; }
    setError('');
    await onSubmit(item ? updatePayloadFromDraft(item, draft) : createPayloadFromDraft(draft), draft);
  };
  return (
    <form className="memory-object-editor" onSubmit={(event) => { event.preventDefault(); void submit(); }}>
      {error ? <p className="inline-feedback inline-feedback--error" role="alert">{error}</p> : null}
      <div className="memory-object-editor-grid">
        <label className="wf-label">Owner
          {owners.length ? <select className="wf-input" value={draft.owner_user_id} disabled={Boolean(item)} onChange={(event) => update('owner_user_id', event.target.value)} required><option value="">选择 owner</option>{owners.map((owner) => <option key={owner.owner_user_id} value={owner.owner_user_id}>{owner.display_name} · {owner.owner_user_id}</option>)}</select> : <input className="wf-input" value={draft.owner_user_id} disabled={Boolean(item)} onChange={(event) => update('owner_user_id', event.target.value)} required />}
        </label>
        <label className="wf-label">Scope<select className="wf-input" value={draft.scope} onChange={(event) => update('scope', event.target.value as MemoryObjectDraft['scope'])}><option value="user">user</option><option value="persona">persona</option><option value="session">session</option><option value="public">public</option><option value="legacy_session">legacy_session</option></select></label>
        <label className="wf-label">Persona ID<input className="wf-input" value={draft.persona_id} onChange={(event) => update('persona_id', event.target.value)} /></label>
        <label className="wf-label">Session ID<input className="wf-input" value={draft.session_id} onChange={(event) => update('session_id', event.target.value)} /></label>
        <label className="wf-label">类型<input className="wf-input" value={draft.memory_type} onChange={(event) => update('memory_type', event.target.value)} required /></label>
        <label className="wf-label">Canonical key<input className="wf-input" value={draft.canonical_key} onChange={(event) => update('canonical_key', event.target.value)} /></label>
        <label className="wf-label">重要性 (0–1)<input className="wf-input" type="number" min="0" max="1" step="0.05" value={draft.importance} onChange={(event) => update('importance', event.target.value)} /></label>
        <label className="wf-label">置信度 (0–1)<input className="wf-input" type="number" min="0" max="1" step="0.05" value={draft.confidence} onChange={(event) => update('confidence', event.target.value)} /></label>
        <label className="wf-label memory-object-editor-content">内容<textarea className="wf-input" rows={10} value={draft.content} onChange={(event) => update('content', event.target.value)} required /></label>
        <label className="wf-label memory-object-editor-content">原因<input className="wf-input" value={draft.reason} onChange={(event) => update('reason', event.target.value)} /></label>
        <label className="memory-checkbox"><input type="checkbox" checked={draft.group_safe} onChange={(event) => update('group_safe', event.target.checked)} /> 允许在群聊使用</label>
      </div>
      <div className="form-actions"><button className="wf-button" type="button" disabled={busy} onClick={onCancel}>取消</button><button className="wf-button wf-button--primary" type="submit" disabled={busy || !draft.content.trim()}>{busy ? '保存中…' : submitLabel}</button></div>
    </form>
  );
}

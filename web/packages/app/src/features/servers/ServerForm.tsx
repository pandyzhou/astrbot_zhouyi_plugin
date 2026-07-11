import { useEffect, useState, type FormEvent } from 'react';
import type { ServerRecord } from '../../api/types';

export interface ServerFormValue {
  name: string;
  host: string;
  force: boolean;
}

interface ServerFormProps {
  mode: 'add' | 'edit';
  server?: ServerRecord | null;
  busy: boolean;
  onSubmit: (value: ServerFormValue) => void;
  onCancel: () => void;
}

export function ServerForm({ mode, server, busy, onSubmit, onCancel }: ServerFormProps) {
  const [name, setName] = useState('');
  const [host, setHost] = useState('');
  const [force, setForce] = useState(false);

  useEffect(() => {
    setName(server?.name ?? '');
    setHost(server?.host ?? '');
    setForce(false);
  }, [server, mode]);

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const trimmedName = name.trim();
    const trimmedHost = host.trim();
    if (!trimmedName || !trimmedHost || busy) return;
    onSubmit({ name: trimmedName, host: trimmedHost, force });
  }

  return (
    <form className="server-form" onSubmit={submit}>
      <label className="wf-label">
        名称
        <input className="wf-input" value={name} maxLength={64} required autoFocus onChange={(event) => setName(event.target.value)} />
      </label>
      <label className="wf-label">
        地址
        <input className="wf-input" value={host} maxLength={255} required placeholder="host.example.com:25565" onChange={(event) => setHost(event.target.value)} />
      </label>
      {mode === 'add' ? (
        <details className="advanced-options">
          <summary>高级选项</summary>
          <label className="check-control">
            <input type="checkbox" checked={force} onChange={(event) => setForce(event.target.checked)} />
            预查询失败时仍强制添加（force）
          </label>
        </details>
      ) : null}
      <div className="form-actions">
        <button className="wf-button" type="button" disabled={busy} onClick={onCancel}>取消</button>
        <button className="wf-button wf-button--primary" type="submit" disabled={busy || !name.trim() || !host.trim()}>
          {busy ? '保存中…' : mode === 'add' ? '添加服务器' : '保存修改'}
        </button>
      </div>
    </form>
  );
}

import { useEffect, useRef, useState } from 'react';
import type { MemoryMergePreviewData, MemoryObject } from './types';

interface Props {
  open: boolean;
  items: MemoryObject[];
  busy?: boolean;
  preview: MemoryMergePreviewData | null;
  onPreview: (survivorId: string) => Promise<void> | void;
  onPreviewInvalidated: () => void;
  onConfirm: (content: string, reason: string) => Promise<void> | void;
  onClose: () => void;
}

export function MemoryMergeDialog({
  open,
  items,
  busy = false,
  preview,
  onPreview,
  onPreviewInvalidated,
  onConfirm,
  onClose,
}: Props) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const [survivor, setSurvivor] = useState(items[0]?.memory_item_id ?? '');
  const [content, setContent] = useState('');
  const [reason, setReason] = useState('');

  useEffect(() => {
    if (open) dialogRef.current?.showModal();
    else dialogRef.current?.close();
  }, [open]);

  useEffect(() => {
    setSurvivor(items[0]?.memory_item_id ?? '');
    setContent('');
    setReason('');
  }, [items, open]);

  useEffect(() => {
    if (preview) setContent(preview.merged_content);
  }, [preview]);

  const expectedSourceIds = items
    .filter((item) => item.memory_item_id !== survivor)
    .map((item) => item.memory_item_id)
    .sort();
  const previewMatchesSelection = Boolean(
    preview
    && preview.survivor_memory_item_id === survivor
    && preview.source_memory_item_ids.length === expectedSourceIds.length
    && [...preview.source_memory_item_ids].sort().every((itemId, index) => (
      itemId === expectedSourceIds[index]
    )),
  );

  return (
    <dialog
      ref={dialogRef}
      className="memory-admin-dialog"
      onCancel={(event) => {
        if (busy) event.preventDefault();
        else onClose();
      }}
      onClose={() => {
        if (open && !busy) onClose();
      }}
    >
      <form onSubmit={(event) => {
        event.preventDefault();
        if (previewMatchesSelection) void onConfirm(content, reason);
      }}>
        <header>
          <h2>合并记忆对象</h2>
          <button className="wf-button" type="button" disabled={busy} onClick={onClose}>关闭</button>
        </header>
        <label className="wf-label">
          保留对象
          <select
            className="wf-input"
            value={survivor}
            onChange={(event) => {
              setSurvivor(event.target.value);
              setContent('');
              onPreviewInvalidated();
            }}
          >
            {items.map((item) => (
              <option key={item.memory_item_id} value={item.memory_item_id}>
                {item.memory_item_id} · {item.content.slice(0, 40)}
              </option>
            ))}
          </select>
        </label>
        <button
          className="wf-button"
          type="button"
          disabled={busy || items.length < 2}
          onClick={() => void onPreview(survivor)}
        >
          生成合并预览
        </button>
        {preview?.warnings.length ? (
          <ul className="warning-list">
            {preview.warnings.map((warning) => <li key={warning}>{warning}</li>)}
          </ul>
        ) : null}
        <label className="wf-label">
          合并后内容
          <textarea
            className="wf-input"
            rows={10}
            value={content}
            onChange={(event) => setContent(event.target.value)}
            required
          />
        </label>
        <label className="wf-label">
          原因
          <input
            className="wf-input"
            value={reason}
            onChange={(event) => setReason(event.target.value)}
          />
        </label>
        <footer>
          <button className="wf-button" type="button" disabled={busy} onClick={onClose}>取消</button>
          <button
            className="wf-button wf-button--primary"
            type="submit"
            disabled={busy || !previewMatchesSelection || !content.trim()}
          >
            {busy ? '合并中…' : '确认合并'}
          </button>
        </footer>
      </form>
    </dialog>
  );
}

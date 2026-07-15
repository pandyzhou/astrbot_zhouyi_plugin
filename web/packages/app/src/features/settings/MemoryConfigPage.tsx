import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { DataState, SelectField, SwitchField, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { ApiClientError, apiClient } from '../../api/client';
import type { MemoryConfigData, MemoryConfigRevision, MemoryConfigValue } from '../../api/types';
import { queryCache } from '../../store/queryCacheCore';
import { queryKeyPrefixes, queryKeys } from '../../store/queryKeys';
import { useCachedQuery } from '../../store/useCachedQuery';
import {
  convertMemoryConfigDraft,
  countMemoryConfigChanges,
  createMemoryConfigDraft,
  getAtPath,
  memoryConfigEquals,
  parseMemoryConfigSchema,
  pathKey,
  setAtPath,
  validateMemoryConfigDraft,
  type MemoryConfigDraft,
  type MemoryConfigField,
} from './memoryConfigSchema';

const RELOAD_INITIAL_DELAY_MS = 1_000;
const RELOAD_POLL_INTERVAL_MS = 1_000;
const RELOAD_TIMEOUT_MS = 30_000;

interface MemoryConfigPageProps {
  onNavigationLockChange?: (locked: boolean) => void;
}

type FeedbackState =
  | { kind: 'progress'; message: string }
  | { kind: 'success'; message: string }
  | { kind: 'warning'; message: string };

function messageOf(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message || fallback : fallback;
}

function isRevisionConflict(reason: unknown) {
  if (reason instanceof ApiClientError
    && (reason.code.includes('REVISION_CONFLICT') || reason.code === 'CONFLICT')) return true;
  if (!(reason instanceof Error)) return false;
  return /已被其他请求修改|revision\s+conflict|版本冲突/i.test(reason.message);
}

function delay(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) {
      reject(new DOMException('请求已取消', 'AbortError'));
      return;
    }
    const timer = window.setTimeout(resolve, ms);
    signal.addEventListener('abort', () => {
      window.clearTimeout(timer);
      reject(new DOMException('请求已取消', 'AbortError'));
    }, { once: true });
  });
}

function revisionMatches(left: MemoryConfigRevision, right: MemoryConfigRevision) {
  return memoryConfigEquals(left, right);
}

function fieldHelp(field: MemoryConfigField) {
  const parts = [field.description, field.hint];
  if (field.defaultValue !== undefined) parts.push(`默认值：${String(field.defaultValue)}`);
  const { min, max, exclusive_min: exclusiveMin, exclusive_max: exclusiveMax, step, unit } = field.constraint;
  if (min !== undefined || max !== undefined || exclusiveMin !== undefined || exclusiveMax !== undefined) {
    const lower = exclusiveMin !== undefined ? `>${exclusiveMin}` : min ?? '不限';
    const upper = exclusiveMax !== undefined ? `<${exclusiveMax}` : max ?? '不限';
    parts.push(`范围：${lower}–${upper}${unit ? ` ${unit}` : ''}`);
  }
  if (step !== undefined) parts.push(`步长：${step}`);
  return parts.filter(Boolean).join(' ');
}

export function MemoryConfigPage({ onNavigationLockChange }: MemoryConfigPageProps) {
  const initialData = queryCache.peek<MemoryConfigData>(queryKeys.memoryConfig)?.data ?? null;
  const initialParsed = initialData
    ? parseMemoryConfigSchema(initialData.schema, initialData.config, initialData.providers, initialData.constraints)
    : null;
  const [data, setData] = useState<MemoryConfigData | null>(initialData);
  const [baseline, setBaseline] = useState(initialData?.config ?? null);
  const [draft, setDraft] = useState<MemoryConfigDraft | null>(
    initialData && initialParsed ? createMemoryConfigDraft(initialData.config, initialParsed.fields) : null,
  );
  const [activeCategory, setActiveCategory] = useState(initialParsed?.categories[0]?.key ?? 'basic');
  const [saving, setSaving] = useState(false);
  const [reloading, setReloading] = useState(false);
  const [revisionConflict, setRevisionConflict] = useState(false);
  const [feedback, setFeedback] = useState<FeedbackState | null>(null);
  const [error, setError] = useState('');
  const feedbackTimerRef = useRef<number | null>(null);
  const pollControllerRef = useRef<AbortController | null>(null);

  const memoryConfigQuery = useCachedQuery<MemoryConfigData>(queryKeys.memoryConfig, () => apiClient.memoryConfig());
  const parsed = useMemo(() => data
    ? parseMemoryConfigSchema(data.schema, data.config, data.providers, data.constraints)
    : { categories: [], fields: [] }, [data]);
  const validationErrors = useMemo(() => draft
    ? validateMemoryConfigDraft(draft, parsed.fields)
    : {}, [draft, parsed.fields]);
  const changeCount = useMemo(() => draft && baseline
    ? countMemoryConfigChanges(draft, baseline, parsed.fields)
    : 0, [baseline, draft, parsed.fields]);
  const dirty = changeCount > 0;
  const locked = dirty || saving;
  const currentCategory = parsed.categories.find((category) => category.key === activeCategory)
    ?? parsed.categories[0];
  const loading = !data && memoryConfigQuery.isInitialLoading;
  const loadError = !data && memoryConfigQuery.error ? messageOf(memoryConfigQuery.error, '读取记忆配置失败') : '';

  const clearFeedbackTimer = useCallback(() => {
    if (feedbackTimerRef.current === null) return;
    window.clearTimeout(feedbackTimerRef.current);
    feedbackTimerRef.current = null;
  }, []);

  const showFeedback = useCallback((nextFeedback: FeedbackState | null) => {
    clearFeedbackTimer();
    setFeedback(nextFeedback);
  }, [clearFeedbackTimer]);

  const applyLoadedData = useCallback((loaded: MemoryConfigData, preserveDraft = false) => {
    const nextParsed = parseMemoryConfigSchema(loaded.schema, loaded.config, loaded.providers, loaded.constraints);
    setData(loaded);
    setBaseline(loaded.config);
    if (!preserveDraft) setDraft(createMemoryConfigDraft(loaded.config, nextParsed.fields));
    setActiveCategory((current) => nextParsed.categories.some((category) => category.key === current)
      ? current
      : nextParsed.categories[0]?.key ?? 'basic');
  }, []);

  useEffect(() => {
    if (!memoryConfigQuery.data || dirty || saving || revisionConflict) return;
    applyLoadedData(memoryConfigQuery.data);
  }, [applyLoadedData, dirty, memoryConfigQuery.data, revisionConflict, saving]);

  useEffect(() => {
    onNavigationLockChange?.(locked);
  }, [locked, onNavigationLockChange]);

  useEffect(() => {
    const handleBeforeUnload = (event: BeforeUnloadEvent) => {
      if (!locked) return;
      event.preventDefault();
      event.returnValue = '';
    };
    window.addEventListener('beforeunload', handleBeforeUnload);
    return () => window.removeEventListener('beforeunload', handleBeforeUnload);
  }, [locked]);

  useEffect(() => {
    clearFeedbackTimer();
    if (feedback?.kind !== 'success') return undefined;
    const successFeedback = feedback;
    feedbackTimerRef.current = window.setTimeout(() => {
      feedbackTimerRef.current = null;
      setFeedback((current) => current === successFeedback ? null : current);
    }, 4_000);
    return clearFeedbackTimer;
  }, [clearFeedbackTimer, feedback]);

  useEffect(() => () => {
    clearFeedbackTimer();
    pollControllerRef.current?.abort();
    onNavigationLockChange?.(false);
  }, [clearFeedbackTimer, onNavigationLockChange]);

  function updateField(field: MemoryConfigField, value: MemoryConfigValue) {
    setDraft((current) => current ? setAtPath(current, field.path, value) : current);
    setFeedback((current) => current?.kind === 'warning' ? current : null);
    setError('');
  }

  function cancelChanges() {
    if (!data) return;
    applyLoadedData(data);
    setRevisionConflict(false);
    setError('');
    showFeedback({ kind: 'success', message: '已取消未保存的更改。' });
  }

  async function reloadConfig(preserveDraft = false) {
    if (!preserveDraft && dirty && !window.confirm('重新加载会丢弃当前未保存更改，确定继续吗？')) return;
    setReloading(true);
    setError('');
    showFeedback({ kind: 'progress', message: '正在重新加载记忆配置…' });
    try {
      const loaded = await apiClient.memoryConfig();
      queryCache.set(queryKeys.memoryConfig, loaded);
      applyLoadedData(loaded, preserveDraft);
      setRevisionConflict(false);
      showFeedback({
        kind: 'success',
        message: preserveDraft ? '已读取最新版本，并保留当前草稿供审查。' : '已重新加载记忆配置。',
      });
    } catch (reason) {
      showFeedback(null);
      setError(messageOf(reason, '重新加载记忆配置失败'));
    } finally {
      setReloading(false);
    }
  }

  async function pollReload(
    expectedRevision: MemoryConfigRevision,
    expectedConfig: MemoryConfigData['config'],
    oldRuntimeId: string,
    signal: AbortSignal,
  ) {
    await delay(RELOAD_INITIAL_DELAY_MS, signal);
    const deadline = Date.now() + RELOAD_TIMEOUT_MS;
    while (Date.now() < deadline) {
      try {
        const loaded = await apiClient.memoryConfig(signal);
        if (loaded.reload_failed || loaded.reload_status === 'failed') {
          return { loaded, reloadFailed: true };
        }
        if (loaded.runtime_id !== oldRuntimeId
          && revisionMatches(loaded.revision, expectedRevision)
          && memoryConfigEquals(loaded.config, expectedConfig)) {
          return { loaded, reloadFailed: false };
        }
      } catch (reason) {
        if ((reason as Error).name === 'AbortError') throw reason;
      }
      await delay(RELOAD_POLL_INTERVAL_MS, signal);
    }
    return null;
  }

  async function saveConfig() {
    if (!data || !draft || !dirty || saving || Object.keys(validationErrors).length) return;
    const nextConfig = convertMemoryConfigDraft(draft, parsed.fields);
    setSaving(true);
    setError('');
    showFeedback({ kind: 'progress', message: '正在保存记忆配置…' });
    const controller = new AbortController();
    pollControllerRef.current?.abort();
    pollControllerRef.current = controller;
    try {
      const result = await apiClient.saveMemoryConfig({
        config: nextConfig,
        expected_revision: data.revision,
      }, controller.signal);
      queryCache.invalidate(queryKeyPrefixes.memory);
      const savedSnapshot: MemoryConfigData = {
        ...data,
        config: result.config,
        values: result.config,
        revision: result.revision,
        runtime_id: result.runtime_id ?? data.runtime_id,
        reload_status: result.reload_status ?? data.reload_status,
        reload_failed: result.reload_failed ?? data.reload_failed,
      };

      if (result.manual_reload_required) {
        queryCache.set(queryKeys.memoryConfig, savedSnapshot);
        applyLoadedData(savedSnapshot);
        showFeedback({
          kind: 'warning',
          message: result.message ?? '配置已保存，但当前环境要求手动重载插件后才会生效。',
        });
        return;
      }

      if (!result.reload_scheduled && !result.reload_pending) {
        queryCache.set(queryKeys.memoryConfig, savedSnapshot);
        applyLoadedData(savedSnapshot);
        showFeedback({ kind: 'success', message: '记忆配置已保存。' });
        return;
      }

      showFeedback({ kind: 'progress', message: '配置已保存，正在等待插件重载…' });
      const reloadResult = await pollReload(
        result.revision,
        result.config,
        result.old_runtime_id || data.runtime_id,
        controller.signal,
      );
      if (!reloadResult) {
        queryCache.set(queryKeys.memoryConfig, savedSnapshot);
        applyLoadedData(savedSnapshot);
        showFeedback({ kind: 'warning', message: '配置已保存，但在超时时间内无法确认插件已完成重载。' });
        return;
      }
      queryCache.set(queryKeys.memoryConfig, reloadResult.loaded);
      applyLoadedData(reloadResult.loaded);
      if (reloadResult.reloadFailed) {
        showFeedback({ kind: 'warning', message: '配置已保存，但自动重载插件失败，请手动重载插件。' });
        return;
      }
      showFeedback({ kind: 'success', message: '记忆配置已保存，插件已完成重载。' });
    } catch (reason) {
      if ((reason as Error).name === 'AbortError') return;
      if (isRevisionConflict(reason)) {
        setRevisionConflict(true);
        setError(`${messageOf(reason, '配置版本冲突')}。当前草稿已保留，请重新加载最新版本后审查差异。`);
      } else {
        setError(messageOf(reason, '保存记忆配置失败'));
      }
      showFeedback(null);
    } finally {
      if (pollControllerRef.current === controller) pollControllerRef.current = null;
      setSaving(false);
    }
  }

  function renderField(field: MemoryConfigField) {
    if (!draft) return null;
    const value = getAtPath(draft, field.path);
    const id = `memory-config-${pathKey(field.path).replace(/[^a-zA-Z0-9_-]/g, '-')}`;
    const help = fieldHelp(field);
    const helpId = `${id}-help`;
    const fieldError = validationErrors[pathKey(field.path)];

    return (
      <div className="settings-field memory-config-field" key={pathKey(field.path)}>
        <div className="field__topline">
          <code className="field__key">{pathKey(field.path)}</code>
          {field.providerType ? <span className="settings-scope-badge">Provider</span> : null}
        </div>
        {field.kind === 'boolean' ? (
          <SwitchField
            id={id}
            label={field.label}
            checked={Boolean(value)}
            description={help}
            disabled={saving}
            onChange={(checked) => updateField(field, checked)}
          />
        ) : field.options.length ? (
          <div className="memory-config-select-wrap">
            <SelectField
              id={id}
              label={field.label}
              options={field.options.map((option) => ({
                value: option.value,
                label: option.label,
                disabled: option.unavailable,
              }))}
              value={typeof value === 'string' ? value : ''}
              disabled={saving}
              onChange={(next) => updateField(field, next)}
            />
            {help ? <span className="wf-help" id={helpId}>{help}</span> : null}
          </div>
        ) : (
          <label className="wf-label" htmlFor={id}>
            <span>{field.label}</span>
            <span className={field.kind === 'string' ? 'memory-config-text-control' : 'settings-number-control'}>
              <input
                className="wf-input"
                id={id}
                type={field.kind === 'string' ? 'text' : 'number'}
                inputMode={field.kind === 'integer' ? 'numeric' : field.kind === 'number' ? 'decimal' : undefined}
                min={field.constraint.min}
                max={field.constraint.max}
                step={field.constraint.step ?? (field.kind === 'integer' ? 1 : 'any')}
                value={typeof value === 'string' || typeof value === 'number' ? String(value) : ''}
                disabled={saving}
                aria-describedby={help ? helpId : undefined}
                aria-invalid={Boolean(fieldError)}
                onChange={(event) => updateField(field, event.target.value)}
              />
              {field.constraint.unit ? <span>{field.constraint.unit}</span> : null}
            </span>
            {help ? <span className="wf-help" id={helpId}>{help}</span> : null}
            {fieldError ? <span className="settings-field-error" role="alert">{fieldError}</span> : null}
          </label>
        )}
      </div>
    );
  }

  return (
    <div className="page-stack">
      <header className="page-heading">
        <div>
          <p className="eyebrow">Memory Configuration</p>
          <h1>记忆配置</h1>
        </div>
        <div className="page-actions settings-scope">
          <button className="wf-button" type="button" disabled={saving || reloading} onClick={() => void reloadConfig(false)}>
            {reloading ? '重新加载中…' : '重新加载'}
          </button>
        </div>
      </header>

      <p className="settings-context">配置由后端 Schema 动态生成；保存后可能短暂重载整个插件。</p>
      {feedback ? (
        <div
          className={`memory-config-toast${feedback.kind === 'warning' ? ' memory-config-toast--warning' : ''}`}
          role={feedback.kind === 'warning' ? 'alert' : 'status'}
          aria-live={feedback.kind === 'warning' ? undefined : 'polite'}
          aria-atomic="true"
        >
          <div className="memory-config-toast__content">
            <strong>{feedback.kind === 'progress' ? '正在处理' : feedback.kind === 'success' ? '操作成功' : '需要处理'}</strong>
            <span>{feedback.message}</span>
          </div>
          {feedback.kind !== 'progress' ? (
            <button className="memory-config-toast__close" type="button" aria-label="关闭通知" onClick={() => showFeedback(null)}>关闭</button>
          ) : null}
        </div>
      ) : null}
      {error ? (
        <div className="inline-feedback inline-feedback--error" role="alert">
          <span>{error}</span>
          {revisionConflict ? <button className="wf-button" type="button" disabled={reloading || saving} onClick={() => void reloadConfig(true)}>重新加载并保留草稿</button> : null}
        </div>
      ) : null}
      {data && memoryConfigQuery.error ? <p className="inline-feedback inline-feedback--error" role="alert">{messageOf(memoryConfigQuery.error, '后台刷新记忆配置失败')}</p> : null}
      {loading ? <DataState state="loading" title="正在读取记忆配置" /> : null}
      {!loading && loadError ? <DataState state="error" title="读取记忆配置失败" message={loadError} action={<button className="wf-button" type="button" onClick={() => void reloadConfig(false)}>重试</button>} /> : null}
      {!loading && data && parsed.categories.length === 0 ? <DataState state="empty" title="后端未返回可编辑的记忆配置字段" /> : null}

      {!loading && data && draft && currentCategory ? (
        <div className="settings-layout memory-config-layout">
          <aside className="category-panel">
            <WorkshopPanel title="配置分类">
              <nav className="category-nav" aria-label="记忆配置分类">
                {parsed.categories.map((category) => (
                  <button
                    className="wf-button wf-button--quiet category-nav__button"
                    type="button"
                    key={category.key}
                    aria-current={currentCategory.key === category.key ? 'page' : undefined}
                    onClick={() => setActiveCategory(category.key)}
                  >
                    <strong>{category.title}</strong>
                    <span>{category.fields.length} 项配置</span>
                  </button>
                ))}
              </nav>
            </WorkshopPanel>
          </aside>

          <main className="main-panel">
            <WorkshopPanel title={currentCategory.title}>
              <div className="settings-fields">{currentCategory.fields.map(renderField)}</div>
            </WorkshopPanel>
          </main>

          <aside className="summary-panel">
            <WorkshopPanel title="配置摘要">
              <dl className="summary-list">
                <div><dt>当前分类</dt><dd>{currentCategory.title}</dd></div>
                <div><dt>配置版本</dt><dd>{typeof data.revision === 'object' ? 'opaque' : String(data.revision)}</dd></div>
                <div><dt>Runtime ID</dt><dd>{data.runtime_id || '—'}</dd></div>
                <div><dt>变更项数</dt><dd>{changeCount}</dd></div>
                <div><dt>字段校验</dt><dd>{Object.keys(validationErrors).length ? `${Object.keys(validationErrors).length} 项待修正` : '已通过'}</dd></div>
                <div><dt>保存状态</dt><dd className="save-state">{saving ? '保存/重载中…' : dirty ? '待保存' : '已保存'}</dd></div>
              </dl>
              <div className="form-actions summary-actions">
                <button className="wf-button" type="button" disabled={!dirty || saving} onClick={cancelChanges}>取消更改</button>
                <button className="wf-button wf-button--primary" type="button" disabled={!dirty || saving || Boolean(Object.keys(validationErrors).length)} onClick={() => void saveConfig()}>
                  {saving ? '保存中…' : '保存配置'}
                </button>
              </div>
            </WorkshopPanel>
          </aside>
        </div>
      ) : null}
    </div>
  );
}

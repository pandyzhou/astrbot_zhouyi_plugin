import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ConfirmDialog, DataState, SwitchField, WorkshopPanel } from '@pandyzhou/astrbot-mc-ui';
import { ApiClientError, apiClient } from '../../api/client';
import type {
  GroupRuntimeSettingKey,
  RuntimeSettingKey,
  RuntimeSettings,
  SettingConstraint,
  SettingsData,
  SettingsMutationInput,
  SettingsPreviewData,
  SettingsScope,
} from '../../api/types';
import { queryCache } from '../../store/queryCacheCore';
import { queryKeyPrefixes, queryKeys } from '../../store/queryKeys';
import { useCachedQuery } from '../../store/useCachedQuery';
import { useWorkshopStore } from '../../store/workshopStore';

type Draft = { [Key in RuntimeSettingKey]: RuntimeSettings[Key] | string };

const decimalSettingKeys = new Set<RuntimeSettingKey>([
  'mc_lookup_timeout_seconds',
  'mc_status_timeout_seconds',
]);

const groupKeys: GroupRuntimeSettingKey[] = [
  'max_history_points',
  'trend_sampling_enabled',
  'auto_cleanup_enabled',
  'auto_cleanup_days',
  'auto_refresh_on_page_open',
  'default_trend_hours',
  'mc_lookup_timeout_seconds',
  'mc_status_timeout_seconds',
];

const fallbackConstraints: Partial<Record<RuntimeSettingKey, SettingConstraint>> = {
  max_history_points: { min: 168, max: 100000, step: 1, unit: '点/服务器' },
  auto_cleanup_days: { min: 1, max: 365, step: 1, unit: '天' },
  default_trend_hours: { min: 1, max: 168, step: 1, unit: '小时' },
  mc_lookup_timeout_seconds: { min: 0.5, max: 30, step: 0.5, unit: '秒' },
  mc_status_timeout_seconds: { min: 1, max: 60, step: 0.5, unit: '秒' },
  max_concurrent_queries: { min: 1, max: 20, step: 1, unit: '个' },
};

type SettingsSectionKey = 'trend' | 'query' | 'cleanup' | 'experience';

const sections: Array<{
  key: SettingsSectionKey;
  title: string;
  fields: RuntimeSettingKey[];
}> = [
  {
    key: 'trend',
    title: '趋势数据',
    fields: ['trend_sampling_enabled', 'max_history_points', 'default_trend_hours'],
  },
  {
    key: 'query',
    title: '查询行为',
    fields: ['mc_lookup_timeout_seconds', 'mc_status_timeout_seconds', 'max_concurrent_queries'],
  },
  {
    key: 'cleanup',
    title: '自动清理',
    fields: ['auto_cleanup_enabled', 'auto_cleanup_days'],
  },
  {
    key: 'experience',
    title: '页面体验',
    fields: ['auto_refresh_on_page_open'],
  },
];

const fieldCopy: Record<RuntimeSettingKey, { label: string; help: string }> = {
  max_history_points: { label: '每台服务器最大历史点数', help: '降低上限可能立即裁剪已有趋势点，保存前会显示准确影响。' },
  trend_sampling_enabled: { label: '启用趋势采样', help: '关闭后停止新增趋势采样，不删除已有历史。' },
  auto_cleanup_enabled: { label: '启用自动清理', help: '允许后端按清理规则处理长期无成功记录的服务器。' },
  auto_cleanup_days: { label: '自动清理判定天数', help: '降低天数可能增加候选服务器，但保存配置本身不代表立即删除。' },
  auto_refresh_on_page_open: { label: '进入服务器页时自动刷新', help: '每次进入或切换群组后最多执行一次全量状态查询，不会周期刷新。' },
  default_trend_hours: { label: '趋势页默认小时数', help: '进入趋势页或切换群组时使用。' },
  mc_lookup_timeout_seconds: { label: 'Minecraft 地址解析超时', help: '用于域名与 SRV 等地址查找阶段。' },
  mc_status_timeout_seconds: { label: 'Minecraft 状态查询超时', help: '用于连接服务器并读取状态阶段。' },
  max_concurrent_queries: { label: '最大并发查询数', help: '仅全局配置，限制同时执行的 Minecraft 查询数量。' },
};

function createDraft(values: RuntimeSettings): Draft {
  return { ...values };
}

function inheritedKeys(data: SettingsData) {
  return new Set<GroupRuntimeSettingKey>(groupKeys.filter((key) => data.group_overrides[key] === undefined));
}

function validateGroup(value: SettingsData, groupId: string): SettingsData {
  if (value.group_id !== groupId) throw new Error('读取到的配置不属于当前群组，请重新加载。');
  return value;
}

function errorMessage(reason: unknown, fallback: string) {
  return reason instanceof Error ? reason.message || fallback : reason ? fallback : '';
}

interface SettingsPageProps {
  onNavigationLockChange?: (locked: boolean) => void;
}

export function SettingsPage({ onNavigationLockChange }: SettingsPageProps) {
  const groupId = useWorkshopStore((state) => state.selectedGroupId);
  const groupIdRef = useRef(groupId);
  const scopeRef = useRef<SettingsScope>('global');
  groupIdRef.current = groupId;

  const settingsKey = queryKeys.mcSettings(groupId);
  const initialCached = queryCache.peek<SettingsData>(settingsKey)?.data;
  const initialData = initialCached?.group_id === groupId ? initialCached : null;
  const initialDraft = initialData ? createDraft(initialData.global) : null;
  const [scope, setScope] = useState<SettingsScope>('global');
  const [activeSection, setActiveSection] = useState<SettingsSectionKey>('trend');
  const [data, setData] = useState<SettingsData | null>(initialData);
  const [draft, setDraft] = useState<Draft | null>(initialDraft);
  const [inherited, setInherited] = useState<Set<GroupRuntimeSettingKey>>(new Set());
  const [initialSignature, setInitialSignature] = useState(initialDraft
    ? JSON.stringify({ draft: initialDraft, inherited: [] })
    : '');
  const [reloading, setReloading] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [feedback, setFeedback] = useState('');
  const [pendingPreview, setPendingPreview] = useState<SettingsPreviewData | null>(null);

  const settingsQuery = useCachedQuery<SettingsData>(settingsKey, async () => (
    validateGroup(await apiClient.settings(groupId), groupId)
  ));
  const cachedData = settingsQuery.data?.group_id === groupId ? settingsQuery.data : undefined;
  const cacheError = errorMessage(settingsQuery.error, '读取运行配置失败');

  const applyLoadedData = useCallback((loaded: SettingsData, nextScope: SettingsScope) => {
    const nextDraft = createDraft(nextScope === 'global' ? loaded.global : loaded.effective);
    const nextInherited = nextScope === 'group' ? inheritedKeys(loaded) : new Set<GroupRuntimeSettingKey>();
    setData(loaded);
    setDraft(nextDraft);
    setInherited(nextInherited);
    setInitialSignature(JSON.stringify({ draft: nextDraft, inherited: [...nextInherited].sort() }));
  }, []);

  const load = useCallback(async (
    nextScope: SettingsScope,
    requestedGroupId = groupIdRef.current,
  ): Promise<boolean> => {
    setReloading(true);
    try {
      const loaded = await queryCache.revalidate(
        queryKeys.mcSettings(requestedGroupId),
        async () => validateGroup(await apiClient.settings(requestedGroupId), requestedGroupId),
      );
      if (groupIdRef.current !== requestedGroupId) return false;
      applyLoadedData(loaded, nextScope);
      return true;
    } catch {
      return false;
    } finally {
      if (groupIdRef.current === requestedGroupId) setReloading(false);
    }
  }, [applyLoadedData]);

  useEffect(() => {
    setFeedback('');
    setError('');
    setPendingPreview(null);
    setData(null);
    setDraft(null);
    setInherited(new Set());
    setInitialSignature('');
  }, [groupId]);

  const changeCount = useMemo(() => {
    if (!draft || !initialSignature) return 0;
    try {
      const initial = JSON.parse(initialSignature) as { draft: Draft; inherited: GroupRuntimeSettingKey[] };
      const initialInherited = new Set(initial.inherited);
      const keys = scope === 'global' ? (Object.keys(draft) as RuntimeSettingKey[]) : groupKeys;
      return keys.reduce((count, key) => {
        const valueChanged = String(draft[key]) !== String(initial.draft[key]);
        const inheritanceChanged = scope === 'group'
          && inherited.has(key as GroupRuntimeSettingKey) !== initialInherited.has(key as GroupRuntimeSettingKey);
        return count + (valueChanged || inheritanceChanged ? 1 : 0);
      }, 0);
    } catch {
      return 0;
    }
  }, [draft, inherited, initialSignature, scope]);
  const dirty = changeCount > 0;

  useEffect(() => {
    if (!cachedData || dirty || saving || pendingPreview) return;
    applyLoadedData(cachedData, scopeRef.current);
  }, [applyLoadedData, cachedData, dirty, pendingPreview, saving]);

  useEffect(() => {
    onNavigationLockChange?.(dirty || saving);
  }, [dirty, onNavigationLockChange, saving]);

  useEffect(() => () => {
    onNavigationLockChange?.(false);
  }, [onNavigationLockChange]);

  const validationErrors = useMemo(() => {
    const errors: Partial<Record<RuntimeSettingKey, string>> = {};
    if (!draft || !data) return errors;
    const keys = scope === 'global' ? (Object.keys(draft) as RuntimeSettingKey[]) : groupKeys;
    for (const key of keys) {
      if (scope === 'group' && inherited.has(key as GroupRuntimeSettingKey)) continue;
      if (typeof data.effective[key] === 'boolean') continue;
      const value = Number(draft[key]);
      const constraint = data.constraints[key] ?? fallbackConstraints[key];
      if (!Number.isFinite(value)) errors[key] = '请输入有效数字。';
      else if (!decimalSettingKeys.has(key) && !Number.isInteger(value)) errors[key] = '请输入整数。';
      else if (constraint?.min !== undefined && value < constraint.min) errors[key] = `不能小于 ${constraint.min}。`;
      else if (constraint?.max !== undefined && value > constraint.max) errors[key] = `不能大于 ${constraint.max}。`;
      else if (constraint?.step !== undefined) {
        const stepBase = constraint.min ?? 0;
        const stepOffset = (value - stepBase) / constraint.step;
        if (Math.abs(stepOffset - Math.round(stepOffset)) > 1e-8) errors[key] = `必须按 ${constraint.step} 递增。`;
      }
    }
    return errors;
  }, [data, draft, inherited, scope]);

  const hasValidationError = Object.keys(validationErrors).length > 0;

  function switchScope(nextScope: SettingsScope) {
    if (nextScope === scope) return;
    if (dirty) {
      setError('当前范围有未保存更改，请先保存或取消更改后再切换。');
      setFeedback('');
      return;
    }
    scopeRef.current = nextScope;
    const latestSnapshot = cachedData ?? (data?.group_id === groupId ? data : undefined);
    if (latestSnapshot) applyLoadedData(latestSnapshot, nextScope);
    setScope(nextScope);
    setError('');
    setFeedback('');
  }

  function cancelChanges() {
    const latestSnapshot = cachedData ?? data;
    if (latestSnapshot) applyLoadedData(latestSnapshot, scope);
    setError('');
    setFeedback('已取消未保存的更改。');
  }

  function setValue<Key extends RuntimeSettingKey>(key: Key, value: Draft[Key]) {
    setDraft((current) => current ? { ...current, [key]: value } : current);
  }

  function setInherit(key: GroupRuntimeSettingKey, shouldInherit: boolean) {
    if (!data) return;
    setInherited((current) => {
      const next = new Set(current);
      if (shouldInherit) next.add(key);
      else next.delete(key);
      return next;
    });
    if (shouldInherit) setValue(key, data.global[key]);
  }

  function mutationInput(): SettingsMutationInput {
    const values: Partial<RuntimeSettings> = {};
    if (!draft || !data) throw new Error('配置尚未加载');
    const keys = scope === 'global' ? (Object.keys(data.global) as RuntimeSettingKey[]) : groupKeys;
    for (const key of keys) {
      if (scope === 'group' && inherited.has(key as GroupRuntimeSettingKey)) continue;
      const raw = draft[key];
      (values as Record<RuntimeSettingKey, RuntimeSettings[RuntimeSettingKey]>)[key] = typeof data.effective[key] === 'boolean'
        ? Boolean(raw) as never
        : Number(raw) as never;
    }
    return {
      scope,
      group_id: scope === 'group' ? groupId : undefined,
      values,
      reset_keys: scope === 'group' ? [...inherited] : [],
      expected_revision: scope === 'global' ? data.revision.global : data.revision.group,
    };
  }

  async function handleContractError(reason: unknown, fallback: string) {
    const apiError = reason instanceof ApiClientError ? reason : null;
    if (apiError && ['SETTINGS_REVISION_CONFLICT', 'SETTINGS_PREVIEW_STALE'].includes(apiError.code)) {
      const reloaded = await load(scopeRef.current);
      setError(reloaded ? `${apiError.message}，已重新加载最新配置。` : apiError.message);
      return;
    }
    setError((reason as Error).message || fallback);
  }

  async function saveConfirmed(preview: SettingsPreviewData) {
    setSaving(true);
    setError('');
    try {
      const input = mutationInput();
      input.preview_id = preview.preview_id;
      if (preview.history_trim.required) {
        input.confirmation = {
          history_trim: true,
          expected_points_to_delete: preview.history_trim.points_to_delete,
        };
      }
      const result = await apiClient.saveSettings(input);
      if (input.scope === 'global') {
        queryCache.invalidate(queryKeyPrefixes.mcSettings);
        queryCache.invalidate(queryKeyPrefixes.mcTrends);
      } else {
        queryCache.invalidate(queryKeys.mcSettings(groupId));
        queryCache.invalidate([...queryKeyPrefixes.mcTrends, groupId]);
      }
      const reloaded = await load(scopeRef.current);
      setPendingPreview(null);
      if (!reloaded) {
        setFeedback('配置已保存，但重新读取当前群组失败。');
        return;
      }
      setFeedback(result.history_trim.performed
        ? `配置已保存，并裁剪 ${result.history_trim.deleted_points.toLocaleString()} 个历史点。`
        : '运行配置已保存。');
    } catch (reason) {
      setPendingPreview(null);
      await handleContractError(reason, '保存运行配置失败');
    } finally {
      setSaving(false);
    }
  }

  async function previewAndSave() {
    if (!dirty || hasValidationError || saving) return;
    setSaving(true);
    setError('');
    setFeedback('');
    try {
      const preview = await apiClient.previewSettings(mutationInput());
      const cleanupDaysLowered = preview.next_effective.auto_cleanup_days < preview.current_effective.auto_cleanup_days;
      if (preview.requires_confirmation || cleanupDaysLowered) {
        setPendingPreview(preview);
        setSaving(false);
        return;
      }
      await saveConfirmed(preview);
    } catch (reason) {
      await handleContractError(reason, '预览运行配置失败');
      setSaving(false);
    }
  }

  function renderField(key: RuntimeSettingKey) {
    if (!draft || !data) return null;
    const copy = fieldCopy[key];
    const isBoolean = typeof data.effective[key] === 'boolean';
    const globalOnly = scope === 'group' && key === 'max_concurrent_queries';
    const canInherit = scope === 'group' && !globalOnly;
    const isInherited = canInherit && inherited.has(key as GroupRuntimeSettingKey);
    const constraint = data.constraints[key] ?? fallbackConstraints[key];
    const effectiveValue = globalOnly ? data.global.max_concurrent_queries : (isInherited ? data.global[key] : draft[key]);
    const helpId = `${key}-help`;

    return (
      <div className={`settings-field${isInherited ? ' settings-field--inherited' : ''}`} key={key}>
        <div className="settings-field__inheritance field__topline">
          <code className="field__key">{key}</code>
          <div className="field__action-slot">
            {globalOnly ? <span className="settings-scope-badge">仅全局</span> : canInherit ? (
              <button
                className="wf-button wf-button--quiet settings-inherit-button"
                type="button"
                aria-pressed={isInherited}
                disabled={saving}
                onClick={() => setInherit(key as GroupRuntimeSettingKey, !isInherited)}
              >
                {isInherited ? '继承全局' : '群组覆盖'}
              </button>
            ) : <span className="settings-scope-badge">全局</span>}
          </div>
        </div>
        {isBoolean ? (
          <SwitchField
            id={`setting-${key}`}
            label={copy.label}
            checked={Boolean(effectiveValue)}
            disabled={saving || isInherited || globalOnly}
            description={copy.help}
            onChange={(checked) => setValue(key, checked)}
          />
        ) : (
          <label className="wf-label" htmlFor={`setting-${key}`}>
            <span>{copy.label}</span>
            <span className="settings-number-control">
              <input
                className="wf-input"
                id={`setting-${key}`}
                type="number"
                inputMode="numeric"
                min={constraint?.min}
                max={constraint?.max}
                step={constraint?.step ?? 1}
                value={String(effectiveValue)}
                disabled={saving || isInherited || globalOnly}
                aria-describedby={helpId}
                aria-invalid={Boolean(validationErrors[key])}
                onChange={(event) => setValue(key, event.target.value)}
              />
              {constraint?.unit ? <span>{constraint.unit}</span> : null}
            </span>
            <span className="wf-help" id={helpId}>
              {copy.help}
              {constraint?.min !== undefined && constraint.max !== undefined
                ? ` 范围 ${constraint.min}–${constraint.max}${constraint.unit ? ` ${constraint.unit}` : ''}。`
                : ''}
            </span>
            {key === 'max_history_points' ? (
              <span className="settings-estimate">按每小时 1 个采样点估算，约保留 {(Number(effectiveValue) / 24).toFixed(1)} 天。</span>
            ) : null}
            {validationErrors[key] ? <span className="settings-field-error">{validationErrors[key]}</span> : null}
          </label>
        )}
        <p className="settings-effective">
          {globalOnly
            ? `当前 effective：${String(data.global.max_concurrent_queries)}（仅全局）`
            : scope === 'global'
              ? `当前 effective：${String(effectiveValue)}（全局）`
              : `当前 effective：${String(isInherited ? data.global[key] : effectiveValue)}${isInherited ? '（来自全局）' : '（群组覆盖）'}`}
        </p>
      </div>
    );
  }

  const dialogDescription = pendingPreview ? [
    pendingPreview.history_trim.required
      ? `历史上限将从 ${pendingPreview.history_trim.current_limit} 降至 ${pendingPreview.history_trim.next_limit}，影响 ${pendingPreview.history_trim.affected_servers} 台服务器，并删除 ${pendingPreview.history_trim.points_to_delete.toLocaleString()} 个最旧趋势点。`
      : '',
    pendingPreview.next_effective.auto_cleanup_days < pendingPreview.current_effective.auto_cleanup_days
      ? `清理判定天数降低后，候选数量将从 ${pendingPreview.cleanup_impact.current_candidate_count} 变为 ${pendingPreview.cleanup_impact.next_candidate_count}（新增 ${pendingPreview.cleanup_impact.new_candidate_count}）。这只是候选变化，保存配置不会立即删除服务器。`
      : '',
  ].filter(Boolean).join(' ') : '';
  const currentSection = sections.find((section) => section.key === activeSection) ?? sections[0];
  const currentGroupLoaded = data?.group_id === groupId;
  const loading = !currentGroupLoaded && (settingsQuery.isInitialLoading || reloading);
  const blockingLoadError = !currentGroupLoaded ? cacheError : '';
  const validationErrorCount = Object.keys(validationErrors).length;

  return (
    <div className="page-stack">
      <header className="page-heading">
        <div>
          <p className="eyebrow">Runtime Settings</p>
          <h1>运行配置</h1>
        </div>
        <div className="page-actions settings-scope" role="group" aria-label="配置范围">
          <button className="wf-button" type="button" aria-pressed={scope === 'global'} disabled={saving || loading || !currentGroupLoaded} onClick={() => switchScope('global')}>全局</button>
          <button className="wf-button" type="button" aria-pressed={scope === 'group'} disabled={saving || loading || !currentGroupLoaded} onClick={() => switchScope('group')}>当前群组</button>
          <button className="wf-button" type="button" disabled={saving || reloading} onClick={() => void load(scopeRef.current)}>{reloading ? '重新加载中…' : '重新加载'}</button>
        </div>
      </header>

      <p className="settings-context">{scope === 'global' ? '正在编辑全局默认值。' : `正在编辑群组 ${groupId}；标记为“继承全局”的字段不会保存群组覆盖值。`}</p>
      <div className="wf-sr-only" aria-live="polite">{saving ? '正在保存运行配置' : feedback}</div>
      {feedback ? <p className="inline-feedback" role="status">{feedback}</p> : null}
      {error ? <p className="inline-feedback inline-feedback--error" role="alert">{error}</p> : null}
      {currentGroupLoaded && cacheError ? <p className="inline-feedback inline-feedback--error" role="alert">{cacheError}</p> : null}
      {loading ? <DataState state="loading" title="正在读取运行配置" /> : null}
      {!loading && (blockingLoadError || !currentGroupLoaded) ? (
        <DataState
          state={blockingLoadError ? 'error' : 'empty'}
          title={blockingLoadError ? '读取运行配置失败' : '暂无当前群组配置'}
          message={blockingLoadError || `未读取到群组 ${groupId} 的运行配置。`}
          action={<button className="wf-button" type="button" onClick={() => void load(scopeRef.current)}>重新加载</button>}
        />
      ) : null}

      {!loading && currentGroupLoaded && draft && data ? (
        <div className="settings-layout">
          <aside className="category-panel">
            <WorkshopPanel title="配置分类">
              <nav className="category-nav" aria-label="运行配置分类">
                {sections.map((section) => (
                  <button
                    className="wf-button wf-button--quiet category-nav__button"
                    type="button"
                    key={section.key}
                    aria-current={activeSection === section.key ? 'page' : undefined}
                    onClick={() => setActiveSection(section.key)}
                  >
                    <strong>{section.title}</strong>
                    <span>{section.fields.length} 项配置</span>
                  </button>
                ))}
              </nav>
            </WorkshopPanel>
          </aside>

          <main className="main-panel">
            <WorkshopPanel
              key={currentSection.key}
              title={currentSection.title}
            >
              <div className="settings-fields">{currentSection.fields.map(renderField)}</div>
            </WorkshopPanel>
          </main>

          <aside className="summary-panel">
            <WorkshopPanel title="配置摘要">
              <dl className="summary-list">
                <div><dt>目标</dt><dd>{scope === 'global' ? '全局默认值（全部群组）' : `群组 ${groupId}`}</dd></div>
                <div><dt>当前分类</dt><dd>{currentSection.title}</dd></div>
                <div><dt>继承状态</dt><dd>{scope === 'global' ? '9 项全局值' : `${inherited.size} 项继承 / ${groupKeys.length - inherited.size} 项覆盖`}</dd></div>
                <div><dt>变更项数</dt><dd>{changeCount}</dd></div>
                <div><dt>字段校验</dt><dd>{validationErrorCount ? `${validationErrorCount} 项待修正` : '已通过'}</dd></div>
                <div><dt>保存状态</dt><dd className="save-state">{saving ? '保存中…' : dirty ? '待保存' : '已保存'}</dd></div>
              </dl>
              <p className="risk-copy">
                保存前会先执行安全预览；降低历史上限可能裁剪旧趋势点，降低清理天数只会改变候选范围，不会立即删除服务器。
              </p>
              <div className="form-actions summary-actions">
                <button className="wf-button" type="button" disabled={!dirty || saving} onClick={cancelChanges}>取消更改</button>
                <button className="wf-button wf-button--primary" type="button" disabled={!dirty || hasValidationError || saving} onClick={() => void previewAndSave()}>
                  {saving ? '保存中…' : '保存配置'}
                </button>
              </div>
            </WorkshopPanel>
          </aside>
        </div>
      ) : null}

      <ConfirmDialog
        open={Boolean(pendingPreview)}
        title={pendingPreview?.history_trim.required ? '确认配置影响' : '确认候选变化'}
        description={dialogDescription}
        confirmLabel={pendingPreview?.history_trim.required ? '确认保存并裁剪' : '确认保存'}
        danger={Boolean(pendingPreview?.history_trim.required)}
        busy={saving}
        onClose={() => { if (!saving) setPendingPreview(null); }}
        onConfirm={() => { if (pendingPreview) void saveConfirmed(pendingPreview); }}
      />
    </div>
  );
}

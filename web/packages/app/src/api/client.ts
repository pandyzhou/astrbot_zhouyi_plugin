import { MEMORY_ADMIN_PATHS } from './memoryAdminContract';
import { mockRequest } from './mockClient';
import type {
  AddServerInput,
  ApiEnvelope,
  BackendBootstrapData as RawBootstrapData,
  BackendCleanupExecuteData as RawCleanupExecuteData,
  BackendCleanupPreviewData as RawCleanupPreviewData,
  BackendDeleteServerData as RawDeleteData,
  BackendServerMutationData as RawMutationData,
  BackendServersData as RawServersData,
  BackendStatusData as RawStatusData,
  BackendStatusServer as RawStatusServer,
  BackendTrendsData as RawTrendsData,
  BootstrapData,
  CleanupData,
  DeleteServerData,
  DeleteServerInput,
  MemoryAliasMoveInput,
  MemoryConfigData,
  MemoryConfigMutationInput,
  MemoryConfigSaveData,
  MemoryConflict,
  MemoryConflictResolveInput,
  MemoryIdentitiesData,
  MemoryIdentityAlias,
  MemoryIndexRetryInput,
  MemoryMaintenanceStatusData,
  MemoryMergeInput,
  MemoryMergePreviewData,
  MemoryMergePreviewInput,
  MemoryObject,
  MemoryObjectBatchInput,
  MemoryObjectDetailData,
  MemoryObjectFilters,
  MemoryObjectMutationInput,
  MemoryObjectsData,
  MemoryObjectScope,
  MemoryObjectStatus,
  MemoryObjectUpdateInput,
  MemoryOwner,
  MemoryOwnerMergeInput,
  MemoryOwnerMergePreviewData,
  MemoryOwnerUpdateInput,
  MemoryRelation,
  MemoryRevision,
  MemorySourceMessage,
  MemorySupersedeInput,
  MemoryProviderOption,
  RefreshStatusData,
  RefreshStatusInput,
  ServerMutationData,
  ServerRecord,
  ServersData,
  SettingsData,
  SettingsMutationInput,
  SettingsPreviewData,
  SettingsSaveData,
  SourceUpdateItem,
  SourceUpdateStatus,
  SourceUpdatesData,
  TrendPoint,
  TrendsData,
  UpdateServerInput,
} from './types';

const API_PREFIX = '/api/plug/astrbot_zhouyi_plugin';
const mutationKeys = new Set<string>();
let bridgeReady: Promise<unknown> | null = null;

type Query = Record<string, string | number | boolean | undefined>;
type RawObject = Record<string, unknown>;

export class ApiClientError extends Error {
  readonly code: string;
  readonly details?: unknown;

  constructor(code: string, message: string, details?: unknown) {
    super(message);
    this.name = 'ApiClientError';
    this.code = code;
    this.details = details;
  }
}

function requireOwnerUserId(ownerUserId: string): string {
  const normalized = ownerUserId.trim();
  if (!normalized) {
    throw new ApiClientError('MEMORY_OWNER_REQUIRED', '必须选择具体 owner，禁止跨 owner 查询');
  }
  return normalized;
}

interface RequestOptions {
  method?: 'GET' | 'POST';
  query?: Query;
  body?: unknown;
  signal?: AbortSignal;
  mutationKey?: string;
}

function abortError() {
  return new DOMException('请求已取消', 'AbortError');
}

function withAbort<T>(promise: Promise<T>, signal?: AbortSignal): Promise<T> {
  if (!signal) return promise;
  if (signal.aborted) return Promise.reject(abortError());

  return new Promise<T>((resolve, reject) => {
    const onAbort = () => reject(abortError());
    signal.addEventListener('abort', onAbort, { once: true });
    promise.then(resolve, reject).finally(() => signal.removeEventListener('abort', onAbort));
  });
}

function normalizeEnvelope<T>(value: unknown): ApiEnvelope<T> {
  if (!value || typeof value !== 'object') {
    throw new ApiClientError('INVALID_RESPONSE', '后端返回了无法识别的数据格式');
  }
  const payload = value as RawObject;
  if (typeof payload.success === 'boolean') return payload as unknown as ApiEnvelope<T>;
  if (payload.status === 'ok') return { success: true, data: payload.data as T };
  if (payload.status === 'error') {
    const details = payload.data && typeof payload.data === 'object' ? payload.data as RawObject : undefined;
    return {
      success: false,
      error: {
        code: typeof details?.code === 'string' ? details.code : 'REQUEST_FAILED',
        message: typeof payload.message === 'string' ? payload.message : '请求失败',
        details,
      },
    };
  }
  throw new ApiClientError('INVALID_RESPONSE', '后端返回了无法识别的数据格式');
}

function unwrap<T>(envelope: ApiEnvelope<T>): T {
  if (envelope.success) return envelope.data;
  throw new ApiClientError(envelope.error.code, envelope.error.message, envelope.error.details);
}

async function request<T>(endpoint: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET';
  const mutationKey = options.mutationKey;
  if (mutationKey && mutationKeys.has(mutationKey)) {
    throw new ApiClientError('DUPLICATE_SUBMISSION', '相同操作正在处理中，请勿重复提交');
  }
  if (mutationKey) mutationKeys.add(mutationKey);

  try {
    let payload: unknown;
    if (import.meta.env.VITE_MOCK_API === 'true') {
      payload = await mockRequest<T>(endpoint, method, options.query, options.body, options.signal);
    } else if (window.AstrBotPluginPage) {
      bridgeReady ??= window.AstrBotPluginPage.ready();
      await withAbort(bridgeReady, options.signal);
      const relativeEndpoint = endpoint.replace(/^\/+/, '');
      const bridgeCall = method === 'GET'
        ? window.AstrBotPluginPage.apiGet(relativeEndpoint, options.query)
        : window.AstrBotPluginPage.apiPost(relativeEndpoint, options.body);
      return await withAbort(bridgeCall, options.signal) as T;
    } else {
      const url = new URL(`${API_PREFIX}${endpoint}`, window.location.origin);
      Object.entries(options.query ?? {}).forEach(([key, value]) => {
        if (value !== undefined) url.searchParams.set(key, String(value));
      });
      const response = await fetch(url, {
        method,
        credentials: 'include',
        signal: options.signal,
        headers: method === 'POST' ? { 'Content-Type': 'application/json' } : undefined,
        body: method === 'POST' ? JSON.stringify(options.body ?? {}) : undefined,
      });
      if (response.status === 401) {
        throw new ApiClientError('AUTH_REQUIRED', '请先登录 AstrBot 后再使用独立管理页');
      }
      const text = await response.text();
      try {
        payload = text ? JSON.parse(text) : null;
      } catch {
        throw new ApiClientError('INVALID_JSON', `后端返回非 JSON 内容（HTTP ${response.status}）`);
      }
      if (!response.ok && (!payload || typeof payload !== 'object')) {
        throw new ApiClientError('HTTP_ERROR', `请求失败（HTTP ${response.status}）`);
      }
    }
    return unwrap(normalizeEnvelope<T>(payload));
  } finally {
    if (mutationKey) mutationKeys.delete(mutationKey);
  }
}

function numberOrNull(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function stringOrNull(value: unknown): string | null {
  return typeof value === 'string' && value ? value : null;
}

function timestampOrNull(value: unknown): number | null {
  const numeric = numberOrNull(value);
  if (numeric !== null) return numeric;
  if (typeof value !== 'string') return null;
  const parsed = Date.parse(value);
  return Number.isFinite(parsed) ? Math.floor(parsed / 1000) : null;
}

function objectOrEmpty(value: unknown): RawObject {
  return value && typeof value === 'object' ? value as RawObject : {};
}

const sourceStatuses = new Set<SourceUpdateStatus>(['current', 'new_version', 'new_commits', 'changed', 'unavailable']);
const sourceRoles: Record<string, string> = {
  livingmemory: '提供长期记忆能力的上游来源',
  mcgetter_enhanced: '提供 Minecraft 服务器查询能力的上游来源',
};

function repositoryUrl(repository: string): string | null {
  return /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/.test(repository) ? `https://github.com/${repository}` : null;
}

function normalizeSourceUpdateItem(value: unknown): SourceUpdateItem {
  const raw = objectOrEmpty(value);
  const baselineRaw = objectOrEmpty(raw.baseline);
  const upstreamRaw = objectOrEmpty(raw.upstream);
  const id = stringOrNull(raw.id) ?? '';
  const repository = stringOrNull(baselineRaw.repository) ?? stringOrNull(raw.repository) ?? '';
  const upstreamCommit = stringOrNull(upstreamRaw.commit_sha) ?? stringOrNull(raw.latest_commit);
  const upstreamRepositoryUrl = stringOrNull(upstreamRaw.repository_url) ?? repositoryUrl(repository);
  const status = stringOrNull(raw.status);

  return {
    id,
    display_name: stringOrNull(raw.display_name) ?? stringOrNull(raw.name) ?? id,
    role: stringOrNull(raw.role) ?? sourceRoles[id] ?? '监控固定上游来源的版本与提交变化',
    status: status && sourceStatuses.has(status as SourceUpdateStatus) ? status as SourceUpdateStatus : 'unavailable',
    stale: raw.stale === true,
    baseline: {
      version: stringOrNull(baselineRaw.version) ?? stringOrNull(raw.baseline_version),
      commit_sha: stringOrNull(baselineRaw.commit_sha) ?? stringOrNull(raw.baseline_commit),
      repository,
      branch: stringOrNull(baselineRaw.branch) ?? stringOrNull(raw.branch) ?? '',
    },
    upstream: {
      version: stringOrNull(upstreamRaw.version) ?? stringOrNull(raw.latest_version),
      commit_sha: upstreamCommit,
      committed_at: timestampOrNull(upstreamRaw.committed_at),
      commit_title: stringOrNull(upstreamRaw.commit_title),
      repository_url: upstreamRepositoryUrl,
      commit_url: stringOrNull(upstreamRaw.commit_url)
        ?? (upstreamRepositoryUrl && upstreamCommit ? `${upstreamRepositoryUrl}/commit/${upstreamCommit}` : null),
    },
    error: stringOrNull(raw.error),
  };
}

function normalizeSourceUpdates(value: unknown): SourceUpdatesData {
  const raw = objectOrEmpty(value);
  const rateLimitRaw = objectOrEmpty(raw.rate_limit);
  const hasRateLimit = raw.rate_limit !== null && typeof raw.rate_limit === 'object';
  return {
    checked_at: timestampOrNull(raw.checked_at),
    next_check_at: timestampOrNull(raw.next_check_at),
    refresh_allowed_at: timestampOrNull(raw.refresh_allowed_at),
    rate_limit: hasRateLimit ? {
      limit: numberOrNull(rateLimitRaw.limit),
      remaining: numberOrNull(rateLimitRaw.remaining),
      reset_at: timestampOrNull(rateLimitRaw.reset_at),
    } : null,
    sources: Array.isArray(raw.sources) ? raw.sources.map(normalizeSourceUpdateItem) : [],
  };
}

function normalizeProviderOptions(value: unknown): MemoryProviderOption[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const raw = objectOrEmpty(item);
    const id = stringOrNull(raw.id);
    if (!id) return [];
    return [{
      id,
      label: stringOrNull(raw.label) ?? undefined,
      model: stringOrNull(raw.model) ?? undefined,
      type: stringOrNull(raw.type) ?? undefined,
    }];
  });
}

function memoryReloadStatus(value: unknown): MemoryConfigData['reload_status'] {
  return value === 'idle' || value === 'scheduled' || value === 'running' || value === 'failed'
    ? value
    : undefined;
}

function normalizeMemoryConfig(value: unknown): MemoryConfigData {
  const raw = objectOrEmpty(value);
  const providerRaw = objectOrEmpty(raw.providers ?? raw.provider_options);
  const configRaw = objectOrEmpty(raw.config ?? raw.values);
  const schemaRaw = objectOrEmpty(raw.schema);
  const constraintsRaw = objectOrEmpty(raw.constraints);
  const revision = stringOrNull(raw.revision);
  if (!revision) {
    throw new ApiClientError('INVALID_MEMORY_CONFIG', '记忆配置响应缺少有效 revision');
  }
  return {
    schema: schemaRaw,
    config: configRaw as MemoryConfigData['config'],
    values: configRaw as MemoryConfigData['values'],
    revision,
    runtime_id: stringOrNull(raw.runtime_id) ?? '',
    runtime_generation: numberOrNull(raw.runtime_generation) ?? undefined,
    reload_status: memoryReloadStatus(raw.reload_status),
    reload_failed: raw.reload_failed === true,
    providers: {
      llm: normalizeProviderOptions(providerRaw.llm ?? providerRaw.llm_providers),
      embedding: normalizeProviderOptions(providerRaw.embedding ?? providerRaw.embedding_providers),
    },
    constraints: constraintsRaw as MemoryConfigData['constraints'],
  };
}

function isRawObject(value: unknown): value is RawObject {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

function normalizeMemoryConfigSave(value: unknown): MemoryConfigSaveData {
  const raw = objectOrEmpty(value);
  const revision = stringOrNull(raw.revision);
  if (!revision) {
    throw new ApiClientError('INVALID_MEMORY_CONFIG_SAVE', '保存响应缺少有效 revision');
  }
  if (!isRawObject(raw.config)) {
    throw new ApiClientError('INVALID_MEMORY_CONFIG_SAVE', '保存响应缺少规范化 config');
  }
  return {
    config: raw.config as MemoryConfigData['config'],
    revision,
    old_runtime_id: stringOrNull(raw.old_runtime_id) ?? stringOrNull(raw.previous_runtime_id) ?? '',
    runtime_id: stringOrNull(raw.runtime_id) ?? undefined,
    reload_scheduled: raw.reload_scheduled === true,
    reload_pending: raw.reload_pending === true,
    reload_status: memoryReloadStatus(raw.reload_status),
    reload_failed: raw.reload_failed === true,
    manual_reload_required: raw.manual_reload_required === true,
    message: stringOrNull(raw.message) ?? undefined,
  };
}

function validateGroupResponse<T extends { group_id: string }>(value: T, expectedGroupId: string, label: string): T {
  if (value.group_id !== expectedGroupId) {
    throw new ApiClientError('GROUP_MISMATCH', `${label}响应不属于当前群组，请重试。`);
  }
  return value;
}

function normalizeSavedServer(value: unknown, idHint?: string): ServerRecord {
  const raw = (value ?? {}) as RawObject;
  return {
    id: String(raw.id ?? idHint ?? ''),
    name: typeof raw.name === 'string' ? raw.name : '',
    host: typeof raw.host === 'string' ? raw.host : '',
    created_time: numberOrNull(raw.created_time) ?? 0,
    last_success_time: numberOrNull(raw.last_success_time),
    last_failed_time: numberOrNull(raw.last_failed_time),
    failed_count: numberOrNull(raw.failed_count) ?? 0,
    status: 'unknown',
    version: null,
    latency: null,
    players: null,
    icon: null,
    queried_at: null,
  };
}

function normalizeServerCollection(raw: RawServersData): ServersData {
  const servers = Object.entries(raw.servers ?? {}).map(([id, server]) => normalizeSavedServer(server, id));
  return { group_id: raw.group_id, servers, last_manual_refresh_time: null };
}

function normalizeIcon(value: string | null) {
  if (!value) return null;
  return value.startsWith('data:') ? value : `data:image/png;base64,${value}`;
}

function applyStatus(saved: ServerRecord, status: RawStatusServer): ServerRecord {
  const online = status.state === 'online';
  return {
    ...saved,
    status: online ? 'online' : 'offline',
    version: status.version,
    latency: status.latency,
    players: online && status.players_online !== null && status.players_max !== null ? {
      online: status.players_online,
      max: status.players_max,
      sample: status.players_sample.map((name) => ({ name })),
    } : null,
    icon: normalizeIcon(status.icon_base64),
    queried_at: status.queried_at,
  };
}

async function loadServers(groupId: string, signal?: AbortSignal) {
  const raw = validateGroupResponse(
    await request<RawServersData>('/page/v1/mc/servers', { query: { group_id: groupId }, signal }),
    groupId,
    '服务器列表',
  );
  return normalizeServerCollection(raw);
}

function fillHourlyPoints(raw: RawTrendsData, points: Array<{ ts: number; count: number }>): TrendPoint[] {
  const currentBucket = Math.floor(raw.generated_at / 3600) * 3600;
  const start = currentBucket - (raw.hours - 1) * 3600;
  const byBucket = new Map(points.map((point) => [point.ts, point.count]));
  return Array.from({ length: raw.hours }, (_, index) => {
    const timestamp = start + index * 3600;
    return { timestamp, players: byBucket.get(timestamp) ?? null };
  });
}

const memoryScopes = new Set<MemoryObjectScope>(['user', 'persona', 'session', 'public', 'legacy_session']);
const memoryStatuses = new Set<MemoryObjectStatus>(['active', 'conflicted', 'archived', 'superseded']);
const memoryIndexStatuses = new Set<MemoryObject['index_status']>(['synced', 'pending', 'needs_repair', 'disabled']);

function stringValue(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : fallback;
}

function booleanValue(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

function numberValue(value: unknown, fallback = 0): number {
  return numberOrNull(value) ?? fallback;
}

function recordOrNull(value: unknown): Record<string, unknown> | null {
  return isRawObject(value) ? value : null;
}

export function normalizeMemoryObject(value: unknown): MemoryObject {
  const raw = objectOrEmpty(value);
  const owner = objectOrEmpty(raw.owner);
  const revision = objectOrEmpty(raw.current_revision ?? raw.revision);
  const scope = stringValue(raw.scope, 'persona');
  const status = stringValue(raw.status, 'active');
  const indexStatus = stringValue(raw.index_status, 'pending');
  return {
    memory_item_id: stringValue(raw.memory_item_id ?? raw.item_id ?? raw.id),
    owner_user_id: stringValue(raw.owner_user_id ?? owner.owner_user_id ?? owner.id),
    owner_display_name: stringOrNull(raw.owner_display_name ?? owner.display_name),
    scope: memoryScopes.has(scope as MemoryObjectScope) ? scope as MemoryObjectScope : 'persona',
    session_id: stringOrNull(raw.session_id),
    persona_id: stringOrNull(raw.persona_id),
    memory_type: stringValue(raw.memory_type ?? raw.type, 'GENERAL'),
    canonical_key: stringOrNull(raw.canonical_key),
    status: memoryStatuses.has(status as MemoryObjectStatus) ? status as MemoryObjectStatus : 'active',
    content: stringValue(raw.content ?? raw.text ?? revision.content),
    structured_payload: recordOrNull(raw.structured_payload ?? revision.structured_payload),
    current_revision_no: numberValue(raw.current_revision_no ?? raw.revision_no ?? revision.revision_no, 1),
    version: numberValue(raw.version, 1),
    importance: numberValue(raw.importance, 0.5),
    confidence: numberValue(raw.confidence, 0.5),
    useful_score: numberValue(raw.useful_score),
    group_safe: booleanValue(raw.group_safe),
    current_document_id: numberOrNull(raw.current_document_id),
    index_status: memoryIndexStatuses.has(indexStatus as MemoryObject['index_status']) ? indexStatus as MemoryObject['index_status'] : 'pending',
    conflict_count: numberValue(raw.conflict_count),
    source_count: numberValue(raw.source_count),
    relation_count: numberValue(raw.relation_count),
    created_at: timestampOrNull(raw.created_at ?? raw.create_time),
    updated_at: timestampOrNull(raw.updated_at),
  };
}

export function normalizeMemoryObjects(value: unknown): MemoryObjectsData {
  const raw = objectOrEmpty(value);
  const items = Array.isArray(raw.items) ? raw.items : Array.isArray(raw.objects) ? raw.objects : [];
  const page = numberValue(raw.page, 1);
  const pageSize = numberValue(raw.page_size, Math.max(items.length, 20));
  const total = numberValue(raw.total, items.length);
  return {
    items: items.map(normalizeMemoryObject).filter((item) => item.memory_item_id),
    total,
    page,
    page_size: pageSize,
    has_more: typeof raw.has_more === 'boolean' ? raw.has_more : page * pageSize < total,
  };
}

export function normalizeMemoryRevision(value: unknown): MemoryRevision {
  const raw = objectOrEmpty(value);
  return {
    memory_item_id: stringValue(raw.memory_item_id ?? raw.item_id),
    revision_no: numberValue(raw.revision_no ?? raw.revision, 1),
    operation: stringValue(raw.operation, 'update'),
    content: stringValue(raw.content ?? raw.text),
    structured_payload: recordOrNull(raw.structured_payload),
    base_version: numberOrNull(raw.base_version),
    actor: stringOrNull(raw.actor ?? raw.actor_user_id),
    reason: stringOrNull(raw.reason),
    created_at: timestampOrNull(raw.created_at),
  };
}

export function normalizeMemorySource(value: unknown): MemorySourceMessage {
  const raw = objectOrEmpty(value);
  return {
    source_id: stringValue(raw.source_id ?? raw.id),
    revision_no: numberValue(raw.revision_no, 1),
    source_type: stringValue(raw.source_type ?? raw.type, 'unknown'),
    document_id: numberOrNull(raw.document_id),
    message_id_start: stringOrNull(raw.message_id_start ?? raw.start_message_id),
    message_id_end: stringOrNull(raw.message_id_end ?? raw.end_message_id),
    session_id: stringOrNull(raw.session_id),
    platform_id: stringOrNull(raw.platform_id),
    content_snapshot: stringOrNull(raw.content_snapshot ?? raw.snapshot ?? raw.content),
    availability: stringValue(raw.availability, 'available'),
    created_at: timestampOrNull(raw.created_at),
  };
}

export function normalizeMemoryRelation(value: unknown): MemoryRelation {
  const raw = objectOrEmpty(value);
  return {
    relation_id: stringValue(raw.relation_id ?? raw.id),
    relation_type: stringValue(raw.relation_type ?? raw.type, 'related_to') as MemoryRelation['relation_type'],
    source_memory_item_id: stringValue(raw.source_memory_item_id ?? raw.source_item_id),
    target_memory_item_id: stringValue(raw.target_memory_item_id ?? raw.target_item_id),
    target_content: stringOrNull(raw.target_content),
    created_at: timestampOrNull(raw.created_at),
  };
}

export function normalizeMemoryConflict(value: unknown): MemoryConflict {
  const raw = objectOrEmpty(value);
  return {
    conflict_id: stringValue(raw.conflict_id ?? raw.id),
    owner_user_id: stringValue(raw.owner_user_id),
    conflict_type: stringValue(raw.conflict_type ?? raw.type, 'contradiction'),
    severity: stringValue(raw.severity, 'medium'),
    status: stringValue(raw.status, 'open') as MemoryConflict['status'],
    left_item: normalizeMemoryObject(raw.left_item ?? raw.left),
    right_item: normalizeMemoryObject(raw.right_item ?? raw.right),
    resolution: stringOrNull(raw.resolution),
    resolution_reason: stringOrNull(raw.resolution_reason ?? raw.reason),
    created_at: timestampOrNull(raw.created_at),
    resolved_at: timestampOrNull(raw.resolved_at),
  };
}

function normalizeMemoryObjectDetail(value: unknown): MemoryObjectDetailData {
  const raw = objectOrEmpty(value);
  const item = normalizeMemoryObject(raw.item ?? raw.object ?? value);
  return {
    item,
    relations: Array.isArray(raw.relations) ? raw.relations.map(normalizeMemoryRelation) : [],
    conflicts: Array.isArray(raw.conflicts) ? raw.conflicts.map(normalizeMemoryConflict) : [],
  };
}

function normalizeIdentityAlias(value: unknown): MemoryIdentityAlias {
  const raw = objectOrEmpty(value);
  return {
    identity_link_id: stringValue(raw.identity_link_id ?? raw.alias_id ?? raw.id),
    owner_user_id: stringValue(raw.owner_user_id),
    platform_id: stringValue(raw.platform_id),
    bot_id: stringValue(raw.bot_id),
    external_user_id: stringValue(raw.external_user_id),
    verified: booleanValue(raw.verified),
    source: stringValue(raw.source, 'manual'),
    status: stringValue(raw.status, 'active'),
    created_at: timestampOrNull(raw.created_at),
    updated_at: timestampOrNull(raw.updated_at),
  };
}

function normalizeOwner(value: unknown): MemoryOwner {
  const raw = objectOrEmpty(value);
  const aliases = Array.isArray(raw.aliases) ? raw.aliases.map(normalizeIdentityAlias) : [];
  return {
    owner_user_id: stringValue(raw.owner_user_id ?? raw.id),
    display_name: stringValue(raw.display_name ?? raw.name),
    status: stringValue(raw.status, 'active') as MemoryOwner['status'],
    aliases,
    created_at: timestampOrNull(raw.created_at),
    updated_at: timestampOrNull(raw.updated_at),
    expected_updated_at: stringValue(raw.expected_updated_at),
  };
}

function normalizeIdentities(value: unknown): MemoryIdentitiesData {
  const raw = objectOrEmpty(value);
  const owners = Array.isArray(raw.owners) ? raw.owners.map(normalizeOwner) : [];
  const unmapped = Array.isArray(raw.unmapped_aliases) ? raw.unmapped_aliases.map(normalizeIdentityAlias) : [];
  return { owners, unmapped_aliases: unmapped, total: numberValue(raw.total, owners.length) };
}

function normalizeExpectedVersions(
  value: unknown,
  requiredItemIds: string[],
): Record<string, number> {
  if (!isRawObject(value)) {
    throw new ApiClientError('INVALID_MEMORY_MERGE_PREVIEW', '对象合并预览缺少 expected_versions');
  }
  const versions: Record<string, number> = {};
  Object.entries(value).forEach(([itemId, rawVersion]) => {
    const version = numberOrNull(rawVersion);
    if (!itemId.trim() || version === null || !Number.isInteger(version) || version < 1) {
      throw new ApiClientError('INVALID_MEMORY_MERGE_PREVIEW', '对象合并预览 expected_versions 格式无效');
    }
    versions[itemId] = version;
  });
  if (requiredItemIds.some((itemId) => versions[itemId] === undefined)) {
    throw new ApiClientError('INVALID_MEMORY_MERGE_PREVIEW', '对象合并预览缺少完整 expected_versions');
  }
  return versions;
}

function normalizeOwnerMergeStates(value: unknown): MemoryOwnerMergePreviewData['expected_owner_states'] {
  if (!isRawObject(value)) {
    throw new ApiClientError('INVALID_OWNER_MERGE_PREVIEW', 'Owner 合并预览缺少 expected_owner_states');
  }
  const states: MemoryOwnerMergePreviewData['expected_owner_states'] = {};
  Object.entries(value).forEach(([ownerUserId, rawState]) => {
    if (!ownerUserId.trim() || !isRawObject(rawState)) {
      throw new ApiClientError('INVALID_OWNER_MERGE_PREVIEW', 'Owner 合并预览状态格式无效');
    }
    const state = Object.fromEntries(
      Object.entries(rawState).flatMap(([key, item]) => (
        (typeof item === 'string' || typeof item === 'number') && typeof item !== 'boolean'
          ? [[key, String(item)]]
          : []
      )),
    );
    if (!stringValue(state.status) || !stringValue(state.updated_at)) {
      throw new ApiClientError('INVALID_OWNER_MERGE_PREVIEW', 'Owner 合并预览状态缺少 status 或 updated_at');
    }
    states[ownerUserId] = state as MemoryOwnerMergePreviewData['expected_owner_states'][string];
  });
  if (!Object.keys(states).length) {
    throw new ApiClientError('INVALID_OWNER_MERGE_PREVIEW', 'Owner 合并预览状态为空');
  }
  return states;
}

function normalizeMaintenance(value: unknown): MemoryMaintenanceStatusData {
  const raw = objectOrEmpty(value);
  const migration = objectOrEmpty(raw.migration ?? raw.migration_status);
  const index = objectOrEmpty(raw.index ?? raw.index_status);
  const sources = objectOrEmpty(raw.sources ?? raw.source_coverage);
  const totalItems = numberValue(sources.total_items);
  const coveredItems = numberValue(sources.covered_items);
  return {
    migration: {
      state: stringValue(migration.state ?? migration.status, 'idle'),
      processed: numberValue(migration.processed),
      total: numberValue(migration.total),
      created: numberValue(migration.created),
      deduped: numberValue(migration.deduped),
      skipped: numberValue(migration.skipped),
      conflicted: numberValue(migration.conflicted),
      errors: numberValue(migration.errors ?? migration.error_count),
      unresolved_owner_count: numberValue(migration.unresolved_owner_count ?? migration.unresolved_owners),
    },
    index: {
      state: stringValue(index.state ?? index.status, 'synced'),
      synced_count: numberValue(index.synced_count),
      pending_count: numberValue(index.pending_count),
      needs_repair_count: numberValue(index.needs_repair_count),
      disabled_count: numberValue(index.disabled_count),
      last_success_at: timestampOrNull(index.last_success_at),
      last_error: stringOrNull(index.last_error),
    },
    sources: {
      total_items: totalItems,
      covered_items: coveredItems,
      partial_items: numberValue(sources.partial_items),
      unavailable_items: numberValue(sources.unavailable_items),
      coverage_ratio: numberValue(sources.coverage_ratio, totalItems ? coveredItems / totalItems : 0),
    },
  };
}

export const apiClient = {
  bootstrap: async (signal?: AbortSignal): Promise<BootstrapData> => {
    const raw = await request<RawBootstrapData>('/page/v1/bootstrap', { signal });
    const unavailable = { available: false, enabled: false, initialized: false, error: null };
    return {
      brand: raw.brand ?? 'Zhouyi Dashboard',
      groups: raw.groups.map((group) => ({ group_id: group.id, label: group.id })),
      default_group_id: raw.selected_group_id,
      capabilities: raw.capabilities ?? {
        mc: { available: true, enabled: true, initialized: true, error: null },
        memory: unavailable,
      },
    };
  },
  servers: loadServers,
  settings: (groupId: string, signal?: AbortSignal): Promise<SettingsData> => request('/page/v1/mc/settings', {
    query: { group_id: groupId }, signal,
  }),
  previewSettings: (input: SettingsMutationInput, signal?: AbortSignal): Promise<SettingsPreviewData> => request('/page/v1/mc/settings/preview', {
    method: 'POST', body: input, signal,
  }),
  saveSettings: (input: SettingsMutationInput, signal?: AbortSignal): Promise<SettingsSaveData> => request('/page/v1/mc/settings', {
    method: 'POST', body: input, signal, mutationKey: `settings:${input.scope}:${input.group_id ?? 'global'}`,
  }),
  addServer: async (input: AddServerInput, signal?: AbortSignal): Promise<ServerMutationData> => {
    const raw = validateGroupResponse(await request<RawMutationData>('/page/v1/mc/servers/add', {
      method: 'POST', body: input, signal, mutationKey: `add:${input.group_id}`,
    }), input.group_id, '新增服务器');
    return { server: normalizeSavedServer(raw.server) };
  },
  updateServer: async (input: UpdateServerInput, signal?: AbortSignal): Promise<ServerMutationData> => {
    const raw = validateGroupResponse(await request<RawMutationData>('/page/v1/mc/servers/update', {
      method: 'POST',
      body: { group_id: input.group_id, identifier: input.server_id, name: input.name, host: input.host },
      signal,
      mutationKey: `update:${input.group_id}:${input.server_id}`,
    }), input.group_id, '更新服务器');
    return { server: normalizeSavedServer(raw.server) };
  },
  deleteServer: async (input: DeleteServerInput, signal?: AbortSignal): Promise<DeleteServerData> => {
    const raw = validateGroupResponse(await request<RawDeleteData>('/page/v1/mc/servers/delete', {
      method: 'POST',
      body: { group_id: input.group_id, identifier: input.server_id, confirm: true },
      signal,
      mutationKey: `delete:${input.group_id}:${input.server_id}`,
    }), input.group_id, '删除服务器');
    return {
      deleted_server_id: String(raw.server.id ?? input.server_id),
      trend_cascade_deleted: raw.trend_cascade_deleted,
      trend_existed: raw.trend_existed,
    };
  },
  refreshStatus: async (input: RefreshStatusInput, signal?: AbortSignal): Promise<RefreshStatusData> => {
    const raw = await request<RawStatusData>('/page/v1/mc/status', {
      method: 'POST',
      body: { group_id: input.group_id, identifier: input.server_id },
      signal,
      mutationKey: `status:${input.group_id}:${input.server_id ?? 'all'}`,
    });
    const saved = await loadServers(input.group_id, signal);
    const savedById = new Map(saved.servers.map((server) => [server.id, server]));
    return {
      group_id: raw.group_id,
      refreshed_at: raw.queried_at,
      servers: raw.servers.map((status) => {
        const id = String(status.id);
        return applyStatus(savedById.get(id) ?? normalizeSavedServer(status, id), status);
      }),
    };
  },
  trends: async (groupId: string, serverId: string | undefined, hours: number, signal?: AbortSignal): Promise<TrendsData> => {
    const raw = await request<RawTrendsData>('/page/v1/mc/trends', {
      query: { group_id: groupId, identifier: serverId, hours }, signal,
    });
    return {
      group_id: raw.group_id,
      hours: raw.hours,
      results: raw.servers.map((result) => {
        const server = normalizeSavedServer(result.server);
        return {
          server: { id: server.id, name: server.name, host: server.host },
          latest: result.latest,
          max: result.max,
          average: result.average,
          count: result.count,
          points: fillHourlyPoints(raw, result.points),
        };
      }),
    };
  },
  memoryConfig: async (signal?: AbortSignal): Promise<MemoryConfigData> => normalizeMemoryConfig(
    await request<unknown>('/page/v1/config/memory', { signal }),
  ),
  saveMemoryConfig: async (input: MemoryConfigMutationInput, signal?: AbortSignal): Promise<MemoryConfigSaveData> => normalizeMemoryConfigSave(
    await request<unknown>('/page/v1/config/memory', {
      method: 'POST',
      body: input,
      signal,
      mutationKey: 'memory-config:save',
    }),
  ),
  sourceUpdates: async (signal?: AbortSignal): Promise<SourceUpdatesData> => normalizeSourceUpdates(
    await request<unknown>('/page/v1/sources/updates', { signal }),
  ),
  refreshSourceUpdates: async (signal?: AbortSignal): Promise<SourceUpdatesData> => normalizeSourceUpdates(
    await request<unknown>('/page/v1/sources/updates/refresh', {
      method: 'POST',
      body: { force: true },
      signal,
      mutationKey: 'source-updates:refresh',
    }),
  ),
  cleanup: async (groupId: string, execute: boolean, signal?: AbortSignal): Promise<CleanupData> => {
    if (!execute) {
      const raw = await request<RawCleanupPreviewData>('/page/v1/mc/cleanup', {
        query: { group_id: groupId }, signal,
      });
      return { mode: 'preview', candidates: raw.candidates, deleted_count: 0 };
    }
    const raw = await request<RawCleanupExecuteData>('/page/v1/mc/cleanup', {
      method: 'POST',
      body: { group_id: groupId, confirm: true },
      signal,
      mutationKey: `cleanup:${groupId}:execute`,
    });
    return { mode: 'execute', candidates: raw.deleted, deleted_count: raw.deleted_count };
  },
};

export function memoryGet<T>(path: string, query?: Query, signal?: AbortSignal): Promise<T> {
  return request<T>(`/page/v1/memory/${path.replace(/^\/+/, '')}`, { query, signal });
}

export function memoryPost<T>(
  path: string,
  body: unknown,
  signal?: AbortSignal,
  mutationKey?: string,
): Promise<T> {
  return request<T>(`/page/v1/memory/${path.replace(/^\/+/, '')}`, {
    method: 'POST',
    body,
    signal,
    mutationKey,
  });
}

function memoryObjectQuery(filters: MemoryObjectFilters): Query {
  return {
    page: filters.page,
    page_size: filters.page_size,
    owner_user_id: requireOwnerUserId(filters.owner_user_id),
    keyword: filters.keyword,
    scope: filters.scope === 'all' ? undefined : filters.scope,
    persona_id: filters.persona_id,
    status: filters.status === 'all' ? undefined : filters.status,
    memory_type: filters.memory_type && filters.memory_type !== 'all' ? filters.memory_type : undefined,
    conflict: filters.conflict === 'all' ? undefined : filters.conflict,
    index_status: filters.index_status === 'all' ? undefined : filters.index_status,
    sort: filters.sort,
  };
}

export const memoryAdminClient = {
  objects: async (filters: MemoryObjectFilters, signal?: AbortSignal): Promise<MemoryObjectsData> => normalizeMemoryObjects(
    await memoryGet<unknown>(MEMORY_ADMIN_PATHS.objects, memoryObjectQuery(filters), signal),
  ),
  objectDetail: async (ownerUserId: string, memoryItemId: string, signal?: AbortSignal): Promise<MemoryObjectDetailData> => normalizeMemoryObjectDetail(
    await memoryGet<unknown>(MEMORY_ADMIN_PATHS.objectDetail, {
      owner_user_id: requireOwnerUserId(ownerUserId),
      memory_item_id: memoryItemId,
    }, signal),
  ),
  revisions: async (ownerUserId: string, memoryItemId: string, signal?: AbortSignal): Promise<MemoryRevision[]> => {
    const raw = await memoryGet<unknown>(MEMORY_ADMIN_PATHS.objectRevisions, {
      owner_user_id: requireOwnerUserId(ownerUserId),
      memory_item_id: memoryItemId,
    }, signal);
    const value = objectOrEmpty(raw);
    const items = Array.isArray(raw) ? raw : Array.isArray(value.items) ? value.items : Array.isArray(value.revisions) ? value.revisions : [];
    return items.map(normalizeMemoryRevision);
  },
  sources: async (ownerUserId: string, memoryItemId: string, revisionNo?: number, signal?: AbortSignal): Promise<MemorySourceMessage[]> => {
    const raw = await memoryGet<unknown>(MEMORY_ADMIN_PATHS.objectSources, {
      owner_user_id: requireOwnerUserId(ownerUserId),
      memory_item_id: memoryItemId,
      revision_no: revisionNo,
    }, signal);
    const value = objectOrEmpty(raw);
    const items = Array.isArray(raw) ? raw : Array.isArray(value.items) ? value.items : Array.isArray(value.sources) ? value.sources : [];
    return items.map(normalizeMemorySource);
  },
  createObject: async (input: MemoryObjectMutationInput, signal?: AbortSignal): Promise<MemoryObjectDetailData> => {
    if (input.expected_version !== 0) {
      throw new ApiClientError('MEMORY_REVISION_CONFLICT', '新建对象的 expected_version 必须为 0');
    }
    const body = { ...input, owner_user_id: requireOwnerUserId(input.owner_user_id) };
    return normalizeMemoryObjectDetail(await memoryPost<unknown>(
      MEMORY_ADMIN_PATHS.objectCreate,
      body,
      signal,
      `memory-object:create:${body.owner_user_id}:${body.canonical_key ?? body.content}`,
    ));
  },
  updateObject: async (input: MemoryObjectUpdateInput, signal?: AbortSignal): Promise<MemoryObjectDetailData> => {
    const body = { ...input, owner_user_id: requireOwnerUserId(input.owner_user_id) };
    return normalizeMemoryObjectDetail(await memoryPost<unknown>(
      MEMORY_ADMIN_PATHS.objectUpdate,
      body,
      signal,
      `memory-object:update:${body.owner_user_id}:${body.memory_item_id}:${body.expected_version}`,
    ));
  },
  archiveObject: async (ownerUserId: string, memoryItemId: string, expectedVersion: number, reason?: string, signal?: AbortSignal): Promise<MemoryObjectDetailData> => normalizeMemoryObjectDetail(
    await memoryPost<unknown>(MEMORY_ADMIN_PATHS.objectArchive, {
      owner_user_id: requireOwnerUserId(ownerUserId),
      memory_item_id: memoryItemId,
      expected_version: expectedVersion,
      reason,
    }, signal, `memory-object:archive:${ownerUserId}:${memoryItemId}:${expectedVersion}`),
  ),
  batchObjects: (input: MemoryObjectBatchInput, signal?: AbortSignal): Promise<unknown> => {
    const body = { ...input, owner_user_id: requireOwnerUserId(input.owner_user_id) };
    return memoryPost(
      MEMORY_ADMIN_PATHS.objectBatch,
      body,
      signal,
      `memory-object:batch:${body.owner_user_id}:${body.action}:${body.items.map((item) => `${item.memory_item_id}@${item.expected_version}`).join(',')}`,
    );
  },
  mergePreview: async (input: MemoryMergePreviewInput, signal?: AbortSignal): Promise<MemoryMergePreviewData> => {
    const body = { ...input, owner_user_id: requireOwnerUserId(input.owner_user_id) };
    const raw = objectOrEmpty(await memoryPost<unknown>(
      MEMORY_ADMIN_PATHS.objectMergePreview,
      body,
      signal,
      `memory-object:merge-preview:${body.owner_user_id}:${body.source_memory_item_ids.join(',')}`,
    ));
    const survivorMemoryItemId = stringValue(
      raw.survivor_memory_item_id,
      body.survivor_memory_item_id,
    );
    const sourceMemoryItemIds = Array.isArray(raw.source_memory_item_ids)
      ? raw.source_memory_item_ids.map(String)
      : body.source_memory_item_ids;
    return {
      owner_user_id: stringValue(raw.owner_user_id, body.owner_user_id),
      survivor_memory_item_id: survivorMemoryItemId,
      source_memory_item_ids: sourceMemoryItemIds,
      merged_content: stringValue(raw.merged_content ?? raw.content),
      merged_structured_payload: recordOrNull(raw.merged_structured_payload ?? raw.structured_payload),
      warnings: Array.isArray(raw.warnings) ? raw.warnings.map(String) : [],
      expected_versions: normalizeExpectedVersions(
        raw.expected_versions,
        [survivorMemoryItemId, ...sourceMemoryItemIds],
      ),
    };
  },
  merge: async (input: MemoryMergeInput, signal?: AbortSignal): Promise<MemoryObjectDetailData> => {
    const body = { ...input, owner_user_id: requireOwnerUserId(input.owner_user_id) };
    return normalizeMemoryObjectDetail(await memoryPost<unknown>(
      MEMORY_ADMIN_PATHS.objectMerge,
      body,
      signal,
      `memory-object:merge:${body.owner_user_id}:${body.survivor_memory_item_id}:${Object.values(body.expected_versions).join(',')}`,
    ));
  },
  supersede: async (input: MemorySupersedeInput, signal?: AbortSignal): Promise<MemoryObjectDetailData> => {
    const body = { ...input, owner_user_id: requireOwnerUserId(input.owner_user_id) };
    return normalizeMemoryObjectDetail(await memoryPost<unknown>(
      MEMORY_ADMIN_PATHS.objectSupersede,
      body,
      signal,
      `memory-object:supersede:${body.owner_user_id}:${body.old_memory_item_id}:${body.new_memory_item_id}`,
    ));
  },
  conflicts: async (ownerUserId: string, status = 'open', signal?: AbortSignal): Promise<MemoryConflict[]> => {
    const raw = await memoryGet<unknown>(MEMORY_ADMIN_PATHS.conflicts, {
      owner_user_id: requireOwnerUserId(ownerUserId),
      status,
    }, signal);
    const value = objectOrEmpty(raw);
    const items = Array.isArray(raw) ? raw : Array.isArray(value.items) ? value.items : Array.isArray(value.conflicts) ? value.conflicts : [];
    return items.map(normalizeMemoryConflict);
  },
  resolveConflict: (input: MemoryConflictResolveInput, signal?: AbortSignal): Promise<unknown> => {
    const body = { ...input, owner_user_id: requireOwnerUserId(input.owner_user_id) };
    return memoryPost(
      MEMORY_ADMIN_PATHS.conflictResolve,
      body,
      signal,
      `memory-conflict:${body.owner_user_id}:${body.conflict_id}:${body.action}`,
    );
  },
  identities: async (signal?: AbortSignal): Promise<MemoryIdentitiesData> => normalizeIdentities(
    await memoryGet<unknown>(MEMORY_ADMIN_PATHS.identities, undefined, signal),
  ),
  createOwner: (displayName: string, signal?: AbortSignal): Promise<unknown> => memoryPost(
    MEMORY_ADMIN_PATHS.ownerCreate,
    { display_name: displayName },
    signal,
    `memory-owner:create:${displayName}`,
  ),
  updateOwner: (input: MemoryOwnerUpdateInput, signal?: AbortSignal): Promise<unknown> => {
    const body = { ...input, owner_user_id: requireOwnerUserId(input.owner_user_id) };
    return memoryPost(
      MEMORY_ADMIN_PATHS.ownerUpdate,
      body,
      signal,
      `memory-owner:update:${body.owner_user_id}:${body.expected_updated_at}`,
    );
  },
  linkAlias: (input: { owner_user_id: string; platform_id: string; bot_id: string; external_user_id: string; verified?: boolean }, signal?: AbortSignal): Promise<unknown> => {
    const body = { ...input, owner_user_id: requireOwnerUserId(input.owner_user_id) };
    return memoryPost(
      MEMORY_ADMIN_PATHS.aliasLink,
      body,
      signal,
      `memory-alias:link:${body.owner_user_id}:${body.platform_id}:${body.bot_id}:${body.external_user_id}`,
    );
  },
  moveAlias: (input: MemoryAliasMoveInput, signal?: AbortSignal): Promise<unknown> => {
    const body = {
      ...input,
      owner_user_id: requireOwnerUserId(input.owner_user_id),
      expected_owner_user_id: requireOwnerUserId(input.expected_owner_user_id),
    };
    return memoryPost(
      MEMORY_ADMIN_PATHS.aliasMove,
      body,
      signal,
      `memory-alias:move:${body.identity_link_id}:${body.expected_owner_user_id}:${body.owner_user_id}`,
    );
  },
  ownerMergePreview: async (survivorOwnerUserId: string, sourceOwnerUserIds: string[], signal?: AbortSignal): Promise<MemoryOwnerMergePreviewData> => {
    const survivor = requireOwnerUserId(survivorOwnerUserId);
    const sources = sourceOwnerUserIds.map(requireOwnerUserId);
    const raw = objectOrEmpty(await memoryPost<unknown>(
      MEMORY_ADMIN_PATHS.ownerMergePreview,
      { survivor_owner_user_id: survivor, source_owner_user_ids: sources },
      signal,
      `memory-owner:merge-preview:${survivor}:${sources.join(',')}`,
    ));
    const previewId = stringValue(raw.preview_id);
    if (!previewId) {
      throw new ApiClientError('INVALID_OWNER_MERGE_PREVIEW', 'Owner 合并预览缺少 preview_id');
    }
    return {
      preview_id: previewId,
      survivor_owner_user_id: stringValue(raw.survivor_owner_user_id, survivor),
      source_owner_user_ids: Array.isArray(raw.source_owner_user_ids) ? raw.source_owner_user_ids.map(String) : sources,
      alias_count: numberValue(raw.alias_count),
      memory_item_count: numberValue(raw.memory_item_count),
      conflict_count: numberValue(raw.conflict_count),
      warnings: Array.isArray(raw.warnings) ? raw.warnings.map(String) : [],
      expected_owner_states: normalizeOwnerMergeStates(raw.expected_owner_states),
    };
  },
  mergeOwners: (input: MemoryOwnerMergeInput, signal?: AbortSignal): Promise<unknown> => {
    const body = {
      ...input,
      survivor_owner_user_id: requireOwnerUserId(input.survivor_owner_user_id),
      source_owner_user_ids: input.source_owner_user_ids.map(requireOwnerUserId),
    };
    return memoryPost(
      MEMORY_ADMIN_PATHS.ownerMerge,
      body,
      signal,
      `memory-owner:merge:${body.survivor_owner_user_id}:${body.preview_id}`,
    );
  },
  maintenance: async (signal?: AbortSignal): Promise<MemoryMaintenanceStatusData> => normalizeMaintenance(
    await memoryGet<unknown>(MEMORY_ADMIN_PATHS.maintenanceStatus, undefined, signal),
  ),
  retryIndex: (input: MemoryIndexRetryInput, signal?: AbortSignal): Promise<unknown> => {
    const body = { ...input, owner_user_id: requireOwnerUserId(input.owner_user_id) };
    return memoryPost(
      MEMORY_ADMIN_PATHS.indexRetry,
      body,
      signal,
      `memory-index:retry:${body.owner_user_id}:${body.items.map((item) => `${item.memory_item_id}@${item.expected_version}`).join(',')}`,
    );
  },
};

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
  MemoryConfigData,
  MemoryConfigMutationInput,
  MemoryConfigSaveData,
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

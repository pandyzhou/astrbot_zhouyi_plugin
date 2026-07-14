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
  RefreshStatusData,
  RefreshStatusInput,
  ServerMutationData,
  ServerRecord,
  ServersData,
  SettingsData,
  SettingsMutationInput,
  SettingsPreviewData,
  SettingsSaveData,
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

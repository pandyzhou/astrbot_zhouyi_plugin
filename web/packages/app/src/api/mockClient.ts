import type { ApiEnvelope, CleanupCandidate } from './types';

type Query = Record<string, string | number | boolean | undefined>;

interface SavedServer {
  id: number;
  name: string;
  host: string;
  created_time: number;
  last_success_time: number | null;
  last_failed_time: number | null;
  failed_count: number;
}

interface RawTrendPoint {
  ts: number;
  count: number;
}

const CLEANUP_DAYS = 10;
const now = () => Math.floor(Date.now() / 1000);
const bucketNow = () => Math.floor(now() / 3600) * 3600;
const hoursAgo = (hours: number) => now() - hours * 3600;

const groups = ['10001', '10002'];
const serverDb: Record<string, Record<string, SavedServer>> = {
  '10001': {
    '1': {
      id: 1,
      name: '暮色群岛',
      host: 'play.example.test:25565',
      created_time: hoursAgo(720),
      last_success_time: hoursAgo(1),
      last_failed_time: null,
      failed_count: 0,
    },
    '2': {
      id: 2,
      name: '红石工坊',
      host: 'redstone.example.test:25565',
      created_time: hoursAgo(480),
      last_success_time: hoursAgo(260),
      last_failed_time: hoursAgo(12),
      failed_count: 4,
    },
  },
  '10002': {
    '1': {
      id: 1,
      name: '建筑测试区',
      host: 'build.example.test:25565',
      created_time: hoursAgo(220),
      last_success_time: hoursAgo(3),
      last_failed_time: hoursAgo(1),
      failed_count: 1,
    },
  },
};

const trendDb: Record<string, Record<string, RawTrendPoint[]>> = {
  '10001': { '1': [] },
  '10002': { '1': [] },
};

function seedTrend(groupId: string, serverId: string, offset: number) {
  const points: RawTrendPoint[] = [];
  const current = bucketNow();
  for (let index = 167; index >= 0; index -= 1) {
    if (index % (19 + offset) === 0) continue;
    const count = Math.max(0, Math.round(11 + Math.sin((168 - index + offset) / 5) * 7 + offset * 3));
    points.push({ ts: current - index * 3600, count });
  }
  trendDb[groupId] ??= {};
  trendDb[groupId][serverId] = points;
}

seedTrend('10001', '1', 0);
seedTrend('10002', '1', 1);

function clone<T>(value: T): T {
  return structuredClone(value);
}

function ok<T>(data: T): ApiEnvelope<T> {
  return { success: true, data: clone(data) };
}

function fail<T>(code: string, message: string): ApiEnvelope<T> {
  return { success: false, error: { code, message } };
}

async function wait(signal?: AbortSignal) {
  if (signal?.aborted) throw new DOMException('请求已取消', 'AbortError');
  await new Promise<void>((resolve, reject) => {
    const finish = () => {
      signal?.removeEventListener('abort', onAbort);
      resolve();
    };
    const timer = window.setTimeout(finish, 120);
    const onAbort = () => {
      window.clearTimeout(timer);
      reject(new DOMException('请求已取消', 'AbortError'));
    };
    signal?.addEventListener('abort', onAbort, { once: true });
  });
}

function serversFor(groupId: string) {
  return serverDb[groupId];
}

function findServer(groupId: string, identifier: unknown): [string, SavedServer] | null {
  const servers = serversFor(groupId);
  if (!servers) return null;
  const value = String(identifier ?? '');
  if (servers[value]) return [value, servers[value]];
  const entry = Object.entries(servers).find(([, server]) => server.name === value);
  return entry ?? null;
}

function nextId(groupId: string) {
  const ids = Object.keys(serversFor(groupId) ?? {}).map(Number).filter(Number.isFinite);
  return (ids.length ? Math.max(...ids) : 0) + 1;
}

function cleanupCandidates(groupId: string): CleanupCandidate[] {
  const cutoff = now() - CLEANUP_DAYS * 24 * 3600;
  return Object.entries(serversFor(groupId) ?? {}).flatMap(([id, server]) => {
    const history = trendDb[groupId]?.[id] ?? [];
    const latestTrend = history.at(-1)?.ts ?? 0;
    const effective = Math.max(server.last_success_time ?? 0, latestTrend);
    if (effective >= cutoff) return [];
    return [{
      id,
      name: server.name,
      host: server.host,
      last_success_time: server.last_success_time,
      effective_last_success_time: effective || null,
      failed_count: server.failed_count,
    }];
  });
}

function appendTrend(groupId: string, serverId: string, count: number) {
  trendDb[groupId] ??= {};
  trendDb[groupId][serverId] ??= [];
  const bucket = bucketNow();
  const history = trendDb[groupId][serverId];
  const existing = history.find((point) => point.ts === bucket);
  if (existing) existing.count = count;
  else history.push({ ts: bucket, count });
}

export async function mockRequest<T>(
  endpoint: string,
  method: 'GET' | 'POST',
  query: Query | undefined,
  body: unknown,
  signal?: AbortSignal,
): Promise<ApiEnvelope<T>> {
  await wait(signal);

  if (method === 'GET' && endpoint === '/page/bootstrap') {
    return ok({ groups: groups.map((id) => ({ id })), selected_group_id: groups[0] ?? null }) as ApiEnvelope<T>;
  }

  if (method === 'GET' && endpoint === '/page/servers') {
    const groupId = String(query?.group_id ?? '');
    const servers = serversFor(groupId);
    if (!servers) return fail('GROUP_NOT_FOUND', '群组不存在');
    return ok({ group_id: groupId, servers }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/servers/add') {
    const input = body as { group_id: string; name: string; host: string; force?: boolean };
    const servers = serversFor(input.group_id);
    if (!servers) return fail('GROUP_NOT_FOUND', '群组不存在');
    if (Object.values(servers).some((server) => server.name === input.name)) return fail('DUPLICATE_NAME', '已存在同名服务器');
    if (Object.values(servers).some((server) => server.host === input.host)) return fail('DUPLICATE_HOST', '已存在相同地址的服务器');
    if (!input.force && input.host.includes('offline')) return fail('PROBE_FAILED', '服务器预探测失败；确认地址后可使用 force=true 强制添加');
    const id = nextId(input.group_id);
    const server: SavedServer = {
      id,
      name: input.name,
      host: input.host,
      created_time: now(),
      last_success_time: now(),
      last_failed_time: null,
      failed_count: 0,
    };
    servers[String(id)] = server;
    return ok({ group_id: input.group_id, server }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/servers/update') {
    const input = body as { group_id: string; identifier: string; name?: string; host?: string };
    const found = findServer(input.group_id, input.identifier);
    if (!found) return fail('SERVER_NOT_FOUND', '服务器不存在');
    const [id, current] = found;
    const servers = serversFor(input.group_id)!;
    const name = input.name ?? current.name;
    const host = input.host ?? current.host;
    if (Object.entries(servers).some(([otherId, server]) => otherId !== id && server.name === name)) return fail('DUPLICATE_NAME', '已存在同名服务器');
    if (Object.entries(servers).some(([otherId, server]) => otherId !== id && server.host === host)) return fail('DUPLICATE_HOST', '已存在相同地址的服务器');
    const server = { ...current, name, host };
    servers[id] = server;
    return ok({ group_id: input.group_id, server }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/servers/delete') {
    const input = body as { group_id: string; identifier: string; confirm: boolean };
    if (input.confirm !== true) return fail('CONFIRM_REQUIRED', '删除服务器必须显式设置 confirm=true');
    const found = findServer(input.group_id, input.identifier);
    if (!found) return fail('SERVER_NOT_FOUND', '服务器不存在');
    const [id, server] = found;
    const trendExisted = Boolean(trendDb[input.group_id]?.[id]);
    delete serverDb[input.group_id][id];
    delete trendDb[input.group_id]?.[id];
    return ok({
      group_id: input.group_id,
      deleted: true,
      server,
      trend_cascade_deleted: true,
      trend_existed: trendExisted,
    }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/status') {
    const input = body as { group_id: string; identifier?: string };
    const servers = serversFor(input.group_id);
    if (!servers) return fail('GROUP_NOT_FOUND', '群组不存在');
    const selected = input.identifier
      ? [findServer(input.group_id, input.identifier)].filter((value): value is [string, SavedServer] => value !== null)
      : Object.entries(servers).sort(([left], [right]) => Number(left) - Number(right));
    if (input.identifier && !selected.length) return fail('SERVER_NOT_FOUND', '服务器不存在');
    const queriedAt = now();
    const results = selected.map(([id, server]) => {
      const online = !server.name.includes('红石');
      if (online) {
        server.last_success_time = queriedAt;
        server.failed_count = 0;
        const playersOnline = id === '1' ? 18 : 7;
        appendTrend(input.group_id, id, playersOnline);
        return {
          id: server.id,
          name: server.name,
          host: server.host,
          state: 'online',
          online: true,
          queried_at: queriedAt,
          latency: id === '1' ? 42 : 68,
          version: '1.21.4',
          players_online: playersOnline,
          players_max: 60,
          players_sample: ['Alex', 'Steve', 'Builder_01'],
          players_sample_complete: false,
          icon_base64: null,
        };
      }
      server.last_failed_time = queriedAt;
      server.failed_count += 1;
      return {
        id: server.id,
        name: server.name,
        host: server.host,
        state: 'unreachable',
        online: false,
        queried_at: queriedAt,
        latency: null,
        version: null,
        players_online: null,
        players_max: null,
        players_sample: [],
        players_sample_complete: false,
        icon_base64: null,
      };
    });
    return ok({ group_id: input.group_id, queried_at: queriedAt, servers: results }) as ApiEnvelope<T>;
  }

  if (method === 'GET' && endpoint === '/page/trends') {
    const groupId = String(query?.group_id ?? '');
    const servers = serversFor(groupId);
    if (!servers) return fail('GROUP_NOT_FOUND', '群组不存在');
    const hours = Number(query?.hours ?? 24);
    if (!Number.isInteger(hours) || hours < 1 || hours > 168) return fail('INVALID_HOURS', 'hours 必须在 1-168 之间');
    const selected = query?.identifier
      ? [findServer(groupId, query.identifier)].filter((value): value is [string, SavedServer] => value !== null)
      : Object.entries(servers).sort(([left], [right]) => Number(left) - Number(right));
    if (query?.identifier && !selected.length) return fail('SERVER_NOT_FOUND', '服务器不存在');
    const generatedAt = now();
    const currentBucket = Math.floor(generatedAt / 3600) * 3600;
    const cutoff = currentBucket - (hours - 1) * 3600;
    const results = selected.map(([id, server]) => {
      const points = (trendDb[groupId]?.[id] ?? []).filter((point) => cutoff <= point.ts && point.ts <= currentBucket);
      const counts = points.map((point) => point.count);
      return {
        server,
        points,
        latest: counts.at(-1) ?? null,
        max: counts.length ? Math.max(...counts) : null,
        average: counts.length ? Math.round(counts.reduce((sum, count) => sum + count, 0) / counts.length * 100) / 100 : null,
        count: counts.length,
      };
    });
    return ok({ group_id: groupId, hours, generated_at: generatedAt, servers: results }) as ApiEnvelope<T>;
  }

  if (method === 'GET' && endpoint === '/page/cleanup') {
    const groupId = String(query?.group_id ?? '');
    if (!serversFor(groupId)) return fail('GROUP_NOT_FOUND', '群组不存在');
    return ok({ group_id: groupId, cleanup_days: CLEANUP_DAYS, candidates: cleanupCandidates(groupId) }) as ApiEnvelope<T>;
  }

  if (method === 'POST' && endpoint === '/page/cleanup') {
    const input = body as { group_id: string; confirm: boolean };
    if (input.confirm !== true) return fail('CONFIRM_REQUIRED', '执行清理必须显式设置 confirm=true');
    if (!serversFor(input.group_id)) return fail('GROUP_NOT_FOUND', '群组不存在');
    const deleted = cleanupCandidates(input.group_id);
    deleted.forEach((candidate) => {
      delete serverDb[input.group_id][candidate.id];
      delete trendDb[input.group_id]?.[candidate.id];
    });
    return ok({
      group_id: input.group_id,
      cleanup_days: CLEANUP_DAYS,
      deleted,
      deleted_count: deleted.length,
    }) as ApiEnvelope<T>;
  }

  return fail('ENDPOINT_NOT_FOUND', `Mock 未实现 endpoint：${endpoint}`);
}

import assert from 'node:assert/strict';
import { test } from 'node:test';
import { QueryCacheCore } from './queryCacheCore';

function deferred<T>() {
  let resolve!: (value: T | PromiseLike<T>) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}

test('同键请求复用同一个 in-flight Promise', async () => {
  const cache = new QueryCacheCore();
  const pending = deferred<string>();
  let calls = 0;
  const queryFn = () => {
    calls += 1;
    return pending.promise;
  };

  const first = cache.fetch(['mc', 'servers', 'group-a'], queryFn);
  const second = cache.fetch(['mc', 'servers', 'group-a'], queryFn, { force: true });

  assert.strictEqual(first, second);
  assert.equal(calls, 0);
  pending.resolve('servers-a');
  assert.equal(await first, 'servers-a');
  assert.equal(calls, 1);
  assert.equal(cache.getSnapshot<string>(['mc', 'servers', 'group-a']).data, 'servers-a');
});

test('不同 group key 的请求和数据互相隔离', async () => {
  const cache = new QueryCacheCore();
  let calls = 0;

  const [groupA, groupB] = await Promise.all([
    cache.fetch(['mc', 'settings', 'group-a'], async () => {
      calls += 1;
      return { group: 'group-a' };
    }),
    cache.fetch(['mc', 'settings', 'group-b'], async () => {
      calls += 1;
      return { group: 'group-b' };
    }),
  ]);

  assert.equal(calls, 2);
  assert.deepEqual(groupA, { group: 'group-a' });
  assert.deepEqual(groupB, { group: 'group-b' });
  assert.deepEqual(cache.getSnapshot(['mc', 'settings', 'group-a']).data, { group: 'group-a' });
  assert.deepEqual(cache.getSnapshot(['mc', 'settings', 'group-b']).data, { group: 'group-b' });
});

test('SWR 刷新期间保留旧数据', async () => {
  let now = 1_000;
  const cache = new QueryCacheCore({ now: () => now });
  const key = ['memory', 'overview', 'stats'] as const;
  cache.set(key, { total: 1 }, { ttl: 10 });
  now = 1_011;
  const pending = deferred<{ total: number }>();

  const refresh = cache.fetch(key, () => pending.promise);
  const refreshing = cache.getSnapshot<{ total: number }>(key);
  assert.deepEqual(refreshing.data, { total: 1 });
  assert.equal(refreshing.fetching, true);

  pending.resolve({ total: 2 });
  await refresh;
  assert.deepEqual(cache.getSnapshot<{ total: number }>(key).data, { total: 2 });
});

test('prefix invalidate 保留数据并只标记匹配项过期', () => {
  const cache = new QueryCacheCore();
  const serversA = ['mc', 'servers', 'group-a'] as const;
  const serversB = ['mc', 'servers', 'group-b'] as const;
  const memoryStats = ['memory', 'overview', 'stats'] as const;
  cache.set(serversA, 'a');
  cache.set(serversB, 'b');
  cache.set(memoryStats, 'stats');
  const generationA = cache.getSnapshot(serversA).generation;
  const generationB = cache.getSnapshot(serversB).generation;
  const memoryGeneration = cache.getSnapshot(memoryStats).generation;

  assert.equal(cache.invalidate(['mc', 'servers']), 2);
  assert.equal(cache.getSnapshot(serversA).data, 'a');
  assert.equal(cache.getSnapshot(serversB).data, 'b');
  assert.equal(cache.getSnapshot(serversA).staleAt, 0);
  assert.equal(cache.getSnapshot(serversB).staleAt, 0);
  assert.equal(cache.getSnapshot(serversA).generation, generationA + 1);
  assert.equal(cache.getSnapshot(serversB).generation, generationB + 1);
  assert.equal(cache.getSnapshot(memoryStats).generation, memoryGeneration);
  assert.equal(cache.isStale(memoryStats), false);
});

test('invalidate 后旧请求响应不能覆盖新一代数据', async () => {
  const cache = new QueryCacheCore();
  const key = ['mc', 'trends', 'group-a', null, 24] as const;
  const oldPending = deferred<string>();
  const oldRequest = cache.fetch(key, () => oldPending.promise);

  cache.invalidate(key);
  const newRequest = cache.fetch(key, async () => 'new-data');
  assert.equal(await newRequest, 'new-data');

  oldPending.resolve('old-data');
  assert.equal(await oldRequest, 'old-data');
  assert.equal(cache.getSnapshot<string>(key).data, 'new-data');
});

test('背景刷新失败保留缓存数据并记录错误', async () => {
  let now = 2_000;
  const cache = new QueryCacheCore({ now: () => now });
  const key = ['memory', 'list', { page: 1 }] as const;
  const cached = { items: ['cached'] };
  const failure = new Error('background failed');
  cache.set(key, cached, { ttl: 5 });
  now = 2_006;

  await assert.rejects(cache.fetch(key, async () => {
    throw failure;
  }), failure);

  const snapshot = cache.getSnapshot<typeof cached>(key);
  assert.strictEqual(snapshot.data, cached);
  assert.strictEqual(snapshot.error, failure);
  assert.equal(snapshot.fetching, false);
  assert.equal(snapshot.staleAt, 2_005);
});

test('getSnapshot 在状态未变化时保持引用稳定且不同 key 不串数据', () => {
  const cache = new QueryCacheCore();
  const firstKey = ['memory', 'graph', 'overview', null, null] as const;
  const secondKey = ['memory', 'graph', 'overview', 'session-b', null] as const;

  assert.strictEqual(cache.getSnapshot(firstKey), cache.getSnapshot(firstKey));
  cache.set(firstKey, { mode: 'overview-a' });
  const firstSnapshot = cache.getSnapshot(firstKey);
  assert.strictEqual(firstSnapshot, cache.getSnapshot(firstKey));
  assert.equal(cache.getSnapshot(secondKey).data, undefined);

  cache.set(secondKey, { mode: 'overview-b' });
  assert.deepEqual(cache.getSnapshot(firstKey).data, { mode: 'overview-a' });
  assert.deepEqual(cache.getSnapshot(secondKey).data, { mode: 'overview-b' });
});

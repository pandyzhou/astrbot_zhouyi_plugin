import { useCallback, useEffect, useRef, useSyncExternalStore } from 'react';
import {
  queryCache,
  serializeQueryKey,
  type QueryKey,
  type QueryUpdater,
} from './queryCacheCore';

export interface UseCachedQueryOptions {
  ttl?: number;
  enabled?: boolean;
}

export interface UseCachedQueryResult<T> {
  data: T | undefined;
  error: unknown | null;
  isInitialLoading: boolean;
  isRefreshing: boolean;
  refresh: () => Promise<T>;
  setData: (value: T | QueryUpdater<T>) => T | undefined;
  invalidate: () => number;
}

export function useCachedQuery<T>(
  key: QueryKey,
  queryFn: () => Promise<T>,
  options: UseCachedQueryOptions = {},
): UseCachedQueryResult<T> {
  const { enabled = true, ttl } = options;
  const queryFnRef = useRef(queryFn);
  queryFnRef.current = queryFn;

  const keyHash = serializeQueryKey(key);
  const stableKeyRef = useRef<{ hash: string; key: QueryKey } | null>(null);
  if (!stableKeyRef.current || stableKeyRef.current.hash !== keyHash) {
    stableKeyRef.current = { hash: keyHash, key: Object.freeze([...key]) };
  }
  const stableKey = stableKeyRef.current.key;

  const subscribe = useCallback(
    (listener: () => void) => queryCache.subscribe(stableKey, listener),
    [keyHash, stableKey],
  );
  const getSnapshot = useCallback(
    () => queryCache.getSnapshot<T>(stableKey),
    [keyHash, stableKey],
  );
  const snapshot = useSyncExternalStore(subscribe, getSnapshot, getSnapshot);

  useEffect(() => {
    if (!enabled) return;
    void queryCache.fetch(stableKey, () => queryFnRef.current(), { ttl }).catch(() => undefined);
  }, [enabled, keyHash, snapshot.generation, stableKey, ttl]);

  const refresh = useCallback(
    () => queryCache.revalidate(stableKey, () => queryFnRef.current(), { ttl }),
    [keyHash, stableKey, ttl],
  );

  const setData = useCallback((value: T | QueryUpdater<T>) => {
    const updater: QueryUpdater<T> = typeof value === 'function'
      ? value as QueryUpdater<T>
      : () => value;
    return queryCache.update(stableKey, updater, { ttl });
  }, [keyHash, stableKey, ttl]);

  const invalidate = useCallback(
    () => queryCache.invalidate(stableKey),
    [keyHash, stableKey],
  );

  const hasData = queryCache.hasData(stableKey);
  return {
    data: snapshot.data,
    error: snapshot.error,
    isInitialLoading: enabled && !hasData && snapshot.error === null,
    isRefreshing: hasData && snapshot.fetching,
    refresh,
    setData,
    invalidate,
  };
}

export type QueryKey = readonly unknown[];

export interface QueryCacheSnapshot<T = unknown> {
  readonly data: T | undefined;
  readonly error: unknown | null;
  readonly updatedAt: number;
  readonly staleAt: number;
  readonly fetching: boolean;
  readonly generation: number;
}

export interface QueryFetchOptions {
  ttl?: number;
  force?: boolean;
}

export interface QueryWriteOptions {
  ttl?: number;
}

export type QueryUpdater<T> = (current: T | undefined) => T | undefined;

type Listener = () => void;

interface StoredEntry {
  readonly key: QueryKey;
  readonly parts: readonly string[];
  hasData: boolean;
  snapshot: QueryCacheSnapshot<unknown>;
}

interface InFlightRequest {
  readonly generation: number;
  promise: Promise<unknown>;
}

export interface QueryCacheCoreOptions {
  defaultTtl?: number;
  now?: () => number;
}

const EMPTY_SNAPSHOT: QueryCacheSnapshot<never> = Object.freeze({
  data: undefined,
  error: null,
  updatedAt: 0,
  staleAt: 0,
  fetching: false,
  generation: 0,
});

const DEFAULT_TTL = 30_000;

function normalizeStableValue(value: unknown, seen: WeakSet<object>): unknown {
  if (value === null) return ['null'];

  switch (typeof value) {
    case 'string':
      return ['string', value];
    case 'boolean':
      return ['boolean', value];
    case 'undefined':
      return ['undefined'];
    case 'number':
      if (Number.isNaN(value)) return ['number', 'NaN'];
      if (value === Infinity) return ['number', 'Infinity'];
      if (value === -Infinity) return ['number', '-Infinity'];
      if (Object.is(value, -0)) return ['number', '-0'];
      return ['number', value];
    case 'bigint':
      return ['bigint', value.toString()];
    case 'function':
    case 'symbol':
      throw new TypeError(`Query keys cannot contain ${typeof value} values.`);
    case 'object': {
      if (seen.has(value)) throw new TypeError('Query keys cannot contain circular values.');
      seen.add(value);
      try {
        if (Array.isArray(value)) {
          return ['array', value.map((item) => normalizeStableValue(item, seen))];
        }
        if (value instanceof Date) {
          return ['date', value.toJSON()];
        }
        const prototype = Object.getPrototypeOf(value);
        if (prototype !== Object.prototype && prototype !== null) {
          throw new TypeError('Query key objects must be plain objects, arrays, or Date instances.');
        }
        return [
          'object',
          Object.keys(value)
            .sort()
            .map((key) => [key, normalizeStableValue((value as Record<string, unknown>)[key], seen)]),
        ];
      } finally {
        seen.delete(value);
      }
    }
  }
}

function serializeKeyPart(value: unknown): string {
  return JSON.stringify(normalizeStableValue(value, new WeakSet<object>()));
}

export function serializeQueryKey(key: QueryKey): string {
  if (!Array.isArray(key)) throw new TypeError('Query keys must be readonly arrays.');
  return JSON.stringify(key.map((part) => normalizeStableValue(part, new WeakSet<object>())));
}

function normalizeTtl(ttl: number | undefined, fallback: number): number {
  const value = ttl ?? fallback;
  if (Number.isNaN(value) || value < 0) throw new RangeError('Query cache TTL must be zero or greater.');
  return value;
}

export class QueryCacheCore {
  private readonly entries = new Map<string, StoredEntry>();
  private readonly inFlight = new Map<string, InFlightRequest>();
  private readonly listeners = new Map<string, Set<Listener>>();
  private readonly defaultTtl: number;
  private readonly now: () => number;

  constructor(options: QueryCacheCoreOptions = {}) {
    this.defaultTtl = normalizeTtl(options.defaultTtl, DEFAULT_TTL);
    this.now = options.now ?? Date.now;
  }

  peek<T>(key: QueryKey): QueryCacheSnapshot<T> | undefined {
    const entry = this.entries.get(serializeQueryKey(key));
    return entry?.snapshot as QueryCacheSnapshot<T> | undefined;
  }

  getSnapshot<T>(key: QueryKey): QueryCacheSnapshot<T> {
    return this.peek<T>(key) ?? (EMPTY_SNAPSHOT as QueryCacheSnapshot<T>);
  }

  hasData(key: QueryKey): boolean {
    return this.entries.get(serializeQueryKey(key))?.hasData ?? false;
  }

  isStale(key: QueryKey, at = this.now()): boolean {
    const entry = this.entries.get(serializeQueryKey(key));
    return !entry?.hasData || at >= entry.snapshot.staleAt;
  }

  subscribe(key: QueryKey, listener: Listener): () => void {
    const hash = serializeQueryKey(key);
    let subscribers = this.listeners.get(hash);
    if (!subscribers) {
      subscribers = new Set();
      this.listeners.set(hash, subscribers);
    }
    subscribers.add(listener);
    return () => {
      subscribers?.delete(listener);
      if (!subscribers?.size) this.listeners.delete(hash);
    };
  }

  fetch<T>(key: QueryKey, queryFn: () => Promise<T>, options: QueryFetchOptions = {}): Promise<T> {
    const hash = serializeQueryKey(key);
    const activeRequest = this.inFlight.get(hash);
    if (activeRequest) return activeRequest.promise as Promise<T>;

    let entry = this.entries.get(hash);
    const now = this.now();
    if (!options.force && entry?.hasData && now < entry.snapshot.staleAt) {
      return Promise.resolve(entry.snapshot.data as T);
    }

    const ttl = normalizeTtl(options.ttl, this.defaultTtl);
    if (!entry) {
      entry = {
        key: Object.freeze([...key]),
        parts: Object.freeze(key.map(serializeKeyPart)),
        hasData: false,
        snapshot: EMPTY_SNAPSHOT,
      };
      this.entries.set(hash, entry);
    }

    const generation = entry.snapshot.generation;
    const request: InFlightRequest = {
      generation,
      promise: undefined as unknown as Promise<unknown>,
    };
    const promise = Promise.resolve()
      .then(queryFn)
      .then(
        (data) => {
          const current = this.entries.get(hash);
          if (current && this.inFlight.get(hash) === request && current.snapshot.generation === generation) {
            const updatedAt = this.now();
            current.hasData = true;
            current.snapshot = {
              data,
              error: null,
              updatedAt,
              staleAt: updatedAt + ttl,
              fetching: false,
              generation,
            };
            this.emit(hash);
          }
          return data;
        },
        (error: unknown) => {
          const current = this.entries.get(hash);
          if (current && this.inFlight.get(hash) === request && current.snapshot.generation === generation) {
            current.snapshot = {
              ...current.snapshot,
              error,
              fetching: false,
            };
            this.emit(hash);
          }
          throw error;
        },
      )
      .finally(() => {
        if (this.inFlight.get(hash) === request) this.inFlight.delete(hash);
      });

    request.promise = promise;
    this.inFlight.set(hash, request);
    entry.snapshot = {
      ...entry.snapshot,
      error: null,
      fetching: true,
    };
    this.emit(hash);
    return promise;
  }

  revalidate<T>(key: QueryKey, queryFn: () => Promise<T>, options: QueryFetchOptions = {}): Promise<T> {
    return this.fetch(key, queryFn, { ...options, force: options.force ?? true });
  }

  set<T>(key: QueryKey, data: T, options: QueryWriteOptions = {}): T {
    const hash = serializeQueryKey(key);
    const previous = this.entries.get(hash);
    const updatedAt = this.now();
    const ttl = normalizeTtl(options.ttl, this.defaultTtl);
    this.inFlight.delete(hash);
    this.entries.set(hash, {
      key: previous?.key ?? Object.freeze([...key]),
      parts: previous?.parts ?? Object.freeze(key.map(serializeKeyPart)),
      hasData: true,
      snapshot: {
        data,
        error: null,
        updatedAt,
        staleAt: updatedAt + ttl,
        fetching: false,
        generation: previous ? previous.snapshot.generation + 1 : 0,
      },
    });
    this.emit(hash);
    return data;
  }

  update<T>(key: QueryKey, updater: QueryUpdater<T>, options: QueryWriteOptions = {}): T | undefined {
    const current = this.entries.get(serializeQueryKey(key));
    const next = updater(current?.hasData ? current.snapshot.data as T : undefined);
    this.set(key, next, options);
    return next;
  }

  invalidate(prefix: QueryKey = []): number {
    const prefixParts = prefix.map(serializeKeyPart);
    let invalidated = 0;
    for (const [hash, entry] of this.entries) {
      if (!this.matchesPrefix(entry.parts, prefixParts)) continue;
      this.inFlight.delete(hash);
      entry.snapshot = {
        ...entry.snapshot,
        staleAt: 0,
        fetching: false,
        generation: entry.snapshot.generation + 1,
      };
      invalidated += 1;
      this.emit(hash);
    }
    return invalidated;
  }

  evict(key: QueryKey): boolean {
    const hash = serializeQueryKey(key);
    const existed = this.entries.delete(hash);
    const wasFetching = this.inFlight.delete(hash);
    if (existed || wasFetching) this.emit(hash);
    return existed;
  }

  clear(): void {
    const hashes = new Set([...this.entries.keys(), ...this.inFlight.keys(), ...this.listeners.keys()]);
    this.entries.clear();
    this.inFlight.clear();
    hashes.forEach((hash) => this.emit(hash));
  }

  private matchesPrefix(parts: readonly string[], prefixParts: readonly string[]): boolean {
    if (prefixParts.length > parts.length) return false;
    return prefixParts.every((part, index) => parts[index] === part);
  }

  private emit(hash: string): void {
    this.listeners.get(hash)?.forEach((listener) => listener());
  }
}

export const queryCache = new QueryCacheCore();
export const queryCacheCore = queryCache;

/* The one data layer.
 *
 * The old frontend had three hand-written API wrappers (intelApi.ts,
 * motionApi.ts, distributionApi.ts) duplicating the client and each other, so
 * loading / error / retry / cancellation was re-implemented per panel and a
 * screen could render a stale value with no error state at all.
 *
 * The promise here is narrow and testable: a component using useQuery can
 * always tell loading from empty from error from loaded, and can never paint a
 * half-state. That is what makes the empty/loading/error surfaces honest
 * instead of decorative.
 */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, wsApi } from "../lib/api";

export type QueryState<T> = {
  data: T | null;
  error: string | null;
  /**
   * The HTTP status that produced `error`, or `null`.
   *
   * WHY THIS EXISTS (Work 16.5.7): `ApiError` carries `status`, and the fetch
   * handler already computed it -- then threw it away, storing only
   * `err.message`, which is the server's `detail` text. Every consumer therefore
   * had to decide "was this a refusal?" by pattern-matching prose.
   *
   * That is unworkable, and measurably so. The backend emits SEVEN distinct 403
   * details (`scripts/denial_vocabulary.py`): "insufficient role",
   * "not a workspace member", "insufficient project role", and four more. Four of
   * the seven matched nothing, so those refusals rendered as a generic load
   * failure WITH a Retry button that could never succeed -- including
   * "not a workspace member", the single most common one.
   *
   * Carrying the status fixes all thirteen denial strings at once and means a new
   * one needs no frontend change. Optional, so every existing consumer that only
   * reads `error` is unaffected.
   */
  errorStatus: number | null;
  /** First load only. A background refresh must not blank the screen. */
  loading: boolean;
  /** True once a load has settled, successfully or not. */
  settled: boolean;
  /** True while a refetch runs over data already on screen. */
  refreshing: boolean;
  reload: () => void;
  /** Replace the cached value without a round trip. */
  setData: (updater: T | ((prev: T | null) => T | null)) => void;
};

export type QueryOptions = {
  /** Skip the request entirely. */
  enabled?: boolean;
  /** Re-run when any of these change. */
  deps?: unknown[];
  /** Poll interval in ms. Omit for none. */
  refetchMs?: number;
  /** Statuses that are not worth showing the user as an error. */
  ignoreStatuses?: number[];
};

export function useQuery<T>(
  fetcher: () => Promise<T>,
  options: QueryOptions = {},
): QueryState<T> {
  const { enabled = true, deps = [], refetchMs, ignoreStatuses } = options;

  const [data, setDataState] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [errorStatus, setErrorStatus] = useState<number | null>(null);
  const [loading, setLoading] = useState(enabled);
  const [settled, setSettled] = useState(false);
  const [refreshing, setRefreshing] = useState(false);
  const [tick, setTick] = useState(0);

  /* A generation counter, not a mount flag. The old useFetch tracked mount, so
   * switching workspaces let a late response from the previous key paint the
   * new screen. Here a stale response is discarded by identity. */
  const generation = useRef(0);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;
  const ignoreRef = useRef(ignoreStatuses);
  ignoreRef.current = ignoreStatuses;
  const hasDataRef = useRef(false);

  useEffect(() => {
    if (!enabled) {
      setLoading(false);
      return;
    }
    const mine = ++generation.current;
    if (hasDataRef.current) setRefreshing(true);
    else setLoading(true);

    fetcherRef.current().then(
      (value) => {
        if (generation.current !== mine) return;
        hasDataRef.current = true;
        setDataState(value);
        setError(null);
        // Cleared with the message, or a recovered load would keep reporting the
        // previous failure's status next to fresh data.
        setErrorStatus(null);
        setLoading(false);
        setRefreshing(false);
        setSettled(true);
      },
      (err: unknown) => {
        if (generation.current !== mine) return;
        const status = err instanceof ApiError ? err.status : 0;
        if (!ignoreRef.current?.includes(status)) {
          setError(err instanceof Error ? err.message : String(err));
          // Keep the status alongside the message so the UI can tell a REFUSAL
          // from a BREAKAGE without pattern-matching the server's prose.
          // 0 means "not an HTTP error" (network/abort), so it is stored as null
          // rather than a fake status.
          setErrorStatus(status || null);
        }
        setLoading(false);
        setRefreshing(false);
        setSettled(true);
      },
    );
  }, [enabled, tick, refetchMs, ...deps]);

  useEffect(() => {
    if (!enabled || !refetchMs) return;
    const id = window.setInterval(() => setTick((t) => t + 1), refetchMs);
    return () => window.clearInterval(id);
  }, [enabled, refetchMs, ...deps]);

  const reload = useCallback(() => setTick((t) => t + 1), []);
  const setData = useCallback((updater: T | ((prev: T | null) => T | null)) => {
    setDataState((prev) =>
      typeof updater === "function"
        ? (updater as (p: T | null) => T | null)(prev)
        : updater,
    );
  }, []);

  return { data, error, errorStatus, loading, settled, refreshing, reload, setData };
}

export type MutationState<TArgs, TResult> = {
  run: (args: TArgs) => Promise<TResult | undefined>;
  pending: boolean;
  error: string | null;
  reset: () => void;
};

/**
 * A mutation that never throws into the render path.
 *
 * This is a correctness concern, not style: a mutation touching money must
 * report a refusal AS a refusal, never as an unhandled rejection that reads as
 * a network blip and invites a blind retry. The ApiError is preserved so a
 * caller can branch on the status rather than on a string.
 */
export function useMutation<TArgs, TResult>(
  fn: (args: TArgs) => Promise<TResult>,
  options: {
    onSuccess?: (result: TResult, args: TArgs) => void;
    onError?: (error: Error, args: TArgs) => void;
  } = {},
): MutationState<TArgs, TResult> {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const alive = useRef(true);
  const fnRef = useRef(fn);
  fnRef.current = fn;
  const optsRef = useRef(options);
  optsRef.current = options;

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  const run = useCallback(async (args: TArgs) => {
    setPending(true);
    setError(null);
    try {
      const result = await fnRef.current(args);
      if (alive.current) optsRef.current.onSuccess?.(result, args);
      return result;
    } catch (err) {
      const e = err instanceof Error ? err : new Error(String(err));
      if (alive.current) {
        setError(e.message);
        optsRef.current.onError?.(e, args);
      }
      return undefined;
    } finally {
      if (alive.current) setPending(false);
    }
  }, []);

  const reset = useCallback(() => setError(null), []);

  return { run, pending, error, reset };
}

/** Workspace-scoped GET. The overwhelming majority of reads. */
export function useWsQuery<T>(path: string, options: QueryOptions = {}): QueryState<T> {
  // `wsApi.get` is not generic (it is the preserved client verbatim), so the
  // shape is asserted here rather than by changing that file's signature.
  return useQuery<T>(() => wsApi.get(path) as Promise<T>, { deps: [path], ...options });
}

export type CombinedResult<T extends Record<string, unknown>> = {
  data: { [K in keyof T]: T[K] | null };
  /** True until every panel has settled. */
  loading: boolean;
  settled: boolean;
  errors: Partial<Record<keyof T, string>>;
  reload: () => void;
  /** True when some panels arrived and others failed. */
  partial: boolean;
};

/**
 * Several independent reads for one screen, with one settled flag.
 *
 * The Command Center shows a dozen panels. Without this each panel spins
 * independently and the page "loads" eleven times; with it the page has one
 * honest loading state, can still render what did arrive, and reports which
 * panel failed instead of collapsing to a single "something went wrong".
 */
export function useCombinedQueries<T extends Record<string, unknown>>(
  entries: { [K in keyof T]: () => Promise<T[K]> },
): CombinedResult<T> {
  const keys = Object.keys(entries) as (keyof T)[];
  const [data, setData] = useState<{ [K in keyof T]: T[K] | null }>(
    () => keys.reduce((acc, k) => ({ ...acc, [k]: null }), {} as { [K in keyof T]: T[K] | null }),
  );
  const [errors, setErrors] = useState<Partial<Record<keyof T, string>>>({});
  const [inFlight, setInFlight] = useState<Record<string, boolean>>({});
  const [tick, setTick] = useState(0);
  const entriesRef = useRef(entries);
  entriesRef.current = entries;

  useEffect(() => {
    let alive = true;
    const current = entriesRef.current;
    const names = Object.keys(current) as (keyof T)[];
    if (names.length === 0) return;

    setInFlight(Object.fromEntries(names.map((n) => [n, true])));
    for (const name of names) {
      current[name]().then(
        (value) => {
          if (!alive) return;
          setData((d) => ({ ...d, [name]: value }));
          setErrors((e) => {
            const next = { ...e };
            delete next[name];
            return next;
          });
          setInFlight((p) => ({ ...p, [String(name)]: false }));
        },
        (err: unknown) => {
          if (!alive) return;
          setErrors((e) => ({ ...e, [name]: err instanceof Error ? err.message : String(err) }));
          setInFlight((p) => ({ ...p, [String(name)]: false }));
        },
      );
    }
    return () => {
      alive = false;
    };
  }, [tick]);

  // A key absent from `inFlight` has not started, so it is not settled.
  const settled = keys.every((k) => inFlight[String(k)] === false);
  const errorKeys = Object.keys(errors);

  return {
    data,
    loading: !settled,
    settled,
    errors,
    reload: useCallback(() => setTick((t) => t + 1), []),
    partial: settled && errorKeys.length > 0 && errorKeys.length < keys.length,
  };
}
import { useCallback, useEffect, useRef, useState } from "react";
import { activityStreamUrl } from "../lib/api";

export function useFetch<T = any>(fn: () => Promise<T>, deps: any[] = []): {
  data: T | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
} {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  useEffect(() => {
    let cancelled = false;
    setLoading(true);
    setError(null);
    fn()
      .then((r) => {
        if (!cancelled && alive.current) setData(r);
      })
      .catch((e: any) => {
        if (!cancelled && alive.current) setError(e?.message ?? "request failed");
      })
      .finally(() => {
        if (!cancelled && alive.current) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [...deps, tick]);
  const reload = useCallback(() => setTick((t) => t + 1), []);
  return { data, loading, error, reload };
}

export function useInterval(fn: () => void, ms: number | null) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    if (ms == null) return;
    const id = setInterval(() => ref.current(), ms);
    return () => clearInterval(id);
  }, [ms]);
}

export type FeedItem = {
  id?: string;
  level: string;
  source: string;
  kind: string;
  message: string;
  created_at?: string;
};

export function useActivityFeed(onEvent?: (e: FeedItem) => void): FeedItem[] {
  const [feed, setFeed] = useState<FeedItem[]>([]);
  const cb = useRef(onEvent);
  cb.current = onEvent;
  useEffect(() => {
    let es: EventSource | null = null;
    try {
      es = new EventSource(activityStreamUrl());
      es.onmessage = (ev) => {
        try {
          const item = JSON.parse(ev.data) as FeedItem;
          setFeed((f) => [...f.slice(-150), item]);
          cb.current?.(item);
        } catch {}
      };
      es.onerror = () => {};
    } catch {}
    return () => es?.close();
  }, []);
  return feed;
}

export function useTheme(): ["light" | "dark" | "system", (t: "light" | "dark" | "system") => void] {
  // Dark-first (from-scratch rebuild §5): the product direction is dark, and
  // the design-system token layer is dark-only -- under `html.light` the page
  // chrome goes paper while every `ym-*` surface stays graphite, which is an
  // incoherent mixed theme, not a light theme. So the DEFAULT is dark; "light"
  // and "system" remain explicit user choices in Appearance. This also makes
  // rendering deterministic: headless Chrome reports a light OS scheme, so a
  // "system" default renders CI screenshots in a theme no developer ever sees.
  const [theme, setThemeState] = useState<"light" | "dark" | "system">(
    () => (localStorage.getItem("ym_theme") as any) || "dark"
  );
  const apply = (t: "light" | "dark" | "system") => {
    const dark = t !== "light" && (t === "dark" || window.matchMedia("(prefers-color-scheme: dark)").matches);
    document.documentElement.classList.toggle("light", !dark);
  };
  useEffect(() => {
    apply(localStorage.getItem("ym_theme") as any || "dark");
  }, []);
  const setTheme = (t: "light" | "dark" | "system") => {
    setThemeState(t);
    localStorage.setItem("ym_theme", t);
    apply(t);
  };
  return [theme, setTheme];
}

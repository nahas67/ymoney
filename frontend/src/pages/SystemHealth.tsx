import { useCallback, useEffect, useState } from "react";
import { Badge, Card, PageHeader, StatusDot, fmtDate } from "../components/ui";

type Health = {
  status: string;
  database: boolean;
  video_engine: boolean;
  video_engine_name: string;
  llm_provider: boolean;
  publishers: Record<string, { mode: string; ready: boolean; detail: string }>;
  mocks: Record<string, boolean>;
  time: string;
};

const COMPONENTS = (h: Health | null) => h ? [
  ["API", true, "backend responding"],
  ["Database", h.database, "SQLite/Postgres reachable"],
  ["Video engine", h.video_engine, `${h.video_engine_name} (${h.video_engine === undefined ? "?" : ""})`],
  ["LLM provider", h.llm_provider ?? (h.mocks?.llm ?? false), h.mocks?.llm ? "mock mode — always available" : "OpenAI-compatible endpoint"],
  ...Object.entries(h.publishers ?? {}).map(([name, p]: any) => [
    `Publisher: ${name}`, p.ready, `${p.mode}${p.relay ? ` via ${p.relay}` : ""} — ${p.detail}`,
  ]),
  ["Analytics", !h.mocks?.analytics || true, h.mocks?.analytics ? "mock mode — simulated metrics" : "platform APIs"],
] as [string, boolean, string][] : [];

export default function SystemHealth() {
  const [health, setHealth] = useState<Health | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [checkedAt, setCheckedAt] = useState<string>("");

  const load = useCallback(async () => {
    setError(null);
    try {
      const res = await fetch("/api/v1/system/health");
      if (!res.ok) throw new Error(`health check returned ${res.status}`);
      const data: Health = await res.json();
      setHealth(data);
      setCheckedAt(new Date().toLocaleTimeString());
    } catch (e: any) {
      setError(e.message);
    }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 15000);
    return () => clearInterval(t);
  }, [load]);

  return (
    <div className="space-y-5 max-w-3xl">
      <PageHeader
        title="System Health"
        subtitle={error ? undefined : checkedAt ? `Last checked ${checkedAt} · auto-refreshes every 15s` : undefined}
        actions={<button className="btn-outline" onClick={load}>Recheck</button>}
      />

      {error && (
        <Card className="border-red-500/40">
          <div className="flex items-start gap-2.5">
            <span className="text-red-500">✕</span>
            <div>
              <p className="font-medium text-sm">Health check failed</p>
              <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>{error} — the API may be down. Retry above.</p>
            </div>
          </div>
        </Card>
      )}

      {health && (
        <>
          <Card>
            <div className="flex items-center gap-2.5 mb-4">
              <StatusDot tone={health.status === "healthy" ? "success" : "warning"} pulse />
              <span className="font-semibold">{health.status.toUpperCase()}</span>
            </div>
            <div className="space-y-1.5">
              {COMPONENTS(health).map(([name, ok, detail]) => (
                <div key={name as string} className="flex items-center justify-between rounded-lg px-3.5 py-2.5 text-sm border"
                     style={{ borderColor: "var(--border)" }}>
                  <span className="flex items-center font-medium"><StatusDot tone={(ok as boolean) ? "success" : "error"} />{name}</span>
                  <span className="text-[12px] flex items-center gap-2" style={{ color: "var(--text-muted)" }}>
                    {detail}
                    <Badge tone={(ok as boolean) ? "success" : "error"}>{ok ? "HEALTHY" : "DOWN"}</Badge>
                  </span>
                </div>
              ))}
            </div>
          </Card>

          <Card>
            <h3 className="text-xs font-semibold uppercase tracking-wider mb-3" style={{ color: "var(--text-muted)" }}>
              Environment
            </h3>
            <div className="flex gap-2 flex-wrap">
              {Object.entries(health.mocks ?? {}).map(([flag, on]) => (
                <Badge key={flag} tone={on ? "warning" : "success"}>
                  {flag}: {on ? "MOCK" : "REAL"}
                </Badge>
              ))}
            </div>
            <p className="text-[12px] mt-3" style={{ color: "var(--text-muted)" }}>
              MOCK providers are simulated locally and clearly labeled everywhere in the product.
              Switch them in Settings → AI & Trends or via environment variables before going live.
            </p>
          </Card>
        </>
      )}
      {!health && !error && <p style={{ color: "var(--text-muted)" }}>Checking…</p>}
    </div>
  );
}

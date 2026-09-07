import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { wsApi } from "../lib/api";
import { Badge, Card, Modal, PageHeader, Skeleton, fmtDate } from "../components/ui";

const STAGES = ["FIND", "SCORE", "SELECT", "RESEARCH", "BUILD", "VERIFY", "UPLOAD", "MEASURE", "LEARN"];

type Job = {
  id: string; type: string; status: string; cycle_id: string | null;
  retry_count: number; last_error: string; created_at: string;
  result?: any;
};

type Cycle = {
  id: string; number: number; stage: string; status: string; cost_usd: number;
  topic: string | null; started_at: string | null; finished_at: string | null; error: string;
};

export default function Autopilot() {
  const [status, setStatus] = useState<any>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [cycles, setCycles] = useState<Cycle[]>([]);
  const [rendering, setRendering] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);
  const [detail, setDetail] = useState<any | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const pollRef = useRef<number | null>(null);

  const refresh = useCallback(async () => {
    try {
      const [s, j, c] = await Promise.all([
        wsApi.get("/autopilot/status"),
        wsApi.get("/jobs?limit=80"),
        wsApi.get("/cycles?limit=20"),
      ]);
      setStatus(s); setJobs(j.items ?? []); setCycles(c.items ?? []);
      // live render progress straight from the persisted engine reconciliation row
      try {
        const vids = await wsApi.get("/videos?limit=10");
        setRendering((vids.items ?? []).find((v: any) => v.status === "RENDERING") ?? null);
      } catch {}
    } catch {} finally { setLoading(false); }
  }, []);

  useEffect(() => {
    refresh();
    pollRef.current = window.setInterval(refresh, 4000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [refresh]);

  async function act(action: string) {
    try {
      if (action === "start") await wsApi.post("/autopilot/start", { mode: "CONTINUOUS" });
      else await wsApi.post(`/autopilot/${action}`);
      await refresh();
    } catch {}
  }

  async function openCycle(id: string) {
    setDetailLoading(true);
    setDetail({ loading: true });
    try {
      const d = await wsApi.get(`/cycles/${id}`);
      setDetail(d);
    } catch {
      setDetail(null);
    } finally {
      setDetailLoading(false);
    }
  }

  const running = status?.state === "RUNNING" || status?.state === "STARTING";
  const currentStage = status?.current_cycle?.stage ?? "";
  const stageIdx = STAGES.indexOf(currentStage);

  // group live/recent jobs by pipeline stage
  const jobsByStage: Record<string, Job[]> = {};
  for (const j of jobs) {
    const m = j.type.match(/^cycle\.(\w+)$/);
    if (!m) continue;
    const stage = m[1].toUpperCase();
    (jobsByStage[stage] ??= []).push(j);
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Autopilot"
        subtitle="The durable FIND→LEARN loop. Every stage is a queued job — crash-safe and observable."
        actions={
          <>
            {!running && <button className="btn-primary" onClick={() => act("start")}>Start</button>}
            {running && <button className="btn-outline" onClick={() => act("pause")}>Pause</button>}
            {status?.state === "PAUSED" && <button className="btn-primary" onClick={() => act("resume")}>Resume</button>}
            {(running || status?.state === "PAUSED") && <button className="btn-danger" onClick={() => act("stop")}>Stop</button>}
            {!running && <button className="btn-ghost" onClick={() => act("run-one-cycle")}>Run one cycle</button>}
          </>
        }
      />

      {/* Live render progress — real engine data, never faked */}
      {rendering && (
        <Card>
          <div className="flex items-center justify-between mb-2">
            <h2 className="text-xs font-semibold uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>
              BUILD ● Rendering video
            </h2>
            <span className="font-mono text-sm">{rendering.progress ?? 0}%</span>
          </div>
          <div className="h-2 rounded-full overflow-hidden" style={{ background: "var(--bg-subtle)" }}>
            <div className="h-full rounded-full transition-all duration-500"
                 style={{ width: `${rendering.progress ?? 0}%`, background: "var(--accent)" }} />
          </div>
          <p className="text-[11px] mt-2" style={{ color: "var(--text-muted)" }}>
            Engine: {rendering.engine}
            {rendering.engine_task_id ? ` · task ${String(rendering.engine_task_id).slice(0, 12)}` : ""}
          </p>
        </Card>
      )}

      {/* Stage map */}
      <Card>
        <div className="flex flex-wrap gap-y-3">
          {STAGES.map((s, i) => {
            const state = running && i === stageIdx ? "active"
              : jobsByStage[s]?.some((j) => j.status === "COMPLETED") ? "done"
              : jobsByStage[s]?.some((j) => ["RUNNING", "RETRYING", "QUEUED"].includes(j.status)) ? "queued"
              : "idle";
            return <StageBlock key={s} label={s} state={state as any} jobs={jobsByStage[s] ?? []} />;
          })}
        </div>
        {running && (
          <p className="text-xs mt-3" style={{ color: "var(--text-muted)" }}>
            Current cycle #{String(status?.current_cycle?.number ?? "?").padStart(3, "0")} · stage {currentStage}
          </p>
        )}
      </Card>

      {/* Recent cycles */}
      <section>
        <h2 className="text-xs font-semibold uppercase tracking-wider mb-2.5" style={{ color: "var(--text-muted)" }}>Recent cycles</h2>
        {loading ? <Skeleton rows={4} /> : (
          <Card pad={false} className="overflow-x-auto">
            <table className="table">
              <thead>
                <tr><th>Cycle</th><th>Topic</th><th>Status</th><th>Cost</th><th>Started</th></tr>
              </thead>
              <tbody>
                {cycles.map((c) => (
                  <tr key={c.id} className="cursor-pointer" onClick={() => openCycle(c.id)}
                      title="Open execution detail">
                    <td className="font-mono">#{String(c.number).padStart(3, "0")}</td>
                    <td className="max-w-[280px] truncate">{c.topic ?? "—"}</td>
                    <td>
                      <Badge tone={c.status === "COMPLETED" ? "success" : c.status === "FAILED" ? "error" : "info"}>
                        {c.status}
                      </Badge>
                      {c.error && <span className="text-[11px] ml-2 text-red-500">{c.error.slice(0, 60)}</span>}
                    </td>
                    <td className="font-mono">${(c.cost_usd ?? 0).toFixed(4)}</td>
                    <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtDate(c.started_at)}</td>
                  </tr>
                ))}
                {cycles.length === 0 && (
                  <tr><td colSpan={5} className="text-center py-8" style={{ color: "var(--text-muted)" }}>No cycles yet.</td></tr>
                )}
              </tbody>
            </table>
          </Card>
        )}
      </section>

      {/* Dead letters */}
      {jobs.some((j) => j.status === "DEAD") && (
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wider mb-2.5" style={{ color: "var(--danger)" }}>
            Failed permanently (dead letters)
          </h2>
          <Card pad={false} className="overflow-x-auto">
            <table className="table">
              <thead><tr><th>Type</th><th>Error</th><th>Retries</th><th>When</th></tr></thead>
              <tbody>
                {jobs.filter((j) => j.status === "DEAD").slice(0, 10).map((j) => (
                  <tr key={j.id}>
                    <td className="font-mono text-[12px]">{j.type}</td>
                    <td className="max-w-[360px] truncate text-red-500">{j.last_error}</td>
                    <td>{j.retry_count}</td>
                    <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtDate(j.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </section>
      )}

      <p className="text-xs" style={{ color: "var(--text-muted)" }}>
        Jobs survive restarts: interrupted work is retried automatically with exponential backoff.
        See <Link to="/health" className="underline">System Health</Link> for queue diagnostics.
      </p>

      <Modal open={!!detail} onClose={() => setDetail(null)} wide>
        {detail && !detail.loading && <CycleDetail d={detail} />}
        {detail?.loading && <Skeleton rows={6} />}
      </Modal>
    </div>
  );
}

function StageBlock({ label, state, jobs }: { label: string; state: "active" | "done" | "queued" | "idle"; jobs: Job[] }) {
  const marker = state === "done" ? "✓" : state === "active" ? "●" : state === "queued" ? "◐" : "○";
  const retries = jobs.reduce((a, j) => a + (j.retry_count ?? 0), 0);
  const failed = jobs.some((j) => j.status === "DEAD");
  return (
    <div className="flex items-center flex-1 min-w-fit" title={`${label}: ${state}${retries ? ` · ${retries} retries` : ""}`}>
      <div
        className={`rounded-lg px-3 py-2 text-center min-w-[84px] border transition-all`}
        style={{
          background: state === "active" ? "var(--accent)" : "transparent",
          borderColor: failed ? "var(--danger)" : state === "active" ? "transparent" : "var(--border)",
          color: state === "active" ? "#fff" : state === "idle" ? "var(--text-muted)" : "var(--text)",
        }}
      >
        <div className="flex items-center gap-1.5 text-[11px] font-bold justify-center">
          <span>{failed ? "✕" : marker}</span> {label}
        </div>
        {retries > 0 && <div className="text-[9px] opacity-70">{retries} retry</div>}
      </div>
      {label !== "LEARN" && <span className="mx-1 opacity-30">→</span>}
    </div>
  );
}

function CycleDetail({ d }: { d: any }) {
  const stageNames = Object.keys(d.stages ?? {});
  return (
    <div className="space-y-4">
      <div className="flex items-center justify-between">
        <div>
          <h2 className="text-base font-semibold">
            Cycle #{String(d.number).padStart(3, "0")}
            {d.topic && <span className="font-normal text-[13px] ml-2" style={{ color: "var(--text-muted)" }}>{d.topic}</span>}
          </h2>
          <p className="text-[12px] mt-0.5" style={{ color: "var(--text-muted)" }}>
            {d.stage} · started {fmtDate(d.started_at)}
            {d.cost_usd != null && ` · $${Number(d.cost_usd).toFixed(4)}`}
          </p>
        </div>
        <Badge tone={d.status === "COMPLETED" ? "success" : d.status === "FAILED" ? "error" : "info"}>{d.status}</Badge>
      </div>

      {d.error && (
        <div className="rounded-lg border px-3 py-2 text-[12px]" style={{ borderColor: "var(--danger)", color: "var(--danger)" }}>
          {d.error}
        </div>
      )}

      {stageNames.length === 0 && (
        <p className="text-sm" style={{ color: "var(--text-muted)" }}>No stage jobs recorded for this cycle.</p>
      )}

      {stageNames.map((stage) => (
        <div key={stage}>
          <h3 className="text-[11px] font-semibold uppercase tracking-wider mb-1.5" style={{ color: "var(--text-muted)" }}>
            {stage}
          </h3>
          <div className="space-y-2">
            {(d.stages[stage] as any[]).map((j) => (
              <div key={j.id} className="rounded-lg border p-2.5" style={{ borderColor: "var(--border)" }}>
                <div className="flex items-center justify-between gap-3">
                  <span className="font-mono text-[12px]">{j.type}</span>
                  <div className="flex items-center gap-2">
                    {j.retry_count > 0 && <span className="text-[10px]" style={{ color: "var(--text-muted)" }}>{j.retry_count} retries</span>}
                    <Badge tone={j.status === "COMPLETED" ? "success" : j.status === "DEAD" ? "error" : "info"}>{j.status}</Badge>
                  </div>
                </div>
                {j.last_error && <p className="text-[11px] mt-1 text-red-500">{j.last_error.slice(0, 200)}</p>}
                {j.agent_runs?.length > 0 && (
                  <div className="mt-2 space-y-1.5 border-l-2 pl-3" style={{ borderColor: "var(--border)" }}>
                    {j.agent_runs.map((r: any) => <AgentRunTrace key={r.id} r={r} />)}
                  </div>
                )}
              </div>
            ))}
          </div>
        </div>
      ))}

      {(d.agent_runs ?? []).filter((r: any) => !stageNames.some((s) => (d.stages[s] as any[]).some((j) => j.agent_runs?.some((jr: any) => jr.id === r.id)))).length > 0 && (
        <div>
          <h3 className="text-[11px] font-semibold uppercase tracking-wider mb-1.5" style={{ color: "var(--text-muted)" }}>Other agent runs</h3>
          <div className="space-y-1.5">
            {(d.agent_runs ?? []).map((r: any) => <AgentRunTrace key={r.id} r={r} />)}
          </div>
        </div>
      )}
    </div>
  );
}

function AgentRunTrace({ r }: { r: any }) {
  const [open, setOpen] = useState(false);
  const tone = r.status === "SUCCESS" || r.status === "COMPLETED" ? "success" : r.status === "FAILED" ? "error" : "info";
  const steps = r.steps ?? [];
  return (
    <div>
      <button className="flex items-center gap-2 text-[12px] w-full text-left" onClick={() => setOpen(!open)}>
        <span className="font-medium">{r.agent_key}</span>
        <span style={{ color: "var(--text-muted)" }}>{r.task_type}</span>
        {r.duration_ms != null && <span className="font-mono text-[10px]" style={{ color: "var(--text-muted)" }}>{r.duration_ms}ms</span>}
        {r.cost_usd > 0 && <span className="font-mono text-[10px]" style={{ color: "var(--text-muted)" }}>${Number(r.cost_usd).toFixed(4)}</span>}
        <Badge tone={tone as any}>{r.status}</Badge>
        {steps.length > 0 && <span className="ml-auto text-[10px] underline" style={{ color: "var(--text-muted)" }}>{open ? "hide" : `${steps.length} steps`}</span>}
      </button>
      {r.error && <p className="text-[11px] text-red-500 ml-2">{r.error.slice(0, 200)}</p>}
      {open && steps.length > 0 && (
        <div className="mt-1 ml-3 space-y-0.5">
          {steps.map((s: any, i: number) => (
            <div key={i} className="flex items-center gap-2 text-[11px]">
              <span style={{ color: s.status === "done" ? "var(--accent)" : s.status === "failed" ? "var(--danger)" : "var(--text-muted)" }}>
                {s.status === "done" ? "✓" : s.status === "failed" ? "✕" : "○"}
              </span>
              <span className="font-mono">{s.step}</span>
              {s.detail && <span className="truncate" style={{ color: "var(--text-muted)" }}>{s.detail}</span>}
              {s.duration_ms != null && <span className="font-mono text-[10px] ml-auto" style={{ color: "var(--text-muted)" }}>{s.duration_ms}ms</span>}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}


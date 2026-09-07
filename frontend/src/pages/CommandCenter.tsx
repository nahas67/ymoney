import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { api, wsApi } from "../lib/api";
import {
  Badge, Card, StatusDot, fmtNum, fmtUsd,
} from "../components/ui";

const STAGES = ["FIND", "SCORE", "SELECT", "RESEARCH", "BUILD", "VERIFY", "UPLOAD", "MEASURE", "LEARN"];

type Status = {
  state: string;
  cycles_completed: number;
  current_cycle?: { id: string; number: number; stage: string; status: string } | null;
  queued_jobs?: number;
  last_error?: string;
};

export default function CommandCenter() {
  const [status, setStatus] = useState<Status | null>(null);
  const [decision, setDecision] = useState<any>(null);
  const [overview, setOverview] = useState<any>(null);
  const [patterns, setPatterns] = useState<any[]>([]);
  const [graph, setGraph] = useState<any>(null);
  const [pulse, setPulse] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState("");
  const [onboarding, setOnboarding] = useState<{ niche: boolean; llm: boolean } | null>(null);
  const pollRef = useRef<number | null>(null);

  const api_state: any = {};

  const refresh = useCallback(async () => {
    wsApi.get("/autopilot/status").then(setStatus).catch(() => {});
    wsApi.get("/decision").then(setDecision).catch(() => {});
    wsApi.get("/analytics/overview").then(setOverview).catch(() => {});
    wsApi.get("/analytics/patterns").then((r) => setPatterns((r.items ?? []).slice(0, 4))).catch(() => {});
    wsApi.get("/live/agents/graph").then(setGraph).catch(() => {});
    wsApi.get("/live/metrics").then(setPulse).catch(() => {});
    Promise.all([
      api("GET", "/workspaces"),
      wsApi.get("/connections").catch(() => ({ items: [] })),
    ]).then(([wsl, conn]: any) => {
      const me = (wsl.items ?? [])[0] ?? {};
      setOnboarding({
        niche: Boolean(me.niche),
        llm: (conn.items ?? []).some((c: any) => c.key === "llm.api_key" && c.configured)
          || !(api_state.mock_llm ?? true),
      });
    }).catch(() => {});
  }, []);

  useEffect(() => {
    fetch("/api/v1/system/health").then((r) => r.json()).then((h) => {
      api_state.mock_llm = h?.mocks?.llm;
    }).catch(() => {});
    refresh();
    pollRef.current = window.setInterval(refresh, 5000);
    return () => { if (pollRef.current) clearInterval(pollRef.current); };
  }, [refresh]);

  async function act(action: string, body?: any) {
    setBusy(true);
    setErr("");
    try {
      if (action === "start") await wsApi.post("/autopilot/start", body ?? { mode: "CONTINUOUS" });
      else await wsApi.post(`/autopilot/${action}`);
      await refresh();
    } catch (e: any) {
      setErr(e.message);
    } finally {
      setBusy(false);
    }
  }

  const running = status?.state === "RUNNING" || status?.state === "STARTING";
  const paused = status?.state === "PAUSED";
  const stageIdx = STAGES.indexOf(status?.current_cycle?.stage ?? "");
  const blocker = !running && (err || status?.last_error || "") || "";

  // Big state word drives the whole hero
  const headline = running
    ? "Producing"
    : paused
    ? "Paused"
    : status?.state === "STOPPING"
    ? "Stopping…"
    : (status?.cycles_completed ?? 0) > 0
    ? "Standing by"
    : "Ready to launch";

  const busyAgents = (graph?.nodes ?? []).filter((n: any) => n.status === "busy");
  const recentRuns = (graph?.recent_runs ?? []).slice(0, 4);
  const stepsLeft = onboarding ? [!onboarding.niche && "niche", !onboarding.llm && "AI key"].filter(Boolean) : [];

  return (
    <div className="space-y-6">
      {/* ============================== HERO ============================== */}
      <Card className="overflow-hidden">
        <div className="px-6 md:px-8 pt-7 pb-6">
          <div className="flex flex-wrap items-start justify-between gap-5">
            {/* Mission headline */}
            <div className="min-w-0">
              <p className="text-[11px] font-semibold uppercase tracking-[0.12em] mb-1.5"
                 style={{ color: "var(--text-faint)" }}>
                Mission control
              </p>
              <h1 className="text-[34px] md:text-[40px] font-bold leading-none tracking-tight">
                {headline}
                {running && <span className="live-dot inline-block w-2.5 h-2.5 rounded-full align-middle ml-3 mb-1" style={{ background: "var(--accent)" }} />}
              </h1>
              <p className="text-sm mt-2.5" style={{ color: "var(--text-muted)" }}>
                {running ? (
                  <>Cycle <b className="font-mono">#{String(status?.cycles_completed ?? 0).padStart(3, "0")}</b>
                    {" · "}{status?.current_cycle?.stage ?? "…"} stage
                    {busyAgents.length > 0 && <> · <span style={{ color: "var(--accent)" }}>{busyAgents.length} agent{busyAgents.length > 1 ? "s" : ""} working</span></>}
                  </>
                ) : paused ? (
                  <>Cycle #{String(status?.cycles_completed ?? 0).padStart(3, "0")} finished its stage and is holding.</>
                ) : (
                  <>The crew is idle. Press launch and YMONEY finds a trend, makes a video, and publishes it — on its own.</>
                )}
              </p>
            </div>

            {/* Controls */}
            <div className="flex items-center gap-2.5 shrink-0">
              {running || status?.state === "STOPPING" ? (
                <>
                  <button className="btn-outline" onClick={() => act("pause")} disabled={busy || status?.state === "STOPPING"}>
                    Pause
                  </button>
                  <button
                    onClick={() => act("stop")}
                    disabled={busy}
                    className="h-11 px-6 rounded-xl font-semibold text-sm border-2 transition-all hover:bg-[var(--danger-dim)] active:scale-[0.98] disabled:opacity-50"
                    style={{ borderColor: "var(--danger)", color: "var(--danger)", background: "transparent" }}
                  >
                    {status?.state === "STOPPING" ? "Stopping…" : "Stop"}
                  </button>
                </>
              ) : paused ? (
                <>
                  <button className="btn-outline" onClick={() => act("stop")} disabled={busy}>Stop</button>
                  <button
                    onClick={() => act("resume")}
                    disabled={busy}
                    className="h-11 px-7 rounded-xl font-semibold text-sm transition-all active:scale-[0.98] disabled:opacity-50"
                    style={{ background: "var(--accent)", color: "#fff" }}
                  >
                    Resume
                  </button>
                </>
              ) : (
                <>
                  <button className="btn-outline h-11 px-5" onClick={() => act("start", { mode: "SINGLE_CYCLE" })} disabled={busy}
                          title="Run exactly one FIND→…→LEARN cycle">
                    One cycle
                  </button>
                  <button
                    onClick={() => act("start")}
                    disabled={busy}
                    className="h-11 px-8 rounded-xl font-bold text-sm tracking-wide transition-all hover:brightness-105 active:scale-[0.98] disabled:opacity-50"
                    style={{ background: "var(--accent)", color: "#fff", boxShadow: "0 6px 20px -6px var(--accent-glow)" }}
                  >
                    Launch autopilot
                  </button>
                </>
              )}
            </div>
          </div>

          {/* Blocker strip — inline, impossible to miss */}
          {blocker && (
            <div className="mt-5 rounded-xl border px-4 py-3 flex items-center gap-3"
                 style={{ borderColor: "var(--warn)", background: "var(--warn-dim)" }} role="alert">
              <span className="grid place-items-center w-6 h-6 rounded-full text-[12px] font-bold shrink-0"
                    style={{ background: "var(--warn)", color: "#fff" }}>!</span>
              <p className="text-[13px] min-w-0 flex-1" style={{ color: "var(--text)" }}>
                <b style={{ color: "var(--warn)" }}>Launch blocked.</b>{" "}
                <span className="break-words" style={{ color: "var(--text-muted)" }}>{blocker}</span>
              </p>
              <Link to="/health" className="text-xs font-semibold underline shrink-0" style={{ color: "var(--warn)" }}>
                Fix →
              </Link>
            </div>
          )}
          {err && !blocker && (
            <p className="text-xs mt-3" style={{ color: "var(--danger)" }}>{err}</p>
          )}

          {/* Onboarding — one calm line, not a wall of cards */}
          {stepsLeft.length > 0 && !blocker && (
            <div className="mt-5 flex items-center gap-2 flex-wrap text-[13px] rounded-xl border px-4 py-3"
                 style={{ borderColor: "var(--border)", background: "var(--bg-inset)" }}>
              <span style={{ color: "var(--text-muted)" }}>Finish setup ({stepsLeft.length} left):</span>
              {!onboarding?.niche && (
                <Link to="/settings" className="font-medium underline" style={{ color: "var(--accent)" }}>set your niche</Link>
              )}
              {!onboarding?.niche && !onboarding?.llm && <span style={{ color: "var(--text-faint)" }}>·</span>}
              {!onboarding?.llm && (
                <Link to="/settings" className="font-medium underline" style={{ color: "var(--accent)" }}>add your AI key</Link>
              )}
              <span className="text-[12px]" style={{ color: "var(--text-faint)" }}>— or explore in simulation mode first</span>
            </div>
          )}
        </div>

        {/* Pipeline flow — full width, animated */}
        <div className="px-6 md:px-8 py-5 border-t" style={{ borderColor: "var(--border)", background: "var(--bg-inset)" }}>
          <PipelineFlow stages={STAGES} activeIdx={running ? stageIdx : -1} done={running ? stageIdx : (status?.cycles_completed ?? 0) > 0 ? STAGES.length : -1} />
        </div>
      </Card>

      {/* ============================== THE BRIEF ============================== */}
      <div className="grid xl:grid-cols-3 gap-5">
        {/* NOW — live agent activity */}
        <Card className="p-5 flex flex-col">
          <div className="flex items-center justify-between mb-3">
            <h2 className="panel-label">Now</h2>
            <Link to="/live" className="text-[11px] font-medium" style={{ color: "var(--accent)" }}>Live monitor →</Link>
          </div>
          {busyAgents.length > 0 ? (
            <ul className="space-y-2.5">
              {busyAgents.slice(0, 3).map((a: any) => (
                <li key={a.key} className="flex items-center gap-2.5">
                  <StatusDot tone="success" pulse />
                  <span className="text-[13px] font-medium">{a.title}</span>
                  <span className="text-[11.5px] truncate" style={{ color: "var(--text-muted)" }}>{a.current_task ?? a.stage}</span>
                </li>
              ))}
            </ul>
          ) : recentRuns.length > 0 ? (
            <ul className="space-y-2">
              {recentRuns.map((r: any, i: number) => (
                <li key={r.id ?? i} className="flex items-center gap-2.5 text-[12.5px]">
                  <StatusDot tone={r.status === "FAILED" ? "error" : "neutral"} />
                  <span className="font-mono text-[11px] w-24 shrink-0" style={{ color: "var(--text-muted)" }}>{r.agent_key}</span>
                  <span className="truncate" style={{ color: "var(--text-muted)" }}>{r.task_type || "done"}</span>
                  {r.duration_ms != null && (
                    <span className="ml-auto font-mono text-[11px] shrink-0" style={{ color: "var(--text-faint)" }}>
                      {(r.duration_ms / 1000).toFixed(1)}s
                    </span>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>
              Nothing running. Launch the autopilot and the crew picks up work within seconds.
            </p>
          )}
        </Card>

        {/* NEXT — the decision engine */}
        <Card className="p-5">
          <h2 className="panel-label mb-3">Next</h2>
          {decision && !(decision.action === "WAIT" && decision.score === 0) ? (
            <WhyBlock decision={decision} />
          ) : (
            <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>
              No opportunity queued yet — discovery fills this in during the FIND stage.
            </p>
          )}
        </Card>

        {/* LEARNED — what the system figured out */}
        <Card className="p-5">
          <div className="flex items-center justify-between mb-3">
            <h2 className="panel-label">Learned</h2>
            <Link to="/intelligence" className="text-[11px] font-medium" style={{ color: "var(--accent)" }}>All insights →</Link>
          </div>
          {patterns.length > 0 ? (
            <ul className="space-y-2.5">
              {patterns.slice(0, 3).map((p) => (
                <li key={p.pattern_key} className="flex items-start justify-between gap-3">
                  <p className="text-[12.5px] leading-snug">{p.description}</p>
                  <Badge tone={p.confidence === "high" ? "success" : p.confidence === "medium" ? "info" : "neutral"}>
                    {p.improvement_pct >= 0 ? "+" : ""}{p.improvement_pct.toFixed(0)}%
                  </Badge>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-[13px]" style={{ color: "var(--text-muted)" }}>
              After a few published posts, YMONEY starts reporting what works best for your channel.
            </p>
          )}
        </Card>
      </div>

      {/* ============================== RESULTS BAND ============================== */}
      <Card className="p-5">
        <div className="flex items-center justify-between mb-3">
          <h2 className="panel-label">Results so far</h2>
          <Link to="/analytics" className="text-[11px] font-medium" style={{ color: "var(--accent)" }}>Analytics →</Link>
        </div>
        <div className="grid md:grid-cols-[1fr_260px] gap-5 items-center">
          <div className="grid grid-cols-2 sm:grid-cols-4 gap-3">
            <BigStat label="Views" value={fmtNum(overview?.totals?.views)} />
            <BigStat label="Followers +" value={fmtNum(overview?.totals?.followers_gained)} />
            <BigStat label="Published" value={fmtNum(overview?.posts_published)} />
            <BigStat label="Spend" value={fmtUsd(overview?.cost_total_usd)} />
          </div>
          {/* Hourly activity sparkline from live metrics */}
          <div className="hidden md:block">
            <p className="text-[10px] uppercase tracking-wide mb-1" style={{ color: "var(--text-faint)" }}>
              Agent activity · last hour
            </p>
            <Sparkline data={(pulse?.series ?? []).map((p: any) => p.runs ?? 0)} />
          </div>
        </div>
      </Card>
    </div>
  );
}

/* ================================ pieces ================================ */

/** Connected pipeline with animated progress. done = stages fully completed (-1 none). */
function PipelineFlow({ stages, activeIdx, done }: { stages: string[]; activeIdx: number; done: number }) {
  return (
    <div className="flex items-center" role="img" aria-label="pipeline progress">
      {stages.map((s, i) => {
        const state = i === activeIdx ? "active" : i < activeIdx || (activeIdx < 0 && i < done) ? "done" : "idle";
        return (
          <div key={s} className="flex items-center flex-1 last:flex-none min-w-0">
            <div className="flex flex-col items-center gap-1.5 shrink-0" style={{ minWidth: 46 }}>
              <span
                className={`grid place-items-center w-7 h-7 rounded-full text-[10px] font-bold border-2 transition-all duration-500 ${state === "active" ? "live-dot" : ""}`}
                style={
                  state === "active"
                    ? { background: "var(--accent)", borderColor: "var(--accent)", color: "#fff", boxShadow: "0 0 0 5px var(--accent-dim)" }
                    : state === "done"
                    ? { background: "var(--accent-dim)", borderColor: "var(--accent)", color: "var(--accent)" }
                    : { background: "var(--bg-panel)", borderColor: "var(--border-strong)", color: "var(--text-faint)" }
                }
              >
                {state === "done" ? "✓" : state === "active" ? "●" : i + 1}
              </span>
              <span className="text-[9px] font-bold tracking-[0.1em] whitespace-nowrap"
                    style={{ color: state === "idle" ? "var(--text-faint)" : state === "active" ? "var(--accent)" : "var(--text-muted)" }}>
                {s}
              </span>
            </div>
            {i < stages.length - 1 && (
              <div className="flex-1 h-[3px] rounded-full mx-1 mb-4 overflow-hidden" style={{ background: "var(--border)" }} aria-hidden>
                <div className="h-full rounded-full transition-all duration-700"
                     style={{ width: state === "done" ? "100%" : state === "active" ? "45%" : "0%", background: "var(--accent)", opacity: state === "active" ? 0.5 : 1 }} />
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function BigStat({ label, value }: { label: string; value: string | number }) {
  return (
    <div>
      <p className="text-[22px] font-bold leading-none">{value}</p>
      <p className="text-[10.5px] uppercase tracking-wide mt-1" style={{ color: "var(--text-muted)" }}>{label}</p>
    </div>
  );
}

/** Tiny dependency-free sparkline of hourly agent runs. */
function Sparkline({ data }: { data: number[] }) {
  if (!data.length) return <div className="h-8" />;
  const max = Math.max(...data, 1);
  const w = 240, h = 30, step = data.length > 1 ? w / (data.length - 1) : 0;
  const pts = data.map((y, i) => `${(i * step).toFixed(1)},${(h - 2 - (y / max) * (h - 6)).toFixed(1)}`).join(" ");
  return (
    <svg viewBox={`0 0 ${w} ${h}`} width="100%" height={h} role="img" aria-label="hourly activity sparkline">
      <polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
    </svg>
  );
}

export function WhyBlock({ decision }: { decision: any }) {
  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between gap-3">
        <Badge tone={
          decision.action === "PRODUCE" ? "success"
          : decision.action === "HUMAN_REVIEW" ? "warning"
          : decision.action === "SKIP" ? "error" : "info"
        }>
          {String(decision.action).replace(/_/g, " ")}
        </Badge>
        {decision.score > 0 && (
          <span className="font-mono text-sm">{Number(decision.score).toFixed(0)}/100</span>
        )}
      </div>
      {decision.topic && <p className="text-sm font-medium leading-snug">{decision.topic}</p>}
      <ul className="space-y-1">
        {(decision.reasons ?? []).slice(0, 4).map((r: string, i: number) => (
          <li key={i} className="text-[12.5px] flex gap-2">
            <span style={{ color: "var(--text-muted)" }}>—</span>{r}
          </li>
        ))}
      </ul>
      {(decision.factors ?? []).length > 0 && (
        <details>
          <summary className="text-xs cursor-pointer select-none" style={{ color: "var(--text-muted)" }}>
            Score factors
          </summary>
          <div className="mt-2 space-y-1">
            {decision.factors.map((f: any, i: number) => (
              <div key={i} className="flex justify-between text-[11px]">
                <span style={{ color: "var(--text-muted)" }}>{f.name.replace(/_/g, " ")}</span>
                <span className={`font-mono ${f.contribution > 0 ? "text-emerald-500" : f.contribution < 0 ? "text-red-500" : "opacity-60"}`}>
                  {f.value}
                </span>
              </div>
            ))}
          </div>
        </details>
      )}
      {decision.confidence != null && decision.confidence > 0 && (
        <p className="text-[11px]" style={{ color: "var(--text-muted)" }}>
          Confidence: {(decision.confidence * 100).toFixed(0)}%
        </p>
      )}
    </div>
  );
}

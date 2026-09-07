import { useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { wsApi } from "../lib/api";
import { Badge, Card, PageHeader, StatusDot, fmtUsd } from "../components/ui";

type SeriesPoint = { minute: string; runs: number; failures: number; cost_usd: number; events: number };
type Metrics = {
  series: SeriesPoint[];
  now: {
    running_agents: number;
    runs_last_hour: number;
    failures_last_hour: number;
    cost_last_hour_usd: number;
    total_cost_usd: number;
    completed_runs: number;
  };
};
type GraphNode = {
  key: string;
  title: string;
  stage: string;
  status: "idle" | "busy" | "error" | "disabled";
  current_task: string | null;
  runs: number;
  failures: number;
  avg_ms: number | null;
  cost_usd: number;
};
type Graph = { nodes: GraphNode[]; recent_runs: any[] };

const POLL_MS = 4000;

export default function LiveMonitor() {
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [paused, setPaused] = useState(false);
  const [lastTick, setLastTick] = useState<string>("");

  const load = useCallback(async () => {
    const [m, g] = await Promise.all([
      wsApi.get("/live/metrics").catch(() => null),
      wsApi.get("/live/agents/graph").catch(() => null),
    ]);
    if (m) { setMetrics(m); setLastTick(new Date().toLocaleTimeString()); }
    if (g) setGraph(g);
  }, []);

  useEffect(() => {
    load();
    if (paused) return;
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [load, paused]);

  const now = metrics?.now;
  return (
    <div className="space-y-5">
      <PageHeader
        title="Live Monitor"
        subtitle="Real-time system pulse and the agent work graph — refreshing every few seconds."
        actions={
          <button className="btn-outline" onClick={() => setPaused((p) => !p)}>
            {paused ? "▶ Resume live" : "⏸ Freeze"}
          </button>
        }
      />

      {/* Headline strip */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Agents busy now" value={now?.running_agents ?? 0} accent={Boolean(now?.running_agents)} live />
        <Stat label="Runs · last hour" value={now?.runs_last_hour ?? 0} />
        <Stat label="Failures · last hour" value={now?.failures_last_hour ?? 0} danger={Boolean(now?.failures_last_hour)} />
        <Stat label="Cost · last hour" value={fmtUsd(now?.cost_last_hour_usd ?? 0)} />
      </div>

      {/* Real-time charts */}
      <div className="grid lg:grid-cols-2 gap-5">
        <Card className="p-4">
          <ChartHeader title="Agent activity" sub="runs per minute" right={<LiveBadge on={!paused} />} />
          <LiveBars
            data={(metrics?.series ?? []).map((p) => ({ x: p.minute, y: p.runs }))}
            color="var(--accent)"
          />
        </Card>
        <Card className="p-4">
          <ChartHeader title="Problems" sub="errors + warnings per minute" />
          <LiveBars
            data={(metrics?.series ?? []).map((p) => ({ x: p.minute, y: p.failures }))}
            color="var(--danger)"
          />
        </Card>
      </div>

      {/* Agent work graph */}
      <Card className="p-4">
        <ChartHeader
          title="Agent work graph"
          sub="the FIND→…→LEARN pipeline, live"
          right={
            <Link to="/agents" className="btn-outline !py-1 !px-2.5 text-xs">Agent details →</Link>
          }
        />
        <AgentDag nodes={graph?.nodes ?? []} />
      </Card>

      {/* Recent runs ticker */}
      <Card className="p-4">
        <ChartHeader title="Run log" sub="most recent agent executions" right={<span className="text-[11px] font-mono" style={{ color: "var(--text-faint)" }}>{lastTick}</span>} />
        <div className="space-y-1.5 max-h-56 overflow-y-auto">
          {(graph?.recent_runs ?? []).length === 0 && (
            <p className="text-[13px] py-4 text-center" style={{ color: "var(--text-muted)" }}>
              No agent runs yet — press START on the Command Center.
            </p>
          )}
          {(graph?.recent_runs ?? []).map((r, i) => (
            <div key={r.id ?? i} className="flex items-center gap-3 text-[12.5px] py-1">
              <StatusDot tone={r.status === "FAILED" ? "error" : r.status === "RUNNING" ? "success" : "neutral"} pulse={r.status === "RUNNING"} />
              <span className="font-mono text-[11.5px] w-28 shrink-0" style={{ color: "var(--text-muted)" }}>{r.agent_key}</span>
              <span className="truncate flex-1">{r.task_type || "—"}</span>
              <span className="font-mono text-[11px] shrink-0" style={{ color: "var(--text-faint)" }}>
                {r.duration_ms != null ? `${(r.duration_ms / 1000).toFixed(1)}s` : "…"}
              </span>
              <span className="font-mono text-[11px] w-16 text-right shrink-0" style={{ color: "var(--text-faint)" }}>
                {fmtUsd(r.cost_usd ?? 0)}
              </span>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}

/* ------------------------------- Pieces ---------------------------------- */

function Stat({ label, value, accent, danger, live }: { label: string; value: number | string; accent?: boolean; danger?: boolean; live?: boolean }) {
  return (
    <Card className="p-4">
      <div className="flex items-center gap-1.5">
        {live && <StatusDot tone="success" pulse />}
        <p className="text-[11px] font-medium uppercase tracking-wide" style={{ color: "var(--text-muted)" }}>{label}</p>
      </div>
      <p className="text-2xl font-semibold mt-1" style={{ color: danger ? "var(--danger)" : accent ? "var(--accent)" : "var(--text)" }}>
        {value}
      </p>
    </Card>
  );
}

function ChartHeader({ title, sub, right }: { title: string; sub?: string; right?: React.ReactNode }) {
  return (
    <div className="flex items-center justify-between gap-3 mb-3">
      <div>
        <h2 className="text-sm font-semibold">{title}</h2>
        {sub && <p className="text-[11.5px]" style={{ color: "var(--text-muted)" }}>{sub}</p>}
      </div>
      {right}
    </div>
  );
}

function LiveBadge({ on }: { on: boolean }) {
  return (
    <span className="badge" style={on
      ? { background: "var(--accent-dim)", color: "var(--accent)" }
      : { background: "var(--bg-subtle)", color: "var(--text-muted)" }}>
      <StatusDot tone={on ? "success" : "neutral"} pulse={on} /> {on ? "LIVE" : "PAUSED"}
    </span>
  );
}

/** Minimal animated bar series — fills width, no dependencies. */
function LiveBars({ data, color }: { data: { x: string; y: number }[]; color: string }) {
  const max = Math.max(...data.map((d) => d.y), 1);
  const n = data.length || 60;
  const bw = 100 / n;
  return (
    <div>
      <div className="flex items-end gap-[2px] h-28" role="img" aria-label="live bar chart">
        {data.map((d, i) => {
          const h = d.y === 0 ? 2 : Math.max(6, (d.y / max) * 100);
          return (
            <div key={i} className="flex-1 rounded-t-[3px] transition-all duration-700"
                 style={{ height: `${h}%`, minHeight: 2, background: color, opacity: d.y === 0 ? 0.15 : 0.85 }}
                 title={`${d.x} — ${d.y}`}>
              {i % 10 === 0 && <span className="sr-only">{d.x}</span>}
            </div>
          );
        })}
      </div>
      <div className="flex justify-between text-[10px] font-mono mt-1.5" style={{ color: "var(--text-faint)" }}>
        <span>{data[0]?.x ?? ""}</span>
        <span>{data[Math.floor(n / 2)]?.x ?? ""}</span>
        <span>{data[n - 1]?.x ?? ""}</span>
      </div>
    </div>
  );
}

/** Horizontal pipeline DAG — one row per stage, agents as nodes. */
function AgentDag({ nodes }: { nodes: GraphNode[] }) {
  if (nodes.length === 0) {
    return <p className="text-[13px] py-6 text-center" style={{ color: "var(--text-muted)" }}>Loading graph…</p>;
  }
  const stages: { stage: string; agents: GraphNode[] }[] = [];
  for (const n of nodes) {
    const last = stages[stages.length - 1];
    if (last && last.stage === n.stage) last.agents.push(n);
    else stages.push({ stage: n.stage, agents: [n] });
  }

  return (
    <div className="overflow-x-auto pb-2">
      <div className="flex items-stretch gap-0 min-w-[860px]">
        {stages.map((st, si) => (
          <div key={st.stage + si} className="flex items-center shrink-0">
            {/* stage column */}
            <div className="flex flex-col items-center gap-2 w-[128px]">
              <span className="text-[10px] font-mono font-bold tracking-widest px-2 py-0.5 rounded-full"
                    style={{ background: "var(--bg-subtle)", color: "var(--text-muted)" }}>
                {st.stage}
              </span>
              {st.agents.map((a) => <AgentNode key={a.key} node={a} />)}
            </div>
            {/* connector */}
            {si < stages.length - 1 && (
              <div className="w-10 flex items-center justify-center" aria-hidden>
                <svg width="40" height="12" viewBox="0 0 40 12">
                  <line x1="0" y1="6" x2="32" y2="6" stroke="var(--border-strong)" strokeWidth="1.5" />
                  <path d="M32 2 L40 6 L32 10 Z" fill="var(--border-strong)" />
                </svg>
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}

const NODE_STYLE: Record<string, { bg: string; fg: string; border: string; icon: string }> = {
  busy: { bg: "var(--accent)", fg: "#fff", border: "var(--accent)", icon: "◉" },
  idle: { bg: "var(--bg-panel)", fg: "var(--text-muted)", border: "var(--border-strong)", icon: "○" },
  error: { bg: "var(--danger-dim)", fg: "var(--danger)", border: "var(--danger)", icon: "!" },
  disabled: { bg: "var(--bg-inset)", fg: "var(--text-faint)", border: "var(--border)", icon: "⊘" },
};

function AgentNode({ node }: { node: GraphNode }) {
  const s = NODE_STYLE[node.status] ?? NODE_STYLE.idle;
  return (
    <div
      className="w-[116px] rounded-xl border px-2 py-2.5 text-center transition-all duration-300"
      style={{
        background: s.bg,
        borderColor: s.border,
        color: s.fg,
        boxShadow: node.status === "busy" ? "0 0 0 4px var(--accent-dim)" : undefined,
      }}
      title={`${node.title} — ${node.status}${node.current_task ? ` (${node.current_task})` : ""}\nruns: ${node.runs} · failures: ${node.failures}${node.avg_ms ? ` · avg ${(node.avg_ms / 1000).toFixed(1)}s` : ""}`}
    >
      <p className="text-[11px] font-semibold leading-tight truncate">{node.title}</p>
      <p className="text-[10px] font-mono mt-0.5 opacity-80">
        {node.status === "busy" ? <span className="live-dot inline-block w-1.5 h-1.5 rounded-full bg-current mr-1" /> : s.icon + " "}
        {node.runs} runs
      </p>
    </div>
  );
}

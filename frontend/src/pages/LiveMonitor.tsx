import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch, useInterval } from "../hooks/hooks";
import { Badge, Card, PageHeader, statusTone } from "../components/ui";
import { Bars } from "../components/charts";
import { fmtUSD } from "../lib/format";

export default function LiveMonitor() {
  const [frozen, setFrozen] = useState(false);
  const metrics = useFetch(() => wsApi.get("/live/metrics"), []);
  const graph = useFetch(() => wsApi.get("/live/agents/graph"), []);
  useInterval(() => {
    if (!frozen) {
      metrics.reload();
      graph.reload();
    }
  }, 4000);

  const m: any = metrics.data;
  const buckets: any[] = m?.buckets ?? m?.per_minute ?? [];
  const runs: number[] = buckets.map((b: any) => b.runs ?? b.count ?? 0);
  const errors: number[] = buckets.map((b: any) => b.errors ?? b.failures ?? 0);
  const nodes: any[] = (graph.data as any)?.nodes ?? (graph.data as any)?.agents ?? [];

  return (
    <div className="space-y-4">
      <PageHeader title="Live Monitor" subtitle="Per-minute system pulse and the live 22-agent work graph."
        actions={<button className={`!text-xs ${frozen ? "btn-primary" : "btn-outline"}`} onClick={() => setFrozen(!frozen)}>{frozen ? "▶ Resume" : "⏸ Freeze"}</button>} />
      <div className="grid md:grid-cols-2 gap-4">
        <Card>
          <b className="text-[13.5px]">Agent runs / min (60m)</b>
          <div className="mt-2"><Bars data={runs.map((v, i) => ({ label: `${i}`, value: v }))} /></div>
        </Card>
        <Card>
          <b className="text-[13.5px]">Errors / min (60m)</b>
          <div className="mt-2"><Bars data={errors.map((v, i) => ({ label: `${i}`, value: v }))} /></div>
        </Card>
      </div>
      <Card>
        <b className="text-[14px]">Agent work graph</b>
        <div className="grid sm:grid-cols-2 lg:grid-cols-4 gap-2.5 mt-3">
          {nodes.map((n: any, i: number) => (
            <div key={n.key ?? n.agent ?? i} className="rounded-xl p-3" style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
              <div className="flex items-center gap-2">
                <span className="live-dot inline-block w-2 h-2 rounded-full"
                  style={{ background: n.state === "busy" ? "var(--accent)" : n.state === "error" ? "var(--danger)" : "var(--border-strong)" }} />
                <b className="text-[12.5px] truncate">{n.title ?? n.key ?? n.agent}</b>
                {i < nodes.length - 1 && <span className="ml-auto" style={{ color: "var(--text-faint)" }}>→</span>}
              </div>
              <div className="flex gap-2 mt-1.5 font-mono text-[11px]" style={{ color: "var(--text-muted)" }}>
                <Badge tone={statusTone(n.state)}>{n.state ?? "idle"}</Badge>
                <span>{n.runs ?? 0} runs</span>
                {(n.failure_rate ?? n.failures) != null && <span>{Math.round((n.failure_rate ?? 0) * 100)}% fail</span>}
                {(n.avg_duration_ms ?? n.avg_ms) != null && <span>{n.avg_duration_ms ?? n.avg_ms}ms</span>}
                {(n.cost_usd ?? 0) > 0 && <span>{fmtUSD(n.cost_usd)}</span>}
              </div>
            </div>
          ))}
          {!nodes.length && <div className="text-[13px]" style={{ color: "var(--text-faint)" }}>No live graph data — start the autopilot to light it up.</div>}
        </div>
      </Card>
    </div>
  );
}

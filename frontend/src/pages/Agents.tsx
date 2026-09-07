import { useCallback, useEffect, useState } from "react";
import { wsApi } from "../lib/api";
import {
  AsyncSection, Badge, Card, Modal, PageHeader, StatusDot, fmtDate, fmtUsd,
} from "../components/ui";

export default function Agents() {
  const [agents, setAgents] = useState<any[]>([]);
  const [recent, setRecent] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [detailKey, setDetailKey] = useState<string | null>(null);
  const [capabilities, setCapabilities] = useState<{ skills: any[]; tools: any[] } | null>(null);
  const [audits, setAudits] = useState<any[] | null>(null);

  const load = useCallback(async () => {
    setLoading(true); setError(null);
    try {
      const [r, catalog] = await Promise.all([
        wsApi.get("/agents"),
        wsApi.get("/agents/capabilities"),
      ]);
      setAgents(r.items ?? []);
      setRecent(r.recent_runs ?? []);
      setCapabilities({ skills: catalog.skills ?? [], tools: catalog.tools ?? [] });
      wsApi.get("/agents/tool-audits?limit=20")
        .then((a) => setAudits(a.items ?? []))
        .catch(() => setAudits(null)); // admin-only; viewers see nothing rather than an error
    } catch (e: any) { setError(e.message); } finally { setLoading(false); }
  }, []);

  useEffect(() => {
    load();
    const t = setInterval(load, 6000);
    return () => clearInterval(t);
  }, [load]);

  async function toggle(key: string) {
    const current = agents.find((a) => a.key === key);
    await wsApi.put(`/agents/config/${key}`, { enabled: !current?.enabled });
    load();
  }

  return (
    <div className="space-y-5">
      <PageHeader
        title="Agent Center"
        subtitle="The autonomous crew. They operate on their own; you observe and can disable any of them."
      />

      {capabilities && (
        <div className="grid lg:grid-cols-2 gap-4">
          <CapabilityPanel title="Registered skills" items={capabilities.skills} kind="skill" />
          <CapabilityPanel title="Controlled tools" items={capabilities.tools} kind="tool" />
        </div>
      )}

      <AsyncSection data={agents} loading={loading} error={error} onRetry={load}
        empty="No agents registered">
        {(list) => (
          <div className="grid md:grid-cols-2 xl:grid-cols-3 gap-4">
            {(list as any[]).map((a) => (
              <Card key={a.key} className="flex flex-col">
                <div className="flex items-start justify-between gap-2 mb-1">
                  <h3 className="font-semibold text-sm">{a.title}</h3>
                  <span className="flex items-center text-[11px]" style={{ color: "var(--text-muted)" }}>
                    <StatusDot tone={a.status === "busy" ? "success" : "neutral"} pulse={a.status === "busy"} />
                    {a.status}
                  </span>
                </div>                  <p className="text-[12px] leading-relaxed mb-3" style={{ color: "var(--text-muted)" }}>{a.description}</p>
                  <div className="flex flex-wrap gap-1 mb-3">
                    {(a.skills ?? []).map((skill: string) => <Badge key={skill} tone="info">skill: {skill}</Badge>)}
                    {(a.tools ?? []).map((tool: string) => <Badge key={tool} tone="neutral">tool: {tool}</Badge>)}
                  </div>

                <dl className="grid grid-cols-4 gap-1 text-center text-[12px] mb-3">
                  <div><dt className="text-[9px] uppercase" style={{ color: "var(--text-muted)" }}>Runs</dt><dd>{a.runs}</dd></div>
                  <div><dt className="text-[9px] uppercase" style={{ color: "var(--text-muted)" }}>Fail</dt><dd>{Math.round(a.failure_rate * 100)}%</dd></div>
                  <div><dt className="text-[9px] uppercase" style={{ color: "var(--text-muted)" }}>Avg ms</dt><dd>{a.avg_duration_ms ?? "—"}</dd></div>
                  <div><dt className="text-[9px] uppercase" style={{ color: "var(--text-muted)" }}>Cost</dt><dd>{fmtUsd(a.total_cost_usd)}</dd></div>
                </dl>
                <div className="mt-auto flex gap-2">
                  <button className="btn-outline flex-1 !py-1 text-xs" onClick={() => setDetailKey(a.key)}>Details & logs</button>
                  <button
                    className={`flex-1 !py-1 text-xs rounded-lg font-medium transition-colors ${a.enabled ? "bg-emerald-500/15 text-emerald-600 dark:text-emerald-400 hover:bg-emerald-500/25" : "bg-zinc-500/10 opacity-70 hover:opacity-100"}`}
                    onClick={() => toggle(a.key)}
                  >
                    {a.enabled ? "Enabled — click to disable" : "Disabled"}
                  </button>
                </div>
              </Card>
            ))}
          </div>
        )}
      </AsyncSection>

      {audits && audits.length > 0 && (
        <section>
          <h2 className="text-xs font-semibold uppercase tracking-wider mb-2.5" style={{ color: "var(--text-muted)" }}>
            Recent tool executions
          </h2>
          <Card pad={false} className="overflow-x-auto">
            <table className="table">
              <thead><tr><th>Agent</th><th>Tool</th><th>Status</th><th>Summary</th><th>Duration</th><th>Cost</th><th>When</th></tr></thead>
              <tbody>
                {audits.map((t: any) => (
                  <tr key={t.id}>
                    <td>{t.agent_key}</td>
                    <td className="font-mono text-[12px]">{t.tool_name}</td>
                    <td><Badge tone={t.status === "COMPLETED" ? "success" : t.status === "FAILED" ? "error" : "info"}>{t.status.toLowerCase()}</Badge></td>
                    <td className="max-w-[240px] truncate text-[12px]" style={{ color: "var(--text-muted)" }}>{t.error || t.output_summary || t.input_summary}</td>
                    <td>{t.duration_ms != null ? `${t.duration_ms}ms` : "—"}</td>
                    <td>{fmtUsd(t.actual_cost_usd ?? t.estimated_cost_usd)}</td>
                    <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtDate(t.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
        </section>
      )}

      <section>
        <h2 className="text-xs font-semibold uppercase tracking-wider mb-2.5" style={{ color: "var(--text-muted)" }}>Latest runs across the crew</h2>
        <Card pad={false} className="overflow-x-auto">
          <table className="table">
            <thead><tr><th>Agent</th><th>Task</th><th>Status</th><th>Duration</th><th>Output</th><th>When</th></tr></thead>
            <tbody>
              {recent.slice(0, 15).map((r: any, i: number) => (
                <tr key={i}>
                  <td>{r.agent_key}</td>
                  <td>{r.task_type}</td>
                  <td><Badge tone={r.status === "COMPLETED" ? "success" : r.status === "RUNNING" ? "info" : "error"}>{r.status.toLowerCase()}</Badge></td>
                  <td>{r.duration_ms != null ? `${r.duration_ms}ms` : "—"}</td>
                  <td className="max-w-[260px] truncate text-[12px]" style={{ color: "var(--text-muted)" }}>{r.output_summary}</td>
                  <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtDate(r.created_at)}</td>
                </tr>
              ))}
              {recent.length === 0 && <tr><td colSpan={6} className="text-center py-8" style={{ color: "var(--text-muted)" }}>No runs yet.</td></tr>}
            </tbody>
          </table>
        </Card>
      </section>

      <AgentDetailModal agentKey={detailKey} onClose={() => setDetailKey(null)} />
    </div>
  );
}

function AgentDetailModal({ agentKey, onClose }: { agentKey: string | null; onClose: () => void }) {
  const [detail, setDetail] = useState<any>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!agentKey) { setDetail(null); return; }
    setDetail(null); setError(null);
    wsApi.get(`/agents/${agentKey}`).then(setDetail).catch((e) => setError(e.message));
  }, [agentKey]);

  return (
    <Modal open={!!agentKey} onClose={onClose} wide>
      {!detail && !error && <p>Loading…</p>}
      {error && <p className="text-sm text-red-500">{error}</p>}
      {detail && (
        <div className="space-y-5">
          <div>
            <h2 className="text-lg font-semibold">{detail.title}</h2>
            <p className="text-[13px] mt-0.5" style={{ color: "var(--text-muted)" }}>{detail.description}</p>
          </div>

          <div className="grid grid-cols-4 gap-3 text-sm">
            <Stat k="Runs" v={detail.stats.runs} />
            <Stat k="Failure rate" v={`${Math.round(detail.stats.failure_rate * 100)}%`} />
            <Stat k="Avg duration" v={detail.stats.avg_duration_ms != null ? `${detail.stats.avg_duration_ms}ms` : "—"} />
            <Stat k="Total cost" v={fmtUsd(detail.stats.total_cost_usd)} />
          </div>

          <div className="flex gap-4 text-[13px]">
            <Badge tone={detail.enabled ? "success" : "error"}>{detail.enabled ? "enabled" : "disabled"}</Badge>
            <span style={{ color: "var(--text-muted)" }}>
              model: {detail.model || "system default"} · timeout: {detail.timeout_seconds}s ·
              cost limit: {detail.cost_limit_usd != null ? `$${detail.cost_limit_usd}` : "none"}
            </span>
          </div>
          <div className="flex flex-wrap gap-1">
            {(detail.skills ?? []).map((skill: string) => <Badge key={skill} tone="info">skill: {skill}</Badge>)}
            {(detail.tools ?? []).map((tool: string) => <Badge key={tool} tone="neutral">tool: {tool}</Badge>)}
          </div>
          <div className="text-[11px]" style={{ color: "var(--text-muted)" }}>
            Permissions: {(detail.permissions ?? []).join(", ") || "none"} · execution: {detail.execution_policy}
          </div>

          <div>
            <h3 className="text-xs font-semibold uppercase tracking-wider mb-2" style={{ color: "var(--text-muted)" }}>Run log (last 50)</h3>
            <div className="max-h-96 overflow-y-auto rounded-lg border" style={{ borderColor: "var(--border)" }}>
              <table className="table">
                <thead><tr><th>Task</th><th>Status</th><th>Output / error</th><th>When</th></tr></thead>
                <tbody>
                  {detail.runs.map((r: any) => (
                    <RunRow key={r.id} run={r} />
                  ))}
                  {detail.runs.length === 0 && (
                    <tr><td colSpan={4} className="text-center py-6" style={{ color: "var(--text-muted)" }}>This agent has not run yet.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      )}
    </Modal>
  );
}

function RunRow({ run }: { run: any }) {
  const [expanded, setExpanded] = useState(false);
  const steps: any[] = run.steps ?? [];
  return (
    <>
      <tr
        className={steps.length ? "cursor-pointer" : ""}
        onClick={steps.length ? () => setExpanded((e) => !e) : undefined}
      >
        <td>
          {run.task_type}
          {steps.length > 0 && (
            <span className="ml-1.5 text-[10px]" style={{ color: "var(--text-muted)" }}>
              {expanded ? "▾" : "▸"} {steps.length} step{steps.length === 1 ? "" : "s"}
            </span>
          )}
        </td>
        <td><Badge tone={run.status === "COMPLETED" ? "success" : run.status === "RUNNING" ? "info" : "error"}>{run.status.toLowerCase()}</Badge></td>
        <td className="max-w-[300px] truncate text-[12px]" style={{ color: run.error ? "var(--danger)" : "var(--text-muted)" }}>
          {run.error || run.output_summary}
        </td>
        <td className="text-[11px] whitespace-nowrap" style={{ color: "var(--text-muted)" }}>{fmtDate(run.created_at)}</td>
      </tr>
      {expanded && steps.map((s: any, i: number) => (
        <tr key={`${run.id}-step-${i}`} style={{ background: "var(--bg-subtle)" }}>
          <td className="pl-5 text-[12px]">
            <span className="font-mono">{s.step}</span>
            {s.detail && <span className="block text-[11px]" style={{ color: "var(--text-muted)" }}>{s.detail}</span>}
          </td>
          <td>
            <Badge tone={s.status === "ok" ? "success" : s.status === "running" ? "info" : "error"}>{s.status}</Badge>
          </td>
          <td className="text-[11px]" style={{ color: "var(--text-muted)" }}>
            {s.duration_ms != null ? `${(s.duration_ms / 1000).toFixed(1)}s` : ""}
          </td>
          <td />
        </tr>
      ))}
    </>
  );
}

function CapabilityPanel({ title, items, kind }: { title: string; items: any[]; kind: "skill" | "tool" }) {
  return (
    <Card>
      <h2 className="text-xs font-semibold uppercase tracking-wider mb-3" style={{ color: "var(--text-muted)" }}>{title}</h2>
      <div className="space-y-2.5">
        {items.map((item) => (
          <div key={item.key ?? item.name} className="rounded-lg border p-3" style={{ borderColor: "var(--border)" }}>
            <div className="flex items-center justify-between gap-2">
              <span className="font-medium text-sm">{item.title ?? item.name}</span>
              <Badge tone={kind === "skill" ? "info" : "neutral"}>{item.version ?? item.provider}</Badge>
            </div>
            <p className="text-[12px] mt-1" style={{ color: "var(--text-muted)" }}>{item.description}</p>
            {kind === "tool" && (
              <p className="text-[11px] mt-1" style={{ color: "var(--text-muted)" }}>
                Permissions: {(item.permissions ?? []).join(", ") || "none"} · timeout: {item.timeout_seconds}s · estimate: {fmtUsd(item.estimated_cost_usd)}
              </p>
            )}
          </div>
        ))}
      </div>
    </Card>
  );
}

function Stat({ k, v }: any) {
  return (
    <div className="rounded-lg p-3 text-center" style={{ background: "var(--bg-subtle)" }}>
      <dd className="font-semibold">{v}</dd>
      <dt className="text-[10px] uppercase mt-0.5" style={{ color: "var(--text-muted)" }}>{k}</dt>
    </div>
  );
}


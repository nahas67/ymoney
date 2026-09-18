import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Field, Modal, PageHeader, Section, statusTone } from "../components/ui";
import { fmtAgo, fmtUSD } from "../lib/format";

export default function Agents() {
  const fleet = useFetch(() => wsApi.get("/agents"), []);
  const caps = useFetch(() => wsApi.get("/agents/capabilities"), []);
  const audits = useFetch(() => wsApi.get("/agents/tool-audits?limit=30"), []);
  const [open, setOpen] = useState<string | null>(null);
  const [view, setView] = useState<"fleet" | "audits">("fleet");

  return (
    <div className="space-y-4">
      <PageHeader title="Agents" subtitle="The 12-agent crew: live status, per-agent config, run history with step traces, and the tool-call audit trail."
        actions={
          <div className="flex gap-1.5">
            <button className={`tab ${view === "fleet" ? "active" : ""}`} onClick={() => setView("fleet")}>Fleet</button>
            <button className={`tab ${view === "audits" ? "active" : ""}`} onClick={() => setView("audits")}>Tool audits</button>
          </div>
        } />

      {view === "fleet" && (
        <Section data={(fleet.data as any)?.items} loading={fleet.loading} error={fleet.error} onRetry={fleet.reload}
          empty="No agent data" emptyHint="Agents register on backend boot.">
          {(list) => (
            <div className="grid md:grid-cols-2 lg:grid-cols-3 gap-3">
              {list.map((a: any) => (
                <Card key={a.key} className="cursor-pointer" style={{ padding: 15 }} >
                  <div onClick={() => setOpen(a.key)}>
                    <div className="flex gap-2 items-center mb-1">
                      <b className="text-[13.5px]">{a.title}</b>
                      <Badge tone={a.enabled ? (a.status === "busy" ? "warning" : "success") : "muted"}>
                        {a.enabled ? a.status : "disabled"}
                      </Badge>
                    </div>
                    <div className="text-[12.5px] line-clamp-2" style={{ color: "var(--text-muted)" }}>{a.description}</div>
                    <div className="flex gap-3 mt-2 font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>
                      <span>{a.runs} runs</span>
                      <span>{(a.failure_rate * 100).toFixed(0)}% fail</span>
                      <span>{fmtUSD(a.total_cost_usd)}</span>
                    </div>
                  </div>
                </Card>
              ))}
            </div>
          )}
        </Section>
      )}

      {view === "audits" && (
        <Section data={(audits.data as any)?.items} loading={audits.loading} error={audits.error} onRetry={audits.reload}
          empty="No tool calls audited" emptyHint="Controlled tool invocations are recorded here.">
          {(list) => (
            <Card pad={false} className="overflow-x-auto">
              <table className="table">
                <thead><tr><th>Agent</th><th>Tool</th><th>Status</th><th>Duration</th><th>When</th></tr></thead>
                <tbody>
                  {list.map((t: any) => (
                    <tr key={t.id}>
                      <td className="font-mono text-[12px]">{t.agent_key}</td>
                      <td className="font-mono text-[12px]">{t.tool_name}</td>
                      <td><Badge tone={statusTone(t.status)}>{t.status}</Badge></td>
                      <td className="font-mono">{t.duration_ms ?? "—"}ms</td>
                      <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtAgo(t.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </Section>
      )}

      <Modal open={!!open} onClose={() => setOpen(null)} title="Agent detail" wide>
        {open && <AgentDetail agentKey={open} capabilities={(caps.data as any)} />}
      </Modal>
    </div>
  );
}

function AgentDetail({ agentKey, capabilities }: { agentKey: string; capabilities: any }) {
  const d = useFetch(() => wsApi.get(`/agents/${agentKey}`), [agentKey]);
  const [enabled, setEnabled] = useState<boolean | null>(null);
  const [model, setModel] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const a: any = d.data;

  async function save() {
    setSaving(true);
    try {
      await wsApi.put(`/agents/config/${agentKey}`, {
        ...(enabled != null ? { enabled } : {}),
        ...(model != null ? { model } : {}),
      });
      d.reload();
      setEnabled(null);
      setModel(null);
    } catch (e: any) {
      alert(e.message);
    } finally {
      setSaving(false);
    }
  }

  if (d.loading) return <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>Loading…</div>;
  if (d.error || !a) return <div className="text-[13px]" style={{ color: "var(--danger)" }}>{d.error ?? "Missing"}</div>;
  return (
    <div className="space-y-4">
      <div className="flex gap-2 items-center flex-wrap">
        <b>{a.title}</b>
        <Badge tone={a.enabled ? "success" : "muted"}>{a.enabled ? "enabled" : "disabled"}</Badge>
        <span className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>{a.runs} runs · {(a.failure_rate * 100).toFixed(1)}% fail · {a.avg_duration_ms ?? "—"}ms avg · {fmtUSD(a.total_cost_usd)}</span>
      </div>
      <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>{a.description}</div>
      <div className="grid md:grid-cols-2 gap-3">
        <Card>
          <b className="text-[13px]">Configuration</b>
          <div className="mt-2 space-y-2.5">
            <Field label="Enabled">
              <select className="select" value={String(enabled ?? a.enabled)} onChange={(e) => setEnabled(e.target.value === "true")}>
                <option value="true">Enabled</option>
                <option value="false">Disabled</option>
              </select>
            </Field>
            <Field label="Model override (blank = workspace default)">
              <input className="input" value={model ?? a.model ?? ""} onChange={(e) => setModel(e.target.value)} placeholder="e.g. gpt-4o-mini" />
            </Field>
            <button className="btn-primary !text-xs" disabled={saving || (enabled == null && model == null)} onClick={save}>Save</button>
          </div>
        </Card>
        <Card>
          <b className="text-[13px]">Skills · tools · permissions</b>
          <div className="mt-2 text-[12.5px] space-y-1">
            <div><span style={{ color: "var(--text-faint)" }}>skills: </span><span className="font-mono">{(a.skills ?? []).join(", ") || "—"}</span></div>
            <div><span style={{ color: "var(--text-faint)" }}>tools: </span><span className="font-mono">{(a.tools ?? []).join(", ") || "—"}</span></div>
            <div><span style={{ color: "var(--text-faint)" }}>permissions: </span><span className="font-mono">{(a.permissions ?? []).join(", ") || "—"}</span></div>
            <div><span style={{ color: "var(--text-faint)" }}>policy: </span><span className="font-mono">{a.model_policy} · {a.execution_policy}</span></div>
          </div>
          {capabilities && <div className="text-[11.5px] mt-2" style={{ color: "var(--text-faint)" }}>{(capabilities.skills ?? []).length} skills · {(capabilities.tools ?? []).length} tools registered platform-wide.</div>}
        </Card>
      </div>
      <div>
        <div className="panel-label mb-1.5">Recent runs</div>
        <AgentRunsList runs={(a as any).runs ?? []} />
      </div>
    </div>
  );
}

function AgentRunsList({ runs }: { runs: any[] }) {
  if (!runs.length) return <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>No runs recorded.</div>;
  return (
    <div className="space-y-2">
      {runs.slice(0, 12).map((r: any) => (
        <div key={r.id} className="rounded-lg p-2.5 text-[12.5px]" style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
          <div className="flex gap-2 items-center flex-wrap">
            <span className="font-mono">{r.task_type}</span>
            <Badge tone={statusTone(r.status)}>{r.status}</Badge>
            {r.duration_ms != null && <span className="font-mono" style={{ color: "var(--text-faint)" }}>{r.duration_ms}ms</span>}
            {r.cost_usd > 0 && <span className="font-mono" style={{ color: "var(--text-faint)" }}>{fmtUSD(r.cost_usd)}</span>}
          </div>
          {r.output_summary && <div className="mt-0.5" style={{ color: "var(--text-muted)" }}>{r.output_summary}</div>}
          {r.error && <div style={{ color: "var(--danger)" }}>{r.error}</div>}
          {(r.steps ?? []).length > 0 && (
            <div className="mt-1 font-mono text-[11.5px]" style={{ color: "var(--text-muted)" }}>
              {r.steps.map((s: any, i: number) => <div key={i}>{s.status === "ok" ? "✓" : s.status === "failed" ? "✗" : "…"} {s.step}{s.detail ? ` — ${s.detail}` : ""}</div>)}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

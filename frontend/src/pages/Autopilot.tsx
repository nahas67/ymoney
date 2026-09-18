import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Modal, PageHeader, Section, Tabs, WhyPanel, statusTone } from "../components/ui";
import { fmtAgo, fmtDate, fmtUSD } from "../lib/format";

const STAGES = ["FIND", "SCORE", "SELECT", "RESEARCH", "BUILD", "VERIFY", "UPLOAD", "MEASURE", "LEARN"];

export default function Autopilot() {
  const cycles = useFetch(() => wsApi.get("/cycles?limit=30"), []);
  const jobs = useFetch(() => wsApi.get("/jobs?limit=60"), []);
  const [tab, setTab] = useState<"cycles" | "jobs">("cycles");
  const [openId, setOpenId] = useState<string | null>(null);

  return (
    <div className="space-y-5">
      <PageHeader title="Autopilot" subtitle="Every FIND → … → LEARN execution, stage by stage. Click any cycle for the full forensic drawer."
        actions={<button className="btn-outline !text-xs" onClick={() => { cycles.reload(); jobs.reload(); }}>Refresh</button>} />

      {/* Stage legend */}
      <Card>
        <div className="flex items-center gap-1 flex-wrap">
          {STAGES.map((s, i) => (
            <span key={s} className="flex items-center gap-1">
              <span className="font-mono text-[11px] px-2 py-1 rounded-md" style={{ background: "var(--bg-subtle)", color: "var(--text-muted)" }}>{s}</span>
              {i < STAGES.length - 1 && <span style={{ color: "var(--text-faint)" }}>→</span>}
            </span>
          ))}
        </div>
      </Card>

      <Tabs tabs={[{ key: "cycles", label: "Cycles", count: (cycles.data as any)?.items?.length }, { key: "jobs", label: "Job queue", count: (jobs.data as any)?.items?.length }]}
        active={tab} onChange={setTab} />

      {tab === "cycles" && (
        <Section data={(cycles.data as any)?.items} loading={cycles.loading} error={cycles.error} onRetry={cycles.reload}
          empty="No cycles yet" emptyHint="Press START on the Command Center to run the first loop.">
          {(list) => (
            <Card pad={false} className="overflow-x-auto">
              <table className="table">
                <thead><tr><th>#</th><th>Stage</th><th>Status</th><th>Topic</th><th>Cost</th><th>Started</th></tr></thead>
                <tbody>
                  {list.map((c: any) => (
                    <tr key={c.id} className="cursor-pointer" onClick={() => setOpenId(c.id)}>
                      <td className="font-mono">{c.number}</td>
                      <td className="font-mono text-[12px]">{c.stage}</td>
                      <td><Badge tone={statusTone(c.status)}>{c.status}</Badge></td>
                      <td className="max-w-[300px] truncate text-[12.5px]">{c.topic ?? "—"}</td>
                      <td className="font-mono text-[12.5px]">{fmtUSD(c.cost_usd)}</td>
                      <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtAgo(c.started_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </Section>
      )}

      {tab === "jobs" && (
        <Section data={(jobs.data as any)?.items} loading={jobs.loading} error={jobs.error} onRetry={jobs.reload}
          empty="Queue is empty" emptyHint="Durable jobs appear here with retries and dead letters.">
          {(list) => (
            <Card pad={false} className="overflow-x-auto">
              <table className="table">
                <thead><tr><th>Type</th><th>Status</th><th>Retries</th><th>Error</th><th></th></tr></thead>
                <tbody>
                  {list.map((j: any) => (
                    <tr key={j.id}>
                      <td className="font-mono text-[12px]">{j.type}</td>
                      <td><Badge tone={statusTone(j.status)}>{j.status}</Badge></td>
                      <td className="font-mono">{j.retry_count}/{j.max_retries}</td>
                      <td className="max-w-[320px] truncate text-[12px]" style={{ color: "var(--text-muted)" }}>{j.last_error || "—"}</td>
                      <td>
                        {["QUEUED", "WAITING", "RETRYING", "RUNNING"].includes(j.status) && (
                          <button className="btn-ghost !text-xs !py-1" onClick={async () => { await wsApi.post(`/jobs/${j.id}/cancel`); jobs.reload(); }}>Cancel</button>
                        )}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </Section>
      )}

      <Modal open={!!openId} onClose={() => setOpenId(null)} title="Cycle execution" wide>
        {openId && <CycleDetail id={openId} />}
      </Modal>
    </div>
  );
}

function CycleDetail({ id }: { id: string }) {
  const d = useFetch(() => wsApi.get(`/cycles/${id}`), [id]);
  if (d.loading) return <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>Loading…</div>;
  if (d.error || !d.data) return <div className="text-[13px]" style={{ color: "var(--danger)" }}>{d.error ?? "Not found"}</div>;
  const c: any = d.data;
  const stages = c.stages ?? {};
  return (
    <div className="space-y-4">
      <div className="flex gap-2 items-center flex-wrap">
        <Badge tone={statusTone(c.status)}>Cycle #{c.number} · {c.status}</Badge>
        <span className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtDate(c.started_at)} → {c.finished_at ? fmtDate(c.finished_at) : "…"}</span>
        {c.error && <span className="text-[12.5px]" style={{ color: "var(--danger)" }}>{c.error}</span>}
      </div>
      {c.decision && <WhyPanel why={c.decision.why ?? c.decision} />}
      {Object.entries(stages).map(([stage, arr]: any) => (
        <div key={stage}>
          <div className="panel-label mb-1.5">{stage} ({arr.length})</div>
          {arr.map((j: any) => (
            <div key={j.id} className="rounded-lg p-3 mb-2 text-[12.5px]" style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
              <div className="flex gap-2 items-center flex-wrap">
                <span className="font-mono">{j.type}</span>
                <Badge tone={statusTone(j.status)}>{j.status}</Badge>
                {j.retry_count > 0 && <span className="font-mono" style={{ color: "var(--text-muted)" }}>retry {j.retry_count}</span>}
                {j.last_error && <span className="truncate" style={{ color: "var(--danger)" }}>{j.last_error.slice(0, 160)}</span>}
              </div>
              {(j.agent_runs ?? []).map((r: any) => (
                <div key={r.id} className="mt-2 pl-3" style={{ borderLeft: "2px solid var(--border-strong)" }}>
                  <div className="flex gap-2 items-center flex-wrap">
                    <b>{r.agent_key}</b>
                    <span style={{ color: "var(--text-muted)" }}>{r.task_type}</span>
                    <Badge tone={statusTone(r.status)}>{r.status}</Badge>
                    {r.duration_ms != null && <span className="font-mono" style={{ color: "var(--text-faint)" }}>{r.duration_ms}ms</span>}
                    {r.cost_usd > 0 && <span className="font-mono" style={{ color: "var(--text-faint)" }}>{fmtUSD(r.cost_usd)}</span>}
                  </div>
                  {r.output_summary && <div className="mt-0.5" style={{ color: "var(--text-muted)" }}>{r.output_summary}</div>}
                  {r.error && <div style={{ color: "var(--danger)" }}>{r.error}</div>}
                  {(r.steps ?? []).length > 0 && (
                    <div className="mt-1.5 space-y-0.5 font-mono text-[11.5px]">
                      {r.steps.map((s: any, i: number) => (
                        <div key={i} style={{ color: s.status === "failed" ? "var(--danger)" : "var(--text-muted)" }}>
                          {s.status === "ok" ? "✓" : s.status === "failed" ? "✗" : "…"} {s.step}
                          {s.detail ? <span style={{ color: "var(--text-faint)" }}> — {s.detail}</span> : null}
                          {s.duration_ms != null ? <span style={{ color: "var(--text-faint)" }}> ({s.duration_ms}ms)</span> : null}
                        </div>
                      ))}
                    </div>
                  )}
                </div>
              ))}
            </div>
          ))}
        </div>
      ))}
    </div>
  );
}

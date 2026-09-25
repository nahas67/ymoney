import { useEffect, useState } from "react";
import { getWorkspace, wsApi } from "../lib/api";
import { useFetch, useInterval } from "../hooks/hooks";
import { Accordion, Badge, Card, Field, PageHeader, Section, Stat, WhyPanel, toast } from "../components/ui";
import { fmtAgo, fmtUSD } from "../lib/format";

/* Learned patterns + decision feed + cost intelligence + Work-05 provider-independent
   intelligence: decisions/shadow, routing, context budget, browser runs, verification. */

function is404(err: string | null): boolean {
  return !!err && /404/.test(err);
}

export default function Intelligence() {
  const pat = useFetch(() => wsApi.get("/analytics/patterns"), []);
  const decision = useFetch(() => wsApi.get("/decision"), []);
  const costIntel = useFetch(() => wsApi.get("/costs/intelligence"), []);
  const ci: any = costIntel.data;

  return (
    <div className="space-y-4">
      <PageHeader title="Intelligence" subtitle="What the system learned, what it decided, and what it costs." />

      <div className="grid lg:grid-cols-2 gap-4">
        <Card>
          <b className="text-[14px]">Current decision context</b>
          {(decision.data as any)?.action ? (
            <div className="mt-2"><WhyPanel why={decision.data} /></div>
          ) : (decision.data as any) ? (
            <pre className="text-[12px] font-mono whitespace-pre-wrap mt-2 max-h-[300px] overflow-y-auto">{JSON.stringify(decision.data, null, 2)}</pre>
          ) : decision.loading ? (
            <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>Loading…</div>
          ) : (
            <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>No decision context available.</div>
          )}
        </Card>
        <Card>
          <b className="text-[14px]">Cost intelligence</b>
          {ci ? (
            <div className="mt-2 space-y-2">
              <div className="grid grid-cols-2 gap-2">
                <Stat label="Total spend" value={fmtUSD(ci.total_cost_usd)} />
                <Stat label="Per video" value={fmtUSD(ci.per_video_usd)} />
                <Stat label="Per publication" value={fmtUSD(ci.per_publication_usd)} />
                <Stat label="Per 1k views" value={ci.cost_per_1000_views_usd != null ? fmtUSD(ci.cost_per_1000_views_usd) : "—"} />
              </div>
              <Accordion title="Raw breakdown">
                <pre className="text-[12px] font-mono whitespace-pre-wrap max-h-[260px] overflow-y-auto">{JSON.stringify(ci, null, 2)}</pre>
              </Accordion>
            </div>
          ) : costIntel.loading ? (
            <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>Loading…</div>
          ) : (
            <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>No cost data yet.</div>
          )}
        </Card>
      </div>

      <Card>
        <b className="text-[14px]">Learned patterns</b>
        <Section data={(pat.data as any)?.items} loading={pat.loading} error={pat.error} onRetry={pat.reload}
          empty="No patterns yet" emptyHint="Patterns form after 4+ measured posts, with confidence from sample size.">
          {(list) => (
            <div className="mt-2 space-y-2">
              {list.map((p: any) => (
                <div key={p.pattern_key} className="flex items-center gap-2 text-[13px] flex-wrap">
                  <code className="text-[12px]">{p.pattern_key}</code>
                  <span className="flex-1" style={{ color: "var(--text-muted)" }}>{p.description}</span>
                  <Badge tone={p.active ? "success" : "muted"}>{p.active ? "active" : "inactive"}</Badge>
                  <span className="font-mono text-[12px]">{p.improvement_pct}% · {p.confidence} · n={p.sample_size} · {fmtAgo(p.updated_at)}</span>
                </div>
              ))}
            </div>
          )}
        </Section>
      </Card>

      <DecisionsPanel />
      <RoutingPanel />
      <ContextPanel />
      <BrowserPanel />
      <VerificationPanel />
      <IntelSettings />
    </div>
  );
}

/* ---- Decisions: recent log + shadow agreement summary ---- */

function DecisionsPanel() {
  const log = useFetch(() => wsApi.get("/intelligence/decisions/log?limit=20"), []);
  const shadow = useFetch(() => wsApi.get("/intelligence/decisions/shadow-report"), []);
  const items: any[] = (log.data as any)?.items ?? [];
  const rep: any = shadow.data;

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap">
        <b className="text-[14px]">Decision engine</b>
        <Badge tone="info">provider-independent</Badge>
        <span className="ml-auto flex gap-2">
          <button className="btn-ghost !text-xs !py-1" onClick={() => { log.reload(); shadow.reload(); }}>Refresh</button>
        </span>
      </div>
      {shadow.loading && !rep ? (
        <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>Loading shadow report…</div>
      ) : shadow.error && !rep ? (
        <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>
          {is404(shadow.error) ? "Decision API unavailable on this backend (404)." : `Shadow report failed: ${shadow.error}`}
        </div>
      ) : rep ? (
        <div className="grid grid-cols-2 md:grid-cols-5 gap-2 mt-3">
          <Stat label="Shadow runs" value={rep.total ?? "—"} />
          <Stat label="Agreed" value={rep.agreed ?? "—"} tone="var(--accent)" />
          <Stat label="Disagreed" value={rep.disagreed ?? "—"} tone={rep.disagreed ? "var(--warn)" : undefined} />
          <Stat label="Agreement" value={rep.agreement_rate != null ? `${Math.round(rep.agreement_rate * 100)}%` : "—"} />
          <Stat label="Avg latency" value={rep.avg_latency_ms != null ? `${rep.avg_latency_ms}ms` : "—"} />
        </div>
      ) : null}
      <div className="mt-3">
        <div className="text-[12px] font-medium mb-1.5" style={{ color: "var(--text-muted)" }}>Recent decisions</div>
        <Section data={items} loading={log.loading} error={log.error} onRetry={log.reload}
          empty="No decisions logged yet" emptyHint="Run autopilot with decision_mode SHADOW or above to record decisions.">
          {(list) => (
            <div className="space-y-2">
              {list.slice(0, 20).map((d: any) => (
                <div key={d.id} className="flex items-center gap-2 text-[12.5px] flex-wrap">
                  <Badge tone="muted">{d.kind}</Badge>
                  <Badge tone={d.mode === "DISABLED" ? "muted" : "info"}>{d.mode}</Badge>
                  <code className="text-[12px]">{d.model || d.actual_provider || "—"}</code>
                  <span className="font-mono text-[12px]" style={{ color: "var(--text-muted)" }}>
                    {d.latency_ms ?? "—"}ms · {d.cost_usd != null ? fmtUSD(d.cost_usd) : "—"}
                  </span>
                  {d.fallback_reason ? (
                    <span className="text-[12px]" style={{ color: "var(--warn)" }}>fallback: {String(d.fallback_reason).slice(0, 120)}</span>
                  ) : null}
                  {d.agree === true ? <Badge tone="success">agree</Badge> : d.agree === false ? <Badge tone="warning">disagree</Badge> : null}
                  <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{fmtAgo(d.created_at)}</span>
                </div>
              ))}
            </div>
          )}
        </Section>
      </div>
    </Card>
  );
}

/* ---- Routing: provider health + recent task→model selections ---- */

function RoutingPanel() {
  const health = useFetch(() => wsApi.get("/intelligence/routing/health"), []);
  const rlog = useFetch(() => wsApi.get("/intelligence/routing/log"), []);
  const providers: Record<string, boolean> = (health.data as any)?.providers ?? {};
  const entries: any[] = [...(((rlog.data as any)?.entries ?? []) as any[])].reverse();

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap">
        <b className="text-[14px]">Model routing</b>
        <Badge tone="info">tier, never vendor</Badge>
        <span className="ml-auto">
          <button className="btn-ghost !text-xs !py-1" onClick={() => { health.reload(); rlog.reload(); }}>Refresh</button>
        </span>
      </div>
      <div className="mt-2">
        {health.loading && !health.data ? (
          <div className="text-[13px]" style={{ color: "var(--text-faint)" }}>Loading provider health…</div>
        ) : health.error && !health.data ? (
          <div className="text-[13px]" style={{ color: "var(--text-faint)" }}>
            {is404(health.error) ? "Routing API unavailable on this backend (404)." : `Health failed: ${health.error}`}
          </div>
        ) : (
          <div className="flex gap-2 flex-wrap">
            {Object.keys(providers).length === 0 ? (
              <span className="text-[13px]" style={{ color: "var(--text-faint)" }}>No provider health reported.</span>
            ) : Object.entries(providers).map(([name, ok]) => (
              <Badge key={name} tone={ok ? "success" : "error"}>{name}: {ok ? "healthy" : "down"}</Badge>
            ))}
          </div>
        )}
      </div>
      <div className="mt-3">
        <div className="text-[12px] font-medium mb-1.5" style={{ color: "var(--text-muted)" }}>Recent task → model selections</div>
        <Section data={entries} loading={rlog.loading} error={rlog.error} onRetry={rlog.reload}
          empty="No routing decisions yet" emptyHint="Route a task via the API or run autopilot to populate this log.">
          {(list) => (
            <div className="space-y-2">
              {list.slice(0, 20).map((e: any, i: number) => (
                <div key={i} className="text-[12.5px]">
                  <div className="flex items-center gap-2 flex-wrap">
                    <code className="text-[12px]">{e.task_type}</code>
                    <span style={{ color: "var(--text-faint)" }}>→</span>
                    <code className="text-[12px]">{e.model || e.tier}</code>
                    <Badge tone={e.remote ? "warning" : "success"}>{e.remote ? "remote" : "local"}</Badge>
                    {e.tier && e.model && e.tier !== e.model ? <Badge tone="muted">{e.tier}</Badge> : null}
                    <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{e.at ? fmtAgo(e.at) : ""}</span>
                  </div>
                  {e.reason ? <div className="font-mono text-[11.5px] mt-0.5 break-words" style={{ color: "var(--text-muted)" }}>{e.reason}</div> : null}
                </div>
              ))}
            </div>
          )}
        </Section>
      </div>
    </Card>
  );
}

/* ---- Context: run a budget pass + recall lookup ---- */

function ContextPanel() {
  const [text, setText] = useState("");
  const [maxTokens, setMaxTokens] = useState("4000");
  const [result, setResult] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [recallId, setRecallId] = useState("");
  const [recallOut, setRecallOut] = useState<any>(null);
  const [recallErr, setRecallErr] = useState<string | null>(null);
  const [recallBusy, setRecallBusy] = useState(false);
  const [recalls, setRecalls] = useState<any[]>([]);

  const m = result?.metrics;

  async function runBudget() {
    if (!text.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      const r = await wsApi.post("/intelligence/context/budget", {
        items: [{ content: text }],
        max_tokens: Math.max(1, Number(maxTokens) || 4000),
      });
      setResult(r);
    } catch (e: any) {
      setErr(e?.message ?? "budget failed");
    } finally {
      setBusy(false);
    }
  }

  async function runRecall(id?: string) {
    const ref = (id ?? recallId).trim();
    if (!ref) return;
    setRecallBusy(true);
    setRecallErr(null);
    try {
      const r = await wsApi.post(`/intelligence/context/recall/${encodeURIComponent(ref)}`, {});
      setRecallOut(r);
      setRecalls((prev) => [{ ref_id: ref, at: new Date().toISOString(), tokens: (r as any)?.tokens }, ...prev].slice(0, 10));
    } catch (e: any) {
      setRecallErr(e?.message ?? "recall failed");
      setRecallOut(null);
    } finally {
      setRecallBusy(false);
    }
  }

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap">
        <b className="text-[14px]">Context budget</b>
        <Badge tone="info">reversible filtering</Badge>
      </div>
      <div className="grid md:grid-cols-[1fr_180px_auto] gap-2 mt-3 items-end">
        <Field label="Content to budget">
          <textarea className="textarea font-mono !text-xs" rows={3} value={text}
            placeholder="Paste oversized context here, then run a budget pass…"
            onChange={(e) => setText(e.target.value)} />
        </Field>
        <Field label="Max tokens">
          <input className="input font-mono" type="number" value={maxTokens} onChange={(e) => setMaxTokens(e.target.value)} />
        </Field>
        <button className="btn-primary !text-xs mb-3" disabled={busy || !text.trim()} onClick={runBudget}>
          {busy ? "Budgeting…" : "Run budget"}
        </button>
      </div>
      {err && <div className="text-[12.5px] font-mono" style={{ color: "var(--danger)" }}>{/404/.test(err) ? "Context API unavailable on this backend (404)." : err}</div>}
      {m && (
        <div className="mt-2 space-y-2">
          <div className="grid grid-cols-2 md:grid-cols-5 gap-2">
            <Stat label="Raw tokens" value={m.raw_tokens ?? "—"} />
            <Stat label="Kept" value={m.kept_tokens ?? "—"} tone="var(--accent)" />
            <Stat label="Filtered" value={m.filtered_tokens ?? "—"} />
            <Stat label="Compression" value={m.compression_ratio != null ? Number(m.compression_ratio).toFixed(3) : "—"} />
            <Stat label="Recalls" value={m.recalls ?? recalls.length} />
          </div>
          {(result.references ?? []).length > 0 && (
            <div className="space-y-1.5">
              <div className="text-[12px] font-medium" style={{ color: "var(--text-muted)" }}>Filtered references (click to recall)</div>
              {(result.references as any[]).map((r: any) => (
                <div key={r.ref_id} className="flex items-center gap-2 text-[12.5px] flex-wrap">
                  <code className="text-[11.5px]">{r.ref_id}</code>
                  <Badge tone="muted">{r.category}</Badge>
                  <span className="font-mono text-[12px]" style={{ color: "var(--text-muted)" }}>{r.tokens} tok</span>
                  <span className="flex-1 text-[12px]" style={{ color: "var(--text-muted)" }}>{r.summary}</span>
                  <button className="btn-ghost !text-xs !py-0.5" onClick={() => { setRecallId(r.ref_id); runRecall(r.ref_id); }}>Recall</button>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
      <div className="grid md:grid-cols-[1fr_auto] gap-2 mt-3 items-end">
        <Field label="Recall lookup" hint="Reference id from a budget pass restores the exact original.">
          <input className="input font-mono !text-xs" value={recallId}
            placeholder="ref id…" onChange={(e) => setRecallId(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && runRecall()} />
        </Field>
        <button className="btn-outline !text-xs mb-3" disabled={recallBusy || !recallId.trim()} onClick={() => runRecall()}>
          {recallBusy ? "Recalling…" : "Recall"}
        </button>
      </div>
      {recallErr && <div className="text-[12.5px] font-mono" style={{ color: "var(--danger)" }}>{recallErr}</div>}
      {recallOut && (
        <pre className="text-[12px] font-mono whitespace-pre-wrap max-h-[200px] overflow-y-auto p-3 rounded-xl" style={{ background: "var(--bg-inset)" }}>
          {JSON.stringify(recallOut, null, 2)}
        </pre>
      )}
      {recalls.length > 0 && (
        <div className="mt-2 text-[12px]" style={{ color: "var(--text-muted)" }}>
          Recent recall events: {recalls.map((r: any) => `${r.ref_id.slice(0, 8)}… (${fmtAgo(r.at)})`).join(" · ")}
        </div>
      )}
    </Card>
  );
}

/* ---- Browser: create runs, track by id, refresh + cancel ---- */

function BrowserPanel() {
  const ws = getWorkspace();
  const storeKey = `ym_bruns_${ws}`;
  const [ids, setIds] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem(storeKey) ?? "[]");
    } catch {
      return [];
    }
  });
  const [runs, setRuns] = useState<Record<string, any>>({});
  const [goal, setGoal] = useState("");
  const [domains, setDomains] = useState("");
  const [creating, setCreating] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    localStorage.setItem(storeKey, JSON.stringify(ids));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids]);

  async function fetchRun(id: string) {
    try {
      const r = await wsApi.get(`/intelligence/browser/runs/${id}`);
      setRuns((prev) => ({ ...prev, [id]: r }));
    } catch {
      /* keep last known state; row shows stale data */
    }
  }

  useEffect(() => {
    ids.forEach((id) => {
      if (!runs[id]) void fetchRun(id);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids]);

  const anyRunning = ids.some((id) => ["QUEUED", "RUNNING"].includes(runs[id]?.status));
  useInterval(() => {
    ids.forEach((id) => {
      if (["QUEUED", "RUNNING"].includes(runs[id]?.status)) void fetchRun(id);
    });
  }, anyRunning ? 4000 : null);

  async function create() {
    if (!goal.trim()) return;
    setCreating(true);
    setErr(null);
    try {
      const r: any = await wsApi.post("/intelligence/browser/runs", {
        goal: goal.trim(),
        allowed_domains: domains.split(/[,\s]+/).map((s) => s.trim()).filter(Boolean),
      });
      if (r?.id) {
        setIds((prev) => [r.id, ...prev].slice(0, 20));
        setRuns((prev) => ({ ...prev, [r.id]: r }));
        setGoal("");
        toast("Browser run started", "success");
      }
    } catch (e: any) {
      setErr(e?.message ?? "create failed");
    } finally {
      setCreating(false);
    }
  }

  async function cancel(id: string) {
    try {
      const r = await wsApi.post(`/intelligence/browser/runs/${id}/cancel`, {});
      setRuns((prev) => ({ ...prev, [id]: r }));
    } catch (e: any) {
      toast(e?.message ?? "cancel failed", "error", "Cancel failed");
    }
  }

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap">
        <b className="text-[14px]">Browser intelligence</b>
        <Badge tone="info">domain-gated evidence</Badge>
      </div>
      <div className="grid md:grid-cols-[1fr_220px_auto] gap-2 mt-3 items-end">
        <Field label="Goal">
          <input className="input" value={goal} placeholder="What should the browser gather evidence for…"
            onChange={(e) => setGoal(e.target.value)} onKeyDown={(e) => e.key === "Enter" && create()} />
        </Field>
        <Field label="Allowed domains (comma-separated)" hint="Empty = workspace policy.">
          <input className="input font-mono !text-xs" value={domains} placeholder="example.com, docs…"
            onChange={(e) => setDomains(e.target.value)} />
        </Field>
        <button className="btn-primary !text-xs mb-3" disabled={creating || !goal.trim()} onClick={create}>
          {creating ? "Starting…" : "Start run"}
        </button>
      </div>
      {err && <div className="text-[12.5px] font-mono" style={{ color: "var(--danger)" }}>{/404/.test(err) ? "Browser API unavailable on this backend (404)." : err}</div>}
      <div className="mt-2 space-y-2">
        {ids.length === 0 && (
          <div className="text-[13px]" style={{ color: "var(--text-faint)" }}>No browser runs tracked yet — start one above.</div>
        )}
        {ids.map((id) => {
          const r = runs[id];
          if (!r) return <div key={id} className="text-[12.5px] font-mono" style={{ color: "var(--text-faint)" }}>{id.slice(0, 8)}… loading…</div>;
          const steps: any[] = r.steps ?? [];
          const running = ["QUEUED", "RUNNING"].includes(r.status);
          return (
            <div key={id} className="rounded-xl p-3" style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
              <div className="flex items-center gap-2 flex-wrap text-[12.5px]">
                <Badge tone={r.status === "DONE" || r.status === "COMPLETED" ? "success" : running ? "warning" : r.status === "CANCELLED" ? "muted" : "error"}>
                  {r.status}
                </Badge>
                <span className="flex-1 min-w-[160px]">{r.goal}</span>
                <span className="font-mono text-[11.5px]" style={{ color: "var(--text-muted)" }}>
                  {steps.length} steps · {r.cost_usd != null ? fmtUSD(r.cost_usd) : "—"}
                </span>
                <button className="btn-ghost !text-xs !py-0.5" onClick={() => fetchRun(id)}>Refresh</button>
                {running && <button className="btn-outline !text-xs !py-0.5" onClick={() => cancel(id)}>Cancel</button>}
              </div>
              {steps.length > 0 && (
                <div className="mt-2 space-y-1">
                  {steps.slice(0, 8).map((s: any, i: number) => (
                    <div key={i} className="font-mono text-[11.5px] break-words" style={{ color: "var(--text-muted)" }}>
                      {i + 1}. {[s.domain, s.action, s.url, s.title, s.summary].filter(Boolean).join(" · ") || JSON.stringify(s).slice(0, 160)}
                    </div>
                  ))}
                  {steps.length > 8 && <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>+{steps.length - 8} more steps</div>}
                </div>
              )}
            </div>
          );
        })}
      </div>
    </Card>
  );
}

/* ---- Verification: counts by status + latest ledger entries ---- */

function VerificationPanel() {
  const [kind, setKind] = useState("");
  const [status, setStatus] = useState("");
  const [query, setQuery] = useState("");
  const ledger = useFetch(() => wsApi.get(`/intelligence/verification/ledger${query}`), [query]);
  const items: any[] = (ledger.data as any)?.items ?? [];

  function apply() {
    const p = new URLSearchParams();
    if (kind.trim()) p.set("kind", kind.trim());
    if (status.trim()) p.set("status", status.trim());
    const q = p.toString();
    setQuery(q ? `?${q}` : "");
  }

  const counts: Record<string, number> = {};
  for (const it of items) {
    const k = String(it.verification_status ?? "unknown");
    counts[k] = (counts[k] ?? 0) + 1;
  }

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap">
        <b className="text-[14px]">Verification ledger</b>
        <Badge tone="info">hash-chained</Badge>
        <span className="ml-auto">
          <button className="btn-ghost !text-xs !py-1" onClick={ledger.reload}>Refresh</button>
        </span>
      </div>
      <div className="grid md:grid-cols-[180px_180px_auto] gap-2 mt-3 items-end">
        <Field label="Kind filter">
          <input className="input font-mono !text-xs" value={kind} placeholder="e.g. video" onChange={(e) => setKind(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && apply()} />
        </Field>
        <Field label="Status filter">
          <input className="input font-mono !text-xs" value={status} placeholder="e.g. PASS" onChange={(e) => setStatus(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && apply()} />
        </Field>
        <button className="btn-outline !text-xs mb-3" onClick={apply}>Apply filters</button>
      </div>
      {ledger.loading && !ledger.data ? (
        <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>Loading ledger…</div>
      ) : ledger.error && !ledger.data ? (
        <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>
          {is404(ledger.error) ? "Verification API unavailable on this backend (404)." : `Ledger failed: ${ledger.error}`}
        </div>
      ) : (
        <div className="mt-2 space-y-2">
          <div className="flex gap-2 flex-wrap">
            {Object.keys(counts).length === 0 ? (
              <span className="text-[13px]" style={{ color: "var(--text-faint)" }}>No ledger entries yet.</span>
            ) : Object.entries(counts).map(([k, n]) => (
              <Badge key={k} tone={/pass|ok|verified/i.test(k) ? "success" : /fail|mismatch/i.test(k) ? "error" : "muted"}>
                {k}: {n}
              </Badge>
            ))}
          </div>
          {items.slice(0, 15).map((it: any) => (
            <div key={it.id} className="flex items-center gap-2 text-[12.5px] flex-wrap">
              <Badge tone="muted">{it.kind}</Badge>
              <code className="text-[11.5px]">{String(it.subject_id).slice(0, 8)}…</code>
              <Badge tone={/pass|ok|verified/i.test(String(it.verification_status)) ? "success" : /fail|mismatch/i.test(String(it.verification_status)) ? "error" : "warning"}>
                {it.verification_status}
              </Badge>
              <span className="font-mono text-[11.5px]" style={{ color: "var(--text-muted)" }}>
                {(it.checks ?? []).length} checks · digest {String(it.digest ?? "—").slice(0, 10)}
              </span>
              <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{fmtAgo(it.created_at)}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

/* ---- Intelligence settings (persisted via workspace PATCH) ---- */

const DECISION_MODES = ["DISABLED", "SHADOW", "ASSISTED", "PRIMARY"];
const PROVIDER_PREFS = ["deterministic", "local", "llm", "claude"];
const PRIVACY_MODES = ["STANDARD", "PRIVATE", "LOCAL_ONLY"];
const ROUTING_STRATEGIES = ["balanced", "cost", "quality", "latency", "cheap-first"];

function IntelSettings() {
  const ws = useFetch(() => wsApi.get(""), []);
  const [form, setForm] = useState<any>(null);
  const [saved, setSaved] = useState(false);
  const [saving, setSaving] = useState(false);
  const intel: any = (ws.data as any)?.settings?.intelligence ?? {};

  useEffect(() => {
    if (ws.data && form === null) {
      setForm({
        decision_mode: intel.decision_mode ?? "SHADOW",
        provider_preference: intel.provider_preference ?? "deterministic",
        remote_allowed: !!intel.remote_allowed,
        context_filtering: intel.context_filtering ?? true,
        browser_enabled: intel.browser_enabled ?? false,
        allowed_domains: Array.isArray(intel.allowed_domains) ? intel.allowed_domains.join(", ") : (intel.allowed_domains ?? ""),
        routing_strategy: intel.routing_strategy ?? "balanced",
        privacy_mode: String(intel.privacy_mode ?? "STANDARD").toUpperCase(),
      });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ws.data]);

  const cur = form ?? {};
  const set = (k: string, v: any) => setForm({ ...cur, [k]: v });

  async function save() {
    setSaving(true);
    try {
      await wsApi.patch("", {
        settings: {
          intelligence: {
            decision_mode: cur.decision_mode,
            provider_preference: cur.provider_preference,
            remote_allowed: !!cur.remote_allowed,
            context_filtering: !!cur.context_filtering,
            browser_enabled: !!cur.browser_enabled,
            allowed_domains: String(cur.allowed_domains ?? "").split(/[,\s]+/).map((s: string) => s.trim()).filter(Boolean),
            routing_strategy: cur.routing_strategy,
            privacy_mode: cur.privacy_mode,
          },
        },
      });
      setSaved(true);
      setTimeout(() => setSaved(false), 2000);
      ws.reload();
      toast("Intelligence settings saved", "success");
    } catch (e: any) {
      toast(e?.message ?? "save failed", "error", "Save failed");
    } finally {
      setSaving(false);
    }
  }

  if (ws.loading) return <Card>Loading intelligence settings…</Card>;
  if (ws.error) return <Card>Settings unavailable: {ws.error}</Card>;

  const boolSel = (k: string, onLabel: string, offLabel: string) => (
    <select className="select" value={String(!!cur[k])} onChange={(e) => set(k, e.target.value === "true")}>
      <option value="true">{onLabel}</option>
      <option value="false">{offLabel}</option>
    </select>
  );

  return (
    <Card className="max-w-[720px]">
      <div className="flex items-center gap-2 mb-3 flex-wrap">
        <b className="text-[14px]">Intelligence settings</b>
        <Badge tone="info">workspace-scoped</Badge>
      </div>
      <div className="grid md:grid-cols-2 gap-x-5">
        <Field label="Decision mode">
          <select className="select" value={cur.decision_mode ?? "SHADOW"} onChange={(e) => set("decision_mode", e.target.value)}>
            {DECISION_MODES.map((m) => <option key={m} value={m}>{m}</option>)}
          </select>
        </Field>
        <Field label="Provider preference">
          <select className="select" value={cur.provider_preference ?? "deterministic"} onChange={(e) => set("provider_preference", e.target.value)}>
            {PROVIDER_PREFS.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </Field>
        <Field label="Remote allowed">{boolSel("remote_allowed", "Allowed", "Local only")}</Field>
        <Field label="Context filtering">{boolSel("context_filtering", "On", "Off")}</Field>
        <Field label="Browser enabled">{boolSel("browser_enabled", "On", "Off")}</Field>
        <Field label="Routing strategy">
          <select className="select" value={cur.routing_strategy ?? "balanced"} onChange={(e) => set("routing_strategy", e.target.value)}>
            {ROUTING_STRATEGIES.map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
        </Field>
        <Field label="Privacy mode" hint="LOCAL_ONLY blocks remote semantic calls and remote browser fetch.">
          <select className="select" value={cur.privacy_mode ?? "STANDARD"} onChange={(e) => set("privacy_mode", e.target.value)}>
            {PRIVACY_MODES.map((p) => <option key={p} value={p}>{p}</option>)}
          </select>
        </Field>
        <Field label="Allowed domains (comma-separated)">
          <textarea className="textarea font-mono !text-xs" rows={2} value={cur.allowed_domains ?? ""}
            placeholder="example.com, docs.example.com"
            onChange={(e) => set("allowed_domains", e.target.value)} />
        </Field>
      </div>
      <button className="btn-primary !text-xs mt-1" disabled={saving} onClick={save}>
        {saving ? "Saving…" : "Save intelligence settings"}
      </button>
      {saved && <span className="ml-2 text-[12.5px]" style={{ color: "var(--accent)" }}>Saved ✓</span>}
    </Card>
  );
}

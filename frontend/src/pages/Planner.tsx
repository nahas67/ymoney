import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Empty, Field, PageHeader, Section, Stat, Tabs, toast } from "../components/ui";
import { platformLabel } from "../lib/format";

/* Work 15 §13 — the planner views.
 *
 * The one rule this screen enforces visually: an AI RECOMMENDATION is never
 * rendered in the same lane as MEASURED EVIDENCE. Every opportunity and every
 * signal carries a `basis` (OBSERVED / INFERRED / RECOMMENDED) and a
 * `measured` flag per factor, and the UI colours and labels them separately.
 * An unmeasured factor is drawn as "no data", never as a zero score.
 */

const BASIS_TONE: Record<string, string> = {
  OBSERVED: "success",
  INFERRED: "muted",
  RECOMMENDED: "warning",
};

const BASIS_HINT: Record<string, string> = {
  OBSERVED: "the demand itself was seen in evidence",
  INFERRED: "derived from evidence by scoring",
  RECOMMENDED: "an AI suggestion — no measurement behind it",
};

const FRESH_TONE: Record<string, string> = {
  FRESH: "success",
  AGING: "warning",
  STALE: "danger",
};

const AUTONOMY_HINT: Record<string, string> = {
  DISABLED: "no planning; signals are still ingested",
  RECOMMEND: "suggestions only — nothing is written",
  APPROVAL: "may create plan + campaign drafts; needs a human before production",
  AUTONOMOUS: "may advance explicitly allow-listed actions within budget",
};

/* ------------------------------------------------------------------ signals */

function Signals() {
  const sig = useFetch(() => wsApi.get("/planner/signals"), []);
  const [source, setSource] = useState("");
  const [topic, setTopic] = useState("");
  const [busy, setBusy] = useState(false);

  const rows: any[] = (sig.data as any)?.signals ?? [];
  const filtered = source ? rows.filter((s) => s.source === source) : rows;
  const sources: string[] = (sig.data as any)?.sources ?? [];

  async function ingest() {
    if (!topic.trim()) return;
    setBusy(true);
    try {
      // An operator signal is a person stating something, so the server caps its
      // confidence: "verified evidence" is reserved for sources YMONEY resolves
      // itself (research, source connectors, official platform APIs). The
      // confidence asked for here is therefore advisory.
      const r = await wsApi.post("/planner/signals", {
        source: "operator",
        topic: topic.trim(),
        confidence: 0.9,
        evidence_ids: [`operator:${Date.now()}`],
      });
      toast(
        r.created
          ? "Signal recorded — confidence is capped: a stated topic is not a measurement"
          : "Existing signal refreshed",
        "success",
      );
      setTopic("");
      sig.reload();
    } catch (e: any) {
      toast(e.message, "error", "Ingest failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Signals" value={rows.length} hint="observations with evidence" />
        <Stat label="Usable as demand" value={rows.filter((s) => s.usable_as_demand).length} tone="success" />
        <Stat label="Stale" value={rows.filter((s) => s.freshness === "STALE").length} tone={rows.some((s) => s.freshness === "STALE") ? "warning" : undefined} />
        <Stat label="With a measurable rate" value={rows.filter((s) => s.velocity).length} hint="need 2+ observations" />
      </div>

      <Card>
        <div className="flex flex-wrap gap-3 items-end">
          <Field label="Record an operator signal" hint="a stated topic, not a measurement — confidence is capped">
            <input className="input" value={topic} placeholder="e.g. how to budget a tight paycheck"
              onChange={(e) => setTopic(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && ingest()} />
          </Field>
          <button className="btn-primary" disabled={busy || !topic.trim()} onClick={ingest}>
            {busy ? "Recording…" : "Record"}
          </button>
          <div className="flex-1" />
          <div className="flex gap-1.5 flex-wrap">
            <button className={`btn-xs ${source === "" ? "btn-outline" : ""}`} onClick={() => setSource("")}>All</button>
            {sources.map((s) => (
              <button key={s} className={`btn-xs ${source === s ? "btn-outline" : ""}`} onClick={() => setSource(s)}>{s}</button>
            ))}
          </div>
        </div>
        <p className="text-[12px] mt-2" style={{ color: "var(--text-faint)" }}>
          A signal is an <b>observation</b>, not a demand estimate. A velocity figure appears only once the
          same topic is observed twice — one observation has no measurable rate, and none is invented.
        </p>
      </Card>

      <Section data={filtered} loading={sig.loading} error={sig.error} onRetry={sig.reload}
        empty="No signals yet" emptyHint="Ingest from a source connector, or record an operator observation above.">
        {() => (
          <div className="space-y-2">
            {filtered.map((s) => (
              <Card key={s.id} className="!py-2.5">
                <div className="flex items-start gap-3 flex-wrap">
                  <div className="flex-1 min-w-[220px]">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-semibold text-[13.5px]">{s.topic}</span>
                      <Badge tone="muted">{s.source}</Badge>
                      <Badge tone={FRESH_TONE[s.freshness] ?? "muted"}>{s.freshness}</Badge>
                      {s.recurrence > 1 && <Badge tone="success">{s.recurrence}× observed</Badge>}
                    </div>
                    <div className="text-[12px] mt-1" style={{ color: "var(--text-muted)" }}>
                      seen {new Date(s.observed_at).toLocaleString()} · confidence {(s.confidence * 100).toFixed(0)}%
                      {s.external_ref && <> · ref {s.external_ref}</>}
                    </div>
                    <div className="text-[12px] mt-1" style={{ color: "var(--text-faint)" }}>
                      evidence: {s.evidence_ids.length ? s.evidence_ids.join(", ") : "none"}
                    </div>
                    {s.velocity ? (
                      <div className="text-[12px] mt-1 font-mono" style={{ color: "var(--accent)" }}>
                        {s.velocity.observations_per_day}/day over {s.velocity.span_days}d
                        <span style={{ color: "var(--text-faint)" }}> (observed count delta, not a forecast)</span>
                      </div>
                    ) : (
                      <div className="text-[12px] mt-1" style={{ color: "var(--text-faint)" }}>
                        velocity: unknown — needs a second observation
                      </div>
                    )}
                  </div>
                  {!s.usable_as_demand && (
                    <Badge tone="warning">not usable as fresh demand</Badge>
                  )}
                </div>
              </Card>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}

/* ------------------------------------------------------------- opportunities */

function OpportunityFactors({ scoring }: { scoring: any }) {
  const factors = scoring?.factors ?? {};
  const rows = Object.values<any>(factors);
  if (!rows.length) return null;
  return (
    <div className="space-y-1 mt-2">
      {rows.map((f) => (
        <div key={f.factor} className="flex items-baseline justify-between gap-3 font-mono text-[11.5px]">
          <span style={{ color: f.measured ? "var(--text-muted)" : "var(--text-faint)" }}>
            {f.factor}
            {f.measured ? "" : " — no data"}
          </span>
          <span style={{ color: f.measured ? "var(--text)" : "var(--text-faint)" }}>
            {f.measured ? Number(f.value).toFixed(2) : "—"}
            <b style={{ color: f.measured ? "var(--accent)" : "var(--text-faint)" }}>
              {" "}{f.measured ? `+${Number(f.contribution).toFixed(3)}` : "+0"}
            </b>
          </span>
        </div>
      ))}
      <div className="text-[11px] pt-1" style={{ color: "var(--text-faint)" }}>
        An unmeasured factor contributes 0 because the data does not exist — that is not a demand of zero.
      </div>
    </div>
  );
}

function Opportunities() {
  const opps = useFetch(() => wsApi.get("/planner/opportunities"), []);
  const [basis, setBasis] = useState("");
  const rows: any[] = (opps.data as any)?.opportunities ?? [];
  const filtered = basis ? rows.filter((o) => o.basis === basis) : rows;

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <Stat label="Opportunities" value={rows.length} />
        <Stat label="Observed" value={rows.filter((o) => o.basis === "OBSERVED").length} tone="success" />
        <Stat label="Inferred" value={rows.filter((o) => o.basis === "INFERRED").length} />
        <Stat label="AI-suggested" value={rows.filter((o) => o.basis === "RECOMMENDED").length}
          tone={rows.some((o) => o.basis === "RECOMMENDED") ? "warning" : undefined}
          hint="capped so it cannot outrank measured demand" />
      </div>

      <div className="flex gap-1.5 flex-wrap">
        {["", "OBSERVED", "INFERRED", "RECOMMENDED"].map((b) => (
          <button key={b || "all"} className={`btn-xs ${basis === b ? "btn-outline" : ""}`} onClick={() => setBasis(b)}>
            {b || "All"}
          </button>
        ))}
      </div>

      <Section data={filtered} loading={opps.loading} error={opps.error} onRetry={opps.reload}
        empty="No opportunities yet" emptyHint="Opportunities appear once a topic has evidence-carrying signals.">
        {() => (
          <div className="space-y-2">
            {filtered.map((o) => (
              <Card key={o.id}>
                <div className="flex items-start gap-3 flex-wrap">
                  <div className="flex-1 min-w-[240px]">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="font-semibold text-[13.5px]">{o.topic}</span>
                      <Badge tone={BASIS_TONE[o.basis] ?? "muted"}>{o.basis}</Badge>
                      {o.freshness && <Badge tone={FRESH_TONE[o.freshness] ?? "muted"}>{o.freshness}</Badge>}
                      {o.dedupe_verdict && <Badge tone={o.dedupe_verdict === "NEW" ? "success" : "warning"}>{o.dedupe_verdict}</Badge>}
                    </div>
                    <div className="text-[12px] mt-1" style={{ color: "var(--text-muted)" }}>{BASIS_HINT[o.basis]}</div>
                    {o.angle && <div className="text-[12.5px] mt-1.5">angle: {o.angle}</div>}
                    <div className="text-[12px] mt-1" style={{ color: "var(--text-muted)" }}>
                      priority {(o.score * 100).toFixed(0)}/100
                      {o.platforms?.length ? <> · {o.platforms.map(platformLabel).join(", ")}</> : null}
                      {o.estimated_cost_usd ? <> · est. ${Number(o.estimated_cost_usd).toFixed(2)}</> : null}
                    </div>
                    {o.why && (
                      <div className="text-[12px] mt-1.5" style={{ color: "var(--text-muted)" }}>
                        <b>why:</b> {o.why}
                      </div>
                    )}
                    {o.dedupe_reason && (
                      <div className="text-[12px] mt-1" style={{ color: "var(--text-faint)" }}>dedupe: {o.dedupe_reason}</div>
                    )}
                    <OpportunityFactors scoring={o.scoring} />
                  </div>
                  <div className="text-right">
                    <div className="font-mono text-[20px] font-semibold">{(o.score * 100).toFixed(0)}</div>
                    <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>priority</div>
                  </div>
                </div>
                {!!(o.evidence ?? []).length && (
                  <details className="mt-2">
                    <summary className="text-[12px] cursor-pointer" style={{ color: "var(--text-muted)" }}>
                      Evidence ({o.evidence.length})
                    </summary>
                    <div className="mt-1.5 space-y-1">
                      {o.evidence.map((e: any, i: number) => (
                        <div key={i} className="text-[11.5px] font-mono" style={{ color: "var(--text-muted)" }}>
                          {e.source} · {e.freshness} · conf {(e.confidence * 100).toFixed(0)}%
                          {e.evidence_ids?.length ? <> · {e.evidence_ids.join(", ")}</> : null}
                        </div>
                      ))}
                    </div>
                  </details>
                )}
              </Card>
            ))}
          </div>
        )}
      </Section>
    </div>
  );
}

/* --------------------------------------------------------------------- plans */

function Plans() {
  const plans = useFetch(() => wsApi.get("/planner/plans"), []);
  const [busy, setBusy] = useState("");
  const rows: any[] = (plans.data as any)?.plans ?? [];

  async function act(itemId: string, action: string, extra: Record<string, unknown> = {}) {
    setBusy(itemId + action);
    try {
      const autonomy = action === "campaign" ? "APPROVAL" : action === "schedule" ? "AUTONOMOUS" : "APPROVAL";
      const body: Record<string, unknown> = { autonomy, ...extra };
      if (action === "schedule") body.allowed_actions = ["SCHEDULE"];
      const r = await wsApi.post(`/planner/items/${itemId}/${action}`, body);
      toast(
        action === "schedule" && r.status === "SCHEDULED"
          ? `Placed${r.why_scheduled ? ` — ${r.why_scheduled}` : ""}`
          : `${action} → ${r.status ?? "ok"}`,
        action === "reject" ? "warning" : "success",
      );
      plans.reload();
    } catch (e: any) {
      toast(e.message, "error", `${action} failed`);
    } finally {
      setBusy("");
    }
  }

  return (
    <Section data={rows} loading={plans.loading} error={plans.error} onRetry={plans.reload}
      empty="No plans yet" emptyHint="Run a planning cycle from the Autonomous Planning tab.">
      {() => (
        <div className="space-y-4">
          {rows.map((p) => (
            <Card key={p.id}>
              <div className="flex items-start gap-3 flex-wrap">
                <div className="flex-1">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="font-semibold text-[14px]">Plan · {p.horizon_days}d horizon</span>
                    <Badge tone="muted">{p.autonomy}</Badge>
                    <Badge tone="muted">{p.item_count} items</Badge>
                    {p.budget_usd > 0 && <Badge tone="muted">${p.budget_remaining.toFixed(2)} left</Badge>}
                  </div>
                  <div className="text-[12px] mt-1" style={{ color: "var(--text-muted)" }}>
                    {AUTONOMY_HINT[p.autonomy] ?? ""}
                  </div>
                </div>
              </div>
              <div className="mt-3 space-y-2">
                {(p.items ?? []).map((it: any) => (
                  <div key={it.id} className="rounded-lg p-2.5" style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
                    <div className="flex items-start gap-2 flex-wrap">
                      <div className="flex-1 min-w-[200px]">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="text-[13px] font-medium">{it.angle || "(no angle)"}</span>
                          <Badge tone={it.status === "BLOCKED" ? "danger" : it.status === "SCHEDULED" ? "success" : "muted"}>{it.status}</Badge>
                          <Badge tone="muted">{it.content_format}</Badge>
                          <span className="font-mono text-[11.5px]" style={{ color: "var(--text-muted)" }}>
                            priority {(it.priority * 100).toFixed(0)}
                          </span>
                          {it.target_date && (
                            <span className="text-[11.5px]" style={{ color: "var(--text-muted)" }}>
                              {new Date(it.target_date).toLocaleDateString()}
                            </span>
                          )}
                        </div>
                        {it.blocked_reason && (
                          <div className="text-[12px] mt-1" style={{ color: "var(--danger)" }}>blocked: {it.blocked_reason}</div>
                        )}
                        {it.why?.why_created && (
                          <div className="text-[12px] mt-1" style={{ color: "var(--text-muted)" }}>why: {it.why.why_created}</div>
                        )}
                        {it.why?.why_scheduled && (
                          <div className="text-[12px] mt-0.5" style={{ color: "var(--text-muted)" }}>
                            why scheduled: {it.why.why_scheduled}
                            {it.why.evidence_backed_slot === false && (
                              <span style={{ color: "var(--text-faint)" }}> (seed window — no measured timing data)</span>
                            )}
                          </div>
                        )}
                        {!!(it.why?.memories_used ?? []).length && (
                          <div className="text-[11.5px] mt-0.5 font-mono" style={{ color: "var(--text-faint)" }}>
                            memories consulted: {it.why.memories_used.length}
                            {it.why.lessons_influencing?.length ? ` · lessons: ${it.why.lessons_influencing.length}` : ""}
                          </div>
                        )}
                      </div>
                      <div className="flex gap-1.5 flex-wrap">
                        {it.status === "IDEA" && (
                          <button className="btn-xs" disabled={!!busy} onClick={() => act(it.id, "approve")}>Approve</button>
                        )}
                        <button className="btn-xs" disabled={!!busy} onClick={() => act(it.id, "research_more", { reason: "operator wants more evidence" })}>
                          Research more
                        </button>
                        {!it.campaign_id && (
                          <button className="btn-xs" disabled={!!busy} onClick={() => act(it.id, "campaign")}>Create campaign</button>
                        )}
                        {it.campaign_id && ["PLANNED", "READY", "IDEA"].includes(it.status) && (
                          <button className="btn-xs" disabled={!!busy} onClick={() => act(it.id, "schedule")}>Schedule</button>
                        )}
                        <button className="btn-xs" disabled={!!busy}
                          onClick={() => act(it.id, "reject", { reason: "operator rejected" })}>
                          Reject
                        </button>
                      </div>
                    </div>
                  </div>
                ))}
              </div>
            </Card>
          ))}
        </div>
      )}
    </Section>
  );
}

/* ----------------------------------------------------------------- autonomous */

function Autonomous() {
  const policy = useFetch(() => wsApi.get("/planner/policy"), []);
  const cap = useFetch(() => wsApi.get("/planner/calendar"), []);
  const [mode, setMode] = useState("RECOMMEND");
  const [horizon, setHorizon] = useState(30);
  const [platforms, setPlatforms] = useState("threads");
  const [budget, setBudget] = useState("20");
  const [goals, setGoals] = useState("");
  const [result, setResult] = useState<any>(null);
  const [busy, setBusy] = useState(false);

  const modes: string[] = (policy.data as any)?.modes ?? [];
  const capData: any = (cap.data as any)?.capacity;

  async function run(preview: boolean) {
    setBusy(true);
    try {
      const r = await wsApi.post("/planner/plan", {
        autonomy: mode,
        horizon_days: horizon,
        platforms: platforms.split(",").map((p) => p.trim()).filter(Boolean),
        budget_usd: Number(budget) || 0,
        goals: goals.split(",").map((g) => g.trim()).filter(Boolean),
        preview,
      });
      setResult(r);
      toast(
        r.items?.length
          ? `Planned ${r.items.length} item(s)`
          : r.suggestions?.length
            ? `${r.suggestions.length} suggestion(s) — nothing written`
            : "Nothing to plan",
        r.items?.length ? "success" : "info",
      );
    } catch (e: any) {
      toast(e.message, "error", "Planning failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
          <Field label="Autonomy" hint={AUTONOMY_HINT[mode]}>
            <select className="input" value={mode} onChange={(e) => setMode(e.target.value)}>
              {(modes.length ? modes : ["DISABLED", "RECOMMEND", "APPROVAL", "AUTONOMOUS"]).map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          </Field>
          <Field label="Horizon (days)">
            <input className="input" type="number" min={1} max={365} value={horizon}
              onChange={(e) => setHorizon(Number(e.target.value))} />
          </Field>
          <Field label="Platforms" hint="comma separated">
            <input className="input" value={platforms} onChange={(e) => setPlatforms(e.target.value)} />
          </Field>
          <Field label="Budget (USD)" hint="the planner refuses a plan it cannot fund">
            <input className="input" type="number" min={0} step="0.5" value={budget}
              onChange={(e) => setBudget(e.target.value)} />
          </Field>
          <div className="md:col-span-2">
            <Field label="Goals" hint="comma separated">
              <input className="input" value={goals} placeholder="grow newsletter, test a new hook"
                onChange={(e) => setGoals(e.target.value)} />
            </Field>
          </div>
        </div>
        <div className="flex gap-2 mt-3">
          <button className="btn-primary" disabled={busy} onClick={() => run(false)}>
            {busy ? "Planning…" : "Plan"}
          </button>
          <button className="btn-outline" disabled={busy} onClick={() => run(true)}>Preview only</button>
        </div>
        <p className="text-[12px] mt-2" style={{ color: "var(--text-faint)" }}>
          No planning mode can publish. Scheduling stops at SCHEDULED; publication still requires the
          existing approval path.
        </p>
      </Card>

      {result && (
        <Card>
          <div className="flex items-center gap-2 flex-wrap">
            <span className="font-semibold text-[13.5px]">Result</span>
            <Badge tone="muted">{result.autonomy}</Badge>
            {result.preview && <Badge tone="warning">preview — nothing written</Badge>}
            <Badge tone="success">{result.items?.length ?? 0} items</Badge>
            <Badge tone="muted">{result.suggestions?.length ?? 0} suggestions</Badge>
            {(result.blocked ?? []).length > 0 && <Badge tone="danger">{result.blocked.length} blocked</Badge>}
          </div>
          {(result.notes ?? []).map((n: string, i: number) => (
            <div key={i} className="text-[12px] mt-1.5" style={{ color: "var(--text-muted)" }}>{n}</div>
          ))}
          {(result.suggestions ?? []).length > 0 && (
            <div className="mt-2 space-y-1.5">
              {result.suggestions.map((s: any, i: number) => (
                <div key={i} className="rounded-lg p-2 text-[12.5px]"
                  style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
                  <div className="flex items-center gap-2 flex-wrap">
                    <span>{s.topic}</span>
                    <Badge tone={BASIS_TONE[s.basis] ?? "muted"}>{s.basis}</Badge>
                    <span className="font-mono" style={{ color: "var(--text-muted)" }}>
                      {(s.score * 100).toFixed(0)}/100
                    </span>
                  </div>
                  <div className="text-[11.5px] mt-0.5" style={{ color: "var(--text-faint)" }}>{s.reason}</div>
                </div>
              ))}
            </div>
          )}
          {(result.blocked ?? []).map((b: any, i: number) => (
            <div key={i} className="text-[12px] mt-1.5" style={{ color: "var(--danger)" }}>
              {b.verdict}{b.topic ? `: ${b.topic}` : ""} — {(b.reasons ?? [b.reason]).filter(Boolean).join("; ")}
            </div>
          ))}
        </Card>
      )}

      <Card>
        <div className="font-semibold text-[13.5px] mb-2">Capacity</div>
        {capData?.declared ? (
          <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
            {(["shorts_per_day", "longform_per_week", "ugc_per_day", "localization_per_day",
               "render_hours_per_day", "review_slots_per_day"] as const).map((k) => (
              <Stat key={k} label={k.replace(/_/g, " ")} value={capData[k]} />
            ))}
          </div>
        ) : (
          <Empty title="No capacity declared"
            hint="Unbounded means the operator has set no limit — the planner will not invent one and claim the work is infeasible." />
        )}
        {(cap.data as any)?.committed?.item_count > 0 && (
          <p className="text-[12px] mt-2" style={{ color: "var(--text-muted)" }}>
            {(cap.data as any).committed.item_count} item(s) already committed against this capacity.
          </p>
        )}
      </Card>
    </div>
  );
}

/* -------------------------------------------------------------------- export */

type TabKey = "signals" | "opportunities" | "plans" | "autonomous";

export default function Planner() {
  const [tab, setTab] = useState<TabKey>("signals");
  return (
    <div className="space-y-4">
      <PageHeader
        title="Planner"
        subtitle="signals → opportunities → plan → calendar → learn. AI suggestions are shown separately from measured evidence."
      />
      <Tabs
        tabs={[
          { key: "signals", label: "Signals" },
          { key: "opportunities", label: "Opportunities" },
          { key: "plans", label: "Editorial Plan" },
          { key: "autonomous", label: "Autonomous Planning" },
        ]}
        active={tab}
        onChange={setTab}
      />
      {tab === "signals" && <Signals />}
      {tab === "opportunities" && <Opportunities />}
      {tab === "plans" && <Plans />}
      {tab === "autonomous" && <Autonomous />}
    </div>
  );
}

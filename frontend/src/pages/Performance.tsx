import { useEffect, useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Accordion, Badge, Card, ConfirmButton, Field, PageHeader, Section, Stat, Tabs, toast } from "../components/ui";
import { Bars, Spark } from "../components/charts";
import { fmtAgo, fmtCompact } from "../lib/format";

/* Performance Intelligence (Work 06): campaign rollups, retention curves,
   creative group comparisons, experiments, and scoped lessons.
   Every panel degrades to an empty/error state when its route 404s —
   no hardcoded mocks anywhere. */

const COMPARE_GROUPS = ["hook", "caption", "duration", "voice", "broll", "posting-window"] as const;
const CHECKPOINT_ORDER = ["1s", "3s", "25%", "50%", "75%", "100%"];

function is404(err: string | null): boolean {
  return !!err && /404/.test(err);
}

function notFoundMsg(err: string | null, fallback: string): string {
  return is404(err) ? "Performance API unavailable on this backend (404)." : (err ?? fallback);
}

export default function Performance() {
  const [tab, setTab] = useState<"overview" | "retention" | "creative" | "experiments" | "lessons">("overview");
  const camps = useFetch(() => wsApi.get("/campaigns"), []);
  const items: any[] = (camps.data as any)?.items ?? [];
  const [campaignId, setCampaignId] = useState("");

  useEffect(() => {
    if (!campaignId && items.length) setCampaignId(items[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [camps.data]);

  return (
    <div className="space-y-4">
      <PageHeader title="Performance" subtitle="What performed, where viewers drop, and what to try next."
        actions={
          items.length > 0 ? (
            <select className="select !text-xs" value={campaignId} onChange={(e) => setCampaignId(e.target.value)}
              aria-label="Select campaign">
              {items.map((c: any) => (
                <option key={c.id} value={c.id}>{c.name ?? c.id.slice(0, 8)}</option>
              ))}
            </select>
          ) : undefined
        } />
      {camps.error && (
        <div className="text-[12.5px] font-mono" style={{ color: "var(--warn)" }}>{notFoundMsg(camps.error, "Campaigns failed to load.")}</div>
      )}
      <Tabs tabs={[
        { key: "overview", label: "Overview" },
        { key: "retention", label: "Retention" },
        { key: "creative", label: "Creative Analysis" },
        { key: "experiments", label: "Experiments" },
        { key: "lessons", label: "Lessons" },
      ]} active={tab} onChange={setTab} />

      {tab === "overview" && <OverviewPanel campaignId={campaignId} />}
      {tab === "retention" && <RetentionPanel />}
      {tab === "creative" && <CreativePanel campaignId={campaignId} />}
      {tab === "experiments" && <ExperimentsPanel />}
      {tab === "lessons" && <LessonsPanel />}
    </div>
  );
}

/* ---- Overview: campaign rollup + platform comparison + top/bottom ---- */

function OverviewPanel({ campaignId }: { campaignId: string }) {
  const over = useFetch(
    () => wsApi.get(`/performance/overview?campaign_id=${encodeURIComponent(campaignId)}`),
    [campaignId]
  );
  const rollup: any = (over.data as any)?.rollup;
  const totals: any = rollup?.totals;
  const platforms: Record<string, any> = (over.data as any)?.platforms ?? {};
  const shorts: any[] = [...(rollup?.shorts ?? [])].sort((a, b) => (b.views ?? 0) - (a.views ?? 0));

  if (!campaignId) {
    return <Card><div className="text-[13px]" style={{ color: "var(--text-faint)" }}>No campaign selected — create one in Campaigns first.</div></Card>;
  }
  if (over.loading && !over.data) {
    return <Card><div className="text-[13px]" style={{ color: "var(--text-faint)" }}>Loading campaign performance…</div></Card>;
  }
  if (over.error && !over.data) {
    return (
      <Card>
        <div className="text-[13px]" style={{ color: "var(--text-faint)" }}>{notFoundMsg(over.error, "Overview failed to load.")}
          {!is404(over.error) && <button className="btn-outline !text-xs ml-3" onClick={over.reload}>Retry</button>}
        </div>
      </Card>
    );
  }
  if (!rollup) {
    return <Card><div className="text-[13px]" style={{ color: "var(--text-faint)" }}>No performance data for this campaign yet.</div></Card>;
  }

  const top = shorts.slice(0, 5);
  const bottom = shorts.length > 5 ? shorts.slice(-3).reverse() : [];

  return (
    <div className="space-y-4">
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-3">
        <Stat label="Total views" value={fmtCompact(totals?.views)} hint={`${rollup.post_count ?? 0} posts · ${rollup.short_count ?? 0} shorts`} />
        <Stat label="Engagement" value={totals?.engagement_rate != null ? `${(totals.engagement_rate * 100).toFixed(1)}%` : "—"} />
        <Stat label="Completion" value={totals?.completion != null ? `${(totals.completion * 100).toFixed(1)}%` : "—"} />
        <Stat label="Watch time" value={totals?.watch_time != null ? `${fmtCompact(totals.watch_time)}s` : "—"} />
      </div>
      <div className="grid lg:grid-cols-2 gap-4">
        <Card>
          <b className="text-[14px]">Platform comparison</b>
          {Object.keys(platforms).length === 0 ? (
            <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>No platform data yet.</div>
          ) : (
            <div className="mt-2">
              <Bars data={Object.entries(platforms).map(([label, v]: any) => ({ label, value: v.views ?? 0 }))} />
              <div className="mt-2 space-y-1.5">
                {Object.entries(platforms).map(([p, v]: any) => (
                  <div key={p} className="flex items-center gap-2 text-[12.5px] flex-wrap">
                    <Badge tone="muted">{p}</Badge>
                    <span className="font-mono">{fmtCompact(v.views)} views · {v.posts ?? 0} posts</span>
                    <span className="font-mono" style={{ color: "var(--text-muted)" }}>
                      {(v.completion != null ? `${(v.completion * 100).toFixed(0)}% completion` : "")}
                    </span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </Card>
        <Card>
          <b className="text-[14px]">Views trend across shorts</b>
          {shorts.length === 0 ? (
            <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>No shorts attributed to this campaign yet.</div>
          ) : (
            <div className="mt-2">
              <Spark data={shorts.map((s) => s.views ?? 0)} height={64} />
              <div className="text-[12px] mt-1 font-mono" style={{ color: "var(--text-muted)" }}>
                {shorts.length} shorts · best {fmtCompact(shorts[0]?.views)} views
              </div>
            </div>
          )}
        </Card>
      </div>
      <div className="grid lg:grid-cols-2 gap-4">
        <Card>
          <b className="text-[14px]">Top content</b>
          {top.length === 0 ? (
            <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>Nothing measured yet.</div>
          ) : (
            <div className="mt-2 space-y-2">
              {top.map((s: any) => (
                <ShortRow key={s.short_id} s={s} tone="var(--accent)" />
              ))}
            </div>
          )}
        </Card>
        <Card>
          <b className="text-[14px]">Bottom content</b>
          {bottom.length === 0 ? (
            <div className="text-[13px] mt-2" style={{ color: "var(--text-faint)" }}>
              {shorts.length === 0 ? "Nothing measured yet." : "Not enough shorts to rank laggards (6+ needed)."}
            </div>
          ) : (
            <div className="mt-2 space-y-2">
              {bottom.map((s: any) => (
                <ShortRow key={s.short_id} s={s} tone="var(--danger)" />
              ))}
            </div>
          )}
        </Card>
      </div>
    </div>
  );
}

function ShortRow({ s, tone }: { s: any; tone: string }) {
  return (
    <div className="flex items-center gap-2 text-[12.5px] flex-wrap">
      <code className="text-[11.5px]">{String(s.short_id ?? "—").slice(0, 8)}…</code>
      <b className="font-mono" style={{ color: tone }}>{fmtCompact(s.views)}</b>
      <span className="font-mono" style={{ color: "var(--text-muted)" }}>
        {s.completion != null ? `${(s.completion * 100).toFixed(0)}% compl` : ""} · {s.posts ?? 0} posts
      </span>
    </div>
  );
}

/* ---- Retention: curve from points + scene mapping + drop/rewatch markers ---- */

function orderCheckpoints(curve: Record<string, number>): string[] {
  const keys = Object.keys(curve ?? {});
  return keys.sort((a, b) => {
    const ia = CHECKPOINT_ORDER.indexOf(a);
    const ib = CHECKPOINT_ORDER.indexOf(b);
    if (ia === -1 && ib === -1) return a.localeCompare(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
}

function RetentionPanel() {
  const [postId, setPostId] = useState("");
  const [shortId, setShortId] = useState("");
  const [data, setData] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  async function load() {
    const p = new URLSearchParams();
    if (postId.trim()) p.set("post_id", postId.trim());
    if (shortId.trim()) p.set("short_id", shortId.trim());
    if (!p.toString()) return;
    setBusy(true);
    setErr(null);
    try {
      setData(await wsApi.get(`/performance/retention?${p.toString()}`));
    } catch (e: any) {
      setErr(e?.message ?? "retention lookup failed");
      setData(null);
    } finally {
      setBusy(false);
    }
  }

  const status = data?.status;
  const curve: Record<string, number> = data?.curve ?? {};
  const ordered = orderCheckpoints(curve);
  const drops: any[] = data?.drops ?? [];
  const rewatches: any[] = data?.rewatches ?? [];
  const mapping: Record<string, any> = data?.mapping ?? {};

  return (
    <div className="space-y-4">
      <Card>
        <div className="grid md:grid-cols-[1fr_1fr_auto] gap-2 items-end">
          <Field label="Post ID" hint="Published post to analyze.">
            <input className="input font-mono !text-xs" value={postId} placeholder="post id…"
              onChange={(e) => setPostId(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load()} />
          </Field>
          <Field label="Short ID" hint="Or a short (content item) id.">
            <input className="input font-mono !text-xs" value={shortId} placeholder="short id…"
              onChange={(e) => setShortId(e.target.value)} onKeyDown={(e) => e.key === "Enter" && load()} />
          </Field>
          <button className="btn-primary !text-xs mb-3" disabled={busy || (!postId.trim() && !shortId.trim())} onClick={load}>
            {busy ? "Loading…" : "Load curve"}
          </button>
        </div>
        {err && <div className="text-[12.5px] font-mono" style={{ color: "var(--danger)" }}>{notFoundMsg(err, "Retention lookup failed.")}</div>}
      </Card>

      {!data && !busy && (
        <Card><div className="text-[13px]" style={{ color: "var(--text-faint)" }}>Enter a post or short id to load its retention curve.</div></Card>
      )}

      {data && status === "UNAVAILABLE" && (
        <Card>
          <div className="flex items-center gap-2 flex-wrap">
            <b className="text-[14px]">Retention unavailable</b>
            <Badge tone="warning">UNAVAILABLE</Badge>
          </div>
          <div className="text-[13px] mt-2" style={{ color: "var(--text-muted)" }}>
            {data.reason ?? "No granular retention data for this platform."}
          </div>
          {data.coarse_proxy && (
            <div className="mt-2 text-[12.5px] font-mono" style={{ color: "var(--text-muted)" }}>
              Coarse proxy (labeled, not a real curve): {JSON.stringify(data.coarse_proxy).slice(0, 300)}
            </div>
          )}
        </Card>
      )}

      {data && status === "AVAILABLE" && (
        <>
          <Card>
            <div className="flex items-center gap-2 flex-wrap">
              <b className="text-[14px]">Retention curve</b>
              <Badge tone="success">AVAILABLE</Badge>
              {data.source ? <Badge tone="muted">source: {data.source}</Badge> : null}
              {(data.missing ?? []).length > 0 && (
                <Badge tone="warning">missing: {(data.missing as string[]).join(", ")}</Badge>
              )}
            </div>
            <RetentionCurve ordered={ordered} curve={curve} drops={drops} />
            <div className="mt-2 space-y-1.5">
              {drops.length === 0 && rewatches.length === 0 && (
                <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>No sharp drops or rewatches detected.</div>
              )}
              {drops.map((d: any, i: number) => (
                <div key={`d${i}`} className="flex items-center gap-2 text-[12.5px] flex-wrap">
                  <Badge tone="error">drop</Badge>
                  <span className="font-mono text-[12px]">
                    {d.from ?? `${d.from_t}s`} → {d.to ?? `${d.to_t}s`} · −{(d.loss * 100).toFixed(1)}%
                  </span>
                  <span className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>{d.type}</span>
                </div>
              ))}
              {rewatches.map((r: any, i: number) => (
                <div key={`r${i}`} className="flex items-center gap-2 text-[12.5px] flex-wrap">
                  <Badge tone="info">rewatch</Badge>
                  <span className="font-mono text-[12px]">t={r.t}s · {(r.value * 100).toFixed(0)}%</span>
                </div>
              ))}
            </div>
          </Card>
          <Card>
            <b className="text-[14px]">Scene boundaries</b>
            <div className="mt-2 space-y-2">
              {ordered.map((cp) => {
                const r = mapping[cp] ?? {};
                return (
                  <div key={cp} className="flex items-center gap-2 text-[12.5px] flex-wrap">
                    <code className="text-[11.5px] w-[44px]">{cp}</code>
                    <b className="font-mono">{((curve[cp] ?? 0) * 100).toFixed(0)}%</b>
                    {r.mapped ? (
                      <>
                        {r.scene_title ? <span>{r.scene_title}</span> : <span style={{ color: "var(--text-muted)" }}>scene {r.scene_index ?? "?"}</span>}
                        {r.chapter_title ? <Badge tone="muted">{r.chapter_title}</Badge> : null}
                        {r.is_hook ? <Badge tone="warning">hook</Badge> : null}
                        {r.caption_state ? <Badge tone="info">{Array.isArray(r.caption_state) ? `${r.caption_state.length} captions` : "captioned"}</Badge> : null}
                      </>
                    ) : (
                      <span className="text-[12px]" style={{ color: "var(--text-faint)" }}>unmapped{r.reason ? ` — ${r.reason}` : ""}</span>
                    )}
                  </div>
                );
              })}
            </div>
          </Card>
        </>
      )}
    </div>
  );
}

function RetentionCurve({ ordered, curve, drops }: { ordered: string[]; curve: Record<string, number>; drops: any[] }) {
  const W = 560;
  const H = 180;
  const PAD = 28;
  const n = Math.max(1, ordered.length);
  const x = (i: number) => (n === 1 ? W / 2 : PAD + (i / (n - 1)) * (W - PAD * 2));
  const y = (v: number) => H - PAD - Math.max(0, Math.min(1, v)) * (H - PAD * 2);
  const pts = ordered.map((cp, i) => `${x(i).toFixed(1)},${y(curve[cp] ?? 0).toFixed(1)}`).join(" ");
  const dropIdx = new Set<number>();
  drops.forEach((d: any) => {
    const j = ordered.indexOf(d.to);
    if (j >= 0) dropIdx.add(j);
  });
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: 190 }} role="img" className="mt-2">
      {[0.25, 0.5, 0.75, 1].map((f) => (
        <line key={f} x1={PAD} x2={W - PAD} y1={y(f)} y2={y(f)} stroke="var(--bg-subtle)" strokeWidth={1} />
      ))}
      <polyline points={pts} fill="none" stroke="var(--accent)" strokeWidth={2.5} strokeLinejoin="round" strokeLinecap="round" />
      {ordered.map((cp, i) => (
        <g key={cp}>
          <circle cx={x(i)} cy={y(curve[cp] ?? 0)} r={dropIdx.has(i) ? 5 : 3.5}
            fill={dropIdx.has(i) ? "var(--danger)" : "var(--accent)"} stroke="var(--bg-panel)" strokeWidth={1.5}>
            <title>{`${cp}: ${(((curve[cp] ?? 0) * 100).toFixed(1))}%`}</title>
          </circle>
          <text x={x(i)} y={H - 8} fontSize={10} textAnchor="middle" fill="var(--text-faint)">{cp}</text>
        </g>
      ))}
    </svg>
  );
}

/* ---- Creative analysis: group comparisons with sample sizes ---- */

function CreativePanel({ campaignId }: { campaignId: string }) {
  const [groupBy, setGroupBy] = useState<string>("hook");
  const cmp = useFetch(
    () => wsApi.get(`/performance/compare?campaign_id=${encodeURIComponent(campaignId)}&group_by=${encodeURIComponent(groupBy)}`),
    [campaignId, groupBy]
  );
  const groups: any[] = (cmp.data as any)?.groups ?? [];

  if (!campaignId) {
    return <Card><div className="text-[13px]" style={{ color: "var(--text-faint)" }}>No campaign selected — create one in Campaigns first.</div></Card>;
  }

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap">
        <b className="text-[14px]">Creative analysis</b>
        <Badge tone="info">correlational only</Badge>
        <span className="ml-auto">
          <select className="select !text-xs" value={groupBy} onChange={(e) => setGroupBy(e.target.value)} aria-label="Group by">
            {COMPARE_GROUPS.map((g) => <option key={g} value={g}>{g}</option>)}
          </select>
        </span>
      </div>
      <Section data={groups} loading={cmp.loading} error={null} onRetry={cmp.reload}
        empty={`No measured posts for ${groupBy} comparison`}
        emptyHint="Publish and measure posts in this campaign to populate group comparisons.">
        {(list) => (
          <div className="mt-2 space-y-2">
            {(cmp.data as any)?.note && (
              <div className="text-[12px]" style={{ color: "var(--text-faint)" }}>{(cmp.data as any).note}</div>
            )}
            {list.map((g: any) => (
              <div key={g.group} className="flex items-center gap-2 text-[12.5px] flex-wrap">
                <code className="text-[12px] min-w-[110px]">{g.group}</code>
                <span className="font-mono">n={g.n}</span>
                <span className="font-mono">{fmtCompact(g.views)} views</span>
                <span className="font-mono" style={{ color: "var(--text-muted)" }}>{(g.avg_completion * 100).toFixed(0)}% avg completion</span>
                {g.low_sample ? <Badge tone="warning">low-sample</Badge> : <Badge tone="success">n≥2</Badge>}
              </div>
            ))}
          </div>
        )}
      </Section>
      {cmp.error && (
        <div className="text-[12.5px] font-mono mt-2" style={{ color: "var(--danger)" }}>
          {notFoundMsg(cmp.error, "Comparison failed to load.")}
          {!is404(cmp.error) && <button className="btn-outline !text-xs ml-3" onClick={cmp.reload}>Retry</button>}
        </div>
      )}
    </Card>
  );
}

/* ---- Experiments: hypothesis, variants, sample, result, status + actions ---- */

function ExperimentsPanel() {
  const list = useFetch(() => wsApi.get("/experiments"), []);
  const items: any[] = (list.data as any)?.items ?? [];
  const [open, setOpen] = useState(false);
  const [hypothesis, setHypothesis] = useState("");
  const [kind, setKind] = useState("HOOK");
  const [platform, setPlatform] = useState("");
  const [primaryMetric, setPrimaryMetric] = useState("views");
  const [minimumSample, setMinimumSample] = useState("60");
  const [creating, setCreating] = useState(false);
  const [acting, setActing] = useState<string | null>(null);

  async function create() {
    if (!hypothesis.trim()) return;
    setCreating(true);
    try {
      await wsApi.post("/experiments", {
        kind: kind.trim() || "HOOK",
        hypothesis: hypothesis.trim(),
        platform: platform.trim(),
        primary_metric: primaryMetric.trim() || "views",
        minimum_sample: Math.max(2, Number(minimumSample) || 60),
      });
      setHypothesis("");
      setOpen(false);
      list.reload();
      toast("Experiment created (DRAFT)", "success");
    } catch (e: any) {
      toast(e?.message ?? "create failed", "error", "Create failed");
    } finally {
      setCreating(false);
    }
  }

  async function act(id: string, action: "start" | "cancel" | "analyze") {
    setActing(`${id}:${action}`);
    try {
      await wsApi.post(`/experiments/${id}/${action}`, {});
      list.reload();
      toast(`Experiment ${action}ed`, "success");
    } catch (e: any) {
      toast(e?.message ?? `${action} failed`, "error", "Action failed");
    } finally {
      setActing(null);
    }
  }

  return (
    <div className="space-y-4">
      <Card>
        <div className="flex items-center gap-2 flex-wrap">
          <b className="text-[14px]">Experiments</b>
          <Badge tone="info">{items.length} total</Badge>
          <span className="ml-auto flex gap-2">
            <button className="btn-ghost !text-xs !py-1" onClick={list.reload}>Refresh</button>
            <button className="btn-primary !text-xs !py-1" onClick={() => setOpen(!open)}>+ New experiment</button>
          </span>
        </div>
        {open && (
          <div className="grid md:grid-cols-2 gap-x-4 mt-3">
            <Field label="Hypothesis">
              <textarea className="textarea !text-xs" rows={2} value={hypothesis}
                placeholder="Question-style hooks lift 3s retention vs number hooks…"
                onChange={(e) => setHypothesis(e.target.value)} />
            </Field>
            <div className="grid grid-cols-2 gap-x-3">
              <Field label="Kind">
                <input className="input font-mono !text-xs" value={kind} onChange={(e) => setKind(e.target.value)} />
              </Field>
              <Field label="Platform">
                <input className="input font-mono !text-xs" value={platform} placeholder="youtube…"
                  onChange={(e) => setPlatform(e.target.value)} />
              </Field>
              <Field label="Primary metric">
                <input className="input font-mono !text-xs" value={primaryMetric} onChange={(e) => setPrimaryMetric(e.target.value)} />
              </Field>
              <Field label="Minimum sample">
                <input className="input font-mono !text-xs" type="number" value={minimumSample}
                  onChange={(e) => setMinimumSample(e.target.value)} />
              </Field>
            </div>
            <div>
              <button className="btn-primary !text-xs" disabled={creating || !hypothesis.trim()} onClick={create}>
                {creating ? "Creating…" : "Create draft"}
              </button>
            </div>
          </div>
        )}
      </Card>
      <Section data={items} loading={list.loading} error={list.error} onRetry={list.reload}
        empty="No experiments yet" emptyHint="Create one above to test a creative hypothesis with a real sample.">
        {(rows) => (
          <div className="space-y-3">
            {rows.map((x: any) => (
              <Card key={x.id}>
                <div className="flex items-center gap-2 flex-wrap">
                  <Badge tone="muted">{x.kind}</Badge>
                  <Badge tone={x.status === "RUNNING" ? "success" : x.status === "DRAFT" ? "info" : x.status === "CANCELLED" ? "muted" : "warning"}>
                    {x.status}
                  </Badge>
                  {x.platform ? <Badge tone="muted">{x.platform}</Badge> : null}
                  {x.confidence ? <Badge tone="info">{x.confidence}</Badge> : null}
                  <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{fmtAgo(x.updated_at)}</span>
                </div>
                <div className="text-[13px] mt-1.5">{x.hypothesis || "—"}</div>
                <div className="font-mono text-[12px] mt-1" style={{ color: "var(--text-muted)" }}>
                  metric: {x.primary_metric} · min sample: {x.minimum_sample} · variants: {(x.variants ?? []).length}
                </div>
                {x.result && Object.keys(x.result).length > 0 && (
                  <Accordion title="Result">
                    <pre className="text-[12px] font-mono whitespace-pre-wrap max-h-[220px] overflow-y-auto">
                      {JSON.stringify(x.result, null, 2)}
                    </pre>
                  </Accordion>
                )}
                <div className="flex gap-2 mt-2 flex-wrap">
                  {x.status === "DRAFT" && (
                    <button className="btn-primary !text-xs !py-1" disabled={acting === `${x.id}:start`} onClick={() => act(x.id, "start")}>
                      {acting === `${x.id}:start` ? "Starting…" : "Start"}
                    </button>
                  )}
                  {x.status === "RUNNING" && (
                    <>
                      <button className="btn-outline !text-xs !py-1" disabled={acting === `${x.id}:analyze`} onClick={() => act(x.id, "analyze")}>
                        {acting === `${x.id}:analyze` ? "Analyzing…" : "Analyze"}
                      </button>
                      <button className="btn-ghost !text-xs !py-1" disabled={acting === `${x.id}:cancel`} onClick={() => act(x.id, "cancel")}>
                        {acting === `${x.id}:cancel` ? "Cancelling…" : "Cancel"}
                      </button>
                    </>
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

/* ---- Lessons: lesson, scope, evidence count, effect, confidence, freshness + disable ---- */

function LessonsPanel() {
  const [platform, setPlatform] = useState("");
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const [query, setQuery] = useState("");
  const list = useFetch(() => wsApi.get(`/lessons${query}`), [query]);
  const items: any[] = (list.data as any)?.items ?? [];
  const [evidence, setEvidence] = useState<Record<string, any>>({});
  const [evBusy, setEvBusy] = useState<string | null>(null);

  function apply() {
    const p = new URLSearchParams();
    if (platform.trim()) p.set("platform", platform.trim());
    if (status.trim()) p.set("status", status.trim());
    if (kind.trim()) p.set("kind", kind.trim());
    const q = p.toString();
    setQuery(q ? `?${q}` : "");
  }

  async function loadEvidence(id: string) {
    if (evidence[id]) {
      setEvidence((prev) => {
        const next = { ...prev };
        delete next[id];
        return next;
      });
      return;
    }
    if (evBusy) return;
    setEvBusy(id);
    try {
      const out = await wsApi.get(`/lessons/${id}/evidence`);
      setEvidence((prev) => ({ ...prev, [id]: out }));
    } catch (e: any) {
      toast(e?.message ?? "evidence failed", "error", "Evidence failed");
    } finally {
      setEvBusy(null);
    }
  }

  async function disable(id: string) {
    try {
      await wsApi.post(`/lessons/${id}/disable`, {});
      list.reload();
      toast("Lesson disabled (history preserved)", "success");
    } catch (e: any) {
      toast(e?.message ?? "disable failed", "error", "Disable failed");
    }
  }

  return (
    <Card>
      <div className="flex items-center gap-2 flex-wrap">
        <b className="text-[14px]">Lessons</b>
        <Badge tone="info">{items.length} shown</Badge>
        <span className="ml-auto">
          <button className="btn-ghost !text-xs !py-1" onClick={list.reload}>Refresh</button>
        </span>
      </div>
      <div className="grid md:grid-cols-[1fr_180px_180px_auto] gap-2 mt-3 items-end">
        <Field label="Platform filter">
          <input className="input font-mono !text-xs" value={platform} placeholder="e.g. youtube"
            onChange={(e) => setPlatform(e.target.value)} onKeyDown={(e) => e.key === "Enter" && apply()} />
        </Field>
        <Field label="Status filter">
          <input className="input font-mono !text-xs" value={status} placeholder="e.g. fresh"
            onChange={(e) => setStatus(e.target.value)} onKeyDown={(e) => e.key === "Enter" && apply()} />
        </Field>
        <Field label="Kind filter">
          <input className="input font-mono !text-xs" value={kind} placeholder="e.g. hook"
            onChange={(e) => setKind(e.target.value)} onKeyDown={(e) => e.key === "Enter" && apply()} />
        </Field>
        <button className="btn-outline !text-xs mb-3" onClick={apply}>Apply</button>
      </div>
      <Section data={items} loading={list.loading} error={list.error} onRetry={list.reload}
        empty="No lessons yet" emptyHint="Lessons form from measured performance once patterns validate with evidence.">
        {(rows) => (
          <div className="mt-2 space-y-3">
            {rows.map((l: any) => {
              const scope = Object.entries(l.scope ?? {}).filter(([, v]) => v);
              const ev = evidence[l.id];
              return (
                <div key={l.id} className="rounded-xl p-3" style={{ border: "var(--seam)", background: "var(--bg-inset)" }}>
                  <div className="flex items-center gap-2 flex-wrap text-[12.5px]">
                    <code className="text-[11.5px]">{l.pattern_key}</code>
                    <Badge tone={l.status === "fresh" ? "success" : l.status === "disabled" ? "muted" : "warning"}>{l.status}</Badge>
                    <Badge tone={l.confidence === "high" ? "success" : l.confidence === "medium" ? "info" : "muted"}>
                      {l.confidence} · n={l.sample_size}
                    </Badge>
                    <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                      {l.last_validated_at ? `validated ${fmtAgo(l.last_validated_at)}` : l.created_at ? fmtAgo(l.created_at) : ""}
                    </span>
                  </div>
                  <div className="text-[13px] mt-1">{l.description}</div>
                  <div className="flex items-center gap-2 mt-1.5 flex-wrap text-[12px]" style={{ color: "var(--text-muted)" }}>
                    {scope.length === 0 ? <span>scope: global</span> : scope.map(([k, v]) => (
                      <Badge key={k} tone="muted">{k}: {String(v)}</Badge>
                    ))}
                    <span className="font-mono">{(l.evidence_ids ?? []).length} evidence</span>
                    {l.effect && Object.keys(l.effect).length > 0 && (
                      <span className="font-mono">effect: {JSON.stringify(l.effect).slice(0, 120)}</span>
                    )}
                  </div>
                  <div className="flex gap-2 mt-2">
                    <button className="btn-ghost !text-xs !py-0.5" onClick={() => loadEvidence(l.id)}>
                      {ev ? "Hide evidence" : evBusy === l.id ? "Loading…" : "Show evidence"}
                    </button>
                    {l.status !== "disabled" && (
                      <ConfirmButton onConfirm={() => disable(l.id)} className="btn-outline !text-xs !py-0.5">
                        Disable
                      </ConfirmButton>
                    )}
                  </div>
                  {evBusy === l.id && !ev && (
                    <div className="text-[12px] mt-1" style={{ color: "var(--text-faint)" }}>Loading evidence…</div>
                  )}
                  {ev && (
                    <pre className="text-[11.5px] font-mono whitespace-pre-wrap max-h-[220px] overflow-y-auto mt-2 p-2.5 rounded-lg"
                      style={{ background: "var(--bg-panel)" }}>
                      {JSON.stringify(ev, null, 2)}
                    </pre>
                  )}
                </div>
              );
            })}
          </div>
        )}
      </Section>
    </Card>
  );
}

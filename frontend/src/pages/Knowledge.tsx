/* Knowledge — Work 10 Lane G: /knowledge page over backend/app/api/v1/knowledge.py.
 *
 * Four tabs (client-side state, same pattern as Inbox):
 *   Memory           GET /knowledge/memories (filters) + verify/disable/supersede actions
 *   Graph            GET /knowledge/graph — nodes grouped by node_type, edges grouped
 *                    by relationship as `from --RELATIONSHIP--> to` (no graph lib)
 *   Sources          GET /knowledge/sources (+ register/sync/disconnect, per-connector
 *                    documents) — polled every 30s while the tab is open
 *   Community        GET /knowledge/community-signals + POST /knowledge/promote-insights
 *
 * Mirrors the Work 09 Inbox page: wsApi (workspace-scoped mount), useFetch with deps +
 * reload-after-mutation, shared ui.tsx primitives and design tokens only — no new deps.
 * Every fetch degrades honestly: HTTP status + detail on error, explicit empty states,
 * and an "unexpected response shape" card instead of guessing when a payload does not
 * match the contract (never fabricated data). Roles are enforced server-side (403 is
 * surfaced verbatim) — this page, like Inbox, has no client-side role context.
 */
import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch, useInterval } from "../hooks/hooks";
import {
  Badge, Card, ConfirmButton, Empty, ErrorBox, Field, Loading, Modal,
  PageHeader, SearchInput, Tabs, toast,
} from "../components/ui";
import { fmtAgo } from "../lib/format";

type TabKey = "memory" | "graph" | "sources" | "signals";

const TABS: { key: TabKey; label: string }[] = [
  { key: "memory", label: "Memory" },
  { key: "graph", label: "Graph" },
  { key: "sources", label: "Sources" },
  { key: "signals", label: "Community Signals" },
];

/* contract vocabulary (work10_contracts.md — Lane A / Lane B / Lane C / Lane D) */
const MEMORY_TYPES = [
  "RESEARCH_FACT", "SOURCE", "CONTENT_RESULT", "AUDIENCE_INSIGHT", "COMMUNITY_INSIGHT",
  "BRAND_KNOWLEDGE", "CREATIVE_LESSON", "EXPERIMENT_RESULT", "PLATFORM_LEARNING",
  "ENTITY", "RELATIONSHIP", "USER_APPROVED_KNOWLEDGE",
];
/* lifecycle statuses (knowledge_memories.status) — freshness bands are display-only */
const MEMORY_STATUSES = ["ACTIVE", "UNVERIFIED", "CONFLICTED", "SUPERSEDED", "DISABLED"];
const NODE_TYPES = [
  "Brand", "Campaign", "Content", "Scene", "Topic", "Entity", "Source", "Claim",
  "AudienceInsight", "CommunityInsight", "Experiment", "CreativeLesson", "Publication",
];
const CONNECTOR_KINDS = [
  "local", "url", "rss", "youtube", "s3", "google_drive", "dropbox", "onedrive",
  "zoom", "riverside", "twitch", "vimeo", "loom",
];

/* effective status → badge tone (FRESH/AGING/STALE freshness band + lifecycle status) */
const EFF_TONE: Record<string, string> = {
  FRESH: "success", AGING: "info", STALE: "warning",
  CONFLICTED: "error", SUPERSEDED: "muted", UNVERIFIED: "warning",
  DISABLED: "muted", ACTIVE: "success",
};
const CONNECTOR_TONE: Record<string, string> = {
  AVAILABLE: "success", UNAVAILABLE: "muted", DISABLED: "muted", ERROR: "error",
};

/* ------------------------------------------------------------------ */
/* helpers                                                             */
/* ------------------------------------------------------------------ */

function qs(params: Record<string, string | boolean | undefined | null>): string {
  const p = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === "" || v === false) continue;
    p.set(k, String(v));
  }
  const s = p.toString();
  return s ? `?${s}` : "";
}

/** Error text keeps the HTTP status (ApiError carries it) so viewers see *why*. */
function errText(e: any): string {
  const status = typeof e?.status === "number" ? e.status : null;
  const msg = String(e?.message ?? e ?? "request failed");
  return status != null && !msg.startsWith(`${status}`) ? `${status} — ${msg}` : msg;
}

/** GET with status-preserving errors (useFetch stores strings only). */
function kget(path: string): Promise<any> {
  return wsApi.get(path).catch((e: any) => {
    throw new Error(errText(e));
  });
}

/**
 * Tolerate the two honest response shapes (bare array / {items:[...]}) plus a few
 * plausible key aliases — anything else is reported as `unknown` instead of guessed.
 */
function listOf(d: any, keys: string[]): { rows: any[]; unknown: boolean } {
  if (d == null) return { rows: [], unknown: false };
  if (Array.isArray(d)) return { rows: d, unknown: false };
  if (typeof d === "object") {
    for (const k of keys) if (Array.isArray(d[k])) return { rows: d[k], unknown: false };
    return { rows: [], unknown: true };
  }
  return { rows: [], unknown: false };
}

/** effective_status(row): status unless ACTIVE → freshness band (contract §Foundation). */
function effectiveStatus(m: any): string {
  const explicit = typeof m?.effective_status === "string" ? m.effective_status.toUpperCase() : "";
  if (explicit) return explicit;
  const st = String(m?.status ?? "").toUpperCase();
  if (st && st !== "ACTIVE") return st;
  const band = String(m?.freshness ?? "").toUpperCase();
  if (band) return band;
  return st || "—";
}

function countOf(v: any): number {
  return Array.isArray(v) ? v.length : 0;
}

function fmtConf(v: any): string {
  return typeof v === "number" && isFinite(v) ? v.toFixed(2) : "—";
}

function promotedCount(r: any): number | null {
  if (Array.isArray(r)) return r.length;
  if (r && typeof r === "object") {
    if (Array.isArray(r.promoted)) return r.promoted.length;
    for (const k of ["promoted", "promoted_count", "count", "created"]) {
      if (typeof r[k] === "number") return r[k];
    }
    if (Array.isArray(r.items)) return r.items.length;
  }
  return null;
}

/** Honest unknown-shape card: show what came back instead of inventing rows. */
function ShapeNote({ what, data }: { what: string; data: any }) {
  let raw: string;
  try {
    raw = JSON.stringify(data);
  } catch {
    raw = String(data);
  }
  return (
    <Card style={{ borderColor: "var(--warn)" }}>
      <div className="text-[13.5px] font-medium" style={{ color: "var(--warn)" }}>
        Unexpected {what} response shape
      </div>
      <div className="text-[12.5px] mt-1" style={{ color: "var(--text-muted)" }}>
        The endpoint answered, but the payload does not match the documented contract — showing it
        as-is rather than guessing at rows.
      </div>
      <pre className="font-mono text-[11px] mt-2 overflow-x-auto whitespace-pre-wrap break-all"
        style={{ color: "var(--text-faint)" }}>{(raw ?? "").slice(0, 800)}</pre>
    </Card>
  );
}

/* ------------------------------------------------------------------ */
/* page                                                                */
/* ------------------------------------------------------------------ */

export default function Knowledge() {
  /* ---- tab ---- */
  const [tab, setTab] = useState<TabKey>("memory");

  /* ---- memory filters ---- */
  const [mType, setMType] = useState("");
  const [mStatus, setMStatus] = useState("");
  const [mQ, setMQ] = useState("");

  /* ---- graph filter ---- */
  const [nodeType, setNodeType] = useState("");

  /* ---- mutation state ---- */
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [promoted, setPromoted] = useState<number | null>(null);

  /* ---- sources: documents expand + register form ---- */
  const [openDoc, setOpenDoc] = useState<string | null>(null);
  const [regOpen, setRegOpen] = useState(false);
  const [regKind, setRegKind] = useState("local");
  const [regName, setRegName] = useState("");
  const [regConfig, setRegConfig] = useState("{}");

  /* ---- data (only the active tab fetches — same convention as Inbox) ---- */
  const memories = useFetch(
    () => (tab === "memory"
      ? kget(`/knowledge/memories${qs({ type: mType, status: mStatus, q: mQ, limit: "100" })}`)
      : Promise.resolve(null)),
    [tab, mType, mStatus, mQ]
  );
  const graph = useFetch(
    () => (tab === "graph"
      ? kget(`/knowledge/graph${qs({ node_type: nodeType, limit: "200" })}`)
      : Promise.resolve(null)),
    [tab, nodeType]
  );
  const sources = useFetch(
    () => (tab === "sources" ? kget("/knowledge/sources") : Promise.resolve(null)),
    [tab]
  );
  const signals = useFetch(
    () => (tab === "signals" ? kget("/knowledge/community-signals") : Promise.resolve(null)),
    [tab]
  );
  const docs = useFetch(
    () => (openDoc
      ? kget(`/knowledge/sources/${encodeURIComponent(openDoc)}/documents?limit=20`)
      : Promise.resolve(null)),
    [openDoc]
  );

  /* sources tab stays fresh — same conditional-poll convention as Inbox/Ugc */
  useInterval(() => {
    sources.reload();
    if (openDoc) docs.reload();
  }, tab === "sources" ? 30000 : null);

  /* ---- mutations ---- */

  async function call(key: string, fn: () => Promise<any>, ok?: string): Promise<any | null> {
    setBusy(key);
    setNotice(null);
    try {
      const r = await fn();
      if (ok) toast(ok, "success");
      return r;
    } catch (e: any) {
      const msg = errText(e);
      setNotice(msg);
      toast(msg, "error", "Knowledge");
      return null;
    } finally {
      setBusy("");
    }
  }

  function reloadActive() {
    if (tab === "memory") memories.reload();
    else if (tab === "graph") graph.reload();
    else if (tab === "sources") sources.reload();
    else signals.reload();
  }

  async function verify(m: any) {
    const r = await call("verify", () =>
      wsApi.post(`/knowledge/memories/${encodeURIComponent(m.id)}/verify`), "Memory verified");
    if (r) memories.reload();
  }

  async function disableMemory(m: any) {
    const r = await call("disable", () =>
      wsApi.post(`/knowledge/memories/${encodeURIComponent(m.id)}/disable`), "Memory disabled");
    if (r) memories.reload();
  }

  async function supersede(m: any) {
    const input = window.prompt(`Replacement memory id for ${m.id}:`, "");
    if (input == null) return;
    const replacementId = input.trim();
    if (!replacementId) {
      setNotice("Supersede needs a replacement memory id — nothing was changed.");
      return;
    }
    const r = await call("supersede", () =>
      wsApi.post(`/knowledge/memories/${encodeURIComponent(m.id)}/supersede`, { replacement_id: replacementId }),
      "Memory superseded");
    if (r) memories.reload();
  }

  async function syncSource(c: any) {
    const r = await call("sync", () =>
      wsApi.post(`/knowledge/sources/${encodeURIComponent(c.id)}/sync`), "Sync queued");
    if (r?.job_id) toast(`Job ${String(r.job_id).slice(0, 8)}`, "info", "Queued");
    sources.reload();
  }

  async function disconnectSource(c: any) {
    const r = await call("disconnect", () =>
      wsApi.post(`/knowledge/sources/${encodeURIComponent(c.id)}/disconnect`), "Connector disconnected");
    if (r) sources.reload();
  }

  async function registerConnector() {
    const name = regName.trim();
    if (!name) { setNotice("Connector name is required."); return; }
    let config: any;
    try {
      config = JSON.parse(regConfig.trim() || "{}");
    } catch {
      setNotice("Config must be valid JSON — fix the textarea and try again.");
      return;
    }
    if (!config || typeof config !== "object" || Array.isArray(config)) {
      setNotice("Config must be a JSON object, e.g. {\"path\": \"…\"}.");
      return;
    }
    const r = await call("register", () =>
      wsApi.post("/knowledge/sources", { kind: regKind, name, config }),
      "Connector registered");
    if (r) {
      setRegOpen(false);
      setRegName("");
      setRegConfig("{}");
      sources.reload();
    }
  }

  async function promote() {
    const r = await call("promote", () => wsApi.post("/knowledge/promote-insights", {}));
    if (!r) return;
    const n = promotedCount(r);
    setPromoted(n);
    if (n == null) {
      setNotice("Promotion finished, but the response carried no promoted count — check the Memory tab.");
    }
    signals.reload();
  }

  /* ---- derived ---- */

  const memShape = listOf(memories.data, ["items", "memories"]);
  const srcShape = listOf(sources.data, ["items", "connectors", "sources"]);
  const sigShape = listOf(signals.data, ["items", "insights", "signals"]);
  const docShape = listOf(docs.data, ["items", "documents"]);

  const gData: any = graph.data;
  const gNodes: any[] = Array.isArray(gData?.nodes) ? gData.nodes : [];
  const gEdges: any[] = Array.isArray(gData?.edges) ? gData.edges : [];
  const gShapeBad = !!gData && !(Array.isArray(gData.nodes) && Array.isArray(gData.edges));

  const nodeGroups: Record<string, any[]> = {};
  for (const n of gNodes) {
    const t = String(n?.node_type ?? "unknown");
    (nodeGroups[t] ||= []).push(n);
  }
  const groupOrder = [
    ...NODE_TYPES.filter((t) => nodeGroups[t]),
    ...Object.keys(nodeGroups).filter((t) => !NODE_TYPES.includes(t)).sort(),
  ];
  const labelById = new Map<string, string>();
  for (const n of gNodes) labelById.set(String(n?.id ?? ""), String(n?.label || n?.node_key || n?.id || ""));
  const edgeGroups: Record<string, any[]> = {};
  for (const e of gEdges) {
    const rel = String(e?.relationship ?? "UNKNOWN");
    (edgeGroups[rel] ||= []).push(e);
  }
  const relOrder = Object.keys(edgeGroups).sort();

  function edgeEnd(id: any): { text: string; known: boolean } {
    const s = String(id ?? "");
    const hit = labelById.get(s);
    if (hit) return { text: hit, known: true };
    return { text: s ? `${s.slice(0, 8)}…` : "(missing)", known: false };
  }

  const activeDocs = openDoc && docShape.rows.length ? docShape.rows : [];

  /* ------------------------------------------------------------------ */

  return (
    <div className="space-y-4">
      <PageHeader
        title="Knowledge"
        subtitle="Workspace memory, knowledge graph, source connectors and community signals — provenance-backed, never invented."
        actions={<button className="btn-outline !text-xs" onClick={reloadActive}>↻ Refresh</button>}
      />

      <Tabs tabs={TABS} active={tab} onChange={setTab} />

      {notice && (
        <Card style={{ borderColor: "var(--danger)", padding: 13 }}>
          <div className="flex items-start gap-2">
            <div className="flex-1 text-[13px] break-words" style={{ color: "var(--danger)" }}>{notice}</div>
            <button className="btn-ghost !px-2 !py-1 !text-xs" onClick={() => setNotice(null)}>✕</button>
          </div>
        </Card>
      )}

      {/* ================= Memory ================= */}
      {tab === "memory" && (
        <section className="space-y-3">
          <Card>
            <div className="flex gap-2 flex-wrap items-center">
              <div className="flex-1 min-w-[200px]">
                <SearchInput value={mQ} onChange={setMQ} placeholder="Search memory content / topic…" />
              </div>
              <select className="select !text-[12.5px] w-auto" value={mType}
                onChange={(e) => setMType(e.target.value)} aria-label="Filter by type">
                <option value="">Any type</option>
                {MEMORY_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
              <select className="select !text-[12.5px] w-auto" value={mStatus}
                onChange={(e) => setMStatus(e.target.value)} aria-label="Filter by status">
                <option value="">Any status</option>
                {MEMORY_STATUSES.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
              {(mType || mStatus || mQ) && (
                <button className="btn-outline !text-xs"
                  onClick={() => { setMType(""); setMStatus(""); setMQ(""); }}>Clear</button>
              )}
            </div>
          </Card>

          {memories.loading && !memories.data ? <Loading rows={6} />
            : memories.error && !memories.data ? <ErrorBox error={memories.error} onRetry={memories.reload} />
            : memShape.unknown ? <ShapeNote what="memories" data={memories.data} />
            : !memShape.rows.length ? (
              <Card><Empty title="No memories yet"
                hint="Typed workspace knowledge lands here from agents, promoted community signals and manual entries — each row carries its evidence."
                action={<button className="btn-outline !text-xs" onClick={memories.reload}>Refresh</button>} /></Card>
            ) : (
              <Card pad={false} className="overflow-x-auto">
                <table className="table">
                  <thead>
                    <tr>
                      <th>Type</th><th>Content</th><th>Confidence</th><th>Freshness</th>
                      <th>Status</th><th>Evidence</th><th>Last verified</th><th></th>
                    </tr>
                  </thead>
                  <tbody>
                    {memShape.rows.map((m: any) => {
                      const eff = effectiveStatus(m);
                      const ev = countOf(m.evidence_ids);
                      const src = countOf(m.source_ids);
                      return (
                        <tr key={String(m.id ?? Math.random())}>
                          <td className="whitespace-nowrap"><Badge tone="muted">{String(m.type ?? "—")}</Badge></td>
                          <td className="max-w-[340px]">
                            <div className="text-[13px] line-clamp-2" title={String(m.content ?? "")}>
                              {String(m.content ?? "—")}
                            </div>
                            <div className="text-[11.5px] mt-1 flex gap-2 flex-wrap" style={{ color: "var(--text-faint)" }}>
                              {m.topic ? <span>topic: {String(m.topic)}</span> : null}
                              {m.scope ? <span>scope: {String(m.scope)}</span> : null}
                              {m.platform ? <span>platform: {String(m.platform)}</span> : null}
                              {m.origin ? <span>origin: {String(m.origin)}</span> : null}
                            </div>
                            <div className="font-mono text-[10.5px] mt-0.5 break-all" style={{ color: "var(--text-faint)" }}>
                              {String(m.id ?? "")}
                            </div>
                          </td>
                          <td className="font-mono text-[12px]">{fmtConf(m.confidence)}</td>
                          <td><Badge tone="muted">{String(m.freshness ?? "—")}</Badge></td>
                          <td><Badge tone={EFF_TONE[eff] ?? "muted"}>{eff}</Badge></td>
                          <td className="font-mono text-[12px] whitespace-nowrap">
                            {ev} ev · {src} src
                          </td>
                          <td className="font-mono text-[12px] whitespace-nowrap"
                            style={{ color: "var(--text-muted)" }}>
                            {fmtAgo(m.last_verified_at ?? null)}
                          </td>
                          <td className="text-right whitespace-nowrap">
                            <div className="flex gap-1.5 justify-end">
                              <button className="btn-outline !text-xs" disabled={busy !== ""}
                                onClick={() => verify(m)}>Verify</button>
                              <ConfirmButton className="btn-danger !text-xs" confirmText="Disable?"
                                disabled={busy !== ""} onConfirm={() => disableMemory(m)}>
                                Disable
                              </ConfirmButton>
                              <button className="btn-outline !text-xs" disabled={busy !== ""}
                                onClick={() => supersede(m)}>Supersede</button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </Card>
            )}
        </section>
      )}

      {/* ================= Graph ================= */}
      {tab === "graph" && (
        <section className="space-y-3">
          <Card>
            <div className="flex gap-2 flex-wrap items-center">
              <select className="select !text-[12.5px] w-auto" value={nodeType}
                onChange={(e) => setNodeType(e.target.value)} aria-label="Filter by node type">
                <option value="">All node types</option>
                {NODE_TYPES.map((t) => <option key={t} value={t}>{t}</option>)}
              </select>
              <span className="text-[12px]" style={{ color: "var(--text-faint)" }}>
                {gNodes.length} nodes · {gEdges.length} edges
              </span>
              <span className="ml-auto">
                <button className="btn-outline !text-xs" onClick={graph.reload}>↻ Reload</button>
              </span>
            </div>
          </Card>

          {graph.loading && !graph.data ? <Loading rows={5} />
            : graph.error && !graph.data ? <ErrorBox error={graph.error} onRetry={graph.reload} />
            : gShapeBad ? <ShapeNote what="graph" data={graph.data} />
            : (
              <>
                {/* nodes grouped by node_type */}
                {!gNodes.length ? (
                  <Card><Empty title="No graph nodes yet"
                    hint="Nodes appear once content, campaigns, sources, topics and lessons are linked by the pipeline."
                    action={<button className="btn-outline !text-xs" onClick={graph.reload}>Refresh</button>} /></Card>
                ) : (
                  <div className="grid md:grid-cols-2 gap-3">
                    {groupOrder.map((t) => {
                      const rows = nodeGroups[t];
                      return (
                        <Card key={t} style={{ padding: 15 }}>
                          <div className="flex items-center gap-2 mb-2">
                            <Badge tone="info">{t}</Badge>
                            <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                              {rows.length} nodes
                            </span>
                          </div>
                          <ul className="space-y-1 text-[12.5px]" style={{ color: "var(--text-muted)" }}>
                            {rows.slice(0, 40).map((n: any, i: number) => (
                              <li key={String(n?.id ?? i)} className="truncate">
                                • {String(n?.label || n?.node_key || n?.id || "(unnamed)")}
                              </li>
                            ))}
                          </ul>
                          {rows.length > 40 && (
                            <div className="text-[11.5px] mt-1.5" style={{ color: "var(--text-faint)" }}>
                              … +{rows.length - 40} more (raise the limit to see them)
                            </div>
                          )}
                        </Card>
                      );
                    })}
                  </div>
                )}

                {/* edges grouped by relationship */}
                {!gEdges.length ? (
                  <Card><Empty title="No relationships yet"
                    hint="Edges show up when nodes are linked (content→topic, claim→source, experiment→lesson, …)." /></Card>
                ) : (
                  <Card>
                    <div className="panel-label mb-2">Relationships ({gEdges.length})</div>
                    <div className="space-y-4">
                      {relOrder.map((rel) => (
                        <div key={rel}>
                          <div className="flex items-center gap-2 mb-1.5">
                            <Badge tone="muted">{rel}</Badge>
                            <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                              {edgeGroups[rel].length}
                            </span>
                          </div>
                          <div className="space-y-1">
                            {edgeGroups[rel].slice(0, 60).map((e: any, i: number) => {
                              const from = edgeEnd(e?.from_node_id);
                              const to = edgeEnd(e?.to_node_id);
                              return (
                                <div key={String(e?.id ?? i)}
                                  className="font-mono text-[12px] break-words"
                                  style={{ color: "var(--text-muted)" }}>
                                  {from.text} <span style={{ color: "var(--accent-bright)" }}>--{rel}--&gt;</span> {to.text}
                                  {(!from.known || !to.known) && (
                                    <span style={{ color: "var(--text-faint)" }}> (endpoint outside this view)</span>
                                  )}
                                  {typeof e?.weight === "number" && (
                                    <span style={{ color: "var(--text-faint)" }}> · w={e.weight}</span>
                                  )}
                                </div>
                              );
                            })}
                            {edgeGroups[rel].length > 60 && (
                              <div className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>
                                … +{edgeGroups[rel].length - 60} more
                              </div>
                            )}
                          </div>
                        </div>
                      ))}
                    </div>
                  </Card>
                )}
              </>
            )}
        </section>
      )}

      {/* ================= Sources ================= */}
      {tab === "sources" && (
        <section className="space-y-3">
          <Card>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>
                Connectors feed documents into the knowledge graph. Secret config values are never shown —
                only whether credentials are present.
              </span>
              <span className="ml-auto flex gap-2">
                <button className="btn-outline !text-xs" onClick={sources.reload}>↻ Refresh</button>
                <button className="btn-primary !text-xs" onClick={() => { setRegOpen(true); setNotice(null); }}>
                  + Register connector
                </button>
              </span>
            </div>
          </Card>

          {sources.loading && !sources.data ? <Loading rows={4} />
            : sources.error && !sources.data ? <ErrorBox error={sources.error} onRetry={sources.reload} />
            : srcShape.unknown ? <ShapeNote what="sources" data={sources.data} />
            : !srcShape.rows.length ? (
              <Card><Empty title="No connectors yet"
                hint="Register a local folder, URL, RSS feed, YouTube channel or S3 bucket to pull documents in."
                action={<button className="btn-primary !text-xs" onClick={() => setRegOpen(true)}>Register connector</button>} /></Card>
            ) : (
              <div className="space-y-3">
                {srcShape.rows.map((c: any) => {
                  const st = String(c?.status ?? "").toUpperCase() || "—";
                  const open = openDoc === String(c?.id);
                  return (
                    <Card key={String(c?.id ?? Math.random())} style={{ padding: 15 }}>
                      <div className="flex items-center gap-2 flex-wrap">
                        <b className="text-[14px]">{String(c?.name ?? "unnamed connector")}</b>
                        <Badge tone="muted">{String(c?.kind ?? "—")}</Badge>
                        <Badge tone={CONNECTOR_TONE[st] ?? "muted"}>{st}</Badge>
                        <Badge tone={c?.has_credentials ? "success" : "muted"}>
                          {c?.has_credentials ? "credentials present" : "no credentials"}
                        </Badge>
                        <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                          {typeof c?.doc_count === "number" ? c.doc_count : 0} docs · last sync {fmtAgo(c?.last_sync_at ?? null)}
                        </span>
                      </div>

                      {c?.unavailable_reason && (
                        <div className="text-[12.5px] mt-2" style={{ color: "var(--warn)" }}>
                          Unavailable: {String(c.unavailable_reason)}
                        </div>
                      )}
                      {c?.last_error && (
                        <div className="text-[12.5px] mt-2 break-words" style={{ color: "var(--danger)" }}>
                          Last error: {String(c.last_error)}
                        </div>
                      )}

                      <div className="text-[11.5px] mt-2 flex gap-3 flex-wrap" style={{ color: "var(--text-faint)" }}>
                        <span>cursor:{" "}
                          {c?.last_cursor
                            ? <code className="font-mono break-all">{String(c.last_cursor).slice(0, 48)}{String(c.last_cursor).length > 48 ? "…" : ""}</code>
                            : "—"}
                        </span>
                        <span>enabled: {c?.enabled === false ? "no" : "yes"}</span>
                      </div>

                      <div className="flex gap-2 mt-3 flex-wrap">
                        <button className="btn-outline !text-xs" disabled={busy !== ""}
                          onClick={() => syncSource(c)}>
                          {busy === "sync" ? "Queueing…" : "⇄ Sync"}
                        </button>
                        <ConfirmButton className="btn-danger !text-xs" confirmText="Disconnect?"
                          disabled={busy !== ""} onConfirm={() => disconnectSource(c)}>
                          Disconnect
                        </ConfirmButton>
                        <button className="btn-ghost !text-xs"
                          onClick={() => setOpenDoc(open ? null : String(c?.id ?? ""))}>
                          {open ? "▾ Documents" : "▸ Documents"}
                        </button>
                      </div>

                      {open && (
                        <div className="mt-3 pt-3" style={{ borderTop: "var(--seam)" }}>
                          {docs.loading && !docs.data ? <Loading rows={3} />
                            : docs.error ? <ErrorBox error={docs.error} onRetry={docs.reload} />
                            : docShape.unknown ? <ShapeNote what="documents" data={docs.data} />
                            : !docShape.rows.length ? (
                              <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                                No documents synced from this connector yet — run Sync.
                              </div>
                            ) : (
                              <table className="table">
                                <thead>
                                  <tr><th>Title</th><th>MIME</th><th>State</th><th>Checksum</th><th>Updated</th></tr>
                                </thead>
                                <tbody>
                                  {activeDocs.map((d: any, i: number) => (
                                    <tr key={String(d?.id ?? i)}>
                                      <td className="max-w-[280px]">
                                        <div className="truncate" title={String(d?.title ?? "")}>
                                          {String(d?.title || "(untitled)")}
                                        </div>
                                      </td>
                                      <td className="font-mono text-[11.5px]">{String(d?.mime_type ?? "—")}</td>
                                      <td><Badge tone={d?.state === "deleted" ? "muted" : d?.state === "updated" ? "info" : "success"}>{String(d?.state ?? "—")}</Badge></td>
                                      <td className="font-mono text-[11.5px]">
                                        {d?.checksum ? `${String(d.checksum).slice(0, 10)}…` : "—"}
                                      </td>
                                      <td className="font-mono text-[11.5px]" style={{ color: "var(--text-muted)" }}>
                                        {fmtAgo(d?.remote_updated_at ?? d?.updated_at ?? null)}
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            )}
                        </div>
                      )}
                    </Card>
                  );
                })}
              </div>
            )}
        </section>
      )}

      {/* ================= Community Signals ================= */}
      {tab === "signals" && (
        <section className="space-y-3">
          <Card>
            <div className="flex items-center gap-2 flex-wrap">
              <span className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>
                Repeated community themes, with interaction evidence. Promotion stores only insights that
                meet the evidence threshold.
              </span>
              <span className="ml-auto flex gap-2">
                <button className="btn-outline !text-xs" onClick={signals.reload}>↻ Refresh</button>
                <button className="btn-primary !text-xs" disabled={busy !== ""} onClick={promote}>
                  {busy === "promote" ? "Promoting…" : "↑ Promote to memory"}
                </button>
              </span>
            </div>
          </Card>

          {promoted != null && (
            <Card style={{ borderColor: "var(--accent)", padding: 13 }}>
              <div className="text-[13px]" style={{ color: "var(--accent-bright)" }}>
                Promoted {promoted} insight{promoted === 1 ? "" : "s"} to memory — open the Memory tab to review them.
              </div>
            </Card>
          )}

          {signals.loading && !signals.data ? <Loading rows={4} />
            : signals.error && !signals.data ? <ErrorBox error={signals.error} onRetry={signals.reload} />
            : sigShape.unknown ? <ShapeNote what="community-signals" data={signals.data} />
            : !sigShape.rows.length ? (
              <Card><Empty title="No community signals yet"
                hint="Signals appear once repeated questions, requests and themes cross the interaction threshold."
                action={<button className="btn-outline !text-xs" onClick={signals.reload}>Refresh</button>} /></Card>
            ) : (
              <div className="grid md:grid-cols-2 gap-3">
                {sigShape.rows.map((s: any, i: number) => {
                  const meets = s?.meets_threshold;
                  const count = typeof s?.interaction_count === "number" ? s.interaction_count
                    : typeof s?.evidence_count === "number" ? s.evidence_count
                    : typeof s?.count === "number" ? s.count : null;
                  const followup = String(
                    s?.suggested_followup ?? s?.followup ?? s?.suggestion ?? s?.suggested ?? ""
                  );
                  return (
                    <Card key={String(s?.id ?? i)} style={{ padding: 15 }}>
                      <div className="flex items-center gap-2 flex-wrap">
                        <Badge tone={meets === true ? "success" : meets === false ? "warning" : "muted"}>
                          {meets === true ? "meets threshold" : meets === false ? "below threshold" : "threshold n/a"}
                        </Badge>
                        {typeof s?.confidence === "number" && (
                          <Badge tone="info">confidence {fmtConf(s.confidence)}</Badge>
                        )}
                        {count != null && (
                          <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                            {count} interactions
                          </span>
                        )}
                      </div>
                      <div className="text-[14px] font-semibold mt-2">{String(s?.topic ?? s?.topic_key ?? "Untitled topic")}</div>
                      {followup && (
                        <div className="text-[12.5px] mt-1" style={{ color: "var(--text-muted)" }}>
                          {followup}
                        </div>
                      )}
                      <div className="flex gap-2 mt-2 flex-wrap text-[11.5px]" style={{ color: "var(--text-faint)" }}>
                        {s?.freshness ? <Badge tone="muted">{String(s.freshness)}</Badge> : null}
                        {s?.scope ? <span>scope: {String(s.scope)}</span> : null}
                        {s?.platform ? <span>platform: {String(s.platform)}</span> : null}
                        {s?.platforms && Array.isArray(s.platforms) && s.platforms.length
                          ? <span>platforms: {s.platforms.map((p: any) => String(p)).join(", ")}</span> : null}
                      </div>
                    </Card>
                  );
                })}
              </div>
            )}
        </section>
      )}

      {/* ---- register connector (admin — enforced server-side) ---- */}
      <Modal open={regOpen} onClose={() => setRegOpen(false)} title="Register connector">
        <Field label="Kind" hint="local / url / rss / youtube work today; cloud drives need credentials and report UNAVAILABLE until they exist.">
          <select className="select" value={regKind} onChange={(e) => setRegKind(e.target.value)}>
            {CONNECTOR_KINDS.map((k) => <option key={k} value={k}>{k}</option>)}
          </select>
        </Field>
        <Field label="Name" hint="Unique per kind in this workspace.">
          <input className="input" value={regName} placeholder="blog-rss"
            onChange={(e) => setRegName(e.target.value)} />
        </Field>
        <Field label="Config (JSON)" hint='e.g. {"url": "https://…/feed.xml"} — stored server-side, secrets are never echoed back by the API.'>
          <textarea className="textarea !text-[12.5px] min-h-[120px] font-mono" value={regConfig}
            onChange={(e) => setRegConfig(e.target.value)} spellCheck={false} />
        </Field>
        <div className="flex justify-end gap-2 mt-4">
          <button className="btn-ghost !text-xs" onClick={() => setRegOpen(false)}>Cancel</button>
          <button className="btn-primary !text-xs" disabled={busy !== ""} onClick={registerConnector}>
            {busy === "register" ? "Registering…" : "Register"}
          </button>
        </div>
      </Modal>
    </div>
  );
}

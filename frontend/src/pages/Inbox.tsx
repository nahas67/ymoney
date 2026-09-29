/* Inbox — Work 09 Lane D: unified social inbox over backend/app/api/v1/inbox.py.
 *
 * Four panes: Platforms+Filters | Conversations/Interactions | Interaction
 * detail (AI classification, suggested reply editor, brand check, actions) |
 * plus the analytics KPI strip and Opportunities/Insights tabs on top.
 *
 * Mirrors the Work 08 Brands page: wsApi (workspace-scoped mount), useFetch
 * with deps + reload-after-mutation, shared ui.tsx primitives and design
 * tokens only — no new dependencies.
 */
import { useEffect, useMemo, useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch, useInterval } from "../hooks/hooks";
import {
  Avatar, Badge, Card, ConfirmButton, Empty, ErrorBox, Loading, PageHeader,
  SearchInput, Stat, Tabs, toast,
} from "../components/ui";
import { fmtAgo, platformLabel, statusTone } from "../lib/format";

type TabKey = "inbox" | "opportunities" | "insights";
type ListMode = "conversations" | "interactions";

const TABS: { key: TabKey; label: string }[] = [
  { key: "inbox", label: "Inbox" },
  { key: "opportunities", label: "Opportunities" },
  { key: "insights", label: "Insights" },
];

const STATUS_OPTIONS = [
  "unread", "read", "classified", "drafted", "replied", "escalated", "ignored", "spam",
];
const PRIORITY_OPTIONS = ["low", "normal", "high", "urgent"];
const BULK_CAP = 100; // server rejects >100 ids with 422 — enforce it here too

const PRIORITY_TONE: Record<string, string> = {
  urgent: "error", high: "warning", normal: "info", low: "muted",
};
const BRAND_TONE: Record<string, string> = {
  PASS: "success", PASS_WITH_WARNINGS: "warning", WARN: "warning",
  REVIEW_REQUIRED: "info", FAIL: "error",
};
const BADGE_TONE: Record<string, string> = {
  "AI DRAFT": "info", "HUMAN EDITED": "warning", "AUTO SENT": "success", SENT: "success",
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

/** Classification entries are {label, confidence, provider, evidence} or plain strings. */
function labelOf(c: any): string {
  return typeof c === "string" ? c : String(c?.label ?? c?.name ?? "");
}
function confOf(c: any): number | null {
  const v = typeof c === "object" && c ? c.confidence : null;
  return typeof v === "number" ? v : null;
}
function firstText(v: any): string {
  if (Array.isArray(v)) return v.map((x) => (typeof x === "string" ? x : JSON.stringify(x))).join(", ");
  if (v && typeof v === "object") return Object.entries(v).map(([k, val]) => `${k}: ${val}`).join(", ");
  return String(v ?? "");
}

/* ------------------------------------------------------------------ */
/* page                                                                */
/* ------------------------------------------------------------------ */

export default function Inbox() {
  /* ---- filters ---- */
  const [platform, setPlatform] = useState("");
  const [status, setStatus] = useState("");
  const [classification, setClassification] = useState("");
  const [priority, setPriority] = useState("");
  const [unreadOnly, setUnreadOnly] = useState(false);
  const [search, setSearch] = useState("");
  const [tab, setTab] = useState<TabKey>("inbox");
  const [mode, setMode] = useState<ListMode>("conversations");

  /* ---- selection ---- */
  const [convId, setConvId] = useState<string | null>(null);
  const [interactionId, setInteractionId] = useState<string | null>(null);
  const [picked, setPicked] = useState<Set<string>>(new Set());
  const [bulkMode, setBulkMode] = useState(false);

  /* ---- mutation state ---- */
  const [busy, setBusy] = useState("");
  const [notice, setNotice] = useState<string | null>(null);
  const [replyText, setReplyText] = useState("");
  const [replyTouched, setReplyTouched] = useState(false);

  /* ---- data ---- */
  const platforms = useFetch(() => wsApi.get("/inbox/platforms"), []);
  const autonomy = useFetch(() => wsApi.get("/inbox/autonomy"), []);
  const analytics = useFetch(() => wsApi.get("/inbox/analytics"), []);
  const conversations = useFetch(
    () => wsApi.get(`/inbox/conversations${qs({ platform, unread: unreadOnly ? true : undefined })}`),
    [platform, unreadOnly]
  );
  const interactions = useFetch(
    () => (mode === "interactions"
      ? wsApi.get(`/inbox/interactions${qs({
          platform, status, priority, classification, search,
          unread: unreadOnly ? true : undefined, limit: "50",
        })}`)
      : Promise.resolve(null)),
    [mode, platform, status, priority, classification, search, unreadOnly]
  );
  const conversation = useFetch(
    () => (convId && !interactionId ? wsApi.get(`/inbox/conversations/${convId}`) : Promise.resolve(null)),
    [convId, interactionId]
  );
  const detail = useFetch(
    () => (interactionId ? wsApi.get(`/inbox/interactions/${interactionId}`) : Promise.resolve(null)),
    [interactionId]
  );
  const opportunities = useFetch(
    () => (tab === "opportunities" ? wsApi.get("/inbox/opportunities") : Promise.resolve(null)),
    [tab]
  );
  const insights = useFetch(
    () => (tab === "insights" ? wsApi.get("/inbox/insights") : Promise.resolve(null)),
    [tab]
  );

  /* keep the list fresh — same conditional-poll convention as Ugc/Localization */
  useInterval(() => {
    conversations.reload();
    if (mode === "interactions") interactions.reload();
  }, tab === "inbox" ? 30000 : null);

  const convList: any[] = conversations.data?.items ?? [];
  const ixList: any[] = mode === "interactions" ? (interactions.data?.items ?? []) : [];
  const platformsList: any[] = platforms.data?.platforms ?? [];
  const observed: string[] = platforms.data?.observed ?? [];
  const classOptions: string[] = autonomy.data?.classes ?? [];
  const autonomyMode: string = autonomy.data?.autonomy?.mode ?? "—";

  const detailData: any = detail.data ?? null;
  const current: any = detailData?.interaction ?? null;
  const thread: any[] = detailData?.thread ?? [];
  const actions: any[] = detailData?.actions ?? [];
  const latestAction: any = actions[0] ?? null;
  const convThread: any[] = conversation.data?.interactions ?? [];
  const convMeta: any = conversation.data?.conversation ?? null;

  /* prefill the editor when the action changes (never clobber typing) */
  useEffect(() => {
    if (!latestAction) return;
    setReplyText(latestAction.final_text || latestAction.draft_text || "");
    setReplyTouched(false);
    setNotice(null);
  }, [latestAction?.id]); // eslint-disable-line react-hooks/exhaustive-deps

  const platformOptions = useMemo(() => {
    const keys = new Set<string>(observed);
    for (const p of platformsList) {
      const k = p?.platform ?? p?.key ?? p?.name;
      if (typeof k === "string" && k) keys.add(k);
    }
    return [...keys].sort();
  }, [observed, platformsList]);

  /* ---- mutations ---- */

  async function call(key: string, fn: () => Promise<any>, ok?: string): Promise<any | null> {
    setBusy(key);
    setNotice(null);
    try {
      const r = await fn();
      if (ok) toast(ok, "success");
      return r;
    } catch (e: any) {
      const msg = String(e?.message ?? e);
      setNotice(msg);
      toast(msg, "error", "Inbox");
      return null;
    } finally {
      setBusy("");
    }
  }

  function reloadLists() {
    conversations.reload();
    if (mode === "interactions") interactions.reload();
    analytics.reload();
  }

  async function sync() {
    const r = await call("sync", () => wsApi.post("/inbox/sync", {}), "Sync queued");
    if (r?.job_id) toast(`Job ${String(r.job_id).slice(0, 8)} · ${r.type}`, "info", "Queued");
    reloadLists();
  }

  async function toggleRead(row: any) {
    const next = row.status === "unread";
    const r = await call("read", () =>
      wsApi.post(`/inbox/interactions/${row.id}/read`, { read: next }));
    if (r) reloadLists();
    if (interactionId === row.id) detail.reload();
  }

  async function classify() {
    if (!current) return;
    const r = await call("classify", () =>
      wsApi.post(`/inbox/interactions/${current.id}/classify`, {}), "Classified");
    if (r) { detail.reload(); interactions.reload(); }
  }

  async function draft(regenerate: boolean) {
    if (!current) return;
    const r = await call("draft", () =>
      wsApi.post(`/inbox/interactions/${current.id}/draft`, { regenerate }));
    if (r?.action) {
      setReplyText(r.action.final_text || r.action.draft_text || "");
      setReplyTouched(false);
      detail.reload();
    }
  }

  async function queueReply(send: boolean) {
    if (!current?.conversation_id) {
      setNotice("This interaction has no conversation to reply into.");
      return;
    }
    const text = replyText.trim();
    if (!text) { setNotice("Reply text must not be empty."); return; }
    const r = await call("reply", () =>
      wsApi.post(`/inbox/conversations/${current.conversation_id}/reply`, {
        text, send, interaction_id: current.id,
      }), send ? "Reply sent" : "Draft queued");
    if (r) { detail.reload(); reloadLists(); }
  }

  async function sendEdited() {
    if (!latestAction) return;
    const text = replyText.trim();
    if (!text) { setNotice("Send text must not be empty."); return; }
    const r = await call("send", () =>
      wsApi.post(`/inbox/actions/${latestAction.id}/send`, { text }),
      "Reply sent");
    if (r) { detail.reload(); reloadLists(); }
  }

  async function decide(action: any, approve: boolean) {
    const r = await call("decide", () =>
      wsApi.post(`/inbox/actions/${action.id}/${approve ? "approve" : "reject"}`, {}),
      approve ? "Action approved" : "Action rejected");
    if (r) { detail.reload(); reloadLists(); }
  }

  function togglePick(id: string) {
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) { next.delete(id); return next; }
      if (next.size >= BULK_CAP) {
        toast(`Bulk selection is capped at ${BULK_CAP} rows`, "warning", "Cap reached");
        return prev;
      }
      next.add(id);
      return next;
    });
  }

  async function bulk(action: "read" | "unread" | "spam" | "ignore") {
    if (!picked.size) return;
    const r = await call("bulk", () =>
      wsApi.post("/inbox/bulk", { ids: [...picked], action }),
      `${action.toUpperCase()} applied to ${picked.size}`);
    if (r) { setPicked(new Set()); reloadLists(); }
  }

  function openConversation(id: string) {
    setConvId(id);
    setInteractionId(null);
    setNotice(null);
  }

  function openInteraction(id: string) {
    setInteractionId(id);
    setNotice(null);
  }

  function clearFilters() {
    setPlatform(""); setStatus(""); setClassification(""); setPriority("");
    setUnreadOnly(false); setSearch(""); setMode("conversations");
  }

  /* auto-flip to the interaction list when a filter only that view can serve */
  useEffect(() => {
    if (search || classification || status || priority) setMode("interactions");
    else if (mode === "interactions" && !search && !classification && !status && !priority) {
      // leave the user's manual choice alone; only flip *to* interactions above
    }
  }, [search, classification, status, priority]); // eslint-disable-line react-hooks/exhaustive-deps

  /* ---- analytics KPI strip ---- */
  const metrics: Record<string, any> = analytics.data?.metrics ?? {};
  const kpiCards = useMemo(() => {
    const out: { label: string; value: any; hint?: string }[] = [];
    for (const [key, val] of Object.entries(metrics)) {
      if (typeof val !== "number") continue;
      if (key === "window_days") continue;
      if (out.length >= 6) break;
      out.push({
        label: key.replace(/_/g, " "),
        value: key.endsWith("_rate") ? `${Math.round(val * 100)}%` : val,
        hint: key === "window_days" ? undefined : `last ${metrics.window_days ?? 30} days`,
      });
    }
    return out;
  }, [metrics]);

  const listLoading = mode === "interactions" ? interactions.loading : conversations.loading;
  const listError = mode === "interactions" ? interactions.error : conversations.error;
  const listReload = mode === "interactions" ? interactions.reload : conversations.reload;

  /* ------------------------------------------------------------------ */

  return (
    <div className="space-y-4">
      <PageHeader
        title="Inbox"
        subtitle="Comments, mentions, messages and reviews — classify, draft, approve and reply from one desk."
        actions={
          <>
            <button className="btn-outline !text-xs" onClick={reloadLists}>↻ Refresh</button>
            <button className="btn-primary !text-xs" disabled={busy === "sync"} onClick={sync}>
              {busy === "sync" ? "Queueing…" : "⇄ Sync now"}
            </button>
          </>
        }
      />

      {/* -------- analytics KPI strip -------- */}
      {analytics.loading && !analytics.data ? (
        <Loading rows={1} />
      ) : analytics.data?.available === false ? (
        <Card style={{ padding: 13 }}>
          <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
            Community engine not available — KPIs appear once metrics land.
            {analytics.data?.error ? ` (${analytics.data.error})` : ""}
          </div>
        </Card>
      ) : kpiCards.length ? (
        <div className="grid grid-cols-2 md:grid-cols-3 xl:grid-cols-6 gap-3">
          {kpiCards.map((k) => (
            <Stat key={k.label} label={k.label} value={k.value} hint={k.hint} />
          ))}
        </div>
      ) : null}

      <Tabs tabs={TABS} active={tab} onChange={setTab} />

      {/* ================= Opportunities ================= */}
      {tab === "opportunities" && (
        <section className="space-y-3">
          {opportunities.loading && !opportunities.data ? <Loading /> : opportunities.error ? (
            <ErrorBox error={opportunities.error} onRetry={opportunities.reload} />
          ) : !(opportunities.data?.items ?? []).length ? (
            <Card><Empty title="No opportunities yet"
              hint="Leads, partnerships and repeated content requests surface here once the community engine detects them."
              action={<button className="btn-outline !text-xs" onClick={opportunities.reload}>Refresh</button>} /></Card>
          ) : (
            <div className="grid md:grid-cols-2 gap-3">
              {(opportunities.data.items as any[]).map((o) => (
                <Card key={o.id} style={{ padding: 15 }}>
                  <div className="flex items-center gap-2 flex-wrap">
                    <Badge tone={o.state === "converted" ? "success" : o.state === "dismissed" ? "muted" : "info"}>
                      {o.opportunity_type?.replace(/_/g, " ")}
                    </Badge>
                    <Badge tone="muted">{o.state}</Badge>
                    <Badge tone={o.confidence === "high" ? "success" : o.confidence === "medium" ? "warning" : "muted"}>
                      {o.confidence} confidence
                    </Badge>
                    <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                      {o.evidence_count} signals
                    </span>
                  </div>
                  <div className="text-[14px] font-semibold mt-2">{o.title || "Untitled signal"}</div>
                  {o.detail && <div className="text-[12.5px] mt-1" style={{ color: "var(--text-muted)" }}>{o.detail}</div>}
                  <div className="flex gap-2 mt-3 flex-wrap">
                    <button className="btn-outline !text-xs" disabled={busy === "opp" || o.state !== "open"}
                      onClick={async () => {
                        const r = await call("opp", () =>
                          wsApi.post(`/inbox/opportunities/${o.id}/convert`, {}),
                          "Opportunity converted");
                        if (r) opportunities.reload();
                      }}>
                      Convert to idea
                    </button>
                    <ConfirmButton className="btn-danger !text-xs" confirmText="Dismiss it?"
                      disabled={busy === "opp" || o.state === "dismissed"}
                      onConfirm={async () => {
                        const r = await call("opp", () =>
                          wsApi.post(`/inbox/opportunities/${o.id}/dismiss`, {}),
                          "Opportunity dismissed");
                        if (r) opportunities.reload();
                      }}>
                      Dismiss
                    </ConfirmButton>
                  </div>
                </Card>
              ))}
            </div>
          )}
        </section>
      )}

      {/* ================= Insights ================= */}
      {tab === "insights" && (
        <section className="space-y-3">
          {insights.loading && !insights.data ? <Loading /> : insights.error ? (
            <ErrorBox error={insights.error} onRetry={insights.reload} />
          ) : (
            <>
              {insights.data?.suggestions_available === false && (
                <Card style={{ padding: 13 }}>
                  <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                    Content suggestions are unavailable until the community engine lands.
                  </div>
                </Card>
              )}
              {(insights.data?.suggestions ?? []).length > 0 && (
                <Card>
                  <div className="panel-label mb-2">Content suggestions</div>
                  <ul className="space-y-1.5 text-[13px]" style={{ color: "var(--text-muted)" }}>
                    {(insights.data.suggestions as any[]).map((s, i) => (
                      <li key={i}>• {typeof s === "string" ? s : (s?.title ?? s?.suggestion ?? JSON.stringify(s))}</li>
                    ))}
                  </ul>
                </Card>
              )}
              {!(insights.data?.items ?? []).length ? (
                <Card><Empty title="No insights yet"
                  hint="Repeated questions and topics aggregate here once the engine has enough evidence."
                  action={<button className="btn-outline !text-xs" onClick={insights.reload}>Refresh</button>} /></Card>
              ) : (
                <div className="grid md:grid-cols-2 gap-3">
                  {(insights.data.items as any[]).map((ins) => (
                    <Card key={ins.id} style={{ padding: 15 }}>
                      <div className="flex items-center gap-2 flex-wrap">
                        <Badge tone={statusTone(ins.state)}>{ins.state}</Badge>
                        <Badge tone={ins.confidence === "high" ? "success" : "muted"}>{ins.confidence}</Badge>
                        <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                          {ins.evidence_count}× evidence
                        </span>
                      </div>
                      <div className="text-[14px] font-semibold mt-2">{ins.topic || ins.topic_key}</div>
                      {ins.representative_text && (
                        <div className="text-[12.5px] mt-1 line-clamp-3" style={{ color: "var(--text-muted)" }}>
                          “{ins.representative_text}”
                        </div>
                      )}
                      {!!(ins.platforms ?? []).length && (
                        <div className="flex gap-1.5 mt-2 flex-wrap">
                          {ins.platforms.map((p: string) => <Badge key={p} tone="muted">{platformLabel(p)}</Badge>)}
                        </div>
                      )}
                    </Card>
                  ))}
                </div>
              )}
            </>
          )}
        </section>
      )}

      {/* ================= 4-pane inbox ================= */}
      {tab === "inbox" && (
        <div className="grid lg:grid-cols-[220px_minmax(260px,330px)_1fr] gap-4 items-start">

          {/* -------- pane 1: platforms + filters -------- */}
          <Card>
            <div className="panel-label mb-1.5">Platforms</div>
            <div className="flex flex-wrap gap-1.5">
              <button className={`chip !py-1 !text-[11.5px] ${!platform ? "on" : ""}`}
                onClick={() => setPlatform("")}>All</button>
              {platformOptions.map((p) => (
                <button key={p} className={`chip !py-1 !text-[11.5px] ${platform === p ? "on" : ""}`}
                  onClick={() => setPlatform(platform === p ? "" : p)}>
                  {platformLabel(p)}
                </button>
              ))}
              {!platformOptions.length && (
                <span className="text-[11.5px]" style={{ color: "var(--text-faint)" }}>
                  {platforms.loading ? "Loading…" : (platforms.data?.error ?? "No platforms yet")}
                </span>
              )}
            </div>
            {platforms.data?.available === false && (
              <div className="text-[11px] mt-2" style={{ color: "var(--text-faint)" }}>
                Registry unavailable: {platforms.data.error}
              </div>
            )}

            <div className="panel-label mt-4 mb-1.5">Filters</div>
            <div className="space-y-2">
              <SearchInput value={search} onChange={setSearch} placeholder="Search text or author…" />
              <select className="select !text-[12.5px]" value={status} onChange={(e) => setStatus(e.target.value)}>
                <option value="">Any status</option>
                {STATUS_OPTIONS.map((s) => <option key={s} value={s}>{s}</option>)}
              </select>
              <select className="select !text-[12.5px]" value={classification}
                onChange={(e) => setClassification(e.target.value)}>
                <option value="">Any classification</option>
                {classOptions.map((c) => <option key={c} value={c}>{c}</option>)}
              </select>
              <select className="select !text-[12.5px]" value={priority} onChange={(e) => setPriority(e.target.value)}>
                <option value="">Any priority</option>
                {PRIORITY_OPTIONS.map((p) => <option key={p} value={p}>{p}</option>)}
              </select>
              <button className={`chip !py-1 !text-[11.5px] w-full justify-center ${unreadOnly ? "on" : ""}`}
                onClick={() => setUnreadOnly(!unreadOnly)}>
                {unreadOnly ? "● Unread only" : "○ Unread only"}
              </button>
              <div className="flex gap-1.5">
                <button className="btn-outline !text-xs flex-1" onClick={clearFilters}>Clear</button>
                <button className={`btn-outline !text-xs flex-1 ${bulkMode ? "!border-[var(--accent)] !text-[var(--accent-bright)]" : ""}`}
                  onClick={() => { setBulkMode(!bulkMode); setPicked(new Set()); }}>
                  {bulkMode ? "Bulk on" : "Bulk select"}
                </button>
              </div>
            </div>

            <div className="mt-4 pt-3" style={{ borderTop: "var(--seam)" }}>
              <div className="panel-label mb-1.5">Autonomy</div>
              <Badge tone={autonomyMode === "DISABLED" ? "muted" : autonomyMode === "DRAFT_ONLY" ? "info" : "warning"}>
                {autonomyMode}
              </Badge>
              <div className="text-[11px] mt-1.5" style={{ color: "var(--text-faint)" }}>
                Replies stay drafts until the mode allows auto-send.
              </div>
            </div>

            {bulkMode && (
              <div className="mt-3 pt-3 space-y-1.5" style={{ borderTop: "var(--seam)" }}>
                <div className="panel-label">Bulk ({picked.size}/{BULK_CAP})</div>
                <div className="grid grid-cols-2 gap-1.5">
                  <button className="btn-outline !text-xs" disabled={!picked.size || busy === "bulk"}
                    onClick={() => bulk("read")}>Read</button>
                  <button className="btn-outline !text-xs" disabled={!picked.size || busy === "bulk"}
                    onClick={() => bulk("unread")}>Unread</button>
                  <ConfirmButton className="btn-danger !text-xs" confirmText="Mark spam?"
                    disabled={!picked.size || busy === "bulk"} onConfirm={() => bulk("spam")}>
                    Spam
                  </ConfirmButton>
                  <ConfirmButton className="btn-danger !text-xs" confirmText="Ignore?"
                    disabled={!picked.size || busy === "bulk"} onConfirm={() => bulk("ignore")}>
                    Ignore
                  </ConfirmButton>
                </div>
              </div>
            )}
          </Card>

          {/* -------- pane 2: conversations / interactions -------- */}
          <Card pad={false} style={{ overflow: "hidden" }}>
            <div className="flex items-center gap-1.5 px-3.5 py-3" style={{ borderBottom: "var(--seam)" }}>
              <button className={`chip !py-1 !text-[11.5px] ${mode === "conversations" ? "on" : ""}`}
                onClick={() => setMode("conversations")}>
                Conversations {convList.length ? <span className="font-mono opacity-70">{convList.length}</span> : null}
              </button>
              <button className={`chip !py-1 !text-[11.5px] ${mode === "interactions" ? "on" : ""}`}
                onClick={() => setMode("interactions")}>
                Interactions {ixList.length ? <span className="font-mono opacity-70">{ixList.length}</span> : null}
              </button>
              <span className="ml-auto">
                <button className="btn-ghost !px-2 !py-1 !text-[11px]" onClick={listReload} title="Refresh list">↻</button>
              </span>
            </div>

            <div className="max-h-[62vh] overflow-y-auto">
              {listLoading && !convList.length && !ixList.length ? <div className="p-3"><Loading rows={4} /></div>
                : listError && !convList.length && !ixList.length ? <div className="p-3"><ErrorBox error={listError} onRetry={listReload} /></div>
                : mode === "conversations" ? (
                  !convList.length ? (
                    <div className="p-4"><Empty title="No conversations"
                      hint="Connect a platform with comment-read access and hit Sync — threads land here." /></div>
                  ) : convList.map((c) => (
                    <button key={c.id} onClick={() => openConversation(c.id)}
                      className="w-full text-left px-3.5 py-3 flex gap-2.5"
                      style={{
                        borderBottom: "var(--seam)",
                        ...(convId === c.id && !interactionId
                          ? { background: "var(--accent-dim)" }
                          : {}),
                      }}>
                      <Avatar name={c.participant_name || c.title || "??"} size={30} />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5">
                          <span className="text-[13px] font-semibold truncate">
                            {c.participant_name || c.title || "Conversation"}
                          </span>
                          <Badge tone="muted">{platformLabel(c.platform)}</Badge>
                          {c.priority && c.priority !== "normal" && (
                            <Badge tone={PRIORITY_TONE[c.priority] ?? "muted"}>{c.priority}</Badge>
                          )}
                          {!!c.unread_count && (
                            <span className="ml-auto badge" style={{
                              background: "var(--accent-dim)", color: "var(--accent-bright)",
                            }}>{c.unread_count}</span>
                          )}
                        </div>
                        <div className="text-[12.5px] truncate" style={{ color: "var(--text-muted)" }}>
                          {c.last_interaction?.text || "—"}
                        </div>
                        <div className="font-mono text-[10.5px]" style={{ color: "var(--text-faint)" }}>
                          {fmtAgo(c.last_interaction?.created_at || c.last_interaction_at)}
                        </div>
                      </div>
                    </button>
                  ))
                ) : !ixList.length ? (
                  <div className="p-4"><Empty title="Nothing matches"
                    hint="Loosen the platform, status or search filters." /></div>
                ) : ixList.map((row) => (
                  <div key={row.id} className="flex items-start gap-2 px-3.5 py-3"
                    style={{ borderBottom: "var(--seam)", ...(interactionId === row.id ? { background: "var(--accent-dim)" } : {}) }}>
                    {bulkMode && (
                      <input type="checkbox" className="mt-1" checked={picked.has(row.id)}
                        onChange={() => togglePick(row.id)} aria-label={`Select ${row.id}`} />
                    )}
                    <button className="text-left min-w-0 flex-1" onClick={() => openInteraction(row.id)}>
                      <div className="flex items-center gap-1.5 flex-wrap">
                        {row.status === "unread" && (
                          <span className="w-1.5 h-1.5 rounded-full inline-block"
                            style={{ background: "var(--accent)" }} />
                        )}
                        <span className="text-[13px] font-semibold truncate">{row.author_name || "Unknown"}</span>
                        <Badge tone="muted">{platformLabel(row.platform)}</Badge>
                        <Badge tone={statusTone(row.status)}>{row.status}</Badge>
                        <Badge tone={PRIORITY_TONE[row.priority] ?? "muted"}>{row.priority}</Badge>
                      </div>
                      <div className="text-[12.5px] mt-1 line-clamp-2" style={{ color: "var(--text-muted)" }}>
                        {row.text || "—"}
                      </div>
                      <div className="flex gap-1 mt-1 flex-wrap">
                        {(row.classifications ?? []).map((c: any, i: number) => (
                          <span key={`${labelOf(c)}-${i}`} className="badge"
                            style={{ background: "var(--info-dim)", color: "var(--info)" }}>
                            {labelOf(c)}
                          </span>
                        ))}
                        <span className="font-mono text-[10.5px] self-center" style={{ color: "var(--text-faint)" }}>
                          {fmtAgo(row.created_at)}
                        </span>
                      </div>
                    </button>
                  </div>
                ))}
            </div>
          </Card>

          {/* -------- pane 3: interaction detail -------- */}
          <div className="space-y-4">
            {notice && (
              <Card style={{ borderColor: "var(--danger)", padding: 13 }}>
                <div className="flex items-start gap-2">
                  <div className="flex-1 text-[13px]" style={{ color: "var(--danger)" }}>
                    {notice}
                  </div>
                  <button className="btn-ghost !px-2 !py-1 !text-xs" onClick={() => setNotice(null)}>✕</button>
                </div>
              </Card>
            )}

            {!interactionId && !convId && (
              <Card><Empty title="Pick a conversation"
                hint="Select a thread on the left — its messages, classification, draft and brand check open here." /></Card>
            )}

            {/* conversation thread (no specific interaction selected) */}
            {!interactionId && convId && (
              <Card>
                <div className="flex items-center gap-2 flex-wrap mb-3">
                  <b className="text-[14px]">{convMeta?.title || convMeta?.participant_name || "Conversation"}</b>
                  <Badge tone="muted">{platformLabel(convMeta?.platform ?? "")}</Badge>
                  <Badge tone={statusTone(convMeta?.status)}>{convMeta?.status ?? "open"}</Badge>
                  <span className="ml-auto text-[11.5px]" style={{ color: "var(--text-faint)" }}>
                    {convThread.length} messages · {convMeta?.unread_count ?? 0} unread
                  </span>
                </div>
                {conversation.loading ? <Loading rows={3} /> : !convThread.length ? (
                  <Empty title="No messages in this thread" />
                ) : (
                  <div className="space-y-2">
                    {convThread.map((m) => (
                      <button key={m.id} onClick={() => openInteraction(m.id)}
                        className="w-full text-left rounded-xl px-3.5 py-2.5"
                        style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <span className="text-[12.5px] font-semibold">{m.author_name || "Unknown"}</span>
                          <Badge tone="muted">{m.kind}</Badge>
                          <Badge tone={statusTone(m.status)}>{m.status}</Badge>
                          <span className="ml-auto font-mono text-[10.5px]" style={{ color: "var(--text-faint)" }}>
                            {fmtAgo(m.created_at)}
                          </span>
                        </div>
                        <div className="text-[13px] mt-1" style={{ color: "var(--text)" }}>{m.text}</div>
                      </button>
                    ))}
                  </div>
                )}
              </Card>
            )}

            {/* full interaction detail */}
            {interactionId && (
              detail.loading && !detail.data ? <Loading rows={5} />
                : detail.error && !detail.data ? <ErrorBox error={detail.error} onRetry={detail.reload} />
                : current && (
                <>
                  {/* --- header + actions --- */}
                  <Card>
                    <div className="flex items-start gap-2.5">
                      <Avatar name={current.author_name || "??"} size={36} />
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-1.5 flex-wrap">
                          <b className="text-[14px]">{current.author_name || "Unknown author"}</b>
                          <Badge tone="muted">{platformLabel(current.platform)}</Badge>
                          <Badge tone="info">{current.kind}</Badge>
                          <Badge tone={statusTone(current.status)}>{current.status}</Badge>
                          <Badge tone={PRIORITY_TONE[current.priority] ?? "muted"}>{current.priority}</Badge>
                          {current.is_question && <Badge tone="info">question</Badge>}
                        </div>
                        <div className="font-mono text-[10.5px] mt-1" style={{ color: "var(--text-faint)" }}>
                          {current.remote_id} · {fmtAgo(current.created_at)}
                        </div>
                      </div>
                    </div>
                    <p className="text-[13.5px] mt-3 whitespace-pre-wrap">{current.text}</p>
                    {detailData?.linked_publication && (
                      <div className="mt-3 rounded-xl px-3 py-2 text-[12.5px]"
                        style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
                        On post: <b>{detailData.linked_publication.title || detailData.linked_publication.remote_post_id}</b>
                        {detailData.linked_publication.platform ? ` · ${platformLabel(detailData.linked_publication.platform)}` : ""}
                      </div>
                    )}
                    <div className="flex gap-2 mt-3 flex-wrap">
                      <button className="btn-outline !text-xs" disabled={busy === "read"}
                        onClick={() => toggleRead(current)}>
                        {current.status === "unread" ? "Mark read" : "Mark unread"}
                      </button>
                      <button className="btn-outline !text-xs" disabled={busy === "classify"}
                        onClick={classify}>
                        {busy === "classify" ? "Classifying…" : "Run AI classification"}
                      </button>
                      <button className="btn-outline !text-xs" onClick={() => setInteractionId(null)}>
                        Back to thread
                      </button>
                    </div>
                  </Card>

                  {/* --- thread (ancestors) --- */}
                  {thread.length > 0 && (
                    <Card>
                      <div className="panel-label mb-2">Thread ({thread.length} earlier)</div>
                      <div className="space-y-2">
                        {thread.map((t) => (
                          <div key={t.id} className="rounded-xl px-3 py-2"
                            style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
                            <div className="flex items-center gap-1.5">
                              <span className="text-[12.5px] font-semibold">{t.author_name || "Unknown"}</span>
                              <span className="font-mono text-[10.5px] ml-auto" style={{ color: "var(--text-faint)" }}>
                                {fmtAgo(t.created_at)}
                              </span>
                            </div>
                            <div className="text-[13px] mt-0.5" style={{ color: "var(--text-muted)" }}>{t.text}</div>
                          </div>
                        ))}
                      </div>
                    </Card>
                  )}

                  {/* --- AI classification panel --- */}
                  <Card>
                    <div className="flex items-center gap-2 mb-2">
                      <b className="text-[13.5px]">AI classification</b>
                      <Badge tone="muted">
                        {(current.classifications ?? []).length} labels
                      </Badge>
                      <span className="ml-auto font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                        sentiment: {current.sentiment || "—"} · intent: {current.intent || "—"}
                      </span>
                    </div>
                    {(current.classifications ?? []).length ? (
                      <div className="flex gap-1.5 flex-wrap">
                        {current.classifications.map((c: any, i: number) => {
                          const conf = confOf(c);
                          return (
                            <span key={`${labelOf(c)}-${i}`} className="badge"
                              style={{ background: "var(--info-dim)", color: "var(--info)" }}>
                              {labelOf(c)}
                              {conf != null && <span className="opacity-70">{Math.round(conf * 100)}%</span>}
                            </span>
                          );
                        })}
                      </div>
                    ) : (
                      <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                        No labels yet — run the classifier to triage this item.
                      </div>
                    )}
                    {!(current.moderation ?? []).length ? null : (
                      <div className="mt-3 space-y-1">
                        <div className="panel-label">Moderation</div>
                        {current.moderation.map((m: any, i: number) => (
                          <div key={i} className="text-[12px] flex gap-2 flex-wrap">
                            <Badge tone={m.verdict === "BLOCK_ACTION" ? "error" : m.verdict === "ALLOW" ? "success" : "warning"}>
                              {String(m.verdict ?? "").replace(/_/g, " ")}
                            </Badge>
                            <span style={{ color: "var(--text-muted)" }}>{m.reason ?? m.rule ?? ""}</span>
                          </div>
                        ))}
                      </div>
                    )}
                  </Card>

                  {/* --- suggested reply editor + brand check + approval --- */}
                  <Card>
                    <div className="flex items-center gap-2 flex-wrap mb-2">
                      <b className="text-[13.5px]">Suggested reply</b>
                      {latestAction && (
                        <Badge tone={BADGE_TONE[latestAction.badge] ?? "muted"}>{latestAction.badge}</Badge>
                      )}
                      {latestAction && (
                        <Badge tone={statusTone(latestAction.state)}>
                          {latestAction.state.replace(/_/g, " ")}
                        </Badge>
                      )}
                      {latestAction?.is_mock && <Badge tone="warning">mock receipt</Badge>}
                      <span className="ml-auto flex gap-1.5">
                        <button className="btn-outline !text-xs" disabled={busy === "draft" || !current.conversation_id}
                          onClick={() => draft(false)}>
                          {busy === "draft" ? "Drafting…" : latestAction ? "Re-draft" : "Draft reply"}
                        </button>
                        <button className="btn-outline !text-xs" disabled={busy === "draft" || !latestAction}
                          onClick={() => draft(true)}>
                          ↻ Regenerate
                        </button>
                      </span>
                    </div>

                    {!current.conversation_id && (
                      <div className="text-[12px] mb-2" style={{ color: "var(--text-faint)" }}>
                        Not linked to a conversation — replies are unavailable for this item.
                      </div>
                    )}

                    <textarea className="textarea !text-[13px] min-h-[110px]" value={replyText}
                      placeholder="AI draft or your own words — edit freely before sending…"
                      disabled={!current.conversation_id}
                      onChange={(e) => { setReplyText(e.target.value); setReplyTouched(true); }} />

                    <div className="flex items-center gap-2 mt-2 flex-wrap">
                      <button className="btn-outline !text-xs" disabled={!current.conversation_id || busy === "reply"}
                        onClick={() => queueReply(false)}>
                        Queue draft
                      </button>
                      {latestAction ? (
                        <button className="btn-primary !text-xs" disabled={busy === "send" || !replyText.trim()}
                          onClick={sendEdited}>
                          {busy === "send" ? "Sending…" : "Send now"}
                        </button>
                      ) : (
                        <button className="btn-primary !text-xs" disabled={busy === "reply" || !replyText.trim()}
                          onClick={() => queueReply(true)}>
                          {busy === "reply" ? "Sending…" : "Send now"}
                        </button>
                      )}
                      {latestAction && (latestAction.state === "draft" || latestAction.state === "pending_approval") && (
                        <>
                          <button className="btn-outline !text-xs" disabled={busy === "decide"}
                            onClick={() => decide(latestAction, true)}>Approve</button>
                          <ConfirmButton className="btn-danger !text-xs" confirmText="Reject draft?"
                            disabled={busy === "decide"} onConfirm={() => decide(latestAction, false)}>
                            Reject
                          </ConfirmButton>
                        </>
                      )}
                      {replyTouched && <Badge tone="warning">edited by human</Badge>}
                    </div>

                    {/* brand check */}
                    <div className="mt-3 pt-3" style={{ borderTop: "var(--seam)" }}>
                      <div className="flex items-center gap-2 flex-wrap">
                        <div className="panel-label">Brand check</div>
                        {latestAction?.brand_check?.status ? (
                          <Badge tone={BRAND_TONE[String(latestAction.brand_check.status).toUpperCase()] ?? "muted"}>
                            {String(latestAction.brand_check.status).replace(/_/g, " ")}
                          </Badge>
                        ) : (
                          <Badge tone="muted">not run</Badge>
                        )}
                      </div>
                      {latestAction?.brand_check && Object.keys(latestAction.brand_check).length > 0 ? (
                        <div className="text-[12px] mt-1.5 space-y-1" style={{ color: "var(--text-muted)" }}>
                          {latestAction.brand_check.forbidden_hits?.length > 0 && (
                            <div>⚠ forbidden phrases: {firstText(latestAction.brand_check.forbidden_hits)}</div>
                          )}
                          {latestAction.brand_check.required_disclaimers?.length > 0 && (
                            <div>ℹ disclaimers: {firstText(latestAction.brand_check.required_disclaimers)}</div>
                          )}
                          {latestAction.brand_check.tone && <div>tone: {String(latestAction.brand_check.tone)}</div>}
                          {!latestAction.brand_check.forbidden_hits?.length
                            && !latestAction.brand_check.required_disclaimers?.length
                            && !latestAction.brand_check.tone && (
                            <div>No violations found in the draft.</div>
                          )}
                        </div>
                      ) : (
                        <div className="text-[12px] mt-1.5" style={{ color: "var(--text-faint)" }}>
                          Draft a reply to run the brand policy check on the text.
                        </div>
                      )}
                      {latestAction?.error && (
                        <div className="text-[12px] mt-1.5" style={{ color: "var(--danger)" }}>
                          Last send error: {latestAction.error}
                        </div>
                      )}
                    </div>
                  </Card>

                  {/* --- action history for this interaction --- */}
                  <Card>
                    <div className="panel-label mb-2">Action history ({actions.length})</div>
                    {!actions.length ? (
                      <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>
                        No actions yet — drafts, approvals and sends are recorded here.
                      </div>
                    ) : (
                      <div className="space-y-2">
                        {actions.map((a) => (
                          <div key={a.id} className="rounded-xl px-3 py-2"
                            style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
                            <div className="flex items-center gap-1.5 flex-wrap">
                              <Badge tone={BADGE_TONE[a.badge] ?? "muted"}>{a.badge}</Badge>
                              <Badge tone={statusTone(a.state)}>{a.state}</Badge>
                              <Badge tone="muted">{a.action_type}</Badge>
                              <Badge tone="muted">{a.mode}</Badge>
                              <span className="ml-auto font-mono text-[10.5px]" style={{ color: "var(--text-faint)" }}>
                                {fmtAgo(a.created_at)}
                              </span>
                            </div>
                            {(a.final_text || a.draft_text) && (
                              <div className="text-[12.5px] mt-1" style={{ color: "var(--text-muted)" }}>
                                {a.final_text || a.draft_text}
                              </div>
                            )}
                            {a.remote_reply_id && (
                              <div className="font-mono text-[10.5px] mt-1" style={{ color: "var(--text-faint)" }}>
                                remote reply {a.remote_reply_id}{a.is_mock ? " (mock)" : ""}
                              </div>
                            )}
                          </div>
                        ))}
                      </div>
                    )}
                  </Card>
                </>
              )
            )}
          </div>
        </div>
      )}
    </div>
  );
}

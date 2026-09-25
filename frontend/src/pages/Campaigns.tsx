import { useMemo, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Field, Modal, PageHeader, Section, statusTone, toast } from "../components/ui";
import { fmtDate } from "../lib/format";

/* Work 04 campaign workspace (Lane C, integrated).
 *
 * Routes (all live):
 *   GET  /campaigns, /campaigns/{id}, /campaigns/{id}/content,
 *        /campaigns/{id}/progress, /campaigns/{id}/qc
 *   POST /campaigns/{id}/generate-more (fallback: .../derive), .../schedule,
 *        .../publish, .../cancel, .../platform-variants
 *   POST /campaigns/{id}/content/{itemId}/regenerate
 *        (fallback: /content/{itemId}/regenerate)
 *   Approve uses the canonical content actions API:
 *        POST /content/{itemId}/actions {action:"approve"}
 * Calendar reuses the existing scheduler data (GET /calendar, ScheduleEntry
 * rows); no new scheduling storage.
 */

async function tryGet(paths: string[]): Promise<any | null> {
  for (const p of paths) {
    try {
      return await wsApi.get(p);
    } catch {
      /* try next fallback */
    }
  }
  return null;
}

async function tryPost(paths: string[], body: any = {}): Promise<any> {
  let last: any = new Error("no endpoint available");
  for (const p of paths) {
    try {
      return await wsApi.post(p, body);
    } catch (e: any) {
      last = e;
    }
  }
  throw last;
}

export default function Campaigns() {
  const { id } = useParams();
  return id ? <CampaignWorkspace id={id} /> : <CampaignList />;
}

/* ------------------------------------------------------------------ list */

function CampaignList() {
  // Enrich each row with detail progress (derived/scheduled/published);
  // per-row detail fetch fails soft for campaigns Lane B hasn't shaped yet.
  const list = useFetch(async () => {
    const items = ((await wsApi.get("/campaigns")) as any)?.items ?? [];
    const enriched = await Promise.all(
      items.map(async (c: any) => {
        try {
          const d = (await wsApi.get(`/campaigns/${c.id}`)) as any;
          return { ...c, progress: d?.progress, master_title: d?.master_title ?? d?.master?.topic };
        } catch {
          return c;
        }
      })
    );
    return { items: enriched };
  }, []);
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [goal, setGoal] = useState("");
  const [platforms, setPlatforms] = useState<string[]>(["youtube", "tiktok"]);

  async function create() {
    if (!name.trim()) return;
    try {
      await wsApi.post("/campaigns", { name, goal, platforms });
      setName("");
      setGoal("");
      setOpen(false);
      list.reload();
      toast("Campaign created", "success");
    } catch (e: any) {
      toast(e.message, "error", "Create failed");
    }
  }

  function toggle(p: string) {
    setPlatforms(platforms.includes(p) ? platforms.filter((x) => x !== p) : [...platforms, p]);
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Campaigns" subtitle="Master video → multi-platform shorts."
        actions={<button className="btn-primary !text-xs" onClick={() => setOpen(true)}>+ New campaign</button>} />
      <Section data={(list.data as any)?.items} loading={list.loading} error={list.error} onRetry={list.reload}
        empty="No campaigns" emptyHint="Campaigns group a master video with its derived shorts.">
        {(rows) => (
          <div className="grid md:grid-cols-2 gap-3">
            {rows.map((c: any) => (
              <Link key={c.id} to={`/campaigns/${c.id}`}>
                <Card className="card-hover" style={{ padding: 15 }}>
                  <div className="flex gap-2 items-center mb-1">
                    <b className="text-[14px]">{c.name}</b>
                    <Badge tone={statusTone(c.status)}>{c.status}</Badge>
                  </div>
                  <div className="text-[12.5px] line-clamp-2" style={{ color: "var(--text-muted)" }}>
                    {c.master_title ? `Master: ${c.master_title}` : c.goal || "—"}
                  </div>
                  <div className="flex gap-1.5 mt-2 flex-wrap items-center">
                    {(c.platforms ?? []).map((p: string) => <Badge key={p} tone="muted">{p}</Badge>)}
                    <span className="font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>
                      {(c.platforms ?? []).length} platforms · derived {c.progress?.derived ?? c.progress?.content_items ?? "—"}
                      {" "}· scheduled {c.progress?.scheduled ?? "—"} · published {c.progress?.published ?? "—"}
                    </span>
                  </div>
                </Card>
              </Link>
            ))}
          </div>
        )}
      </Section>
      <Modal open={open} onClose={() => setOpen(false)} title="New campaign">
        <Field label="Name"><input className="input" value={name} onChange={(e) => setName(e.target.value)} /></Field>
        <Field label="Goal"><textarea className="textarea" rows={2} value={goal} onChange={(e) => setGoal(e.target.value)} /></Field>
        <Field label="Platforms">
          <div className="flex gap-2 flex-wrap">
            {["youtube", "tiktok", "facebook", "instagram"].map((p) => (
              <button key={p} className={platforms.includes(p) ? "btn-primary !text-xs" : "btn-outline !text-xs"} onClick={() => toggle(p)}>{p}</button>
            ))}
          </div>
        </Field>
        <button className="btn-primary !text-xs" onClick={create}>Create</button>
      </Modal>
    </div>
  );
}

/* -------------------------------------------------------------- workspace */

function CampaignWorkspace({ id }: { id: string }) {
  const nav = useNavigate();
  const detail = useFetch(() => wsApi.get(`/campaigns/${id}`), [id]);
  const content = useFetch(() => tryGet([`/campaigns/${id}/content`]), [id]);
  const progress = useFetch(() => tryGet([`/campaigns/${id}/progress`]), [id]);
  const qc = useFetch(() => tryGet([`/campaigns/${id}/qc`]), [id]);
  const schedule = useFetch(() => wsApi.get("/calendar"), [id]);
  const [selected, setSelected] = useState<string[]>([]);
  const [reviewOnly, setReviewOnly] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const d: any = detail.data ?? {};
  const items: any[] = useMemo(() => {
    const c: any = content.data;
    return c?.items ?? c?.shorts ?? d?.content ?? d?.shorts ?? [];
  }, [content.data, detail.data]);
  const entries: any[] = useMemo(() => {
    const all = ((schedule.data as any)?.items ?? []) as any[];
    const ids = new Set(items.map((i: any) => i.id ?? i.content_item_id));
    return all.filter((e: any) => e.campaign_id === id || (e.content_item_id && ids.has(e.content_item_id)));
  }, [schedule.data, items, id]);
  const entryByContent = useMemo(() => {
    const m = new Map<string, any>();
    for (const e of entries) if (e.content_item_id && !m.has(e.content_item_id)) m.set(e.content_item_id, e);
    return m;
  }, [entries]);
  const days = useMemo(() => {
    const groups = new Map<string, any[]>();
    for (const e of entries) {
      const day = (e.run_at ?? "").slice(0, 10) || "unscheduled";
      if (!groups.has(day)) groups.set(day, []);
      groups.get(day)!.push(e);
    }
    return [...groups.entries()].sort((a, b) => a[0].localeCompare(b[0]));
  }, [entries]);
  const visible = reviewOnly ? items.filter((i: any) => approvalOf(i) !== "APPROVED") : items;
  const allChecked = visible.length > 0 && visible.every((i: any) => selected.includes(itemId(i)));

  function reloadAll() {
    detail.reload();
    content.reload();
    progress.reload();
    qc.reload();
    schedule.reload();
    setSelected([]);
  }

  async function campaignAction(action: string, body: any = {}) {
    const paths =
      action === "generate-more"
        ? [`/campaigns/${id}/generate-more`, `/campaigns/${id}/derive`]
        : [`/campaigns/${id}/${action}`];
    setBusy(action);
    try {
      await tryPost(paths, body);
      toast(`Campaign ${action} requested`, "success");
      reloadAll();
    } catch (e: any) {
      toast(e.message ?? `${action} failed`, "error", "Campaign action failed");
    } finally {
      setBusy(null);
    }
  }

  async function itemAction(item: any, action: string, body: any = {}) {
    const iid = itemId(item);
    if (action === "approve") {
      // canonical approval endpoint (existing content actions API)
      setBusy(`${action}:${iid}`);
      try {
        await wsApi.post(`/content/${iid}/actions`, { action: "approve" });
        toast(`Approved ${iid.slice(0, 8)}`, "success");
        reloadAll();
      } catch (e: any) {
        toast(e.message ?? "approve failed", "error", "Item action failed");
      } finally {
        setBusy(null);
      }
      return;
    }
    const paths =
      action === "regenerate"
        ? [`/campaigns/${id}/content/${iid}/regenerate`, `/content/${iid}/regenerate`]
        : [`/campaigns/${id}/content/${iid}/${action}`];
    setBusy(`${action}:${iid}`);
    try {
      await tryPost(paths, body);
      toast(`${action} requested for ${iid.slice(0, 8)}`, "success");
      reloadAll();
    } catch (e: any) {
      toast(e.message ?? `${action} failed`, "error", "Item action failed");
    } finally {
      setBusy(null);
    }
  }

  async function bulk(action: "approve" | "regenerate" | "cancel" | "schedule") {
    if (selected.length === 0) {
      toast("Select at least one short", "info");
      return;
    }
    if (action === "cancel" || action === "schedule") {
      await campaignAction(action, { content_item_ids: selected });
      return;
    }
    setBusy(`bulk:${action}`);
    try {
      for (const iid of selected) {
        const item = items.find((i: any) => itemId(i) === iid);
        if (item) await itemAction(item, action);
      }
    } finally {
      setBusy(null);
      reloadAll();
    }
  }

  function toggleSelect(iid: string) {
    setSelected(selected.includes(iid) ? selected.filter((x) => x !== iid) : [...selected, iid]);
  }

  const master: any = d.master ?? (d.master_title ? { topic: d.master_title } : null);
  const prog: any = (progress.data as any) ?? d.progress ?? {};

  return (
    <div className="space-y-4">
      <PageHeader
        title={d.name ?? "Campaign"}
        subtitle={d.goal}
        actions={
          <div className="flex gap-2 flex-wrap">
            <button className="btn-outline !text-xs" onClick={() => nav("/campaigns")}>← All campaigns</button>
            <button className="btn-outline !text-xs" disabled={busy !== null} onClick={() => campaignAction("generate-more")}>Generate More</button>
            <button className={reviewOnly ? "btn-primary !text-xs" : "btn-outline !text-xs"} onClick={() => setReviewOnly(!reviewOnly)}>Review</button>
            <button className="btn-outline !text-xs" disabled={busy !== null} onClick={() => campaignAction("schedule")}>Schedule</button>
            <button className="btn-primary !text-xs" disabled={busy !== null} onClick={() => campaignAction("publish")}>Publish</button>
            <button className="btn-outline !text-xs" disabled={busy !== null} onClick={() => campaignAction("cancel")}>Cancel</button>
          </div>
        }
      />

      {/* master card */}
      <Card style={{ padding: 15 }}>
        <div className="flex gap-2 items-center mb-1">
          <b className="text-[14px]">Master video</b>
          {d.status && <Badge tone={statusTone(d.status)}>{d.status}</Badge>}
        </div>
        {detail.loading ? (
          <div className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>Loading…</div>
        ) : detail.error ? (
          <div className="text-[12.5px]">Failed to load campaign. <button className="btn-outline !text-xs ml-2" onClick={detail.reload}>Retry</button></div>
        ) : (
          <div className="text-[13px] space-y-1.5">
            <div><b>{master?.topic ?? master?.title ?? d.master_title ?? "—"}</b></div>
            <div style={{ color: "var(--text-muted)" }}>
              Derived <b className="font-mono">{prog.derived ?? prog.content_items ?? items.length}</b>
              {" "}· scheduled <b className="font-mono">{prog.scheduled ?? entries.length}</b>
              {" "}· published <b className="font-mono">{prog.published ?? "—"}</b>
              {(qc.data as any)?.score != null && (
                <> · QC <b className="font-mono">{(qc.data as any).score}</b></>
              )}
            </div>
            <div className="flex gap-2 flex-wrap items-center">
              {(d.platforms ?? []).map((p: string) => <Badge key={p} tone="muted">{p}</Badge>)}
              {(master?.timeline_id || d.master_timeline_id) && (
                <Link className="btn-outline !text-xs" to={`/editor/${master?.timeline_id ?? d.master_timeline_id}`}>Open master in editor</Link>
              )}
              <span className="font-mono text-[12px]" style={{ color: "var(--text-faint)" }}>
                {d.starts_at ? fmtDate(d.starts_at) : "no start"} → {d.ends_at ? fmtDate(d.ends_at) : "open ended"}
              </span>
            </div>
          </div>
        )}
      </Card>

      {/* calendar strip */}
      <Card style={{ padding: 15 }}>
        <b className="text-[13px]">Schedule</b>
        {schedule.loading ? (
          <div className="text-[12.5px] mt-1" style={{ color: "var(--text-muted)" }}>Loading…</div>
        ) : days.length === 0 ? (
          <div className="text-[12.5px] mt-1" style={{ color: "var(--text-muted)" }}>Nothing scheduled for this campaign yet.</div>
        ) : (
          <div className="flex gap-3 mt-2 overflow-x-auto pb-1">
            {days.map(([day, list]) => (
              <div key={day} className="min-w-[190px] rounded-lg p-2" style={{ background: "var(--bg-soft)" }}>
                <div className="font-mono text-[12px] mb-1.5">{day}</div>
                <div className="space-y-1.5">
                  {list.map((e: any) => (
                    <div key={e.id} className="text-[12px] flex gap-1.5 items-center">
                      <Badge tone={statusTone(e.status)}>{e.status}</Badge>
                      <span style={{ color: "var(--text-muted)" }}>{e.platform}</span>
                      <span className="truncate font-mono" title={e.content_item_id}>{contentTitle(items, e.content_item_id)}</span>
                    </div>
                  ))}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* bulk bar */}
      <div className="flex gap-2 flex-wrap items-center">
        <label className="text-[12.5px] flex gap-1.5 items-center" style={{ color: "var(--text-muted)" }}>
          <input type="checkbox" checked={allChecked} onChange={() => setSelected(allChecked ? [] : visible.map(itemId))} />
          Select all ({selected.length})
        </label>
        <button className="btn-outline !text-xs" disabled={busy !== null} onClick={() => bulk("approve")}>Approve selected</button>
        <button className="btn-outline !text-xs" disabled={busy !== null} onClick={() => bulk("schedule")}>Schedule selected</button>
        <button className="btn-outline !text-xs" disabled={busy !== null} onClick={() => bulk("regenerate")}>Regenerate selected</button>
        <button className="btn-outline !text-xs" disabled={busy !== null} onClick={() => bulk("cancel")}>Cancel selected</button>
      </div>

      {/* short cards */}
      {content.loading ? (
        <Card style={{ padding: 15 }}><div className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>Loading derivatives…</div></Card>
      ) : items.length === 0 ? (
        <Card style={{ padding: 15 }}>
          <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>
            No derivatives yet{content.data === null ? " (derivation endpoints not available yet — Lane B)." : "."} Use Generate More to derive shorts from the master.
          </div>
        </Card>
      ) : (
        <div className="grid md:grid-cols-2 gap-3">
          {visible.map((item: any) => {
            const iid = itemId(item);
            const variants = normalizeVariants(item);
            const entry = entryByContent.get(iid);
            const approval = approvalOf(item);
            const score = item.virality_score ?? item.score ?? null;
            const qcInfo = item.qc ?? item.qc_report ?? null;
            return (
              <Card key={iid} style={{ padding: 15 }}>
                <div className="flex gap-2 items-start">
                  <input type="checkbox" className="mt-1" checked={selected.includes(iid)} onChange={() => toggleSelect(iid)} />
                  {item.thumbnail_url && (
                    <img src={item.thumbnail_url} alt="" className="w-16 h-24 object-cover rounded-md" />
                  )}
                  <div className="flex-1 min-w-0">
                    <div className="flex gap-2 items-center flex-wrap">
                      <b className="text-[13.5px] truncate">{item.topic ?? item.title ?? iid.slice(0, 8)}</b>
                      <Badge tone={statusTone(approval)}>{approval}</Badge>
                      {score != null && <Badge tone="muted">score {score}</Badge>}
                      {qcInfo && (
                        <Badge tone={qcInfo.passed === false ? "danger" : "success"}>
                          QC {qcInfo.score ?? (qcInfo.passed ? "pass" : "fail")}
                        </Badge>
                      )}
                    </div>
                    <div className="text-[12.5px] mt-0.5 line-clamp-2" style={{ color: "var(--text-muted)" }}>
                      {item.hook ?? item.selected_hook ?? "—"}
                    </div>
                    <div className="font-mono text-[11.5px] mt-0.5" style={{ color: "var(--text-faint)" }}>
                      chapter: {item.chapter ?? item.source_chapter ?? item.chapter_title ?? "—"}
                      {entry?.run_at ? ` · scheduled ${fmtDate(entry.run_at)}` : ""}
                    </div>
                    <div className="flex gap-1.5 mt-2 flex-wrap">
                      {variants.map((v: any) => (
                        <Badge key={v.platform} tone={statusTone(v.status)}>{v.platform}{v.status ? ` · ${v.status}` : ""}</Badge>
                      ))}
                    </div>
                    <div className="flex gap-2 mt-2.5 flex-wrap">
                      <Link className="btn-outline !text-xs" to={`/editor/${item.timeline_id ?? iid}`}>Edit</Link>
                      <button className="btn-outline !text-xs" disabled={busy !== null} onClick={() => itemAction(item, "approve")}>Approve</button>
                      <button className="btn-outline !text-xs" disabled={busy !== null} onClick={() => itemAction(item, "regenerate")}>Regenerate</button>
                      <button className="btn-outline !text-xs" disabled={busy !== null}
                        onClick={async () => {
                          setBusy(`variants:${iid}`);
                          try {
                            await tryPost([`/campaigns/${id}/platform-variants`], { content_item_id: iid });
                            toast("Platform variants requested", "success");
                            reloadAll();
                          } catch (e: any) {
                            toast(e.message ?? "request failed", "error", "Variants failed");
                          } finally {
                            setBusy(null);
                          }
                        }}>Platform variants</button>
                    </div>
                  </div>
                </div>
              </Card>
            );
          })}
        </div>
      )}
    </div>
  );
}

/* ---------------------------------------------------------------- helpers */

function itemId(item: any): string {
  return item.id ?? item.content_item_id ?? "";
}

function approvalOf(item: any): string {
  return item.approval_state ?? item.approval ?? item.status ?? "UNKNOWN";
}

function normalizeVariants(item: any): Array<{ platform: string; status?: string }> {
  const raw = item.platform_variants ?? item.variants ?? item.platforms ?? [];
  return (Array.isArray(raw) ? raw : []).map((v: any) =>
    typeof v === "string" ? { platform: v } : { platform: v.platform ?? v.name ?? "?", status: v.status }
  );
}

function contentTitle(items: any[], contentItemId: string): string {
  const found = items.find((i: any) => itemId(i) === contentItemId);
  const t = found?.topic ?? found?.title;
  return t ? String(t).slice(0, 24) : (contentItemId ?? "").slice(0, 8);
}

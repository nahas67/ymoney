import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { wsApi, videoFileUrl, videoThumbUrl, coverFileUrl, aiCoverFileUrl, downloadAudit } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Modal, PageHeader, ScoreBar, Tabs, WhyPanel, toast } from "../components/ui";
import TimelinesPanel from "../components/TimelinesPanel";
import { fmtDate } from "../lib/format";

export default function ContentDetail() {
  const { contentId } = useParams();
  const nav = useNavigate();
  const detail = useFetch(() => wsApi.get(`/content/${contentId}`), [contentId]);
  const timeline = useFetch(() => wsApi.get(`/content/${contentId}/timeline`), [contentId]);
  const [tab, setTab] = useState<"video" | "research" | "strategy" | "variants" | "timeline" | "edit">("video");
  const [busy, setBusy] = useState("");
  const [thumbAt, setThumbAt] = useState("1.0");
  const [coversOpen, setCoversOpen] = useState(false);
  const [covers, setCovers] = useState<any[]>([]);
  const [coversBusy, setCoversBusy] = useState(false);
  const [pick, setPick] = useState<number | null>(null);
  const [coversMode, setCoversMode] = useState<"frames" | "ai">("frames");
  const [aiPrompt, setAiPrompt] = useState("");
  const [aiPick, setAiPick] = useState<number | null>(null);
  const [sched, setSched] = useState<{ platform: string; runAt: string } | null>(null);

  const c: any = detail.data;

  async function action(a: string, reason?: string) {
    setBusy(a);
    try {
      await wsApi.post(`/content/${contentId}/actions`, reason == null ? { action: a } : { action: a, reason });
      detail.reload();
      if (a === "approve") toast("Approved — upload queued", "success");
      else if (a === "reject") toast("Rejected", "warning");
    } catch (e: any) {
      toast(e.message, "error", "Action failed");
    } finally {
      setBusy("");
    }
  }

  async function reject() {
    const reason = window.prompt("Rejection reason (stored on the item):", "");
    if (reason == null) return;
    await action("reject", reason);
  }

  async function remakeThumb() {
    if (!c?.video?.id) return;
    setBusy("thumb");
    try {
      await wsApi.post(`/videos/${c.video.id}/thumbnail`, { at_seconds: parseFloat(thumbAt) || 1 });
      detail.reload();
      toast("Poster updated", "success");
    } catch (e: any) {
      toast(e.message, "error", "Thumbnail failed");
    } finally {
      setBusy("");
    }
  }

  async function openCovers() {
    if (!c?.video?.id) return;
    setCoversOpen(true);
    setCovers([]);
    setPick(null);
    setAiPick(null);
    setCoversMode("frames");
    setCoversBusy(true);
    try {
      const r = await wsApi.post(`/videos/${c.video.id}/covers`, { count: 3 });
      setCovers(r.covers ?? []);
    } catch (e: any) {
      toast(e.message, "error", "Covers failed");
      setCoversOpen(false);
    } finally {
      setCoversBusy(false);
    }
  }

  async function pickCover() {
    if (!c?.video?.id) return;
    const body = coversMode === "ai" ? { ai_cover_index: aiPick } : { cover_index: pick };
    if (coversMode === "ai" ? aiPick == null : pick == null) return;
    setBusy("pick");
    try {
      await wsApi.post(`/videos/${c.video.id}/thumbnail`, body);
      setCoversOpen(false);
      detail.reload();
      toast("Cover set", "success");
    } catch (e: any) {
      toast(e.message, "error", "Set cover failed");
    } finally {
      setBusy("");
    }
  }

  async function genAiCovers() {
    if (!c?.video?.id) return;
    setCovers([]);
    setAiPick(null);
    setCoversBusy(true);
    try {
      const r = await wsApi.post(`/videos/${c.video.id}/ai-covers`,
        { prompt: aiPrompt.trim() || undefined, count: 3 });
      setCovers(r.covers ?? []);
    } catch (e: any) {
      toast(e.message, "error", "AI covers failed");
    } finally {
      setCoversBusy(false);
    }
  }

  async function schedule() {
    if (!sched) return;
    setBusy("sched");
    try {
      // datetime-local is naive — the API requires explicit timezone ISO.
      await wsApi.post("/calendar", { content_item_id: contentId, platform: sched.platform, run_at: new Date(sched.runAt).toISOString() });
      toast("Scheduled", "success");
      setSched(null);
    } catch (e: any) {
      toast(e.message, "error", "Schedule failed");
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title={c?.topic ?? "Content"} subtitle={c ? `${c.status} · ${fmtDate(c.created_at)}` : undefined}
        actions={<>
          <button className="btn-ghost !text-xs" onClick={() => nav("/studio")}>← Library</button>
          <button className="btn-outline !text-xs" onClick={() => contentId && downloadAudit(contentId).then(
            () => toast("Audit exported", "success")).catch((e: any) => toast(e.message, "error", "Export failed"))}>Export audit</button>
          {c && ["QC", "APPROVED", "SCHEDULED"].includes(c.status) && (
            <>
              <button className="btn-primary !text-xs" disabled={busy === "approve"} onClick={() => action("approve")}>Approve</button>
              <button className="btn-outline !text-xs" disabled={busy === "retry"} onClick={() => action("retry")}>Retry</button>
              <button className="btn-outline !text-xs" disabled={busy === "reject"} onClick={reject}>Reject</button>
              <button className="btn-ghost !text-xs" disabled={busy === "skip"} onClick={() => action("skip")}>Skip</button>
            </>
          )}
        </>} />
      {detail.loading && <Card>Loading…</Card>}
      {detail.error && <Card>Error: {detail.error}</Card>}
      {c && (
        <>
          {c.video?.quality_notes?.includes("originality") || c.video?.quality_notes?.includes("fact") ? (
            <Card style={{ borderColor: "var(--warn)" }}>
              <span className="text-[13px]">⚠️ {c.video.quality_notes}</span>
            </Card>
          ) : null}
          <Tabs tabs={[
            { key: "video", label: "Video & QC" }, { key: "research", label: "Research & claims" },
            { key: "strategy", label: "Strategy" }, { key: "variants", label: `Variants (${c.variants?.length ?? 0})` },
            { key: "timeline", label: "Timeline" }, { key: "edit", label: "Edit timeline" },
          ]} active={tab} onChange={setTab} />

          {tab === "video" && (
            <div className="grid lg:grid-cols-2 gap-4">
              <Card>
                {c.video?.id ? (
                  <>
                    <video key={c.video.id} controls preload="metadata" className="w-full rounded-lg aspect-[9/16] max-h-[520px] bg-black"
                      src={videoFileUrl(c.video.id)} poster={c.video.thumbnail_path ? videoThumbUrl(c.video.id) : undefined} />
                    <div className="flex gap-2 mt-3 items-center flex-wrap">
                      <input className="input !w-24" value={thumbAt} onChange={(e) => setThumbAt(e.target.value)} aria-label="Thumbnail timestamp" />
                      <button className="btn-outline !text-xs" disabled={busy === "thumb"} onClick={remakeThumb}>Remake cover @sec</button>
                      <button className="btn-primary !text-xs" onClick={openCovers}>Compare covers</button>
                      <span className="text-[12px] font-mono" style={{ color: "var(--text-muted)" }}>{c.video.engine} · {c.video.aspect_ratio} · {c.video.status}</span>
                    </div>
                  </>
                ) : <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>No render yet for this item.</div>}
              </Card>
              <Card>
                <b className="text-[14px]">Quality control</b>
                {c.video?.quality != null ? (
                  <div className="mt-2 space-y-1.5">
                    <div className="flex items-center gap-2">
                      <ScoreBar value={c.video.quality} />
                      <Badge tone={c.video.quality_passed ? "success" : "error"}>{c.video.quality_passed ? "PASSED" : "REJECTED"}</Badge>
                    </div>
                    {Object.entries(c.video.quality_components ?? {}).map(([k, v]: any) => (
                      <div key={k} className="flex justify-between text-[12.5px]">
                        <span className="font-mono" style={{ color: "var(--text-muted)" }}>{k}</span>
                        <ScoreBar value={Number(v)} />
                      </div>
                    ))}
                    {c.video.quality_notes && <div className="text-[12.5px] pt-1" style={{ color: "var(--text-muted)" }}>{c.video.quality_notes}</div>}
                  </div>
                ) : <div className="text-[13px] mt-2" style={{ color: "var(--text-muted)" }}>Not QC'd yet.</div>}
                <div className="mt-4 pt-3" style={{ borderTop: "var(--seam)" }}>
                  <b className="text-[13px]">Publishing metadata</b>
                  <MetaPack variants={c.variants ?? []} />
                </div>
                <div className="mt-4 pt-3" style={{ borderTop: "var(--seam)" }}>
                  <b className="text-[13px]">Schedule this video</b>
                  <div className="flex gap-2 mt-2 flex-wrap">
                    <select className="select !w-36" value={sched?.platform ?? "youtube"} onChange={(e) => setSched({ platform: e.target.value, runAt: sched?.runAt ?? "" })}>
                      {["youtube", "tiktok", "facebook", "instagram"].map((p) => <option key={p} value={p}>{p}</option>)}
                    </select>
                    <input className="input !w-56" type="datetime-local" value={sched?.runAt ?? ""} onChange={(e) => setSched({ platform: sched?.platform ?? "youtube", runAt: e.target.value })} />
                    <button className="btn-outline !text-xs" disabled={busy === "sched" || !sched?.runAt} onClick={schedule}>Schedule</button>
                  </div>
                  <p className="text-[11.5px] mt-1.5" style={{ color: "var(--text-faint)" }}>Use timezone-aware ISO time; the sweep publishes when due. Best hours live on the Calendar page.</p>
                </div>
              </Card>
            </div>
          )}

          {tab === "research" && (
            <Card>
              <p className="text-[13.5px] mb-3">{c.research?.summary ?? "No research brief."}</p>
              <div className="grid md:grid-cols-2 gap-4">
                <div>
                  <div className="panel-label mb-1.5">Key facts</div>
                  {(c.research?.key_facts ?? []).map((f: string, i: number) => <div key={i} className="text-[13px] py-1" style={{ borderBottom: "var(--seam)" }}>• {f}</div>)}
                  <div className="panel-label mt-3 mb-1.5">Angles</div>
                  {(c.research?.angles ?? []).map((f: string, i: number) => <div key={i} className="text-[13px] py-1" style={{ borderBottom: "var(--seam)" }}>• {f}</div>)}
                </div>
                <div>
                  <div className="panel-label mb-1.5">Claims + factual confidence {c.research?.factual_confidence != null ? `(${Math.round(c.research.factual_confidence * 100)}% · ${c.research.fact_status})` : ""}</div>
                  {(c.research?.claims ?? []).map((cl: any, i: number) => (
                    <div key={i} className="py-1.5" style={{ borderBottom: "var(--seam)" }}>
                      <Badge tone={cl.status === "VERIFIED" ? "success" : cl.status === "LIKELY" ? "info" : cl.status === "CONFLICTING" ? "error" : "warning"}>{cl.status}</Badge>
                      <span className="text-[13px] ml-2">{cl.claim}</span>
                      {cl.basis && <div className="text-[12px]" style={{ color: "var(--text-faint)" }}>{cl.basis}</div>}
                    </div>
                  ))}
                  {!((c.research?.claims ?? []).length) && <div className="text-[13px]" style={{ color: "var(--text-faint)" }}>No tracked claims.</div>}
                </div>
              </div>
            </Card>
          )}

          {tab === "strategy" && (
            <Card>
              <pre className="text-[12.5px] font-mono whitespace-pre-wrap">{JSON.stringify(c.strategy ?? {}, null, 2)}</pre>
              {c.strategy?.decision && <div className="mt-3"><WhyPanel why={c.strategy.decision} /></div>}
            </Card>
          )}

          {tab === "variants" && (
            <VariantsCompare variants={c.variants ?? []} contentId={contentId!} onChange={detail.reload} />
          )}

          {tab === "timeline" && (
            <Card>
              {((timeline.data as any)?.items ?? []).map((t: any, i: number) => (
                <div key={i} className="flex gap-3 py-2 text-[13px]" style={{ borderBottom: "var(--seam)" }}>
                  <span className="font-mono text-[11.5px] whitespace-nowrap" style={{ color: "var(--text-faint)" }}>{t.at?.slice(0, 16).replace("T", " ")}</span>
                  <div><b className="font-mono text-[11.5px]">{t.kind}</b> — {t.label}{t.detail ? <span style={{ color: "var(--text-muted)" }}> · {t.detail}</span> : null}</div>
                </div>
              ))}
            </Card>
          )}

          {tab === "edit" && contentId && (
            <TimelinesPanel contentId={contentId} videoId={c.video?.id} />
          )}
        </>
      )}

      <Modal open={coversOpen} onClose={() => setCoversOpen(false)} title="Compare covers" wide>
        <div className="flex gap-2 mb-3">
          <button className={coversMode === "frames" ? "btn-primary !text-xs" : "btn-ghost !text-xs"}
            onClick={() => { setCoversMode("frames"); setAiPick(null); if (c?.video?.id) openCovers(); }}>Frames</button>
          <button className={coversMode === "ai" ? "btn-primary !text-xs" : "btn-ghost !text-xs"}
            onClick={() => { setCoversMode("ai"); setCovers([]); setPick(null); }}>✨ AI generate</button>
        </div>
        {coversMode === "ai" && (
          <div className="flex gap-2 mb-3">
            <input className="input" placeholder="Prompt (blank = topic + hook)…" value={aiPrompt} onChange={(e) => setAiPrompt(e.target.value)} />
            <button className="btn-outline !text-xs whitespace-nowrap" disabled={coversBusy} onClick={genAiCovers}>
              {coversBusy ? "…" : "Generate 3"}
            </button>
          </div>
        )}
        {coversBusy && <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>{coversMode === "ai" ? "Generating…" : "Extracting candidates…"}</div>}
        {!coversBusy && covers.length > 0 && (
          <>
            <div className="grid grid-cols-3 gap-3">
              {covers.map((cv: any) => {
                const sel = coversMode === "ai" ? aiPick : pick;
                const setSel = coversMode === "ai" ? setAiPick : setPick;
                return (
                  <button key={cv.index} onClick={() => setSel(cv.index)}
                    className="rounded-xl overflow-hidden text-left"
                    style={{ border: sel === cv.index ? "2px solid var(--accent)" : "var(--seam)", padding: 0, background: "var(--bg-inset)" }}>
                    <img src={coversMode === "ai" ? aiCoverFileUrl(c.video.id, cv.index) : coverFileUrl(c.video.id, cv.index)}
                      alt={coversMode === "ai" ? `AI cover ${cv.index}` : `cover @${cv.at_seconds}s`} className="w-full aspect-[9/16] object-cover" />
                    <div className="px-2 py-1.5 font-mono text-[11px]" style={{ color: "var(--text-muted)" }}>
                      {coversMode === "ai" ? "✨ AI" : `@${cv.at_seconds}s`}
                    </div>
                  </button>
                );
              })}
            </div>
            <div className="flex justify-end mt-3">
              <button className="btn-primary !text-xs"
                disabled={(coversMode === "ai" ? aiPick == null : pick == null) || busy === "pick"} onClick={pickCover}>
                {busy === "pick" ? "…" : "Set as cover"}
              </button>
            </div>
          </>
        )}
        {!coversBusy && !covers.length && (
          <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>
            {coversMode === "ai"
              ? "Press Generate — needs an image provider key (Settings → Connections)."
              : "No candidates — ffmpeg may be unavailable."}
          </div>
        )}
      </Modal>
    </div>
  );
}

function estSeconds(script: string): number {
  return Math.max(5, Math.round((script || "").split(/\s+/).filter(Boolean).length / 2.6));
}

function MetaPack({ variants }: { variants: any[] }) {
  const sel = variants.find((v) => v.selected) ?? variants[0];
  const meta = sel?.metadata ?? {};
  const plats = Object.keys(meta);
  const [plat, setPlat] = useState("");
  const [copied, setCopied] = useState("");
  const active = plat || (plats[0] ?? "");
  const m = meta[active] ?? {};

  if (!sel) return <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>No metadata yet — generated at publish time.</div>;
  if (!plats.length) return <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>No metadata yet — generated at publish time.</div>;

  async function copy(key: string, text: string) {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      setTimeout(() => setCopied(""), 1500);
    } catch {
      toast("Copy failed — select the text manually.", "warning");
    }
  }

  const copyBtn = (key: string, text: string) => (
    <button className="btn-ghost !text-[11px] !py-0.5 ml-auto" onClick={() => copy(key, text)}>
      {copied === key ? "Copied ✓" : "Copy"}
    </button>
  );

  return (
    <div className="mt-2">
      <div className="flex gap-1.5 flex-wrap mb-2">
        {plats.map((p) => (
          <button key={p} className={`tab ${active === p ? "active" : ""}`} onClick={() => setPlat(p)}>{p}</button>
        ))}
      </div>
      <div className="text-[13px] font-medium">{m.title}</div>
      {(m.title_variants ?? []).length > 0 && (
        <div className="text-[12px] mt-1" style={{ color: "var(--text-muted)" }}>
          A/B: {(m.title_variants ?? []).join("  ·  ")}
        </div>
      )}
      {[
        ["First comment (post manually, then pin the CTA)", m.first_comment, "first"],
        ["Pinned comment CTA", m.pinned_comment, "pinned"],
      ].map(([label, text, key]: any) => (
        text ? (
          <div key={key} className="mt-2 rounded-lg p-2.5" style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
            <div className="flex items-center gap-2 mb-1">
              <span className="text-[11.5px] font-semibold uppercase tracking-wide" style={{ color: "var(--text-muted)" }}>{label}</span>
              {copyBtn(key, text)}
            </div>
            <div className="text-[12.5px] whitespace-pre-wrap">{text}</div>
          </div>
        ) : null
      ))}
      <div className="text-[11.5px] mt-1.5" style={{ color: "var(--text-faint)" }}>
        Auto-posting comments isn't available on current platform scopes — post from the phone app after publishing.
      </div>
    </div>
  );
}

function VariantsCompare({ variants, contentId, onChange }: { variants: any[]; contentId: string; onChange: () => void }) {
  const [compare, setCompare] = useState<string[]>([]);
  const [busy, setBusy] = useState("");

  function toggle(id: string) {
    setCompare((cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id].slice(-2)));
  }

  async function select(id: string) {
    setBusy(id);
    try {
      await wsApi.post(`/content/${contentId}/variants/${id}/select`);
      onChange();
      toast("Variant selected", "success");
    } catch (e: any) {
      toast(e.message, "error", "Select failed");
    } finally {
      setBusy("");
    }
  }

  const shown = compare.length === 2 ? variants.filter((v) => compare.includes(v.id)) : variants;
  const titlesOf = (v: any) => v.metadata?.youtube?.title_variants ?? v.metadata?.tiktok?.title_variants ?? [];

  return (
    <div className="space-y-3">
      {variants.length >= 2 && (
        <div className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>
          Tick two variants to compare side-by-side. Switching is allowed before production starts.
        </div>
      )}
      <div className={compare.length === 2 ? "grid md:grid-cols-2 gap-3" : "space-y-3"}>
        {shown.map((v: any) => (
          <Card key={v.id} style={v.selected ? { borderColor: "var(--accent)" } : undefined}>
            <div className="flex gap-2 items-center flex-wrap mb-2">
              {variants.length >= 2 && (
                <input type="checkbox" checked={compare.includes(v.id)} onChange={() => toggle(v.id)} aria-label={`Compare ${v.label}`} />
              )}
              <Badge tone={v.selected ? "success" : "muted"}>{v.label}{v.selected ? " · selected" : ""}</Badge>
              {v.predicted_score != null && <span className="font-mono text-[12px]">hook {v.predicted_score}</span>}
              <span className="font-mono text-[12px]" style={{ color: "var(--text-faint)" }}>~{estSeconds(v.script)}s · {v.script.split(/\s+/).filter(Boolean).length}w</span>
              {!v.selected && (
                <button className="btn-outline !text-[11px] !py-1 ml-auto" disabled={busy === v.id} onClick={() => select(v.id)}>
                  {busy === v.id ? "…" : "Set as selected"}
                </button>
              )}
            </div>
            <p className="text-[13.5px] whitespace-pre-wrap max-h-[220px] overflow-y-auto">{v.script}</p>
            {titlesOf(v).length > 0 && (
              <div className="mt-2 pt-2" style={{ borderTop: "var(--seam)" }}>
                <div className="panel-label mb-1">Title A/B</div>
                {titlesOf(v).map((t: string, i: number) => (
                  <div key={i} className="text-[12.5px] py-0.5">• {t}</div>
                ))}
              </div>
            )}
          </Card>
        ))}
      </div>
    </div>
  );
}

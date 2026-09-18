import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { wsApi, videoFileUrl, videoThumbUrl } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, PageHeader, ScoreBar, Tabs, WhyPanel } from "../components/ui";
import { fmtDate } from "../lib/format";

export default function ContentDetail() {
  const { contentId } = useParams();
  const nav = useNavigate();
  const detail = useFetch(() => wsApi.get(`/content/${contentId}`), [contentId]);
  const timeline = useFetch(() => wsApi.get(`/content/${contentId}/timeline`), [contentId]);
  const [tab, setTab] = useState<"video" | "research" | "strategy" | "variants" | "timeline">("video");
  const [busy, setBusy] = useState("");
  const [thumbAt, setThumbAt] = useState("1.0");
  const [sched, setSched] = useState<{ platform: string; runAt: string } | null>(null);

  const c: any = detail.data;

  async function action(a: string) {
    setBusy(a);
    try {
      await wsApi.post(`/content/${contentId}/actions`, { action: a });
      detail.reload();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy("");
    }
  }

  async function remakeThumb() {
    if (!c?.video?.id) return;
    setBusy("thumb");
    try {
      await wsApi.post(`/videos/${c.video.id}/thumbnail`, { at_seconds: parseFloat(thumbAt) || 1 });
      detail.reload();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy("");
    }
  }

  async function schedule() {
    if (!sched) return;
    setBusy("sched");
    try {
      // datetime-local is naive — the API requires explicit timezone ISO.
      await wsApi.post("/calendar", { content_item_id: contentId, platform: sched.platform, run_at: new Date(sched.runAt).toISOString() });
      alert("Scheduled");
      setSched(null);
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title={c?.topic ?? "Content"} subtitle={c ? `${c.status} · ${fmtDate(c.created_at)}` : undefined}
        actions={<>
          <button className="btn-ghost !text-xs" onClick={() => nav("/studio")}>← Library</button>
          {c && ["QC", "APPROVED", "SCHEDULED"].includes(c.status) && (
            <>
              <button className="btn-primary !text-xs" disabled={busy === "approve"} onClick={() => action("approve")}>Approve</button>
              <button className="btn-outline !text-xs" disabled={busy === "retry"} onClick={() => action("retry")}>Retry</button>
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
            { key: "timeline", label: "Timeline" },
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
            <div className="space-y-3">
              {(c.variants ?? []).map((v: any) => (
                <Card key={v.id} style={v.selected ? { borderColor: "var(--accent)" } : undefined}>
                  <div className="flex gap-2 items-center flex-wrap mb-2">
                    <Badge tone={v.selected ? "success" : "muted"}>{v.label}{v.selected ? " · selected" : ""}</Badge>
                    {v.predicted_score != null && <span className="font-mono text-[12px]">hook {v.predicted_score}</span>}
                  </div>
                  <p className="text-[13.5px] whitespace-pre-wrap">{v.script}</p>
                </Card>
              ))}
            </div>
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
        </>
      )}
    </div>
  );
}

import { useCallback, useEffect, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { wsApi } from "../lib/api";
import { Badge, Card, PageHeader, Skeleton, Tabs, fmtDate } from "../components/ui";

const TABS = ["overview", "research", "strategy", "script", "video", "publishing", "timeline"];

export default function ContentDetail() {
  const { contentId } = useParams();
  const nav = useNavigate();
  const [item, setItem] = useState<any>(null);
  const [timeline, setTimeline] = useState<any[]>([]);
  const [posts, setPosts] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState("overview");

  const load = useCallback(async () => {
    if (!contentId) return;
    setLoading(true);
    setError(null);
    try {
      const detail = await wsApi.get(`/content/${contentId}`);
      setItem(detail);
      wsApi.get(`/content/${contentId}/timeline`).then((tl) => setTimeline(tl.items ?? [])).catch(() => {});
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }, [contentId]);

  useEffect(() => { load(); }, [load]);

  // poll while a render is in flight so real progress reaches the UI
  useEffect(() => {
    if (item?.video?.status !== "RENDERING") return;
    const t = setInterval(() => wsApi.get(`/content/${contentId}`).then(setItem).catch(() => {}), 3000);
    return () => clearInterval(t);
  }, [item?.video?.status, contentId]);

  async function act(action: string) {
    if (!item) return;
    await wsApi.post(`/content/${item.id}/actions`, { action });
    load();
  }

  if (loading && !item) return <Skeleton rows={8} />;
  if (error)
    return (
      <div className="space-y-4">
        <Link to="/studio" className="btn-outline inline-flex">← Studio</Link>
        <Card><p className="text-sm text-red-500">{error}</p></Card>
      </div>
    );
  if (!item) return null;

  const why = item.strategy?.decision;
  const research = item.research ?? {};
  const selectedVariant = (item.variants ?? []).find((v: any) => v.selected);

  return (
    <div className="space-y-5">
      <button className="btn-outline" onClick={() => nav("/studio")}>← Content Studio</button>

      <PageHeader
        title={item.topic}
        subtitle={`Created ${fmtDate(item.created_at)} · ${item.variants_count} variant(s)`}
        actions={
          <>
            <Badge tone={
              ["LEARNED", "PUBLISHED", "APPROVED"].includes(item.status) ? "success"
              : ["FAILED", "SKIPPED"].includes(item.status) ? "error" : "info"
            }>{item.status.toLowerCase()}</Badge>
            <button className="btn-outline" onClick={() => act("retry")}>Retry</button>
            <button className="btn-ghost" onClick={() => act("skip")}>Archive</button>
            {item.status === "QC" && <button className="btn-primary" onClick={() => act("approve")}>Approve</button>}
          </>
        }
      />

      <Tabs tabs={TABS.map((t) => ({ key: t, label: t[0].toUpperCase() + t.slice(1) }))} active={tab} onChange={setTab} />

      {tab === "overview" && (
        <div className="grid lg:grid-cols-2 gap-5">
          {why && (
            <Card>
              <h3 className="text-xs font-semibold uppercase tracking-wider mb-2.5" style={{ color: "var(--text-muted)" }}>
                Why was this produced?
              </h3>
              <Badge tone="success">{why.action}</Badge>
              <ul className="mt-2.5 space-y-1">
                {(why.reasons ?? []).map((r: string, i: number) => (
                  <li key={i} className="text-[13px] flex gap-2"><span style={{ color: "var(--text-muted)" }}>—</span>{r}</li>
                ))}
              </ul>
              {(why.factors ?? []).map((f: any, i: number) => (
                <div key={i} className="flex justify-between text-[12px] mt-1">
                  <span style={{ color: "var(--text-muted)" }}>{String(f.name).replace(/_/g, " ")}</span>
                  <span className={`font-mono ${f.contribution >= 0 ? "text-emerald-500" : "text-red-500"}`}>{f.value}</span>
                </div>
              ))}
            </Card>
          )}
          <Card>
            <h3 className="text-xs font-semibold uppercase tracking-wider mb-2.5" style={{ color: "var(--text-muted)" }}>Video</h3>
            {item.video ? (
              <div className="space-y-1.5 text-[13px]">
                <p>Engine: {item.video.engine} {item.video.engine === "mock" && <Badge tone="warning">MOCK</Badge>}</p>
                <p>{item.video.aspect_ratio} · {item.video.resolution}</p>
                <p>QC: {item.video.quality != null ? `${Math.round(item.video.quality)}/100` : "pending"}</p>
              {item.video.status === "READY" && !item.video.file_path?.startsWith("mock:") && (
                <video
                  className="rounded-lg w-full max-h-[420px] bg-black"
                  src={`/api/v1/workspaces/${localStorage.getItem("ym_ws")}/videos/${item.video.id}/file`}
                  poster={item.video.thumbnail_path ? `/api/v1/workspaces/${localStorage.getItem("ym_ws")}/videos/${item.video.id}/thumbnail` : undefined}
                  controls
                  preload="none"
                  playsInline
                />
              )}
                <a
                  className="btn-outline mt-2 inline-flex"
                  href={`/api/v1/workspaces/${localStorage.getItem("ym_ws")}/videos/${item.video.id}/file`}
                  target="_blank" rel="noreferrer"
                >
                  Open artifact{item.video.engine === "mock" ? " (render spec)" : ""}
                </a>
              </div>
            ) : <p style={{ color: "var(--text-muted)" }}>No video yet.</p>}
          </Card>
        </div>
      )}

      {tab === "research" && (
        <Card>
          {research.summary ? (
            <div className="space-y-4 text-sm">
              <p>{research.summary}</p>
              {research.fact_status && (
                <Badge tone={
                  research.fact_status === "OK" ? "success"
                  : research.fact_status === "CONFLICTING" ? "error" : "warning"
                }>
                  facts: {String(research.fact_status).toLowerCase()} ({research.factual_confidence})
                </Badge>
              )}
              {(research.claims ?? []).length > 0 && (
                <div className="space-y-2">
                  {research.claims.map((c: any, i: number) => (
                    <div key={i} className="flex items-start gap-2 text-[13px]">
                      <Badge tone={
                        c.status === "VERIFIED" ? "success" : c.status === "LIKELY" ? "info"
                        : c.status === "CONFLICTING" ? "error" : "neutral"
                      }>{c.status}</Badge>
                      <div>{c.claim}<span className="block text-[11px]" style={{ color: "var(--text-muted)" }}>{c.basis}</span></div>
                    </div>
                  ))}
                </div>
              )}
              <ul className="list-disc pl-5 space-y-1">
                {(research.key_facts ?? []).map((f: string, i: number) => <li key={i}>{f}</li>)}
              </ul>
            </div>
          ) : <p style={{ color: "var(--text-muted)" }}>Research not available yet.</p>}
        </Card>
      )}

      {tab === "strategy" && (
        <Card>
          {item.strategy?.angle ? (
            <dl className="grid sm:grid-cols-2 gap-x-6 gap-y-3 text-sm">
              {[["Angle", item.strategy.angle], ["Audience", item.strategy.target_audience],
                ["Hook", item.strategy.hook_type], ["Duration", `${item.strategy.duration_seconds}s`],
                ["Tone", item.strategy.tone], ["CTA", item.strategy.cta],
                ["Platforms", (item.strategy.platforms ?? []).join(", ")], ["Aspect", item.strategy.aspect_ratio]].map(([k, v]) => (
                <div key={k as string}>
                  <dt className="text-[11px] uppercase tracking-wider" style={{ color: "var(--text-muted)" }}>{k}</dt>
                  <dd>{String(v ?? "—")}</dd>
                </div>
              ))}
            </dl>
          ) : <p style={{ color: "var(--text-muted)" }}>Strategy not generated yet.</p>}
        </Card>
      )}

      {tab === "script" && (
        <div className="space-y-4">
          {(item.variants ?? []).map((v: any) => (
            <Card key={v.id}>
              <div className="flex items-center justify-between mb-2">
                <h3 className="font-medium text-sm">Variant {v.label}</h3>
                <div className="flex gap-2">
                  {v.predicted_score != null && <Badge tone="info">hook score {v.predicted_score}</Badge>}
                  {v.selected && <Badge tone="success">selected</Badge>}
                </div>
              </div>
              <p className="text-sm whitespace-pre-wrap leading-relaxed">{v.script}</p>
              {v.metadata && Object.keys(v.metadata).length > 0 && (
                <details className="mt-3">
                  <summary className="text-xs cursor-pointer" style={{ color: "var(--text-muted)" }}>Platform metadata</summary>
                  <pre className="text-[11px] mt-2 overflow-x-auto">{JSON.stringify(v.metadata, null, 2)}</pre>
                </details>
              )}
            </Card>
          ))}
          {(item.variants ?? []).length === 0 && <Card><p style={{ color: "var(--text-muted)" }}>No scripts yet.</p></Card>}
        </div>
      )}

      {tab === "video" && (
        <Card>
          {item.video ? (
            <div className="space-y-3 text-[13px]">
              {item.video.status === "RENDERING" && (
                <div>
                  <div className="flex justify-between mb-1">
                    <span className="font-medium">Rendering…</span>
                    <span className="font-mono">{item.video.progress ?? 0}%</span>
                  </div>
                  <div className="h-2 rounded-full overflow-hidden" style={{ background: "var(--bg-subtle)" }}>
                    <div className="h-full transition-all" style={{ width: `${item.video.progress ?? 0}%`, background: "var(--accent)" }} />
                  </div>
                </div>
              )}
              <p>Engine: {item.video.engine}{item.video.engine === "mock" && " — simulated artifact, no real render"}</p>
              <p>Status: {item.video.status} · {item.video.aspect_ratio} · {item.video.resolution}</p>
              {item.video.duration_seconds != null && <p>Duration: {Number(item.video.duration_seconds).toFixed(1)}s</p>}
              {selectedVariant && <p>Script used: variant {selectedVariant.label}</p>}
              {(() => {
                const qc = item.video?.quality_components ?? {};
                const vis = qc.vision;
                if (!vis) return null;
                return (
                  <div className="mt-3 rounded-lg border p-3" style={{ borderColor: "var(--border)" }}>
                    <div className="flex items-center gap-2 mb-2">
                      <h4 className="text-xs font-semibold uppercase tracking-wider">Media inspection</h4>
                      {vis.is_mock && <Badge tone="warning">MOCK</Badge>}
                    </div>
                    <div className="flex flex-wrap gap-1.5 mb-2">
                      <Badge tone={vis.audio_present ? "success" : "error"}>{vis.audio_present ? "audio track" : "no audio"}</Badge>
                      <Badge tone={(vis.silent_sections ?? []).length ? "warning" : "success"}>
                        {(vis.silent_sections ?? []).length ? `${vis.silent_sections.length} silent section(s)` : "no silent gaps"}
                      </Badge>
                      <Badge tone={vis.subtitles_aligned ? "success" : "error"}>{vis.subtitles_aligned ? "subtitles aligned" : "subtitles misaligned"}</Badge>
                      {(vis.scenes ?? []).some((s: any) => s.black_frames || s.corrupted) && (
                        <Badge tone="error">black/corrupted frames detected</Badge>
                      )}
                    </div>
                    {vis.notes && <p className="text-[12px]" style={{ color: "var(--text-muted)" }}>{vis.notes}</p>}
                    {vis.provider && <p className="text-[11px] mt-1" style={{ color: "var(--text-muted)" }}>inspector: {vis.provider}</p>}
                  </div>
                );
  })()}
              {item.video.status === "READY" && !item.video.file_path?.startsWith("mock:") && (
                <video
                  className="rounded-lg w-full max-h-[420px] bg-black"
                  src={`/api/v1/workspaces/${localStorage.getItem("ym_ws")}/videos/${item.video.id}/file`}
                  poster={item.video.thumbnail_path ? `/api/v1/workspaces/${localStorage.getItem("ym_ws")}/videos/${item.video.id}/thumbnail` : undefined}
                  controls
                  preload="none"
                  playsInline
                />
              )}
                <a
                  className="btn-outline inline-flex mt-1"
                href={`/api/v1/workspaces/${localStorage.getItem("ym_ws")}/videos/${item.video.id}/file`}
                target="_blank" rel="noreferrer"
              >
                Open artifact
              </a>
            </div>
          ) : <p style={{ color: "var(--text-muted)" }}>No video rendered.</p>}
        </Card>
      )}

      {tab === "publishing" && (
        <Card pad={false}>
          <table className="table">
            <thead><tr><th>Platform</th><th>Title</th><th>Views</th><th>Published</th><th>Link</th></tr></thead>
            <tbody>
              {posts.filter((p: any) => p.title && p.published_at).slice(0, 10).map((p: any) => (
                <tr key={p.id}>
                  <td className="capitalize">{p.platform}{p.is_mock && <Badge tone="warning">mock</Badge>}</td>
                  <td className="max-w-[240px] truncate">{p.title}</td>
                  <td>{p.metrics?.views ?? 0}</td>
                  <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{fmtDate(p.published_at)}</td>
                  <td>{p.remote_url?.startsWith("http") ? <a className="underline" href={p.remote_url} target="_blank" rel="noreferrer">open</a> : "—"}</td>
                </tr>
              ))}
              {posts.filter((p: any) => p.title && p.published_at).length === 0 && (
                <tr><td colSpan={5} className="text-center py-8" style={{ color: "var(--text-muted)" }}>Not published yet.</td></tr>
              )}
            </tbody>
          </table>
        </Card>
      )}

      {tab === "timeline" && (
        <Card>
          <ol className="relative border-l ml-2 space-y-4" style={{ borderColor: "var(--border)" }}>
            {timeline.map((t, i) => (
              <li key={i} className="ml-4">
                <span className="absolute -left-[5px] mt-1.5 h-2 w-2 rounded-full" style={{ background: "var(--text-muted)" }} />
                <p className="text-sm">{t.label}</p>
                <p className="text-[11px]" style={{ color: "var(--text-muted)" }}>
                  {fmtDate(t.at)}{t.detail ? ` · ${t.detail}` : ""}
                </p>
              </li>
            ))}
            {timeline.length === 0 && <li className="text-[13px]" style={{ color: "var(--text-muted)" }}>No events recorded.</li>}
          </ol>
        </Card>
      )}
    </div>
  );
}

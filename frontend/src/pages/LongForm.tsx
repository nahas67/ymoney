import { useCallback, useEffect, useState } from "react";
import { useNavigate } from "react-router-dom";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, PageHeader, toast } from "../components/ui";

const FORMATS = ["EXPLAINER", "DOCUMENTARY", "VIDEO_ESSAY", "EDUCATIONAL", "TUTORIAL",
  "NEWS_ANALYSIS", "FACELESS", "LISTICLE", "PRODUCT_EXPLAINER", "STORYTELLING", "PODCAST_STYLE"];

export default function LongForm() {
  const nav = useNavigate();
  const lib = useFetch(() => wsApi.get(`/long-form/projects`), []);
  const [sel, setSel] = useState<string | null>(null);
  const [showForm, setShowForm] = useState(false);

  return (
    <div className="space-y-4">
      <PageHeader title="Long-Form Studio" subtitle="Autonomous multi-chapter video projects on the canonical timeline."
        actions={<button className="btn-primary !text-xs" onClick={() => setShowForm((v) => !v)}>
          {showForm ? "Close" : "+ New long-form project"}</button>} />
      {showForm && <CreateForm onDone={(id) => { setShowForm(false); setSel(id); lib.reload(); }} />}
      <div className="grid lg:grid-cols-[320px_1fr] gap-4">
        <Card pad={false} className="overflow-hidden">
          {(lib.data as any)?.items?.map((p: any) => (
            <button key={p.id} onClick={() => setSel(p.id)} className="w-full text-left px-3 py-2.5"
              style={{ borderBottom: "var(--seam)", background: sel === p.id ? "var(--seam)" : undefined }}>
              <div className="text-[13px] font-medium line-clamp-1">{p.topic}</div>
              <div className="flex gap-2 mt-1 items-center">
                <Badge tone={p.status === "COMPLETE" ? "success" : p.status === "FAILED" ? "error" : "info"}>{p.status}</Badge>
                <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>
                  {p.content_format} · {Math.round(p.target_duration_seconds / 60)}min · {p.stage}
                </span>
              </div>
            </button>
          ))}
          {!(lib.data as any)?.items?.length && !lib.loading && (
            <div className="p-4 text-[13px]" style={{ color: "var(--text-muted)" }}>No long-form projects yet.</div>
          )}
        </Card>
        {sel ? <ProjectDetail key={sel} projectId={sel} onOpenEditor={(tid) => nav(`/editor/${tid}`)} />
          : <Card><div className="text-[13px]" style={{ color: "var(--text-muted)" }}>Select a project to inspect stages, artifacts, and renders.</div></Card>}
      </div>
    </div>
  );
}

function CreateForm({ onDone }: { onDone: (id: string) => void }) {
  const [f, setF] = useState<any>({ topic: "", content_format: "EXPLAINER", minutes: 10, autonomy: "AUTO", budget_strategy: "BALANCED", aspect_ratio: "16:9" });
  const [busy, setBusy] = useState(false);
  const set = (k: string, v: any) => setF({ ...f, [k]: v });

  async function submit() {
    if (f.topic.trim().length < 3) {
      toast("Topic needs at least 3 characters", "warning");
      return;
    }
    setBusy(true);
    try {
      const r: any = await wsApi.post(`/long-form/projects`, {
        topic: f.topic.trim(), content_format: f.content_format,
        target_duration_seconds: Math.round(Number(f.minutes) * 60),
        autonomy: f.autonomy, budget_strategy: f.budget_strategy, aspect_ratio: f.aspect_ratio,
      });
      const est: any = await wsApi.get(`/long-form/projects/${r.id}/estimate`).catch(() => null);
      if (est) {
        const t = Object.values(est.estimates_usd ?? {}).reduce((a: number, b: any) => a + Number(b || 0), 0);
        toast(`Estimated ~$${Number(t).toFixed(3)} — starting durable chain`, "success");
      }
      await wsApi.post(`/long-form/projects/${r.id}/generate`, {});
      onDone(r.id);
    } catch (e: any) {
      toast(e.message, "error", "Create failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div className="grid md:grid-cols-3 gap-2">
        <input className="input md:col-span-3" placeholder="Topic — e.g. How rivers shape cities" value={f.topic} onChange={(e) => set("topic", e.target.value)} />
        <label className="block text-[12px]">Format
          <select className="select mt-0.5" value={f.content_format} onChange={(e) => set("content_format", e.target.value)}>
            {FORMATS.map((x) => <option key={x} value={x}>{x}</option>)}
          </select>
        </label>
        <label className="block text-[12px]">Minutes (3–60)
          <input type="number" className="input mt-0.5" value={f.minutes} min={3} max={60} onChange={(e) => set("minutes", e.target.value)} />
        </label>
        <label className="block text-[12px]">Autonomy
          <select className="select mt-0.5" value={f.autonomy} onChange={(e) => set("autonomy", e.target.value)}>
            <option value="AUTO">AUTO — run all safe stages</option>
            <option value="REVIEW">REVIEW — pause at script + render</option>
            <option value="MANUAL">MANUAL — advance each stage</option>
          </select>
        </label>
        <label className="block text-[12px]">Budget
          <select className="select mt-0.5" value={f.budget_strategy} onChange={(e) => set("budget_strategy", e.target.value)}>
            <option value="ECONOMY">ECONOMY — stock/local/graphics</option>
            <option value="BALANCED">BALANCED — + AI images</option>
            <option value="PREMIUM">PREMIUM — + AI video candidates</option>
          </select>
        </label>
        <label className="block text-[12px]">Aspect
          <select className="select mt-0.5" value={f.aspect_ratio} onChange={(e) => set("aspect_ratio", e.target.value)}>
            <option value="16:9">16:9 landscape</option>
            <option value="9:16">9:16 vertical</option>
            <option value="1:1">1:1 square</option>
          </select>
        </label>
        <div className="flex items-end">
          <button className="btn-primary !text-xs" disabled={busy} onClick={submit}>
            {busy ? "Creating…" : "Create + generate"}
          </button>
        </div>
      </div>
    </Card>
  );
}

function ProjectDetail({ projectId, onOpenEditor }: { projectId: string; onOpenEditor: (tid: string) => void }) {
  const detail = useFetch(() => wsApi.get(`/long-form/projects/${projectId}`), [projectId]);
  const [prog, setProg] = useState<any>(null);
  const [busy, setBusy] = useState("");
  const p: any = detail.data ?? prog;

  const refresh = useCallback(async () => {
    try {
      const r: any = await wsApi.get(`/long-form/projects/${projectId}/progress`);
      setProg((prev: any) => ({ ...(detail.data ?? prev ?? {}), ...r }));
    } catch { /* noop */ }
  }, [projectId, detail.data]);

  useEffect(() => {
    if ((detail.data as any)?.status !== "RUNNING") return;
    const t = setInterval(() => {
      refresh().then(() => {
        wsApi.get(`/long-form/projects/${projectId}`).then((d: any) => {
          if (d.status !== "RUNNING") {
            detail.reload();
            clearInterval(t);
          }
        }).catch(() => undefined);
      });
    }, 3000);
    return () => clearInterval(t);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [(detail.data as any)?.status]);

  async function act(kind: "advance" | "cancel" | "regen") {
    setBusy(kind);
    try {
      if (kind === "regen") {
        try {
          await wsApi.post(`/long-form/projects/${projectId}/regenerate-timeline`, { confirm: false });
        } catch (e: any) {
          if (e.status === 409 && e.message?.includes?.("manually edited")) {
            if (!window.confirm("Timeline was manually edited. Rebuild anyway? (A snapshot version is kept.)")) return;
            await wsApi.post(`/long-form/projects/${projectId}/regenerate-timeline`, { confirm: true });
          } else {
            throw e;
          }
        }
        toast("Regeneration queued", "success");
      } else {
        await wsApi.post(`/long-form/projects/${projectId}/${kind}`, {});
        toast(kind === "cancel" ? "Cancelled" : "Advanced", "success");
      }
      detail.reload();
    } catch (e: any) {
      toast(e.message, "error");
    } finally {
      setBusy("");
    }
  }

  if (detail.loading && !p?.id) return <Card>Loading…</Card>;
  if (detail.error && !p?.id) return <Card>Error: {detail.error}</Card>;
  if (!p) return null;
  const stages: string[] = prog?.stages ?? [];
  const curIdx = stages.indexOf(p.stage);

  return (
    <div className="space-y-3">
      <Card>
        <div className="flex gap-2 items-center flex-wrap">
          <b className="text-[14px]">{p.topic}</b>
          <Badge tone={p.status === "COMPLETE" ? "success" : p.status === "FAILED" ? "error" : "info"}>{p.status}</Badge>
          <span className="font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>
            {p.stage} · ${(p.cost_usd ?? 0).toFixed(4)} spent
          </span>
          <span className="flex-1" />
          {(p.status === "WAITING_REVIEW" || p.autonomy === "MANUAL") && (
            <button className="btn-primary !text-xs" disabled={busy === "advance"} onClick={() => act("advance")}>Advance stage</button>
          )}
          {p.status === "RUNNING" && (
            <button className="btn-outline !text-xs" disabled={busy === "cancel"} onClick={() => act("cancel")}>Cancel</button>
          )}
          {p.timeline_id && (
            <>
              <button className="btn-primary !text-xs" onClick={() => onOpenEditor(p.timeline_id)}>🎞 Open in Editor</button>
              <button className="btn-ghost !text-xs" disabled={busy === "regen"} onClick={() => act("regen")}>Rebuild timeline</button>
            </>
          )}
        </div>
        {p.error && <div className="text-[12.5px] mt-2" style={{ color: "var(--warn)" }}>{p.error}</div>}
        <div className="flex gap-1 mt-3 flex-wrap">
          {stages.map((s, i) => (
            <span key={s} title={s} className="font-mono text-[10px] px-1.5 py-0.5 rounded"
              style={{
                background: i < curIdx || p.status === "COMPLETE" ? "var(--accent)" : i === curIdx ? "var(--warn)" : "var(--seam)",
                color: i <= curIdx || p.status === "COMPLETE" ? "#04120a" : "var(--text-faint)",
              }}>
              {s}
            </span>
          ))}
        </div>
        {p.progress && Object.keys(p.progress).length > 0 && (
          <div className="font-mono text-[11.5px] mt-2" style={{ color: "var(--text-muted)" }}>
            {Object.entries(p.progress).map(([k, v]) => `${k}:${v}`).join(" · ")}
          </div>
        )}
      </Card>

      {(p.chapters ?? []).length > 0 && (
        <Card>
          <b className="text-[13px]">Chapters ({p.chapters.length})</b>
          {p.chapters.map((c: any) => (
            <div key={c.id} className="flex gap-2 py-1.5 text-[12.5px]" style={{ borderBottom: "var(--seam)" }}>
              <span className="font-mono" style={{ color: "var(--text-faint)" }}>{String(c.index + 1).padStart(2, "0")}</span>
              <div>
                <b>{c.title}</b>
                <span style={{ color: "var(--text-muted)" }}> — {c.role} · {c.segments} segs · target {c.target_s}s
                  {c.measured?.[0] != null && ` · measured ${c.measured[0].toFixed(0)}–${c.measured[1].toFixed(0)}s`}
                </span>
              </div>
            </div>
          ))}
        </Card>
      )}

      <div className="grid md:grid-cols-2 gap-3">
        <Card>
          <b className="text-[13px]">Artifacts</b>
          <ArtifactRow label="Research claims" value={p.artifacts?.research?.claims} />
          <ArtifactRow label="Script segments / words" value={p.artifacts?.script ? `${p.artifacts.script.segments} / ${p.artifacts.script.words}` : "—"} />
          <ArtifactRow label="Fact verdicts" value={p.artifacts?.fact_report?.verdicts ? JSON.stringify(p.artifacts.fact_report.verdicts) : "—"} />
          <ArtifactRow label="Voice measured" value={p.artifacts?.voice?.measured_seconds ? `${Number(p.artifacts.voice.measured_seconds).toFixed(0)}s` : "—"} />
          <ArtifactRow label="QC" value={p.artifacts?.qc?.result} />
          <ArtifactRow label="Render chunks" value={p.artifacts?.render?.chunks?.length} />
        </Card>
        <Card>
          <b className="text-[13px]">Metadata</b>
          {(p.artifacts?.metadata?.titles ?? []).slice(0, 4).map((t: any, i: number) => (
            <div key={i} className="text-[12.5px] py-1" style={{ borderBottom: "var(--seam)" }}>
              {t.title} <Badge tone="info">{t.intent}</Badge>
            </div>
          ))}
          {(p.artifacts?.metadata?.chapters ?? []).slice(0, 6).map((c: any, i: number) => (
            <div key={i} className="font-mono text-[11.5px]" style={{ color: "var(--text-muted)" }}>{c.at} {c.title}</div>
          ))}
          {(p.artifacts?.metadata?.thumbnails ?? []).length > 0 && (
            <div className="text-[12px] mt-1" style={{ color: "var(--text-muted)" }}>
              {p.artifacts.metadata.thumbnails.length} thumbnail candidates stored as assets.
            </div>
          )}
          {!((p.artifacts?.metadata?.titles ?? []).length) && (
            <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>Metadata generates after render.</div>
          )}
        </Card>
      </div>
    </div>
  );
}

function ArtifactRow({ label, value }: { label: string; value: any }) {
  return (
    <div className="flex justify-between py-1 text-[12.5px]" style={{ borderBottom: "var(--seam)" }}>
      <span style={{ color: "var(--text-muted)" }}>{label}</span>
      <span className="font-mono">{value ?? "—"}</span>
    </div>
  );
}

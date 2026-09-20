import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, Modal, PageHeader, Section, Tabs, statusTone } from "../components/ui";
import { fmtAgo } from "../lib/format";

export default function Assets() {
  const [tab, setTab] = useState<"library" | "images" | "repurpose" | "motion" | "templates">("library");
  const lib = useFetch(() => wsApi.get("/assets"), [tab]);
  const imgStatus = useFetch(() => wsApi.get("/assets/images/status"), [tab]);
  const clipStatus = useFetch(() => wsApi.get("/repurpose/status"), [tab]);
  const motionStatus = useFetch(() => wsApi.get("/assets/motion/status"), [tab]);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState("");
  const [prompt, setPrompt] = useState("");
  const [genImgs, setGenImgs] = useState<string[]>([]);
  const [src, setSrc] = useState("");
  const [clips, setClips] = useState<any[]>([]);
  const [preset, setPreset] = useState("minimal");
  const [rank, setRank] = useState(true);
  const [mKind, setMKind] = useState("hook");
  const [mTitle, setMTitle] = useState("");
  const [mSub, setMSub] = useState("");
  const [mPath, setMPath] = useState("");

  const caps: any = (lib.data as any)?.capabilities;

  async function upload() {
    if (!file) return;
    setBusy("upload");
    try {
      await wsApi.upload("/assets/upload", file);
      setFile(null);
      lib.reload();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy("");
    }
  }

  async function generate() {
    if (!prompt.trim()) return;
    setBusy("gen");
    try {
      const r = await wsApi.post("/assets/images/generate", { prompt, n: 1 });
      setGenImgs(r.images ?? []);
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy("");
    }
  }

  async function repurpose() {
    if (!src.trim()) return;
    setBusy("clip");
    try {
      const r = await wsApi.post("/assets/repurpose", { source: src, max_clips: 5, rank, caption_preset: preset });
      setClips(r.clips ?? []);
      lib.reload();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy("");
    }
  }

  async function renderMotion() {
    if (!mTitle.trim()) return;
    setBusy("motion");
    try {
      const r = await wsApi.post("/assets/motion", { kind: mKind, title: mTitle, subtitle: mSub });
      setMPath(r.path ?? "");
      lib.reload();
    } catch (e: any) {
      alert(e.message);
    } finally {
      setBusy("");
    }
  }

  return (
    <div className="space-y-4">
      <PageHeader title="Assets" subtitle="System renders, operator uploads, AI scene images and long-form repurposing."
        actions={caps && <Badge tone={caps.upload ? "success" : "muted"}>{caps.upload ? "uploads on" : caps.note}</Badge>} />
      <Tabs tabs={[{ key: "library", label: "Library" }, { key: "images", label: "Generate images" }, { key: "repurpose", label: "Repurpose" }, { key: "motion", label: "Motion cards" }, { key: "templates", label: "Templates" }]}
        active={tab} onChange={setTab} />

      {tab === "library" && (
        <>
          <Card>
            <b className="text-[13.5px]">Upload a file</b>
            <div className="flex gap-2 mt-2 flex-wrap items-center">
              <input type="file" accept="video/mp4,video/quicktime,image/jpeg,image/png,audio/mpeg,audio/wav"
                onChange={(e) => setFile(e.target.files?.[0] ?? null)} aria-label="Upload asset" />
              <button className="btn-primary !text-xs" disabled={!file || busy === "upload"} onClick={upload}>
                {busy === "upload" ? "…" : "Upload (500MB max)"}
              </button>
            </div>
          </Card>
          <Section data={(lib.data as any)?.items} loading={lib.loading} error={lib.error} onRetry={lib.reload}
            empty="No assets yet" emptyHint="Renders land here automatically; uploads appear as type 'upload'.">
            {(list) => (
              <Card pad={false} className="overflow-x-auto">
                <table className="table">
                  <thead><tr><th>Title</th><th>Type</th><th>Engine</th><th>Status</th><th>Size</th><th>When</th></tr></thead>
                  <tbody>
                    {list.map((a: any) => (
                      <tr key={a.id}>
                        <td className="max-w-[320px] truncate">{a.title}</td>
                        <td><Badge tone={a.type === "upload" ? "info" : "muted"}>{a.type}</Badge></td>
                        <td className="font-mono text-[12px]">{a.engine}</td>
                        <td><Badge tone={statusTone(a.status)}>{a.status}</Badge></td>
                        <td className="font-mono text-[12px]">{a.size_bytes != null ? `${(a.size_bytes / 1024 / 1024).toFixed(1)}MB` : "—"}</td>
                        <td className="text-[12px]" style={{ color: "var(--text-muted)" }}>{a.created_at ? fmtAgo(a.created_at) : "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            )}
          </Section>
        </>
      )}

      {tab === "images" && (
        <Card>
          <b className="text-[13.5px]">Scene image generation</b>
          <div className="text-[12.5px] mt-1" style={{ color: "var(--text-muted)" }}>
            Provider: <b className="font-mono">{(imgStatus.data as any)?.provider ?? "…"}</b>
            {(imgStatus.data as any)?.is_mock ? " (labeled mock)" : ""}
          </div>
          <div className="flex gap-2 mt-3">
            <input className="input" placeholder="e.g. neon stock chart rising over a city skyline" value={prompt} onChange={(e) => setPrompt(e.target.value)} />
            <button className="btn-primary !text-xs whitespace-nowrap" disabled={busy === "gen"} onClick={generate}>{busy === "gen" ? "…" : "Generate"}</button>
          </div>
          {genImgs.length > 0 && <div className="text-[12.5px] mt-2 font-mono break-words" style={{ color: "var(--text-muted)" }}>Stored: {genImgs.join(", ")}</div>}
        </Card>
      )}

      {tab === "repurpose" && (
        <Card>
          <b className="text-[13.5px]">Cut long-form into vertical shorts</b>
          <div className="text-[12.5px] mt-1" style={{ color: "var(--text-muted)" }}>
            {(clipStatus.data as any)?.ffmpeg ? "ffmpeg ready" : "ffmpeg missing"} · {(clipStatus.data as any)?.yt_dlp ? "yt-dlp ready" : "yt-dlp missing (URLs unavailable)"}
          </div>
          <div className="flex gap-2 mt-3 flex-wrap">
            <input className="input flex-1 min-w-[200px]" placeholder="YouTube URL or local file path" value={src} onChange={(e) => setSrc(e.target.value)} />
            <select className="select !w-32" value={preset} onChange={(e) => setPreset(e.target.value)} aria-label="Caption preset">
              {["minimal", "pop", "karaoke"].map((p) => <option key={p} value={p}>{p}</option>)}
            </select>
            <label className="flex items-center gap-1.5 text-[12.5px]" style={{ color: "var(--text-muted)" }}>
              <input type="checkbox" checked={rank} onChange={(e) => setRank(e.target.checked)} /> rank moments
            </label>
            <button className="btn-primary !text-xs whitespace-nowrap" disabled={busy === "clip"} onClick={repurpose}>{busy === "clip" ? "Cutting…" : "Cut clips"}</button>
          </div>
          {clips.length > 0 && (
            <div className="mt-3 space-y-2">
              {clips.map((c: any, i: number) => (
                <div key={i} className="rounded-lg p-2.5" style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
                  <div className="flex gap-2 items-center flex-wrap">
                    <b className="font-mono text-[12.5px]">🔥 {(c.score ?? 0).toFixed(0)}</b>
                    <span className="text-[12.5px]">{c.hook || `clip ${i + 1}`}</span>
                    {c.preset && <span className="font-mono text-[10.5px]" style={{ color: "var(--text-faint)" }}>{c.preset}</span>}
                  </div>
                  {c.reason && <div className="text-[11.5px]" style={{ color: "var(--text-muted)" }}>{c.reason}</div>}
                  <div className="text-[11.5px] font-mono" style={{ color: "var(--text-faint)" }}>
                    {c.start?.toFixed(0)}s → {c.end?.toFixed(0)}s · {c.resolution} · {c.path}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Card>
      )}
      {tab === "motion" && (
        <Card>
          <b className="text-[13.5px]">Kinetic motion card (HyperFrames)</b>
          <div className="text-[12.5px] mt-1" style={{ color: "var(--text-muted)" }}>
            {(motionStatus.data as any)?.ready
              ? `ready · ${(motionStatus.data as any)?.version ?? ""} · hook/stat/CTA/lower-third in 9:16`
              : "not ready here — needs HyperFrames CLI + working Chrome + ffmpeg. Renders fail closed with remediation."}
          </div>
          <div className="grid md:grid-cols-[140px_1fr_1fr_auto] gap-2 mt-3">
            <select className="select" value={mKind} onChange={(e) => setMKind(e.target.value)} aria-label="Card kind">
              {["hook", "stat", "cta", "lower"].map((k) => <option key={k} value={k}>{k}</option>)}
            </select>
            <input className="input" placeholder="Title (e.g. Stop losing money)" value={mTitle} onChange={(e) => setMTitle(e.target.value)} />
            <input className="input" placeholder="Subtitle (optional)" value={mSub} onChange={(e) => setMSub(e.target.value)} />
            <button className="btn-primary !text-xs whitespace-nowrap" disabled={busy === "motion" || !mTitle.trim()} onClick={renderMotion}>
              {busy === "motion" ? "Rendering…" : "Render card"}
            </button>
          </div>
          {mPath && <div className="text-[12.5px] mt-2 font-mono break-words" style={{ color: "var(--accent)" }}>Rendered: {mPath}</div>}
        </Card>
      )}

      {tab === "templates" && <TemplatesTab />}
    </div>
  );
}

function TemplatesTab() {
  const [module, setModule] = useState("");
  const list = useFetch(() => wsApi.get(`/assets/templates${module ? `?module=${module}` : ""}`), [module]);
  const [open, setOpen] = useState<any>(null);
  const [overrideJson, setOverrideJson] = useState("");
  const [saving, setSaving] = useState(false);

  async function inspect(t: any) {
    const d = await wsApi.get(`/assets/templates/${t.module}/${t.id}`);
    setOpen(d);
    const s = await wsApi.get("/settings");
    const cur = (s.settings?.templates ?? {})[`${t.module}/${t.id}`] ?? {};
    setOverrideJson(JSON.stringify(cur, null, 2));
  }

  async function saveOverride() {
    if (!open) return;
    setSaving(true);
    try {
      const patch = overrideJson.trim() ? JSON.parse(overrideJson) : {};
      const s = await wsApi.get("/settings");
      const templates = { ...(s.settings?.templates ?? {}) };
      if (!Object.keys(patch).length) delete templates[`${open.module}/${open.id}`];
      else templates[`${open.module}/${open.id}`] = patch;
      await wsApi.put("/settings", { settings: { templates } });
      setOpen({ ...open, overridden: !!Object.keys(patch).length });
    } catch (e: any) {
      alert(e.message);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="space-y-3">
      <div className="flex gap-1.5 flex-wrap">
        {[["", "all"], ["captions", "captions"], ["hooks", "hooks"], ["motion", "motion"]].map(([k, label]) => (
          <button key={k} className={`tab ${module === k ? "active" : ""}`} onClick={() => setModule(k)}>{label}</button>
        ))}
      </div>
      <Section data={(list.data as any)?.items} loading={list.loading} error={list.error} onRetry={list.reload}
        empty="No templates" emptyHint="Built-ins ship with the backend; workspaces override them without touching files.">
        {(rows) => (
          <div className="grid md:grid-cols-2 gap-3">
            {rows.map((t: any) => (
              <Card key={`${t.module}/${t.id}`} style={{ padding: 14 }}>
                <div className="flex gap-2 items-center flex-wrap">
                  <Badge tone="muted">{t.module}</Badge>
                  <b className="text-[13.5px]">{t.title}</b>
                  <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{t.id} · {t.version}</span>
                  {t.overridden && <Badge tone="info">overridden</Badge>}
                  <button className="btn-ghost !text-[11px] !py-0.5 ml-auto" onClick={() => inspect(t)}>Inspect / override</button>
                </div>
                <div className="text-[12.5px] mt-1" style={{ color: "var(--text-muted)" }}>{t.description}</div>
              </Card>
            ))}
          </div>
        )}
      </Section>
      <Modal open={!!open} onClose={() => setOpen(null)} title={open ? `${open.module}/${open.id}` : ""} wide>
        {open && (
          <div className="space-y-3">
            <div className="text-[12.5px]" style={{ color: "var(--text-muted)" }}>
              Versions: {(open.versions ?? []).join(", ")} · {open.attribution ?? ""}
            </div>
            <div>
              <div className="panel-label mb-1">Resolved payload</div>
              <pre className="text-[12px] font-mono whitespace-pre-wrap p-3 rounded-lg max-h-[220px] overflow-y-auto" style={{ background: "var(--bg-inset)", border: "var(--seam)" }}>
                {JSON.stringify(open.payload, null, 2)}
              </pre>
            </div>
            <div>
              <div className="panel-label mb-1">Workspace override (JSON patch — empty clears)</div>
              <textarea className="textarea font-mono !text-xs" rows={6} value={overrideJson} onChange={(e) => setOverrideJson(e.target.value)}
                placeholder='{"payload": {"accent": "#f59e0b"}}' />
            </div>
            <button className="btn-primary !text-xs" disabled={saving} onClick={saveOverride}>Save override</button>
          </div>
        )}
      </Modal>
    </div>
  );
}

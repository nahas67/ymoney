import { useState } from "react";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, PageHeader, Section, Tabs, statusTone } from "../components/ui";
import { fmtAgo } from "../lib/format";

export default function Assets() {
  const [tab, setTab] = useState<"library" | "images" | "repurpose">("library");
  const lib = useFetch(() => wsApi.get("/assets"), [tab]);
  const imgStatus = useFetch(() => wsApi.get("/assets/images/status"), [tab]);
  const clipStatus = useFetch(() => wsApi.get("/repurpose/status"), [tab]);
  const [file, setFile] = useState<File | null>(null);
  const [busy, setBusy] = useState("");
  const [prompt, setPrompt] = useState("");
  const [genImgs, setGenImgs] = useState<string[]>([]);
  const [src, setSrc] = useState("");
  const [clips, setClips] = useState<any[]>([]);
  const [preset, setPreset] = useState("minimal");
  const [rank, setRank] = useState(true);

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

  return (
    <div className="space-y-4">
      <PageHeader title="Assets" subtitle="System renders, operator uploads, AI scene images and long-form repurposing."
        actions={caps && <Badge tone={caps.upload ? "success" : "muted"}>{caps.upload ? "uploads on" : caps.note}</Badge>} />
      <Tabs tabs={[{ key: "library", label: "Library" }, { key: "images", label: "Generate images" }, { key: "repurpose", label: "Repurpose" }]}
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
    </div>
  );
}

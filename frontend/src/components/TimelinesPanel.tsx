import { useState } from "react";
import { useNavigate } from "react-router-dom";
import { wsApi } from "../lib/api";
import { useFetch } from "../hooks/hooks";
import { Badge, Card, toast } from "./ui";

/** Minimal timeline foundation UI: load, visualize tracks, rename, create.
 *  Full multi-track editing (split/trim/waveform) is the next phase; this
 *  panel proves the canonical backend timeline round-trips through the UI. */
export default function TimelinesPanel({ contentId, videoId }: { contentId: string; videoId?: string | null }) {
  const nav = useNavigate();
  const lib = useFetch(() => wsApi.get(`/timelines?content_item_id=${contentId}`), [contentId]);
  const [sel, setSel] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);

  const items: any[] = (lib.data as any)?.items ?? [];
  const cur = items.find((t) => t.id === sel) ?? items[0] ?? null;
  const dur = Math.max(Number(cur?.duration_seconds ?? 0), 0.001);

  async function create(fromVideo: boolean) {
    setBusy(true);
    try {
      if (fromVideo && videoId) {
        await wsApi.post(`/timelines/from-video`, { video_id: videoId, aspect: "9:16" });
      } else {
        await wsApi.post(`/timelines`, { name: "main", content_item_id: contentId });
      }
      lib.reload();
      toast("Timeline created", "success");
    } catch (e: any) {
      toast(e.message, "error", "Create failed");
    } finally {
      setBusy(false);
    }
  }

  async function rename() {
    if (!cur || !name.trim()) return;
    setBusy(true);
    try {
      await wsApi.put(`/timelines/${cur.id}`, { name: name.trim() });
      setName("");
      lib.reload();
      toast("Renamed", "success");
    } catch (e: any) {
      toast(e.message, "error", "Save failed");
    } finally {
      setBusy(false);
    }
  }

  return (
    <Card>
      <div className="flex gap-2 items-center flex-wrap mb-3">
        <b className="text-[14px]">Timelines ({items.length})</b>
        <button className="btn-outline !text-xs" disabled={busy} onClick={() => create(false)}>+ Empty</button>
        {videoId && <button className="btn-outline !text-xs" disabled={busy} onClick={() => create(true)}>Import render</button>}
        {cur && (
          <>
            <input className="input !w-48" placeholder="Rename timeline…" value={name} onChange={(e) => setName(e.target.value)} />
            <button className="btn-primary !text-xs" disabled={busy || !name.trim()} onClick={rename}>Save</button>
            <button className="btn-primary !text-xs" onClick={() => nav(`/editor/${cur.id}`)}>Open full editor →</button>
          </>
        )}
      </div>
      {lib.loading && <div className="text-[13px]">Loading…</div>}
      {lib.error && <div className="text-[13px]">Error: {lib.error}</div>}
      {!lib.loading && !items.length && <div className="text-[13px]" style={{ color: "var(--text-muted)" }}>No timelines yet — create one or import the render.</div>}
      {items.length > 1 && (
        <div className="flex gap-2 flex-wrap mb-3">
          {items.map((t: any) => (
            <button key={t.id} className={cur?.id === t.id ? "btn-primary !text-xs" : "btn-ghost !text-xs"} onClick={() => setSel(t.id)}>
              {t.name} <span className="font-mono">v{t.version}</span>
            </button>
          ))}
        </div>
      )}
      {cur && (
        <div className="space-y-1.5">
          <div className="flex gap-2 items-center text-[12px]" style={{ color: "var(--text-muted)" }}>
            <Badge tone="info">v{cur.version}</Badge>
            <span className="font-mono">{cur.duration_seconds?.toFixed?.(1) ?? 0}s · {cur.aspect_ratio} · {cur.fps}fps</span>
          </div>
          {(cur.tracks ?? []).map((tr: any) => (
            <div key={tr.id} className="flex items-center gap-2">
              <span className="font-mono text-[11px] w-16 shrink-0" style={{ color: "var(--text-faint)" }}>{tr.kind}</span>
              <div className="relative flex-1 h-5 rounded" style={{ background: "var(--seam)" }}>
                {(tr.clips ?? []).map((c: any) => (
                  <div key={c.id} title={`${c.name} (${c.start}s +${c.duration}s)`} className="absolute top-0.5 bottom-0.5 rounded"
                    style={{ left: `${(c.start / dur) * 100}%`, width: `${Math.max((c.duration / dur) * 100, 1.5)}%`, background: "var(--accent)" }} />
                ))}
              </div>
              <span className="font-mono text-[11px]" style={{ color: "var(--text-faint)" }}>{(tr.clips ?? []).length}</span>
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}

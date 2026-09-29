import { memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import WaveSurfer from "wavesurfer.js";
import { getToken, mediaFileUrl, videoFileUrl, wsApi } from "../lib/api";
import { Badge, Card, PageHeader, toast } from "../components/ui";
import CreativeDirector from "../components/CreativeDirector";
import CommentsPanel from "../components/collab/CommentsPanel";
import ConflictNotice from "../components/collab/ConflictNotice";
import ReviewStatusBar from "../components/collab/ReviewStatusBar";
import VersionCompare from "../components/collab/VersionCompare";
import {
  TRACK_FAMILY, applyOpsLocal, clipEnd, findClip, inverseOps, snapTime, sortedTracks,
} from "../editor/adapters/timelineAdapter";

type Sel = { track: string; clipId: string } | null;
type UndoEntry = { label: string; forward: any[]; inverse: any[] };
type SaveState = "Saved" | "Saving…" | "Unsaved changes" | "Save failed" | "Conflict";

const KIND_ICON: Record<string, string> = {
  video: "🎥", broll: "🎬", avatar: "👤", text: "📝",
  caption: "💬", voice: "🗣", music: "🎵", sfx: "💥",
};

function fmt(t: number): string {
  const m = Math.floor(t / 60);
  const s = (t % 60).toFixed(1).padStart(4, "0");
  return `${String(m).padStart(2, "0")}:${s}`;
}

/** Parsed 409 body ({error, expected_version, actual_version}), if any. */
function parseConflict(message?: string): any {
  try {
    const parsed = JSON.parse(message ?? "");
    return parsed && typeof parsed === "object" ? parsed : null;
  } catch {
    return null;
  }
}

let idSeq = 0;
const nid = (p: string) => `${p}_${Date.now().toString(36)}_${idSeq++}`;

export default function Editor() {
  const { timelineId } = useParams();
  const nav = useNavigate();
  const [doc, setDoc] = useState<any>(null);
  const [version, setVersion] = useState(1);
  const [loadError, setLoadError] = useState("");
  const [sel, setSel] = useState<Sel>(null);
  const [time, setTime] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [pxPerSec, setPxPerSec] = useState(40);
  const [snap, setSnap] = useState(true);
  const [saveState, setSaveState] = useState<SaveState>("Saved");
  const [conflict, setConflict] = useState<any>(null);
  const [undo, setUndo] = useState<UndoEntry[]>([]);
  const [redo, setRedo] = useState<UndoEntry[]>([]);
  const [versions, setVersions] = useState<any[]>([]);
  const [showVersions, setShowVersions] = useState(false);
  const [showComments, setShowComments] = useState(false);
  const [showCompare, setShowCompare] = useState(false);
  const [scenes, setScenes] = useState<any[]>([]);
  const [assets, setAssets] = useState<any[]>([]);
  const [assetQ, setAssetQ] = useState("");
  const [rendering, setRendering] = useState(false);
  const [renderOut, setRenderOut] = useState<any>(null);

  const docRef = useRef<any>(null);
  const versionRef = useRef(1);
  const pendingRef = useRef<any[]>([]);
  const saveTimer = useRef<any>(null);
  const playRef = useRef<number | null>(null);
  const lastTick = useRef(0);
  const mediaRefs = useRef<Map<string, HTMLMediaElement>>(new Map());
  const waveRef = useRef<HTMLDivElement>(null);
  const wave = useRef<WaveSurfer | null>(null);
  const timeRef = useRef(0);
  const scenesRef = useRef<any[]>([]);
  const snapRef = useRef(true);

  docRef.current = doc;
  timeRef.current = time;
  scenesRef.current = scenes;
  snapRef.current = snap;

  const load = useCallback(async () => {
    try {
      const d: any = await wsApi.get(`/timelines/${timelineId}`);
      setDoc(d);
      setVersion(d.version);
      versionRef.current = d.version;
      pendingRef.current = [];
      setUndo([]);
      setRedo([]);
      setSaveState("Saved");
      setConflict(null);
      const v: any = await wsApi.get(`/timelines/${timelineId}/versions`);
      setVersions(v.versions ?? []);
      const s: any = await wsApi.get(`/timelines/${timelineId}/scenes`);
      setScenes(s.scenes ?? []);
      const a: any = await wsApi.get(`/assets/media?limit=200`);
      setAssets(a.items ?? []);
    } catch (e: any) {
      setLoadError(e.message);
    }
  }, [timelineId]);

  useEffect(() => {
    load();
  }, [load]);

  const tracks = useMemo(() => (doc ? sortedTracks(doc) : []), [doc]);
  const duration = Math.max(Number(doc?.duration_seconds ?? 0), 1);
  const selClip = sel ? findClip(doc, sel.track, sel.clipId) : null;

  const flushSave = useCallback(async () => {
    const ops = pendingRef.current;
    if (!ops.length) return;
    pendingRef.current = [];
    setSaveState("Saving…");
    try {
      const res: any = await wsApi.post(`/timelines/${timelineId}/operations`, {
        base_version: versionRef.current, operations: ops,
      });
      setDoc(res);
      setVersion(res.version);
      versionRef.current = res.version;
      setSaveState("Saved");
      setConflict(null);
    } catch (e: any) {
      if (e.status === 409) {
        // Optimistic-concurrency conflict: the server moved on. Keep the local
        // tracks on screen (user intent stays visible) and require an explicit
        // reload — never a silent last-write-wins overwrite.
        const detail = parseConflict(e.message);
        setConflict(detail);
        setSaveState("Conflict");
        toast("Someone else saved this timeline — reload latest to continue",
          "warning", "Edit conflict");
      } else {
        setSaveState("Save failed");
        toast(e.message, "error", "Autosave failed — reloading server state");
        await load();
      }
    }
  }, [timelineId, load]);

  const scheduleSave = useCallback((ops: any[]) => {
    pendingRef.current = [...pendingRef.current, ...ops];
    setSaveState("Unsaved changes");
    clearTimeout(saveTimer.current);
    saveTimer.current = setTimeout(flushSave, 800);
  }, [flushSave]);

  const onSelectClip = useCallback((track: string, clipId: string) => {
    setSel({ track, clipId });
  }, []);

  const getSnap = useCallback(() => ({
    doc: docRef.current, time: timeRef.current, scenes: scenesRef.current, snap: snapRef.current,
  }), []);

  const commitOps = useCallback((ops: any[], label: string) => {
    if (!docRef.current || !ops.length) return;
    const inv = inverseOps(docRef.current, ops);
    const next = applyOpsLocal(docRef.current, ops);
    setDoc(next);
    setUndo((u) => [...u.slice(-49), { label, forward: ops, inverse: inv }]);
    setRedo([]);
    scheduleSave(ops);
  }, [scheduleSave]);

  const doUndo = useCallback(() => {
    const entry = undo[undo.length - 1];
    if (!entry || !docRef.current) return;
    setDoc(applyOpsLocal(docRef.current, entry.inverse));
    setUndo((u) => u.slice(0, -1));
    setRedo((r) => [...r, entry]);
    scheduleSave(entry.inverse);
  }, [undo, scheduleSave]);

  const doRedo = useCallback(() => {
    const entry = redo[redo.length - 1];
    if (!entry || !docRef.current) return;
    setDoc(applyOpsLocal(docRef.current, entry.forward));
    setRedo((r) => r.slice(0, -1));
    setUndo((u) => [...u, entry]);
    scheduleSave(entry.forward);
  }, [redo, scheduleSave]);

  // ---- preview clock ----
  useEffect(() => {
    if (!playing) {
      if (playRef.current) cancelAnimationFrame(playRef.current);
      playRef.current = null;
      mediaRefs.current.forEach((m) => { try { m.pause(); } catch { /* noop */ } });
      return;
    }
    lastTick.current = performance.now();
    const tick = (now: number) => {
      const dt = (now - lastTick.current) / 1000;
      lastTick.current = now;
      setTime((t) => {
        const nt = t + dt;
        if (nt >= duration) {
          setPlaying(false);
          return duration;
        }
        return nt;
      });
      playRef.current = requestAnimationFrame(tick);
    };
    playRef.current = requestAnimationFrame(tick);
    mediaRefs.current.forEach((m) => { try { void m.play().catch(() => undefined); } catch { /* noop */ } });
    return () => {
      if (playRef.current) cancelAnimationFrame(playRef.current);
    };
  }, [playing, duration]);

  const seek = useCallback((t: number) => {
    const nt = Math.max(0, Math.min(duration, t));
    setTime(nt);
    mediaRefs.current.forEach((m) => {
      try {
        if (Math.abs(m.currentTime - nt) > 0.35) m.currentTime = nt;
      } catch { /* noop */ }
    });
    wave.current?.setTime(nt);
  }, [duration]);

  // ---- waveform for selected audio clip ----
  const selAudioUrl = useMemo(() => {
    if (!selClip || !sel) return null;
    const fam = TRACK_FAMILY[sel.track];
    if (fam !== "audio") return null;
    const src = selClip.source ?? {};
    if (src.asset_id) return mediaFileUrl(src.asset_id);
    return null;
  }, [selClip, sel]);

  useEffect(() => {
    if (!waveRef.current) return;
    wave.current?.destroy();
    wave.current = null;
    if (!selAudioUrl) return;
    const ws = WaveSurfer.create({
      container: waveRef.current, url: selAudioUrl,
      waveColor: "#4ade80", progressColor: "#16a34a", height: 64,
    });
    ws.on("click", (rel: number) => {
      if (selClip) seek(selClip.start + rel * selClip.duration);
    });
    wave.current = ws;
    return () => {
      ws.destroy();
    };
  }, [selAudioUrl, seek, selClip]);

  // ---- editing actions ----
  function clipAt(kind: string, t: number): any | null {
    const tr = tracks.find((x) => x.kind === kind);
    return tr?.clips.find((c) => t >= c.start && t < clipEnd(c)) ?? null;
  }

  function splitAtPlayhead() {
    if (!doc) return;
    let target: { track: string; clip: any } | null = null;
    for (const k of ["video", "broll", "avatar", "voice"]) {
      const c = clipAt(k, time);
      if (c && time > c.start + 0.05 && time < clipEnd(c) - 0.05) {
        target = { track: k, clip: c };
        break;
      }
    }
    if (!target) {
      toast("Playhead is not inside a splittable clip", "warning");
      return;
    }
    commitOps([{ type: "split_item", track: target.track, clip_id: target.clip.id, at: time }], "Split");
  }

  function onDropAsset(e: React.DragEvent, trackKind: string) {
    e.preventDefault();
    const assetId = e.dataTransfer.getData("text/ym-asset");
    if (!assetId || !doc) return;
    const asset = assets.find((a) => a.id === assetId);
    if (!asset) return;
    const fam = TRACK_FAMILY[trackKind];
    const ok = (fam === "visual" && asset.type.startsWith("video")) ||
      (fam === "visual" && asset.type.includes("image")) ||
      (fam === "audio" && ["audio", "voice", "music"].some((k) => asset.type.includes(k))) ||
      fam === "overlay";
    if (!ok) {
      toast(`Asset type ${asset.type} cannot go on a ${trackKind} track`, "error");
      return;
    }
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    const at = snapTime((e.clientX - rect.left) / pxPerSec, doc, time, scenes, snap);
    commitOps([{
      type: "add_item", track: trackKind,
      clip: { id: nid("clip"), name: asset.storage_key.split("/").pop() ?? asset.id,
        start: at, duration: Math.min(asset.duration_seconds ?? 5, 10),
        source: { asset_id: asset.id }, effects: [] },
    }], "Add asset");
  }

  async function renderNow() {
    setRendering(true);
    try {
      const res: any = await wsApi.post(`/timelines/${timelineId}/render`, {});
      setRenderOut(res);
      toast("Render complete", "success");
    } catch (e: any) {
      toast(e.message, "error", "Render failed");
    } finally {
      setRendering(false);
    }
  }

  async function downloadExport(fmt: "otio" | "fcpxml") {
    try {
      const token = getToken();
      const ws = localStorage.getItem("ym_ws");
      const res = await fetch(`/api/v1/workspaces/${ws}/timelines/${timelineId}/export/${fmt}`, {
        headers: token ? { Authorization: `Bearer ${token}` } : {},
      });
      if (res.status === 501) {
        const body = await res.json();
        toast(body.detail?.reason ?? "Not available", "warning", "Export unavailable");
        return;
      }
      if (!res.ok) throw new Error(res.statusText);
      const blob = await res.blob();
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = fmt === "otio" ? "timeline.otio" : "timeline.fcpxml";
      a.click();
      toast("Export downloaded", "success");
    } catch (e: any) {
      toast(e.message, "error", "Export failed");
    }
  }

  async function restoreVersion(v: any) {
    try {
      const res: any = await wsApi.post(`/timelines/${timelineId}/versions/${v.id}/restore`, {});
      setDoc(res);
      setVersion(res.version);
      versionRef.current = res.version;
      pendingRef.current = [];
      setUndo([]);
      setRedo([]);
      setSaveState("Saved");
      setConflict(null);
      const vv: any = await wsApi.get(`/timelines/${timelineId}/versions`);
      setVersions(vv.versions ?? []);
      toast("Version restored onto a new tip", "success");
    } catch (e: any) {
      toast(e.message, "error", "Restore failed");
    }
  }

  if (loadError) return <Card>Error: {loadError}</Card>;
  if (!doc) return <Card>Loading editor…</Card>;

  const activeVisual = (["avatar", "broll", "video"] as const)
    .map((k) => ({ k, c: clipAt(k, time) }))
    .find((x) => x.c)?.c ?? null;
  const activeCaptions = (tracks.find((t) => t.kind === "caption")?.clips ?? [])
    .filter((c) => time >= c.start && time < clipEnd(c));
  const activeTexts = (tracks.find((t) => t.kind === "text")?.clips ?? [])
    .filter((c) => time >= c.start && time < clipEnd(c));
  const audible = tracks
    .filter((t) => TRACK_FAMILY[t.kind] === "audio")
    .flatMap((t) => t.clips.filter((c) => time >= c.start && time < clipEnd(c))
      .map((c) => ({ track: t.kind, clip: c })));

  return (
    <div className="space-y-3">
      <PageHeader title={doc.name ?? "Editor"} subtitle={`v${version} · ${fmt(duration)} · ${saveState}`}
        actions={<>
          <button className="btn-ghost !text-xs" onClick={() => nav(-1)}>← Back</button>
          <button className="btn-outline !text-xs" onClick={() => downloadExport("otio")}>Export .otio</button>
          <button className="btn-outline !text-xs" onClick={() => downloadExport("fcpxml")}>Export FCPXML</button>
          <button className="btn-primary !text-xs" disabled={rendering} onClick={renderNow}>
            {rendering ? "Rendering…" : "Render MP4"}
          </button>
        </>} />
      {saveState === "Conflict" && (
        <ConflictNotice conflict={conflict} localVersion={version} onReload={() => void load()} />
      )}
      {saveState === "Save failed" && (
        <Card><b>Autosave failed.</b>
          <button className="btn-outline !text-xs ml-3" onClick={flushSave}>Retry save</button>
        </Card>
      )}
      {timelineId && <ReviewStatusBar timelineId={timelineId} version={version} />}

      <div className="grid xl:grid-cols-[280px_1fr_300px] gap-3">
        {/* Assets */}
        <Card>
          <b className="text-[13px]">Assets</b>
          <input className="input mt-2" placeholder="Filter…" value={assetQ} onChange={(e) => setAssetQ(e.target.value)} />
          <div className="mt-2 space-y-1 max-h-[420px] overflow-auto">
            {assets.filter((a) => (a.storage_key + a.type).toLowerCase().includes(assetQ.toLowerCase())).map((a) => (
              <div key={a.id} draggable onDragStart={(e) => e.dataTransfer.setData("text/ym-asset", a.id)}
                className="text-[12.5px] font-mono px-2 py-1 rounded cursor-grab" style={{ background: "var(--seam)" }}
                title="Drag onto a timeline track">
                {a.type} · {(a.storage_key ?? "").split("/").pop()}
              </div>
            ))}
            {!assets.length && <div className="text-[12.5px]" style={{ color: "var(--text-faint)" }}>No media assets yet.</div>}
          </div>
          {scenes.length > 0 && (
            <div className="mt-3">
              <b className="text-[13px]">Scenes ({scenes.length})</b>
              {scenes.map((s: any) => (
                <button key={s.id} className="btn-ghost !text-xs w-full text-left mt-1" onClick={() => seek(s.start_seconds)}>
                  #{s.index + 1} {s.title} <span className="font-mono">[{fmt(s.start_seconds)}–{fmt(s.end_seconds)}]</span>
                </button>
              ))}
            </div>
          )}
        </Card>

        {/* Preview */}
        <Card>
          <div className="relative w-full bg-black rounded-lg overflow-hidden" style={{ aspectRatio: "9/16", maxHeight: 460 }}>
            <PreviewMedia clip={activeVisual} mediaRefs={mediaRefs} time={time} />
            {activeTexts.map((c: any) => (
              <div key={c.id} className="absolute font-bold"
                style={{
                  left: `${c.transform?.x ?? 50}%`, top: `${c.transform?.y ?? 20}%`,
                  fontSize: `${(c.text?.size ?? 64) / 3}px`,
                  color: c.text?.color ?? "white", opacity: (c.transform?.opacity ?? 100) / 100,
                  transform: `translate(-50%,-50%) scale(${c.transform?.scale ?? 1}) rotate(${c.transform?.rotation ?? 0}deg)`,
                }}>
                {c.text?.content ?? c.name}
              </div>
            ))}
            {activeCaptions.map((c: any) => (
              <div key={c.id} className="absolute bottom-[8%] left-0 right-0 text-center font-bold"
                style={{ fontSize: 22, color: "white", textShadow: "2px 2px 0 #000" }}>{c.name}</div>
            ))}
            {audible.map(({ track, clip }: any) => (
              <AudioTag key={clip.id} clip={clip} mediaRefs={mediaRefs} />
            ))}
          </div>
          <div className="flex items-center gap-2 mt-2">
            <button className="btn-primary !text-xs" onClick={() => setPlaying((p) => !p)}>{playing ? "Pause" : "Play"}</button>
            <span className="font-mono text-[12px]">{fmt(time)} / {fmt(duration)}</span>
            <input type="range" min={0} max={duration} step={0.05} value={time} className="flex-1"
              onChange={(e) => seek(Number(e.target.value))} aria-label="Seek" />
          </div>
          <div ref={waveRef} className="mt-2" />
          {!selAudioUrl && sel && TRACK_FAMILY[sel.track] === "audio" && (
            <div className="text-[12px]" style={{ color: "var(--text-faint)" }}>Waveform needs an asset-backed clip.</div>
          )}
        </Card>

        {/* Inspector */}
        <Card>
          <b className="text-[13px]">Inspector</b>
          {!selClip && <div className="text-[12.5px] mt-2" style={{ color: "var(--text-faint)" }}>Select a clip.</div>}
          {selClip && sel && (
            <Inspector sel={sel} clip={selClip} commit={commitOps} splitAt={splitAtPlayhead} time={time} />
          )}
          <div className="mt-3 pt-3" style={{ borderTop: "var(--seam)" }}>
            <button className="btn-ghost !text-xs" onClick={() => setShowVersions((v) => !v)}>
              {showVersions ? "Hide versions" : `Versions (${versions.length})`}
            </button>
            <button
              className={showComments ? "btn-primary !text-xs" : "btn-ghost !text-xs"}
              onClick={() => setShowComments((v) => !v)}
            >
              {showComments ? "Hide comments" : "💬 Comments"}
            </button>
            {showVersions && versions.map((v: any) => (
              <div key={v.id} className="flex items-center gap-2 mt-1 text-[12px]">
                <Badge tone={v.is_tip ? "success" : "info"}>v{v.version}</Badge>
                <span className="font-mono" style={{ color: "var(--text-faint)" }}>{v.id.slice(0, 8)}</span>
                {!v.is_tip && <button className="btn-outline !text-xs" onClick={() => restoreVersion(v)}>Restore</button>}
              </div>
            ))}
            <button className="btn-outline !text-xs mt-2" onClick={async () => {
              try {
                await wsApi.post(`/timelines/${timelineId}/versions`, { label: "manual" });
                const v: any = await wsApi.get(`/timelines/${timelineId}/versions`);
                setVersions(v.versions ?? []);
                toast("Version saved", "success");
              } catch (e: any) {
                toast(e.message, "error");
              }
            }}>+ Save version</button>
          </div>
          {renderOut && (
            <div className="mt-3 text-[12.5px]">
              <b>Render:</b> <a className="underline" href={mediaFileUrl(renderOut.asset_id)} target="_blank" rel="noreferrer">
                {renderOut.width}×{renderOut.height} · {Number(renderOut.duration_seconds ?? 0).toFixed(1)}s</a>
            </div>
          )}
        </Card>
      </div>

      {/* Toolbar */}
      <Card>
        <div className="flex gap-2 flex-wrap items-center">
          <button className="btn-outline !text-xs" onClick={splitAtPlayhead}>✂ Split @ {fmt(time)}</button>
          <button className="btn-outline !text-xs" disabled={!sel} onClick={() => sel && commitOps(
            [{ type: "delete_item", track: sel.track, clip_id: sel.clipId }], "Delete")}>🗑 Delete</button>
          <button className="btn-outline !text-xs" disabled={!sel} onClick={() => {
            if (!sel || !selClip) return;
            commitOps([{ type: "duplicate_item", track: sel.track, clip_id: sel.clipId,
              at: clipEnd(selClip), new_id: nid("clip") }], "Duplicate");
          }}>📑 Duplicate</button>
          <button className="btn-ghost !text-xs" disabled={!undo.length} onClick={doUndo}>↩ Undo ({undo.length})</button>
          <button className="btn-ghost !text-xs" disabled={!redo.length} onClick={doRedo}>↪ Redo ({redo.length})</button>
          <button className="btn-ghost !text-xs" onClick={() => setPxPerSec((z) => Math.min(240, z * 1.25))}>🔍+</button>
          <button className="btn-ghost !text-xs" onClick={() => setPxPerSec((z) => Math.max(8, z / 1.25))}>🔎−</button>
          <button className={snap ? "btn-primary !text-xs" : "btn-ghost !text-xs"} onClick={() => setSnap((s) => !s)}>
            🧲 Snap {snap ? "on" : "off"}
          </button>
          <button className={showComments ? "btn-primary !text-xs" : "btn-ghost !text-xs"}
            onClick={() => setShowComments((v) => !v)}>
            {showComments ? "Hide comments" : "💬 Comments"}
          </button>
          <button className={showCompare ? "btn-primary !text-xs" : "btn-ghost !text-xs"}
            onClick={() => setShowCompare((v) => !v)}>
            {showCompare ? "Hide compare" : "⇄ Compare"}
          </button>
          <Badge tone={saveState === "Saved" ? "success" : saveState === "Conflict" ? "error" : "warning"}>{saveState}</Badge>
        </div>
      </Card>

      {/* Collaboration panels (Work 11 FE-B) */}
      {showComments && timelineId && (
        <CommentsPanel
          timelineId={timelineId}
          time={time}
          scenes={scenes}
          selectedClipId={sel?.clipId ?? null}
          selectedClip={selClip}
          onSeek={seek}
        />
      )}
      {showCompare && timelineId && (
        <VersionCompare timelineId={timelineId} versions={versions} />
      )}

      {/* Creative Director: NL → parse → preview → apply → undo */}
      {timelineId && <CreativeDirector timelineId={timelineId} onApplied={load} />}

      {/* Tracks */}
      <Card>
        <div className="overflow-x-auto">
          <div style={{ minWidth: 640 }}>
            {/* ruler */}
            <div className="flex mb-1">
              <div className="w-20 shrink-0" />
              <div className="relative flex-1 h-5" onClick={(e) => {
                const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
                seek((e.clientX - r.left) / pxPerSec);
              }}>
                {Array.from({ length: Math.ceil(duration) + 1 }, (_, s) => (
                  <span key={s} className="absolute font-mono text-[10px]" style={{ left: s * pxPerSec, color: "var(--text-faint)" }}>
                    {fmt(s)}
                  </span>
                ))}
              </div>
            </div>
            {tracks.map((tr) => (
              <div key={tr.kind} className="flex mb-1.5"
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => onDropAsset(e, tr.kind)}>
                <div className="w-20 shrink-0 text-[12px] font-mono pt-1" style={{ color: "var(--text-muted)" }}>
                  {KIND_ICON[tr.kind]} {tr.kind}
                </div>
                <div className="relative flex-1 h-11 rounded" style={{ background: "var(--seam)", width: duration * pxPerSec }}
                  onClick={(e) => {
                    if ((e.target as HTMLElement) === e.currentTarget) {
                      const r = (e.currentTarget as HTMLElement).getBoundingClientRect();
                      seek((e.clientX - r.left) / pxPerSec);
                    }
                  }}>
                  {tr.clips.map((c) => (
                    <ClipBlock key={c.id} clip={c} trackKind={tr.kind} pxPerSec={pxPerSec}
                      selected={sel?.track === tr.kind && sel?.clipId === c.id}
                      onSelectClip={onSelectClip}
                      onCommit={commitOps} getSnap={getSnap} />
                  ))}
                  {/* playhead */}
                  <div className="absolute top-0 bottom-0" style={{ left: time * pxPerSec, width: 2, background: "#ef4444" }} />
                </div>
              </div>
            ))}
          </div>
        </div>
      </Card>
    </div>
  );
}

function MediaURL({ clip }: { clip: any }): string | null {
  const src = clip.source ?? {};
  if (src.asset_id) return mediaFileUrl(src.asset_id);
  if (src.video_id) return videoFileUrl(src.video_id);
  return null;
}

function PreviewMedia({ clip, mediaRefs, time }: any) {
  const url = clip ? MediaURL({ clip }) : null;
  if (!clip || !url) {
    return <div className="absolute inset-0 flex items-center justify-center text-white/40 text-[13px]">No visual at {fmt(time)}</div>;
  }
  const key = `pv-${clip.id}`;
  if (clip.source?.asset_id && url.endsWith(".png")) {
    return <img src={url} alt="" className="absolute inset-0 w-full h-full object-cover" />;
  }
  return (
    <video key={key} className="absolute inset-0 w-full h-full object-cover" src={url} muted playsInline preload="auto"
      ref={(el) => {
        if (el) {
          mediaRefs.current.set(key, el);
          try {
            const off = Math.max(0, time - clip.start);
            if (Math.abs(el.currentTime - off) > 0.4) el.currentTime = off;
          } catch { /* noop */ }
        } else {
          mediaRefs.current.delete(key);
        }
      }} />
  );
}

function AudioTag({ clip, mediaRefs }: any) {
  const url = MediaURL({ clip });
  if (!url) return null;
  const key = `au-${clip.id}`;
  return (
    <audio src={url} preload="auto" ref={(el) => {
      if (el) {
        mediaRefs.current.set(key, el);
        el.volume = Math.min(1, clip.volume ?? 1);
        try {
          const off = Math.max(0, (clip.start ? 0 : 0));
          void off;
        } catch { /* noop */ }
      } else {
        mediaRefs.current.delete(key);
      }
    }} />
  );
}

function Inspector({ sel, clip, commit, splitAt, time }: any) {
  const [start, setStart] = useState(String(clip.start));
  const [dur, setDur] = useState(String(clip.duration));
  useEffect(() => {
    setStart(String(clip.start));
    setDur(String(clip.duration));
  }, [clip.id, clip.start, clip.duration]);
  void splitAt;
  void time;

  function num(v: string, fallback: number): number {
    const n = Number(v);
    return Number.isFinite(n) ? n : fallback;
  }

  return (
    <div className="mt-2 space-y-2 text-[12.5px]">
      <div className="font-mono text-[11.5px]" style={{ color: "var(--text-faint)" }}>{sel.track} · {clip.id.slice(0, 12)}</div>
      <label className="block">Start (s)
        <input className="input mt-0.5" value={start} onChange={(e) => setStart(e.target.value)}
          onBlur={() => commit([{ type: "move_item", track: sel.track, clip_id: clip.id, start: Math.max(0, num(start, clip.start)) }], "Move")} />
      </label>
      <label className="block">Duration (s)
        <input className="input mt-0.5" value={dur} onChange={(e) => setDur(e.target.value)}
          onBlur={() => commit([{ type: "trim_item", track: sel.track, clip_id: clip.id, edge: "end", end: num(start, clip.start) + Math.max(0.1, num(dur, clip.duration)) }], "Trim")} />
      </label>
      {(sel.track === "voice" || sel.track === "music" || sel.track === "sfx" || sel.track === "video") && (
        <>
          <label className="block">Volume ({clip.volume ?? 1})
            <input type="range" min={0} max={2} step={0.05} defaultValue={clip.volume ?? 1} className="w-full"
              onMouseUp={(e) => commit([{ type: "update_volume", track: sel.track, clip_id: clip.id, volume: Number((e.target as HTMLInputElement).value) }], "Volume")} />
          </label>
          <label className="block">Speed ({clip.speed ?? 1}×)
            <input type="range" min={0.25} max={2} step={0.05} defaultValue={clip.speed ?? 1} className="w-full"
              onMouseUp={(e) => commit([{ type: "update_speed", track: sel.track, clip_id: clip.id, speed: Number((e.target as HTMLInputElement).value) }], "Speed")} />
          </label>
        </>
      )}
      {(sel.track === "video" || sel.track === "broll" || sel.track === "avatar") && (
        <TransformEditor clip={clip} commit={(t: any) => commit(
          [{ type: "update_transform", track: sel.track, clip_id: clip.id, transform: t }], "Transform")} />
      )}
      {(sel.track === "text") && (
        <TextEditor clip={clip} commit={(patch: any) => commit(
          [{ type: "update_text", track: sel.track, clip_id: clip.id, text: patch }], "Text")} />
      )}
      {(sel.track === "caption") && (
        <CaptionEditor clip={clip} commit={(patch: any) => commit(
          [{ type: "update_caption", track: sel.track, clip_id: clip.id, ...patch }], "Caption")} />
      )}
    </div>
  );
}

function TransformEditor({ clip, commit }: any) {
  const t = clip.transform ?? {};
  const [v, setV] = useState<any>({ x: t.x ?? 50, y: t.y ?? 50, scale: t.scale ?? 1, rotation: t.rotation ?? 0, opacity: t.opacity ?? 100 });
  useEffect(() => {
    setV({ x: t.x ?? 50, y: t.y ?? 50, scale: t.scale ?? 1, rotation: t.rotation ?? 0, opacity: t.opacity ?? 100 });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clip.id]);
  return (
    <div className="grid grid-cols-2 gap-1.5">
      {[["x", 0, 100], ["y", 0, 100], ["scale", 0.2, 3], ["rotation", -180, 180], ["opacity", 0, 100]].map(([k, lo, hi]: any) => (
        <label key={k} className="block">{k}
          <input type="number" className="input mt-0.5" value={v[k]} min={lo} max={hi} step="any"
            onChange={(e) => setV({ ...v, [k]: Number(e.target.value) })}
            onBlur={() => commit({ [k]: Number(v[k]) })} />
        </label>
      ))}
    </div>
  );
}

function TextEditor({ clip, commit }: any) {
  const t = clip.text ?? {};
  const [v, setV] = useState<any>({ content: t.content ?? clip.name, size: t.size ?? 64, color: t.color ?? "white" });
  useEffect(() => {
    setV({ content: t.content ?? clip.name, size: t.size ?? 64, color: t.color ?? "white" });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clip.id]);
  return (
    <div className="space-y-1.5">
      <label className="block">Text
        <input className="input mt-0.5" value={v.content} onChange={(e) => setV({ ...v, content: e.target.value })}
          onBlur={() => commit({ content: v.content })} />
      </label>
      <label className="block">Size
        <input type="number" className="input mt-0.5" value={v.size} onChange={(e) => setV({ ...v, size: Number(e.target.value) })}
          onBlur={() => commit({ size: Number(v.size) })} />
      </label>
      <label className="block">Color
        <input className="input mt-0.5" value={v.color} onChange={(e) => setV({ ...v, color: e.target.value })}
          onBlur={() => commit({ color: v.color })} />
      </label>
    </div>
  );
}

function CaptionEditor({ clip, commit }: any) {
  const [text, setText] = useState(clip.name);
  const [style, setStyle] = useState(clip.text?.preset ?? "minimal");
  useEffect(() => {
    setText(clip.name);
    setStyle(clip.text?.preset ?? "minimal");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [clip.id]);
  return (
    <div className="space-y-1.5">
      <label className="block">Caption
        <input className="input mt-0.5" value={text} onChange={(e) => setText(e.target.value)}
          onBlur={() => commit({ text })} />
      </label>
      <label className="block">Style
        <select className="select mt-0.5" value={style} onChange={(e) => { setStyle(e.target.value); commit({ style: e.target.value }); }}>
          {["minimal", "pop", "karaoke"].map((s) => <option key={s} value={s}>{s}</option>)}
        </select>
      </label>
    </div>
  );
}

const ClipBlock = memo(function ClipBlock({ clip, trackKind, pxPerSec, selected, onSelectClip, onCommit, getSnap }: any) {
  const drag = useRef<{ mode: "move" | "l" | "r"; startX: number; orig: any } | null>(null);

  function onPointerDown(e: React.PointerEvent, mode: "move" | "l" | "r") {
    e.stopPropagation();
    onSelectClip(trackKind, clip.id);
    (e.target as HTMLElement).setPointerCapture(e.pointerId);
    drag.current = { mode, startX: e.clientX, orig: { ...clip } };
  }

  function onPointerMove(e: React.PointerEvent) {
    const d = drag.current;
    if (!d) return;
    const dx = (e.clientX - d.startX) / pxPerSec;
    const { doc, time, scenes, snap } = getSnap();
    if (d.mode === "move") {
      const at = snapTime(d.orig.start + dx, doc, time, scenes, snap);
      (e.currentTarget as HTMLElement).style.transform = `translateX(${(at - clip.start) * pxPerSec}px)`;
      (e.currentTarget as HTMLElement).dataset.pending = String(at);
    } else if (d.mode === "l") {
      const at = snapTime(d.orig.start + dx, doc, time, scenes, snap);
      (e.currentTarget as HTMLElement).dataset.pending = `l:${at}`;
    } else {
      const at = snapTime(d.orig.start + d.orig.duration + dx, doc, time, scenes, snap);
      (e.currentTarget as HTMLElement).dataset.pending = `r:${at}`;
    }
  }

  function onPointerUp(e: React.PointerEvent) {
    const d = drag.current;
    drag.current = null;
    (e.currentTarget as HTMLElement).style.transform = "";
    const pending = (e.currentTarget as HTMLElement).dataset.pending;
    (e.currentTarget as HTMLElement).dataset.pending = "";
    if (pending == null || pending === "") return;
    if (!d) return;
    if (d?.mode === "move") {
      const at = Math.max(0, Number(pending));
      if (Math.abs(at - d.orig.start) > 1e-6) {
        onCommit([{ type: "move_item", track: trackKind, clip_id: clip.id, start: at }], "Move");
      }
    } else if (pending.startsWith("l:")) {
      const at = Math.max(0, Number(pending.slice(2)));
      if (at < d.orig.start + d.orig.duration - 0.05 && at >= 0) {
        onCommit([{ type: "trim_item", track: trackKind, clip_id: clip.id, edge: "start", start: at }], "Trim");
      }
    } else if (pending.startsWith("r:")) {
      const at = Number(pending.slice(2));
      if (at > d.orig.start + 0.05) {
        onCommit([{ type: "trim_item", track: trackKind, clip_id: clip.id, edge: "end", end: at }], "Trim");
      }
    }
  }

  return (
    <div
      className="absolute top-1 bottom-1 rounded text-[11px] font-mono px-1.5 py-0.5 overflow-hidden cursor-grab select-none"
      style={{
        left: clip.start * pxPerSec, width: Math.max(clip.duration * pxPerSec, 14),
        background: selected ? "var(--accent-strong, #16a34a)" : "var(--accent, #22c55e)",
        border: selected ? "2px solid #fff" : "1px solid transparent", color: "#04120a",
      }}
      onPointerDown={(e) => onPointerDown(e, "move")}
      onPointerMove={onPointerMove}
      onPointerUp={onPointerUp}
      title={`${clip.name} [${clip.start.toFixed(2)}–${(clip.start + clip.duration).toFixed(2)}]`}
    >
      <span className="absolute left-0 top-0 bottom-0 w-2 cursor-ew-resize"
        onPointerDown={(e) => onPointerDown(e, "l")} />
      <span className="truncate block px-2">{clip.name}</span>
      <span className="absolute right-0 top-0 bottom-0 w-2 cursor-ew-resize"
        onPointerDown={(e) => onPointerDown(e, "r")} />
    </div>
  );
});

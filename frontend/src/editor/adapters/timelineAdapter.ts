/* Canonical Timeline <-> editor-row adapter.
 * The backend ContentTimeline is the ONLY domain model; these helpers project
 * it into renderable rows and build the typed operation batches consumed by
 * POST /timelines/{id}/operations (the same layer the AI Director will use). */

export type Clip = {
  id: string; name: string; start: number; duration: number;
  source?: Record<string, any>; effects?: any[];
  source_start?: number; volume?: number; speed?: number;
  fade_in?: number; fade_out?: number;
  transform?: Record<string, any>; text?: Record<string, any>;
  transition_in?: string; transition_out?: string;
};

export type Track = { id: string; kind: string; name: string; clips: Clip[] };

export const TRACK_ORDER = ["video", "broll", "avatar", "text", "caption", "voice", "music", "sfx"];

export const TRACK_FAMILY: Record<string, string> = {
  video: "visual", broll: "visual", avatar: "visual",
  voice: "audio", music: "audio", sfx: "audio",
  text: "overlay", caption: "overlay",
};

export function sortedTracks(doc: any): Track[] {
  const tracks: Track[] = (doc?.tracks ?? []).map((t: any) => ({
    id: t.id, kind: t.kind, name: t.name ?? t.kind,
    clips: [...(t.clips ?? [])].sort((a, b) => a.start - b.start),
  }));
  tracks.sort((a, b) => TRACK_ORDER.indexOf(a.kind) - TRACK_ORDER.indexOf(b.kind));
  return tracks;
}

export function findClip(doc: any, trackKind: string, clipId: string): Clip | null {
  const tr = (doc?.tracks ?? []).find((t: any) => t.kind === trackKind);
  return tr?.clips?.find((c: any) => c.id === clipId) ?? null;
}

export function clipEnd(c: Clip): number {
  return c.start + c.duration;
}

/** Snap a time to playhead / clip edges / scene edges within threshold seconds. */
export function snapTime(t: number, doc: any, playhead: number, scenes: any[], enabled: boolean): number {
  if (!enabled) return Math.max(0, t);
  const candidates = [playhead];
  for (const tr of doc?.tracks ?? []) {
    for (const c of tr.clips ?? []) {
      candidates.push(c.start, c.start + c.duration);
    }
  }
  for (const s of scenes ?? []) {
    candidates.push(s.start_seconds, s.end_seconds);
  }
  let best = Math.max(0, t);
  const threshold = 0.25; // seconds
  for (const cand of candidates) {
    const d = Math.abs(cand - t);
    if (d < threshold && d < Math.abs(best - t)) best = cand;
  }
  return Math.max(0, best);
}

/** Local optimistic application (mirrors backend timeline_ops semantics). */
export function applyOpsLocal(doc: any, ops: any[]): any {
  const next = JSON.parse(JSON.stringify(doc));
  for (const op of ops) {
    const tr = next.tracks.find((t: any) => t.kind === (op.track ?? op.from_track));
    if (op.type === "move_item") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      c.start = op.start;
    } else if (op.type === "trim_item") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      if (op.edge === "start") {
        c.source_start = (c.source_start ?? 0) + (op.start - c.start);
        c.duration = c.start + c.duration - op.start;
        c.start = op.start;
      } else {
        c.duration = op.end - c.start;
      }
    } else if (op.type === "split_item") {
      const i = tr.clips.findIndex((x: any) => x.id === op.clip_id);
      const c = tr.clips[i];
      const a = { ...c, duration: op.at - c.start };
      const b = { ...c, id: `${c.id}__b`, name: `${c.name} (2)`, start: op.at,
        duration: c.start + c.duration - op.at,
        source_start: (c.source_start ?? 0) + (op.at - c.start) };
      tr.clips.splice(i, 1, a, b);
    } else if (op.type === "delete_item") {
      tr.clips = tr.clips.filter((x: any) => x.id !== op.clip_id);
    } else if (op.type === "duplicate_item") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      tr.clips.push({ ...c, id: op.new_id ?? `${c.id}__copy`, start: op.at });
    } else if (op.type === "move_to_track") {
      const src = next.tracks.find((t: any) => t.kind === op.from_track);
      const dst = next.tracks.find((t: any) => t.kind === op.to_track);
      const i = src.clips.findIndex((x: any) => x.id === op.clip_id);
      const [c] = src.clips.splice(i, 1);
      dst.clips.push(op.start != null ? { ...c, start: op.start } : c);
    } else if (op.type === "add_item") {
      const dst = next.tracks.find((t: any) => t.kind === op.track);
      dst.clips.push({ ...op.clip });
    } else if (op.type === "update_transform") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      c.transform = { ...(c.transform ?? {}), ...op.transform };
    } else if (op.type === "update_volume") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      c.volume = op.volume;
      if (op.fade_in != null) c.fade_in = op.fade_in;
      if (op.fade_out != null) c.fade_out = op.fade_out;
    } else if (op.type === "update_speed") {
      tr.clips.find((x: any) => x.id === op.clip_id).speed = op.speed;
    } else if (op.type === "update_text") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      c.text = { ...(c.text ?? {}), ...op.text };
      if (op.start != null) c.start = op.start;
      if (op.duration != null) c.duration = op.duration;
    } else if (op.type === "update_caption") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      if (op.text != null) c.name = op.text;
      if (op.start != null) c.start = op.start;
      if (op.duration != null) c.duration = op.duration;
      if (op.style != null) c.text = { ...(c.text ?? {}), preset: op.style };
    }
    tr.clips.sort((a: any, b: any) => a.start - b.start);
  }
  const ends = next.tracks.flatMap((t: any) => t.clips.map((c: any) => c.start + c.duration));
  next.duration_seconds = ends.length ? Math.max(...ends) : 0;
  return next;
}

/** Build the inverse operations for undo (computed from the pre-edit doc). */
export function inverseOps(doc: any, ops: any[]): any[] {
  const inverse: any[] = [];
  for (let i = ops.length - 1; i >= 0; i--) {
    const op = ops[i];
    const c = op.clip_id ? findClip(doc, op.track ?? op.from_track, op.clip_id) : null;
    if (op.type === "move_item" && c) inverse.push({ ...op, start: c.start });
    else if (op.type === "trim_item" && c) {
      inverse.push(op.edge === "start"
        ? { ...op, start: c.start }
        : { ...op, end: c.start + c.duration });
    } else if (op.type === "split_item" && c) {
      inverse.push({ type: "delete_item", track: op.track, clip_id: `${op.clip_id}__b` });
      inverse.push({ type: "trim_item", track: op.track, clip_id: op.clip_id,
        edge: "end", end: c.start + c.duration });
    } else if (op.type === "delete_item" && c) {
      inverse.push({ type: "add_item", track: op.track, clip: { ...c } });
    } else if (op.type === "duplicate_item") {
      inverse.push({ type: "delete_item", track: op.track,
        clip_id: op.new_id ?? `${op.clip_id}__copy` });
    } else if (op.type === "move_to_track" && c) {
      inverse.push({ type: "move_to_track", from_track: op.to_track,
        to_track: op.from_track, clip_id: op.clip_id, start: c.start });
    } else if (op.type === "add_item") {
      inverse.push({ type: "delete_item", track: op.track, clip_id: op.clip.id });
    } else if ((op.type === "update_transform" || op.type === "update_volume" ||
        op.type === "update_speed" || op.type === "update_text" ||
        op.type === "update_caption") && c) {
      const back: any = { ...op };
      if (op.transform) back.transform = { ...(c.transform ?? {}) };
      if (op.volume != null) {
        back.volume = c.volume ?? 1;
        back.fade_in = c.fade_in ?? 0;
        back.fade_out = c.fade_out ?? 0;
      }
      if (op.speed != null) back.speed = c.speed ?? 1;
      if (op.text && op.type === "update_text") back.text = { ...(c.text ?? {}) };
      if (op.type === "update_text") {
        if (op.start != null) back.start = c.start;
        if (op.duration != null) back.duration = c.duration;
      }
      if (op.type === "update_caption") {
        if (op.text != null) back.text = c.name;
        if (op.start != null) back.start = c.start;
        if (op.duration != null) back.duration = c.duration;
        if (op.style != null) back.style = c.text?.preset ?? "minimal";
      }
      inverse.push(back);
    }
  }
  return inverse;
}

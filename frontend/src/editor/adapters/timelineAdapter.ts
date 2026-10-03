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
  /** Work 13: real word timing (stored, never synthesised) + its flag. */
  words?: { word: string; start_s: number; end_s: number; speaker_id?: string | null }[];
  word_level?: boolean;
  /** Work 13: stored semantic emphasis, editable and renderer-visible. */
  emphasis?: { word: string; start: number; end: number; kind: string; source: string }[];
  /** Work 13: a validated transition object (from_item/to_item/type/duration). */
  transition?: Record<string, any> | null;
  /** Work 13: per-caption animation keyframes (config, never pixels). */
  keyframes?: { t: number; [k: string]: any }[];
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
    } else if (op.type === "update_caption_style") {
      // Work 13: a typed style patch + preset. Mirrors the backend op, which
      // validates through app.engine.captions.style before persisting.
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      if (!c) continue;
      if (op.preset != null) c.text = { ...(c.text ?? {}), preset: op.preset };
      if (op.style && typeof op.style === "object") {
        c.text = { ...(c.text ?? {}) };
        const patch: any = op.style;
        if (patch.animation && typeof patch.animation === "object") {
          c.text.animation = { ...(c.text.animation ?? {}), ...patch.animation };
          delete patch.animation;
        }
        Object.assign(c.text, patch);
      }
    } else if (op.type === "set_caption_words") {
      // Word timing is STORED, never derived: an empty list clears it and
      // word_level follows the list so the renderer knows not to animate words.
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      if (!c) continue;
      c.words = Array.isArray(op.words) ? op.words : [];
      c.word_level = c.words.length > 0;
      if (op.emphasis) c.emphasis = op.emphasis;
    } else if (op.type === "apply_effect") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      if (!c) continue;
      const effects = Array.isArray(c.effects) ? [...c.effects] : [];
      const next = {
        type: String(op.effect?.type ?? "").toUpperCase(),
        params: { ...(op.effect?.params ?? {}) },
        enabled: op.effect?.enabled !== false,
      };
      const at = effects.findIndex(
        (e: any) => String(e?.type ?? "").toUpperCase() === next.type
      );
      if (at >= 0) effects[at] = next;
      else effects.push(next);
      c.effects = effects;
    } else if (op.type === "remove_effect") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      if (!c) continue;
      const want = String(op.effect ?? "").toUpperCase();
      c.effects = (Array.isArray(c.effects) ? c.effects : []).filter(
        (e: any) => String(e?.type ?? "").toUpperCase() !== want
      );
    } else if (op.type === "set_transition") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      if (!c) continue;
      c.transition = { ...(op.transition ?? {}) };
      c.transition_in =
        Number(op.transition?.duration ?? 0) > 0 ? "crossfade" : "cut";
    } else if (op.type === "add_keyframe") {
      // Work 13.1 canonical keyframes. Sorted by (t, id) so the local preview
      // matches the server's canonical ordering exactly.
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      if (!c) continue;
      const chain = Array.isArray(c.keyframes) ? [...c.keyframes] : [];
      const frame = { ...(op.keyframe ?? {}) };
      if (!frame.id) frame.id = `kf${chain.length + 1}`;
      if (frame.easing == null) frame.easing = "LINEAR";
      if (frame.props == null) frame.props = {};
      const at = chain.findIndex((f: any) => f?.id === frame.id);
      if (at >= 0) chain[at] = frame;
      else chain.push(frame);
      c.keyframes = chain.sort(
        (a: any, b: any) => Number(a.t) - Number(b.t) ||
          String(a.id).localeCompare(String(b.id))
      );
    } else if (op.type === "update_keyframe") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      if (!c) continue;
      c.keyframes = (Array.isArray(c.keyframes) ? c.keyframes : []).map((f: any) =>
        f?.id === op.keyframe_id
          ? { ...f, ...(op.keyframe ?? {}), id: f.id }
          : { ...f }
      );
    } else if (op.type === "move_keyframe") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      if (!c) continue;
      c.keyframes = (Array.isArray(c.keyframes) ? c.keyframes : []).map((f: any) =>
        f?.id === op.keyframe_id ? { ...f, t: Number(op.t) } : { ...f }
      );
      c.keyframes.sort(
        (a: any, b: any) => Number(a.t) - Number(b.t) ||
          String(a.id).localeCompare(String(b.id))
      );
    } else if (op.type === "delete_keyframe") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      if (!c) continue;
      c.keyframes = (Array.isArray(c.keyframes) ? c.keyframes : []).filter(
        (f: any) => f?.id !== op.keyframe_id
      );
    } else if (op.type === "set_keyframes") {
      const c = tr.clips.find((x: any) => x.id === op.clip_id);
      if (!c) continue;
      const chain = (Array.isArray(op.keyframes) ? op.keyframes : []).map((f: any) => ({
        ...f,
        easing: f?.easing ?? "LINEAR",
        props: { ...(f?.props ?? {}) },
      }));
      c.keyframes = chain.sort(
        (a: any, b: any) => Number(a.t) - Number(b.t) ||
          String(a.id).localeCompare(String(b.id))
      );
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
        op.type === "update_caption" || op.type === "update_caption_style" ||
        op.type === "set_caption_words" || op.type === "apply_effect" ||
        op.type === "remove_effect" || op.type === "set_transition" ||
        op.type === "add_keyframe" || op.type === "update_keyframe" ||
        op.type === "move_keyframe" || op.type === "delete_keyframe" ||
        op.type === "set_keyframes") && c) {
      const back: any = { ...op };
      // `let`-like rebinding below: some keyframe inverses replace the op shape
      // entirely (a partial inverse would not restore the chain ORDER).
      let inverseOp: any = back;
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
      // Work 13 inverses: each captures the PRE-edit value so undo restores
      // the exact prior state (a missing branch here would make a Work 13 edit
      // silently un-undoable).
      if (op.type === "update_caption_style") {
        back.style = { ...(c.text ?? {}) };
        if (op.preset != null) back.preset = c.text?.preset ?? "minimal";
      }
      if (op.type === "set_caption_words") {
        back.words = (c.words ?? []).map((w: any) => ({ ...w }));
        if (op.emphasis) back.emphasis = (c.emphasis ?? []).map((e: any) => ({ ...e }));
      }
      if (op.type === "apply_effect") {
        const want = String(op.effect?.type ?? "").toUpperCase();
        const before = (c.effects ?? []).find(
          (e: any) => String(e?.type ?? "").toUpperCase() === want
        );
        back.effect = before ? { ...before, params: { ...(before.params ?? {}) } } : null;
      }
      if (op.type === "remove_effect") {
        const want = String(op.effect ?? "").toUpperCase();
        const before = (c.effects ?? []).find(
          (e: any) => String(e?.type ?? "").toUpperCase() === want
        );
        back.effect = before ? { ...before, params: { ...(before.params ?? {}) } } : null;
      }
      if (op.type === "set_transition") {
        back.transition = c.transition ? { ...c.transition } : null;
      }
      // Work 13.1 keyframe inverses. The whole chain is captured (a deep copy)
      // rather than a single frame, because add/delete/move all mutate an
      // ORDERED list: a partial inverse would restore the frames but not the
      // order the renderer depends on.
      if (op.type === "add_keyframe") {
        const id = op.keyframe?.id;
        const before = (c.keyframes ?? []).find((f: any) => f?.id === id);
        if (before) {
          // replacing an existing keyframe: undo restores the prior frame
          inverseOp = { type: "set_keyframes", track: op.track, clip_id: op.clip_id,
            keyframes: (c.keyframes ?? []).map((f: any) => ({ ...f })) };
        } else {
          // genuinely new: undo removes exactly that one
          inverseOp = { type: "delete_keyframe", track: op.track,
            clip_id: op.clip_id, keyframe_id: id };
        }
      }
      if (op.type === "update_keyframe" || op.type === "move_keyframe") {
        // Pre-edit chain: it already holds the prior frame at its prior time,
        // so restoring it verbatim undoes both the value and the re-ordering.
        const chainBefore: any[] = Array.isArray(c.keyframes) ? c.keyframes : [];
        inverseOp = { type: "set_keyframes", track: op.track, clip_id: op.clip_id,
          keyframes: chainBefore.map((f: any) => ({
            ...f, props: { ...(f?.props ?? {}) } })) };
      }
      if (op.type === "delete_keyframe") {
        // `doc` here is the PRE-edit timeline, so the chain already contains
        // the frame being deleted: the inverse is that chain verbatim, NOT the
        // chain plus the deleted frame (which would duplicate it).
        const chainBefore: any[] = Array.isArray(c.keyframes) ? c.keyframes : [];
        if (chainBefore.some((f: any) => f?.id === op.keyframe_id)) {
          inverseOp = { type: "set_keyframes", track: op.track,
            clip_id: op.clip_id,
            keyframes: chainBefore.map((f: any) => ({ ...f })) };
        } else {
          // nothing to restore: keep a no-op-safe inverse
          inverseOp = { type: "delete_keyframe", track: op.track,
            clip_id: op.clip_id, keyframe_id: op.keyframe_id };
        }
      }
      if (op.type === "set_keyframes") {
        inverseOp = { type: "set_keyframes", track: op.track, clip_id: op.clip_id,
          keyframes: (c.keyframes ?? []).map((f: any) => ({ ...f })) };
      }
      inverse.push(inverseOp);
    }
  }
  return inverse;
}

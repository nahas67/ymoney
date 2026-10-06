/* Transition editor for the SELECTED clip (Work 16.5.7 §1).
 *
 * WHY THIS IS A SEPARATE COMPONENT
 * --------------------------------
 * The transition UI used to live inside `CaptionMotionPanel`, behind
 * `track !== "caption"`. That panel mounts only when `sel.track === "caption"`
 * (`pages/Editor.tsx`), so the conjunction was unsatisfiable and the control was
 * DEAD CODE: present, correct, and unreachable by any selection.
 *
 * Deleting the guard would have made the button appear -- and then commit a
 * transition on a caption clip, which nothing renders. So the guard was not the
 * bug; the MOUNT was.
 *
 * WHAT THE SYSTEM ACTUALLY SUPPORTS (traced, not guessed)
 * -------------------------------------------------------
 * 1. `app/engine/timeline_ops.py` `TRACK_FAMILIES`:
 *      visual = {video, broll, avatar}; audio = {voice, music, sfx};
 *      overlay = {text, caption}.
 * 2. `app/engine/motion/transitions.py` `find_adjacent_pair` refuses any pair
 *    that is not adjacent in time on the SAME track -- it applies no track
 *    restriction of its own.
 * 3. `build_transition_filter` in the same module is a VIDEO cross-dissolve: it
 *    returns an `xfade` video filter and `None` for a CUT. Its own scope note
 *    says "this is a VIDEO cross-dissolve only".
 *
 * So the backend will accept a transition on any track, but the renderer only
 * implements the video one. Offering it on audio or overlay tracks would promise
 * an effect that does not exist on screen -- the exact failure this codebase's
 * `NOT_AVAILABLE - no mask` pattern exists to prevent.
 *
 * ELIGIBILITY, therefore: a VISUAL track (`video|broll|avatar`) AND an adjacent
 * clip to transition into. That is the same visual gate `Inspector` already uses
 * for `TransformEditor` (`Editor.tsx:807`), so this follows the codebase's own
 * convention rather than introducing a second one.
 */

import { useEffect, useState } from "react";
import { Row, SubHead } from "../intel/shared";
import { wsApi } from "../../lib/api";

/** The canonical op-batch commit the editor hands to every panel. */
type CommitFn = (ops: Array<Record<string, unknown>>, label: string) => void;

/* The same workspace-scoped GET the sibling panels use. `wsApi.get` is
 * generically typed at the call site in every other motion panel; the cast is
 * the house style, and it keeps this component from inventing a second client. */
const call = <T,>(path: string) =>
  (wsApi.get as unknown as (p: string) => Promise<T>)(path) as Promise<T>;

type TransitionWire = {
  key: string;
  label: string;
  cross: boolean;
  max_duration_seconds?: number;
};

/** The backend's own visual family. Mirrors `TRACK_FAMILIES["visual"]`. */
export const VISUAL_TRACKS: ReadonlySet<string> = new Set(["video", "broll", "avatar"]);

/**
 * The clip that follows `clip` on the same track, in time.
 *
 * Mirrors `find_adjacent_pair`'s definition: one ends where (or within 0.05s
 * before) the other starts. A gap or an overlap means NOT adjacent, and the
 * backend would refuse the op -- so the control must not offer it either.
 */
export function adjacentClip(
  clips: Array<Record<string, any>>,
  clipId: string,
): Record<string, any> | null {
  const ordered = [...clips].sort(
    (a, b) => Number(a.start ?? 0) - Number(b.start ?? 0),
  );
  for (let i = 0; i < ordered.length - 1; i += 1) {
    const a = ordered[i];
    const b = ordered[i + 1];
    const aEnd = Number(a.start ?? 0) + Number(a.duration ?? 0);
    if (Number(b.start ?? 0) - aEnd > 0.05) continue;
    if (String(a.id) === clipId) return b;
    if (String(b.id) === clipId) return a;
  }
  return null;
}

export default function TransitionEditor({
  clip,
  track,
  siblings,
  commit,
  disabled,
}: {
  clip: Record<string, any>;
  track: string;
  /** Every clip on the SAME track, so adjacency is computed from real data. */
  siblings: Array<Record<string, any>>;
  commit: CommitFn;
  disabled?: boolean;
}) {
  const [transitions, setTransitions] = useState<TransitionWire[]>([]);

  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const tr = await call<{ items: TransitionWire[] }>("/captions/transitions");
        if (live) setTransitions(tr.items);
      } catch {
        // A vocabulary read failure must leave the panel inert, not fake options.
        if (live) setTransitions([]);
      }
    })();
    return () => {
      live = false;
    };
  }, []);

  // A transition may be stored on EITHER clip of the pair; show the attached one
  // so the type/duration editor never edits a phantom.
  const stored: any | null = clip?.transition ?? clip?.transition_spec ?? null;
  const next = adjacentClip(siblings, String(clip.id));

  // Not a visual track: the renderer has no cross-dissolve for it. Say so
  // rather than disappearing, so the absence is legible instead of mysterious.
  if (!VISUAL_TRACKS.has(track)) {
    return (
      <div>
        <SubHead>Transitions</SubHead>
        <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>
          NOT_AVAILABLE &mdash; cross-dissolve renders on visual tracks only (video,
          broll, avatar). This is an {track} track.
        </div>
      </div>
    );
  }

  // Visual, but nothing to transition INTO. `find_adjacent_pair` would refuse,
  // so the control must not pretend otherwise.
  if (!next) {
    return (
      <div>
        <SubHead>Transitions</SubHead>
        <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>
          NOT_AVAILABLE &mdash; no adjacent clip to transition into. A transition
          needs two clips that meet on the same track.
        </div>
      </div>
    );
  }

  return (
    <div>
      <SubHead>Transitions</SubHead>
      <Row label="From this clip">
        <select
          className="select"
          value=""
          disabled={disabled}
          aria-label="Add transition"
          onChange={(e) => {
            if (!e.target.value) return;
            commit(
              [
                {
                  type: "set_transition",
                  track,
                  clip_id: clip.id,
                  to_item: next.id,
                  transition: {
                    from_item: clip.id,
                    to_item: next.id,
                    type: "dissolve",
                    duration: 0.5,
                  },
                },
              ],
              "Transition"
            );
          }}
        >
          <option value="">choose a transition into {String(next.name ?? next.id)}</option>
          {transitions
            .filter((t) => t.cross)
            .map((t) => (
              <option key={t.key} value={t.key}>
                {t.label}
              </option>
            ))}
        </select>
      </Row>
      {stored ? (
        <Row label="Active transition">
          <div className="space-y-1">
            <select
              className="select"
              aria-label="Transition type"
              value={String(stored.type ?? "").toUpperCase()}
              disabled={disabled}
              onChange={(e) =>
                commit(
                  [
                    {
                      type: "set_transition",
                      track,
                      clip_id: stored.from_item ?? clip.id,
                      to_item: stored.to_item,
                      transition: { ...stored, type: e.target.value.toUpperCase() },
                    },
                  ],
                  "Transition type"
                )
              }
            >
              {transitions.map((t) => (
                <option key={t.key} value={t.key.toUpperCase()}>
                  {t.label}
                </option>
              ))}
            </select>
            <label className="flex items-center gap-1.5 text-[11px]">
              <input
                type="number"
                className="input"
                aria-label="Transition duration seconds"
                min={0}
                max={Math.max(0, Number(clip?.duration ?? 0))}
                step={0.1}
                defaultValue={Number(stored.duration ?? 0.5)}
                onBlur={(e) => {
                  const value = Number(e.target.value);
                  if (!Number.isFinite(value) || value < 0) return;
                  if (value === Number(stored.duration)) return;
                  commit(
                    [
                      {
                        type: "set_transition",
                        track,
                        clip_id: stored.from_item ?? clip.id,
                        to_item: stored.to_item,
                        transition: { ...stored, duration: value },
                      },
                    ],
                    "Transition duration"
                  );
                }}
              />
              <span style={{ color: "var(--text-faint)" }}>s overlap</span>
            </label>
            <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>
              {String(stored.type ?? "").toUpperCase()} over{" "}
              {Number(stored.duration ?? 0).toFixed(2)}s, into{" "}
              {String(stored.to_item ?? "?")}
            </div>
          </div>
        </Row>
      ) : (
        <div className="text-[11px]" style={{ color: "var(--text-faint)" }}>
          No transition on this clip &mdash; it renders as a cut.
        </div>
      )}
    </div>
  );
}
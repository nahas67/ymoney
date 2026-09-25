# YMONEY Video Editor (Work 02)

Browser non-linear editor bound 1:1 to the canonical `ContentTimeline`.
Route: `/editor/:timelineId` (opened from Studio → content → Edit timeline →
Open full editor). No frontend-only format exists: every mutation becomes a
typed op batch on `POST /timelines/{id}/operations`.

## Layout

Assets (search + drag onto tracks, scene jump list) | Preview (clock-synced
video/img overlays, captions, text, audio elements) + Inspector (per-kind
fields, versions, render output) | Toolbar (split/delete/duplicate/undo/redo/
zoom/snap/save-state) | 8 track lanes with ruler + playhead.

## Operations (all persisted, all undoable)

Move (drag, snapped), trim (edge handles), split at playhead, delete,
duplicate (asset ref reused), track moves (family-checked server-side),
transform/volume/speed/text/caption edits via inspector.

## Preview vs render

Preview is browser composition (approximate: audio sync is best-effort,
expensive effects may differ — flagged in code where applicable). Truth is
always canonical → RenderManifest → server FFmpeg (`timeline_render.py`).

## History & safety

Command stack with coalesced drags (100 pointer-moves → 1 undo entry);
debounced autosave (800ms) with Saved/Saving/Unsaved/Failed/Conflict states;
409 conflict offers Reload latest (server wins, stacks cleared); versions
list/create/restore (append-only); waveform via wavesurfer.js on the
selected asset-backed audio clip (click-to-seek).

## Limits (honest)

- No DOM virtualization yet: comfortable to ~500 clips; beyond that lanes
  stay correct but interaction slows (virtualization is the next perf task).
- Audio preview sync is approximate; the render is sample-accurate.
- Proxy media not yet generated: preview streams originals (proxies planned).
- Mobile shows preview/status only; desktop is the editing target.

# Scene ↔ Timeline Sync (Work 02)

Closes the Work 01 gap: pipelines now persist scenes instead of only
returning plan dicts.

## Adapters (`app/engine/scene_sync.py`)

- `sync_from_broll_plan` — visual plan → evenly split scenes (title, visual
  intent, ranges). Replace-based per timeline: reruns are deterministic.
- `sync_from_segments` — repurpose/detect ranges (+hook text) → scenes.
- `attach_captions_to_scenes` — caption cues overlapping each scene land in
  `captions_json`.
- `scene_clip_map` — per scene, the clip ids overlapping its range by track.
- `resync_scene_ranges` — after timeline edits, each scene shrinks/expands to
  the union of its overlapping video/voice clips (runs inside the operations
  route; scenes with no overlap keep their ranges).

## Pipeline wiring (no rewrites, best-effort, never breaks the pipeline)

- `BrollResearcherAgent.research` — when `ctx.payload.timeline_id` is set,
  the plan persists as scenes; result carries `synced_scene_ids` (tested).
- `RepurposeEditorAgent.assemble` — when `ctx.payload.timeline_id` is set,
  cut ranges persist as scenes (tested with a stub repurposer).
- `POST /timelines/{id}/scenes` (bulk replace) and scene ranges shown in the
  editor; clicking a scene seeks the playhead.

## Not yet

Automatic caption↔scene attach inside pipelines (engine function ready, call
sites pending), retention→scene mapping (needs retention intelligence,
Work 03+), narration/script segmentation sync (needs long-form pipeline).

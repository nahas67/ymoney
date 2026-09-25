# Timeline Operations (Work 02)

The single mutation layer shared by the browser editor now and the AI
Creative Director later. UI and AI must never write timelines any other way.

## Contract

`POST /timelines/{id}/operations` with `{base_version, operations[]}`.
Absolute values only (no deltas). Batch applies transactionally to a copy,
validates, recomputes duration, bumps `version`, resyncs scene ranges.
Stale `base_version` → 409 `{expected_version, actual_version}`, nothing applied.

## Operation types (`app/engine/timeline_ops.py`)

add_item, delete_item, move_item, trim_item (edge start|end; start trims shift
source_start so content under the playhead stays put), split_item (B inherits
source offset, id `{id}__b`), duplicate_item (asset ref reused, never bytes),
move_to_track (visual/audio/overlay families enforced), update_transform
(x,y,scale,rotation,opacity,crop,z_index allowlist), update_volume (+fades),
update_speed (0.25–4), update_text, update_caption (text/timing/style preset).

## Clip schema (extends Work 01)

id, name, start, duration, source{asset_id|video_id}, effects[],
source_start, volume 0–4, speed 0.25–4, fade_in/out, transform{}, text{},
transition_in/out (cut|fade|crossfade). Ranges validated on every write;
full payload survives OTIO via `ymoney_clip` metadata.

## Frontend mirror

`frontend/src/editor/adapters/timelineAdapter.ts`: row projection,
snapTime (playhead/clip edges/scene edges, toggle), applyOpsLocal (optimistic),
inverseOps (undo). Any semantic drift between mirror and backend surfaces
immediately as 422 → reload; the backend is authoritative.

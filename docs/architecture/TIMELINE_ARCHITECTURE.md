# YMONEY Timeline Architecture

One canonical timeline for long videos, shorts, manual editor, AI Creative
Director, renders, and import/export (Work 01).

## Layers

```text
Content Strategy / Scene Graph
      ↓
ContentTimeline (DB row: fps, duration, tracks_json, version, parent link)
      ↓  engine/timeline.py (pure: create/validate/add/OTIO-dict/manifest/version)
      ↓  engine/otio_adapter.py (ONLY module importing opentimelineio)
RenderManifest (flattened clips + stable sha256 hash)
      ↓  manifest_to_render_request() (best-effort RenderRequest adapter)
VideoEngine (ffmpeg_avatar / MoneyPrinterTurbo HTTP — untouched)
      ↓
MP4 + QC + asset registration
```

## Tracks

video | broll | avatar | text | caption | voice | music | sfx.
Clip: id, name, start, duration, source{}, effects[].
Validation names the offending track+clip; overlaps/negative durations rejected.

## OTIO

- Dict-level `to_otio_dict`/`from_otio_dict` (no dependency).
- Real-object `otio_adapter.py` over opentimelineio==0.18.1 (Apache-2.0):
  `to_otio_timeline` / `from_otio_timeline` / `roundtrip_serialized`.
- Lossy notes: track display names in metadata; clip effects/source dicts ride
  OTIO metadata (OTIO-native consumers ignore them, YMONEY keeps them).
- YMONEY semantics stay independent — the adapter is replaceable.

## Versions (undo primitive)

`save_version` copy-on-write: new row, version+1, `parent_timeline_id` link.
Routes: CRUD + versions + manifest + otio + from-video + scenes, all
workspace-scoped (cross-tenant → 404), invalid tracks → 422.

## Frontend

`TimelinesPanel` (ContentDetail → Edit timeline tab): load, track-bar
visualization, rename/save, create empty, import render. Full editor
(split/trim/waveform/autosave) is the next phase and consumes these routes.

## Performance / security

- No media bytes in rows (asset references only); mutations are small JSON writes.
- No heavy rendering inside requests; `workspace_id`/paths never trusted from
  clients (server-side `ws` + `validate_storage_key` + `managed_path`).

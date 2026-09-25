# YMONEY Content Model

Parent/child content graph (Work 01). Existing tables reused; lineage is
additive — legacy rows without lineage columns behave as roots.

## Hierarchy

```text
Workspace → Campaign → ContentItem (master)
                          ├── ContentItem (short | variant | platform_cut | …)
                          │     └── ContentItem (localized …)
                          ├── VideoVariant → Video → QualityCheck
                          ├── ContentTimeline (+ versions)
                          ├── Scene (timeline ranges)
                          └── MediaAsset (referenced bytes)
PublishingJob / PublishedPost / PostMetric hang off Video.
LearningPattern / MemoryRecord observe everything; never own it.
```

## Lineage columns (`content_items`, migration 0014)

- `parent_content_id` — direct parent, nullable (null = master).
- `root_content_id` — master ancestor (defaults to self).
- `derivation_type` — short|variant|localized|platform_cut|repurpose|translation|other.
- `lineage_version` — 1-based position among siblings.

Engine: `app/engine/content_graph.py` (`derive_content`, `lineage_chain`,
same-workspace enforced). API: `GET /content/{id}/lineage`,
`POST /content/{id}/derive` + `content.derived` event.

## MediaAsset (`media_assets`)

Typed reference rows, never blobs: type/origin/provider/storage_key
(workspace-relative, `..`/absolute rejected by `validate_storage_key`) +
technical metadata (mime, duration, dimensions, codecs, checksum).
Bytes stay in `LocalStorage` (or S3 later); `managed_path` remains the
serve-time boundary.

## Scene (`scenes`)

First-class narrative units: script_segment, narration, visual_intent,
start/end seconds, asset refs, captions, performance_json (retention maps
here later), parent_scene_id. Bulk-replaced per timeline via
`POST /timelines/{id}/scenes`; ordered by `index`.

## Design rules

- Core relationships are explicit columns, not opaque JSON.
- Extensible metadata stays in `*_json` fields.
- Lineage never crosses workspaces (engine + route 404s).
- Renders are described by timelines (`RenderManifest`), never by prompts.

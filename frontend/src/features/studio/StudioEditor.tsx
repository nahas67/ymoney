/* StudioEditor — the SHELL around the canonical timeline engine.
 *
 * =============================================================
 *  NO SECOND TIMELINE. THE ENGINE IS IMPORTED, NOT REWRITTEN.
 * =============================================================
 *
 * The canonical engine is `src/pages/Editor.tsx`, which is the only module in the
 * frontend that owns editing. It already carries, and this screen therefore
 * inherits rather than duplicates:
 *
 *   editor/adapters/timelineAdapter.ts   canonical Work 02/13.1 op vocabulary
 *     sortedTracks / findClip / clipEnd / snapTime
 *     applyOpsLocal   — local optimistic application of a canonical op batch
 *     inverseOps      — undo/redo inverse computation (incl. keyframe chains)
 *   undo / redo                           50-entry stacks over forward+inverse
 *   autosave                              800 ms debounce -> POST /operations
 *   stale-write handling                  base_version gate + 409 -> ConflictNotice,
 *                                        "autosave failed" banner, reload-latest
 *   versions                              list, create, restore onto a new tip
 *   waveform                              wavesurfer on the selected audio clip
 *   captions / transitions / effects      CaptionMotionPanel (canonical style path)
 *   keyframes                             components/intel/KeyframeEditor via the
 *                                        visual intelligence proposal apply path
 *   music / audio / voice tracks          TRACK_ORDER + TRACK_FAMILY from the adapter
 *   review / comments / diff              ReviewStatusBar, CommentsPanel, VersionCompare
 *
 * Those live in `pages/Editor.tsx`; this file does not re-declare an op type, an
 * inverse, an undo stack, a version check or a save timer. What this file adds
 * is the rebuilt shell around it: the `Assets | Scenes` rail, the header facts,
 * and the honest loading/empty/error states for the rail's own reads.
 *
 * WHY THE RAIL IS READ-ONLY.
 *
 * Insertion writes operations to `POST /timelines/{id}/operations` under the
 * engine's undo stack and version gate. A second write path from the shell
 * would be a second timeline by definition: an edit the engine's undo cannot
 * reverse and its ConflictNotice cannot see. So the rail BROWSES the same real
 * rows the engine reads and says where insertion happens, instead of
 * duplicating `commitOps`.
 *
 * ENDPOINTS (the shell's own reads; the engine's are its own)
 *
 *   GET /timelines/{id}          api/v1/timelines.py `get_timeline` (142)
 *   GET /timelines/{id}/scenes   api/v1/timelines.py `list_scenes` (286)
 *   GET /assets/media?limit=200  api/v1/content.py `assets_router.list_media`
 *                                (1345), dto `_media_dto` (1303)
 */

import { useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Field,
  Grid,
  Panel,
  QueryBoundary,
  Select,
  StatTile,
  Tabs,
  humanize,
  type Column,
} from "../../design-system/primitives";
import { useWsQuery } from "../../api/queries";
import { useSession } from "../../state/session";

/* The canonical engine. Importing the component IS the reuse: everything it
 * owns — ops, inverseOps, undo/redo, versions, waveform, autosave, the 409
 * stale-write path — is the engine's, not a copy made here. */
import CanonicalEditor from "../../pages/Editor";

/* The canonical adapter is imported for its TYPES and its read-only helpers so
 * the rail reads the same track vocabulary the engine edits. No operation is
 * built here: building one from the shell would be a second write path. */
import { TRACK_FAMILY, TRACK_ORDER, sortedTracks } from "../../editor/adapters/timelineAdapter";

/* ==========================================================================
 * Response shapes — typed from the backend routers named above
 * ======================================================================= */

/** `timelines._dto` — backend/app/api/v1/timelines.py:47. */
/** One clip as the CANONICAL adapter reads it (editor/adapters/timelineAdapter.ts). */
export type TimelineClip = {
  id: string;
  start: number;
  duration?: number;
  [key: string]: unknown;
};

/** One track. `clips` is what the adapter sorts; the engine owns the rest. */
export type TimelineTrack = {
  id?: string;
  kind: string;
  name?: string;
  clips?: TimelineClip[];
  [key: string]: unknown;
};

export type TimelineDoc = {
  id: string;
  workspace_id: string;
  content_item_id: string | null;
  video_id: string | null;
  name: string;
  fps: number;
  duration_seconds: number;
  aspect_ratio: string;
  version: number;
  parent_timeline_id: string | null;
  created_at: string;
  updated_at: string;
  /**
   * WORK 16.5.3 §4. This field WAS MISSING, and the absence was being papered
   * over with `timeline.data as unknown as { tracks?: unknown[] }`.
   *
   * That cast was not merely untidy. Because the declared type had no `tracks`,
   * the rail counted tracks off an unverified assumption, and a backend that
   * omitted the key would have rendered as "0 tracks" -- a fabricated zero,
   * indistinguishable from a timeline that genuinely has none. The backend has
   * always returned it: `timelines._dto` emits `"tracks"` at line 58.
   *
   * So the contract is now declared, and the bypass is gone. The index
   * signature is deliberate: the adapter reads additional clip/track properties,
   * and the canonical engine -- not this shell -- owns their meaning.
   */
  tracks?: TimelineTrack[];
};

/** `timelines._scene_dto` — backend/app/api/v1/timelines.py:274. */
export type SceneRow = {
  id: string;
  workspace_id: string;
  content_item_id: string | null;
  timeline_id: string | null;
  index: number;
  title: string;
  script_segment: string;
  narration: string;
  visual_intent: string;
  start_seconds: number;
  end_seconds: number;
  assets: { asset_id?: string; role?: string }[];
  captions: unknown[];
};
export type SceneList = { total: number; scenes: SceneRow[] };

/** `assets_router._media_dto` — backend/app/api/v1/content.py:1303. */
export type MediaAsset = {
  id: string;
  workspace_id: string;
  type: string;
  origin: string;
  provider: string;
  storage_key: string;
  mime_type: string;
  duration_seconds: number | null;
  width: number | null;
  height: number | null;
  checksum: string;
  created_at: string;
};
export type MediaList = { total: number; items: MediaAsset[] };

/* ==========================================================================
 * Formatters
 * ======================================================================= */

function seconds(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return "—";
  const m = Math.floor(value / 60);
  return `${String(m).padStart(2, "0")}:${(value % 60).toFixed(1).padStart(4, "0")}`;
}

function createdAt(iso: string): string {
  return iso ? iso.replace("Z", " UTC").replace("T", " ").slice(0, 16) : "—";
}

/**
 * Which TRACK_KIND a media row can feed, from the adapter's own family map.
 * This is a READ of the canonical vocabulary: it labels the row, it does not
 * create a clip.
 */
export function trackKindsFor(asset: MediaAsset): string[] {
  const type = asset.type.toLowerCase();
  if (asset.mime_type.startsWith("video/")) return ["video", "broll"];
  if (asset.mime_type.startsWith("image/")) return ["broll", "avatar"];
  if (type === "voice") return ["voice"];
  if (type === "audio") return ["music", "sfx"];
  if (type === "avatar") return ["avatar"];
  return [];
}

/* ==========================================================================
 * Component
 * ======================================================================= */

type RailTab = "assets" | "scenes";

export function StudioEditor({ timelineId: timelineIdProp }: { timelineId?: string }) {
  const params = useParams();
  /* The route registry owns the param (`/studio/:timelineId`); the prop form is
   * supported so a host can mount this screen outside the router. The engine
   * itself reads the route param, so the two must agree — hence the fallback. */
  const timelineId = timelineIdProp ?? params.timelineId ?? "";
  const { workspaceId } = useSession();
  const enabled = Boolean(workspaceId) && Boolean(timelineId);

  const [rail, setRail] = useState<RailTab>("assets");
  const [assetType, setAssetType] = useState("");
  const [assetSearch, setAssetSearch] = useState("");

  const timeline = useWsQuery<TimelineDoc>(`/timelines/${timelineId}`, { enabled });
  const scenes = useWsQuery<SceneList>(`/timelines/${timelineId}/scenes`, { enabled });
  const media = useWsQuery<MediaList>("/assets/media?limit=200", { enabled });

  /* Read the canonical document through the canonical adapter so the rail
   * counts exactly what the engine shows. `sortedTracks` is the same call the
   * engine makes; nothing here re-derives track order. */
  const trackSummary = useMemo(() => {
    const doc = timeline.data;
    // An UNREAD timeline is not an empty one. Returning null renders "not
    // loaded"; returning {tracks: 0} would claim the timeline has no tracks,
    // which is a different and untrue statement (Work 16.5.2 §8/§16).
    if (!doc) return null;
    const tracks = sortedTracks(doc);
    return {
      tracks: tracks.length,
      populated: tracks.filter((t) => t.clips.length > 0).length,
      families: [...new Set(tracks.map((t) => TRACK_FAMILY[t.kind] ?? "other"))].sort(),
      order: TRACK_ORDER.filter((kind) => tracks.some((t) => t.kind === kind)),
    };
  }, [timeline.data]);

  const visibleAssets = useMemo(() => {
    const items = media.data?.items ?? null;
    if (!items) return null;
    const needle = assetSearch.trim().toLowerCase();
    return items.filter((asset) => {
      if (assetType && asset.type !== assetType) return false;
      if (!needle) return true;
      return [asset.type, asset.origin, asset.provider, asset.mime_type]
        .join(" ")
        .toLowerCase()
        .includes(needle);
    });
  }, [media.data, assetType, assetSearch]);

  const assetTypes = useMemo(() => {
    const set = new Set<string>();
    for (const asset of media.data?.items ?? []) set.add(asset.type);
    return [...set].sort();
  }, [media.data]);

  const assetColumns: Column<MediaAsset>[] = [
    { key: "type", header: "Asset", cell: (a) => <Badge tone="neutral">{humanize(a.type)}</Badge> },
    { key: "origin", header: "Origin", cell: (a) => humanize(a.origin) },
    { key: "provider", header: "Provider", cell: (a) => a.provider || <span className="ym-muted">not reported</span> },
    {
      key: "tracks",
      header: "Feeds tracks",
      cell: (a) => {
        const kinds = trackKindsFor(a);
        if (!kinds.length) return <span className="ym-muted">no mapping</span>;
        return (
          <span style={{ display: "inline-flex", gap: "var(--space-1)", flexWrap: "wrap" }}>
            {kinds.map((k) => (
              <Badge key={k} tone="info">
                {k}
              </Badge>
            ))}
          </span>
        );
      },
    },
    {
      key: "duration",
      header: "Duration",
      align: "right",
      cell: (a) => (a.duration_seconds === null ? <span className="ym-muted">—</span> : seconds(a.duration_seconds)),
      hideBelow: "sm",
    },
    {
      key: "dimensions",
      header: "Size",
      align: "right",
      cell: (a) =>
        a.width === null || a.height === null ? (
          <span className="ym-muted">—</span>
        ) : (
          `${a.width}×${a.height}`
        ),
      hideBelow: "md",
    },
  ];

  const sceneColumns: Column<SceneRow>[] = [
    { key: "index", header: "#", align: "right", cell: (s) => s.index },
    { key: "title", header: "Scene", cell: (s) => s.title || s.id.slice(0, 8) },
    {
      key: "range",
      header: "Range",
      align: "right",
      cell: (s) => `${seconds(s.start_seconds)} → ${seconds(s.end_seconds)}`,
    },
    {
      key: "intent",
      header: "Visual intent",
      cell: (s) => s.visual_intent || <span className="ym-muted">not stated</span>,
      hideBelow: "md",
    },
    {
      key: "assets",
      header: "Refs",
      align: "right",
      cell: (s) => (s.assets?.length ? s.assets.length : <span className="ym-muted">—</span>),
      hideBelow: "lg",
    },
    {
      key: "captions",
      header: "Captions",
      align: "right",
      cell: (s) => (s.captions?.length ? s.captions.length : <span className="ym-muted">—</span>),
      hideBelow: "lg",
    },
  ];

  if (!workspaceId) {
    return (
      <>
        <Panel title="No workspace selected">
          <EmptyState title="Nothing to edit" description="Timelines are workspace-scoped." />
        </Panel>
      </>
    );
  }

  if (!timelineId) {
    return (
      <>
        <Panel title="No timeline selected">
          <EmptyState
            title="Nothing to edit"
            description="Open Studio and pick a timeline. The editor needs a canonical timeline id to load."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      {/* ---- header facts, from the canonical document --------------------- */}
      <QueryBoundary query={timeline} skeletonRows={2}>
        {(doc) => (
          <Panel
            title={doc.name || "Timeline"}
            subtitle={`Assets, scenes, preview, inspector — over the canonical timeline at version ${doc.version}.`}
            dense
          >
            <Grid min={170} gap="sm">
              <StatTile label="Version" value={`v${doc.version}`} hint="base_version for every save" source="GET /timelines/{id}" />
              <StatTile
                label="Duration"
                value={doc.duration_seconds > 0 ? seconds(doc.duration_seconds) : undefined}
                unavailable={!(doc.duration_seconds > 0)}
                hint="An empty timeline has no duration — that is not 0 seconds of content"
                source="GET /timelines/{id}"
              />
              <StatTile label="Aspect" value={doc.aspect_ratio || undefined} unavailable={!doc.aspect_ratio} source="GET /timelines/{id}" />
              <StatTile
                label="FPS"
                value={Number.isFinite(doc.fps) ? doc.fps.toFixed(0) : undefined}
                unavailable={!Number.isFinite(doc.fps)}
                source="GET /timelines/{id}"
              />
              <StatTile
                label="Tracks"
                value={trackSummary?.tracks}
                unavailable={trackSummary === null}
                hint={`${trackSummary?.populated ?? 0} with clips`}
                source="timelineAdapter.sortedTracks"
              />
              <StatTile
                label="Track families"
                value={trackSummary?.families.length}
                unavailable={trackSummary === null}
                hint={trackSummary?.families.join(", ") || undefined}
                source="timelineAdapter.TRACK_FAMILY"
              />
              <StatTile
                label="Project"
                value={doc.content_item_id ? doc.content_item_id.slice(0, 8) : undefined}
                unavailable={!doc.content_item_id}
                hint={doc.parent_timeline_id ? "Derived timeline" : "Root timeline"}
                source="GET /timelines/{id}"
              />
              <StatTile label="Updated" value={createdAt(doc.updated_at)} source="GET /timelines/{id}" />
            </Grid>
            {trackSummary && trackSummary.order.length > 0 ? (
              <p className="ym-hint">
                Canonical track order: {trackSummary.order.join(" → ")}
              </p>
            ) : null}
          </Panel>
        )}
      </QueryBoundary>

      {/* ---- the shell: Assets | Scenes rail over the engine --------------- */}
      <div style={{ display: "grid", gridTemplateColumns: "minmax(280px, 1fr) minmax(420px, 2.2fr)", gap: "var(--space-3)", alignItems: "start" }}>
        <Panel title="Library" dense>
          <Tabs
            tabs={[
              { id: "assets", label: "Assets" },
              { id: "scenes", label: "Scenes" },
            ]}
            active={rail}
            onChange={(id) => setRail(id as RailTab)}
          />

          {rail === "assets" ? (
            <div>
              <form
                onSubmit={(e) => e.preventDefault()}
                style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-end", flexWrap: "wrap" }}
              >
                <Field
                  label="Search assets"
                  type="search"
                  placeholder="type, origin, provider"
                  value={assetSearch}
                  onChange={(e) => setAssetSearch(e.target.value)}
                  style={{ flex: "1 1 160px" }}
                />
                <Select label="Type" value={assetType} onChange={(e) => setAssetType(e.target.value)}>
                  <option value="">All types</option>
                  {assetTypes.map((t) => (
                    <option key={t} value={t}>
                      {humanize(t)}
                    </option>
                  ))}
                </Select>
              </form>
              <QueryBoundary query={media} skeletonRows={5}>
                {() =>
                  !visibleAssets || visibleAssets.length === 0 ? (
                    <EmptyState
                      title="No asset matches"
                      description={
                        assetType || assetSearch
                          ? "GET /assets/media returned rows, none of which match. Clear the filters."
                          : "Nothing is registered yet. Renders, uploads, stock fetches and voice previews all land here."
                      }
                    />
                  ) : (
                    <DataTable
                      rows={visibleAssets}
                      columns={assetColumns}
                      rowKey={(a) => a.id}
                      caption="Assets available to this timeline"
                      maxHeight={420}
                      empty="No asset"
                    />
                  )
                }
              </QueryBoundary>
            </div>
          ) : (
            <QueryBoundary query={scenes} skeletonRows={5}>
              {(d) => (
                <DataTable
                  rows={d.scenes}
                  columns={sceneColumns}
                  rowKey={(s) => s.id}
                  caption="Scenes on this timeline"
                  maxHeight={480}
                  empty="No scene on this timeline"
                  emptyHint="POST /timelines/{id}/scenes replaces the whole set. The editor snaps to scene edges, so an empty set means snapping has nothing to snap to."
                />
              )}
            </QueryBoundary>
          )}

          <p className="ym-hint">
            This rail is read-only by design. Inserting a clip writes canonical operations through the engine
            below, so the edit lands in its undo stack and behind its version gate — a second write path here
            would be a second timeline.
          </p>
        </Panel>

        {/* ================================================================
         * THE CANONICAL ENGINE. Preview | Inspector over Timeline, with
         * inverseOps undo/redo, versions, waveform, captions, transitions,
         * effects, keyframes, music/audio, autosave and 409 stale-write
         * handling — all of it the engine's, none of it reimplemented here.
         * ============================================================== */}
        <CanonicalEditor />
      </div>
    </>
  );
}

export default StudioEditor;

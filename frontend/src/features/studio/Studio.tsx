/* Studio — the timeline index.
 *
 * ENDPOINT
 *
 *   GET /timelines[?content_item_id=]   api/v1/timelines.py `list_timelines`
 *                                       (line 106), dto `_dto` (line 47).
 *
 * That is the whole screen. A timeline is the canonical editing unit
 * (`ContentTimeline`), so the index is a list of canonical documents with their
 * version, not a second, derived model of one.
 *
 * THE TIMELINE ENGINE IS NOT HERE AND IS NOT REBUILT.
 *
 * Rows navigate to `/studio/:timelineId`, which renders `StudioEditor`. That
 * screen wraps the canonical engine (`pages/Editor` + `editor/adapters/
 * timelineAdapter`) rather than writing a second one; see the header of
 * `StudioEditor.tsx`.
 */

import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Field,
  Grid,
  PageHeader,
  Panel,
  QueryBoundary,
  StatTile,
  humanize,
  type Column,
} from "../../design-system/primitives";
import { useWsQuery } from "../../api/queries";
import { useSession } from "../../state/session";

/* ==========================================================================
 * Response shape — `timelines._dto`, backend/app/api/v1/timelines.py:47
 * ======================================================================= */

export type TimelineTrack = {
  id: string;
  kind: string;
  name: string;
  clips: { id: string; name?: string; start: number; duration: number }[];
};

export type TimelineRow = {
  id: string;
  workspace_id: string;
  content_item_id: string | null;
  video_id: string | null;
  name: string;
  fps: number;
  duration_seconds: number;
  aspect_ratio: string;
  tracks: TimelineTrack[];
  /** Optimistic-concurrency token. The save path refuses a stale base_version. */
  version: number;
  parent_timeline_id: string | null;
  created_at: string;
  updated_at: string;
};

export type TimelineList = { total: number; items: TimelineRow[] };

function createdAt(iso: string): string {
  return iso ? iso.replace("Z", " UTC").replace("T", " ").slice(0, 16) : "—";
}

function seconds(value: number): string {
  if (!Number.isFinite(value)) return "—";
  const m = Math.floor(value / 60);
  return `${String(m).padStart(2, "0")}:${(value % 60).toFixed(1).padStart(4, "0")}`;
}

function clipCount(row: TimelineRow): number {
  return (row.tracks ?? []).reduce((sum, track) => sum + (track.clips?.length ?? 0), 0);
}

export function Studio() {
  const { workspaceId } = useSession();
  const navigate = useNavigate();
  const enabled = Boolean(workspaceId);

  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");

  const list = useWsQuery<TimelineList>("/timelines", { enabled });

  const rows = list.data?.items ?? null;
  const total = list.data?.total ?? null;

  const stats = useMemo(() => {
    if (!rows) return null;
    const kinds = new Set<string>();
    let clips = 0;
    let derived = 0;
    let contentItems = 0;
    for (const row of rows) {
      for (const track of row.tracks ?? []) {
        kinds.add(track.kind);
        clips += track.clips?.length ?? 0;
      }
      if (row.parent_timeline_id) derived += 1;
      if (row.content_item_id) contentItems += 1;
    }
    return {
      onPage: rows.length,
      clips,
      trackKinds: kinds.size,
      derived,
      contentItems,
      duration: rows.reduce((sum, row) => sum + (Number(row.duration_seconds) || 0), 0),
      /* A timeline with no clips has duration_seconds = 0, and a row whose
       * duration never got written has nothing at all. Counting the two
       * together is how "0" becomes a KPI nobody can act on. */
      durationReported: rows.filter((row) => Number.isFinite(row.duration_seconds) && row.duration_seconds > 0).length,
    };
  }, [rows]);

  /* `list_timelines` accepts only `content_item_id`, so the free-text search runs
   * over the loaded page. Stated rather than hidden. */
  const visible = useMemo(() => {
    if (!rows) return null;
    const needle = appliedSearch.trim().toLowerCase();
    if (!needle) return rows;
    return rows.filter((row) =>
      [row.name, row.id, row.content_item_id ?? "", row.aspect_ratio]
        .join(" ")
        .toLowerCase()
        .includes(needle),
    );
  }, [rows, appliedSearch]);

  const columns: Column<TimelineRow>[] = [
    { key: "name", header: "Timeline", cell: (t) => t.name || t.id.slice(0, 8) },
    {
      key: "version",
      header: "Version",
      align: "right",
      cell: (t) => <Badge tone="info">v{t.version}</Badge>,
    },
    {
      key: "duration",
      header: "Duration",
      align: "right",
      cell: (t) => (t.duration_seconds > 0 ? seconds(t.duration_seconds) : <span className="ym-muted">empty</span>),
    },
    { key: "aspect", header: "Aspect", cell: (t) => t.aspect_ratio || <span className="ym-muted">not reported</span> },
    {
      key: "fps",
      header: "FPS",
      align: "right",
      cell: (t) => (Number.isFinite(t.fps) ? t.fps.toFixed(0) : <span className="ym-muted">—</span>),
      hideBelow: "md",
    },
    {
      key: "clips",
      header: "Clips",
      align: "right",
      cell: (t) => clipCount(t),
      hideBelow: "sm",
    },
    {
      key: "content",
      header: "Project",
      cell: (t) =>
        t.content_item_id ? (
          <Button size="sm" variant="ghost" onClick={() => navigate(`/projects/${t.content_item_id}`)}>
            {t.content_item_id.slice(0, 8)}
          </Button>
        ) : (
          <span className="ym-muted">unlinked</span>
        ),
      hideBelow: "lg",
    },
    {
      key: "lineage",
      header: "Lineage",
      cell: (t) =>
        t.parent_timeline_id ? (
          <Badge tone="handoff" title={`Derived from ${t.parent_timeline_id}`}>
            derived
          </Badge>
        ) : (
          <span className="ym-muted">root</span>
        ),
      hideBelow: "lg",
    },
    { key: "updated", header: "Updated", cell: (t) => createdAt(t.updated_at), hideBelow: "lg" },
  ];

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Studio" description="Every canonical timeline in this workspace." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to list"
            description="Timelines are workspace-scoped. Select or create a workspace to load its timelines."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Studio"
        description="Canonical timelines. Opening one gives the assets/scenes rail, preview, inspector and the versioned timeline."
        actions={<Button onClick={list.reload}>Refresh</Button>}
      />

      <Panel title="Find a timeline" dense>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            setAppliedSearch(search.trim());
          }}
          style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-end", flexWrap: "wrap" }}
        >
          <Field
            label="Search"
            type="search"
            placeholder="name, id, aspect"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            style={{ flex: "1 1 240px" }}
            hint="Filtering runs over the loaded page: GET /timelines accepts only content_item_id."
          />
          <Button type="submit" variant="primary">
            Search
          </Button>
          <Button
            variant="ghost"
            disabled={!appliedSearch}
            onClick={() => {
              setSearch("");
              setAppliedSearch("");
            }}
          >
            Clear
          </Button>
        </form>
      </Panel>

      <Panel title="Library at a glance" dense>
        <Grid min={170} gap="sm">
          <StatTile
            label="Timelines on this page"
            value={stats?.onPage}
            unavailable={stats === null}
            hint={total === null ? undefined : `${total} total in this workspace`}
            source="GET /timelines"
          />
          <StatTile
            label="Clips"
            value={stats?.clips}
            unavailable={stats === null}
            hint="Summed over the returned documents"
            source="GET /timelines"
          />
          <StatTile
            label="Track kinds in use"
            value={stats?.trackKinds}
            unavailable={stats === null}
            hint="video, broll, avatar, text, caption, voice, music, sfx"
            source="GET /timelines"
          />
          <StatTile
            label="Total duration"
            value={stats ? seconds(stats.duration) : undefined}
            unavailable={stats === null}
            source="GET /timelines"
          />
          <StatTile
            label="With a duration"
            value={stats?.durationReported}
            unavailable={stats === null || stats.durationReported === 0}
            hint="No timeline on this page reports a duration"
            source="GET /timelines"
          />
          <StatTile
            label="Linked to a project"
            value={stats?.contentItems}
            unavailable={stats === null}
            source="GET /timelines"
          />
          <StatTile
            label="Derived timelines"
            value={stats?.derived}
            unavailable={stats === null}
            hint="Has a parent_timeline_id — a fork, never an overwrite"
            source="GET /timelines"
          />
        </Grid>
      </Panel>

      <Panel title="Timelines" subtitle="Newest first." dense>
        <QueryBoundary query={list} skeletonRows={6}>
          {() =>
            !visible || visible.length === 0 ? (
              <EmptyState
                title={appliedSearch ? "No timeline matches that search" : "No timeline yet"}
                description={
                  appliedSearch
                    ? "The backend returned rows, none of which match. Clear the search to see the whole page."
                    : "A timeline appears here once a project imports a render or an operator creates one. Editing happens in the canonical engine, never in this list."
                }
              />
            ) : (
              <DataTable
                rows={visible}
                columns={columns}
                rowKey={(t) => t.id}
                caption="Timelines"
                onRowClick={(t) => navigate(`/studio/${t.id}`)}
                maxHeight={640}
                empty="No timeline"
              />
            )
          }
        </QueryBoundary>
      </Panel>
    </>
  );
}

export default Studio;

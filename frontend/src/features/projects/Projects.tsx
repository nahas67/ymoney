/* Projects — the content library as a list of projects.
 *
 * One content item IS a project (routes/registry.ts: "One workspace per
 * project"), so this list is `GET /content` with the backend's own search,
 * status and campaign filters. Filtering is done by the server, not re-derived
 * here: a client-side stage filter would be a second, silently-divergent copy
 * of the backend's query, and this screen keeps no state that the API does not
 * already own.
 *
 * Every list state is explicit — loading, empty and error — via `QueryBoundary`,
 * and a status column that the backend did not report reads as a dash rather
 * than as a guess.
 */

import { useMemo, useState, type FormEvent } from "react";
import { useNavigate } from "react-router-dom";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Field,
  Grid,
  Money,
  PageHeader,
  Panel,
  QueryBoundary,
  Select,
  StatTile,
  StatusBadge,
  humanize,
  type Column,
  type Tone,
} from "../../design-system/primitives";
import { useWsQuery } from "../../api/queries";
import { useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes — app/api/v1/content.py `_serialize_content` / `list_content`
 * ======================================================================= */

type VideoRow = {
  id: string;
  status: string;
  engine: string;
  progress: number;
  duration_seconds: number | null;
  error: string;
  quality: number | null;
  quality_passed: boolean | null;
  quality_notes: string;
  aspect_ratio: string | null;
};
type ContentRow = {
  id: string;
  topic: string;
  status: string;
  campaign_id: string | null;
  cycle_id: string | null;
  strategy: Record<string, unknown>;
  error: string | null;
  video: VideoRow | null;
  variants_count: number;
  created_at: string;
};
type ContentList = { total: number; items: ContentRow[] };
type CampaignRow = { id: string; name: string; status: string };
type CampaignList = { items: CampaignRow[] };

/** `ContentStatus` in backend/app/models/base.py. */
const CONTENT_STATUSES = [
  "IDEA",
  "RESEARCHING",
  "STRATEGY",
  "SCRIPTING",
  "SCRIPT_READY",
  "PRODUCTION",
  "QC",
  "APPROVED",
  "SCHEDULED",
  "PUBLISHED",
  "ANALYZING",
  "LEARNED",
  "FAILED",
  "SKIPPED",
] as const;

/** States after which no further pipeline work happens. */
const TERMINAL_STATUSES = ["PUBLISHED", "ANALYZING", "LEARNED", "FAILED", "SKIPPED"];
/** States where a render is in flight or waiting for one. */
const RENDER_STATUSES = ["PRODUCTION", "QC", "APPROVED"];

/** The tone of the worst thing known about a row, not the tone of its name. */
function worstTone(item: ContentRow): Tone {
  const vs = item.video?.status?.toUpperCase() ?? "";
  if (vs === "FAILED" || vs === "DEAD") return "danger";
  if (item.video?.quality_passed === false) return "danger";
  if (item.status.toUpperCase() === "FAILED") return "danger";
  return "neutral";
}

function badge(item: ContentRow): React.ReactNode {
  const vs = item.video?.status ?? "";
  if (vs) return <StatusBadge status={vs} />;
  if (item.status.toUpperCase() === "FAILED") return <Badge tone="danger">No render</Badge>;
  return <span className="ym-muted">not rendered</span>;
}

function created(iso: string): string {
  return iso ? iso.replace("Z", " UTC").replace("T", " ").slice(0, 16) : "—";
}

export function Projects() {
  const { workspaceId } = useSession();
  const navigate = useNavigate();

  /* Filter inputs. These describe the QUERY, not the data — the rows always come
   * from the response, so a filter that matches nothing shows an empty state,
   * not a stale list. */
  const [search, setSearch] = useState("");
  const [appliedSearch, setAppliedSearch] = useState("");
  const [status, setStatus] = useState("");
  const [campaignId, setCampaignId] = useState("");

  const listPath = useMemo(() => {
    const params = new URLSearchParams();
    params.set("limit", "200");
    if (appliedSearch) params.set("search", appliedSearch);
    if (status) params.set("status", status);
    if (campaignId) params.set("campaign_id", campaignId);
    return `/content?${params.toString()}`;
  }, [appliedSearch, status, campaignId]);

  const list = useWsQuery<ContentList>(listPath);
  const campaigns = useWsQuery<CampaignList>("/campaigns", { enabled: Boolean(workspaceId) });

  const items = list.data?.items ?? null;
  const total = list.data?.total ?? null;

  const derived = useMemo(() => {
    if (!items) return null;
    const byStatus = new Map<string, number>();
    for (const item of items) byStatus.set(item.status, (byStatus.get(item.status) ?? 0) + 1);
    return {
      inFlight: items.filter((i) => !TERMINAL_STATUSES.includes(i.status.toUpperCase())).length,
      rendering: items.filter((i) => RENDER_STATUSES.includes(i.status.toUpperCase()) && i.video !== null).length,
      published: items.filter((i) => i.status.toUpperCase() === "PUBLISHED").length,
      failed: items.filter((i) => i.status.toUpperCase() === "FAILED" || i.video?.quality_passed === false).length,
      needsAttention: items.filter((i) => worstTone(i) !== "neutral").length,
      byStatus: [...byStatus.entries()].sort((a, b) => b[1] - a[1]),
    };
  }, [items]);

  const onSubmit = (e: FormEvent) => {
    e.preventDefault();
    setAppliedSearch(search.trim());
  };

  const clearFilters = () => {
    setSearch("");
    setAppliedSearch("");
    setStatus("");
    setCampaignId("");
  };

  const filtersActive = Boolean(appliedSearch || status || campaignId);

  const columns: Column<ContentRow>[] = [
    {
      key: "topic",
      header: "Project",
      cell: (item) => (
        <span>
          {item.topic || item.id.slice(0, 8)}
          {item.error ? (
            <span className="ym-notif-detail"> — {item.error.slice(0, 120)}</span>
          ) : null}
        </span>
      ),
    },
    { key: "status", header: "Stage", cell: (item) => <StatusBadge status={item.status} /> },
    { key: "render", header: "Render", cell: (item) => badge(item) },
    {
      key: "quality",
      header: "QC",
      align: "right",
      cell: (item) => {
        const q = item.video?.quality;
        if (q === null || q === undefined) return <span className="ym-muted">—</span>;
        return (
          <Badge tone={item.video?.quality_passed === false ? "danger" : q >= 70 ? "success" : "warning"}>
            {q.toFixed(0)}
          </Badge>
        );
      },
    },
    {
      key: "variants",
      header: "Variants",
      align: "right",
      cell: (item) => (item.variants_count ? item.variants_count : <span className="ym-muted">—</span>),
    },
    {
      key: "campaign",
      header: "Campaign",
      cell: (item) => {
        if (!item.campaign_id) return <span className="ym-muted">—</span>;
        const match = campaigns.data?.items.find((c) => c.id === item.campaign_id);
        return match?.name ?? item.campaign_id.slice(0, 8);
      },
      hideBelow: "lg",
    },
    {
      key: "cost",
      header: "Est. cost",
      align: "right",
      cell: (item) => {
        const value = item.strategy?.estimated_cost_usd;
        return typeof value === "number" ? <Money usd={value} /> : <span className="ym-muted">—</span>;
      },
      hideBelow: "lg",
    },
    { key: "created", header: "Created", cell: (item) => created(item.created_at), hideBelow: "md" },
  ];

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Projects" description="One workspace per project, from idea to publication." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to list"
            description="Projects are workspace-scoped. Select or create a workspace to load its content library."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Projects"
        description="Every content item as a project: research, script, render, review, publish."
        actions={<Button onClick={list.reload}>Refresh</Button>}
      />

      {/* ---- filters: server-side, keyboard operable ---------------------- */}
      <Panel title="Find a project" dense>
        <form className="ym-grid ym-grid--md" onSubmit={onSubmit} style={{ gridTemplateColumns: "minmax(220px, 2fr) minmax(150px, 1fr) minmax(150px, 1fr) auto" }}>
          <Field
            label="Search topics"
            type="search"
            placeholder="e.g. budgeting"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            hint="Filtering runs on the server; a non-matching search shows an empty list rather than stale rows."
          />
          <Select label="Stage" value={status} onChange={(e) => setStatus(e.target.value)}>
            <option value="">All stages</option>
            {CONTENT_STATUSES.map((s) => (
              <option key={s} value={s}>
                {humanize(s)}
              </option>
            ))}
          </Select>
          <Select label="Campaign" value={campaignId} onChange={(e) => setCampaignId(e.target.value)}>
            <option value="">All campaigns</option>
            {(campaigns.data?.items ?? []).map((c) => (
              <option key={c.id} value={c.id}>
                {c.name}
              </option>
            ))}
          </Select>
          <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-end" }}>
            <Button type="submit" variant="primary">
              Search
            </Button>
            <Button variant="ghost" onClick={clearFilters} disabled={!filtersActive}>
              Clear
            </Button>
          </div>
        </form>
      </Panel>

      {/* ---- counts: derived from the returned rows only ------------------ */}
      <Panel title="Library at a glance" dense>
        <Grid min={180} gap="sm">
          <StatTile
            label="Projects on this page"
            value={items === null ? undefined : items.length}
            unavailable={items === null}
            hint={total === null ? undefined : `${total} total in this workspace`}
            source="GET /content"
          />
          <StatTile
            label="In flight"
            value={derived?.inFlight}
            unavailable={derived === null}
            hint="Not yet published, learned, failed or skipped"
            source="GET /content"
          />
          <StatTile
            label="Rendered or rendering"
            value={derived?.rendering}
            unavailable={derived === null}
            source="GET /content"
          />
          <StatTile
            label="Published"
            value={derived?.published}
            unavailable={derived === null}
            tone="success"
            source="GET /content"
          />
          <StatTile
            label="Failed / QC rejected"
            value={derived?.failed}
            unavailable={derived === null}
            tone={derived && derived.failed > 0 ? "danger" : "neutral"}
            source="GET /content"
          />
          <StatTile
            label="Needs attention"
            value={derived?.needsAttention}
            unavailable={derived === null}
            tone={derived && derived.needsAttention > 0 ? "danger" : "neutral"}
            hint="Failed render or rejected QC"
            source="GET /content"
          />
        </Grid>
      </Panel>

      {/* ---- the list ---------------------------------------------------- */}
      <Panel
        title="Projects"
        subtitle={filtersActive ? "Filtered by the server." : "Newest first."}
        dense
      >
        <QueryBoundary query={list} skeletonRows={6}>
          {(d) => (
            <DataTable
              rows={d.items}
              columns={columns}
              rowKey={(item) => item.id}
              caption="Projects"
              maxHeight={720}
              empty={filtersActive ? "No project matches these filters" : "No project yet"}
              emptyHint={
                filtersActive
                  ? "The backend returned an empty page for this query — widen the search or clear the filters."
                  : "A project appears here once the planner or an operator creates a content item."
              }
              onRowClick={(item) => navigate(`/projects/${item.id}`)}
            />
          )}
        </QueryBoundary>
      </Panel>

      {/* ---- stage distribution: the server's own statuses ---------------- */}
      <Panel title="Stages present in this page" dense>
        <QueryBoundary query={list} skeletonRows={3}>
          {(d) =>
            d.items.length === 0 ? (
              <EmptyState title="Nothing to group" description="No rows were returned for this query." />
            ) : (
              <DataTable
                rows={(derived?.byStatus ?? []).map(([status, count]) => ({ status, count }))}
                columns={[
                  { key: "status", header: "Stage", cell: (r) => <StatusBadge status={r.status} /> },
                  { key: "count", header: "Projects", align: "right", cell: (r) => r.count },
                ]}
                rowKey={(r) => r.status}
                caption="Stage distribution"
                empty="No row"
              />
            )
          }
        </QueryBoundary>
      </Panel>
    </>
  );
}

export default Projects;

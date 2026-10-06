/* Command Center — the operator's "what is YMONEY doing right now".
 *
 * Six questions, one screen:
 *   what is running · what needs attention · what is performing
 *   what is costing money · what is blocked · what should happen next
 *
 * Three rules this screen is built around, and they are the reason it is not a
 * grid of `useQuery` calls:
 *
 * 1. NO INVENTED KPIs. Every number here is counted from a field an endpoint
 *    actually returns. When a panel 404s, times out or is missing a field, the
 *    tile says UNAVAILABLE — never 0. A dashboard that renders 0 for "we don't
 *    know" is how a dead provider looks like a quiet afternoon.
 *
 * 2. ONE FAILED PANEL MUST NOT BLANK THE PAGE. `useCombinedQueries` fans out
 *    every read, keeps whatever arrived, and reports *which* panel failed in a
 *    dedicated panel. The operator sees the outage and the surviving data at
 *    the same time.
 *
 * 3. SUBMISSION_UNKNOWN IS ITS OWN CATEGORY. `toneForStatus` maps it to the
 *    `unknown` tone (purple) and this screen never overrides that to `warning`:
 *    the provider MAY have accepted and billed the request. It is reported
 *    verbatim, with the backend's own `recommended_action` and `retry_safe`, and
 *    this screen renders NO retry affordance at all — a blind resubmit is how one
 *    ambiguous charge becomes two.
 *
 * Endpoint shapes are typed from the backend routers (the OpenAPI document has
 * empty response schemas, so the routers are the source of truth).
 */

import { useMemo } from "react";
import { useNavigate } from "react-router-dom";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Grid,
  Money,
  PageHeader,
  Panel,
  QueryBoundary,
  Skeleton,
  StatTile,
  StatusBadge,
  humanize,
  toneForStatus,
  type Column,
  type Tone,
} from "../../design-system/primitives";
import { useCombinedQueries, type QueryState } from "../../api/queries";
import { api, wsApi } from "../../lib/api";
import { useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes — from app/api/v1/*.py
 * ======================================================================= */

type CampaignRow = {
  id: string;
  name: string;
  goal: string;
  status: string;
  target_videos: number;
  videos_per_day: number;
  platforms: string[];
  automation_level: string;
  budget_daily_usd: number | null;
  starts_at: string | null;
  ends_at: string | null;
};
type CampaignList = { items: CampaignRow[] };

type VideoRow = {
  id: string;
  status: string;
  engine: string;
  file_path: string;
  thumbnail_path: string;
  progress: number;
  duration_seconds: number | null;
  error: string;
  quality: number | null;
  quality_passed: boolean | null;
  quality_notes: string;
  quality_components: Record<string, unknown>;
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

type ScheduleRow = {
  id: string;
  platform: string;
  run_at: string;
  content_item_id: string;
  campaign_id: string | null;
  status: string;
};
type ScheduleList = { items: ScheduleRow[] };

type JobRow = {
  id: string;
  type: string;
  cycle_id: string | null;
  status: string;
  priority: number;
  retry_count: number;
  max_retries: number;
  next_run_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  last_error: string;
  result: Record<string, unknown>;
  created_at: string;
  claimed_by: string;
  lease_state: string;
};
type JobList = { items: JobRow[] };

/* HONESTY (Work 16.5.7 §8): `spent_last_24h_usd` is nullable. It is null for an
 * empty window and for a window holding an UNKNOWN_EXPOSURE row -- money
 * possibly spent that nobody can price is not $0.00. `within_budget` and
 * `remaining_usd` are BUDGET GATES and stay non-null on purpose: an unresolved
 * exposure consumes headroom rather than creating it. */
type CostSummary = {
  last_24h_by_category: Record<string, number>;
  spent_last_24h_usd: number | null;
  spent_last_24h_unknown_exposure_rows: number;
  daily_budget_usd: number;
  per_video_budget_usd: number;
  within_budget: boolean;
  remaining_usd: number;
};

type ChainLeg = {
  tier: string;
  target: string;
  outcome: string;
  paid: boolean;
  attempt: number;
  paid_attempt: number;
  model: string;
  estimated_usd: number;
  fell_through: boolean;
  reason: string;
};
type ChainRecord = {
  task_type: string;
  legs: ChainLeg[];
  stop_reason: string;
  paid_legs: number;
  estimated_exposure_usd: number;
  at: string;
  succeeded_tier: string;
};
type ChainsResponse = { chains: ChainRecord[]; note: string };

type PlannerOpportunity = {
  id: string;
  topic: string;
  basis: string;
  basis_meaning: string;
  angle: string | null;
  platforms: string[] | null;
  score: number | null;
  confidence: number | null;
  dedupe_verdict: string | null;
  dedupe_reason: string | null;
  estimated_cost_usd: number | null;
  why: string;
};
type PlannerOpportunities = {
  count: number;
  opportunities: PlannerOpportunity[];
  forbidden_claims: string[];
  note: string;
};

type CommunityOpportunity = {
  id: string;
  opportunity_type: string;
  title: string;
  evidence_count: number;
  confidence: number | null;
  state: string;
  created_at: string;
};
type InboxOpportunities = { items: CommunityOpportunity[] };

type ReadinessCheck = {
  id: string;
  status: string;
  blocking: boolean;
  tier: number;
  detail: string;
  latency_ms: number;
  remediation: string;
};
type Readiness = {
  status: string;
  checked_at: string;
  checks: ReadinessCheck[];
  blocking_failures: string[];
  message: string;
};

type Incident = {
  incident_id: string;
  source: string;
  provider: string;
  operation: string;
  attempted_at: string;
  remote_id: string;
  state: string;
  display_state: string;
  exposure: string;
  estimated_exposure_usd: number | null;
  exposure_unknown: boolean;
  recommended_action: string;
  retry_safe: boolean;
  may_resubmit: boolean;
  detail: string;
  note: string;
};
type Incidents = {
  items: Incident[];
  count: number;
  unknown_exposure_count: number;
  states: string[];
  note: string;
};

type ReviewRow = {
  id: string;
  project_id: string | null;
  target_type: string;
  title: string;
  state: string;
  stale: boolean;
  approval_valid: boolean;
  created_at: string;
  updated_at: string | null;
};
type ReviewList = { items: ReviewRow[] };

type PublishedPost = {
  id: string;
  platform: string;
  title: string;
  remote_url: string | null;
  published_at: string | null;
  is_mock: boolean;
  /* HONESTY (Work 16.5.7 §8): every field is nullable. The backend used to
   * report `views: 0` for a post no provider ever reported on, which rendered as
   * a real "0 views" in this table. null means no PostMetric snapshot exists. */
  metrics: {
    views: number | null;
    likes: number | null;
    comments: number | null;
    completion_rate: number | null;
  };
};
type PostList = { items: PublishedPost[] };

type Panels = {
  campaigns: CampaignList;
  content: ContentList;
  schedule: ScheduleList;
  jobs: JobList;
  costs: CostSummary;
  chains: ChainsResponse;
  planner: PlannerOpportunities;
  inbox: InboxOpportunities;
  readiness: Readiness;
  incidents: Incidents;
  reviews: ReviewList;
  posts: PostList;
};

const PANEL_LABEL: Record<keyof Panels, string> = {
  campaigns: "active campaigns",
  content: "content library",
  schedule: "scheduled content",
  jobs: "jobs & renders",
  costs: "spend",
  chains: "provider chains",
  planner: "planner opportunities",
  inbox: "inbox opportunities",
  readiness: "provider readiness",
  incidents: "paid incidents",
  reviews: "pending reviews",
  posts: "publications",
};

/** Job states that mean work is in flight. `JobStatus` in models/base.py. */
const ACTIVE_JOB_STATUSES = ["QUEUED", "RUNNING", "WAITING", "RETRYING"];
/** Review states that still need a human (engine/collab/reviews.py). */
const OPEN_REVIEW_STATES = ["DRAFT", "IN_REVIEW", "CHANGES_REQUESTED"];
/** Campaign states that are not finished. */
const CLOSED_CAMPAIGN_STATES = ["COMPLETED", "ARCHIVED", "CANCELLED"];
/** Content states that mean the pipeline gave up on this item. */
const FAILED_CONTENT_STATUSES = ["FAILED"];

/* ==========================================================================
 * Adapters
 * ======================================================================= */

/**
 * Turn one `useCombinedQueries` entry into the `QueryState` that
 * `QueryBoundary` expects, so a panel gets the same loading / error / empty
 * treatment as any single-query screen.
 *
 * `setData` is intentionally inert: the combined cache is owned by
 * `useCombinedQueries`, which re-reads every panel together. Handing a panel a
 * second, private write path would let it drift from its siblings.
 */
function panelState<T>(
  value: T | null,
  error: string | undefined,
  settled: boolean,
  reload: () => void,
): QueryState<T> {
  return {
    data: value,
    error: error ?? null,
    /*
     * No status, honestly. This layer aggregates panels that report a
     * pre-stringified `errors.<panel>` message, so the HTTP status was never in
     * scope here. `null` routes the refusal check to its wording classifier,
     * which is written against the backend's measured denial vocabulary
     * (`scripts/denial_vocabulary.py`). Plumbing a status through this merge would
     * be a larger refactor than the fix it buys: the classifier already covers
     * every string the backend actually emits.
     */
    errorStatus: null,
    loading: !settled && !error,
    settled,
    refreshing: false,
    reload,
    setData: () => undefined,
  };
}

function isActiveCampaign(c: CampaignRow): boolean {
  return !CLOSED_CAMPAIGN_STATES.includes(c.status.toUpperCase());
}

function isOpenReview(r: ReviewRow): boolean {
  return OPEN_REVIEW_STATES.includes(r.state.toUpperCase());
}

function isActiveJob(j: JobRow): boolean {
  return ACTIVE_JOB_STATUSES.includes(j.status.toUpperCase());
}

/** Count with an explicit "no data" branch — never a silent 0. */
function countOrNull(rows: unknown[] | null | undefined, predicate?: (row: never) => boolean): number | null {
  if (!Array.isArray(rows)) return null;
  return predicate ? rows.filter((r) => predicate(r as never)).length : rows.length;
}

/** A metric whose value came from a field that was not reported. */
function Metric({
  label,
  value,
  hint,
  source,
  tone = "neutral",
}: {
  label: string;
  value: number | null | undefined;
  hint?: string;
  source?: string;
  tone?: Tone;
}) {
  return (
    <StatTile
      label={label}
      unavailable={value === null || value === undefined || Number.isNaN(value)}
      value={value}
      tone={tone}
      hint={hint}
      source={source}
    />
  );
}

function utc(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("Z", " UTC").replace("T", " ").slice(0, 19);
}

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

export function CommandCenter() {
  const { workspaceId, workspace } = useSession();
  const navigate = useNavigate();

  const panels = useCombinedQueries<Panels>({
    campaigns: () => wsApi.get("/campaigns") as Promise<CampaignList>,
    content: () => wsApi.get("/content?limit=200") as Promise<ContentList>,
    schedule: () => wsApi.get("/calendar") as Promise<ScheduleList>,
    jobs: () => wsApi.get("/jobs?limit=100") as Promise<JobList>,
    costs: () => wsApi.get("/costs") as Promise<CostSummary>,
    chains: () => wsApi.get("/intelligence/routing/chains") as Promise<ChainsResponse>,
    planner: () => wsApi.get("/planner/opportunities") as Promise<PlannerOpportunities>,
    inbox: () => wsApi.get("/inbox/opportunities?limit=50") as Promise<InboxOpportunities>,
    readiness: () => api<Readiness>("GET", "/system/readiness"),
    incidents: () => wsApi.get("/provider-maturity/incidents?limit=50") as Promise<Incidents>,
    reviews: () => wsApi.get("/reviews") as Promise<ReviewList>,
    posts: () => wsApi.get("/publishing/posts?limit=25") as Promise<PostList>,
  });

  const { data, settled, errors, reload } = panels;

  const failed = useMemo(() => Object.entries(errors) as [keyof Panels, string][], [errors]);

  /* ---- derived views. Every one of these reads API fields only. --------- */

  const campaigns = data.campaigns?.items ?? null;
  const activeCampaigns = campaigns?.filter(isActiveCampaign) ?? null;

  const content = data.content?.items ?? null;
  const failedContent = content?.filter((c) => FAILED_CONTENT_STATUSES.includes(c.status.toUpperCase())) ?? null;
  const rendering = content?.filter((c) => {
    const vs = c.video?.status?.toUpperCase();
    return vs === "RENDERING" || vs === "PENDING" || vs === "QUEUED" || vs === "PROCESSING";
  }) ?? null;

  const schedule = data.schedule?.items ?? null;
  const upcoming = schedule?.filter((e) => e.status.toUpperCase() === "PENDING") ?? null;
  const failedSchedule = schedule?.filter((e) => e.status.toUpperCase() === "FAILED") ?? null;

  const jobs = data.jobs?.items ?? null;
  const activeJobs = jobs?.filter(isActiveJob) ?? null;
  const deadJobs = jobs?.filter((j) => j.status.toUpperCase() === "DEAD") ?? null;
  const queueCounts = useMemo(() => {
    if (!jobs) return null;
    const out: Record<string, number> = {};
    for (const j of jobs) out[j.status] = (out[j.status] ?? 0) + 1;
    return out;
  }, [jobs]);

  const costs = data.costs ?? null;
  const incidents = data.incidents?.items ?? null;
  const unknownSubmissions = incidents?.filter((i) => i.state.toUpperCase().includes("SUBMISSION_UNKNOWN")) ?? null;
  const reconcileIncidents = incidents?.filter((i) => i.recommended_action.toUpperCase().includes("RECONCILE")) ?? null;

  const readiness = data.readiness ?? null;
  const failedChecks = readiness?.checks.filter((c) => c.status !== "passed") ?? null;

  const openReviews = data.reviews?.items?.filter(isOpenReview) ?? null;
  const posts = data.posts?.items ?? null;
  const livePosts = posts?.filter((p) => !p.is_mock && p.published_at) ?? null;
  const mockPosts = posts?.filter((p) => p.is_mock) ?? null;
  /* HONESTY (§8): was `completion_rate !== null || views > 0`, which counted a
   * post as "measured" only if it reported completion or had a nonzero view
   * count. A post whose snapshot genuinely says 0 views was therefore excluded
   * from the measured count -- the same measured-zero confusion as above, in the
   * aggregate. `views !== null` is the fact. */
  const measuredPosts = livePosts?.filter((p) => p.metrics.views !== null) ?? null;

  const plannerOps = data.planner?.opportunities ?? null;
  const inboxOps = data.inbox?.items ?? null;
  const chains = data.chains?.chains ?? null;

  /* ---- alerts: a union of real conditions, ranked by tone --------------- */

  type Alert = { key: string; tone: Tone; what: string; detail: string };
  const alerts: Alert[] = [];
  for (const id of readiness?.blocking_failures ?? []) {
    const check = readiness?.checks.find((c) => c.id === id);
    alerts.push({
      key: `readiness-${id}`,
      tone: "danger",
      what: `Blocking dependency down: ${humanize(id)}`,
      detail: check?.remediation || check?.detail || "No remediation reported.",
    });
  }
  for (const inc of reconcileIncidents ?? []) {
    alerts.push({
      key: `inc-${inc.incident_id}`,
      tone: toneForStatus(inc.state),
      what: `Reconcile before acting: ${inc.provider} · ${inc.operation}`,
      detail: inc.detail || inc.note || "Reconciliation state reported by the paid executor.",
    });
  }
  for (const job of deadJobs ?? []) {
    alerts.push({
      key: `job-${job.id}`,
      tone: "danger",
      what: `Job dead: ${humanize(job.type)}`,
      detail: job.last_error || "No error recorded on the job row.",
    });
  }
  for (const item of failedContent ?? []) {
    alerts.push({
      key: `content-${item.id}`,
      tone: "danger",
      what: `Pipeline failed: ${item.topic}`,
      detail: item.error || "No error recorded on the content item.",
    });
  }
  for (const entry of failedSchedule ?? []) {
    alerts.push({
      key: `sched-${entry.id}`,
      tone: "danger",
      what: `Scheduled publication failed: ${humanize(entry.platform)}`,
      detail: "Calendar entry is FAILED. Re-dispatch from Calendar, not from here.",
    });
  }
  for (const rev of (openReviews ?? []).filter((r) => r.stale)) {
    alerts.push({
      key: `review-${rev.id}`,
      tone: "warning",
      what: `Approval invalidated by a new version: ${rev.title || rev.id.slice(0, 8)}`,
      detail: "The bound version is stale; the target changed after this review opened.",
    });
  }

  const openProject = (contentId: string) => navigate(`/projects/${contentId}`);

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Command Center" description="Workspace state at a glance." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to report yet"
            description="The Command Center reads workspace-scoped state. Pick or create a workspace to load it."
          />
        </Panel>
      </>
    );
  }

  /* ---- columns -------------------------------------------------------- */

  const campaignColumns: Column<CampaignRow>[] = [
    { key: "name", header: "Campaign", cell: (c) => c.name },
    { key: "status", header: "Status", cell: (c) => <StatusBadge status={c.status} /> },
    {
      key: "target",
      header: "Target",
      align: "right",
      cell: (c) => (c.target_videos ? `${c.target_videos} vids · ${c.videos_per_day}/day` : "—"),
    },
    {
      key: "budget",
      header: "Daily budget",
      align: "right",
      cell: (c) => <Money usd={c.budget_daily_usd} />,
    },
    { key: "window", header: "Window", cell: (c) => `${utc(c.starts_at)} → ${utc(c.ends_at)}`, hideBelow: "lg" },
  ];

  const scheduleColumns: Column<ScheduleRow>[] = [
    { key: "run_at", header: "Runs at (UTC)", cell: (e) => utc(e.run_at) },
    { key: "platform", header: "Platform", cell: (e) => humanize(e.platform) },
    { key: "status", header: "Status", cell: (e) => <StatusBadge status={e.status} /> },
    {
      key: "content",
      header: "Content",
      cell: (e) => {
        const topic = content?.find((c) => c.id === e.content_item_id)?.topic;
        return topic || e.content_item_id.slice(0, 8);
      },
      hideBelow: "md",
    },
  ];

  const jobColumns: Column<JobRow>[] = [
    { key: "type", header: "Job", cell: (j) => humanize(j.type) },
    { key: "status", header: "Status", cell: (j) => <StatusBadge status={j.status} /> },
    { key: "owner", header: "Worker", cell: (j) => j.claimed_by || "—", hideBelow: "lg" },
    {
      key: "retries",
      header: "Attempts",
      align: "right",
      cell: (j) => `${j.retry_count}/${j.max_retries}`,
    },
    {
      key: "submission",
      header: "Submission",
      cell: (j) => {
        const state = typeof j.result?.submission_state === "string" ? j.result.submission_state : null;
        return state ? <StatusBadge status={state} /> : <span className="ym-muted">—</span>;
      },
    },
    { key: "since", header: "Started", cell: (j) => utc(j.started_at ?? j.created_at), hideBelow: "md" },
  ];

  const incidentColumns: Column<Incident>[] = [
    {
      key: "state",
      header: "State",
      cell: (i) => (
        <Badge tone={toneForStatus(i.state)} dot title={`Verbatim backend state: ${i.state}`}>
          {humanize(i.display_state)}
        </Badge>
      ),
    },
    { key: "provider", header: "Provider", cell: (i) => i.provider },
    { key: "operation", header: "Operation", cell: (i) => i.operation, hideBelow: "md" },
    { key: "exposure", header: "Exposure", cell: (i) => <StatusBadge status={i.exposure} /> },
    {
      key: "amount",
      header: "Est. amount",
      align: "right",
      cell: (i) => (i.estimated_exposure_usd === null ? <span className="ym-muted">unknown</span> : <Money usd={i.estimated_exposure_usd} />),
    },
    {
      key: "action",
      header: "Recommended",
      cell: (i) => (
        <span title={i.detail || undefined}>
          {humanize(i.recommended_action)}
          {i.retry_safe ? "" : " · no retry offered"}
        </span>
      ),
    },
    { key: "at", header: "Attempted", cell: (i) => utc(i.attempted_at), hideBelow: "lg" },
  ];

  const reviewColumns: Column<ReviewRow>[] = [
    { key: "title", header: "Review", cell: (r) => r.title || r.id.slice(0, 8) },
    { key: "state", header: "State", cell: (r) => <StatusBadge status={r.state} /> },
    { key: "target", header: "Target", cell: (r) => humanize(r.target_type), hideBelow: "md" },
    {
      key: "stale",
      header: "Binding",
      cell: (r) =>
        r.stale ? (
          <Badge tone="warning">Approval invalidated</Badge>
        ) : r.approval_valid ? (
          <Badge tone="success">Bound</Badge>
        ) : (
          <span className="ym-muted">—</span>
        ),
    },
    { key: "updated", header: "Updated", cell: (r) => utc(r.updated_at ?? r.created_at), hideBelow: "lg" },
  ];

  const postColumns: Column<PublishedPost>[] = [
    { key: "title", header: "Publication", cell: (p) => p.title || p.id.slice(0, 8) },
    { key: "platform", header: "Platform", cell: (p) => humanize(p.platform) },
    {
      key: "mode",
      header: "Mode",
      cell: (p) => (
        <Badge tone={p.is_mock ? "mock" : "live"} title={p.is_mock ? "Published through mock publishing" : "Published live"}>
          {p.is_mock ? "MOCK" : "LIVE"}
        </Badge>
      ),
    },
    {
      key: "views",
      header: "Views",
      align: "right",
      /* HONESTY (§8): the old guard was `completion_rate === null && views === 0`
       * -- it read a per-post 0 as "no metric", so a post that genuinely recorded
       * ZERO views was also labelled unmeasured, and one that recorded zero views
       * AND a completion rate printed a bare "0" with no signal that anything was
       * missing. `views === null` is now the backend's own statement. */
      cell: (p) => (p.metrics.views === null ? <span className="ym-muted">no metric</span> : p.metrics.views),
    },
    {
      key: "completion",
      header: "Completion",
      align: "right",
      cell: (p) => pct(p.metrics.completion_rate),
    },
    { key: "at", header: "Published", cell: (p) => utc(p.published_at), hideBelow: "md" },
  ];

  const plannerColumns: Column<PlannerOpportunity>[] = [
    { key: "topic", header: "Opportunity", cell: (o) => o.topic },
    {
      key: "basis",
      header: "Basis",
      cell: (o) => (
        <Badge tone={o.basis === "OBSERVED" ? "success" : o.basis === "INFERRED" ? "info" : "warning"} title={o.basis_meaning}>
          {humanize(o.basis)}
        </Badge>
      ),
    },
    { key: "score", header: "Score", align: "right", cell: (o) => (o.score === null ? "—" : o.score.toFixed(1)) },
    {
      key: "verdict",
      header: "Dedupe",
      cell: (o) => (o.dedupe_verdict ? <StatusBadge status={o.dedupe_verdict} /> : <span className="ym-muted">—</span>),
      hideBelow: "md",
    },
    { key: "why", header: "Why", cell: (o) => o.why, hideBelow: "lg" },
  ];

  const inboxColumns: Column<CommunityOpportunity>[] = [
    { key: "title", header: "Signal", cell: (o) => o.title || o.id.slice(0, 8) },
    { key: "type", header: "Type", cell: (o) => humanize(o.opportunity_type) },
    { key: "state", header: "State", cell: (o) => <StatusBadge status={o.state} /> },
    { key: "evidence", header: "Evidence", align: "right", cell: (o) => o.evidence_count },
    {
      key: "confidence",
      header: "Confidence",
      align: "right",
      cell: (o) => pct(o.confidence),
      hideBelow: "md",
    },
  ];

  const chainColumns: Column<ChainRecord>[] = [
    { key: "task", header: "Task", cell: (c) => humanize(c.task_type) },
    {
      key: "legs",
      header: "Legs",
      align: "right",
      cell: (c) => `${c.legs.length}`,
    },
    {
      key: "paid",
      header: "Paid legs",
      align: "right",
      cell: (c) => <Badge tone={c.paid_legs > 0 ? "warning" : "neutral"}>{c.paid_legs}</Badge>,
    },
    {
      key: "exposure",
      header: "Est. exposure",
      align: "right",
      cell: (c) => <Money usd={c.estimated_exposure_usd} />,
    },
    { key: "stop", header: "Stopped because", cell: (c) => c.stop_reason || c.succeeded_tier || "—" },
    { key: "at", header: "At", cell: (c) => utc(c.at), hideBelow: "lg" },
  ];

  const readinessColumns: Column<ReadinessCheck>[] = [
    { key: "id", header: "Dependency", cell: (c) => humanize(c.id) },
    { key: "status", header: "Status", cell: (c) => <StatusBadge status={c.status} /> },
    { key: "block", header: "Blocking", cell: (c) => (c.blocking ? <Badge tone="danger">Blocking</Badge> : "—") },
    { key: "detail", header: "Detail", cell: (c) => c.detail },
    {
      key: "remediation",
      header: "Remediation",
      cell: (c) => (c.remediation ? c.remediation : <span className="ym-muted">—</span>),
      hideBelow: "lg",
    },
  ];

  const campaignState = panelState(data.campaigns, errors.campaigns, settled, reload);
  const scheduleState = panelState(data.schedule, errors.schedule, settled, reload);
  const jobState = panelState(data.jobs, errors.jobs, settled, reload);
  const costsState = panelState(data.costs, errors.costs, settled, reload);
  const incidentsState = panelState(data.incidents, errors.incidents, settled, reload);
  const readinessState = panelState(data.readiness, errors.readiness, settled, reload);
  const reviewsState = panelState(data.reviews, errors.reviews, settled, reload);
  const postsState = panelState(data.posts, errors.posts, settled, reload);
  const plannerState = panelState(data.planner, errors.planner, settled, reload);
  const inboxState = panelState(data.inbox, errors.inbox, settled, reload);
  const chainsState = panelState(data.chains, errors.chains, settled, reload);
  const contentState = panelState(data.content, errors.content, settled, reload);

  return (
    <>
      <PageHeader
        title="Command Center"
        description={
          workspace
            ? `${workspace.name} — what YMONEY is doing, what needs attention, what it is costing.`
            : "Workspace state at a glance."
        }
        actions={
          <Button onClick={reload} loading={!settled}>
            Refresh
          </Button>
        }
      />

      {/* Which panel failed is reported as data, not as a blank page. */}
      {failed.length > 0 && (
        <Panel
          title={`${failed.length} panel${failed.length === 1 ? "" : "s"} could not load`}
          subtitle="Everything below still renders; the metrics from these panels are UNAVAILABLE, not zero."
        >
          <div className="ym-notif-wrap">
            {failed.map(([key, message]) => (
              <div key={key} className="ym-notif-item">
                <Badge tone="danger" dot>
                  {PANEL_LABEL[key]}
                </Badge>
                <span className="ym-notif-detail">{message}</span>
                <Button size="sm" variant="ghost" onClick={reload}>
                  Reload
                </Button>
              </div>
            ))}
          </div>
        </Panel>
      )}

      {/* ---- headline metrics ------------------------------------------- */}
      <Panel title="At a glance" dense>
        <Grid min={190} gap="sm">
          <Metric
            label="Active campaigns"
            value={countOrNull(campaigns, isActiveCampaign as (row: never) => boolean)}
            source={campaigns ? `GET /campaigns` : undefined}
          />
          <Metric
            label="Scheduled to run"
            value={countOrNull(upcoming)}
            source={schedule ? `GET /calendar` : undefined}
          />
          <Metric
            label="Active jobs & renders"
            value={countOrNull(activeJobs)}
            source={jobs ? `GET /jobs` : undefined}
          />
          <Metric
            label="Pending reviews"
            value={countOrNull(openReviews)}
            source={data.reviews ? `GET /reviews` : undefined}
          />
          <StatTile
            label="SUBMISSION_UNKNOWN"
            value={countOrNull(unknownSubmissions)}
            unavailable={unknownSubmissions === null}
            tone={toneForStatus("SUBMISSION_UNKNOWN")}
            hint="May already be billed. Never shown as failed; no retry is offered."
            source={incidents ? `GET /provider-maturity/incidents` : undefined}
          />
          <StatTile
            label="Spend last 24h"
            /* HONESTY (§8): the tile is UNAVAILABLE when the total is null --
             * an empty ledger, or one holding an exposure nobody can price.
             * `<Money usd={null}>` already renders "unknown", but marking the
             * whole tile unavailable keeps it from reading as a figure. The gate
             * below stays a real verdict: unknown spend eats headroom. */
            unavailable={costs === null || costs.spent_last_24h_usd === null}
            value={costs ? <Money usd={costs.spent_last_24h_usd} tone={costs.within_budget ? "success" : "danger"} /> : undefined}
            hint={
              costs
                ? costs.spent_last_24h_usd === null
                  ? `No total: ${costs.spent_last_24h_unknown_exposure_rows} cost row(s) record an unpriceable exposure. Remaining is bounded conservatively at ${costs.remaining_usd.toFixed(4)}.`
                  : `${humanize(String(costs.within_budget ? "within budget" : "over budget"))} · remaining ${costs.remaining_usd.toFixed(4)}`
                : undefined
            }
            tone={costs === null || costs.spent_last_24h_usd === null ? "neutral" : costs.within_budget ? "success" : "danger"}
            source={costs ? `GET /costs` : undefined}
          />
          <Metric
            label="Blocking dependencies"
            value={readiness ? readiness.blocking_failures.length : null}
            tone={readiness && readiness.blocking_failures.length > 0 ? "danger" : "success"}
            source={readiness ? `GET /system/readiness` : undefined}
          />
          <Metric
            label="Needs reconciliation"
            value={countOrNull(reconcileIncidents)}
            tone="unknown"
            source={incidents ? `GET /provider-maturity/incidents` : undefined}
          />
        </Grid>
      </Panel>

      {/* ---- what needs attention --------------------------------------- */}
      <Panel
        title="What needs attention"
        subtitle="Blocking dependencies, dead jobs, failed items and money that must be reconciled."
        dense
      >
        {alerts.length === 0 ? (
          <EmptyState
            title="Nothing is blocked"
            description="No blocking dependency, dead job, failed content item or unreconciled paid incident."
          />
        ) : (
          <div className="ym-notif-wrap">
            {alerts.map((a) => (
              <div key={a.key} className="ym-notif-item">
                <Badge tone={a.tone} dot>
                  {humanize(a.tone)}
                </Badge>
                <div>
                  <div className="ym-notif-title">{a.what}</div>
                  <div className="ym-notif-detail">{a.detail}</div>
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <Grid min={340} gap="md">
        {/* ---- active campaigns ---------------------------------------- */}
        <Panel title="Active campaigns" dense>
          <QueryBoundary query={campaignState} skeletonRows={4}>
            {(d) => (
              <DataTable
                rows={d.items.filter(isActiveCampaign)}
                columns={campaignColumns}
                rowKey={(c) => c.id}
                caption="Active campaigns"
                empty="No active campaigns"
                emptyHint="Every campaign is completed, archived or cancelled."
                onRowClick={() => navigate("/campaigns")}
              />
            )}
          </QueryBoundary>
        </Panel>

        {/* ---- scheduled content --------------------------------------- */}
        <Panel title="Scheduled content" dense>
          <QueryBoundary query={scheduleState} skeletonRows={4}>
            {(d) => (
              <DataTable
                rows={d.items}
                columns={scheduleColumns}
                rowKey={(e) => e.id}
                caption="Scheduled content"
                empty="Nothing scheduled"
                emptyHint="No PENDING calendar entries. DISPATCHING, QUEUED and FAILED entries are listed here too."
                onRowClick={(e) => openProject(e.content_item_id)}
              />
            )}
          </QueryBoundary>
        </Panel>

        {/* ---- active jobs / renders ------------------------------------ */}
        <Panel title="Active jobs & renders" subtitle="Latest 100 jobs plus every content item whose video is still rendering." dense>
          <QueryBoundary query={jobState} skeletonRows={4}>
            {(d) => (
              <DataTable
                rows={d.items.filter(isActiveJob)}
                columns={jobColumns}
                rowKey={(j) => j.id}
                caption="Active jobs"
                empty="No job is queued, running, waiting or retrying"
                emptyHint="This panel reads the latest 100 jobs only; older queued work would not appear here."
              />
            )}
          </QueryBoundary>
          <QueryBoundary query={contentState} skeletonRows={2}>
            {(d) => (
              <DataTable
                rows={(rendering ?? []) as ContentRow[]}
                columns={[
                  { key: "topic", header: "Rendering", cell: (c) => c.topic },
                  { key: "status", header: "Status", cell: (c) => <StatusBadge status={c.video?.status} /> },
                  {
                    key: "progress",
                    header: "Progress",
                    align: "right",
                    cell: (c) => (typeof c.video?.progress === "number" ? `${c.video.progress}%` : "—"),
                  },
                  { key: "engine", header: "Engine", cell: (c) => c.video?.engine || "—", hideBelow: "md" },
                ]}
                rowKey={(c) => c.id}
                caption="Renders in flight"
                empty="No render is in flight"
                onRowClick={(c) => openProject(c.id)}
                maxHeight={220}
              />
            )}
          </QueryBoundary>
        </Panel>

        {/* ---- paid incidents (SUBMISSION_UNKNOWN) ---------------------- */}
        <Panel
          title="Paid incidents"
          subtitle="States travel verbatim from the paid executor. Nothing here is safe to resubmit unless retry_safe is true."
          dense
        >
          <QueryBoundary query={incidentsState} skeletonRows={4}>
            {(d) => (
              <>
                <Grid min={150} gap="sm">
                  <StatTile
                    label="SUBMISSION_UNKNOWN"
                    value={d.items.filter((i) => i.state.toUpperCase().includes("SUBMISSION_UNKNOWN")).length}
                    tone={toneForStatus("SUBMISSION_UNKNOWN")}
                    hint="Billed-but-unusable or 2xx-without-a-body"
                  />
                  <StatTile
                    label="Unknown exposure"
                    value={d.unknown_exposure_count}
                    tone={toneForStatus("UNKNOWN_EXPOSURE")}
                    hint="Accepted calls nobody can price"
                  />
                  <StatTile
                    label="Retry proven safe"
                    value={d.items.filter((i) => i.retry_safe).length}
                    tone="info"
                    hint="Provably undelivered submits only"
                  />
                </Grid>
                <DataTable
                  rows={d.items}
                  columns={incidentColumns}
                  rowKey={(i) => i.incident_id}
                  caption="Paid incidents"
                  empty="No paid incident is open"
                  emptyHint="No video submission and no cost entry is carrying an ambiguous state."
                />
              </>
            )}
          </QueryBoundary>
        </Panel>

        {/* ---- spend ---------------------------------------------------- */}
        <Panel title="Spend" subtitle="Last 24 hours against the daily budget." dense>
          <QueryBoundary query={costsState} skeletonRows={3}>
            {(d) => (
              <>
                <Grid min={150} gap="sm">
                  <StatTile
                    label="Spent 24h"
                    /* HONESTY (§8): null ⇒ UNAVAILABLE. `remaining_usd` and
                     * `within_budget` stay real: they are the budget GATE, and an
                     * unpriceable exposure must consume headroom, not create it.
                     *
                     * Not $0.0000 for an empty window: that reads "$0.00 spent",
                     * which is a claim, so the reason travels in the hint. */
                    unavailable={d.spent_last_24h_usd === null}
                    value={<Money usd={d.spent_last_24h_usd} tone={d.within_budget ? "success" : "danger"} />}
                    tone={d.spent_last_24h_usd === null ? "neutral" : d.within_budget ? "success" : "danger"}
                    hint={
                      d.spent_last_24h_usd === null
                        ? `No total: ${d.spent_last_24h_unknown_exposure_rows} cost row(s) record an exposure nobody can price.`
                        : undefined
                    }
                    source="GET /costs"
                  />
                  <StatTile
                    label="Daily budget"
                    value={<Money usd={d.daily_budget_usd} />}
                    source="GET /costs"
                  />
                  <StatTile
                    label="Remaining"
                    value={<Money usd={d.remaining_usd} tone={d.within_budget ? "success" : "danger"} />}
                    tone={d.within_budget ? "success" : "danger"}
                    hint="Bounded conservatively: an unpriceable exposure is treated as spend, never as room."
                  />
                  <StatTile label="Per video budget" value={<Money usd={d.per_video_budget_usd} />} source="GET /costs" />
                </Grid>
                <DataTable
                  rows={Object.entries(d.last_24h_by_category).map(([category, usd]) => ({ category, usd }))}
                  columns={[
                    { key: "category", header: "Category", cell: (r) => humanize(r.category) },
                    { key: "usd", header: "Spent 24h", align: "right", cell: (r) => <Money usd={r.usd} /> },
                  ]}
                  rowKey={(r) => r.category}
                  caption="Spend by category"
                  empty="No cost entry in the last 24 hours"
                  emptyHint="The ledger has no row for this window, which is different from a zero-cost day."
                />
              </>
            )}
          </QueryBoundary>
        </Panel>

        {/* ---- pending reviews ------------------------------------------ */}
        <Panel title="Pending reviews" dense>
          <QueryBoundary query={reviewsState} skeletonRows={3}>
            {(d) => (
              <DataTable
                rows={d.items.filter(isOpenReview)}
                columns={reviewColumns}
                rowKey={(r) => r.id}
                caption="Pending reviews"
                empty="No review is waiting on a human"
                emptyHint="DRAFT, IN_REVIEW and CHANGES_REQUESTED are the states that need somebody."
                onRowClick={() => navigate("/projects")}
              />
            )}
          </QueryBoundary>
        </Panel>

        {/* ---- recent publications -------------------------------------- */}
        <Panel title="Recent publications" subtitle="MOCK and LIVE never share a badge." dense>
          <QueryBoundary query={postsState} skeletonRows={4}>
            {(d) => (
              <>
                <Grid min={150} gap="sm">
                  <Metric label="Live publications" value={countOrNull(livePosts)} source="GET /publishing/posts" />
                  <Metric label="Mock publications" value={countOrNull(mockPosts)} tone="mock" source="GET /publishing/posts" />
                  <Metric
                    label="With a metric snapshot"
                    value={countOrNull(measuredPosts)}
                    hint="Others report no metric row yet"
                    source="GET /publishing/posts"
                  />
                </Grid>
                <DataTable
                  rows={d.items}
                  columns={postColumns}
                  rowKey={(p) => p.id}
                  caption="Recent publications"
                  empty="Nothing published yet"
                  emptyHint="A publication appears here once a publishing job wrote a post row."
                />
              </>
            )}
          </QueryBoundary>
        </Panel>

        {/* ---- planner opportunities ------------------------------------ */}
        <Panel
          title="Planner opportunities"
          subtitle={data.planner?.note}
          dense
        >
          <QueryBoundary query={plannerState} skeletonRows={4}>
            {(d) => (
              <DataTable
                rows={d.opportunities}
                columns={plannerColumns}
                rowKey={(o) => o.id}
                caption="Planner opportunities"
                empty="No scored opportunity"
                emptyHint="The planner has not scored anything for this workspace yet."
              />
            )}
          </QueryBoundary>
        </Panel>

        {/* ---- inbox opportunities -------------------------------------- */}
        <Panel title="Inbox opportunities" subtitle="Lead, partnership and request signals with their evidence count." dense>
          <QueryBoundary query={inboxState} skeletonRows={4}>
            {(d) => (
              <DataTable
                rows={d.items}
                columns={inboxColumns}
                rowKey={(o) => o.id}
                caption="Inbox opportunities"
                empty="No community signal"
                emptyHint="The inbox has no lead, partnership or request opportunity."
                onRowClick={() => navigate("/inbox")}
              />
            )}
          </QueryBoundary>
        </Panel>

        {/* ---- provider readiness --------------------------------------- */}
        <Panel
          title="Provider health"
          subtitle={readiness?.message}
          dense
        >
          <QueryBoundary query={readinessState} skeletonRows={4}>
            {(d) => (
              <DataTable
                rows={d.checks}
                columns={readinessColumns}
                rowKey={(c) => c.id}
                caption="Readiness probes"
                empty="No readiness probe reported"
                emptyHint="The readiness service returned no checks."
              />
            )}
          </QueryBoundary>
        </Panel>

        {/* ---- queue health --------------------------------------------- */}
        <Panel title="Queue health" subtitle="Status histogram over the latest 100 jobs." dense>
          <QueryBoundary query={jobState} skeletonRows={3}>
            {() =>
              queueCounts === null ? (
                <Metric label="Jobs" value={null} />
              ) : Object.keys(queueCounts).length === 0 ? (
                <EmptyState title="No job recorded" description="This workspace has no job row at all." />
              ) : (
                <DataTable
                  rows={Object.entries(queueCounts).map(([status, count]) => ({ status, count }))}
                  columns={[
                    { key: "status", header: "Status", cell: (r) => <StatusBadge status={r.status} /> },
                    { key: "count", header: "Jobs", align: "right", cell: (r) => r.count },
                  ]}
                  rowKey={(r) => r.status}
                  caption="Job status histogram"
                  empty="No job recorded"
                />
              )
            }
          </QueryBoundary>
        </Panel>

        {/* ---- routing chains ------------------------------------------- */}
        <Panel title="Provider chains" subtitle={data.chains?.note} dense>
          <QueryBoundary query={chainsState} skeletonRows={4}>
            {(d) => (
              <DataTable
                rows={d.chains}
                columns={chainColumns}
                rowKey={(c) => `${c.task_type}-${c.at}`}
                caption="Routed executions"
                empty="No routed execution recorded"
                emptyHint="The chain log is in-process and bounded; an empty log is not a clean bill of health."
              />
            )}
          </QueryBoundary>
        </Panel>
      </Grid>

      {failed.length === 0 && settled && (
        <Panel dense>
          <p className="ym-hint">
            Every panel loaded. Figures come from the workspace endpoints named under each tile — nothing here is
            estimated, and a missing provider reads UNAVAILABLE rather than zero.
          </p>
        </Panel>
      )}

      {!settled && (
        <Panel dense>
          <Skeleton rows={2} />
        </Panel>
      )}
    </>
  );
}

export default CommandCenter;
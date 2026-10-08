/* Command Center — the operations console.
 *
 * This is NOT a card-grid dashboard. It answers the operator's questions in
 * order of urgency:
 *
 *   1. What needs me?            attention queue (paid ambiguity FIRST)
 *   2. What is YMONEY doing now? status rail + active workflows / jobs
 *   3. Blocked? Changed?         readiness, dead jobs, failed items, reviews
 *   4. Publishing?               upcoming schedule + recent output
 *   5. Costing?                  spend vs budget + paid incidents
 *   6. Performing?               measured post metrics + experiments
 *   7. What did agents learn?    derived memory lessons + routing chains
 *
 * Layout: a dense ops frame — status rail on the left, attention queue first,
 * then a tabbed drill-down whose modules disclose progressively (collapsible
 * sections, attention-relevant ones open first).
 *
 * Three honesty rules, carried over from the previous revision:
 *
 * 1. NO INVENTED KPIs. Every number is counted from a field an endpoint
 *    actually returns. A dead panel reads UNAVAILABLE — never 0. Null money
 *    renders as "unknown", never $0.0000.
 *
 * 2. ONE FAILED PANEL MUST NOT BLANK THE PAGE. `useCombinedQueries` fans out
 *    every read; whatever arrived renders, and the failed panels are named.
 *
 * 3. SUBMISSION_UNKNOWN IS ITS OWN CATEGORY. Unknown tone, never warning or
 *    failed, no retry affordance anywhere: a blind resubmit is how one
 *    ambiguous charge becomes two.
 *
 * Endpoint shapes are typed from the backend routers (the OpenAPI document has
 * empty response schemas, so the routers are the source of truth). The two
 * panels beyond the original twelve (`/experiments`, `/knowledge/memories`)
 * reuse the exact shapes their own screens already type.
 */

import { useMemo, useState, type ReactNode } from "react";
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
  Tabs,
  cx,
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

/* HONESTY: `spent_last_24h_usd` is nullable. Null covers an empty window and a
 * window holding an UNKNOWN_EXPOSURE row — money possibly spent that nobody
 * can price is not $0.00. `within_budget` and `remaining_usd` are BUDGET GATES
 * and stay non-null: an unresolved exposure consumes headroom. */
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
  /* Every metric field is nullable. Null means no PostMetric snapshot exists —
   * a post with no snapshot is "no metric", never "0 views". */
  metrics: {
    views: number | null;
    likes: number | null;
    comments: number | null;
    completion_rate: number | null;
  };
};
type PostList = { items: PublishedPost[] };

/* `api/v1/experiments.py::_dto`, verbatim subset this console needs. The full
 * shape lives in features/experiments/Experiments.tsx. */
type ExperimentRow = {
  id: string;
  kind: string;
  hypothesis: string;
  platform: string;
  primary_metric: string;
  minimum_sample: number;
  status: string;
  confidence: string;
  result: {
    total_samples?: number;
    winner?: string | null;
    minimum_sample?: number;
    analyzed_at?: string;
  };
  created_at: string;
};
type ExperimentList = { total: number; items: ExperimentRow[] };

/* `engine/knowledge/memory.py::_to_dict`, verbatim subset. Full shape lives in
 * features/memory/Memory.tsx. */
type MemoryRow = {
  id: string;
  type: string;
  topic: string;
  content: string;
  confidence: number;
  freshness: string;
  status: string;
  effective_status: string;
  source_ids: string[];
  evidence_ids: unknown[];
  created_at: string | null;
};
type MemoryList = { items: MemoryRow[] };

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
  experiments: ExperimentList;
  memories: MemoryList;
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
  experiments: "experiment results",
  memories: "agent learnings",
};

/** Job states that mean work is in flight. `JobStatus` in models/base.py. */
const ACTIVE_JOB_STATUSES = ["QUEUED", "RUNNING", "WAITING", "RETRYING"];
/** Review states that still need a human (engine/collab/reviews.py). */
const OPEN_REVIEW_STATES = ["DRAFT", "IN_REVIEW", "CHANGES_REQUESTED"];
/** Campaign states that are not finished. */
const CLOSED_CAMPAIGN_STATES = ["COMPLETED", "ARCHIVED", "CANCELLED"];
/** Content states that mean the pipeline gave up on this item. */
const FAILED_CONTENT_STATUSES = ["FAILED"];
/** Memory types an engine DERIVED from measured outcomes — interpretations,
 * never evidence. The vocabulary is `engine/knowledge/memory.py::TYPES`; the
 * split is documented in features/memory/Memory.tsx. */
const DERIVED_MEMORY_TYPES = [
  "CONTENT_RESULT",
  "CREATIVE_LESSON",
  "AUDIENCE_INSIGHT",
  "COMMUNITY_INSIGHT",
  "PLATFORM_LEARNING",
  "EXPERIMENT_RESULT",
];

/* ==========================================================================
 * Adapters
 * ======================================================================= */

/**
 * Turn one `useCombinedQueries` entry into the `QueryState` that
 * `QueryBoundary` expects. `setData` is intentionally inert: the combined
 * cache is owned by `useCombinedQueries`.
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
 * Ops chrome — rail, collapsible sections, scoped styles
 * ======================================================================= */

/**
 * One collapsible operations module. The heading is a real button
 * (keyboard-operable disclosure), the body animates open over the
 * 140–220ms motion tokens, and closes instantly by unmounting so no
 * focusable content hides inside a collapsed region.
 */
function OpsSection({
  id,
  title,
  meta,
  count,
  defaultOpen = true,
  actions,
  children,
}: {
  id: string;
  title: string;
  meta?: string;
  count?: number | null;
  defaultOpen?: boolean;
  actions?: ReactNode;
  children: ReactNode;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const bodyId = `ops-sec-${id}`;
  const titleId = `ops-sec-${id}-title`;
  return (
    <section className="ops-sec" aria-labelledby={titleId}>
      <div className="ops-sec-head">
        <button
          type="button"
          className="ops-sec-toggle"
          aria-expanded={open}
          aria-controls={bodyId}
          onClick={() => setOpen((o) => !o)}
        >
          <span className={cx("ops-sec-chev", open && "ops-sec-chev--open")} aria-hidden="true">
            ▸
          </span>
          <h3 className="ops-sec-title" id={titleId}>
            {title}
          </h3>
          {count !== undefined && count !== null && (
            <span className="ops-sec-count" aria-label={`${count} rows`}>
              {count}
            </span>
          )}
          {meta && <span className="ops-sec-meta">{meta}</span>}
        </button>
        {actions && <div className="ops-sec-actions">{actions}</div>}
      </div>
      {open && (
        <div className="ops-sec-body" id={bodyId} role="region" aria-label={title}>
          <div className="ops-sec-inner">{children}</div>
        </div>
      )}
    </section>
  );
}

/** One status-rail entry: a measured figure plus a text verdict, never color-only. */
function RailBlock({
  kicker,
  display,
  verdict,
  tone,
  hint,
  onGo,
  unavailable,
}: {
  kicker: string;
  display: ReactNode;
  verdict: string;
  tone: Tone;
  hint?: string;
  onGo: () => void;
  unavailable?: boolean;
}) {
  return (
    <button
      type="button"
      className={cx("ops-rail-block", `ym-tone-${tone}`)}
      onClick={onGo}
      title={hint ?? verdict}
    >
      <span className="ops-rail-kicker">{kicker}</span>
      <span className={cx("ops-rail-value", unavailable && "ops-rail-value--unavailable")}>
        {unavailable ? "UNAVAILABLE" : display}
      </span>
      <span className="ops-rail-verdict">
        <span className="ops-rail-dot" aria-hidden="true" />
        {verdict}
      </span>
    </button>
  );
}


/* ==========================================================================
 * Screen
 * ======================================================================= */

type TabId = "work" | "publish" | "money" | "intel" | "sys";

const TAB_LABEL: Record<TabId, string> = {
  work: "Workflows",
  publish: "Publish",
  money: "Costs & risk",
  intel: "Intelligence",
  sys: "System",
};

export function CommandCenter() {
  const { workspaceId, workspace } = useSession();
  const navigate = useNavigate();
  const [tab, setTab] = useState<TabId>("work");

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
    experiments: () => wsApi.get("/experiments") as Promise<ExperimentList>,
    memories: () => wsApi.get("/knowledge/memories?limit=200") as Promise<MemoryList>,
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
  /* `views !== null` is the fact: null means no PostMetric snapshot exists. A
   * post whose snapshot genuinely says 0 views counts as measured. */
  const measuredPosts = livePosts?.filter((p) => p.metrics.views !== null) ?? null;

  /* Performance signals are DERIVED from measured snapshots only. Every
   * aggregate below skips null legs, so an unmeasured post contributes
   * nothing — not even a zero. */
  const perf = useMemo(() => {
    if (measuredPosts === null) return null;
    const views = measuredPosts.map((p) => p.metrics.views).filter((v): v is number => v !== null);
    const likes = measuredPosts.map((p) => p.metrics.likes).filter((v): v is number => v !== null);
    const comments = measuredPosts.map((p) => p.metrics.comments).filter((v): v is number => v !== null);
    const completions = measuredPosts
      .map((p) => p.metrics.completion_rate)
      .filter((v): v is number => v !== null);
    return {
      measured: measuredPosts.length,
      total: livePosts?.length ?? null,
      views: views.length > 0 ? views.reduce((a, b) => a + b, 0) : null,
      likes: likes.length > 0 ? likes.reduce((a, b) => a + b, 0) : null,
      comments: comments.length > 0 ? comments.reduce((a, b) => a + b, 0) : null,
      avgCompletion: completions.length > 0 ? completions.reduce((a, b) => a + b, 0) / completions.length : null,
    };
  }, [measuredPosts, livePosts]);

  /* Campaign pulse: per-campaign workload derived from the content rows'
   * campaign_id. When the content panel failed there is no pulse — the
   * section says UNAVAILABLE instead of inventing zeros. */
  const pulse = useMemo(() => {
    if (activeCampaigns === null) return null;
    if (content === null) return "unavailable" as const;
    return activeCampaigns.map((c) => {
      const items = content.filter((i) => i.campaign_id === c.id);
      return {
        campaign: c,
        items: items.length,
        failed: items.filter((i) => FAILED_CONTENT_STATUSES.includes(i.status.toUpperCase())).length,
        rendering: items.filter((i) => {
          const vs = i.video?.status?.toUpperCase();
          return vs === "RENDERING" || vs === "PENDING" || vs === "QUEUED" || vs === "PROCESSING";
        }).length,
      };
    });
  }, [activeCampaigns, content]);

  const chains = data.chains?.chains ?? null;

  const experiments = data.experiments?.items ?? null;
  const runningExperiments = experiments?.filter((e) => e.status.toUpperCase() === "RUNNING") ?? null;

  /* What agents learned: engine-derived interpretations only, statuses
   * verbatim, newest first by created_at when reported. */
  const learnings = useMemo(() => {
    const items = data.memories?.items ?? null;
    if (items === null) return null;
    return items
      .filter((m) => DERIVED_MEMORY_TYPES.includes(m.type.toUpperCase()))
      .slice()
      .sort((a, b) => String(b.created_at ?? "").localeCompare(String(a.created_at ?? "")));
  }, [data.memories]);

  /* ---- attention queue: paid ambiguity FIRST, then blocked, failed, stale */
  type Alert = {
    key: string;
    rank: number;
    severity: string;
    tone: Tone;
    what: string;
    detail: string;
    action: string;
  };
  const alerts: Alert[] = [];
  for (const inc of unknownSubmissions ?? []) {
    alerts.push({
      key: `inc-unknown-${inc.incident_id}`,
      rank: 0,
      severity: "Paid ambiguity",
      tone: toneForStatus(inc.state),
      what: `Reconcile before acting: ${inc.provider} · ${inc.operation}`,
      detail: inc.detail || inc.note || "Ambiguous paid state reported by the paid executor.",
      action: "Next: reconcile with the provider. No retry is offered here — resubmitting may double-bill.",
    });
  }
  for (const inc of (reconcileIncidents ?? []).filter(
    (i) => !(unknownSubmissions ?? []).some((u) => u.incident_id === i.incident_id),
  )) {
    alerts.push({
      key: `inc-${inc.incident_id}`,
      rank: 0,
      severity: "Paid ambiguity",
      tone: toneForStatus(inc.state),
      what: `Reconcile before acting: ${inc.provider} · ${inc.operation}`,
      detail: inc.detail || inc.note || "Reconciliation state reported by the paid executor.",
      action: "Next: reconcile with the provider. No retry is offered here.",
    });
  }
  for (const id of readiness?.blocking_failures ?? []) {
    const check = readiness?.checks.find((c) => c.id === id);
    alerts.push({
      key: `readiness-${id}`,
      rank: 1,
      severity: "Blocked",
      tone: "danger",
      what: `Blocking dependency down: ${humanize(id)}`,
      detail: check?.remediation || check?.detail || "No remediation reported.",
      action: "Next: see the System tab for the probe detail and remediation.",
    });
  }
  for (const job of deadJobs ?? []) {
    alerts.push({
      key: `job-${job.id}`,
      rank: 2,
      severity: "Failed",
      tone: "danger",
      what: `Job dead: ${humanize(job.type)}`,
      detail: job.last_error || "No error recorded on the job row.",
      action: "Next: inspect in Operations. Do not blind-resubmit paid legs.",
    });
  }
  for (const item of failedContent ?? []) {
    alerts.push({
      key: `content-${item.id}`,
      rank: 2,
      severity: "Failed",
      tone: "danger",
      what: `Pipeline failed: ${item.topic}`,
      detail: item.error || "No error recorded on the content item.",
      action: "Next: open the project to see the failed stage.",
    });
  }
  for (const entry of failedSchedule ?? []) {
    alerts.push({
      key: `sched-${entry.id}`,
      rank: 2,
      severity: "Failed",
      tone: "danger",
      what: `Scheduled publication failed: ${humanize(entry.platform)}`,
      detail: "Calendar entry is FAILED.",
      action: "Next: re-dispatch from Calendar, not from here.",
    });
  }
  for (const rev of (openReviews ?? []).filter((r) => r.stale)) {
    alerts.push({
      key: `review-${rev.id}`,
      rank: 3,
      severity: "Stale",
      tone: "warning",
      what: `Approval invalidated by a new version: ${rev.title || rev.id.slice(0, 8)}`,
      detail: "The bound version is stale; the target changed after this review opened.",
      action: "Next: re-open the review against the current version.",
    });
  }
  alerts.sort((a, b) => a.rank - b.rank);

  const openProject = (contentId: string) => navigate(`/projects/${contentId}`);
  const go = (t: TabId) => setTab(t);

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
      /* `views === null` is the backend's own statement of "no metric". A
       * genuine zero-view snapshot prints as 0 — measured zero, not missing. */
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

  const experimentColumns: Column<ExperimentRow>[] = [
    { key: "hypothesis", header: "Experiment", cell: (e) => e.hypothesis || `${humanize(e.kind)} · ${e.id.slice(0, 8)}` },
    { key: "status", header: "Status", cell: (e) => <StatusBadge status={e.status} /> },
    { key: "metric", header: "Primary metric", cell: (e) => humanize(e.primary_metric), hideBelow: "md" },
    {
      key: "samples",
      header: "Samples",
      align: "right",
      cell: (e) =>
        e.result.total_samples === undefined ? (
          <span className="ym-muted" title={`Below-gate: minimum_sample is ${e.minimum_sample}`}>
            —
          </span>
        ) : (
          e.result.total_samples
        ),
    },
    {
      key: "winner",
      header: "Winner",
      /* Only a COMPLETED experiment with a backend-named winner names one.
       * Anything else is "—": this console never invents a lesson. */
      cell: (e) =>
        e.status.toUpperCase() === "COMPLETED" && e.result.winner ? (
          e.result.winner
        ) : (
          <span className="ym-muted">—</span>
        ),
    },
    {
      key: "confidence",
      header: "Confidence",
      cell: (e) => (e.confidence ? e.confidence : <span className="ym-muted">—</span>),
      hideBelow: "lg",
    },
  ];

  const memoryColumns: Column<MemoryRow>[] = [
    {
      key: "lesson",
      header: "Lesson",
      cell: (m) => (
        <span title={m.content}>
          {m.topic || "Untitled"} — {(m.content ?? "").slice(0, 140)}
          {(m.content ?? "").length > 140 ? "…" : ""}
        </span>
      ),
    },
    {
      key: "type",
      header: "Kind",
      cell: (m) => <Badge tone="info" title="Engine-derived interpretation, not evidence">{humanize(m.type)}</Badge>,
      hideBelow: "md",
    },
    {
      key: "status",
      header: "Status",
      cell: (m) => <StatusBadge status={m.effective_status || m.status} />,
    },
    {
      key: "evidence",
      header: "Evidence",
      align: "right",
      cell: (m) => (m.source_ids ?? []).length + (m.evidence_ids ?? []).length,
    },
    {
      key: "confidence",
      header: "Confidence",
      align: "right",
      cell: (m) => pct(m.confidence),
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
  const experimentsState = panelState(data.experiments, errors.experiments, settled, reload);
  const memoriesState = panelState(data.memories, errors.memories, settled, reload);

  const activeWorkCount = countOrNull(activeJobs);
  const renderCount = countOrNull(rendering);
  const workCount =
    activeWorkCount === null || renderCount === null ? null : activeWorkCount + renderCount;

  const tabs = [
    { id: "work", label: "Workflows", count: workCount ?? undefined },
    { id: "publish", label: "Publish", count: countOrNull(upcoming) ?? undefined },
    { id: "money", label: "Costs & risk", count: countOrNull(unknownSubmissions) ?? undefined },
    {
      id: "intel",
      label: "Intelligence",
      count: countOrNull(runningExperiments) ?? undefined,
    },
    {
      id: "sys",
      label: "System",
      count: readiness ? readiness.blocking_failures.length : undefined,
    },
  ];

  const attnSummary =
    settled && failed.length === 0
      ? `${alerts.length} item${alerts.length === 1 ? "" : "s"} need${alerts.length === 1 ? "s" : ""} attention.`
      : settled
        ? `${alerts.length} item${alerts.length === 1 ? "" : "s"} need attention. ${failed.length} panel${failed.length === 1 ? "" : "s"} unavailable.`
        : "Loading workspace state.";

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

      <div className="ops-console">
        {/* ---- status rail ------------------------------------------------ */}
        <nav className="ops-rail" aria-label="Operations status">
          <span className="ops-rail-label" aria-hidden="true">
            Status
          </span>
          <RailBlock
            kicker="System"
            display={readiness ? humanize(readiness.status) : null}
            unavailable={readiness === null}
            verdict={
              readiness === null
                ? "Readiness unknown"
                : readiness.blocking_failures.length > 0
                  ? `${readiness.blocking_failures.length} blocking — down`
                  : "All dependencies verified"
            }
            tone={
              readiness === null
                ? "neutral"
                : readiness.blocking_failures.length > 0
                  ? "danger"
                  : "success"
            }
            hint="GET /system/readiness. Activates the System tab."
            onGo={() => go("sys")}
          />
          <RailBlock
            kicker="Spend 24h"
            display={costs ? <Money usd={costs.spent_last_24h_usd} /> : null}
            unavailable={costs === null || costs.spent_last_24h_usd === null}
            verdict={
              costs === null || costs.spent_last_24h_usd === null
                ? "Spend unknown — headroom bounded"
                : costs.within_budget
                  ? `Within budget · ${costs.remaining_usd.toFixed(4)} left`
                  : `Over budget · ${costs.remaining_usd.toFixed(4)} left`
            }
            tone={
              costs === null || costs.spent_last_24h_usd === null
                ? "neutral"
                : costs.within_budget
                  ? "success"
                  : "danger"
            }
            hint="GET /costs. Activates the Costs & risk tab."
            onGo={() => go("money")}
          />
          <RailBlock
            kicker="Active work"
            display={workCount}
            unavailable={workCount === null}
            verdict={
              workCount === null ? "Queue unknown" : workCount === 0 ? "Queue idle" : `${workCount} in flight`
            }
            tone={workCount === null ? "neutral" : workCount === 0 ? "success" : "info"}
            hint="GET /jobs + GET /content render states. Activates the Workflows tab."
            onGo={() => go("work")}
          />
          <RailBlock
            kicker="Paid risk"
            display={countOrNull(unknownSubmissions)}
            unavailable={unknownSubmissions === null}
            verdict={
              unknownSubmissions === null
                ? "Exposure unknown"
                : unknownSubmissions.length > 0
                  ? `${unknownSubmissions.length} ambiguous — reconcile`
                  : "No ambiguous submission"
            }
            tone={
              unknownSubmissions === null
                ? "neutral"
                : unknownSubmissions.length > 0
                  ? "unknown"
                  : "success"
            }
            hint="GET /provider-maturity/incidents. Activates the Costs & risk tab."
            onGo={() => go("money")}
          />
          <RailBlock
            kicker="Publishing"
            display={
              upcoming === null || livePosts === null ? null : `${upcoming.length} queued · ${livePosts.length} live`
            }
            unavailable={upcoming === null || livePosts === null}
            verdict={
              upcoming === null || livePosts === null
                ? "Schedule unknown"
                : upcoming.length === 0
                  ? "Nothing queued"
                  : `Next: ${utc(upcoming.map((e) => e.run_at).sort()[0])}`
            }
            tone={upcoming === null || livePosts === null ? "neutral" : upcoming.length === 0 ? "neutral" : "info"}
            hint="GET /calendar + GET /publishing/posts. Activates the Publish tab."
            onGo={() => go("publish")}
          />
        </nav>

        {/* ---- main column -------------------------------------------------- */}
        <div className="ops-main">
          <p role="status" aria-live="polite" className="ym-sr-only">
            {attnSummary}
          </p>

          {/* Attention queue — always first, paid ambiguity ranked first. */}
          <section className="ops-attn" aria-labelledby="ops-attn-title">
            <div className="ops-attn-head">
              <h2 className="ops-attn-title" id="ops-attn-title">
                What needs attention
              </h2>
              <p className="ops-attn-sub">
                {settled && (
                  <>
                    {alerts.length} item{alerts.length === 1 ? "" : "s"} ranked: paid ambiguity
                    first, then blocked, failed, stale.{" "}
                  </>
                )}
                SUBMISSION_UNKNOWN is never shown as failed and never offers a retry.
              </p>
            </div>
            {!settled ? (
              <div className="ops-empty">
                <Skeleton rows={3} />
              </div>
            ) : alerts.length === 0 ? (
              <div className="ops-empty">
                <EmptyState
                  title="Nothing is blocked"
                  description="No blocking dependency, dead job, failed content item or unreconciled paid incident."
                />
              </div>
            ) : (
              <ol className="ops-attn-list">
                {alerts.map((a, i) => (
                  <li key={a.key} className={cx("ops-attn-item", `ym-tone-${a.tone}`)}>
                    <span className="ops-attn-rank" aria-hidden="true">
                      {String(i + 1).padStart(2, "0")}
                    </span>
                    <div>
                      <Badge tone={a.tone} dot>
                        {a.severity}
                      </Badge>
                      <div className="ops-attn-what">{a.what}</div>
                      <div className="ops-attn-detail">{a.detail}</div>
                      <div className="ops-attn-action">{a.action}</div>
                    </div>
                  </li>
                ))}
              </ol>
            )}
          </section>

          {/* Tabbed drill-down */}
          <div className="ops-tabwrap">
            <div className="ops-tabbar">
              <Tabs tabs={tabs} active={tab} onChange={(id) => setTab(id as TabId)} />
            </div>
            <div className="ops-tabpanel" role="tabpanel" aria-label={`${TAB_LABEL[tab]} detail`}>
              {tab === "work" && (
                <>
                  <OpsSection
                    id="campaigns"
                    title="Active campaigns"
                    meta="GET /campaigns"
                    count={countOrNull(activeCampaigns)}
                  >
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
                  </OpsSection>

                  <OpsSection
                    id="pulse"
                    title="Campaign pulse"
                    meta="DERIVED from GET /content campaign_id"
                    count={Array.isArray(pulse) ? pulse.length : null}
                  >
                    {pulse === null ? (
                      <Metric label="Campaign workload" value={null} source="GET /campaigns + GET /content" />
                    ) : pulse === "unavailable" ? (
                      <EmptyState
                        title="UNAVAILABLE"
                        description="The content library did not load, so per-campaign workload cannot be derived. No zeros are shown."
                      />
                    ) : (
                      <DataTable
                        rows={pulse}
                        columns={[
                          { key: "name", header: "Campaign", cell: (r) => r.campaign.name },
                          { key: "status", header: "Status", cell: (r) => <StatusBadge status={r.campaign.status} /> },
                          { key: "items", header: "Items", align: "right", cell: (r) => r.items },
                          {
                            key: "failed",
                            header: "Failed",
                            align: "right",
                            cell: (r) => (r.failed > 0 ? <Badge tone="danger">{r.failed}</Badge> : r.failed),
                          },
                          { key: "rendering", header: "Rendering", align: "right", cell: (r) => r.rendering },
                        ]}
                        rowKey={(r) => r.campaign.id}
                        caption="Per-campaign workload derived from content rows"
                        empty="No active campaigns"
                        onRowClick={(r) => navigate(`/campaigns/${r.campaign.id}`)}
                      />
                    )}
                  </OpsSection>

                  <OpsSection
                    id="jobs"
                    title="Active jobs & renders"
                    meta="Latest 100 jobs · GET /jobs"
                    count={workCount}
                  >
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
                      {() => (
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
                  </OpsSection>

                  <OpsSection
                    id="queue"
                    title="Queue health"
                    meta="Status histogram over the latest 100 jobs"
                    defaultOpen={false}
                  >
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
                  </OpsSection>

                  <OpsSection
                    id="reviews"
                    title="Pending reviews"
                    meta="DRAFT · IN_REVIEW · CHANGES_REQUESTED"
                    count={countOrNull(openReviews)}
                  >
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
                  </OpsSection>
                </>
              )}

              {tab === "publish" && (
                <>
                  <OpsSection
                    id="upcoming"
                    title="Upcoming publications"
                    meta="PENDING entries · GET /calendar"
                    count={countOrNull(upcoming)}
                  >
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
                  </OpsSection>

                  <OpsSection
                    id="output"
                    title="Recent output"
                    meta="MOCK and LIVE never share a badge"
                    count={posts ? posts.length : null}
                  >
                    <QueryBoundary query={postsState} skeletonRows={4}>
                      {() => (
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
                            rows={data.posts?.items ?? []}
                            columns={postColumns}
                            rowKey={(p) => p.id}
                            caption="Recent publications"
                            empty="Nothing published yet"
                            emptyHint="A publication appears here once a publishing job wrote a post row."
                            maxHeight={320}
                          />
                        </>
                      )}
                    </QueryBoundary>
                  </OpsSection>
                </>
              )}

              {tab === "money" && (
                <>
                  <OpsSection
                    id="spend"
                    title="Spend — budget status"
                    meta="Last 24h against the daily budget · GET /costs"
                  >
                    <QueryBoundary query={costsState} skeletonRows={3}>
                      {(d) => (
                        <>
                          <Grid min={150} gap="sm">
                            <StatTile
                              label="Spent 24h"
                              /* Null ⇒ UNAVAILABLE. `remaining_usd` and
                               * `within_budget` stay real: they are the budget
                               * GATE, and an unpriceable exposure must consume
                               * headroom, not create it. */
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
                  </OpsSection>

                  <OpsSection
                    id="incidents"
                    title="Paid incidents"
                    meta="States travel verbatim · no resubmit unless retry_safe"
                    count={incidents ? incidents.length : null}
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
                  </OpsSection>
                </>
              )}

              {tab === "intel" && (
                <>
                  <OpsSection
                    id="perf"
                    title="Performance signals"
                    meta="DERIVED from measured snapshots only — unmeasured posts contribute nothing"
                    count={perf ? perf.measured : null}
                  >
                    <QueryBoundary query={postsState} skeletonRows={3}>
                      {() =>
                        perf === null || perf.total === null ? (
                          <Metric label="Measured publications" value={null} source="GET /publishing/posts" />
                        ) : perf.measured === 0 ? (
                          <EmptyState
                            title="No measured publication"
                            description="No live publication has a metric snapshot yet. Coverage is unknown, not zero."
                          />
                        ) : (
                          <>
                            <Grid min={150} gap="sm">
                              <Metric
                                label="Measured views"
                                value={perf.views}
                                hint={`Summed over ${perf.measured} measured publication${perf.measured === 1 ? "" : "s"} of ${perf.total} live`}
                                source="DERIVED · GET /publishing/posts"
                              />
                              <Metric
                                label="Measured likes"
                                value={perf.likes}
                                source="DERIVED · GET /publishing/posts"
                              />
                              <Metric
                                label="Measured comments"
                                value={perf.comments}
                                source="DERIVED · GET /publishing/posts"
                              />
                              <StatTile
                                label="Mean completion"
                                unavailable={perf.avgCompletion === null}
                                value={perf.avgCompletion === null ? undefined : pct(perf.avgCompletion)}
                                hint="Mean over snapshots that report a completion rate"
                                source="DERIVED · GET /publishing/posts"
                              />
                            </Grid>
                            <DataTable
                              rows={(measuredPosts ?? [])
                                .slice()
                                .sort((a, b) => (b.metrics.views ?? 0) - (a.metrics.views ?? 0))}
                              columns={postColumns}
                              rowKey={(p) => p.id}
                              caption="Measured publications, best views first"
                              empty="No measured publication"
                              maxHeight={280}
                            />
                          </>
                        )
                      }
                    </QueryBoundary>
                  </OpsSection>

                  <OpsSection
                    id="experiments"
                    title="Experiment results"
                    meta="No lesson below the sample gate · GET /experiments"
                    count={experiments ? experiments.length : null}
                  >
                    <QueryBoundary query={experimentsState} skeletonRows={3}>
                      {(d) => (
                        <DataTable
                          rows={d.items}
                          columns={experimentColumns}
                          rowKey={(e) => e.id}
                          caption="Experiment results"
                          empty="No experiment recorded"
                          emptyHint="An experiment appears here once POST /experiments wrote a draft row."
                          onRowClick={() => navigate("/experiments")}
                        />
                      )}
                    </QueryBoundary>
                  </OpsSection>

                  <OpsSection
                    id="learnings"
                    title="What agents learned"
                    meta="Engine-derived interpretations, statuses verbatim"
                    count={learnings ? learnings.length : null}
                  >
                    <QueryBoundary query={memoriesState} skeletonRows={3}>
                      {(d) => {
                        const rows = (learnings ?? []).slice(0, 8);
                        return rows.length === 0 ? (
                          <EmptyState
                            title="No derived lesson stored"
                            description="No CREATIVE_LESSON, EXPERIMENT_RESULT, PLATFORM_LEARNING, AUDIENCE_INSIGHT, COMMUNITY_INSIGHT or CONTENT_RESULT memory is stored for this workspace."
                          />
                        ) : (
                          <>
                            <DataTable
                              rows={rows}
                              columns={memoryColumns}
                              rowKey={(m) => m.id}
                              caption="Agent learnings"
                              empty="No derived lesson stored"
                              onRowClick={() => navigate("/memory")}
                              maxHeight={320}
                            />
                            {(learnings ?? []).length > 8 && (
                              <p className="ym-hint">
                                Showing 8 of {(learnings ?? []).length} derived lessons. The rest live under Memory.
                              </p>
                            )}
                            {d.items.length > (learnings ?? []).length && (
                              <p className="ym-hint">
                                {d.items.length - (learnings ?? []).length} further memories are evidence or
                                unclassified rows, not agent lessons — they are listed under Memory, not here.
                              </p>
                            )}
                          </>
                        );
                      }}
                    </QueryBoundary>
                  </OpsSection>

                  <OpsSection
                    id="chains"
                    title="Agent decisions — provider chains"
                    meta={data.chains?.note}
                    count={chains ? chains.length : null}
                    defaultOpen={false}
                  >
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
                  </OpsSection>

                  <OpsSection
                    id="nextwork"
                    title="Suggested next work"
                    meta="Planner basis + inbox signals, never virality claims"
                    defaultOpen={false}
                  >
                    <QueryBoundary query={plannerState} skeletonRows={3}>
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
                    <QueryBoundary query={inboxState} skeletonRows={3}>
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
                  </OpsSection>
                </>
              )}

              {tab === "sys" && (
                <OpsSection
                  id="readiness"
                  title="System readiness"
                  meta={readiness?.message ?? "GET /system/readiness"}
                  count={failedChecks ? failedChecks.length : null}
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
                </OpsSection>
              )}
            </div>
          </div>

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
        </div>
      </div>
    </>
  );
}

export default CommandCenter;

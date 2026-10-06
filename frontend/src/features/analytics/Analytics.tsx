/* Analytics — what was MEASURED, and where nothing was.
 *
 *   GET /analytics/overview                       channel snapshot (misc.py analytics_router)
 *   GET /analytics/breakdowns                    topic / hook-style / duration groups
 *   GET /analytics/patterns                      learned patterns (LearningPattern rows)
 *   GET /publishing/posts                        per-post latest metric snapshot
 *   GET /campaigns                               campaign selector
 *   GET /performance/overview?campaign_id=       campaign rollup + per-platform
 *   GET /performance/compare?campaign_id=&group_by=  creative-dimension groups
 *   GET /performance/retention?post_id=|short_id=    retention curve, or honest UNAVAILABLE
 *   GET /distribution/capabilities               which platforms report analytics at all
 *
 * ---------------------------------------------------------------------------
 * THE RULE THIS SCREEN EXISTS TO ENFORCE
 * ---------------------------------------------------------------------------
 * UNAVAILABLE IS NOT ZERO.
 *
 * WORK 16.5.7 §8 MADE THE BACKEND HONEST, AND THIS FILE NOW BELIEVES IT.
 * Four sites used to fabricate a zero here, all of which are fixed at the layer
 * that owns the fact rather than patched in the browser:
 *
 *  1. `analytics/overview.totals` was seeded at 0 and only ever added rows for
 *     posts that HAVE a `PostMetric` snapshot, so a workspace with 40 published
 *     posts and no reporting returned `views: 0` — "nobody watched" instead of
 *     "nothing was measured". It now returns `null`.
 *
 *  2. `/publishing/posts` hard-coded `views: 0` for a post with no snapshot. It
 *     now returns `null` for the whole `metrics` object.
 *
 *  3. `engine/campaign/analytics.py::empty_rollup` seeded every key at 0/0.0,
 *     including the two DERIVED rates that are only computed when `views > 0`.
 *     All eight keys are now `null`.
 *
 *  4. `analytics/breakdowns.finish()` wrote `avg_views: 0` / `engagement_pct: 0`
 *     when no post in the bucket was measured. Both are `null` now.
 *
 * STILL FABRICATED, ON OTHER SCREENS — do not assume this file's fix generalises:
 * `ops.py::_costs_section` (read by Operations.tsx) still reports `$0.0000` for
 * an empty 24h window, `safety.py::cost_intelligence` still reports `$0.0000`
 * for an empty ledger, and `PostMetric.completion_rate` is still NOT NULL so a
 * provider that cannot report one writes `0.0`. Those three files were outside
 * that change's permitted scope; each gap is listed under `known_out_of_scope`
 * in `docs/ANALYTICS_HONESTY_AUDIT.json`, and `--strict` fails on them.
 *
 * CONSEQUENCE FOR THIS FILE: the client-side guessing these nulls replaced
 * (`anyMeasured`, and `posts === 0` as a proxy for "unmeasured") is DELETED. A
 * null is now the backend's own statement of "cannot be known", and the UI reads
 * it directly. Two reasons, in order:
 *
 *  - Duplicated truth drifts. Two implementations of one rule is how the
 *    original fabrication survived a UI pass that had already been written to
 *    compensate for it. The compensation would have outlived the backend fix and
 *    kept re-deriving availability from a heuristic the backend no longer uses.
 *  - The heuristic was weaker than the fact. `anyMeasured` inferred "nothing
 *    measured" from a COUNT of measured posts; it could not distinguish an empty
 *    ledger from a ledger with an unpriceable row, which is exactly the case
 *    where a confident number is most damaging.
 *
 * What stays is honest client-side reasoning about data the backend genuinely
 * does not compute: `distribution/capabilities.supports_analytics` is the
 * registry's own statement of whether a platform reports analytics at all, and a
 * platform that cannot report has no figure whatever the aggregate says.
 *
 * Nothing on this screen computes a statistic the backend did not compute.
 * `compare` declares `causal: false` and that label is rendered, not dropped.
 */

import { useMemo, useState, type ReactNode } from "react";
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
  Tabs,
  humanize,
  type Column,
} from "../../design-system/primitives";
import { useQuery, useWsQuery, type QueryState } from "../../api/queries";
import { wsApi } from "../../lib/api";
import { useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes
 *
 * None of these routers declare a `response_model`, so every type below is
 * transcribed from the router's own `return` statement. No field is invented
 * and none is renamed.
 * ======================================================================= */

/* `misc.py::analytics_overview` — GET /analytics/overview
 *
 * HONESTY (Work 16.5.7 §8). The backend no longer seeds these totals at 0: a
 * total nothing was summed into is `null`, and `null` renders UNAVAILABLE. A
 * measured zero is still `0`. `cost_total_usd` is `null` for an empty ledger
 * AND for a ledger holding an UNKNOWN_EXPOSURE row — money possibly spent that
 * nobody can price is not $0.00. The COUNT fields stay plain numbers because a
 * COUNT over an empty table is a real measurement. */
export type OverviewTotals = {
  views: number | null;
  likes: number | null;
  comments: number | null;
  shares: number | null;
  followers_gained: number | null;
};

export type OverviewPlatform = { posts: number; views: number };

export type AnalyticsOverview = {
  totals: OverviewTotals;
  posts_published: number;
  content_items: number;
  cost_total_usd: number | null;
  /** How many ledger rows record an unpriceable exposure. >0 ⇒ total is null. */
  cost_total_unknown_exposure_rows: number;
  per_platform: Record<string, OverviewPlatform>;
  /** null when no post in the workspace has a metric snapshot. */
  best_post: { platform: string; title: string; views: number } | null;
  mock_analytics: boolean;
};

/* `misc.py::analytics_breakdowns` — GET /analytics/breakdowns
 *
 * A bucket is registered before the backend knows whether any of its posts were
 * measured, so `total_views` / `avg_views` / `engagement_pct` are `null` for a
 * bucket with no measured post. `engagement_samples` is the denominator the
 * backend actually used, which is NOT `posts`: a published post with 0 views has
 * no engagement rate and is not a sample. */
export type BreakdownRow = {
  key: string;
  posts: number;
  mock_posts: number;
  total_views: number | null;
  avg_views: number | null;
  engagement_pct: number | null;
  engagement_samples: number;
};

export type AnalyticsBreakdowns = {
  by_topic: BreakdownRow[];
  by_hook_style: BreakdownRow[];
  by_duration: BreakdownRow[];
  posts_with_metrics: number;
  mock_analytics: boolean;
};

/* `misc.py::learning_patterns` — GET /analytics/patterns */
export type PatternRow = {
  pattern_key: string;
  description: string;
  /** A measured delta vs the channel median. NOT a probability. */
  improvement_pct: number | null;
  confidence: string;
  sample_size: number;
  active: boolean;
  updated_at: string;
};

/* `misc.py::list_published_posts` — GET /publishing/posts
 *
 * HONESTY (Work 16.5.7 §8): all four fields are nullable, and null is now the
 * ONLY signal that matters. The backend used to report `views: 0` for a post with
 * no snapshot and leave `completion_rate: null` as the sole tell, which forced
 * every consumer to infer availability from an unrelated field. It now returns
 * null for the whole `metrics` object when no PostMetric row exists, and the
 * measured values -- including genuine zeros -- when one does. */
export type PostMetrics = {
  views: number | null;
  likes: number | null;
  comments: number | null;
  /** null ⇔ no provider reported a completion rate for this post. */
  completion_rate: number | null;
};

export type PublishedPostRow = {
  id: string;
  platform: string;
  title: string;
  remote_url: string;
  published_at: string | null;
  is_mock: boolean;
  metrics: PostMetrics;
};

export type PublishedPostList = { items: PublishedPostRow[] };

/* `campaigns_router.list_campaigns` — only id/name/status are used here. */
export type CampaignOption = { id: string; name: string; status: string };
export type CampaignOptions = { items: CampaignOption[] };

/* `engine/campaign/analytics.py::empty_rollup` + `_accumulate`
 *
 * HONESTY (Work 16.5.7 §8): every key is nullable. `null` is UNAVAILABLE — no
 * snapshot was summed, or the rate could not be computed. A measured zero is
 * still `0`, so "nobody watched the video" and "nobody reported on the video"
 * stay distinguishable. */
export type RollupTotals = {
  views: number | null;
  watch_time: number | null;
  likes: number | null;
  comments: number | null;
  shares: number | null;
  saves: number | null;
  /** DERIVED; null unless it was computed (needs a non-zero view count). */
  engagement_rate: number | null;
  /** DERIVED; null unless at least one row REPORTED a completion_rate. */
  completion: number | null;
};

export type ShortRollup = RollupTotals & {
  short_id: string;
  posts: number;
  variants?: Record<string, RollupTotals & { variant_id: string; posts: number }>;
};

export type CampaignRollup = {
  campaign_id: string;
  master_content_id: string | null;
  master: ShortRollup | null;
  shorts: ShortRollup[];
  short_count: number;
  totals: RollupTotals;
  post_count: number;
  platforms: string[];
};

/* `performance.py::performance_overview` — GET /performance/overview */
export type PerformanceOverview = {
  campaign_id: string;
  rollup: CampaignRollup;
  /** compare_platforms(): platform → rollup dict + posts. */
  platforms: Record<string, RollupTotals & { posts: number }>;
};

/* `performance.py::_compare` — GET /performance/compare
 *
 * `avg_completion` is null when no post in the group REPORTED a completion
 * rate; `reported_completion_samples` is how many did, which is not `n`. */
export type CompareGroup = {
  group: string;
  n: number;
  views: number;
  avg_completion: number | null;
  reported_completion_samples: number;
  low_sample: boolean;
};

export type CompareResponse = {
  campaign_id: string;
  group_by: string;
  groups: CompareGroup[];
  /** The backend says so itself. Do not upgrade this to a causal claim. */
  causal: boolean;
  note: string;
};

/* `performance.py::performance_retention` → `RetentionAnalyzer.analyze` */
export type RetentionMapping = {
  mapped?: boolean;
  scene_index?: number | null;
  scene_title?: string | null;
  chapter_title?: string | null;
  is_hook?: boolean;
  caption_state?: unknown;
  reason?: string | null;
};

export type RetentionDrop = {
  type: string;
  from?: string;
  to?: string;
  from_t?: number;
  to_t?: number;
  loss: number;
};

export type RetentionResponse = {
  status: "AVAILABLE" | "UNAVAILABLE";
  reason?: string;
  curve?: Record<string, number>;
  missing?: string[];
  mapping?: Record<string, RetentionMapping>;
  drops?: RetentionDrop[];
  rewatches?: Array<{ t: number; value: number }>;
  coarse_proxy?: Record<string, unknown> | null;
  source?: string;
  post_id: string | null;
  short_content_id: string | null;
};

/* `distribution_router.capabilities` — GET /distribution/capabilities */
export type PlatformCapability = {
  platform: string;
  capabilities: string[];
  publish_mode: string;
  supports_analytics: boolean;
  campaign_platforms: string[];
};

/* ==========================================================================
 * Honesty helpers
 * ======================================================================= */

/**
 * A value the backend did not measure.
 *
 * Rendered as muted text, never as 0 and never as "0%". `tone` lets a caller
 * colour it as `unknown` where it sits next to real figures.
 */
export function Unavailable({
  title = "Not measured — no provider reports this figure",
  children,
}: {
  title?: string;
  children?: ReactNode;
}) {
  return (
    <span className="ym-muted" title={title}>
      {children ?? "UNAVAILABLE"}
    </span>
  );
}

/**
 * Posts that actually have a metric snapshot, per the backend's own counting.
 *
 * `per_platform[*].posts` is incremented only after a `PostMetric` row is found,
 * so this is a measured count — not a guess about which posts were published.
 */
export function measuredPosts(overview: AnalyticsOverview | null): number | null {
  if (!overview) return null;
  return Object.values(overview.per_platform ?? {}).reduce((sum, p) => sum + (p?.posts ?? 0), 0);
}

/**
 * Whether a DERIVED rate is available.
 *
 * WORK 16.5.7 §8: this used to be `views > 0`, inferring availability from a
 * total. `empty_rollup` now returns `null` for a rate it could not compute, so
 * the value itself is the signal — and each rate is checked on its own, because
 * `engagement_rate` and `completion` fail for different reasons (no views to
 * divide by vs. no row that reported a completion rate).
 */
export function rateMeasured(
  totals: Pick<RollupTotals, "engagement_rate" | "completion"> | null | undefined,
): boolean {
  return !!totals && totals.engagement_rate != null && totals.completion != null;
}

function pct(value: number, digits = 1): string {
  return `${(value * 100).toFixed(digits)}%`;
}

function isoDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("Z", "").slice(0, 16).replace("T", " ");
}

/* Checkpoint ordering used by `RetentionAnalyzer.detect_drops`
 * (engine/performance/retention.py STANDARD_CHECKPOINTS). */
const CHECKPOINT_ORDER = ["1s", "3s", "25%", "50%", "75%", "100%"];

function orderCheckpoints(curve: Record<string, number>): string[] {
  return Object.keys(curve ?? {}).sort((a, b) => {
    const ia = CHECKPOINT_ORDER.indexOf(a);
    const ib = CHECKPOINT_ORDER.indexOf(b);
    if (ia === -1 && ib === -1) return a.localeCompare(b);
    if (ia === -1) return 1;
    if (ib === -1) return -1;
    return ia - ib;
  });
}

/** A MOCK ANALYTICS badge — these figures are simulated, and must say so. */
function MockFlag({ on }: { on: boolean | null | undefined }) {
  if (on === null || on === undefined) return null;
  return on ? (
    <Badge tone="mock" title="settings.mock_analytics is on: these figures are generated, not measured.">
      MOCK ANALYTICS
    </Badge>
  ) : (
    <Badge tone="success" title="settings.mock_analytics is off.">
      REAL DATA
    </Badge>
  );
}

/** Platforms the publisher registry says do not report analytics at all. */
function useAnalyticsCapablePlatforms(): {
  byPlatform: Map<string, boolean>;
  loaded: boolean;
  error: string | null;
} {
  const caps = useWsQuery<{ items: PlatformCapability[] }>("/distribution/capabilities");
  const byPlatform = useMemo(() => {
    const m = new Map<string, boolean>();
    for (const c of caps.data?.items ?? []) m.set(c.platform, c.supports_analytics === true);
    return m;
  }, [caps.data]);
  return { byPlatform, loaded: caps.data !== null, error: caps.error };
}

/* ==========================================================================
 * Overview
 * ======================================================================= */

function OverviewView() {
  const overview = useWsQuery<AnalyticsOverview>("/analytics/overview");
  const measured = measuredPosts(overview.data);
  const totals = overview.data?.totals;

  /* WORK 16.5.7 §8: `null` from the backend IS the availability signal. The
     `anyMeasured` heuristic that used to stand in for it is gone — see the
     header. A tile is unavailable when the field is null, and renders the
     measured value (including 0) otherwise. */
  const totalTile = (
    field: keyof OverviewTotals,
    label: string,
    extra?: { hint?: ReactNode },
  ) => {
    const value = totals?.[field] ?? null;
    return (
      <StatTile
        label={label}
        value={value ?? undefined}
        unavailable={overview.data === null || value === null}
        hint={value === null ? "No post has a metric snapshot, so no total exists." : extra?.hint}
        source="GET /analytics/overview · totals"
      />
    );
  };

  return (
    <>
      <Panel title="At a glance" subtitle="GET /analytics/overview" dense>
        <Grid min={170} gap="sm">
          <StatTile
            label="Published posts"
            value={overview.data ? overview.data.posts_published : undefined}
            unavailable={overview.data === null}
            hint="Rows in the publication table."
            source="GET /analytics/overview"
          />
          <StatTile
            label="Posts with metrics"
            value={measured === null ? undefined : measured}
            unavailable={measured === null}
            tone={measured === 0 ? "warning" : "neutral"}
            hint="Posts that have a PostMetric snapshot. Totals sum only these."
            source="GET /analytics/overview · per_platform"
          />
          {totalTile("views", "Views")}
          {totalTile("likes", "Likes")}
          {totalTile("comments", "Comments")}
          {totalTile("shares", "Shares")}
          {totalTile("followers_gained", "Followers gained", {
            hint: "Only providers that report follower deltas contribute.",
          })}
          <StatTile
            label="Content items"
            value={overview.data ? overview.data.content_items : undefined}
            unavailable={overview.data === null}
            hint="A count of rows, not a performance figure."
            source="GET /analytics/overview"
          />
          <StatTile
            label="Total cost"
            value={<Money usd={overview.data ? overview.data.cost_total_usd : null} />}
            unavailable={overview.data === null}
            hint={
              overview.data && overview.data.cost_total_unknown_exposure_rows > 0
                ? `UNAVAILABLE: ${overview.data.cost_total_unknown_exposure_rows} cost row(s) record an exposure nobody can price.`
                : "Sum of CostEntry.amount_usd — a cost, never revenue."
            }
            source="GET /analytics/overview"
          />
        </Grid>
        {overview.data?.mock_analytics ? (
          <p className="ym-hint">
            <strong>MOCK ANALYTICS is on.</strong> Every figure above is generated
            by the backend, not read from a platform. Do not read it as performance.
          </p>
        ) : null}
      </Panel>

      <Panel title="Best post" subtitle="Highest views among posts that have a snapshot" dense>
        <QueryBoundary query={overview} skeletonRows={2}>
          {(d) =>
            d.best_post ? (
              <Grid min={200} gap="sm">
                <StatTile label="Platform" value={humanize(d.best_post.platform)} source="GET /analytics/overview" />
                <StatTile label="Title" value={d.best_post.title || "—"} source="GET /analytics/overview" />
                <StatTile label="Views" value={d.best_post.views} source="GET /analytics/overview" />
              </Grid>
            ) : (
              <EmptyState
                title="No measured post yet"
                description={
                  d.posts_published > 0
                    ? `${d.posts_published} post(s) exist but none has a PostMetric snapshot, so there is no best post to rank. That is "no measurement", not "zero views".`
                    : "Nothing has been published for this workspace yet."
                }
              />
            )
          }
        </QueryBoundary>
      </Panel>

      <Panel title="Breakdowns" subtitle="GET /analytics/breakdowns — measured posts grouped by observable features" dense>
        <BreakdownPanels />
      </Panel>

      <Panel title="Learned patterns" subtitle="GET /analytics/patterns" dense>
        <PatternTable />
      </Panel>
    </>
  );
}

const BREAKDOWN_GROUPS: { key: keyof Pick<AnalyticsBreakdowns, "by_topic" | "by_hook_style" | "by_duration">; label: string }[] = [
  { key: "by_topic", label: "By topic" },
  { key: "by_hook_style", label: "By hook style" },
  { key: "by_duration", label: "By duration" },
];

function BreakdownPanels() {
  const down = useWsQuery<AnalyticsBreakdowns>("/analytics/breakdowns");

  return (
    <QueryBoundary query={down} skeletonRows={4}>
      {(d) => (
        <>
          <Grid min={220} gap="sm">
            <StatTile
              label="Posts with metrics"
              value={d.posts_with_metrics}
              tone={d.posts_with_metrics === 0 ? "warning" : "neutral"}
              hint="Every breakdown below is built from these rows only."
              source="GET /analytics/breakdowns"
            />
            <StatTile
              label="Measured source"
              value={<MockFlag on={d.mock_analytics} />}
              hint="A label, not a figure."
              source="GET /analytics/breakdowns"
            />
          </Grid>
          <Grid min={280} gap="md">
            {BREAKDOWN_GROUPS.map((g) => (
              <Panel key={g.key} title={g.label} dense>
                <DataTable
                  rows={d[g.key] ?? []}
                  rowKey={(r) => r.key}
                  caption={g.label}
                  empty={`No ${g.label.toLowerCase()} measured`}
                  emptyHint="A group appears once a published post with a metric snapshot can be attributed to it."
                  columns={[
                    { key: "key", header: "Key", cell: (r) => <strong>{r.key}</strong> },
                    { key: "posts", header: "Posts", align: "right", cell: (r) => r.posts },
                    {
                      key: "avg",
                      header: "Avg views",
                      align: "right",
                      /* HONESTY (§8): the backend returns null for a bucket
                         with no measured post, so the null IS the signal. The
                         old `r.posts === 0` proxy is gone — `posts` counts
                         measured posts, so it happened to agree, but it was a
                         guess about a fact the backend now states. */
                      cell: (r) =>
                        r.avg_views === null ? (
                          <Unavailable title="The group has no measured post, so no average exists." />
                        ) : (
                          r.avg_views
                        ),
                    },
                    {
                      key: "eng",
                      header: "Engagement",
                      align: "right",
                      cell: (r) =>
                        r.engagement_pct === null ? (
                          <Unavailable title="No measured post has a view count, so no engagement rate exists." />
                        ) : (
                          pct(r.engagement_pct / 100, 2)
                        ),
                    },
                    {
                      key: "mock",
                      header: "Mock posts",
                      align: "right",
                      cell: (r) =>
                        r.mock_posts > 0 ? (
                          <Badge tone="mock" title="Mock publications are counted separately and never merged with real ones.">
                            {r.mock_posts}
                          </Badge>
                        ) : (
                          <span className="ym-muted">0</span>
                        ),
                      hideBelow: "md",
                    },
                  ]}
                />
              </Panel>
            ))}
          </Grid>
        </>
      )}
    </QueryBoundary>
  );
}

function PatternTable() {
  const patterns = useWsQuery<{ items: PatternRow[] }>("/analytics/patterns");

  const columns: Column<PatternRow>[] = [
    { key: "key", header: "Pattern", cell: (p) => <strong>{p.pattern_key}</strong> },
    { key: "desc", header: "Description", cell: (p) => p.description || <span className="ym-muted">—</span> },
    {
      key: "improvement",
      header: "Delta vs median",
      align: "right",
      // improvement_pct is a measured rate difference. It is NOT a confidence and
      // is never rendered as one.
      cell: (p) =>
        p.improvement_pct === null || p.improvement_pct === undefined ? (
          <Unavailable title="The pattern row records no measured improvement." />
        ) : (
          <Badge tone={p.improvement_pct >= 0 ? "success" : "danger"}>
            {p.improvement_pct >= 0 ? "+" : ""}
            {Number(p.improvement_pct).toFixed(1)}%
          </Badge>
        ),
    },
    {
      key: "confidence",
      header: "Backend confidence label",
      cell: (p) =>
        p.confidence ? (
          <Badge tone="info" title="A label the Learning Agent wrote. Not a probability computed here.">
            {humanize(p.confidence)}
          </Badge>
        ) : (
          <Unavailable title="The pattern carries no confidence label." />
        ),
    },
    { key: "n", header: "Sample", align: "right", cell: (p) => p.sample_size },
    {
      key: "active",
      header: "State",
      cell: (p) =>
        p.active ? <Badge tone="success">Active</Badge> : <Badge tone="neutral">Inactive</Badge>,
      hideBelow: "md",
    },
    { key: "updated", header: "Updated", cell: (p) => isoDay(p.updated_at), hideBelow: "lg" },
  ];

  return (
    <QueryBoundary
      query={patterns}
      skeletonRows={4}
      empty="No learned pattern yet"
      emptyHint="The Learning Agent writes a pattern once measured posts support one; a channel without measured posts produces none."
    >
      {(d) => (
        <DataTable
          rows={d.items ?? []}
          columns={columns}
          rowKey={(p) => p.pattern_key}
          caption="Learned performance patterns"
          empty="No learned pattern yet"
          emptyHint="No pattern has cleared the Learning Agent's evidence floor for this workspace."
        />
      )}
    </QueryBoundary>
  );
}

/* ==========================================================================
 * Content — per-post metrics
 * ======================================================================= */

function ContentView() {
  const posts = useWsQuery<PublishedPostList>("/publishing/posts?limit=100");

  const columns: Column<PublishedPostRow>[] = [
    {
      key: "published",
      header: "Published",
      cell: (p) => isoDay(p.published_at),
      hideBelow: "md",
    },
    { key: "platform", header: "Platform", cell: (p) => humanize(p.platform) },
    {
      key: "title",
      header: "Title",
      cell: (p) =>
        p.remote_url ? (
          <a href={p.remote_url} target="_blank" rel="noreferrer">
            {p.title || p.remote_url.slice(0, 40)}
          </a>
        ) : (
          p.title || <span className="ym-muted">—</span>
        ),
    },
    {
      key: "mock",
      header: "Source",
      cell: (p) =>
        p.is_mock ? (
          <Badge tone="mock" title="Nothing was sent to a platform; a local stand-in recorded the intent.">
            MOCK
          </Badge>
        ) : (
          <span className="ym-muted">real</span>
        ),
      hideBelow: "lg",
    },
    {
      key: "views",
      header: "Views",
      align: "right",
      /* HONESTY (§8): the null check is on `views` itself now. It used to be on
       * `completion_rate`, an unrelated field standing in for "does a snapshot
       * exist" — which also mislabelled a post that genuinely recorded 0 views
       * as unmeasured. */
      cell: (p) => (p.metrics?.views == null ? <Unavailable title="No metric snapshot for this post." /> : p.metrics.views),
    },
    {
      key: "likes",
      header: "Likes",
      align: "right",
      cell: (p) => (p.metrics?.likes == null ? <Unavailable title="No metric snapshot for this post." /> : p.metrics.likes),
      hideBelow: "md",
    },
    {
      key: "comments",
      header: "Comments",
      align: "right",
      cell: (p) => (p.metrics?.comments == null ? <Unavailable title="No metric snapshot for this post." /> : p.metrics.comments),
      hideBelow: "md",
    },
    {
      key: "completion",
      header: "Completion",
      align: "right",
      cell: (p) =>
        p.metrics?.completion_rate == null ? (
          <Unavailable title="No metric snapshot for this post." />
        ) : (
          pct(p.metrics.completion_rate, 1)
        ),
    },
  ];

  return (
    <Panel
      title="Measured content"
      subtitle="GET /publishing/posts — latest PostMetric snapshot per publication"
      dense
    >
      <QueryBoundary
        query={posts}
        skeletonRows={6}
        empty="No publication recorded"
        emptyHint="A row appears once a provider accepted the content or a handoff was prepared. Nothing here is estimated."
      >
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(p) => p.id}
            caption="Publications and their measured metrics"
            maxHeight={620}
            empty="No publication recorded"
            emptyHint="Nothing has been published for this workspace yet."
          />
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Campaign + Platform
 * ======================================================================= */

function useCampaignOptions(): { items: CampaignOption[]; query: QueryState<CampaignOptions> } {
  const query = useWsQuery<CampaignOptions>("/campaigns");
  const items = query.data?.items ?? [];
  return { items, query };
}

function CampaignView() {
  const { items, query } = useCampaignOptions();
  const [campaignId, setCampaignId] = useState("");
  const active = campaignId || (items[0]?.id ?? "");

  const overview = useWsQuery<PerformanceOverview>(
    active ? `/performance/overview?campaign_id=${encodeURIComponent(active)}` : "",
    { enabled: !!active },
  );

  const rollup = overview.data?.rollup ?? null;
  const totals = rollup?.totals ?? null;

  const shortColumns: Column<ShortRollup>[] = [
    { key: "short", header: "Short", cell: (s) => <code>{s.short_id?.slice(0, 8)}</code> },
    { key: "posts", header: "Posts", align: "right", cell: (s) => s.posts },
    /* HONESTY (§8): a null total renders UNAVAILABLE. `s.views == null` also
       distinguishes "unmeasured" from a real 0, which `> 0` never could. */
    { key: "views", header: "Views", align: "right", cell: (s) => (s.views ?? <Unavailable title="No post for this short has a metric snapshot." />) },
    { key: "likes", header: "Likes", align: "right", cell: (s) => (s.likes ?? <Unavailable title="No post for this short has a metric snapshot." />), hideBelow: "md" },
    { key: "shares", header: "Shares", align: "right", cell: (s) => (s.shares ?? <Unavailable title="No post for this short has a metric snapshot." />), hideBelow: "md" },
    {
      key: "watch",
      header: "Watch time",
      align: "right",
      cell: (s) => (s.watch_time == null ? <Unavailable title="No watch time was reported." /> : `${Math.round(s.watch_time)}s`),
      hideBelow: "lg",
    },
    {
      key: "completion",
      header: "Completion",
      align: "right",
      cell: (s) => (s.completion == null ? <Unavailable title="No post for this short reported a completion rate." /> : pct(s.completion, 1)),
    },
  ];

  return (
    <>
      <Panel title="Campaign" subtitle="GET /performance/overview" dense>
        <QueryBoundary query={query} skeletonRows={2}>
          {() =>
            items.length === 0 ? (
              <EmptyState
                title="No campaign to report on"
                description="Campaign rollups are per-campaign. Create a campaign first."
              />
            ) : (
              <>
                <Select
                  label="Campaign"
                  value={active}
                  onChange={(e) => setCampaignId(e.target.value)}
                >
                  {items.map((c) => (
                    <option key={c.id} value={c.id}>
                      {c.name || c.id.slice(0, 8)}
                    </option>
                  ))}
                </Select>
                <Grid min={160} gap="sm">
                  <StatTile
                    label="Posts attributed"
                    value={rollup ? rollup.post_count : undefined}
                    unavailable={rollup === null}
                    source="GET /performance/overview"
                  />
                  <StatTile
                    label="Shorts"
                    value={rollup ? rollup.short_count : undefined}
                    unavailable={rollup === null}
                    source="GET /performance/overview"
                  />
                  <StatTile
                    label="Views"
                    value={totals ? totals.views : undefined}
                    unavailable={totals === null || totals.views === 0}
                    hint={totals && totals.views === 0 ? "No attributed post has a metric snapshot." : undefined}
                    source="GET /performance/overview · rollup.totals"
                  />
                  <StatTile
                    label="Engagement rate"
                    value={totals?.engagement_rate == null ? undefined : pct(totals.engagement_rate)}
                    unavailable={totals === null || totals.engagement_rate == null}
                    hint="(likes + comments + shares + saves) / views. Unavailable when no post has a view count to divide by."
                    source="engine/campaign/analytics.py::_accumulate"
                  />
                  <StatTile
                    label="Completion"
                    value={totals?.completion == null ? undefined : pct(totals.completion)}
                    unavailable={totals === null || totals.completion == null}
                    hint="Views-weighted mean over the posts that REPORTED a completion rate."
                    source="engine/campaign/analytics.py::_accumulate"
                  />
                  <StatTile
                    label="Watch time"
                    value={totals?.watch_time == null ? undefined : `${Math.round(totals.watch_time)}s`}
                    unavailable={totals === null || totals.watch_time == null}
                    hint="Sum of PostMetric.watch_time_seconds across measured posts."
                    source="GET /performance/overview · rollup.totals"
                  />
                </Grid>
                {rollup?.master_content_id ? (
                  <p className="ym-hint">
                    Master content: <code>{rollup.master_content_id.slice(0, 8)}</code>. Attribution runs
                    publication → variant → short → master → campaign.
                  </p>
                ) : (
                  <p className="ym-hint">
                    No master content item was resolved for this campaign, so nothing is attributed to one.
                  </p>
                )}
              </>
            )
          }
        </QueryBoundary>
      </Panel>

      <Panel title="Shorts in this campaign" subtitle="Per-short rollup, straight from the backend" dense>
        {active ? (
          <QueryBoundary
            query={overview}
            skeletonRows={4}
            empty="No shorts in this campaign"
            emptyHint="Shorts are derived assets of the master. This campaign has none yet."
          >
            {(d) => (
              <DataTable
                rows={d.rollup?.shorts ?? []}
                columns={shortColumns}
                rowKey={(s) => s.short_id}
                caption="Campaign shorts"
                maxHeight={460}
                empty="No shorts in this campaign"
                emptyHint="A short appears once a derived asset is attached to the campaign."
              />
            )}
          </QueryBoundary>
        ) : (
          <EmptyState title="Select a campaign" description="Short rollups need a campaign." />
        )}
      </Panel>
    </>
  );
}

function PlatformView() {
  const { items } = useCampaignOptions();
  const [campaignId, setCampaignId] = useState("");
  const active = campaignId || (items[0]?.id ?? "");
  const { byPlatform, loaded, error } = useAnalyticsCapablePlatforms();

  const overview = useWsQuery<PerformanceOverview>(
    active ? `/performance/overview?campaign_id=${encodeURIComponent(active)}` : "",
    { enabled: !!active },
  );

  const rows = useMemo(() => {
    const map = overview.data?.platforms ?? {};
    return Object.entries(map).sort((a, b) => (b[1].views ?? 0) - (a[1].views ?? 0));
  }, [overview.data]);

  const columns: Column<[string, RollupTotals & { posts: number }]>[] = [
    { key: "platform", header: "Platform", cell: ([name]) => humanize(name) },
    {
      key: "supported",
      header: "Registry says analytics",
      cell: ([name]) => {
        if (!loaded) return <Unavailable title="The publisher registry has not loaded." />;
        const supported = byPlatform.get(name);
        if (supported === undefined) {
          return <Unavailable title="This platform is not in the publisher registry." />;
        }
        return supported ? (
          <Badge tone="success">supported</Badge>
        ) : (
          <Badge tone="unknown" title="supports_analytics is false: this platform reports no analytics, so any figure for it would be invented.">
            NOT REPORTED
          </Badge>
        );
      },
    },
    { key: "posts", header: "Posts", align: "right", cell: ([, v]) => v.posts },
    {
      key: "views",
      header: "Views",
      align: "right",
      cell: ([, v]) => (v.views == null ? <Unavailable title="No measured post for this platform." /> : v.views),
    },
    {
      key: "engagement",
      header: "Engagement",
      align: "right",
      cell: ([, v]) => (v.engagement_rate == null ? <Unavailable title="No measured post for this platform has a view count to divide by." /> : pct(v.engagement_rate)),
      hideBelow: "md",
    },
    {
      key: "completion",
      header: "Completion",
      align: "right",
      cell: ([, v]) => (v.completion == null ? <Unavailable title="No measured post for this platform reported a completion rate." /> : pct(v.completion)),
      hideBelow: "md",
    },
    {
      key: "watch",
      header: "Watch time",
      align: "right",
      cell: ([, v]) => (v.watch_time == null ? <Unavailable title="No watch time reported." /> : `${Math.round(v.watch_time)}s`),
      hideBelow: "lg",
    },
  ];

  return (
    <>
      <Panel title="Campaign platform split" subtitle="GET /performance/overview · platforms" dense>
        {active ? (
          <>
            <Select label="Campaign" value={active} onChange={(e) => setCampaignId(e.target.value)}>
              {items.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name || c.id.slice(0, 8)}
                </option>
              ))}
            </Select>
            {error ? (
              <p className="ym-error">
                The publisher registry could not be read ({error}), so whether a
                platform reports analytics is unknown. Nothing here is claimed as
                supported.
              </p>
            ) : null}
            <QueryBoundary
              query={overview}
              skeletonRows={4}
              empty="No platform published in this campaign"
              emptyHint="A platform row appears once a post is attributed to it."
            >
              {(d) => (
                <DataTable
                  rows={rows}
                  columns={columns}
                  rowKey={([name]) => name}
                  caption="Platform comparison"
                  empty="No platform published in this campaign"
                  emptyHint="Nothing has been attributed to this campaign yet."
                />
              )}
            </QueryBoundary>
          </>
        ) : (
          <EmptyState title="Select a campaign" description="The platform split is per-campaign." />
        )}
      </Panel>

      <Panel
        title="Workspace-wide platform totals"
        subtitle="GET /analytics/overview · per_platform — measured posts only"
        dense
      >
        <WorkspacePlatformTable />
      </Panel>
    </>
  );
}

function WorkspacePlatformTable() {
  const overview = useWsQuery<AnalyticsOverview>("/analytics/overview");
  const { byPlatform, loaded, error } = useAnalyticsCapablePlatforms();

  const columns: Column<[string, OverviewPlatform]>[] = [
    { key: "platform", header: "Platform", cell: ([name]) => humanize(name) },
    {
      key: "supported",
      header: "Registry says analytics",
      cell: ([name]) => {
        if (!loaded) return <Unavailable title="The publisher registry has not loaded." />;
        const supported = byPlatform.get(name);
        if (supported === undefined) return <Unavailable title="Not in the publisher registry." />;
        return supported ? (
          <Badge tone="success">supported</Badge>
        ) : (
          <Badge tone="unknown" title="supports_analytics is false — no analytics is reported for this platform.">
            NOT REPORTED
          </Badge>
        );
      },
    },
    { key: "posts", header: "Measured posts", align: "right", cell: ([, v]) => v.posts },
    {
      key: "views",
      header: "Views",
      align: "right",
      cell: ([, v]) => (v.posts > 0 ? v.views : <Unavailable title="No measured post on this platform." />),
    },
  ];

  return (
    <QueryBoundary
      query={overview}
      skeletonRows={4}
      empty="No measured platform yet"
      emptyHint="A platform appears here once one of its posts has a metric snapshot."
    >
      {(d) => {
        const entries = Object.entries(d.per_platform ?? {}).sort((a, b) => (b[1].views ?? 0) - (a[1].views ?? 0));
        return (
          <>
            {error ? <p className="ym-error">Publisher registry unreadable ({error}).</p> : null}
            <DataTable
              rows={entries}
              columns={columns}
              rowKey={([name]) => name}
              caption="Workspace platform totals"
              empty="No measured platform yet"
              emptyHint="No publication in this workspace has a metric snapshot."
            />
          </>
        );
      }}
    </QueryBoundary>
  );
}

/* ==========================================================================
 * Retention
 * ======================================================================= */

function RetentionView() {
  const [postId, setPostId] = useState("");
  const [shortId, setShortId] = useState("");
  const [lookup, setLookup] = useState<{ post_id?: string; short_id?: string }>({});

  const canQuery = !!lookup.post_id || !!lookup.short_id;
  const params = new URLSearchParams();
  if (lookup.post_id) params.set("post_id", lookup.post_id);
  if (lookup.short_id) params.set("short_id", lookup.short_id);

  const query = useQuery<RetentionResponse>(
    () => wsApi.get(`/performance/retention?${params.toString()}`) as Promise<RetentionResponse>,
    { enabled: canQuery, deps: [params.toString()] },
  );

  return (
    <Panel
      title="Retention"
      subtitle="GET /performance/retention — the backend returns UNAVAILABLE when no provider reports a curve"
      dense
    >
      <Grid min={220} gap="sm">
        <Field
          label="Post id"
          value={postId}
          onChange={(e) => setPostId(e.target.value)}
          placeholder="published post id…"
          hint="A published post's analytics."
        />
        <Field
          label="Short (content item) id"
          value={shortId}
          onChange={(e) => setShortId(e.target.value)}
          placeholder="content item id…"
        />
        <div>
          <Button
            variant="primary"
            disabled={!postId.trim() && !shortId.trim()}
            onClick={() => {
              const p = postId.trim();
              const s = shortId.trim();
              setLookup(p ? { post_id: p } : s ? { short_id: s } : {});
            }}
          >
            Load curve
          </Button>
        </div>
      </Grid>

      {!canQuery ? (
        <EmptyState
          title="Enter a post or short id"
          description="Retention is per publication. The backend refuses the lookup without one (400) rather than guessing."
        />
      ) : (
        <QueryBoundary query={query} skeletonRows={3}>
          {(d) =>
            d.status === "UNAVAILABLE" ? (
              <>
                <Grid min={200} gap="sm">
                  <StatTile label="Retention curve" unavailable hint="No provider reports granular retention here." source="GET /performance/retention" />
                </Grid>
                <p className="ym-hint">
                  Reason from the backend: {d.reason ?? "not stated"}. This is not a flat curve and must not be
                  drawn as 100% throughout.
                </p>
                {d.coarse_proxy ? (
                  <p className="ym-hint">
                    The backend offers a labelled coarse proxy. It is a substitute, not a curve:
                    <code> {JSON.stringify(d.coarse_proxy).slice(0, 300)}</code>
                  </p>
                ) : null}
              </>
            ) : (
              <RetentionCurvePanel data={d} />
            )
          }
        </QueryBoundary>
      )}
    </Panel>
  );
}

function RetentionCurvePanel({ data }: { data: RetentionResponse }) {
  const curve = data.curve ?? {};
  const ordered = orderCheckpoints(curve);
  const drops = data.drops ?? [];
  const mapping = data.mapping ?? {};
  const dropTargets = new Set(drops.map((d) => d.to).filter(Boolean) as string[]);

  return (
    <>
      <Grid min={180} gap="sm">
        <StatTile
          label="Curve status"
          value={<Badge tone="success">AVAILABLE</Badge>}
          hint={data.source ? `Source: ${data.source}` : "No source string was returned."}
          source="GET /performance/retention"
        />
        <StatTile
          label="Checkpoints measured"
          value={`${ordered.length}/${CHECKPOINT_ORDER.length}`}
          tone={ordered.length < CHECKPOINT_ORDER.length ? "warning" : "neutral"}
          hint="Missing checkpoints stay missing; they are never interpolated."
          source="GET /performance/retention · curve"
        />
        <StatTile
          label="Sharp drops"
          value={drops.length}
          tone={drops.length > 0 ? "danger" : "neutral"}
          hint="Backend threshold detection, not a visual estimate."
          source="RetentionAnalyzer.detect_drops"
        />
        <StatTile
          label="Rewatches"
          value={(data.rewatches ?? []).length}
          hint="The analyzer currently returns an empty list; that is UNAVAILABLE, not zero rewatches."
          source="GET /performance/retention"
        />
      </Grid>

      {(data.missing ?? []).length > 0 ? (
        <p className="ym-hint">
          Missing checkpoints: <strong>{(data.missing ?? []).join(", ")}</strong>. Those points have no
          measurement, so nothing is drawn for them.
        </p>
      ) : null}

      <DataTable
        rows={ordered.map((cp) => ({ cp, value: curve[cp] ?? 0, meta: mapping[cp] ?? {} }))}
        rowKey={(r) => r.cp}
        caption="Retention curve by checkpoint"
        empty="No curve checkpoints"
        emptyHint="The backend reported AVAILABLE but returned an empty curve."
        columns={[
          { key: "cp", header: "Checkpoint", cell: (r) => <strong>{r.cp}</strong> },
          { key: "value", header: "Retained", align: "right", cell: (r) => pct(r.value, 1) },
          {
            key: "drop",
            header: "Drop",
            cell: (r) =>
              dropTargets.has(r.cp) ? (
                <Badge tone="danger">audience loss</Badge>
              ) : (
                <span className="ym-muted">—</span>
              ),
          },
          {
            key: "scene",
            header: "Scene mapping",
            cell: (r) => {
              if (!r.meta.mapped) {
                return (
                  <span className="ym-muted">
                    unmapped{r.meta.reason ? ` — ${r.meta.reason}` : ""}
                  </span>
                );
              }
              return (
                <span>
                  {r.meta.scene_title || (r.meta.scene_index != null ? `scene ${r.meta.scene_index}` : "scene —")}
                  {r.meta.is_hook ? <Badge tone="warning">hook</Badge> : null}
                </span>
              );
            },
            hideBelow: "md",
          },
        ]}
      />

      {drops.length > 0 ? (
        <DataTable
          rows={drops}
          rowKey={(_d, i) => String(i)}
          caption="Detected audience drops"
          empty="No sharp drop detected"
          emptyHint="No checkpoint-to-checkpoint loss crossed the analyzer's threshold."
          columns={[
            { key: "type", header: "Type", cell: (d) => humanize(d.type) },
            {
              key: "from",
              header: "From",
              cell: (d) => d.from ?? (d.from_t != null ? `${d.from_t}s` : "—"),
            },
            { key: "to", header: "To", cell: (d) => d.to ?? (d.to_t != null ? `${d.to_t}s` : "—") },
            { key: "loss", header: "Loss", align: "right", cell: (d) => pct(d.loss, 1) },
          ]}
        />
      ) : null}
    </>
  );
}

/* ==========================================================================
 * Variants / creative dimensions
 * ======================================================================= */

const COMPARE_GROUPS = ["hook", "caption", "duration", "voice", "broll", "posting-window"] as const;

function VariantsView() {
  const { items } = useCampaignOptions();
  const [campaignId, setCampaignId] = useState("");
  const [groupBy, setGroupBy] = useState<string>("hook");
  const active = campaignId || (items[0]?.id ?? "");

  const compare = useWsQuery<CompareResponse>(
    active
      ? `/performance/compare?campaign_id=${encodeURIComponent(active)}&group_by=${encodeURIComponent(groupBy)}`
      : "",
    { enabled: !!active },
  );

  const columns: Column<CompareGroup>[] = [
    { key: "group", header: humanize(groupBy), cell: (g) => <strong>{g.group}</strong> },
    {
      key: "n",
      header: "Sample",
      align: "right",
      cell: (g) => (
        <span>
          {g.n} {g.low_sample ? <Badge tone="warning" title="The backend flags n &lt; 2 as low-sample.">low-sample</Badge> : null}
        </span>
      ),
    },
    {
      key: "views",
      header: "Views",
      align: "right",
      cell: (g) => (g.n === 0 ? <Unavailable title="No measured post in this group." /> : g.views),
    },
    {
      key: "completion",
      header: "Avg completion",
      align: "right",
      /* HONESTY (§8): `avg_completion` is null when no post in the group
         REPORTED a completion rate — a different fact from "no post measured",
         which is what `g.n === 0` tested. Both render UNAVAILABLE; the reason
         differs, and the title says which. */
      cell: (g) =>
        g.avg_completion == null ? (
          <Unavailable
            title={
              g.n === 0
                ? "No measured post in this group."
                : `${g.n} post(s) measured, but none reported a completion rate.`
            }
          />
        ) : (
          <span
            title={`Mean of the backend's PostMetric.completion_rate across ${g.reported_completion_samples} of ${g.n} post(s).`}
          >
            {pct(g.avg_completion, 1)}
          </span>
        ),
      hideBelow: "md",
    },
  ];

  return (
    <Panel
      title="Creative comparison"
      subtitle="GET /performance/compare — correlational only, by the backend's own declaration"
      dense
    >
      {active ? (
        <>
          <Grid min={200} gap="sm">
            <Select label="Campaign" value={active} onChange={(e) => setCampaignId(e.target.value)}>
              {items.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name || c.id.slice(0, 8)}
                </option>
              ))}
            </Select>
            <Select label="Group by" value={groupBy} onChange={(e) => setGroupBy(e.target.value)}>
              {COMPARE_GROUPS.map((g) => (
                <option key={g} value={g}>
                  {humanize(g)}
                </option>
              ))}
            </Select>
          </Grid>
          {compare.data ? (
            <p className="ym-hint">
              <Badge tone={compare.data.causal ? "danger" : "info"}>
                {compare.data.causal ? "CAUSAL" : "CORRELATIONAL"}
              </Badge>{" "}
              {compare.data.note}. A group being higher here is an observation, not a cause.
            </p>
          ) : null}
          <QueryBoundary
            query={compare}
            skeletonRows={4}
            empty={`No measured posts to compare by ${groupBy}`}
            emptyHint="A group exists once a publication with a metric snapshot is attributed to it."
          >
            {(d) => (
              <DataTable
                rows={d.groups ?? []}
                columns={columns}
                rowKey={(g) => g.group}
                caption={`Creative comparison by ${groupBy}`}
                maxHeight={460}
                empty={`No measured posts to compare by ${groupBy}`}
                emptyHint="Publish and measure content in this campaign to populate the comparison."
              />
            )}
          </QueryBoundary>
        </>
      ) : (
        <EmptyState title="Select a campaign" description="Creative comparison is per-campaign." />
      )}
    </Panel>
  );
}

/* ==========================================================================
 * Provider-supported metrics
 * ======================================================================= */

function ProviderView() {
  const caps = useWsQuery<{ items: PlatformCapability[] }>("/distribution/capabilities");

  const columns: Column<PlatformCapability>[] = [
    { key: "platform", header: "Platform", cell: (c) => <strong>{humanize(c.platform)}</strong> },
    {
      key: "analytics",
      header: "Reports analytics",
      cell: (c) =>
        c.supports_analytics ? (
          <Badge tone="success">yes</Badge>
        ) : (
          <Badge tone="unknown" title="The registry declares no analytics for this platform, so every figure for it is UNAVAILABLE.">
            NO
          </Badge>
        ),
    },
    {
      key: "mode",
      header: "Publish mode",
      cell: (c) => <Badge tone={c.publish_mode === "DIRECT_PUBLISH" ? "live" : "handoff"}>{humanize(c.publish_mode)}</Badge>,
      hideBelow: "md",
    },
    { key: "campaigns", header: "Campaign platforms", cell: (c) => (c.campaign_platforms ?? []).map((p) => humanize(p)).join(", ") || <span className="ym-muted">none</span>, hideBelow: "lg" },
    {
      key: "caps",
      header: "Declared capabilities",
      cell: (c) => (
        <span>
          {(c.capabilities ?? []).slice(0, 6).map((k) => (
            <Badge key={k} tone="neutral">
              {humanize(k)}
            </Badge>
          ))}
        </span>
      ),
      hideBelow: "lg",
    },
  ];

  return (
    <Panel
      title="Provider metric support"
      subtitle="GET /distribution/capabilities — which platforms report analytics at all"
      dense
    >
      <p className="ym-hint">
        This registry is the authority on whether a figure can exist. A platform
        marked <strong>NO</strong> produces no measured metric, so this screen shows
        UNAVAILABLE for it rather than a zero.
      </p>
      <QueryBoundary
        query={caps}
        skeletonRows={6}
        empty="No platform registered"
        emptyHint="The publisher registry returned no specs for this deployment."
      >
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(c) => c.platform}
            caption="Platform analytics support"
            maxHeight={520}
            empty="No platform registered"
            emptyHint="No publisher spec is available in this deployment."
          />
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

const TABS = [
  { id: "overview", label: "Overview" },
  { id: "content", label: "Content" },
  { id: "campaign", label: "Campaign" },
  { id: "platform", label: "Platform" },
  { id: "retention", label: "Retention" },
  { id: "variants", label: "Variants" },
  { id: "provider", label: "Provider metrics" },
] as const;

type TabId = (typeof TABS)[number]["id"];

export function Analytics() {
  const { workspaceId, workspace } = useSession();
  const [tab, setTab] = useState<TabId>("overview");

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Analytics" description="Measured outcomes only." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to measure yet"
            description="Analytics is workspace-scoped. Select a workspace to load its measured metrics."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Analytics"
        description={
          workspace?.name
            ? `${workspace.name} — measured outcomes only. A metric that was not measured reads UNAVAILABLE, never 0.`
            : "Measured outcomes only. A metric that was not measured reads UNAVAILABLE, never 0."
        }
      />

      <Tabs tabs={TABS.map((t) => ({ id: t.id, label: t.label }))} active={tab} onChange={(id) => setTab(id as TabId)} />

      {tab === "overview" ? <OverviewView /> : null}
      {tab === "content" ? <ContentView /> : null}
      {tab === "campaign" ? <CampaignView /> : null}
      {tab === "platform" ? <PlatformView /> : null}
      {tab === "retention" ? <RetentionView /> : null}
      {tab === "variants" ? <VariantsView /> : null}
      {tab === "provider" ? <ProviderView /> : null}
    </>
  );
}

export default Analytics;

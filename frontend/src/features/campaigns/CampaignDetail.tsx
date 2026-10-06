/* CampaignDetail — Campaign → Master → Shorts → Platform Variants → Publications.
 *
 *   GET  /campaigns/{id}               the campaign row + content progress
 *   GET  /campaigns/{id}/aggregate     master, shorts, variants, plan, qc, costs
 *   GET  /campaigns/{id}/content       the shorts
 *   GET  /campaigns/{id}/qc            the per-short QC verdicts
 *   GET  /publishing/posts             the workspace's publications + modes
 *   GET  /distribution/capabilities    per-platform publish_mode
 *   GET  /content?campaign_id=…        the campaign's content items
 *
 * `aggregate` is the one endpoint that returns the whole tree, so the hierarchy
 * is rendered from it and the narrower endpoints back the panels where they are
 * the authority (QC is scored per variant; content is the canonical item list).
 *
 * WHAT THE BACKEND ALLOWS, AND ONLY THAT
 *
 *   POST /campaigns/{id}/derive          enqueues the derive chain (202)
 *   POST /campaigns/{id}/generate-more   creates more short IDEAS
 *   POST /campaigns/{id}/schedule        builds a publishing plan + entries
 *   POST /campaigns/{id}/publish         enqueues real publish jobs (202)
 *   POST /campaigns/{id}/cancel          terminal CANCELLED
 *   POST /content/{id}/regenerate        bumps lineage_version, status → IDEA
 *   POST /content/{id}/platform-variants regenerates one platform cut
 *
 * There is deliberately NO retry control anywhere on this screen. A publish that
 * failed is not retried from here: the backend offers no such endpoint, and
 * `POST /campaigns/{id}/publish` on an already-queued variant answers
 * "already queued" rather than re-sending it. Inventing a Retry here is exactly
 * how a paid surface gets double-submitted.
 *
 * LIVE / MOCK / HANDOFF
 *
 * See `Campaigns.tsx` for the full argument. The short version: no endpoint
 * returns `PublishedPost.publication_mode`, so the mode is resolved from
 * `is_mock` plus the publisher registry's `publish_mode`, and the undecidable
 * case renders MODE UNAVAILABLE. It is never collapsed into a status badge, and
 * the three real modes get three distinct design-system colours.
 *
 * `PlatformVariant.status = AWAITING_HANDOFF` IS an authoritative handoff
 * signal — `publish_flow` writes it precisely so nothing downstream treats
 * prepared work as published — so it renders as a HANDOFF mode badge beside the
 * production-state badge, never merged with it.
 *
 * `campaignId` is accepted as a prop and falls back to the route parameter, so
 * the screen works mounted by the router (`/campaigns/:campaignId`) and handed
 * an id directly.
 */

import { useMemo, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Badge,
  Button,
  DataTable,
  DestructiveButton,
  EmptyState,
  Grid,
  Modal,
  ModeBadge,
  Money,
  PageHeader,
  Panel,
  QueryBoundary,
  Select,
  StatTile,
  StatusBadge,
  Tabs,
  humanize,
  type Column,
} from "../../design-system/primitives";
import { useMutation, useWsQuery } from "../../api/queries";
import { wsApi } from "../../lib/api";
import { useSession } from "../../state/session";
import {
  CTA_KINDS,
  CAMPAIGN_PLATFORMS,
  PublicationModeBadge,
  resolvePublicationMode,
  type CampaignRow,
  type PublishedPostList,
  type ResolvedMode,
} from "./Campaigns";

/* ==========================================================================
 * Response shapes — api/v1/campaigns.py
 * ======================================================================= */

/* `campaign_flows_router._short_dto` */
export type ShortDto = {
  id: string;
  topic: string;
  status: string;
  derivation_type: string | null;
  parent_content_id: string | null;
  root_content_id: string | null;
  campaign_id: string | null;
};

/* `campaign_flows_router._read_variants` → PlatformVariant rows */
export type VariantDto = {
  id: string;
  short_content_id: string;
  platform: string;
  aspect_ratio: string;
  timeline_id: string | null;
  shares_base_timeline: boolean;
  metadata: Record<string, unknown>;
  /** DRAFT | READY | SCHEDULED | PUBLISHED | FAILED | AWAITING_HANDOFF */
  status: string;
};

/* `campaign_flows_router._qc_summary` */
export type QcItem = {
  short_content_id: string;
  /** null when no QualityCheck row exists for this short. */
  overall: number | null;
  passed: boolean | null;
};
export type QcSummary = { items: QcItem[]; avg_overall: number | null };

/* `campaign_flows_router._cost_summary` — CostEntry rows matching the campaign */
export type CostSummary = { campaign_id: string; entries: number; total_usd: number };

/* `campaign_flows_router.vc_progress_inner` */
export type Progress = { completed: number; total: number };

/* `engine/campaign/publish_flow.build_publishing_plan` items */
export type PublishingPlanItem = {
  variant_id: string;
  platform: string;
  planned_at: string;
  priority: number;
  depends_on: string[];
  approval_state: string;
  publication_state: string;
  is_master: boolean;
};

/* `campaign_flows_router.vc_aggregate` */
export type CampaignAggregate = {
  campaign: { id: string; name: string; status: string; platforms: string[] };
  master: ShortDto | null;
  shorts: ShortDto[];
  variants: VariantDto[];
  /** The Lane A PublishingPlan row, else the Campaign.kpis_json sidecar plan. */
  plan: Record<string, unknown> & { items?: PublishingPlanItem[]; status?: string };
  progress: Progress;
  qc: QcSummary;
  costs: CostSummary;
};

/* `campaigns_router.campaign_detail` */
export type CampaignDetailRow = CampaignRow & {
  progress: { content_items: number; published: number };
};

export type PublishResult = {
  enqueued: { variant_id: string; job_id: string }[];
  skipped: { variant_id: string; reason: string }[];
};

/* ==========================================================================
 * Small helpers
 * ======================================================================= */

function isoMoment(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("Z", " UTC").replace("T", " ").slice(0, 16);
}

/** The publishing plan items, from either shape the backend may return. */
export function planItems(plan: CampaignAggregate["plan"]): PublishingPlanItem[] {
  if (Array.isArray(plan.items)) return plan.items;
  const nested = plan.publishing_items;
  return Array.isArray(nested) ? (nested as PublishingPlanItem[]) : [];
}

/**
 * A variant's authoritative publication mode, when it has one.
 *
 * `AWAITING_HANDOFF` is written by `publish_flow` exactly so prepared work is
 * never treated as published, so it is a real HANDOFF signal and gets the
 * HANDOFF colour. Every other variant status is PRODUCTION state, not
 * publication mode, and stays a StatusBadge.
 */
export function variantPublicationMode(status: string | null | undefined): ResolvedMode | null {
  return (status ?? "").toUpperCase() === "AWAITING_HANDOFF" ? "HANDOFF" : null;
}

function variantsByShort(variants: VariantDto[]): Map<string, VariantDto[]> {
  const map = new Map<string, VariantDto[]>();
  for (const v of variants) {
    const list = map.get(v.short_content_id);
    if (list) list.push(v);
    else map.set(v.short_content_id, [v]);
  }
  return map;
}

function useHandoffPlatforms(): { ids: Set<string>; loaded: boolean; error: string | null } {
  const caps = useWsQuery<{ items: { platform: string; user_handoff: boolean; publish_mode: string }[] }>(
    "/distribution/capabilities",
  );
  const ids = useMemo(() => {
    const set = new Set<string>();
    for (const c of caps.data?.items ?? []) {
      // `user_handoff` is the boolean; `publish_mode` is the string form. Either
      // one is read from the registry, never inferred from the platform name.
      if (c.user_handoff || c.publish_mode === "USER_HANDOFF") set.add(c.platform);
    }
    return set;
  }, [caps.data]);
  return { ids, loaded: caps.data !== null, error: caps.error };
}

/* ==========================================================================
 * Panels
 * ======================================================================= */

function MasterPanel({ master }: { master: ShortDto | null }) {
  if (!master) {
    return (
      <Panel title="Master" dense>
        <EmptyState
          title="No master on this campaign"
          description="A master is set when the campaign was built with POST /campaigns/from-master, or when the Lane A CampaignPlan row recorded one. Nothing is rendered in its place."
        />
      </Panel>
    );
  }
  return (
    <Panel title="Master" subtitle="Everything below is derived from this one item." dense>
      <Grid min={180} gap="sm">
        <StatTile label="Topic" value={master.topic || master.id.slice(0, 8)} source="aggregate.master" />
        <StatTile label="State" value={<StatusBadge status={master.status} />} source="aggregate.master" />
        <StatTile label="Derivation" value={master.derivation_type ? humanize(master.derivation_type) : "UNAVAILABLE"} unavailable={!master.derivation_type} source="aggregate.master" />
        <StatTile label="Root" value={master.root_content_id ? master.root_content_id.slice(0, 8) : "—"} hint={master.root_content_id ? undefined : "This item has no recorded root."} source="root_content_id" />
        <StatTile label="Parent" value={master.parent_content_id ? master.parent_content_id.slice(0, 8) : "—"} hint={master.parent_content_id ? undefined : "A master has no parent: it is the root of the tree."} source="parent_content_id" />
      </Grid>
    </Panel>
  );
}

function ShortsPanel({
  shorts,
  variants,
  onRegenerate,
}: {
  shorts: ShortDto[];
  variants: Map<string, VariantDto[]>;
  onRegenerate: (short: ShortDto) => void;
}) {
  const columns: Column<ShortDto>[] = [
    { key: "topic", header: "Short", cell: (s) => s.topic || s.id.slice(0, 8) },
    { key: "status", header: "Production state", cell: (s) => <StatusBadge status={s.status} /> },
    {
      key: "variants",
      header: "Platform variants",
      cell: (s) => {
        const list = variants.get(s.id) ?? [];
        if (list.length === 0) {
          return <span className="ym-muted">none derived</span>;
        }
        return (
          <span>
            {list.map((v) => {
              const mode = variantPublicationMode(v.status);
              return (
                <span key={v.id} style={{ marginRight: "var(--space-2)" }}>
                  {mode ? <ModeBadge mode={mode} /> : null} <Badge tone="info">{humanize(v.platform)}</Badge>{" "}
                  <StatusBadge status={v.status} />
                </span>
              );
            })}
          </span>
        );
      },
    },
    {
      key: "lineage",
      header: "Lineage",
      cell: (s) => (
        <span className="ym-notif-detail">
          {s.parent_content_id ? `parent ${s.parent_content_id.slice(0, 6)}` : "no parent"}
          {s.root_content_id ? ` · root ${s.root_content_id.slice(0, 6)}` : ""}
        </span>
      ),
      hideBelow: "lg",
    },
    {
      key: "regenerate",
      header: "Actions",
      cell: (s) => (
        <Button size="sm" onClick={() => onRegenerate(s)}>
          Regenerate
        </Button>
      ),
    },
  ];

  return (
    <Panel title="Shorts" subtitle="Derived children. A short is a ContentItem with derivation_type=short." dense>
      <DataTable
        rows={shorts}
        columns={columns}
        rowKey={(s) => s.id}
        caption="Derived shorts"
        maxHeight={420}
        empty="No short derived"
        emptyHint="POST /campaigns/{id}/derive enqueues the derive chain. Until it runs, this campaign has a plan and nothing to produce."
        onRowClick={onRegenerate}
      />
    </Panel>
  );
}

function VariantsPanel({
  variants,
  shortsById,
  onRegenerateVariant,
}: {
  variants: VariantDto[];
  shortsById: Map<string, ShortDto>;
  onRegenerateVariant: (variant: VariantDto) => void;
}) {
  return (
    <Panel title="Platform variants" subtitle="One cut per (short, platform). A WAITING_HANDOFF cut is prepared work, not a publication." dense>
      <DataTable
        rows={variants}
        rowKey={(v) => v.id}
        caption="Platform variants"
        maxHeight={420}
        empty="No platform variant"
        emptyHint="Variants are created by the derive chain or by POST /content/{id}/platform-variants. None exists for this campaign."
        columns={[
          { key: "short", header: "Short", cell: (v) => shortsById.get(v.short_content_id)?.topic ?? v.short_content_id.slice(0, 8) },
          { key: "platform", header: "Platform", cell: (v) => <Badge tone="info">{humanize(v.platform)}</Badge> },
          {
            key: "mode",
            header: "Publication mode",
            cell: (v) => {
              const mode = variantPublicationMode(v.status);
              return mode ? (
                <ModeBadge mode={mode} />
              ) : (
                <span className="ym-muted" title="Only AWAITING_HANDOFF declares a publication mode; other statuses are production state">
                  not declared
                </span>
              );
            },
          },
          { key: "status", header: "State", cell: (v) => <StatusBadge status={v.status} /> },
          { key: "aspect", header: "Aspect", cell: (v) => v.aspect_ratio || <span className="ym-muted">—</span>, hideBelow: "md" },
          {
            key: "timeline",
            header: "Timeline",
            cell: (v) => (v.shares_base_timeline ? <Badge tone="neutral">shares base</Badge> : v.timeline_id ? v.timeline_id.slice(0, 8) : <span className="ym-muted">—</span>),
            hideBelow: "lg",
          },
          {
            key: "actions",
            header: "Actions",
            cell: (v) => (
              <Button size="sm" onClick={() => onRegenerateVariant(v)}>
                Regenerate cut
              </Button>
            ),
          },
        ]}
      />
    </Panel>
  );
}

function PlanPanel({ plan, progress }: { plan: CampaignAggregate["plan"]; progress: Progress }) {
  const items = planItems(plan);
  const completed = progress.total > 0 ? Math.round((progress.completed / progress.total) * 100) : null;
  return (
    <Panel title="Publishing plan" subtitle="Master first: a short waits on the master so its CTA URL resolves." dense>
      <Grid min={170} gap="sm">
        <StatTile
          label="Plan status"
          value={plan.status ? humanize(plan.status) : "UNAVAILABLE"}
          unavailable={!plan.status}
          source="aggregate.plan.status"
        />
        <StatTile label="Completed" value={progress.completed} source="aggregate.progress.completed" />
        <StatTile label="Total" value={progress.total} source="aggregate.progress.total" />
        <StatTile
          label="Completion"
          value={completed === null ? "UNAVAILABLE" : `${completed}%`}
          unavailable={completed === null}
          hint={completed === null ? "The backend reported a total of 0, so no percentage is defined." : undefined}
          tone={completed !== null && completed >= 100 ? "success" : "info"}
          source="completed / total"
        />
      </Grid>
      <DataTable
        rows={items}
        rowKey={(i) => `${i.variant_id}-${i.platform}`}
        caption="Publishing plan items"
        maxHeight={420}
        empty="No publishing plan"
        emptyHint="POST /campaigns/{id}/schedule builds the plan. Until then nothing has a planned slot, which is why nothing can be published."
        columns={[
          { key: "item", header: "Item", cell: (i) => (i.is_master ? <Badge tone="info">MASTER</Badge> : i.variant_id.slice(0, 10)) },
          { key: "platform", header: "Platform", cell: (i) => humanize(i.platform) },
          { key: "planned", header: "Planned at", cell: (i) => isoMoment(i.planned_at) },
          { key: "priority", header: "Priority", align: "right", cell: (i) => i.priority },
          { key: "approval", header: "Approval", cell: (i) => <StatusBadge status={i.approval_state} /> },
          { key: "publication", header: "Publication", cell: (i) => <StatusBadge status={i.publication_state} /> },
          {
            key: "depends",
            header: "Depends on",
            cell: (i) => ((i.depends_on ?? []).length ? (i.depends_on ?? []).map((d) => d.slice(0, 8)).join(", ") : <span className="ym-muted">—</span>),
            hideBelow: "lg",
          },
        ]}
      />
    </Panel>
  );
}

function QcPanel({ qc }: { qc: QcSummary }) {
  return (
    <Panel title="Quality" subtitle="The best verdict per short, across every variant render." dense>
      <Grid min={170} gap="sm">
        <StatTile
          label="Average QC score"
          value={qc.avg_overall === null || qc.avg_overall === undefined ? undefined : qc.avg_overall.toFixed(1)}
          unavailable={qc.avg_overall === null || qc.avg_overall === undefined}
          tone={qc.avg_overall !== null && qc.avg_overall !== undefined && qc.avg_overall < 70 ? "warning" : "success"}
          hint="No scored short means no average — that is not a zero."
          source="aggregate.qc.avg_overall"
        />
        <StatTile
          label="Shorts scored"
          value={(qc.items ?? []).filter((i) => i.overall !== null).length}
          source="aggregate.qc.items"
        />
        <StatTile
          label="Shorts with no verdict"
          value={(qc.items ?? []).filter((i) => i.overall === null).length}
          tone={qc.items.some((i) => i.overall === null) ? "warning" : "neutral"}
          hint="A short with no QualityCheck row has not been reviewed, not failed."
          source="aggregate.qc.items"
        />
      </Grid>
      <DataTable
        rows={qc.items ?? []}
        rowKey={(i) => i.short_content_id}
        caption="QC verdicts per short"
        maxHeight={320}
        empty="No short to review"
        emptyHint="QC is scored per variant render, so a campaign with no derived short has nothing to review."
        columns={[
          { key: "short", header: "Short", cell: (i) => i.short_content_id.slice(0, 10) },
          {
            key: "overall",
            header: "Overall",
            align: "right",
            cell: (i) =>
              i.overall === null ? (
                <span className="ym-muted" title="No QualityCheck row exists for this short">
                  not reviewed
                </span>
              ) : (
                <Badge tone={i.passed === false ? "danger" : i.overall >= 70 ? "success" : "warning"}>
                  {i.overall.toFixed(0)}
                </Badge>
              ),
          },
          {
            key: "passed",
            header: "Passed",
            cell: (i) =>
              i.passed === null ? (
                <span className="ym-muted">UNAVAILABLE</span>
              ) : (
                <Badge tone={i.passed ? "success" : "danger"}>{i.passed ? "Yes" : "No"}</Badge>
              ),
          },
        ]}
      />
    </Panel>
  );
}

function PublicationsPanel({
  posts,
  handoffIds,
  handoffError,
}: {
  posts: ReturnType<typeof useWsQuery<PublishedPostList>>;
  handoffIds: Set<string>;
  handoffError: string | null;
}) {
  return (
    <Panel
      title="Publications"
      subtitle="Live / Mock / Handoff / not-reported are four different facts and are never merged."
      dense
    >
      <div className="ym-notif-item ym-notif-info">
        <div className="ym-notif-title">Where this panel's scope stops</div>
        <div className="ym-notif-detail">
          <code>GET /publishing/posts</code> carries no <code>campaign_id</code>,
          so these rows are the WORKSPACE's publications and none of them is
          attributed to this campaign. The campaign-scoped publication state is
          the Publishing plan above, which is what the backend actually tracks
          per variant.
        </div>
      </div>
      {handoffError ? (
        <p className="ym-error">
          The publisher registry could not be read ({handoffError}), so every
          row below is MODE UNAVAILABLE rather than guessed.
        </p>
      ) : null}
      <QueryBoundary query={posts} skeletonRows={4}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            rowKey={(p) => p.id}
            caption="Workspace publications"
            maxHeight={400}
            empty="No publication recorded"
            emptyHint="Nothing has been published in this workspace. A row is written only after a provider accepted the content or a handoff was prepared."
            columns={[
              { key: "published", header: "Published at", cell: (p) => (p.published_at ? p.published_at.replace("Z", " UTC").replace("T", " ").slice(0, 16) : <span className="ym-muted">—</span>) },
              { key: "platform", header: "Platform", cell: (p) => humanize(p.platform) },
              {
                key: "mode",
                header: "Publication mode",
                cell: (p) => <PublicationModeBadge mode={resolvePublicationMode(p, handoffIds.has(p.platform))} />,
              },
              { key: "title", header: "Title", cell: (p) => p.title || <span className="ym-muted">—</span>, hideBelow: "md" },
              {
                key: "views",
                header: "Views",
                align: "right",
                /* HONESTY (Work 16.5.7 §8): null ⇔ no PostMetric snapshot for
                 * this post. The backend used to send `0` here, which rendered as
                 * a real view count for a video nobody reported on. */
                cell: (p) =>
                  p.metrics.views === null ? (
                    <span className="ym-muted" title="No provider reported a figure for this post">
                      UNAVAILABLE
                    </span>
                  ) : (
                    p.metrics.views
                  ),
              },
              {
                key: "completion",
                header: "Completion",
                align: "right",
                cell: (p) =>
                  p.metrics.completion_rate === null || p.metrics.completion_rate === undefined ? (
                    <span className="ym-muted" title="The provider did not report a completion rate">
                      UNAVAILABLE
                    </span>
                  ) : (
                    `${(p.metrics.completion_rate * 100).toFixed(1)}%`
                  ),
                hideBelow: "lg",
              },
            ]}
          />
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Actions
 * ======================================================================= */

function ActionBar({
  agg,
  onDone,
}: {
  agg: CampaignAggregate;
  onDone: () => void;
}) {
  const [generateN, setGenerateN] = useState("1");
  const [interval, setIntervalDays] = useState("1");
  const [onlyApproved, setOnlyApproved] = useState(true);
  const [outcome, setOutcome] = useState<string | null>(null);

  const hasShorts = (agg.shorts ?? []).length > 0;
  const cancelled = (agg.campaign.status ?? "").toUpperCase() === "CANCELLED";

  const derive = useMutation<void, { job_id: string; status: string }>(
    () => wsApi.post(`/campaigns/${agg.campaign.id}/derive`) as Promise<{ job_id: string; status: string }>,
    { onSuccess: (r) => { setOutcome(`derive → ${r.status} (job ${r.job_id})`); onDone(); } },
  );

  const generateMore = useMutation<void, { created: string[] }>(
    () =>
      wsApi.post(`/campaigns/${agg.campaign.id}/generate-more`, {
        n: Number(generateN) || 1,
        exclude: [],
      }) as Promise<{ created: string[] }>,
    { onSuccess: (r) => { setOutcome(`generate-more → ${(r.created ?? []).length} short(s)`); onDone(); } },
  );

  const schedule = useMutation<void, { items: PublishingPlanItem[]; schedule: Record<string, unknown> }>(
    () =>
      wsApi.post(`/campaigns/${agg.campaign.id}/schedule`, {
        interval_days: Number(interval) || 1,
      }) as Promise<{ items: PublishingPlanItem[]; schedule: Record<string, unknown> }>,
    { onSuccess: (r) => { setOutcome(`schedule → ${r.items.length} plan item(s)`); onDone(); } },
  );

  const publish = useMutation<void, PublishResult>(
    () =>
      wsApi.post(`/campaigns/${agg.campaign.id}/publish`, {
        variant_ids: [],
        only_approved: onlyApproved,
      }) as Promise<PublishResult>,
    { onSuccess: (r) => {
        setOutcome(
          `publish → ${(r.enqueued ?? []).length} enqueued, ${(r.skipped ?? []).length} skipped`,
        );
        onDone();
      } },
  );

  const cancel = useMutation<void, { id: string; status: string }>(
    () => wsApi.post(`/campaigns/${agg.campaign.id}/cancel`) as Promise<{ id: string; status: string }>,
    { onSuccess: (r) => { setOutcome(`cancel → ${r.status}`); onDone(); } },
  );

  const running = derive.pending || generateMore.pending || schedule.pending || publish.pending || cancel.pending;
  const anyError =
    derive.error ?? generateMore.error ?? schedule.error ?? publish.error ?? cancel.error ?? null;

  return (
    <Panel
      title="Actions the backend allows"
      subtitle="Each control below is a real endpoint. There is no retry control, because none exists."
      dense
    >
      {cancelled ? (
        <div className="ym-notif-item ym-notif-warning">
          <div className="ym-notif-title">This campaign is CANCELLED</div>
          <div className="ym-notif-detail">
            Cancel is terminal on the campaign row. Deriving, scheduling and
            publishing are not offered.
          </div>
        </div>
      ) : (
        <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", alignItems: "flex-end" }}>
          <Button
            variant="primary"
            loading={derive.pending}
            disabled={running}
            onClick={() => void derive.run()}
          >
            Derive shorts
          </Button>
          <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-end" }}>
            <input
              className="ym-input"
              style={{ width: 90 }}
              type="number"
              min={1}
              max={10}
              aria-label="Shorts to generate"
              value={generateN}
              onChange={(e) => setGenerateN(e.target.value)}
            />
            <Button loading={generateMore.pending} disabled={running} onClick={() => void generateMore.run()}>
              Generate more shorts
            </Button>
          </div>
          <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-end" }}>
            <input
              className="ym-input"
              style={{ width: 110 }}
              type="number"
              min={0.25}
              max={30}
              step={0.25}
              aria-label="Interval in days"
              value={interval}
              onChange={(e) => setIntervalDays(e.target.value)}
            />
            <Button
              loading={schedule.pending}
              disabled={running || !hasShorts}
              title={hasShorts ? undefined : "The backend returns 409: no shorts to schedule — derive first."}
              onClick={() => void schedule.run()}
            >
              Build publishing plan
            </Button>
          </div>
          <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "flex-end" }}>
            <select
              className="ym-select"
              aria-label="Publish scope"
              value={onlyApproved ? "approved" : "all"}
              onChange={(e) => setOnlyApproved(e.target.value === "approved")}
            >
              <option value="approved">Approved only</option>
              <option value="all">Every queued variant</option>
            </select>
            <Button
              variant="danger"
              loading={publish.pending}
              disabled={running || !hasShorts}
              title={hasShorts ? undefined : "Nothing to publish — derive shorts first."}
              onClick={() => void publish.run()}
            >
              Publish
            </Button>
          </div>
          <DestructiveButton
            confirmLabel={`Cancel campaign ${agg.campaign.id.slice(0, 8)}? Its status becomes CANCELLED and the campaign plan is marked failed.`}
            onConfirm={() => void cancel.run()}
            disabled={running}
          >
            Cancel campaign
          </DestructiveButton>
        </div>
      )}
      {!hasShorts && !cancelled ? (
        <p className="ym-hint">
          Scheduling and publishing are unavailable: the backend returns 409
          “no shorts to schedule — derive first”. Derive is the only action
          available until a short exists.
        </p>
      ) : null}
      <div className="ym-notif-item ym-notif-unknown">
        <div className="ym-notif-title">No retry control, deliberately</div>
        <div className="ym-notif-detail">
          A failed publish is not retryable from this screen: the backend exposes
          no retry endpoint, and re-running publish on an already-queued variant
          answers “already queued”. A publish that may already have been billed
          must go through its own reconciliation, not a generic button.
        </div>
      </div>
      {outcome ? <p className="ym-hint">{outcome}</p> : null}
      {anyError ? <p className="ym-error">{anyError}</p> : null}
    </Panel>
  );
}

function RegenerateShortModal({
  short,
  onClose,
  onDone,
}: {
  short: ShortDto | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const regen = useMutation<void, { id: string; status: string; lineage_version: number }>(
    () =>
      wsApi.post(`/content/${short?.id ?? ""}/regenerate`) as Promise<{
        id: string;
        status: string;
        lineage_version: number;
      }>,
    { onSuccess: onDone },
  );
  if (!short) return null;
  return (
    <Modal
      open
      onClose={onClose}
      title="Regenerate this short"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Keep as is
          </Button>
          <Button variant="primary" loading={regen.pending} onClick={() => void regen.run()}>
            POST /content/&#123;id&#125;/regenerate
          </Button>
        </>
      }
    >
      <p className="ym-hint">{short.topic || short.id}</p>
      <div className="ym-notif-item ym-notif-warning">
        <div className="ym-notif-title">This resets the short to IDEA</div>
        <div className="ym-notif-detail">
          <code>regenerate</code> increments <code>lineage_version</code>, sets
          the status back to <strong>IDEA</strong> and clears the error. Work
          already produced under the previous version is not deleted — the
          lineage is what records that it existed.
        </div>
      </div>
      {regen.error ? <p className="ym-error">{regen.error}</p> : null}
    </Modal>
  );
}

function RegenerateVariantModal({
  variant,
  onClose,
  onDone,
}: {
  variant: VariantDto | null;
  onClose: () => void;
  onDone: () => void;
}) {
  const [platform, setPlatform] = useState<string>(variant?.platform ?? "");
  const [cta, setCta] = useState<string>("FOLLOW");

  const regen = useMutation<void, Record<string, unknown>>(
    () =>
      wsApi.post(`/content/${variant?.short_content_id ?? ""}/platform-variants`, {
        platform,
        cta_kind: cta,
      }) as Promise<Record<string, unknown>>,
    { onSuccess: onDone },
  );

  if (!variant) return null;
  return (
    <Modal
      open
      onClose={onClose}
      title="Regenerate this platform cut"
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button variant="primary" loading={regen.pending} disabled={!platform} onClick={() => void regen.run()}>
            POST /content/&#123;id&#125;/platform-variants
          </Button>
        </>
      }
    >
      <p className="ym-hint">
        Regenerates the <strong>{humanize(variant.platform)}</strong> cut of short{" "}
        {variant.short_content_id.slice(0, 8)}. If no variant exists for that
        pair yet, the backend creates one instead.
      </p>
      <Select label="Platform" value={platform} onChange={(e) => setPlatform(e.target.value)}>
        {CAMPAIGN_PLATFORMS.map((p) => (
          <option key={p} value={p}>
            {humanize(p)}
          </option>
        ))}
      </Select>
      <Select label="CTA kind" value={cta} onChange={(e) => setCta(e.target.value)}>
        {CTA_KINDS.map((c) => (
          <option key={c} value={c}>
            {humanize(c)}
          </option>
        ))}
      </Select>
      <p className="ym-hint">
        Per-platform metadata is generated from the topic and the CTA, so two
        platforms never receive identical bundles.
      </p>
      {regen.error ? <p className="ym-error">{regen.error}</p> : null}
    </Modal>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

type TabId = "tree" | "plan" | "quality" | "publications";

export function CampaignDetail({ campaignId }: { campaignId?: string }) {
  const params = useParams();
  const navigate = useNavigate();
  const { workspaceId } = useSession();
  const id = campaignId ?? params.campaignId ?? "";

  const [tab, setTab] = useState<TabId>("tree");
  const [regenShort, setRegenShort] = useState<ShortDto | null>(null);
  const [regenVariant, setRegenVariant] = useState<VariantDto | null>(null);

  const detail = useWsQuery<CampaignDetailRow>(id ? `/campaigns/${id}` : "/campaigns", {
    enabled: Boolean(id && workspaceId),
  });
  const agg = useWsQuery<CampaignAggregate>(id ? `/campaigns/${id}/aggregate` : "/campaigns", {
    enabled: Boolean(id && workspaceId),
  });
  const posts = useWsQuery<PublishedPostList>("/publishing/posts?limit=100");
  const handoff = useHandoffPlatforms();

  const variantMap = useMemo(() => variantsByShort(agg.data?.variants ?? []), [agg.data]);
  const shortsById = useMemo(() => {
    const map = new Map<string, ShortDto>();
    for (const s of agg.data?.shorts ?? []) map.set(s.id, s);
    if (agg.data?.master) map.set(agg.data.master.id, agg.data.master);
    return map;
  }, [agg.data]);

  if (!workspaceId || !id) {
    return (
      <>
        <PageHeader title="Campaign" description="Campaign → master → shorts → variants → publications." />
        <Panel title="No campaign selected">
          <EmptyState
            title="Nothing to show"
            description={
              !workspaceId
                ? "Campaigns are workspace-scoped. Select a workspace first."
                : "No campaign id was supplied, so no request was made."
            }
            action={<Button onClick={() => navigate("/campaigns")}>Back to campaigns</Button>}
          />
        </Panel>
      </>
    );
  }

  const reloadAll = () => {
    agg.reload();
    detail.reload();
  };

  const tree = agg.data;

  return (
    <>
      <PageHeader
        breadcrumb={<button className="ym-crumb" onClick={() => navigate("/campaigns")}>Campaigns</button>}
        title={tree?.campaign.name || detail.data?.name || "Campaign"}
        description={tree?.campaign.name ? undefined : "Loading the campaign…"}
        actions={<Button onClick={reloadAll}>Refresh</Button>}
      />

      <QueryBoundary query={agg} skeletonRows={8}>
        {(data) => (
          <>
            <Panel title="Overview" dense>
              <Grid min={170} gap="sm">
                <StatTile label="Status" value={<StatusBadge status={data.campaign.status} />} source="aggregate.campaign.status" />
                <StatTile
                  label="Master"
                  value={data.master ? data.master.topic.slice(0, 40) : "UNAVAILABLE"}
                  unavailable={!data.master}
                  hint={data.master ? undefined : "No master_content_id is recorded on this campaign."}
                  source="aggregate.master"
                />
                <StatTile label="Shorts" value={(data.shorts ?? []).length} source="aggregate.shorts" />
                <StatTile
            label="Platform variants"
            value={(data.variants ?? []).length}
            source="aggregate.variants"
          />
                <StatTile label="Progress" value={`${data.progress.completed} / ${data.progress.total}`} source="aggregate.progress" />
                <StatTile
                  label="Average QC"
                  value={data.qc.avg_overall === null || data.qc.avg_overall === undefined ? undefined : data.qc.avg_overall.toFixed(1)}
                  unavailable={data.qc.avg_overall === null || data.qc.avg_overall === undefined}
                  hint="null means no short has been reviewed. It is not 0."
                  source="aggregate.qc.avg_overall"
                />
                <StatTile
                  label="Cost entries"
                  value={<Money usd={data.costs.total_usd} />}
                  hint={`${data.costs.entries} CostEntry row(s) carry this campaign id.`}
                  source="aggregate.costs"
                />
                <StatTile
                  label="Published content items"
                  value={detail.data === null ? undefined : detail.data.progress.published}
                  unavailable={detail.data === null}
                  hint={
                    detail.data === null
                      ? "GET /campaigns/{id} failed, so this count is UNAVAILABLE."
                      : `of ${detail.data.progress.content_items} content item(s) on this campaign`
                  }
                  source="GET /campaigns/{id}"
                />
              </Grid>
              {detail.data?.progress.published && detail.data.progress.published > 0 ? (
                <div className="ym-notif-item ym-notif-warning">
                  <div className="ym-notif-title">Content items are marked PUBLISHED</div>
                  <div className="ym-notif-detail">
                    That is the CONTENT item's pipeline status, not a publication
                    mode. Whether a post reached a platform live, as a mock, or
                    as prepared work for a human is answered in the Publications
                    tab and nowhere else.
                  </div>
                </div>
              ) : null}
            </Panel>

            <ActionBar agg={data} onDone={reloadAll} />

            <Tabs
              tabs={[
                { id: "tree", label: "Hierarchy", count: (data.shorts ?? []).length },
                { id: "plan", label: "Publishing plan" },
                { id: "quality", label: "Quality", count: (data.qc?.items ?? []).length },
                { id: "publications", label: "Publications" },
              ]}
              active={tab}
              onChange={(t) => setTab(t as TabId)}
            />

            {tab === "tree" ? (
              <>
                <MasterPanel master={data.master} />
                <ShortsPanel shorts={data.shorts} variants={variantMap} onRegenerate={setRegenShort} />
                <VariantsPanel variants={data.variants} shortsById={shortsById} onRegenerateVariant={setRegenVariant} />
              </>
            ) : null}
            {tab === "plan" ? <PlanPanel plan={data.plan} progress={data.progress} /> : null}
            {tab === "quality" ? <QcPanel qc={data.qc} /> : null}
            {tab === "publications" ? (
              <PublicationsPanel posts={posts} handoffIds={handoff.ids} handoffError={handoff.error} />
            ) : null}
          </>
        )}
      </QueryBoundary>

      <RegenerateShortModal short={regenShort} onClose={() => setRegenShort(null)} onDone={() => { setRegenShort(null); reloadAll(); }} />
      <RegenerateVariantModal variant={regenVariant} onClose={() => setRegenVariant(null)} onDone={() => { setRegenVariant(null); reloadAll(); }} />
    </>
  );
}

export default CampaignDetail;
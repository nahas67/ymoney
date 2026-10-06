/* ProjectDetail — one project across its whole lifecycle.
 *
 * A "project" here is a `ContentItem`: the planner, the research, the script
 * variants, the render, the QC verdicts, the publishing jobs and the metrics all
 * hang off one id. This screen is a read model over four endpoints and nothing
 * else:
 *
 *   GET /content/{id}            the item, its strategy, research and variants
 *   GET /content/{id}/timeline   chronological events (pipeline + jobs + QC)
 *   GET /content/{id}/lineage    ancestors / children (derivation versions)
 *   GET /content/{id}/audit      the evidence bundle: WHAT / WHY / render / publish
 *
 * plus `GET /calendar` filtered to this item so the Publishing tab can show
 * where the post is placed.
 *
 * THREE RULES
 *
 * 1. NO INVENTED STATE. Every field below is read from a response. A tab whose
 *    data this read model cannot reach (exports, per-project review threads)
 *    says so and points at the surface that owns it — it never renders a zero,
 *    a placeholder count, or a "no issues" that nobody verified.
 *
 * 2. OVERVIEW ANSWERS FIRST. Current stage, blockers and next actions are above
 *    the fold, derived from the item's own status, its video row, the QC verdicts
 *    and the publishing jobs. Nothing there is a guess.
 *
 * 3. NO GENERIC PAID RETRY. Nothing on this screen mutates a paid action. A
 *    resubmit that may already have been billed is refused by the backend for a
 *    reason, and a button that ignores that reason is the bug this project
 *    exists to prevent. Guidance points at the surface that owns the action.
 *
 * `contentId` is accepted as a prop and falls back to the route parameter, so
 * the screen works both when mounted by the router (`/projects/:contentId`) and
 * when handed an id directly.
 */

import { useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Grid,
  Modal,
  PageHeader,
  Panel,
  QueryBoundary,
  StatTile,
  StatusBadge,
  Tabs,
  humanize,
  toneForStatus,
  type Column,
  type Tone,
} from "../../design-system/primitives";
import { useCombinedQueries, type QueryState } from "../../api/queries";
import { videoFileUrl, videoThumbUrl, wsApi } from "../../lib/api";
import { useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes — app/api/v1/content.py
 * ======================================================================= */

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
type VariantRow = {
  id: string;
  label: string;
  hook: string;
  script: string;
  predicted_score: number | null;
  selected: boolean;
  metadata: Record<string, unknown>;
};
type ContentDetail = {
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
  research: Record<string, unknown>;
  tags: string[];
  variants: VariantRow[];
};

type TimelineItem = { at: string; kind: string; label: string; detail: string };
type Timeline = { items: TimelineItem[] };

type LineageNode = {
  id: string;
  topic: string;
  status: string;
  derivation_type: string | null;
  lineage_version: number | null;
  campaign_id: string | null;
};
type Lineage = { self: LineageNode; root_id: string; ancestors: LineageNode[]; children: LineageNode[] };

type AuditQualityCheck = {
  overall: number;
  passed: boolean;
  notes: string;
  components: Record<string, unknown>;
  created_at: string;
};
type AuditVideo = {
  id: string;
  engine: string;
  status: string;
  aspect_ratio: string | null;
  resolution: string | null;
  duration_seconds: number | null;
  params: Record<string, unknown>;
  error: string | null;
};
type AuditPublishingJob = {
  platform: string;
  status: string;
  attempt: number;
  remote_post_id: string | null;
  remote_url: string | null;
  error: string;
  compliance: unknown;
};
type AuditPublishedPost = {
  platform: string;
  remote_post_id: string | null;
  remote_url: string | null;
  title: string;
  is_mock: boolean;
  metrics: {
    views: number;
    likes: number;
    comments: number;
    completion_rate: number | null;
    captured_at: string | null;
  } | null;
};
type AuditEvent = { at: string; kind: string; level: string; message: string };
type Audit = {
  content: { id: string; topic: string; status: string; error: string | null; created_at: string };
  decision_why: string | null;
  strategy: Record<string, unknown>;
  research: Record<string, unknown>;
  variants: VariantRow[];
  videos: AuditVideo[];
  quality_checks: AuditQualityCheck[];
  publishing_jobs: AuditPublishingJob[];
  published_posts: AuditPublishedPost[];
  event_trail: AuditEvent[];
};

type ScheduleRow = {
  id: string;
  platform: string;
  run_at: string;
  content_item_id: string;
  campaign_id: string | null;
  status: string;
};
type ScheduleList = { items: ScheduleRow[] };

type Panels = {
  detail: ContentDetail;
  timeline: Timeline;
  lineage: Lineage;
  audit: Audit;
  schedule: ScheduleList;
};

/* ==========================================================================
 * Helpers
 * ======================================================================= */

/** The backend's own stage order (models/base.py `ContentStatus`). */
const PIPELINE = [
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
];

function utc(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("Z", " UTC").replace("T", " ").slice(0, 19);
}

function pct(value: number | null | undefined): string {
  if (value === null || value === undefined || Number.isNaN(value)) return "—";
  return `${(value * 100).toFixed(1)}%`;
}

/** Read a string list out of a free-form research/strategy field. */
function stringList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.filter((v): v is string => typeof v === "string");
}

/** Read a number that the backend may not have reported. */
function numberOrNull(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/**
 * Length of an optional list, for a tab badge.
 *
 * `data?.items.length` throws when the payload is present but the list is not,
 * which turns a malformed read into a blank screen. A badge is decoration: it
 * reports a count or it shows nothing.
 */
function count(value: number | undefined | null): number | undefined {
  return typeof value === "number" && value > 0 ? value : undefined;
}

/** Read a string the backend may not have reported. */
function text(value: unknown): string {
  return typeof value === "string" && value.trim() ? value : "";
}

/** A short, readable preview of an object the read model does not model. */
function preview(value: unknown, limit = 320): string {
  if (value === null || value === undefined) return "—";
  try {
    const s = JSON.stringify(value);
    return s.length > limit ? `${s.slice(0, limit)}…` : s;
  } catch {
    return String(value);
  }
}

function panelState<T>(
  value: T | null,
  error: string | undefined,
  settled: boolean,
  reload: () => void,
): QueryState<T> {
  return {
    data: value,
    error: error ?? null,
    /* Aggregated panels report a pre-stringified error, so no status is in scope.
     * `null` falls back to the wording classifier, which covers every denial
     * detail the backend actually emits (`scripts/denial_vocabulary.py`). */
    errorStatus: null,
    loading: !settled && !error,
    settled,
    refreshing: false,
    reload,
    setData: () => undefined,
  };
}

/** Never settles. Used when there is nothing to ask the API yet. */
function idle<T>(): Promise<T> {
  return new Promise<T>(() => undefined);
}

type Blocker = { key: string; tone: Tone; what: string; detail: string };

/** Blockers, read from the fields that record them. Never invented. */
function collectBlockers(detail: ContentDetail | null, audit: Audit | null): Blocker[] {
  const out: Blocker[] = [];
  if (!detail) return out;

  if (detail.status.toUpperCase() === "FAILED") {
    out.push({
      key: "content-failed",
      tone: "danger",
      what: "Pipeline stopped on this item",
      detail: detail.error || "The content row is FAILED and carries no error text.",
    });
  } else if (detail.error) {
    out.push({
      key: "content-error",
      tone: "danger",
      what: "Error recorded on the content item",
      detail: detail.error,
    });
  }

  const video = detail.video;
  if (video) {
    const vs = video.status.toUpperCase();
    if (vs === "FAILED" || vs === "DEAD" || vs === "CANCELLED") {
      out.push({
        key: `video-${vs}`,
        tone: "danger",
        what: `Render ${humanize(vs)}`,
        detail: video.error || "The video row carries no error text.",
      });
    }
    if (video.quality_passed === false) {
      out.push({
        key: "qc-rejected",
        tone: "danger",
        what: `Quality check rejected (${video.quality ?? 0}/100)`,
        detail: video.quality_notes || "The QC row carries no note.",
      });
    }
  }

  for (const check of audit?.quality_checks ?? []) {
    if (!check.passed) {
      out.push({
        key: `qc-${check.created_at}`,
        tone: "danger",
        what: `QC ${check.overall}/100 rejected at ${utc(check.created_at)}`,
        detail: check.notes || "No note on this verdict.",
      });
    }
  }

  for (const job of audit?.publishing_jobs ?? []) {
    const state = job.status.toUpperCase();
    if (state.includes("SUBMISSION_UNKNOWN") || state.includes("UNKNOWN")) {
      out.push({
        key: `pub-${job.platform}-${state}`,
        tone: toneForStatus(job.status),
        what: `Submission outcome unknown on ${humanize(job.platform)}`,
        detail:
          job.error ||
          "The provider may already have billed this submission. Reconcile before any resubmit.",
      });
    } else if (state.includes("FAILED") || state.includes("ERROR")) {
      out.push({
        key: `pub-${job.platform}-failed`,
        tone: "danger",
        what: `Publishing failed on ${humanize(job.platform)} (attempt ${job.attempt})`,
        detail: job.error || "The publishing job carries no error text.",
      });
    }
  }

  return out;
}

type NextAction = { what: string; where: string; tone: Tone };

/** Next actions, described from observed state and the surface that owns them. */
function nextActions(
  detail: ContentDetail | null,
  schedule: ScheduleRow[] | null,
  audit: Audit | null,
): NextAction[] {
  if (!detail) return [];
  const status = detail.status.toUpperCase();
  const entries = (schedule ?? []).filter((e) => e.content_item_id === detail.id);
  const failedEntries = entries.filter((e) => e.status.toUpperCase() === "FAILED");
  const pendingEntries = entries.filter((e) => e.status.toUpperCase() === "PENDING");
  const published = audit?.published_posts ?? [];

  switch (status) {
    case "IDEA":
    case "RESEARCHING":
    case "STRATEGY":
    case "SCRIPTING":
      return [
        {
          what: "The supervisor is building this project",
          where: "Watch the Timeline tab; the stage advances on its own.",
          tone: "info",
        },
      ];
    case "SCRIPT_READY":
      return [
        {
          what: detail.variants.some((v) => v.selected)
            ? "A script variant is selected and awaiting a render"
            : "No script variant is selected yet",
          where: "Compare variants in the Script tab; the Hook Optimizer picks the production variant.",
          tone: "info",
        },
      ];
    case "PRODUCTION":
      return [
        {
          what:
            detail.video?.status.toUpperCase() === "RENDERING"
              ? `Render in flight via ${detail.video.engine} (${detail.video.progress}%)`
              : "A render is expected for this item",
          where: "Progress and engine are in Overview.",
          tone: "info",
        },
      ];
    case "QC":
      return [
        {
          what:
            detail.video?.quality_passed === false
              ? "The quality check rejected this render"
              : "Quality check pending",
          where: "Verdicts and component scores are in the Reviews tab.",
          tone: detail.video?.quality_passed === false ? "danger" : "info",
        },
      ];
    case "APPROVED":
      return [
        {
          what: "Approved and ready to place",
          where: "Place it on the calendar; this screen does not schedule work.",
          tone: "success",
        },
      ];
    case "SCHEDULED":
      return pendingEntries.length > 0
        ? [
            {
              what: `Scheduled for ${utc(pendingEntries[0].run_at)} on ${humanize(pendingEntries[0].platform)}`,
              where: "The Publishing tab lists every entry for this item.",
              tone: "info",
            },
          ]
        : [
            {
              what: "Status says SCHEDULED but no pending calendar entry was returned",
              where: "Check the calendar — a dispatch may already have moved this entry.",
              tone: "warning",
            },
          ];
    case "PUBLISHED":
    case "ANALYZING":
    case "LEARNED": {
      const live = published.find((p) => !p.is_mock);
      return [
        {
          what: live?.remote_url ? `Published on ${humanize(live.platform)}` : "Published",
          where:
            published.length > 0
              ? "Metrics land in the Performance tab once a snapshot exists."
              : "No post row was returned by the audit bundle yet.",
          tone: "success",
        },
      ];
    }
    case "FAILED":
      return [
        {
          what: "This item failed",
          where:
            "Read the Activity trail for the last error. A resubmit is a paid action: if the submission state is " +
            "SUBMISSION_UNKNOWN the backend refuses it on purpose — reconcile in Command Center → Paid incidents.",
          tone: "danger",
        },
      ];
    case "SKIPPED":
      return [{ what: "Skipped", where: "An operator skipped this item.", tone: "neutral" }];
    default:
      return [{ what: `Stage: ${humanize(status)}`, where: "No transition is recorded for this state.", tone: "neutral" }];
  }
}

/* ==========================================================================
 * Overview
 * ======================================================================= */

function OverviewTab({
  detail,
  audit,
  schedule,
}: {
  detail: QueryState<ContentDetail>;
  audit: QueryState<Audit>;
  schedule: QueryState<ScheduleList>;
}) {
  const [scriptOpen, setScriptOpen] = useState(false);
  const d = detail.data;
  const a = audit.data;
  const blockers = collectBlockers(d, a);
  const actions = nextActions(d, schedule.data?.items ?? null, a);
  const currentIndex = d ? PIPELINE.indexOf(d.status.toUpperCase()) : -1;
  const storyboard = (d?.strategy?.storyboard ?? {}) as Record<string, unknown>;
  const scenes = Array.isArray(storyboard.scenes) ? (storyboard.scenes as unknown[]) : [];
  const factConfidence = numberOrNull(d?.research?.factual_confidence);
  const factStatus = text(d?.research?.fact_status);

  return (
    <Grid min={340} gap="md">
      <Panel title="Current stage" dense>
        <QueryBoundary query={detail} skeletonRows={3}>
          {(item) => (
            <>
              <div style={{ display: "flex", gap: "var(--space-2)", flexWrap: "wrap", alignItems: "center" }}>
                <StatusBadge status={item.status} />
                {item.video ? <StatusBadge status={item.video.status} /> : null}
                {item.video?.quality_passed === false ? <Badge tone="danger">QC rejected</Badge> : null}
                {item.video?.quality_passed === true ? <Badge tone="success">QC passed</Badge> : null}
              </div>
              <div style={{ display: "flex", gap: "var(--space-1)", flexWrap: "wrap", marginTop: "var(--space-3)" }}>
                {PIPELINE.map((stage, i) => {
                  const reached = currentIndex >= 0 && i <= currentIndex;
                  const current = i === currentIndex;
                  if (current) return <Badge key={stage} tone="info" dot>{humanize(stage)}</Badge>;
                  if (reached) return <Badge key={stage} tone="neutral">{humanize(stage)}</Badge>;
                  return (
                    <span key={stage} className="ym-muted" style={{ fontSize: "var(--text-xs)" }}>
                      {humanize(stage)}
                    </span>
                  );
                })}
              </div>
              <Grid min={150} gap="sm">
                <StatTile label="Script variants" value={item.variants_count} source="GET /content/{id}" />
                <StatTile
                  label="Render quality"
                  unavailable={item.video?.quality === null || item.video?.quality === undefined}
                  value={item.video?.quality ?? undefined}
                  tone={item.video?.quality_passed === false ? "danger" : item.video?.quality_passed === true ? "success" : "neutral"}
                  hint={item.video?.quality === null || item.video?.quality === undefined ? "No QC verdict on this render" : undefined}
                  source="GET /content/{id}"
                />
                <StatTile
                  label="Factual confidence"
                  unavailable={factConfidence === null}
                  value={factConfidence === null ? undefined : factConfidence.toFixed(2)}
                  tone={factStatus === "CONFLICTING" ? "danger" : factStatus === "INSUFFICIENT" ? "warning" : "neutral"}
                  hint={factStatus ? humanize(factStatus) : "No aggregate confidence reported"}
                  source="research.factual_confidence"
                />
                <StatTile
                  label="Storyboard scenes"
                  unavailable={scenes.length === 0}
                  value={scenes.length ? scenes.length : undefined}
                  hint={scenes.length ? undefined : "Strategy carries no storyboard.scenes list"}
                  source="strategy.storyboard.scenes"
                />
              </Grid>
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Blockers" subtitle="Every entry below is a field the backend actually recorded." dense>
        <QueryBoundary query={detail} skeletonRows={2}>
          {() =>
            blockers.length === 0 ? (
              <EmptyState
                title="No blocker recorded"
                description="No error text, no failed render, no rejected QC verdict and no failed publishing job."
              />
            ) : (
              <div className="ym-notif-wrap">
                {blockers.map((b) => (
                  <div key={b.key} className="ym-notif-item">
                    <Badge tone={b.tone} dot>
                      {humanize(b.tone)}
                    </Badge>
                    <div>
                      <div className="ym-notif-title">{b.what}</div>
                      <div className="ym-notif-detail">{b.detail}</div>
                    </div>
                  </div>
                ))}
              </div>
            )
          }
        </QueryBoundary>
      </Panel>

      <Panel title="Next actions" subtitle="Where the action lives — this screen never fires a paid mutation." dense>
        <QueryBoundary query={detail} skeletonRows={2}>
          {() => (
            <DataTable
              rows={actions}
              columns={[
                { key: "what", header: "Action", cell: (r) => r.what },
                { key: "where", header: "Where", cell: (r) => r.where, hideBelow: "md" },
              ]}
              rowKey={(r) => r.what}
              caption="Next actions"
              empty="No action recorded for this state"
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Identity & decisions" dense>
        <QueryBoundary query={detail} skeletonRows={3}>
          {(item) => (
            <DataTable
              rows={[
                { field: "Content id", value: item.id },
                { field: "Campaign", value: item.campaign_id ?? "not in a campaign" },
                { field: "Cycle", value: item.cycle_id ?? "no cycle" },
                { field: "Created", value: utc(item.created_at) },
                { field: "Tags", value: item.tags.length ? item.tags.join(", ") : "none" },
                { field: "Engine", value: item.video?.engine || "no render yet" },
                { field: "Aspect ratio", value: item.video?.aspect_ratio || "no render yet" },
                { field: "Duration", value: item.video?.duration_seconds ? `${item.video.duration_seconds}s` : "no render yet" },
              ]}
              columns={[
                { key: "field", header: "Field", cell: (r) => r.field },
                { key: "value", header: "Value", cell: (r) => r.value },
              ]}
              rowKey={(r) => r.field}
              caption="Identity"
              empty="No field"
            />
          )}
        </QueryBoundary>
        <QueryBoundary query={audit} skeletonRows={1}>
          {(a) => (
            <p className="ym-hint">
              <strong>Why this was selected:</strong> {a.decision_why || "No decision rationale recorded on the cycle."}
            </p>
          )}
        </QueryBoundary>
        {d?.video?.file_path ? (
          <Button
            variant="secondary"
            onClick={() => setScriptOpen(true)}
          >
            Open preview
          </Button>
        ) : null}
        <Modal open={scriptOpen} onClose={() => setScriptOpen(false)} title="Render preview" width={720}>
          {d?.video ? (
            <video
              controls
              preload="metadata"
              style={{ width: "100%" }}
              src={videoFileUrl(d.video.id)}
              data-testid="project-video"
            />
          ) : null}
        </Modal>
      </Panel>
    </Grid>
  );
}

/* ==========================================================================
 * Research
 * ======================================================================= */

type ClaimRow = { claim: string; status: string; confidence: number | null; basis: string };

function ResearchTab({ detail }: { detail: QueryState<ContentDetail> }) {
  return (
    <QueryBoundary query={detail} skeletonRows={5}>
      {(item) => {
        const r = item.research;
        const claims: ClaimRow[] = Array.isArray(r.claims)
          ? (r.claims as Record<string, unknown>[]).map((c) => ({
              claim: text(c.claim),
              status: text(c.status) || "UNCERTAIN",
              confidence: numberOrNull(c.confidence),
              basis: text(c.basis),
            }))
          : [];
        const keyFacts = stringList(r.key_facts);
        const angles = stringList(r.angles);
        const visuals = stringList(r.visual_keywords);
        const cautions = stringList(r.cautions);
        const summary = text(r.summary);
        const factConfidence = numberOrNull(r.factual_confidence);
        const factStatus = text(r.fact_status);
        const modelled = new Set([
          "summary",
          "key_facts",
          "angles",
          "visual_keywords",
          "cautions",
          "claims",
          "factual_confidence",
          "fact_status",
          "memory",
        ]);
        const extra = Object.entries(r).filter(([k]) => !modelled.has(k));

        return (
          <Grid min={340} gap="md">
            <Panel title="Research brief" dense>
              <Grid min={160} gap="sm">
                <StatTile
                  label="Factual confidence"
                  unavailable={factConfidence === null}
                  value={factConfidence === null ? undefined : factConfidence.toFixed(2)}
                  tone={factStatus === "CONFLICTING" ? "danger" : factStatus === "INSUFFICIENT" ? "warning" : "success"}
                  hint={factStatus ? humanize(factStatus) : "Not reported"}
                />
                <StatTile label="Claims tracked" value={claims.length} hint="VERIFIED / LIKELY / UNCERTAIN / CONFLICTING" />
                <StatTile
                  label="Unverifiable claims"
                  value={claims.length ? claims.filter((c) => c.status === "UNCERTAIN" || c.status === "CONFLICTING").length : 0}
                  tone={claims.some((c) => c.status === "CONFLICTING") ? "danger" : "neutral"}
                />
                <StatTile label="Key facts" value={keyFacts.length} />
              </Grid>
              {summary ? <p style={{ marginTop: "var(--space-3)" }}>{summary}</p> : (
                <EmptyState title="No summary recorded" description="This item has no research brief attached." />
              )}
              {extra.length > 0 && (
                <div style={{ marginTop: "var(--space-3)" }}>
                  <p className="ym-hint">
                    Additional research fields (shown verbatim, not interpreted):{" "}
                    {extra.map(([k, v]) => `${k}=${preview(v, 120)}`).join(" · ")}
                  </p>
                </div>
              )}
            </Panel>

            <Panel title="Claims" dense>
              <DataTable
                rows={claims}
                columns={[
                  { key: "claim", header: "Claim", cell: (c) => c.claim },
                  { key: "status", header: "Status", cell: (c) => <StatusBadge status={c.status} /> },
                  { key: "confidence", header: "Confidence", align: "right", cell: (c) => pct(c.confidence) },
                  { key: "basis", header: "Basis", cell: (c) => c.basis || "—", hideBelow: "lg" },
                ]}
                rowKey={(c, i) => `${c.claim.slice(0, 24)}-${i}`}
                caption="Research claims"
                empty="No claim tracked"
                emptyHint="A claim with no source is recorded as UNCERTAIN, never as verified."
              />
            </Panel>

            <Panel title="Key facts" dense>
              {keyFacts.length === 0 ? (
                <EmptyState title="No key facts" description="The research brief carries no fact list." />
              ) : (
                <ul>
                  {keyFacts.map((f, i) => (
                    <li key={i}>{f}</li>
                  ))}
                </ul>
              )}
            </Panel>

            <Panel title="Angles, visuals & cautions" dense>
              <Grid min={160} gap="md">
                <div>
                  <p className="ym-label">Angles</p>
                  {angles.length ? <ul>{angles.map((a, i) => <li key={i}>{a}</li>)}</ul> : <p className="ym-muted">none</p>}
                </div>
                <div>
                  <p className="ym-label">Visual keywords</p>
                  {visuals.length ? (
                    <div style={{ display: "flex", gap: "var(--space-1)", flexWrap: "wrap" }}>
                      {visuals.map((v) => (
                        <Badge key={v} tone="info">{v}</Badge>
                      ))}
                    </div>
                  ) : (
                    <p className="ym-muted">none</p>
                  )}
                </div>
                <div>
                  <p className="ym-label">Cautions</p>
                  {cautions.length ? <ul>{cautions.map((c, i) => <li key={i}>{c}</li>)}</ul> : <p className="ym-muted">none</p>}
                </div>
              </Grid>
            </Panel>
          </Grid>
        );
      }}
    </QueryBoundary>
  );
}

/* ==========================================================================
 * Script
 * ======================================================================= */

function ScriptTab({ detail }: { detail: QueryState<ContentDetail> }) {
  const [open, setOpen] = useState<string | null>(null);
  const columns: Column<VariantRow>[] = [
    { key: "label", header: "Variant", cell: (v) => (v.selected ? <Badge tone="success">{v.label}</Badge> : v.label) },
    { key: "hook", header: "Hook", cell: (v) => v.hook || "—" },
    {
      key: "score",
      header: "Predicted",
      align: "right",
      cell: (v) => (v.predicted_score === null ? "—" : v.predicted_score.toFixed(1)),
    },
    {
      key: "open",
      header: "Script",
      cell: (v) => (
        <Button size="sm" variant="ghost" onClick={() => setOpen(v.id)}>
          Read
        </Button>
      ),
    },
  ];
  return (
    <>
      <Panel title="Script variants" subtitle="The selected variant is the one the Hook Optimizer chose for production." dense>
        <QueryBoundary query={detail} skeletonRows={4}>
          {(item) => (
            <DataTable
              rows={item.variants}
              columns={columns}
              rowKey={(v) => v.id}
              caption="Script variants"
              empty="No script variant"
              emptyHint="Variants appear once the Script Agent has written them."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Strategy" subtitle="The Content Strategist's record. Keys the read model does not model are shown verbatim." dense>
        <QueryBoundary query={detail} skeletonRows={3}>
          {(item) => (
            <DataTable
              rows={Object.entries(item.strategy).map(([key, value]) => ({ key, value: preview(value) }))}
              columns={[
                { key: "key", header: "Field", cell: (r) => humanize(r.key) },
                { key: "value", header: "Value", cell: (r) => r.value },
              ]}
              rowKey={(r) => r.key}
              caption="Strategy record"
              empty="No strategy recorded"
              emptyHint="The strategist has not run for this item."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Modal open={open !== null} onClose={() => setOpen(null)} title="Script" width={760}>
        {detail.data?.variants
          .filter((v) => v.id === open)
          .map((v) => (
            <div key={v.id}>
              <p className="ym-label">{v.label}</p>
              <p className="ym-hint">Hook: {v.hook}</p>
              <pre style={{ whiteSpace: "pre-wrap" }}>{v.script || "No script text stored on this variant."}</pre>
            </div>
          ))}
      </Modal>
    </>
  );
}

/* ==========================================================================
 * Scenes
 * ======================================================================= */

type SceneRow = { index: number | null; label: string; detail: string };

function ScenesTab({ detail, timeline }: { detail: QueryState<ContentDetail>; timeline: QueryState<Timeline> }) {
  return (
    <QueryBoundary query={detail} skeletonRows={4}>
      {(item) => {
        const storyboard = (item.strategy.storyboard ?? {}) as Record<string, unknown>;
        const raw: unknown[] = Array.isArray(storyboard.scenes) ? (storyboard.scenes as unknown[]) : [];
        const rows: SceneRow[] = raw.map((scene, i) => {
          const s = (scene ?? {}) as Record<string, unknown>;
          const label =
            text(s.prompt) || text(s.query) || text(s.description) || text(s.visual) || text(s.on_screen_text) || `Scene ${i + 1}`;
          return {
            index: numberOrNull(s.index) ?? i + 1,
            label,
            detail: preview(scene, 400),
          };
        });
        const sceneCount = numberOrNull(storyboard.scene_count);

        return (
          <Grid min={340} gap="md">
            <Panel title="Storyboard" subtitle="Scenes come from strategy.storyboard; the timeline editor holds the real cut." dense>
              <Grid min={150} gap="sm">
                <StatTile
                  label="Scenes in storyboard"
                  unavailable={rows.length === 0}
                  value={rows.length ? rows.length : undefined}
                  hint={rows.length ? undefined : "strategy.storyboard.scenes is empty"}
                  source="strategy.storyboard.scenes"
                />
                <StatTile
                  label="Declared scene count"
                  unavailable={sceneCount === null}
                  value={sceneCount ?? undefined}
                  source="strategy.storyboard.scene_count"
                />
              </Grid>
              <DataTable
                rows={rows}
                columns={[
                  { key: "index", header: "#", align: "right", cell: (r) => r.index },
                  { key: "label", header: "Scene", cell: (r) => r.label },
                  { key: "detail", header: "Record", cell: (r) => r.detail, hideBelow: "lg" },
                ]}
                rowKey={(r) => `${r.index}-${r.label}`}
                caption="Storyboard scenes"
                empty="No storyboard scene"
                emptyHint="The Strategist records no storyboard for this item; the edit surface is Studio."
              />
            </Panel>

            <Panel title="Scene events" subtitle="Timeline events the backend filed for this item." dense>
              <QueryBoundary query={timeline} skeletonRows={3}>
                {(t) => {
                  const sceneEvents = t.items.filter((i) => i.kind.toLowerCase().includes("scene"));
                  return (
                    <TimelineRows
                      rows={(sceneEvents.length ? sceneEvents : t.items).map((i) => ({
                        kind: i.kind,
                        label: i.label,
                        at: i.at,
                        detail: i.detail,
                      }))}
                    />
                  );
                }}
              </QueryBoundary>
            </Panel>
          </Grid>
        );
      }}
    </QueryBoundary>
  );
}

/** Timeline rows, filtered to a kind when one is given. */
function TimelineRows({ rows }: { rows: { kind: string; label: string; at: string; detail: string }[] }) {
  return (
    <DataTable
      rows={rows}
      columns={[
        { key: "at", header: "When (UTC)", cell: (r) => utc(r.at) },
        { key: "kind", header: "Kind", cell: (r) => humanize(r.kind) },
        { key: "label", header: "Event", cell: (r) => r.label },
        { key: "detail", header: "Detail", cell: (r) => r.detail || "—", hideBelow: "lg" },
      ]}
      rowKey={(r, i) => `${r.at}-${i}`}
      caption="Events"
      empty="No event recorded"
    />
  );
}

/* ==========================================================================
 * Timeline
 * ======================================================================= */

function TimelineTab({ timeline }: { timeline: QueryState<Timeline> }) {
  return (
    <Panel title="Timeline" subtitle="Pipeline events, agent jobs, renders and quality checks in one order." dense>
      <QueryBoundary query={timeline} skeletonRows={6}>
        {(t) => <TimelineRows rows={t.items} />}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Assets
 * ======================================================================= */

function AssetsTab({ detail, audit }: { detail: QueryState<ContentDetail>; audit: QueryState<Audit> }) {
  return (
    <Grid min={340} gap="md">
      <Panel title="Render artifact" dense>
        <QueryBoundary query={detail} skeletonRows={3}>
          {(item) =>
            item.video && item.video.file_path ? (
              <>
                <img
                  src={videoThumbUrl(item.video.id)}
                  alt={`Thumbnail for ${item.topic}`}
                  style={{ maxWidth: "100%", borderRadius: "var(--radius-default)" }}
                />
                <DataTable
                  rows={[
                    { field: "Engine", value: item.video.engine },
                    { field: "Status", value: item.video.status },
                    { field: "Aspect ratio", value: item.video.aspect_ratio ?? "not reported" },
                    { field: "Duration", value: item.video.duration_seconds ? `${item.video.duration_seconds}s` : "not reported" },
                    { field: "Stored at", value: item.video.file_path },
                  ]}
                  columns={[
                    { key: "field", header: "Field", cell: (r) => r.field },
                    { key: "value", header: "Value", cell: (r) => r.value },
                  ]}
                  rowKey={(r) => r.field}
                  caption="Render artifact"
                  empty="No render artifact"
                />
              </>
            ) : (
              <EmptyState
                title="No render artifact yet"
                description="A video row appears once the Video Producer submits the render."
              />
            )
          }
        </QueryBoundary>
      </Panel>

      <Panel title="Renders on record" dense>
        <QueryBoundary query={audit} skeletonRows={3}>
          {(a) => (
            <DataTable
              rows={a.videos.map((v) => ({ id: v.id, video: v }))}
              columns={[
                { key: "status", header: "Status", cell: (r) => <StatusBadge status={r.video.status} /> },
                { key: "engine", header: "Engine", cell: (r) => r.video.engine || "—" },
                { key: "resolution", header: "Resolution", cell: (r) => r.video.resolution || "—", hideBelow: "md" },
                { key: "error", header: "Error", cell: (r) => r.video.error || "—", hideBelow: "lg" },
              ]}
              rowKey={(r) => r.id}
              caption="Renders"
              empty="No render on record"
              emptyHint="The audit bundle carries one row per render of this item."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Asset library" dense>
        <StatTile
          label="Media assets linked to this project"
          unavailable
          hint="The per-item asset list is not in this read model; the workspace asset library is GET /assets."
          source="GET /assets"
        />
      </Panel>
    </Grid>
  );
}

/* ==========================================================================
 * Versions
 * ======================================================================= */

function VersionsTab({ detail, lineage, audit }: { detail: QueryState<ContentDetail>; lineage: QueryState<Lineage>; audit: QueryState<Audit> }) {
  const lineageColumns: Column<LineageNode>[] = [
    { key: "topic", header: "Item", cell: (n) => n.topic || n.id.slice(0, 8) },
    { key: "status", header: "Status", cell: (n) => <StatusBadge status={n.status} /> },
    { key: "derivation", header: "Derivation", cell: (n) => humanize(n.derivation_type) },
    { key: "version", header: "Version", align: "right", cell: (n) => n.lineage_version ?? "—" },
  ];
  return (
    <Grid min={340} gap="md">
      <Panel title="Lineage" subtitle="Root → … → this item, plus every child derived from it." dense>
        <QueryBoundary query={lineage} skeletonRows={4}>
          {(l) => (
            <>
              <p className="ym-hint">
                Root id: <code>{l.root_id}</code>
              </p>
              <p className="ym-label">Ancestors</p>
              <DataTable
                rows={l.ancestors}
                columns={lineageColumns}
                rowKey={(n) => n.id}
                caption="Ancestors"
                empty="This item is a root"
                emptyHint="No parent content item was recorded."
              />
              <p className="ym-label">Children</p>
              <DataTable
                rows={l.children}
                columns={lineageColumns}
                rowKey={(n) => n.id}
                caption="Children"
                empty="Nothing derived from this item yet"
              />
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Script versions" dense>
        <QueryBoundary query={detail} skeletonRows={3}>
          {(item) => (
            <DataTable
              rows={item.variants}
              columns={[
                { key: "label", header: "Variant", cell: (v) => v.label },
                { key: "selected", header: "Production", cell: (v) => (v.selected ? <Badge tone="success">Selected</Badge> : <span className="ym-muted">—</span>) },
                {
                  key: "score",
                  header: "Predicted",
                  align: "right",
                  cell: (v) => (v.predicted_score === null ? "—" : v.predicted_score.toFixed(1)),
                },
                { key: "metadata", header: "Metadata", cell: (v) => preview(v.metadata, 160), hideBelow: "lg" },
              ]}
              rowKey={(v) => v.id}
              caption="Script versions"
              empty="No script version"
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="QC verdict history" dense>
        <QueryBoundary query={audit} skeletonRows={3}>
          {(a) => (
            <DataTable
              rows={a.quality_checks.map((q, i) => ({ key: `${q.created_at}-${i}`, q }))}
              columns={[
                { key: "at", header: "When (UTC)", cell: (r) => utc(r.q.created_at) },
                {
                  key: "overall",
                  header: "Score",
                  align: "right",
                  cell: (r) => (
                    <Badge tone={r.q.passed ? "success" : "danger"}>{r.q.overall.toFixed(0)}</Badge>
                  ),
                },
                { key: "verdict", header: "Verdict", cell: (r) => <StatusBadge status={r.q.passed ? "PASSED" : "REJECTED"} /> },
                { key: "notes", header: "Notes", cell: (r) => r.q.notes || "—", hideBelow: "md" },
              ]}
              rowKey={(r) => r.key}
              caption="QC verdict history"
              empty="No quality check on record"
              emptyHint="A verdict is written per render; none has run for this item."
            />
          )}
        </QueryBoundary>
      </Panel>
    </Grid>
  );
}

/* ==========================================================================
 * Reviews
 * ======================================================================= */

function ReviewsTab({ detail, audit }: { detail: QueryState<ContentDetail>; audit: QueryState<Audit> }) {
  return (
    <Grid min={340} gap="md">
      <Panel title="Quality verdicts" dense>
        <QueryBoundary query={audit} skeletonRows={4}>
          {(a) =>
            a.quality_checks.length === 0 ? (
              <EmptyState title="No quality verdict" description="No QC row exists for this item's renders." />
            ) : (
              a.quality_checks.map((q, i) => (
                <div key={`${q.created_at}-${i}`} style={{ marginBottom: "var(--space-3)" }}>
                  <div style={{ display: "flex", gap: "var(--space-2)", alignItems: "center" }}>
                    <StatusBadge status={q.passed ? "PASSED" : "REJECTED"} />
                    <span className="ym-num">{q.overall.toFixed(0)}/100</span>
                    <span className="ym-muted">{utc(q.created_at)}</span>
                  </div>
                  {q.notes ? <p className="ym-hint">{q.notes}</p> : null}
                  <DataTable
                    rows={Object.entries(q.components).map(([component, score]) => ({
                      component,
                      score: numberOrNull(score),
                    }))}
                    columns={[
                      { key: "component", header: "Component", cell: (r) => humanize(r.component) },
                      {
                        key: "score",
                        header: "Score",
                        align: "right",
                        cell: (r) => (r.score === null ? preview(r.score) : r.score.toFixed(1)),
                      },
                    ]}
                    rowKey={(r) => r.component}
                    caption="QC components"
                    empty="No component score"
                  />
                </div>
              ))
            )
          }
        </QueryBoundary>
      </Panel>

      <Panel title="Compliance findings" subtitle="Recorded by the pre-publish gate; a HUMAN_REVIEW verdict blocks publication." dense>
        <QueryBoundary query={audit} skeletonRows={3}>
          {(a) => (
            <DataTable
              rows={a.publishing_jobs.map((j, i) => ({ key: `${j.platform}-${i}`, job: j }))}
              columns={[
                { key: "platform", header: "Platform", cell: (r) => humanize(r.job.platform) },
                { key: "status", header: "Publish status", cell: (r) => <StatusBadge status={r.job.status} /> },
                { key: "compliance", header: "Compliance", cell: (r) => preview(r.job.compliance, 200) },
                { key: "error", header: "Error", cell: (r) => r.job.error || "—", hideBelow: "lg" },
              ]}
              rowKey={(r) => r.key}
              caption="Compliance findings"
              empty="No publishing job"
              emptyHint="No compliance gate has run: nothing was submitted to a platform."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Review threads" dense>
        <StatTile
          label="Review threads for this project"
          unavailable
          hint="Review threads are workspace-scoped and not addressable per project in this read model: GET /reviews."
          source="GET /reviews"
        />
        <QueryBoundary query={detail} skeletonRows={1}>
          {() => (
            <p className="ym-hint">
              Approvals are per target version. An approval stops counting the moment the target changes — the QC
              verdict above is the only approval signal this project read model can verify.
            </p>
          )}
        </QueryBoundary>
      </Panel>
    </Grid>
  );
}

/* ==========================================================================
 * Exports
 * ======================================================================= */

function ExportsTab() {
  return (
    <Grid min={340} gap="md">
      <Panel title="Exports" dense>
        {/* Deliberately UNAVAILABLE: no export endpoint is in this screen's read
            model. A fabricated "0 exports" would read as "nothing to ship". */}
        <StatTile
          label="Exports for this project"
          unavailable
          hint="Export jobs are workspace-scoped and not addressable per project here: GET /exports."
          source="GET /exports"
        />
        <EmptyState
          title="Not in this screen's read model"
          description="Export jobs, profiles and downloads live in the Exports surface. This tab says UNAVAILABLE rather than showing a count it cannot verify."
        />
      </Panel>
    </Grid>
  );
}

/* ==========================================================================
 * Publishing
 * ======================================================================= */

function PublishingTab({ audit, schedule }: { audit: QueryState<Audit>; schedule: QueryState<ScheduleList> }) {
  const entries = schedule.data?.items ?? null;
  return (
    <Grid min={340} gap="md">
      <Panel title="Publishing jobs" dense>
        <QueryBoundary query={audit} skeletonRows={4}>
          {(a) => (
            <DataTable
              rows={a.publishing_jobs.map((j, i) => ({ key: `${j.platform}-${i}`, job: j }))}
              columns={[
                { key: "platform", header: "Platform", cell: (r) => humanize(r.job.platform) },
                { key: "status", header: "Status", cell: (r) => <StatusBadge status={r.job.status} /> },
                { key: "attempt", header: "Attempt", align: "right", cell: (r) => r.job.attempt },
                {
                  key: "remote",
                  header: "Remote",
                  cell: (r) =>
                    r.job.remote_url ? (
                      <a href={r.job.remote_url} target="_blank" rel="noreferrer">
                        {r.job.remote_post_id || r.job.remote_url}
                      </a>
                    ) : (
                      <span className="ym-muted">no remote id</span>
                    ),
                },
                { key: "error", header: "Error", cell: (r) => r.job.error || "—", hideBelow: "md" },
              ]}
              rowKey={(r) => r.key}
              caption="Publishing jobs"
              empty="No publishing job"
              emptyHint="Nothing has been submitted to a platform for this project."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Published posts" dense>
        <QueryBoundary query={audit} skeletonRows={3}>
          {(a) => (
            <DataTable
              rows={a.published_posts.map((p, i) => ({ key: `${p.platform}-${i}`, post: p }))}
              columns={[
                { key: "title", header: "Post", cell: (r) => r.post.title || "untitled" },
                { key: "platform", header: "Platform", cell: (r) => humanize(r.post.platform) },
                {
                  key: "mode",
                  header: "Mode",
                  cell: (r) => (
                    <Badge tone={r.post.is_mock ? "mock" : "live"} title={r.post.is_mock ? "Published through mock publishing" : "Published live"}>
                      {r.post.is_mock ? "MOCK" : "LIVE"}
                    </Badge>
                  ),
                },
                {
                  key: "url",
                  header: "Remote",
                  cell: (r) =>
                    r.post.remote_url ? (
                      <a href={r.post.remote_url} target="_blank" rel="noreferrer">
                        {r.post.remote_post_id || "open"}
                      </a>
                    ) : (
                      <span className="ym-muted">—</span>
                    ),
                },
              ]}
              rowKey={(r) => r.key}
              caption="Published posts"
              empty="No published post"
              emptyHint="A post row is written only after a publishing job reports a remote id."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Scheduled entries" subtitle="Calendar entries that point at this project." dense>
        <QueryBoundary query={schedule} skeletonRows={3}>
          {(s) => <ScheduleTable rows={s.items} />}
        </QueryBoundary>
      </Panel>
    </Grid>
  );
}

function ScheduleTable({ rows }: { rows: ScheduleRow[] }) {
  return (
    <DataTable
      rows={rows}
      columns={[
        { key: "run_at", header: "Runs at (UTC)", cell: (r) => utc(r.run_at) },
        { key: "platform", header: "Platform", cell: (r) => humanize(r.platform) },
        { key: "status", header: "Status", cell: (r) => <StatusBadge status={r.status} /> },
        { key: "campaign", header: "Campaign", cell: (r) => r.campaign_id?.slice(0, 8) || "—", hideBelow: "lg" },
      ]}
      rowKey={(r) => r.id}
      caption="Scheduled entries"
      empty="Not on the calendar"
      emptyHint="No pending, dispatching, queued or failed calendar entry references this project."
    />
  );
}

/* ==========================================================================
 * Performance
 * ======================================================================= */

function PerformanceTab({ audit }: { audit: QueryState<Audit> }) {
  return (
    <Panel
      title="Performance"
      subtitle="The audit bundle carries the latest metric snapshot per post; earlier snapshots are not part of this read model."
      dense
    >
      <QueryBoundary query={audit} skeletonRows={4}>
        {(a) => (
          <>
            <Grid min={160} gap="sm">
              <StatTile
                label="Posts with metrics"
                value={a.published_posts.filter((p) => p.metrics !== null).length}
                hint="Latest snapshot only"
                source="audit.published_posts"
              />
              <StatTile
                label="Mock posts"
                value={a.published_posts.filter((p) => p.is_mock).length}
                tone="mock"
                hint="Never counted as live performance"
              />
              <StatTile
                label="Total views (latest)"
                value={
                  a.published_posts.reduce((sum, p) => sum + (p.metrics?.views ?? 0), 0) || null
                }
                unavailable={!a.published_posts.some((p) => p.metrics !== null)}
                hint="Sum of the latest snapshot per post"
              />
            </Grid>
            <DataTable
              rows={a.published_posts.map((p, i) => ({ key: `${p.platform}-${i}`, post: p }))}
              columns={[
                { key: "title", header: "Post", cell: (r) => r.post.title || "untitled" },
                { key: "platform", header: "Platform", cell: (r) => humanize(r.post.platform) },
                {
                  key: "mode",
                  header: "Mode",
                  cell: (r) => <Badge tone={r.post.is_mock ? "mock" : "live"}>{r.post.is_mock ? "MOCK" : "LIVE"}</Badge>,
                },
                {
                  key: "views",
                  header: "Views",
                  align: "right",
                  cell: (r) => (r.post.metrics === null ? <span className="ym-muted">no snapshot</span> : r.post.metrics.views),
                },
                {
                  key: "likes",
                  header: "Likes",
                  align: "right",
                  /* HONESTY (Work 16.5.7 §8): "no snapshot" rather than a bare
                   * em dash. A dash is a rendered VALUE that reads as "zero
                   * likes, quietly"; the row above already says "no snapshot",
                   * and three cells in one table must not disagree about what
                   * missing data looks like. */
                  cell: (r) =>
                    r.post.metrics === null ? (
                      <span className="ym-muted">no snapshot</span>
                    ) : (
                      r.post.metrics.likes
                    ),
                  hideBelow: "md",
                },
                {
                  key: "completion",
                  header: "Completion",
                  align: "right",
                  cell: (r) =>
                    r.post.metrics === null ? (
                      <span className="ym-muted">no snapshot</span>
                    ) : (
                      pct(r.post.metrics.completion_rate)
                    ),
                },
                {
                  key: "captured",
                  header: "Captured",
                  cell: (r) => utc(r.post.metrics?.captured_at),
                  hideBelow: "lg",
                },
              ]}
              rowKey={(r) => r.key}
              caption="Post performance"
              empty="No published post"
              emptyHint="Performance starts when a post row exists and the Analytics Agent captures a snapshot."
            />
          </>
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Activity
 * ======================================================================= */

function ActivityTab({ audit }: { audit: QueryState<Audit> }) {
  return (
    <Panel title="Activity" subtitle="Raw workspace event trail filtered to this project by the backend." dense>
      <QueryBoundary query={audit} skeletonRows={6}>
        {(a) => (
          <DataTable
            rows={a.event_trail.map((e, i) => ({ key: `${e.at}-${i}`, e }))}
            columns={[
              { key: "at", header: "When (UTC)", cell: (r) => utc(r.e.at) },
              { key: "kind", header: "Kind", cell: (r) => humanize(r.e.kind) },
              { key: "level", header: "Level", cell: (r) => <StatusBadge status={r.e.level} /> },
              { key: "message", header: "Message", cell: (r) => r.e.message },
            ]}
            rowKey={(r) => r.key}
            caption="Activity"
            empty="No event recorded"
            emptyHint="The event trail is read from the last 400 workspace events that name this content id."
          />
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

/**
 * Resolve the project id, then mount the reader under a key derived from it.
 *
 * `useCombinedQueries` re-reads its panels when its own tick changes, not when
 * the id changes, and React Router keeps this component mounted across
 * `/projects/:contentIdA → :contentIdB`. Without the key, the second project
 * would be painted with the first project's rows until somebody pressed
 * Refresh. The key makes the id change a remount, which is the honest fix
 * rather than a second private cache.
 */
export function ProjectDetail({ contentId }: { contentId?: string }) {
  const params = useParams<{ contentId: string }>();
  const id = contentId ?? params.contentId ?? "";
  return <ProjectReader key={id} contentId={id} />;
}

function ProjectReader({ contentId }: { contentId: string }) {
  const navigate = useNavigate();
  const { workspaceId } = useSession();
  const [tab, setTab] = useState("overview");

  const id = contentId;
  const ready = Boolean(workspaceId && id);

  const panels = useCombinedQueries<Panels>({
    detail: () => (ready ? (wsApi.get(`/content/${id}`) as Promise<ContentDetail>) : idle<ContentDetail>()),
    timeline: () =>
      ready ? (wsApi.get(`/content/${id}/timeline`) as Promise<Timeline>) : idle<Timeline>(),
    lineage: () => (ready ? (wsApi.get(`/content/${id}/lineage`) as Promise<Lineage>) : idle<Lineage>()),
    audit: () => (ready ? (wsApi.get(`/content/${id}/audit`) as Promise<Audit>) : idle<Audit>()),
    schedule: () => (ready ? (wsApi.get("/calendar") as Promise<ScheduleList>) : idle<ScheduleList>()),
  });

  const { data, settled, errors, reload } = panels;
  const detail = panelState(data.detail, errors.detail, settled, reload);
  const timeline = panelState(data.timeline, errors.timeline, settled, reload);
  const lineage = panelState(data.lineage, errors.lineage, settled, reload);
  const audit = panelState(data.audit, errors.audit, settled, reload);
  const schedule = panelState(data.schedule, errors.schedule, settled, reload);

  const failed = Object.entries(errors) as [keyof Panels, string][];

  const tabs = [
    { id: "overview", label: "Overview" },
    { id: "research", label: "Research" },
    { id: "script", label: "Script", count: count(detail.data?.variants_count) },
    { id: "scenes", label: "Scenes" },
    { id: "timeline", label: "Timeline", count: count(timeline.data?.items?.length) },
    { id: "assets", label: "Assets" },
    {
      id: "versions",
      label: "Versions",
      count: count((lineage.data?.ancestors?.length ?? 0) + (lineage.data?.children?.length ?? 0)),
    },
    { id: "reviews", label: "Reviews", count: count(audit.data?.quality_checks?.length) },
    { id: "exports", label: "Exports" },
    { id: "publishing", label: "Publishing", count: count(audit.data?.publishing_jobs?.length) },
    { id: "performance", label: "Performance", count: count(audit.data?.published_posts?.length) },
    { id: "activity", label: "Activity", count: count(audit.data?.event_trail?.length) },
  ];

  if (!ready) {
    return (
      <>
        <PageHeader
          title="Project"
          description="Research, script, scenes, timeline, publishing and performance for one project."
        />
        <Panel title="No project selected">
          <EmptyState
            title="No project id"
            description="A project is addressed by its content id. Open one from the Projects list."
            action={<Button onClick={() => navigate("/projects")}>Go to Projects</Button>}
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        breadcrumb={<span className="ym-crumb">Projects</span>}
        title={detail.data?.topic || detail.data?.id.slice(0, 8) || "Project"}
        description={
          detail.data
            ? `Stage ${humanize(detail.data.status)} · created ${utc(detail.data.created_at)}`
            : "Reading the project from the workspace API."
        }
        actions={
          <div style={{ display: "flex", gap: "var(--space-2)" }}>
            {detail.data ? <StatusBadge status={detail.data.status} /> : null}
            <Button onClick={reload}>Refresh</Button>
            <Button variant="ghost" onClick={() => navigate("/projects")}>
              All projects
            </Button>
          </div>
        }
      />

      {failed.length > 0 && (
        <Panel title={`${failed.length} read${failed.length === 1 ? "" : "s"} failed`} dense>
          <div className="ym-notif-wrap">
            {failed.map(([key, message]) => (
              <div key={key} className="ym-notif-item">
                <Badge tone="danger" dot>
                  {key}
                </Badge>
                <span className="ym-notif-detail">{message}</span>
              </div>
            ))}
          </div>
        </Panel>
      )}

      <Tabs tabs={tabs} active={tab} onChange={setTab} />

      {tab === "overview" && <OverviewTab detail={detail} audit={audit} schedule={schedule} />}
      {tab === "research" && <ResearchTab detail={detail} />}
      {tab === "script" && <ScriptTab detail={detail} />}
      {tab === "scenes" && <ScenesTab detail={detail} timeline={timeline} />}
      {tab === "timeline" && <TimelineTab timeline={timeline} />}
      {tab === "assets" && <AssetsTab detail={detail} audit={audit} />}
      {tab === "versions" && <VersionsTab detail={detail} lineage={lineage} audit={audit} />}
      {tab === "reviews" && <ReviewsTab detail={detail} audit={audit} />}
      {tab === "exports" && <ExportsTab />}
      {tab === "publishing" && <PublishingTab audit={audit} schedule={schedule} />}
      {tab === "performance" && <PerformanceTab audit={audit} />}
      {tab === "activity" && <ActivityTab audit={audit} />}

      {/* Money is reported, never derived: the audit bundle carries no cost, so
          the field below reads UNAVAILABLE instead of a made-up estimate. */}
      <Panel title="Cost of this project" dense>
        <StatTile
          label="Attributed spend"
          unavailable
          hint="No per-project cost is exposed by the endpoints this screen reads. Spend per category is workspace-scoped: GET /costs."
          source="GET /costs"
        />
      </Panel>
    </>
  );
}

export default ProjectDetail;
/* Operations — the health of the process, and the money it may have lost.
 *
 *   GET  /livez                        liveness (200 while the process serves)
 *   GET  /system/readiness             the 12 production probes, one row each
 *   GET  /system/doctor                readiness + remediation per failed probe
 *   GET  /system/health                subsystem flags + the mock switches
 *   GET  /system/mode                  deployment mode + mock switches
 *   GET  /system/orphans               dangling-row counts
 *   GET  /internal/collectors          which metric collectors are up
 *   GET  /internal/alerts              every alert rule with its live verdict
 *   GET  /internal/slo                 SLO TARGETS (explicitly not achieved)
 *   GET  /ops/overview                 failure-isolated per-section ops view
 *   GET  /jobs?limit=100               jobs with lease ownership
 *   GET  /costs                        24h spend against the daily budget
 *   GET  /costs/intelligence           per-cycle / per-video economics
 *   GET  /provider-maturity/incidents  paid submissions awaiting reconciliation
 *   GET  /retention                    retention policy (NULL = keep forever)
 *
 * THE HONESY CONTRACT ON THIS SCREEN
 *
 * `GET /system/health` returns `llm_provider: true` when `MOCK_LLM=true` --
 * because the backend short-circuits the probe. Reading that boolean as "the LLM
 * is healthy" is exactly the Work 16.1 failure: a simulated answer presented as
 * a live one. So this screen NEVER renders a subsystem as healthy without also
 * rendering `mocks`, and a mocked subsystem is labelled SIMULATED with the
 * `mock` tone, never `success`. The same applies to the video engine (engine
 * name "mock"/"simulation"), to per-platform publishers (`mode`), and to any
 * provider record flagged `simulation_only`.
 *
 * SECOND HONESY RULE: A DOWN COLLECTOR MEANS THE NUMBER IS UNKNOWN.
 * `metrics.collect_all` is explicit -- "a failed collector reports
 * ymoney_collector_up=0 for itself and leaves its gauges untouched: zeroing them
 * would read as 'no backlog' during an outage." So a down collector renders
 * UNAVAILABLE, never 0, and the GPU view says so rather than inventing a count.
 *
 * THIRD: THE GPU GAUGE IS NOT RE-DERIVED HERE. `metrics.collect_gpu_slots`
 * counts MEDIA_INTEL_GPU_SLOT jobs in QUEUED / RUNNING / **WAITING**, because a
 * held slot is parked in WAITING deliberately. A UI that counts QUEUED and
 * RUNNING only matches nothing ever and reports zero on a saturated GPU. This
 * screen therefore shows the backend's own `gpu_queue_starvation` verdict and the
 * collector's up/down state, and computes no slot state of its own.
 *
 * FOURTH: NOTHING AMBIGUOUS IS RETRYABLE. `SUBMISSION_UNKNOWN` and
 * `UNKNOWN_EXPOSURE` are drawn on the `unknown` tone and there is no retry
 * affordance anywhere in this file.
 */

import { useMemo, useState } from "react";
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
  StatTile,
  StatusBadge,
  Tabs,
  humanize,
  toneForStatus,
  type Column,
} from "../../design-system/primitives";
import { useQuery, useWsQuery, type QueryState } from "../../api/queries";
import { INTERNAL_OPS_PATH, fetchInternalOps, type InternalOpsPath } from "../../api/internalOps";
import { ApiError, api } from "../../lib/api";
import { useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes — transcribed from each router's return statement.
 * ======================================================================= */

/* `internal_ops.liveness` */
export type Liveness = {
  status: string;
  detail: string;
  event_loop: boolean;
  checks: { id: string; status: string; blocking: boolean; detail: string }[];
  blocking_failures: string[];
};

/* `services.readiness.run_readiness` */
export type ReadinessCheck = {
  id: string;
  status: string;
  blocking: boolean;
  tier: number;
  detail: string;
  latency_ms: number;
  remediation: string;
};
export type Readiness = {
  status: string;
  checked_at: string;
  stale_after_hours: number;
  checks: ReadinessCheck[];
  blocking_failures: string[];
  message: string;
};

/* `misc.system_doctor` -- readiness plus a grouped remediation index. */
export type Doctor = Readiness & {
  doctor: {
    blocking_failed: string[];
    attention_needed: string[];
    remediations: Record<string, string>;
  };
};

/* `misc._publisher_status` */
export type PublisherState = {
  mode: "mock" | "real";
  ready: boolean;
  detail: string;
  via_relay: boolean;
};
export type PublisherMap = Record<string, PublisherState>;

export type TtsStatus = { provider?: string; healthy?: boolean };

export type Health = {
  status: string;
  database: boolean;
  video_engine: boolean;
  video_engine_name: string;
  video_engine_version: string | null;
  /**
   * `llm_ok = True if settings.mock_llm else _llm_probe()`.
   *
   * TRUE HERE WITH `mocks.llm` TRUE MEANS NOTHING WAS PROBED. Never rendered as
   * a healthy subsystem on its own.
   */
  llm_provider: boolean;
  tts: TtsStatus;
  publishers: PublisherMap;
  queue: {
    backend?: string;
    gpu_worker?: boolean;
    gpu_cuda?: boolean;
    redis?: boolean;
    error?: string;
  };
  mocks: {
    llm: boolean;
    trends: boolean;
    publishing: boolean;
    analytics: boolean;
    video_engine: boolean;
  };
  time: string;
};

/* `misc.system_mode` */
export type SystemMode = {
  mode: string;
  video_engine: string;
  mocks: { publishing: boolean; analytics: boolean; trends: boolean; video_engine: boolean };
};

/* `misc.system_orphans` */
export type Orphans = {
  videos_orphaned: number;
  variants_orphaned: number;
  publishing_jobs_orphaned: number;
  published_posts_orphaned: number;
  healthy: boolean;
  checked_at: string;
  /** Present only when the sweep itself failed. Absent means the sweep ran. */
  detail?: string;
};

/* `internal_ops.collectors_endpoint` */
export type Collectors = {
  collectors: Record<string, "up" | "down">;
  failed: string[];
  note: string;
};

/* `observability.slo.alert_catalog` -- `rules[]` is `AlertRule.to_dict()` plus
 * the live verdict; `verdicts[]` is `AlertVerdict.to_dict()`. */
export type AlertRule = {
  id: string;
  severity: string;
  title: string;
  detects: string;
  threshold: number;
  threshold_source: string;
  unit: string;
  runbook: string;
  firing: boolean;
  /** null when the rule produced no verdict. */
  observed: number | null;
  reason?: string;
};
export type AlertCatalog = {
  evaluated_at_epoch: number;
  note: string;
  thresholds: Record<string, number>;
  firing_count: number;
  rules: AlertRule[];
  verdicts: { rule_id: string; firing: boolean; observed: number; threshold: number; unit: string; reason: string }[];
};

/* `observability.slo.slo_catalog` -- TARGETS, and it says so. */
export type SloCatalog = {
  measured: boolean;
  disclaimer: string;
  thresholds: Record<string, number>;
  targets: Record<string, unknown>[];
};

/* `ops._jobs_section` / `_storage_section` / `_costs_section` / etc. Each section
 * is failure-isolated server-side and answers {available:false, reason} when it
 * cannot be computed. */
export type OpsSectionUnavailable = { available: false; reason: string };
export type JobsSection = {
  available: true;
  by_status: Record<string, number>;
  total: number;
  failed_recent: { id: string; type: string; status: string; error: string; retry_count: number; created_at: string }[];
};
export type StorageSection = {
  available: true;
  bytes: number;
  file_count: number;
  source: string;
};
/* NOT FIXED — Work 16.5.7 §8: `backend/app/api/v1/ops.py` was outside the
 * permitted write scope, so this section still reports `$0.0000` for an empty
 * 24h window and folds an UNKNOWN_EXPOSURE row in as a hard zero. The
 * Command Center reads THIS section rather than GET /costs, so fixing /costs
 * alone does not reach this screen. Types describe today's real payload;
 * recorded as `known_out_of_scope` in `docs/ANALYTICS_HONESTY_AUDIT.json`. */
export type CostsSection = {
  available: true;
  last_24h_by_category: Record<string, number>;
  spent_last_24h_usd: number;
  daily_budget_usd: number;
  per_video_budget_usd: number;
  within_budget: boolean;
  remaining_usd: number;
  since: string;
};
export type ProviderHealthSection = {
  available: true;
  status: string | null;
  checked_at: string | null;
  blocking_failures: string[];
  checks: { id: string; status: string; blocking: boolean; latency_ms: number }[];
};
export type ExportsSection = {
  available: true;
  by_state: Record<string, number>;
  failed: { id: string; format: string; state: string; error: string; attempt: number; created_at: string }[];
  failed_count: number;
};
export type AuditSection = {
  available: true;
  events_last_7d: number;
  since: string;
  retention_enforced: boolean;
};
export type RetentionSection = Record<string, unknown> & { available?: boolean };
export type OpsOverview = {
  workspace_id: string;
  generated_at: string;
  jobs: JobsSection | OpsSectionUnavailable;
  reviews: Record<string, unknown> | OpsSectionUnavailable;
  exports: ExportsSection | OpsSectionUnavailable;
  storage: StorageSection | OpsSectionUnavailable;
  provider_health: ProviderHealthSection | OpsSectionUnavailable;
  costs: CostsSection | OpsSectionUnavailable;
  audit: AuditSection | OpsSectionUnavailable;
  retention: RetentionSection | OpsSectionUnavailable;
};

/* `jobs._to_dict` */
export type JobRow = {
  id: string;
  type: string;
  status: string;
  priority: number;
  retry_count: number;
  max_retries: number;
  next_run_at: string | null;
  started_at: string | null;
  completed_at: string | null;
  last_error: string;
  claimed_by: string;
  claimed_at: string | null;
  lease_expires_at: string | null;
  heartbeat_at: string | null;
  /** `job_leases.lease_state`: QUEUED | CLAIMED | RUNNING | TERMINAL. */
  lease_state: string;
  created_at: string;
};

/* `misc.cost_summary` — HONESTY (§8): nullable spend total, non-null gate. See
 * the CONTRACT LAG note on `CostSummary` in `localization/Localization.tsx`. */
export type CostSummary = {
  last_24h_by_category: Record<string, number>;
  spent_last_24h_usd: number | null;
  spent_last_24h_unknown_exposure_rows: number;
  daily_budget_usd: number;
  per_video_budget_usd: number;
  within_budget: boolean;
  remaining_usd: number;
};

/* `safety.cost_intelligence`
 *
 * NOT FIXED — Work 16.5.7 §8 found three fabrications here and could not fix
 * them: `backend/app/api/v1/safety.py` was outside the permitted write scope.
 * These types therefore describe what the endpoint ACTUALLY returns today:
 *   - `total_cost_usd` is `$0.0000` for an empty ledger, and an
 *     UNKNOWN_EXPOSURE row is dropped from the sum as a silent zero;
 *   - `totals.views` is `0` when no post has a snapshot.
 * Every efficiency below divides that total, so it inherits its fiction. Types
 * stay `number` HERE ON PURPOSE: typing them nullable would make the compiler
 * accept a null the endpoint never sends, which is the same class of error in
 * the other direction. Recorded as `known_out_of_scope` in
 * `docs/ANALYTICS_HONESTY_AUDIT.json`. */
export type CostIntelligence = {
  total_cost_usd: number;
  /** null when the denominator is zero -- NOT $0. */
  per_cycle_usd: number | null;
  per_video_usd: number | null;
  per_publication_usd: number | null;
  cost_per_1000_views_usd: number | null;
  by_category: Record<string, number>;
  by_agent: Record<string, number>;
  publications_by_platform: Record<string, number>;
  totals: {
    cycles: number;
    videos_built: number;
    posts_published: number;
    views: number;
  };
  estimated_return_usd: null;
  estimated_return_note: string;
};

/* `providers._incident_row` -- see features/intelligence for the field notes. */
export type PaidIncident = {
  incident_id: string;
  source: string;
  provider: string;
  operation: string;
  attempted_at: string;
  remote_id: string;
  state: string;
  exposure: string;
  estimated_exposure_usd: number | null;
  exposure_unknown: boolean;
  recommended_action: string;
  retry_safe: boolean;
  may_resubmit: boolean;
  detail: string;
  note: string;
};
export type PaidIncidents = {
  items: PaidIncident[];
  count: number;
  unknown_exposure_count: number;
  states: string[];
  note: string;
};

/* ==========================================================================
 * Derived helpers
 * ======================================================================= */

/**
 * Whether a subsystem's green light is a SIMULATION.
 *
 * The backend is not wrong: it returns `llm_provider: true` and separately
 * `mocks.llm: true`, and refuses to probe when mocking is on. The bug is on
 * the reading side, so the fix is to refuse to render a mocked subsystem's
 * boolean as evidence. Every such subsystem is rendered `SIMULATED`.
 */
export function isSimulated(
  mocks: Partial<Health["mocks"]> | undefined,
  engineName: string | null | undefined,
): boolean {
  if (!mocks) return false;
  if (mocks.llm || mocks.trends || mocks.publishing || mocks.analytics || mocks.video_engine) {
    return true;
  }
  const name = (engineName ?? "").toLowerCase();
  return name === "mock" || name === "simulation";
}

/** The two that specifically decide whether a light is simulated, per subsystem. */
export function simulatedSubsystems(health: Health | null): string[] {
  if (!health) return [];
  const out: string[] = [];
  if (health.mocks.llm) out.push("LLM");
  if (health.mocks.video_engine) out.push("VIDEO ENGINE");
  if (health.mocks.trends) out.push("TREND SOURCES");
  if (health.mocks.publishing) out.push("PUBLISHING");
  if (health.mocks.analytics) out.push("ANALYTICS");
  return out;
}

/**
 * A collector that is down leaves its gauges UNTOUCHED, so the number it feeds
 * is unknown. Returns "up" | "unknown" so the caller can render UNAVAILABLE.
 */
export function collectorState(
  collectors: Collectors | null,
  name: string,
): "up" | "unknown" {
  if (!collectors) return "unknown";
  return collectors.collectors?.[name] === "up" ? "up" : "unknown";
}

/** Section available? The ops router isolates failures per section. */
function isAvailable(section: unknown): boolean {
  return (
    typeof section === "object" &&
    section !== null &&
    (section as { available?: unknown }).available === true
  );
}

function sectionReason(section: unknown): string {
  return (
    typeof section === "object" && section !== null
      ? String((section as { reason?: unknown }).reason ?? "unknown reason")
      : "unknown reason"
  );
}

/** A section the backend could not compute, rendered as an explicit alert. */
function SectionAlert({ name, section }: { name: string; section: unknown }) {
  if (isAvailable(section)) return null;
  return (
    <p className="ym-error" role="alert">
      The {name} section of GET /ops/overview could not be computed ({sectionReason(section)}).
      Its figures read UNAVAILABLE. Every other section is independent and still
      renders.
    </p>
  );
}

function bytes(value: number | null | undefined): React.ReactNode {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return <span className="ym-muted">UNAVAILABLE</span>;
  }
  const units = ["B", "KB", "MB", "GB", "TB"];
  let n = value;
  let i = 0;
  while (n >= 1024 && i < units.length - 1) {
    n /= 1024;
    i += 1;
  }
  return `${n.toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function when(value: string | null | undefined): React.ReactNode {
  return value ? (
    value.replace("Z", " UTC").replace("T", " ")
  ) : (
    <span className="ym-muted">—</span>
  );
}

/* ==========================================================================
 * At a glance
 * ======================================================================= */

function AtAGlance({
  liveness,
  readiness,
  health,
  collectors,
  alerts,
  costs,
  incidents,
}: {
  liveness: QueryState<Liveness>;
  readiness: QueryState<Readiness>;
  health: QueryState<Health>;
  collectors: QueryState<Collectors>;
  alerts: QueryState<AlertCatalog>;
  costs: QueryState<CostSummary>;
  incidents: QueryState<PaidIncidents>;
}) {
  const simulated = simulatedSubsystems(health.data);
  const failedProbes = readiness.data
    ? readiness.data.checks.filter((c) => c.status !== "passed")
    : null;
  const downCollectors = collectors.data?.failed ?? null;

  return (
    <Panel
      title="At a glance"
      subtitle="A figure with no source reads UNAVAILABLE. A simulated subsystem is labelled SIMULATED and is never counted as a healthy one."
      dense
    >
      {simulated.length > 0 ? (
        <p className="ym-error" role="alert">
          SIMULATED in this deployment: {simulated.join(", ")}. Where a subsystem is
          mocked, <code>GET /system/health</code> reports <code>true</code> without
          probing anything, so its flag below is evidence of configuration, not of
          a live service.
        </p>
      ) : null}
      <Grid min={190} gap="sm">
        <StatTile
          label="Liveness"
          value={liveness.data?.status}
          unavailable={liveness.data === null}
          tone={liveness.data === null ? "neutral" : liveness.data.status === "alive" ? "success" : "danger"}
          source="GET /livez"
        />
        <StatTile
          label="Readiness"
          value={readiness.data?.status}
          unavailable={readiness.data === null}
          tone={
            readiness.data === null
              ? "neutral"
              : readiness.data.blocking_failures.length > 0
                ? "danger"
                : "success"
          }
          hint={readiness.data?.blocking_failures.length ? readiness.data.message : undefined}
          source="GET /system/readiness"
        />
        <StatTile
          label="Failed production probes"
          value={failedProbes === null ? undefined : failedProbes.length}
          unavailable={failedProbes === null}
          tone={failedProbes && failedProbes.length > 0 ? "warning" : "success"}
          source="GET /system/readiness"
        />
        <StatTile
          label="Collectors down"
          value={downCollectors === null ? undefined : downCollectors.length}
          unavailable={downCollectors === null}
          tone={downCollectors && downCollectors.length > 0 ? "warning" : "success"}
          hint="A down collector leaves its gauges untouched, so its metrics read UNAVAILABLE rather than 0."
          source="GET /internal/collectors"
        />
        <StatTile
          label="Alerts firing"
          value={alerts.data?.firing_count}
          unavailable={alerts.data === null}
          tone={
            alerts.data === null
              ? "neutral"
              : alerts.data.firing_count > 0
                ? "danger"
                : "success"
          }
          source="GET /internal/alerts"
        />
        <StatTile
          label="Spent, last 24h"
          value={costs.data === null ? undefined : <Money usd={costs.data.spent_last_24h_usd} />}
          unavailable={costs.data === null}
          source="GET /costs"
        />
        <StatTile
          label="Unknown monetary exposure"
          value={incidents.data?.unknown_exposure_count}
          unavailable={incidents.data === null}
          tone={incidents.data === null ? "neutral" : "unknown"}
          hint="An accepted call nobody can price. Not zero -- unknown."
          source="GET /provider-maturity/incidents"
        />
        <StatTile
          label="Simulated subsystems"
          value={simulated.length}
          tone={simulated.length > 0 ? "mock" : "success"}
          hint="Each is labelled SIMULATED where it appears; none counts as live evidence."
          source="GET /system/health"
        />
      </Grid>
    </Panel>
  );
}

/* ==========================================================================
 * System
 * ======================================================================= */

function SystemPanel({
  liveness,
  readiness,
  health,
  mode,
  doctor,
  orphans,
}: {
  liveness: QueryState<Liveness>;
  readiness: QueryState<Readiness>;
  health: QueryState<Health>;
  mode: QueryState<SystemMode>;
  doctor: QueryState<Doctor>;
  orphans: QueryState<Orphans>;
}) {
  const checkColumns: Column<ReadinessCheck>[] = [
    { key: "id", header: "Probe", cell: (c) => <strong>{humanize(c.id)}</strong> },
    {
      key: "status",
      header: "Status",
      cell: (c) => <Badge tone={c.status === "passed" ? "success" : "danger"} dot>{humanize(c.status)}</Badge>,
    },
    {
      key: "blocking",
      header: "Blocks production",
      cell: (c) =>
        c.blocking ? (
          <Badge tone="danger">blocking</Badge>
        ) : (
          <Badge tone="neutral">advisory</Badge>
        ),
    },
    { key: "detail", header: "Detail", cell: (c) => <span className="ym-notif-detail">{c.detail || "—"}</span> },
    { key: "latency", header: "Probe ms", align: "right", cell: (c) => c.latency_ms, hideBelow: "md" },
    {
      key: "remediation",
      header: "Remediation",
      cell: (c) =>
        c.remediation ? (
          <span className="ym-notif-detail">{c.remediation}</span>
        ) : (
          <span className="ym-muted">—</span>
        ),
      hideBelow: "lg",
    },
  ];

  return (
    <>
      <Panel
        title="Liveness"
        subtitle="GET /livez. Dependency-free on purpose: a database outage must never trigger a restart loop."
        dense
      >
        <QueryBoundary query={liveness} skeletonRows={2}>
          {(d) => (
            <Grid min={190} gap="sm">
              <StatTile
                label="Process"
                value={d.status}
                tone={d.status === "alive" ? "success" : "danger"}
                hint={d.detail}
                source="GET /livez"
              />
              <StatTile
                label="Event loop accepting work"
                value={d.event_loop ? "yes" : "no"}
                tone={d.event_loop ? "success" : "danger"}
                source="GET /livez"
              />
              <StatTile
                label="Blocking liveness failures"
                value={d.blocking_failures.length}
                tone={d.blocking_failures.length > 0 ? "danger" : "success"}
                source="GET /livez"
              />
            </Grid>
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Production readiness"
        subtitle="GET /system/readiness. One row per probe, each with the remediation when it fails."
        actions={<StatusBadge status={readiness.data?.status} />}
        dense
      >
        <QueryBoundary query={readiness} skeletonRows={10}>
          {(d) => (
            <>
              <p className="ym-notif-detail">
                {d.message} — checked {when(d.checked_at)}, considered stale after{" "}
                {d.stale_after_hours}h.
              </p>
              <DataTable
                rows={d.checks ?? []}
                columns={checkColumns}
                rowKey={(c) => c.id}
                caption="Production readiness probes"
                maxHeight={520}
                empty="No probe reported"
                emptyHint="Readiness ran and declared no probes. Treat that as no evidence rather than as health."
              />
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Deployment mode and simulation switches"
        subtitle="GET /system/mode. These switches decide whether anything on this screen is real."
        dense
      >
        <QueryBoundary query={mode} skeletonRows={3}>
          {(d) => (
            <Grid min={180} gap="sm">
              <StatTile
                label="Deployment mode"
                value={d.mode}
                tone={d.mode === "production" ? "success" : "warning"}
                source="GET /system/mode"
              />
              <StatTile
                label="Video engine"
                value={d.video_engine}
                tone={d.mocks.video_engine ? "mock" : "info"}
                hint={d.mocks.video_engine ? "SIMULATED — renders are produced by a stand-in." : "A real engine is configured."}
                source="GET /system/mode"
              />
              {(["publishing", "analytics", "trends"] as const).map((k) => (
                <StatTile
                  key={k}
                  label={`${humanize(k)} mock`}
                  value={d.mocks[k] ? "SIMULATED" : "off"}
                  tone={d.mocks[k] ? "mock" : "success"}
                  source="GET /system/mode"
                />
              ))}
            </Grid>
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Doctor" subtitle="GET /system/doctor — failed probes grouped by whether they block production." dense>
        <QueryBoundary query={doctor} skeletonRows={4}>
          {(d) => (
            <>
              <Grid min={180} gap="sm">
                <StatTile
                  label="Blocking failures"
                  value={d.doctor.blocking_failed.length}
                  tone={d.doctor.blocking_failed.length > 0 ? "danger" : "success"}
                  source="GET /system/doctor"
                />
                <StatTile
                  label="Needs attention"
                  value={d.doctor.attention_needed.length}
                  tone={d.doctor.attention_needed.length > 0 ? "warning" : "success"}
                  hint="Advisory: this does not remove the process from rotation."
                  source="GET /system/doctor"
                />
              </Grid>
              {Object.entries(d.doctor.remediations).length === 0 ? (
                <p className="ym-hint">No probe failed, so there is no remediation to give.</p>
              ) : (
                <ul>
                  {Object.entries(d.doctor.remediations).map(([id, fix]) => (
                    <li key={id} className="ym-notif-detail">
                      <strong>{humanize(id)}</strong> — {fix}
                    </li>
                  ))}
                </ul>
              )}
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Orphaned rows"
        subtitle="GET /system/orphans. Counts only, safe to poll."
        dense
      >
        <QueryBoundary query={orphans} skeletonRows={2}>
          {(d) => (
            <>
              {d.detail ? (
                <p className="ym-error" role="alert">
                  The orphan sweep itself failed ({d.detail}). The zero counts below
                  are the sweep's fallback, NOT a clean result -- treat storage
                  integrity as unverified.
                </p>
              ) : null}
              <Grid min={175} gap="sm">
                <StatTile label="Videos without a variant" value={d.videos_orphaned} tone={d.videos_orphaned > 0 ? "warning" : "success"} source="GET /system/orphans" />
                <StatTile label="Variants without content" value={d.variants_orphaned} tone={d.variants_orphaned > 0 ? "warning" : "success"} source="GET /system/orphans" />
                <StatTile label="Publishing jobs without a video" value={d.publishing_jobs_orphaned} tone={d.publishing_jobs_orphaned > 0 ? "warning" : "success"} source="GET /system/orphans" />
                <StatTile label="Posts without a video" value={d.published_posts_orphaned} tone={d.published_posts_orphaned > 0 ? "warning" : "success"} source="GET /system/orphans" />
              </Grid>
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Subsystem probes"
        subtitle="GET /system/health. Every flag is shown next to the mock switch that decides whether it was probed."
        dense
      >
        <QueryBoundary query={health} skeletonRows={4}>
          {(d) => {
            const rows = [
              { id: "database", ok: d.database, simulated: false, detail: "SELECT 1 against the primary." },
              {
                id: "llm",
                ok: d.llm_provider,
                simulated: d.mocks.llm,
                detail: d.mocks.llm
                  ? "SIMULATED: MOCK_LLM is on, so the backend returned true without probing."
                  : "Reachability probe of the configured LLM endpoint.",
              },
              {
                id: "video_engine",
                ok: d.video_engine,
                simulated: d.mocks.video_engine,
                detail: `${d.video_engine_name}${
                  d.video_engine_version ? ` ${d.video_engine_version}` : ""
                }${d.mocks.video_engine ? " — SIMULATED, not a real render backend." : ""}`,
              },
              {
                id: "tts",
                ok: Boolean(d.tts?.healthy),
                simulated: false,
                detail: `provider ${d.tts?.provider ?? "unknown"}`,
              },
              { id: "gpu_worker_enabled", ok: Boolean(d.queue?.gpu_worker), simulated: false, detail: "GPU worker path turned on." },
              { id: "cuda_present", ok: Boolean(d.queue?.gpu_cuda), simulated: false, detail: "A CUDA device was found on this host." },
              { id: "redis", ok: Boolean(d.queue?.redis), simulated: false, detail: `queue backend ${d.queue?.backend ?? "unknown"}` },
            ];
            return (
              <DataTable
                rows={rows}
                columns={[
                  { key: "id", header: "Subsystem", cell: (r) => <strong>{humanize(r.id)}</strong> },
                  {
                    key: "state",
                    header: "Reported state",
                    cell: (r) =>
                      r.simulated ? (
                        <Badge tone="mock" dot title="Nothing was probed. This is a configuration flag.">
                          SIMULATED
                        </Badge>
                      ) : r.ok ? (
                        <Badge tone="success" dot>
                          UP
                        </Badge>
                      ) : (
                        <Badge tone="danger" dot>
                          DOWN
                        </Badge>
                      ),
                  },
                  {
                    key: "raw",
                    header: "Raw flag",
                    cell: (r) => (r.simulated ? <span className="ym-muted">not meaningful</span> : (r.ok ? "true" : "false")),
                    hideBelow: "md",
                  },
                  { key: "detail", header: "Detail", cell: (r) => <span className="ym-notif-detail">{r.detail}</span> },
                ]}
                rowKey={(r) => r.id}
                caption="Subsystem probes and their simulation state"
                empty="No subsystem reported"
                emptyHint="The health route answered with no probes at all, which is not evidence that anything is healthy."
              />
            );
          }}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Workers, queues, jobs
 * ======================================================================= */

function JobsPanel({ jobs, collectors }: { jobs: QueryState<{ items: JobRow[] }>; collectors: QueryState<Collectors> }) {
  const workerState = collectorState(collectors.data, "workers");
  const queueState = collectorState(collectors.data, "jobs");
  const rows = jobs.data?.items ?? [];

  const leaseColumns: Column<JobRow>[] = [
    { key: "id", header: "Job", cell: (j) => <span className="ym-notif-detail">{j.id.slice(0, 12)}</span> },
    { key: "type", header: "Type", cell: (j) => humanize(j.type) },
    { key: "status", header: "Status", cell: (j) => <StatusBadge status={j.status} /> },
    {
      key: "lease",
      header: "Lease",
      cell: (j) =>
        j.lease_state === "TERMINAL" ? (
          <span className="ym-muted">no lease held</span>
        ) : (
          <Badge tone="info">{humanize(j.lease_state)}</Badge>
        ),
    },
    {
      key: "owner",
      header: "Claimed by",
      cell: (j) =>
        j.claimed_by ? (
          <span className="ym-notif-detail">{j.claimed_by}</span>
        ) : (
          <span className="ym-muted">unclaimed</span>
        ),
    },
    {
      key: "expires",
      header: "Lease expires",
      cell: (j) => (j.lease_state === "TERMINAL" ? <span className="ym-muted">—</span> : when(j.lease_expires_at)),
      hideBelow: "md",
    },
    {
      key: "retry",
      header: "Retries",
      align: "right",
      cell: (j) => `${j.retry_count} / ${j.max_retries}`,
      hideBelow: "md",
    },
    { key: "created", header: "Created", cell: (j) => when(j.created_at), hideBelow: "lg" },
    {
      key: "error",
      header: "Last error",
      cell: (j) =>
        j.last_error ? (
          <Badge tone="danger" title={j.last_error}>
            {j.last_error.slice(0, 40)}
          </Badge>
        ) : (
          <span className="ym-muted">—</span>
        ),
      hideBelow: "lg",
    },
  ];

  const leasesHeld = rows.filter((j) => j.lease_state !== "TERMINAL").length;

  return (
    <>
      <Panel
        title="Worker fleet"
        subtitle="The `workers` collector reads the live worker task list from app.services.jobs. It is a read, not a probe of a remote service."
        dense
      >
        <Grid min={190} gap="sm">
          <StatTile
            label="Worker collector"
            value={workerState === "up" ? "up" : "UNAVAILABLE"}
            unavailable={workerState === "unknown"}
            tone={workerState === "up" ? "success" : "unknown"}
            hint={
              workerState === "unknown"
                ? "The collector is down. It leaves its gauges untouched, so worker count is unknown here -- NOT zero."
                : undefined
            }
            source="GET /internal/collectors"
          />
          <StatTile
            label="Jobs with a live lease"
            value={jobs.data === null ? undefined : leasesHeld}
            unavailable={jobs.data === null}
            tone="info"
            hint="A lease is what proves a live owner. Only a lapsed lease may be reclaimed."
            source="GET /jobs"
          />
          <StatTile
            label="Unclaimed work"
            value={jobs.data === null ? undefined : rows.filter((j) => !j.claimed_by).length}
            unavailable={jobs.data === null}
            source="GET /jobs"
          />
        </Grid>
      </Panel>

      <Panel
        title="Queue depth"
        subtitle="The `jobs` collector counts real rows. A down collector means unknown, not an empty queue."
        dense
      >
        <QueryBoundary query={collectors} skeletonRows={3}>
          {(d) => (
            <DataTable
              rows={Object.entries(d.collectors ?? {})}
              columns={[
                { key: "name", header: "Collector", cell: ([n]) => <strong>{humanize(n)}</strong> },
                {
                  key: "state",
                  header: "State",
                  cell: ([, state]) =>
                    state === "up" ? (
                      <Badge tone="success" dot>
                        UP
                      </Badge>
                    ) : (
                      <Badge tone="unknown" dot title="Gauges left untouched: an outage must not read as an empty queue.">
                        DOWN — metrics unknown
                      </Badge>
                    ),
                },
                {
                  key: "meaning",
                  header: "What its metrics mean while down",
                  cell: ([name, state]) =>
                    state === "up" ? (
                      <span className="ym-muted">live readings</span>
                    ) : (
                      <span>every gauge it owns reads UNAVAILABLE</span>
                    ),
                  hideBelow: "md",
                },
              ]}
              rowKey={([n]) => n}
              caption="Metric collectors and their state"
              empty="No collector reported"
              emptyHint="The collectors endpoint answered with nothing, so no queue metric on this screen has a source."
            />
          )}
        </QueryBoundary>
        {queueState === "unknown" ? (
          <p className="ym-error" role="alert">
            The queue-depth collector is down, so no backlog figure is shown for it.
            An empty queue and an unobservable queue are different states and only
            one of them is healthy.
          </p>
        ) : null}
      </Panel>

      <Panel title="Jobs and leases" subtitle="GET /jobs?limit=100. Ownership, lease expiry and heartbeat are on the row." dense>
        <QueryBoundary query={jobs} skeletonRows={8}>
          {(d) => (
            <DataTable
              rows={d.items ?? []}
              columns={leaseColumns}
              rowKey={(j) => j.id}
              caption="Jobs with lease ownership"
              maxHeight={560}
              empty="No job recorded"
              emptyHint="No job row exists for this workspace. A workspace with no queued work and a workspace whose queue is unobservable both look like this; the collector above distinguishes them."
            />
          )}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * GPU + renders
 * ======================================================================= */

function GpuPanel({
  health,
  alerts,
  collectors,
  overview,
}: {
  health: QueryState<Health>;
  alerts: QueryState<AlertCatalog>;
  collectors: QueryState<Collectors>;
  overview: QueryState<OpsOverview>;
}) {
  const slotState = collectorState(collectors.data, "gpu_slots");
  const starvation = alerts.data?.rules.find((r) => r.id === "gpu_queue_starvation") ?? null;

  return (
    <Panel
      title="GPU"
      subtitle="No slot state is computed here. The backend counts MEDIA_INTEL_GPU_SLOT jobs in QUEUED / RUNNING / WAITING, because a held slot is parked in WAITING deliberately; counting only QUEUED and RUNNING matches nothing and reports zero on a saturated GPU."
      dense
    >
      <Grid min={190} gap="sm">
        <StatTile
          label="GPU slot ledger"
          value={slotState === "up" ? "readable" : "UNAVAILABLE"}
          unavailable={slotState === "unknown"}
          tone={slotState === "up" ? "info" : "unknown"}
          hint={
            slotState === "unknown"
              ? "The gpu_slots collector is down and leaves its gauges untouched, so no slot count is available. It is unknown, not zero."
              : undefined
          }
          source="GET /internal/collectors"
        />
        <StatTile
          label="CUDA device present"
          value={health.data?.queue?.gpu_cuda ? "yes" : health.data === null ? undefined : "no"}
          unavailable={health.data === null}
          tone={
            health.data === null ? "neutral" : health.data.queue?.gpu_cuda ? "success" : "warning"
          }
          source="GET /system/health"
        />
        <StatTile
          label="GPU worker path"
          value={health.data?.queue?.gpu_worker ? "enabled" : health.data === null ? undefined : "disabled"}
          unavailable={health.data === null}
          tone={
            health.data === null ? "neutral" : health.data.queue?.gpu_worker ? "info" : "warning"
          }
          source="GET /system/health"
        />
        <StatTile
          label="Gpu-gated jobs waiting"
          value={
            alerts.data === null
              ? undefined
              : starvation?.observed === null || starvation === null
                ? undefined
                : starvation.observed
          }
          unavailable={alerts.data === null || starvation === null || starvation.observed === null}
          tone={
            alerts.data === null
              ? "neutral"
              : starvation && starvation.firing
                ? "danger"
                : "success"
          }
          hint={
            starvation
              ? `Backend verdict: ${starvation.firing ? "FIRING" : "not firing"} at threshold ${starvation.threshold} ${starvation.unit}.`
              : "The gpu_queue_starvation rule was not reported."
          }
          source="GET /internal/alerts"
        />
      </Grid>
      {starvation ? (
        <p className="ym-notif-detail">
          <strong>{starvation.title}</strong> (<code>{starvation.id}</code>) — detects{" "}
          {starvation.detects}.{" "}
          {starvation.firing ? (
            <Badge tone="danger">FIRING</Badge>
          ) : (
            <Badge tone="success">quiet</Badge>
          )}{" "}
          {starvation.reason ? `Reason: ${starvation.reason}` : ""} Runbook:{" "}
          {starvation.runbook}
        </p>
      ) : null}
      <SectionAlert name="jobs" section={overview.data?.jobs} />
    </Panel>
  );
}

function RendersPanel({
  health,
  overview,
  alerts,
}: {
  health: QueryState<Health>;
  overview: QueryState<OpsOverview>;
  alerts: QueryState<AlertCatalog>;
}) {
  const engineMock = health.data?.mocks.video_engine ?? false;
  const publishRule = alerts.data?.rules.find((r) => r.id === "repeated_publish_failure") ?? null;
  const exports = overview.data?.exports;

  return (
    <>
      <Panel title="Render backend" subtitle="GET /system/health. Whether the engine is real is a separate fact from whether it is up." dense>
        <QueryBoundary query={health} skeletonRows={3}>
          {(d) => (
            <Grid min={190} gap="sm">
              <StatTile
                label="Video engine"
                value={d.video_engine_name || "unknown"}
                tone={d.mocks.video_engine ? "mock" : "info"}
                hint={
                  d.mocks.video_engine
                    ? "SIMULATED: a stand-in produced this. A render from it is not a render."
                    : `Version ${d.video_engine_version ?? "unreported"}.`
                }
                source="GET /system/health"
              />
              <StatTile
                label="Engine reachable"
                value={d.mocks.video_engine ? "SIMULATED" : d.video_engine ? "yes" : "no"}
                unavailable={false}
                tone={d.mocks.video_engine ? "mock" : d.video_engine ? "success" : "danger"}
                source="GET /system/health"
              />
              <StatTile
                label="Publishing mock"
                value={d.mocks.publishing ? "SIMULATED" : "off"}
                tone={d.mocks.publishing ? "mock" : "success"}
                hint={d.mocks.publishing ? "Publishes are recorded locally and nothing is sent." : undefined}
                source="GET /system/health"
              />
            </Grid>
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Export and render failures" subtitle="The `exports` section of GET /ops/overview." dense>
        <SectionAlert name="exports" section={exports} />
        <QueryBoundary query={overview} skeletonRows={4}>
          {() => {
            const section = exports;
            if (!isAvailable(section)) {
              return (
                <p className="ym-hint">
                  Unavailable, so no export figure is shown. The zero above would have
                  been the failure value, not a measurement.
                </p>
              );
            }
            const ok = section as ExportsSection;
            return (
              <>
                <Grid min={180} gap="sm">
                  <StatTile label="Failed exports" value={ok.failed_count} tone={ok.failed_count > 0 ? "warning" : "success"} source="GET /ops/overview" />
                  <StatTile
                    label="Repeated publish failures"
                    value={alerts.data === null ? undefined : publishRule?.observed ?? undefined}
                    unavailable={alerts.data === null || publishRule === null || publishRule.observed === null}
                    tone={alerts.data === null ? "neutral" : publishRule?.firing ? "danger" : "success"}
                    source="GET /internal/alerts"
                  />
                </Grid>
                <DataTable
                  rows={ok.failed ?? []}
                  columns={[
                    { key: "id", header: "Export", cell: (e) => e.id.slice(0, 12) },
                    { key: "format", header: "Format", cell: (e) => e.format },
                    { key: "state", header: "State", cell: (e) => <StatusBadge status={e.state} /> },
                    { key: "attempt", header: "Attempt", align: "right", cell: (e) => e.attempt },
                    { key: "error", header: "Error", cell: (e) => <span className="ym-notif-detail">{e.error || "—"}</span> },
                    { key: "at", header: "Created", cell: (e) => when(e.created_at), hideBelow: "md" },
                  ]}
                  rowKey={(e) => e.id}
                  caption="Failed exports"
                  maxHeight={300}
                  empty="No export failed"
                  emptyHint="Every export in the retention window reached a terminal success. This says nothing about exports outside the window."
                />
              </>
            );
          }}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Storage + database
 * ======================================================================= */

function StoragePanel({
  overview,
  collectors,
  orphans,
  retention,
}: {
  overview: QueryState<OpsOverview>;
  collectors: QueryState<Collectors>;
  orphans: QueryState<Orphans>;
  retention: QueryState<Record<string, unknown>>;
}) {
  const storageState = collectorState(collectors.data, "storage");
  const dbState = collectorState(collectors.data, "database");
  const storage = overview.data?.storage;

  return (
    <>
      <Panel
        title="Storage"
        subtitle="The `storage` section of GET /ops/overview. It names which source answered: the database sizes, or a filesystem walk."
        dense
      >
        <SectionAlert name="storage" section={storage} />
        <QueryBoundary query={overview} skeletonRows={3}>
          {() => {
            if (!isAvailable(storage)) {
              return (
                <p className="ym-hint">
                  The storage section is unavailable, so no size is reported. A
                  reported zero here would be the failure value.
                </p>
              );
            }
            const ok = storage as StorageSection;
            return (
              <Grid min={190} gap="sm">
                <StatTile
                  label="Bytes held"
                  value={bytes(ok.bytes)}
                  source={`GET /ops/overview (${ok.source})`}
                />
                <StatTile label="Files" value={ok.file_count} source={`GET /ops/overview (${ok.source})`} />
                <StatTile
                  label="Storage collector"
                  value={storageState === "up" ? "up" : "UNAVAILABLE"}
                  unavailable={storageState === "unknown"}
                  tone={storageState === "up" ? "success" : "unknown"}
                  hint="The `storage` collector is separate from this section and is not what produced these numbers."
                  source="GET /internal/collectors"
                />
              </Grid>
            );
          }}
        </QueryBoundary>
      </Panel>

      <Panel title="Database" subtitle="Pool and orphan integrity." dense>
        <Grid min={190} gap="sm">
          <StatTile
            label="Pool collector"
            value={dbState === "up" ? "up" : "UNAVAILABLE"}
            unavailable={dbState === "unknown"}
            tone={dbState === "up" ? "success" : "unknown"}
            hint={dbState === "unknown" ? "Pool gauges are untouched, so utilisation and waits are unknown here." : undefined}
            source="GET /internal/collectors"
          />
          <StatTile
            label="Orphan sweep"
            value={
              orphans.data === null
                ? undefined
                : orphans.data.detail
                  ? "UNAVAILABLE"
                  : orphans.data.healthy
                    ? "clean"
                    : "orphans found"
            }
            unavailable={orphans.data === null}
            tone={
              orphans.data === null
                ? "neutral"
                : orphans.data.detail
                  ? "unknown"
                  : orphans.data.healthy
                    ? "success"
                    : "warning"
            }
            hint={orphans.data?.detail ?? undefined}
            source="GET /system/orphans"
          />
        </Grid>
      </Panel>

      <Panel
        title="Retention policy"
        subtitle="GET /retention. Every NULL day count means keep forever, which is a real answer and not a zero."
        dense
      >
        <QueryBoundary query={retention} skeletonRows={4}>
          {(d) => {
            const rows = Object.entries(d)
              .filter(([k]) => k !== "available")
              .map(([policy, value]) => ({ policy, value }));
            return (
              <DataTable
                rows={rows}
                columns={[
                  { key: "policy", header: "Policy", cell: (r) => humanize(r.policy) },
                  {
                    key: "value",
                    header: "Days",
                    align: "right",
                    cell: (r) =>
                      r.value === null || r.value === undefined ? (
                        <Badge tone="info" title="A NULL day count keeps rows forever.">
                          KEEP FOREVER
                        </Badge>
                      ) : (
                        String(r.value)
                      ),
                  },
                ]}
                rowKey={(r) => r.policy}
                caption="Retention policy"
                empty="No retention policy reported"
                emptyHint="The route answered with no fields, so no retention window can be stated. Do not read that as 'delete everything' or 'keep everything'."
              />
            );
          }}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Providers
 * ======================================================================= */

function ProvidersPanel({ health, overview }: { health: QueryState<Health>; overview: QueryState<OpsOverview> }) {
  const providerHealth = overview.data?.provider_health;
  const rows = useMemo(() => {
    const publishers = health.data?.publishers ?? {};
    return Object.entries(publishers).map(([platform, state]) => ({ platform, state }));
  }, [health.data]);

  return (
    <>
      <Panel
        title="Publishing platforms"
        subtitle="GET /system/health. `mode` is real or mock per platform and is never inferred from readiness."
        dense
      >
        <QueryBoundary query={health} skeletonRows={4}>
          {() => (
            <DataTable
              rows={rows}
              columns={[
                { key: "platform", header: "Platform", cell: (r) => <strong>{humanize(r.platform)}</strong> },
                {
                  key: "mode",
                  header: "Publish path",
                  /* NOT `ModeBadge`. `_publisher_status` returns
                   * `mode: "mock" | "real"` -- the DEPLOYMENT path, which is a
                   * different vocabulary from the publication-mode trio
                   * LIVE / MOCK / HANDOFF that `ModeBadge` exists for. Using it
                   * here would label a configured channel "Publication mode:
                   * real" and give `real` the `live` tone, which
                   * `engine/distribution/modes.py` reserves for "an official API
                   * returned a remote id". A mock path gets the `mock` tone and a
                   * real path gets `info`, so the two can never share a colour. */
                  cell: (r) =>
                    r.state.mode === "mock" ? (
                      <Badge tone="mock" dot title="Nothing is sent to this platform; a local stand-in records the intent.">
                        MOCK PATH
                      </Badge>
                    ) : (
                      <Badge tone="info" dot title="A configured real path. This says nothing about whether a publish has succeeded.">
                        REAL PATH
                      </Badge>
                    ),
                },
                {
                  key: "ready",
                  header: "Configured",
                  cell: (r) =>
                    r.state.ready ? (
                      <Badge tone="success" dot>
                        yes
                      </Badge>
                    ) : (
                      <Badge tone="warning" dot>
                        no
                      </Badge>
                    ),
                },
                { key: "detail", header: "Why", cell: (r) => <span className="ym-notif-detail">{r.state.detail}</span> },
                {
                  key: "relay",
                  header: "Via relay",
                  cell: (r) =>
                    r.state.via_relay ? (
                      <Badge tone="info" title="No connected account; a third-party relay is used instead.">
                        RELAY
                      </Badge>
                    ) : (
                      <span className="ym-muted">direct</span>
                    ),
                  hideBelow: "md",
                },
              ]}
              rowKey={(r) => r.platform}
              caption="Publishing platform readiness"
              empty="No publishing platform reported"
              emptyHint="The health route named no publisher. That is not evidence that publishing works."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Readiness probes"
        subtitle="The `provider_health` section of GET /ops/overview — the same probes, with their latency."
        dense
      >
        <SectionAlert name="provider_health" section={providerHealth} />
        <QueryBoundary query={overview} skeletonRows={6}>
          {() => {
            if (!isAvailable(providerHealth)) return <p className="ym-hint">Unavailable; no probe figure is shown.</p>;
            const ok = providerHealth as ProviderHealthSection;
            return (
              <DataTable
                rows={ok.checks ?? []}
                columns={[
                  { key: "id", header: "Probe", cell: (c) => humanize(c.id) },
                  {
                    key: "status",
                    header: "Status",
                    cell: (c) => (
                      <Badge tone={c.status === "passed" ? "success" : "danger"} dot>
                        {humanize(c.status)}
                      </Badge>
                    ),
                  },
                  {
                    key: "blocking",
                    header: "Blocking",
                    cell: (c) => (c.blocking ? <Badge tone="danger">yes</Badge> : <Badge tone="neutral">no</Badge>),
                  },
                  { key: "latency", header: "Latency ms", align: "right", cell: (c) => c.latency_ms, hideBelow: "md" },
                ]}
                rowKey={(c) => c.id}
                caption="Provider readiness probes"
                empty="No provider probe reported"
                emptyHint="The probe list is empty, so no provider is verified. Empty is not healthy."
              />
            );
          }}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * SLO + alerts
 * ======================================================================= */

function AlertsPanel({ alerts, slo, collectors }: { alerts: QueryState<AlertCatalog>; slo: QueryState<SloCatalog>; collectors: QueryState<Collectors> }) {
  const rules = alerts.data?.rules ?? [];

  return (
    <>
      <Panel
        title="Alert verdicts"
        subtitle="GET /internal/alerts. Definitions and verdicts are separate: this says what would page you and whether it is paging now."
        dense
      >
        <Grid min={190} gap="sm">
          <StatTile
            label="Firing"
            value={alerts.data?.firing_count}
            unavailable={alerts.data === null}
            tone={alerts.data === null ? "neutral" : alerts.data.firing_count > 0 ? "danger" : "success"}
            source="GET /internal/alerts"
          />
          <StatTile
            label="Rules evaluated"
            value={alerts.data === null ? undefined : rules.length}
            unavailable={alerts.data === null}
            source="GET /internal/alerts"
          />
        </Grid>
        <QueryBoundary query={alerts} skeletonRows={8}>
          {() => (
            <DataTable
              rows={rules}
              columns={[
                { key: "id", header: "Rule", cell: (r) => <strong>{r.id}</strong> },
                {
                  key: "severity",
                  header: "Severity",
                  cell: (r) => (
                    <Badge tone={r.severity === "critical" ? "danger" : r.severity === "warning" ? "warning" : "neutral"}>
                      {humanize(r.severity)}
                    </Badge>
                  ),
                },
                {
                  key: "firing",
                  header: "Now",
                  cell: (r) =>
                    r.firing ? (
                      <Badge tone="danger" dot>
                        FIRING
                      </Badge>
                    ) : (
                      <Badge tone="success" dot>
                        quiet
                      </Badge>
                    ),
                },
                {
                  key: "observed",
                  header: "Observed",
                  align: "right",
                  cell: (r) =>
                    r.observed === null ? (
                      <span className="ym-muted">UNAVAILABLE</span>
                    ) : (
                      `${r.observed} ${r.unit}`
                    ),
                },
                {
                  key: "threshold",
                  header: "Threshold",
                  align: "right",
                  cell: (r) => `${r.threshold} ${r.unit}`,
                  hideBelow: "md",
                },
                { key: "detects", header: "Detects", cell: (r) => <span className="ym-notif-detail">{r.detects}</span>, hideBelow: "lg" },
                {
                  key: "runbook",
                  header: "Runbook",
                  cell: (r) => <span className="ym-notif-detail">{r.runbook}</span>,
                  hideBelow: "lg",
                },
              ]}
              rowKey={(r) => r.id}
              caption="Alert rules and their live verdicts"
              maxHeight={560}
              empty="No alert rule evaluated"
              emptyHint="No rule produced a verdict, so nothing is known about queue depth, GPU pressure, worker fleet or unknown exposure. That is not the same as everything being fine."
            />
          )}
        </QueryBoundary>
        {alerts.data?.note ? <p className="ym-hint">{alerts.data.note}</p> : null}
      </Panel>

      <Panel
        title="SLO targets"
        subtitle="GET /internal/slo. These are TARGETS. No achieved value is claimed anywhere on this screen."
        dense
      >
        <QueryBoundary query={slo} skeletonRows={5}>
          {(d) => (
            <>
              <p className="ym-error">
                {d.measured ? "MEASURED — read the targets below as current values." : "TARGETS ONLY — no achieved SLO is reported."}{" "}
                {d.disclaimer}
              </p>
              <DataTable
                rows={d.targets ?? []}
                columns={[
                  {
                    key: "target",
                    header: "Objective",
                    cell: (t) => (
                      <span className="ym-notif-detail">{JSON.stringify(t).slice(0, 160)}</span>
                    ),
                  },
                ]}
                rowKey={(t, i) => String((t as { id?: unknown }).id ?? (t as { name?: unknown }).name ?? i)}
                caption="SLO targets"
                empty="No SLO target declared"
                emptyHint="The registry declares no objective, so there is nothing to hold this deployment to."
              />
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Collectors behind these numbers" subtitle="Which readings exist right now." dense>
        <QueryBoundary query={collectors} skeletonRows={2}>
          {(d) => (
            <p className="ym-notif-detail">
              {Object.entries(d.collectors ?? {})
                .map(([name, state]) => `${humanize(name)}: ${state}`)
                .join(" · ")}
              {d.failed.length > 0 ? ` — ${d.failed.length} down, so their gauges read UNAVAILABLE.` : ""}
            </p>
          )}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Cost + reconciliation
 * ======================================================================= */

function CostPanel({ costs, intel }: { costs: QueryState<CostSummary>; intel: QueryState<CostIntelligence> }) {
  const categories = useMemo(
    () => Object.entries(costs.data?.last_24h_by_category ?? {}),
    [costs.data],
  );

  return (
    <>
      <Panel title="Spend against budget" subtitle="GET /costs. The window is the last 24 hours, not a calendar day." dense>
        <QueryBoundary query={costs} skeletonRows={4}>
          {(d) => (
            <>
              <Grid min={190} gap="sm">
                {/* HONESTY (§8): this panel reads GET /costs, which IS fixed —
                    a null total renders UNAVAILABLE, not $0.00. The
                    remaining/gate tiles keep their verdict on purpose. */}
                <StatTile
                  label="Spent, last 24h"
                  value={<Money usd={d.spent_last_24h_usd} />}
                  unavailable={d.spent_last_24h_usd === null}
                  hint={
                    d.spent_last_24h_usd === null
                      ? `${d.spent_last_24h_unknown_exposure_rows} cost row(s) record an exposure nobody can price.`
                      : undefined
                  }
                  source="GET /costs"
                />
                <StatTile label="Daily budget" value={<Money usd={d.daily_budget_usd} />} source="GET /costs" />
                <StatTile
                  label="Remaining"
                  value={<Money usd={d.remaining_usd} tone={d.within_budget ? "success" : "danger"} />}
                  tone={d.within_budget ? "success" : "danger"}
                  source="GET /costs"
                />
                <StatTile
                  label="Per-video budget"
                  value={<Money usd={d.per_video_budget_usd} />}
                  source="GET /costs"
                />
              </Grid>
              <DataTable
                rows={categories}
                columns={[
                  { key: "cat", header: "Category", cell: ([c]) => humanize(c) },
                  { key: "amount", header: "Last 24h", align: "right", cell: ([, v]) => <Money usd={v} /> },
                ]}
                rowKey={([c]) => c}
                caption="Spend by category, last 24 hours"
                empty="No cost recorded in this window"
                emptyHint="Nothing is ledgered against this workspace in the last 24 hours. An empty ledger during a paid operation is a reason to look at whether the cost was written, not proof that nothing was spent."
              />
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Economics"
        subtitle="GET /costs/intelligence. Every per-unit figure is null when its denominator is zero — that is UNAVAILABLE, not $0."
        dense
      >
        <QueryBoundary query={intel} skeletonRows={5}>
          {(d) => (
            <>
              <Grid min={190} gap="sm">
                {/* NOT FIXED (§8, out of scope): `safety.py` still reports
                    $0.0000 for an empty ledger, so this tile shows a real
                    number that may be wrong. Rendered plainly rather than
                    dressed as UNAVAILABLE, because claiming UNAVAILABLE here
                    would be a THIRD answer the endpoint cannot produce. The gap
                    is recorded in docs/ANALYTICS_HONESTY_AUDIT.json. */}
                <StatTile
                  label="Total spend, all time"
                  value={<Money usd={d.total_cost_usd} />}
                  hint="Known fabrications: an empty ledger reads $0.0000, and an unpriceable exposure is dropped from this total."
                  source="GET /costs/intelligence"
                />
                <StatTile
                  label="Per completed cycle"
                  value={d.per_cycle_usd === null ? <span className="ym-muted">UNAVAILABLE</span> : <Money usd={d.per_cycle_usd} />}
                  unavailable={d.per_cycle_usd === null}
                  hint="No completed cycle, so there is no average."
                  source="GET /costs/intelligence"
                />
                <StatTile
                  label="Per video"
                  value={d.per_video_usd === null ? <span className="ym-muted">UNAVAILABLE</span> : <Money usd={d.per_video_usd} />}
                  unavailable={d.per_video_usd === null}
                  source="GET /costs/intelligence"
                />
                <StatTile
                  label="Cost per 1,000 views"
                  value={
                    d.cost_per_1000_views_usd === null ? (
                      <span className="ym-muted">UNAVAILABLE</span>
                    ) : (
                      <Money usd={d.cost_per_1000_views_usd} />
                    )
                  }
                  unavailable={d.cost_per_1000_views_usd === null}
                  source="GET /costs/intelligence"
                />
                <StatTile
                  label="Estimated return"
                  value={
                    <span className="ym-muted" title={d.estimated_return_note}>
                      NOT AVAILABLE
                    </span>
                  }
                  unavailable
                  tone="unknown"
                  hint={d.estimated_return_note}
                  source="GET /costs/intelligence"
                />
              </Grid>
              <DataTable
                rows={Object.entries(d.by_agent ?? {})}
                columns={[
                  { key: "agent", header: "Agent", cell: ([a]) => humanize(a) },
                  { key: "cost", header: "Spend", align: "right", cell: ([, v]) => <Money usd={v} /> },
                ]}
                rowKey={([a]) => a}
                caption="Spend by agent"
                empty="No agent spend recorded"
                emptyHint="No AgentRun row carries a cost for this workspace."
              />
            </>
          )}
        </QueryBoundary>
      </Panel>
    </>
  );
}

function ReconciliationPanel({ query }: { query: QueryState<PaidIncidents> }) {
  return (
    <Panel
      title="Paid reconciliation"
      subtitle="SUBMISSION_UNKNOWN means the provider may have accepted and billed the request. UNKNOWN_EXPOSURE means an accepted call nobody can price. Both are drawn on the `unknown` tone and neither is retryable from here."
      dense
    >
      <Grid min={190} gap="sm">
        <StatTile
          label="Awaiting reconciliation"
          value={query.data?.count}
          unavailable={query.data === null}
          source="GET /provider-maturity/incidents"
        />
        <StatTile
          label="Unknown exposure"
          value={query.data?.unknown_exposure_count}
          unavailable={query.data === null}
          tone={query.data === null ? "neutral" : "unknown"}
          source="GET /provider-maturity/incidents"
        />
      </Grid>
      <QueryBoundary query={query} skeletonRows={5}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={[
              { key: "at", header: "Attempted", cell: (i) => when(i.attempted_at), width: "190px" },
              { key: "provider", header: "Provider", cell: (i) => i.provider },
              { key: "op", header: "Operation", cell: (i) => <span className="ym-notif-detail">{i.operation}</span>, hideBelow: "md" },
              {
                key: "state",
                header: "State",
                cell: (i) => (
                  <Badge tone={toneForStatus(i.state)} dot title={i.note || undefined}>
                    {i.state}
                  </Badge>
                ),
              },
              {
                key: "exposure",
                header: "Exposure",
                cell: (i) =>
                  i.exposure_unknown ? (
                    <Badge tone="unknown">{i.exposure}</Badge>
                  ) : (
                    <Money usd={i.estimated_exposure_usd} />
                  ),
              },
              { key: "action", header: "Action", cell: (i) => <Badge tone="info">{humanize(i.recommended_action)}</Badge> },
              {
                key: "remote",
                header: "Remote id",
                cell: (i) =>
                  i.remote_id ? (
                    <span className="ym-notif-detail">{i.remote_id.slice(0, 18)}</span>
                  ) : (
                    <span className="ym-muted" title="Nothing to reconcile against.">
                      none returned
                    </span>
                  ),
                hideBelow: "md",
              },
              {
                key: "resend",
                header: "Safe to resend",
                cell: (i) =>
                  i.retry_safe && i.may_resubmit ? (
                    <Badge tone="success">SAFE</Badge>
                  ) : (
                    <Badge tone="unknown">NO — RECONCILE</Badge>
                  ),
              },
              {
                key: "detail",
                header: "Evidence",
                cell: (i) => <span className="ym-notif-detail">{(i.detail || "—").slice(0, 70)}</span>,
                hideBelow: "lg",
              },
            ]}
            rowKey={(i) => i.incident_id}
            caption="Paid submissions awaiting reconciliation"
            maxHeight={480}
            empty="No paid submission is awaiting reconciliation"
            emptyHint="Only genuinely ambiguous submissions appear here. A confirmed rejection is a FAILED job and is deliberately absent."
          />
        )}
      </QueryBoundary>
      {query.data?.note ? <p className="ym-hint">{query.data.note}</p> : null}
    </Panel>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

type Tab =
  | "system"
  | "workers"
  | "gpu"
  | "renders"
  | "storage"
  | "providers"
  | "alerts"
  | "cost"
  | "reconciliation";

export default function Operations() {
  const { workspaceId, workspace } = useSession();
  const [tab, setTab] = useState<Tab>("system");

  const live = useLiveness();
  const readiness = useGlobalQuery<Readiness>("/system/readiness");
  const health = useGlobalQuery<Health>("/system/health");
  const mode = useGlobalQuery<SystemMode>("/system/mode");
  const doctor = useGlobalQuery<Doctor>("/system/doctor");
  const orphans = useGlobalQuery<Orphans>("/system/orphans");
  const collectors = useCollectors();
  const alerts = useAlerts();
  const slo = useSlo();

  const overview = useWsQuery<OpsOverview>("/ops/overview");
  const jobs = useWsQuery<{ items: JobRow[] }>("/jobs?limit=100");
  const costs = useWsQuery<CostSummary>("/costs");
  const intel = useWsQuery<CostIntelligence>("/costs/intelligence");
  const incidents = useWsQuery<PaidIncidents>("/provider-maturity/incidents");
  const retention = useWsQuery<Record<string, unknown>>("/retention");

  const reloadAll = () => {
    live.reload();
    readiness.reload();
    health.reload();
    mode.reload();
    doctor.reload();
    orphans.reload();
    collectors.reload();
    alerts.reload();
    slo.reload();
    overview.reload();
    jobs.reload();
    costs.reload();
    intel.reload();
    incidents.reload();
    retention.reload();
  };

  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "system", label: "System" },
    { id: "workers", label: "Workers & Queues", count: jobs.data?.items.length },
    { id: "gpu", label: "GPU" },
    { id: "renders", label: "Renders" },
    { id: "storage", label: "Storage & Database" },
    { id: "providers", label: "Providers" },
    { id: "alerts", label: "SLO & Alerts", count: alerts.data?.firing_count },
    { id: "cost", label: "Cost" },
    { id: "reconciliation", label: "Reconciliation", count: incidents.data?.count },
  ];

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Operations" description="System, workers, GPU, cost and paid reconciliation." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to report on"
            description="The workspace-scoped panels here need a workspace. Process-level probes would still answer, but their verdict about THIS tenant would be meaningless."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Operations"
        description={
          workspace?.name
            ? `${workspace.name} — process health, queue state, and money that may already be spent.`
            : "Process health, queue state, and money that may already be spent."
        }
        actions={<Button onClick={reloadAll}>Refresh</Button>}
      />

      <AtAGlance
        liveness={live}
        readiness={readiness}
        health={health}
        collectors={collectors}
        alerts={alerts}
        costs={costs}
        incidents={incidents}
      />

      <Tabs tabs={tabs} active={tab} onChange={(id) => setTab(id as Tab)} />

      {tab === "system" ? (
        <SystemPanel
          liveness={live}
          readiness={readiness}
          health={health}
          mode={mode}
          doctor={doctor}
          orphans={orphans}
        />
      ) : null}
      {tab === "workers" ? <JobsPanel jobs={jobs} collectors={collectors} /> : null}
      {tab === "gpu" ? (
        <GpuPanel health={health} alerts={alerts} collectors={collectors} overview={overview} />
      ) : null}
      {tab === "renders" ? <RendersPanel health={health} overview={overview} alerts={alerts} /> : null}
      {tab === "storage" ? (
        <StoragePanel
          overview={overview}
          collectors={collectors}
          orphans={orphans}
          retention={retention}
        />
      ) : null}
      {tab === "providers" ? <ProvidersPanel health={health} overview={overview} /> : null}
      {tab === "alerts" ? (
        <AlertsPanel alerts={alerts} slo={slo} collectors={collectors} />
      ) : null}
      {tab === "cost" ? <CostPanel costs={costs} intel={intel} /> : null}
      {tab === "reconciliation" ? <ReconciliationPanel query={incidents} /> : null}
    </>
  );
}

/**
 * A process-scoped probe, mounted at the ROOT rather than under `/api/v1`.
 *
 * WORK 16.5.3 §13: this used to be a local
 * `processProbe<T>(path: string)` accepting ANY string and doing a bare
 * `fetch(...).then(r => r.json() as Promise<T>)`. That is precisely the
 * "arbitrary untyped fetch" the work order removes -- a typo compiled, an
 * unauthenticated request left the browser, and nothing failed.
 *
 * The mechanic now lives in `api/internalOps.ts`, where the path set is a
 * frozen const object and inventing a path is a TYPE ERROR. The rationale for
 * these routes being root-mounted and token-free is documented there too,
 * because it is a property of the backend, not of this screen.
 *
 * Anything workspace-scoped on this screen still goes through `api()` and the
 * shared query layer like every other screen.
 */
function processProbe<T>(path: InternalOpsPath): Promise<T> {
  return fetchInternalOps<T>(path);
}

/** A non-workspace-scoped GET under `/api/v1`. */
function useGlobalQuery<T>(path: string): QueryState<T> {
  return useQuery<T>(() => api("GET", path));
}

/** `/livez` — process liveness, mounted at the root. */
function useLiveness(): QueryState<Liveness> {
  return useQuery<Liveness>(() => processProbe<Liveness>(INTERNAL_OPS_PATH.liveness));
}

/** `/internal/alerts` — live verdicts, mounted at the root. */
function useAlerts(): QueryState<AlertCatalog> {
  return useQuery<AlertCatalog>(() => processProbe<AlertCatalog>(INTERNAL_OPS_PATH.alerts));
}

/** `/internal/slo` — targets only, mounted at the root. */
function useSlo(): QueryState<SloCatalog> {
  return useQuery<SloCatalog>(() => processProbe<SloCatalog>(INTERNAL_OPS_PATH.slo));
}

/** `/internal/collectors` — which metric collectors are up, mounted at the root. */
function useCollectors(): QueryState<Collectors> {
  return useQuery<Collectors>(() => processProbe<Collectors>(INTERNAL_OPS_PATH.collectors));
}
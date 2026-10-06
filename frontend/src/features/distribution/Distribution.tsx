/* Distribution — the last screen before a post leaves the building.
 *
 *   GET  /publishing/accounts                          api/v1/misc.py  list_accounts
 *   GET  /publishing/jobs?limit=…                      api/v1/misc.py  list_publishing_jobs
 *   GET  /publishing/posts?limit=…                     api/v1/misc.py  list_published_posts
 *   GET  /distribution/capabilities                    api/v1/distribution.py  capabilities
 *   GET  /distribution/platforms                       api/v1/distribution.py  list_platforms
 *   GET  /planner/calendar?days=…                      api/v1/planner.py       calendar
 *   GET  /provider-maturity                            api/v1/providers.py     workspace_provider_maturity
 *   GET  /provider-maturity/incidents?limit=…          api/v1/providers.py     workspace_paid_incidents
 *   GET  /system/health                                api/v1/misc.py  health
 *   POST /publishing/oauth/{platform}/start            api/v1/misc.py  oauth_*_start
 *   DELETE /publishing/accounts/{account_id}           api/v1/misc.py  disconnect_account
 *
 * ============================ THE CLAIM THIS SCREEN MAKES ============================
 *
 * "Published" is not a fact. `engine/distribution/modes.py` defines four outcomes
 * and `classify_publication` is the only thing allowed to decide between them:
 *
 *     if unavailable_reason:  UNAVAILABLE
 *     elif handoff_required:  MOCK if is_mock else HANDOFF
 *     elif is_mock:           MOCK
 *     else:                   LIVE if remote_id else UNAVAILABLE
 *
 * So the mode is reproduced here from the three facts the API actually returns —
 * `is_mock`, the publisher registry's `user_handoff`, and the remote pointer — in
 * that same precedence. Two consequences are load-bearing:
 *
 *   1. A row with no remote pointer on a DIRECT_PUBLISH platform is
 *      UNAVAILABLE, NOT LIVE. Claiming LIVE there is the Work 14 §5 FAILED case.
 *   2. A mock never carries a remote pointer and a handoff never claims LIVE, so
 *      the four modes stay separable instead of collapsing into one green badge.
 *
 * This is deliberately STRICTER than `Campaigns`'s list version, which reports
 * UNAVAILABLE for every non-mock row because `/publishing/posts` looked too
 * coarse to separate LIVE from HANDOFF. `remote_url` is the remote pointer
 * `classify_publication` names `remote_id`, and `remote_post_id` is written from
 * it (`publish_flow.py`), so it is the same fact read from the payload. Where it
 * is empty the mode stays UNAVAILABLE. Nothing here is guessed from a platform
 * name: handoff comes from `HANDOFF_PLATFORMS` via `/distribution/capabilities`.
 *
 * ============================ THE MONEY RULE =======================================
 *
 * `GET /provider-maturity/incidents` is where an ambiguous paid submission lives.
 * The backend already refuses to call SUBMISSION_UNKNOWN a failure and already
 * computes `retry_safe` from a provably-undelivered submit
 * (`services/paid_executor.RetrySafety`). This screen therefore renders the
 * incident's own verdict and offers NO resubmission affordance of any kind. The
 * only "Retry" this file can produce is the one inside `QueryBoundary`, and that
 * is a re-read of a GET — it sends nothing and spends nothing.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  DestructiveButton,
  EmptyState,
  Grid,
  ModeBadge,
  Money,
  Panel,
  PageHeader,
  QueryBoundary,
  Select,
  StatTile,
  StatusBadge,
  Tabs,
  cx,
  humanize,
  toneForStatus,
  type Column,
} from "../../design-system/primitives";
import { useWsQuery, type QueryState } from "../../api/queries";
import { api, wsApi } from "../../lib/api";
import { blockedReason, can, useSession } from "../../state/session";
import type { ResolvedMode } from "../campaigns/Campaigns";

/* ==========================================================================
 * Response shapes — every one typed from the router that builds it
 * ======================================================================= */

/* `publishing_router.list_accounts` (misc.py) over `models.SocialAccount`.
   `external_id` is already truncated server-side to `xxxxxx…`. */
export type SocialAccountRow = {
  id: string;
  platform: string;
  display_name: string;
  external_id: string;
  /** `connected` | `error` — whatever the row carries, not a narrowed enum. */
  status: string;
  /** null when the platform issued no expiry (an app-password grant). */
  token_expires_at: string | null;
};
export type SocialAccountList = { items: SocialAccountRow[] };

/* `publishing_router.list_publishing_jobs` (misc.py) over
   `models.PublishingJob` LEFT JOINed to `models.ContentItem`. */
export type PublishingJobRow = {
  id: string;
  platform: string;
  status: string;
  remote_url: string;
  remote_post_id: string;
  attempt: number;
  error: string;
  content_item_id: string | null;
  content_topic: string | null;
  scheduled_at: string | null;
  published_at: string | null;
  created_at: string;
};
export type PublishingJobList = { items: PublishingJobRow[] };

/* `publishing_router.list_published_posts` (misc.py) over
   `models.PublishedPost` + the latest `models.PostMetric` per post.

   NOTE: it does NOT return `publication_mode`, `remote_post_id`,
   `campaign_id` or `handoff_payload`, so none of those is read here. */
export type PublishedPostRow = {
  id: string;
  platform: string;
  title: string;
  remote_url: string;
  published_at: string | null;
  /** Cannot express HANDOFF — the model says so on the column. */
  is_mock: boolean;
  metrics: {
    views: number;
    likes: number;
    comments: number;
    /** null when no provider reported one — UNAVAILABLE, not 0%. */
    completion_rate: number | null;
  };
};
export type PublishedPostList = { items: PublishedPostRow[] };

/* `distribution_router.capabilities` (distribution.py). `_publish_mode` derives
   the string from `providers.publishers.factory.HANDOFF_PLATFORMS`. */
export type DistributionCapabilityRow = {
  platform: string;
  capabilities: string[];
  /** `USER_HANDOFF` | `DIRECT_PUBLISH` */
  publish_mode: string;
  direct_publish: boolean;
  user_handoff: boolean;
  supports_inbox: boolean;
  supports_analytics: boolean;
  campaign_platforms: string[];
  media: string | null;
  metadata_limits: string | null;
};
export type DistributionCapabilityList = { items: DistributionCapabilityRow[] };

/* `distribution_router.list_platforms` (distribution.py) over
   `engine.distribution.profiles`. `unverified` is the load-bearing list. */
export type PlatformProfileRow = {
  platform: string;
  media_types: string[];
  capabilities: string[];
  publish_mode: string;
  verified_limits: string[];
  /** Documented-by-nobody fields. Rendered as unknown, never as a limit. */
  unverified: string[];
  verified_notes: string[];
};
export type PlatformProfileList = { items: PlatformProfileRow[] };

/* `planner_router.calendar` (planner.py) over `models.ScheduleEntry`. */
export type ScheduleEntryRow = {
  id: string;
  platform: string;
  /** Naive UTC in the column; the backend appends nothing. */
  run_at: string;
  status: string;
  content_item_id: string | null;
  campaign_id: string | null;
};
export type CalendarPayload = {
  workspace_id: string;
  days: number;
  entries: ScheduleEntryRow[];
  capacity: {
    /** false means UNBOUNDED, which is not zero. */
    declared: boolean;
    locale: string;
    longform_per_week: number;
    shorts_per_day: number;
    ugc_per_day: number;
    localization_per_day: number;
    render_hours_per_day: number;
    review_slots_per_day: number;
    notes: string;
  };
  committed: Record<string, number>;
  remaining: Record<string, number>;
  plan_item_count: number;
  note: string;
};

/* `workspace_paid_incidents` (api/v1/providers.py) over `models.Video` and
   `models.CostEntry`. `state` is VERBATIM — never mapped onto a success/failure
   pair — and `retry_safe` is the backend's own provable-unproven verdict. */
export type PaidIncidentRow = {
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
export type PaidIncidentList = {
  workspace_id: string;
  items: PaidIncidentRow[];
  count: number;
  unknown_exposure_count: number;
  states: string[];
  note: string;
};

/* `workspace_provider_maturity` (api/v1/providers.py) over
   `providers.maturity.ProviderMaturity.to_dict()` plus `_records`' additions. */
export type MaturityRow = {
  provider: string;
  capability: string;
  implementation_status: string;
  contract_status: string;
  live_status: string;
  commercial_status: string;
  credential_status: string;
  health: string;
  last_verified_at: string;
  credential_keys: string[];
  simulation_only: boolean;
  notes: string;
  evidence: string[];
  gaps: string[];
  production_ready: boolean;
  blockers: string[];
};
export type MaturityList = {
  workspace_id: string;
  items: MaturityRow[];
  count: number;
  resolved_credential_states: string[];
  note: string;
  credential_summary: Record<string, number>;
};

/* `system_router.health` (misc.py). Not workspace-scoped: it reports process
   configuration, so it is read through `api`, not `wsApi`. */
export type PublisherHealth = {
  mode: string;
  ready: boolean;
  detail: string;
  via_relay: boolean;
};
export type SystemHealth = {
  status: string;
  publishers: Record<string, PublisherHealth>;
  mocks: { llm: boolean; trends: boolean; publishing: boolean; analytics: boolean; video_engine: boolean };
  time: string;
};

/* ==========================================================================
 * Publication mode
 * ======================================================================= */

/**
 * The four buckets, always all four. A mode that is absent must be VISIBLY
 * absent, so the shape never shrinks.
 */
export type ModeCounts = Record<ResolvedMode, number>;

export function emptyModeCounts(): ModeCounts {
  return { LIVE: 0, MOCK: 0, HANDOFF: 0, UNAVAILABLE: 0 };
}

/**
 * Reproduce `classify_publication` from the facts `/publishing/posts` returns.
 *
 * `handoffOnly` is `DistributionCapabilityRow.user_handoff`, i.e. the publisher
 * registry's `HANDOFF_PLATFORMS` — never a platform-name check.
 */
export function resolvePostMode(
  post: Pick<PublishedPostRow, "is_mock" | "remote_url">,
  handoffOnly: boolean,
): ResolvedMode {
  if (post.is_mock) return "MOCK";
  if (handoffOnly) return "HANDOFF";
  // `classify_publication` reads `remote_id`; the payload's remote pointer is
  // `remote_url`. No pointer, no proof — and NO pointer is not a failure.
  return post.remote_url ? "LIVE" : "UNAVAILABLE";
}

/**
 * LIVE / MOCK / HANDOFF get three distinct design-system tones through
 * `ModeBadge`. UNAVAILABLE is NOT one of them and must never borrow a colour:
 * it is its own `unknown` badge.
 */
export function ModeCell({ mode }: { mode: ResolvedMode }) {
  if (mode === "UNAVAILABLE") {
    return (
      <Badge
        tone="unknown"
        title="No publication_mode is exposed by the API, and this row carries neither a mock flag nor a remote pointer — so the outcome is undecidable and is reported as such."
      >
        MODE UNAVAILABLE
      </Badge>
    );
  }
  return <ModeBadge mode={mode} />;
}

/** Terminal-success statuses. Anything else is not a delivery. */
const DELIVERED = ["PUBLISHED", "SUCCEEDED", "SUCCESS", "COMPLETED", "DONE"];
const FAILED_JOB = ["FAILED", "ERROR", "DEAD"];

/* ==========================================================================
 * Small helpers
 * ======================================================================= */

function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("Z", " UTC").replace("T", " ");
}

function joinTokens(values: string[] | null | undefined, empty: string): string {
  const list = (values ?? []).filter((v) => typeof v === "string" && v.trim() !== "");
  return list.length ? list.join(", ") : empty;
}

function BadgeRow({ values, tone }: { values: string[]; tone?: "info" | "neutral" | "warning" }) {
  if (!values.length) return <span className="ym-muted">none</span>;
  return (
    <>
      {values.map((v) => (
        <Badge key={v} tone={tone ?? "neutral"}>
          {humanize(v)}
        </Badge>
      ))}
    </>
  );
}

/* ==========================================================================
 * Accounts + readiness
 * ======================================================================= */

/* `ConnectAccountBody.platform` pattern in misc.py — the backend 400s anything
   outside this set, so the buttons are generated from it. */
const OAUTH_PLATFORMS = ["youtube", "tiktok", "facebook", "instagram"] as const;

function AccountsPanel({
  accounts,
  health,
  onConnected,
}: {
  accounts: QueryState<SocialAccountList>;
  health: { data: SystemHealth | null; error: string | null };
  onConnected: () => void;
}) {
  const mayConnect = can("publish.execute");
  const connectBlock = blockedReason("publish.execute");
  const mayDisconnect = can("publish.execute");
  const disconnectBlock = blockedReason("publish.execute");

  return (
    <Panel
      title="Accounts and readiness"
      subtitle="Connected accounts, plus whether a real publish path exists per platform."
      dense
    >
      <Grid min={200} gap="sm">
        {OAUTH_PLATFORMS.map((platform) => {
          const info = health.data?.publishers?.[platform];
          return (
            <div key={platform} className={cx("ym-stat", "ym-tone-neutral")}>
              <div className="ym-stat-label">{humanize(platform)}</div>
              {health.error ? (
                <div className="ym-stat-value ym-stat-value--unavailable">UNAVAILABLE</div>
              ) : info === undefined ? (
                <div className="ym-stat-value ym-stat-value--unavailable" title="Not reported by GET /system/health">
                  UNAVAILABLE
                </div>
              ) : (
                <div className="ym-stat-value">{info.mode === "mock" ? "MOCK" : "REAL PATH"}</div>
              )}
              <div className="ym-stat-hint">{info?.detail ?? (health.error ? "Readiness probe failed" : "Not reported")}</div>
              <div className="ym-stat-source">GET /system/health</div>
              {mayConnect ? (
                <ConnectButton platform={platform} onConnected={onConnected} />
              ) : (
                <p className="ym-hint">{connectBlock}</p>
              )}
            </div>
          );
        })}
      </Grid>

      {health.data?.mocks?.publishing ? (
        <p className="ym-error">
          <strong>MOCK_PUBLISHING is on.</strong> Nothing this workspace publishes
          reaches a platform. A publication recorded now is a MOCK and will never
          carry a remote pointer.
        </p>
      ) : null}

      <QueryBoundary query={accounts} skeletonRows={3}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            rowKey={(a) => a.id}
            caption="Connected social accounts"
            maxHeight={300}
            empty="No account connected"
            emptyHint="A publish is only LIVE when a platform API holds a real token. Until then every platform is a handoff or a mock."
            columns={[
              { key: "platform", header: "Platform", cell: (a) => humanize(a.platform) },
              {
                key: "account",
                header: "Account",
                cell: (a) => (
                  <span>
                    <strong>{a.display_name || a.platform}</strong>
                    {a.external_id ? (
                      <span className="ym-notif-detail"> · {a.external_id}</span>
                    ) : (
                      <span className="ym-muted" title="No external id was recorded for this account">
                        {" "}
                        · no external id
                      </span>
                    )}
                  </span>
                ),
              },
              { key: "status", header: "Status", cell: (a) => <StatusBadge status={a.status} /> },
              {
                key: "expiry",
                header: "Token expires",
                cell: (a) =>
                  a.token_expires_at ? (
                    when(a.token_expires_at)
                  ) : (
                    <span className="ym-muted" title="The platform issued no expiry for this token">
                      NONE REPORTED
                    </span>
                  ),
                hideBelow: "md",
              },
              {
                key: "action",
                header: "",
                align: "right",
                cell: (a) =>
                  mayDisconnect ? (
                    <DisconnectButton accountId={a.id} onDone={onConnected} />
                  ) : (
                    <span className="ym-hint">{disconnectBlock}</span>
                  ),
              },
            ]}
          />
        )}
      </QueryBoundary>
    </Panel>
  );
}

function ConnectButton({ platform, onConnected }: { platform: string; onConnected: () => void }) {
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState<string | null>(null);

  /* GET, not POST: `oauth_*_start` returns `{authorize_url}` and the platform's
     own consent page does the authorizing. Nothing is sent from here. */
  const start = useCallback(async () => {
    setBusy(true);
    setRefusal(null);
    try {
      const r = (await wsApi.get(`/publishing/oauth/${platform}/start`)) as
        | { authorize_url?: string }
        | undefined;
      if (!r?.authorize_url) {
        setRefusal("The backend returned no authorize_url, so no consent flow started.");
        return;
      }
      window.open(r.authorize_url, "ymoney-oauth", "width=560,height=700");
      onConnected();
    } catch (e) {
      setRefusal(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }, [platform, onConnected]);

  return (
    <>
      <Button size="sm" loading={busy} onClick={() => void start()}>
        Connect {humanize(platform)}
      </Button>
      {refusal ? <p className="ym-error">{refusal}</p> : null}
    </>
  );
}

function DisconnectButton({ accountId, onDone }: { accountId: string; onDone: () => void }) {
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);
  const disconnect = async () => {
    setPending(true);
    setError(null);
    try {
      await wsApi.del(`/publishing/accounts/${accountId}`);
      onDone();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setPending(false);
    }
  };
  return (
    <>
      <DestructiveButton
        confirmLabel="Revoke this platform token? Publishing to it stops immediately."
        onConfirm={() => void disconnect()}
        loading={pending}
      >
        Disconnect
      </DestructiveButton>
      {error ? <p className="ym-error">{error}</p> : null}
    </>
  );
}

/* ==========================================================================
 * Platform capability differences + verified limits
 * ======================================================================= */

function PlatformsPanel({
  caps,
  profiles,
}: {
  caps: QueryState<DistributionCapabilityList>;
  profiles: QueryState<PlatformProfileList>;
}) {
  const column: Column<DistributionCapabilityRow>[] = [
    { key: "platform", header: "Platform", cell: (r) => <strong>{humanize(r.platform)}</strong> },
    {
      key: "mode",
      header: "Publish path",
      cell: (r) =>
        r.user_handoff ? (
          <Badge tone="handoff" title="USER_HANDOFF — media is prepared and a human publishes it">
            HANDOFF
          </Badge>
        ) : (
          <Badge tone={r.direct_publish ? "live" : "neutral"} title={r.publish_mode}>
            {r.publish_mode === "DIRECT_PUBLISH" ? "DIRECT PUBLISH" : humanize(r.publish_mode)}
          </Badge>
        ),
    },
    {
      key: "caps",
      header: "Capabilities",
      cell: (r) => <BadgeRow values={r.capabilities} tone="info" />,
      hideBelow: "lg",
    },
    {
      key: "inbox",
      header: "Inbox",
      cell: (r) =>
        r.supports_inbox ? (
          <Badge tone="success">supported</Badge>
        ) : (
          <span className="ym-muted" title="The registry declares no inbox support for this platform">
            UNAVAILABLE
          </span>
        ),
      hideBelow: "md",
    },
    {
      key: "analytics",
      header: "Analytics",
      cell: (r) =>
        r.supports_analytics ? (
          <Badge tone="success">supported</Badge>
        ) : (
          <span className="ym-muted" title="The registry declares no analytics support for this platform">
            UNAVAILABLE
          </span>
        ),
      hideBelow: "md",
    },
    {
      key: "limits",
      header: "Verified limits",
      cell: (r) => {
        const known = profiles.data?.items?.find((p) => p.platform === r.platform);
        if (profiles.loading) return <span className="ym-muted">reading…</span>;
        if (profiles.error) return <span className="ym-muted">UNAVAILABLE</span>;
        return (
          <span>
            <span className="ym-muted">verified: </span>
            {joinTokens(known?.verified_limits, "none recorded")}
            {(known?.unverified.length ?? 0) > 0 ? (
              <>
                <br />
                <span className="ym-muted">undocumented: </span>
                {joinTokens(known?.unverified, "")}
              </>
            ) : null}
          </span>
        );
      },
      hideBelow: "lg",
    },
  ];

  return (
    <Panel
      title="Platform capabilities"
      subtitle="Publish path comes from the publisher registry, never a platform-name check. Undocumented limits are shown as undocumented, not invented."
      dense
    >
      <QueryBoundary query={caps} skeletonRows={5}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={column}
            rowKey={(r) => r.platform}
            caption="Platform capabilities"
            maxHeight={520}
            empty="No platform in the registry"
            emptyHint="GET /distribution/capabilities returned no rows. Until it does, no platform can be declared live-capable."
          />
        )}
      </QueryBoundary>
      <ProfilesDetail profiles={profiles} />
    </Panel>
  );
}

function ProfilesDetail({ profiles }: { profiles: QueryState<PlatformProfileList> }) {
  return (
    <QueryBoundary query={profiles} skeletonRows={3}>
      {(d) =>
        d.items.length === 0 ? (
          <p className="ym-muted">No verified platform profile is published.</p>
        ) : (
          <Grid min={260} gap="sm">
            {d.items.map((p) => (
              <div key={p.platform} className="ym-stat ym-tone-neutral">
                <div className="ym-stat-label">{humanize(p.platform)}</div>
                <div className="ym-stat-hint">{joinTokens(p.media_types, "media types unknown")}</div>
                <div className="ym-stat-source">
                  {p.publish_mode} · {p.verified_limits.length} verified · {p.unverified.length} undocumented
                </div>
                {p.verified_notes.map((n) => (
                  <div key={n} className="ym-hint">
                    {n}
                  </div>
                ))}
              </div>
            ))}
          </Grid>
        )
      }
    </QueryBoundary>
  );
}

/* ==========================================================================
 * Schedule
 * ======================================================================= */

function SchedulePanel({ calendar }: { calendar: QueryState<CalendarPayload> }) {
  const column: Column<ScheduleEntryRow>[] = [
    { key: "run", header: "Runs at (UTC)", cell: (e) => <span className="ym-num">{when(e.run_at)}</span> },
    { key: "platform", header: "Platform", cell: (e) => humanize(e.platform) },
    { key: "status", header: "Status", cell: (e) => <StatusBadge status={e.status} /> },
    {
      key: "link",
      header: "Linked to",
      cell: (e) =>
        e.campaign_id ? (
          <span className="ym-notif-detail">campaign {e.campaign_id.slice(0, 8)}</span>
        ) : e.content_item_id ? (
          <span className="ym-notif-detail">content {e.content_item_id.slice(0, 8)}</span>
        ) : (
          <span className="ym-muted">plan item</span>
        ),
      hideBelow: "md",
    },
  ];

  return (
    <Panel
      title="Schedule"
      subtitle="Canonical ScheduleEntry rows only. run_at is stored naive and read as UTC."
      dense
    >
      <QueryBoundary query={calendar} skeletonRows={5}>
        {(d) => (
          <>
            <Grid min={170} gap="sm">
              <StatTile label="Entries in window" value={d.entries.length} source="GET /planner/calendar" />
              <StatTile
                label="Pending"
                value={d.entries.filter((e) => e.status === "PENDING").length}
                tone="info"
                source="GET /planner/calendar"
              />
              <StatTile
                label="Done"
                value={d.entries.filter((e) => e.status === "DONE").length}
                tone="success"
                source="GET /planner/calendar"
              />
              <StatTile
                label="Failed"
                value={d.entries.filter((e) => e.status === "FAILED").length}
                tone={d.entries.some((e) => e.status === "FAILED") ? "danger" : "neutral"}
                source="GET /planner/calendar"
              />
              <StatTile
                label="Shorts per day"
                value={d.capacity.declared ? d.capacity.shorts_per_day : "unbounded"}
                tone={d.capacity.declared ? "neutral" : "unknown"}
                hint={d.capacity.declared ? d.capacity.locale : "No pool declared — unbounded, which is not zero."}
                source="GET /planner/calendar"
              />
            </Grid>
            <DataTable
              rows={d.entries}
              columns={column}
              rowKey={(e) => e.id}
              caption="Scheduled distribution entries"
              maxHeight={420}
              empty="Nothing scheduled in this window"
              emptyHint="A placement appears here once the planner writes a ScheduleEntry. Placing something schedules it; it publishes nothing by itself."
            />
            <p className="ym-hint">{d.note}</p>
          </>
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Publish state: publications, deliveries, failures
 * ======================================================================= */

function PublishStatePanel({
  posts,
  jobs,
  handoff,
  registryError,
}: {
  posts: QueryState<PublishedPostList>;
  jobs: QueryState<PublishingJobList>;
  handoff: Set<string>;
  registryError: string | null;
}) {
  const [mode, setMode] = useState<"all" | "failures">("all");
  const failed = useMemo(
    () => (jobs.data?.items ?? []).filter((j) => FAILED_JOB.includes((j.status ?? "").toUpperCase())),
    [jobs.data],
  );

  return (
    <>
      <Panel
        title="Publications"
        subtitle="Every row carries its own mode. LIVE, MOCK, HANDOFF and UNAVAILABLE never share a badge."
        dense
      >
        {registryError ? (
          <p className="ym-error">
            The publisher registry could not be read ({registryError}). Handoff
            cannot be separated from a direct publish, so affected rows fall back
            to MODE UNAVAILABLE rather than being labelled live on a guess.
          </p>
        ) : null}
        <QueryBoundary query={posts} skeletonRows={4}>
          {(d) => (
            <DataTable
              rows={d.items ?? []}
              rowKey={(p) => p.id}
              caption="Published posts with their publication mode"
              maxHeight={460}
              empty="No publication recorded"
              emptyHint="A row appears only after a provider accepted the content or a handoff was prepared."
              columns={[
                { key: "platform", header: "Platform", cell: (p) => humanize(p.platform) },
                {
                  key: "mode",
                  header: "Mode",
                  cell: (p) => <ModeCell mode={resolvePostMode(p, handoff.has(p.platform))} />,
                },
                { key: "title", header: "Title", cell: (p) => p.title || <span className="ym-muted">—</span>, hideBelow: "md" },
                {
                  key: "remote",
                  header: "Remote",
                  cell: (p) =>
                    p.remote_url ? (
                      <a href={p.remote_url} target="_blank" rel="noreferrer" className="ym-notif-detail">
                        {p.remote_url.slice(0, 44)}
                      </a>
                    ) : (
                      <span className="ym-muted" title="No remote pointer — the outcome is undecidable, not live">
                        none
                      </span>
                    ),
                  hideBelow: "lg",
                },
                {
                  key: "views",
                  header: "Views",
                  align: "right",
                  /* HONESTY (Work 16.5.7 §8): null ⇔ no metric snapshot. Was a
                   * hard 0, indistinguishable from a post nobody watched. */
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
                  key: "likes",
                  header: "Likes",
                  align: "right",
                  cell: (p) =>
                    p.metrics.likes === null ? (
                      <span className="ym-muted" title="No provider reported a figure for this post">
                        UNAVAILABLE
                      </span>
                    ) : (
                      p.metrics.likes
                    ),
                  hideBelow: "md",
                },
                {
                  key: "completion",
                  header: "Completion",
                  align: "right",
                  cell: (p) =>
                    p.metrics.completion_rate === null ? (
                      <span className="ym-muted" title="No provider reported a completion rate">
                        UNAVAILABLE
                      </span>
                    ) : (
                      `${(p.metrics.completion_rate * 100).toFixed(1)}%`
                    ),
                  hideBelow: "lg",
                },
                { key: "at", header: "Recorded", cell: (p) => when(p.published_at), hideBelow: "lg" },
              ]}
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Delivery jobs and failures"
        subtitle="One row per (video, platform). A failure here is a confirmed rejection; an ambiguous submission lives in Paid incidents."
        dense
        actions={
          <Select
            label="Show"
            value={mode}
            onChange={(e) => setMode(e.target.value === "failures" ? "failures" : "all")}
          >
            <option value="all">All deliveries</option>
            <option value="failures">Failures only</option>
          </Select>
        }
      >
        <QueryBoundary query={jobs} skeletonRows={4}>
          {(d) => {
            const rows = mode === "failures" ? failed : (d.items ?? []);
            return (
              <DataTable
                rows={rows}
                rowKey={(j) => j.id}
                caption="Publishing delivery jobs"
                maxHeight={440}
                empty={mode === "failures" ? "No failed delivery" : "No delivery attempted"}
                emptyHint={
                  mode === "failures"
                    ? "A delivery is a FAILED row here. An ambiguous paid submission is NOT failed — look in Paid incidents."
                    : "A delivery job appears once a publish is submitted for a platform."
                }
                columns={[
                  { key: "platform", header: "Platform", cell: (j) => humanize(j.platform) },
                  { key: "status", header: "Status", cell: (j) => <StatusBadge status={j.status} /> },
                  {
                    key: "delivered",
                    header: "Delivered",
                    cell: (j) =>
                      DELIVERED.includes((j.status ?? "").toUpperCase()) ? (
                        <Badge tone="success">yes</Badge>
                      ) : (
                        <span className="ym-muted">no</span>
                      ),
                    hideBelow: "md",
                  },
                  { key: "attempt", header: "Attempts", align: "right", cell: (j) => j.attempt },
                  {
                    key: "remote",
                    header: "Remote id",
                    cell: (j) =>
                      j.remote_post_id ? (
                        <span className="ym-notif-detail">{j.remote_post_id.slice(0, 40)}</span>
                      ) : (
                        <span className="ym-muted">none</span>
                      ),
                    hideBelow: "lg",
                  },
                  {
                    key: "error",
                    header: "Error",
                    cell: (j) =>
                      j.error ? (
                        <span className="ym-error">{j.error.slice(0, 120)}</span>
                      ) : (
                        <span className="ym-muted">—</span>
                      ),
                  },
                  { key: "when", header: "Scheduled", cell: (j) => when(j.scheduled_at ?? j.created_at), hideBelow: "lg" },
                ]}
              />
            );
          }}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Paid incidents — verification, and the absence of a retry
 * ======================================================================= */

function PaidIncidentsPanel({ incidents }: { incidents: QueryState<PaidIncidentList> }) {
  return (
    <Panel
      title="Paid incidents — what may already have been billed"
      subtitle="SUBMISSION_UNKNOWN is not a failure and is never retried from here. Reconcile with the provider first."
      dense
    >
      <QueryBoundary query={incidents} skeletonRows={3}>
        {(d) => (
          <>
            <Grid min={180} gap="sm">
              <StatTile label="Open incidents" value={d.count} tone={d.count ? "warning" : "neutral"} source="GET /provider-maturity/incidents" />
              <StatTile
                label="Unknown exposure"
                value={d.unknown_exposure_count}
                tone={d.unknown_exposure_count ? "unknown" : "neutral"}
                hint="Accepted by the provider, priceable by nobody."
                source="GET /provider-maturity/incidents"
              />
            </Grid>
            <DataTable
              rows={d.items}
              rowKey={(i) => i.incident_id}
              caption="Paid submission incidents"
              empty="No unresolved paid submission"
              emptyHint="Nothing this workspace submitted is sitting in an ambiguous or unattributed state."
              columns={[
                {
                  key: "state",
                  header: "State (verbatim)",
                  cell: (i) => (
                    <Badge tone={toneForStatus(i.state)} title="Reported exactly as the paid contract states it. SUBMISSION_UNKNOWN is never rendered as FAILED.">
                      {humanize(i.state)}
                    </Badge>
                  ),
                },
                { key: "provider", header: "Provider", cell: (i) => i.provider },
                { key: "operation", header: "Operation", cell: (i) => i.operation, hideBelow: "md" },
                {
                  key: "verdict",
                  header: "Backend verdict",
                  cell: (i) => (
                    <span>
                      <Badge tone={i.retry_safe ? "success" : "unknown"}>
                        {i.retry_safe ? "Resubmit proven safe" : "Resubmit NOT safe"}
                      </Badge>
                      <div className="ym-hint">{humanize(i.recommended_action)}</div>
                    </span>
                  ),
                },
                {
                  key: "exposure",
                  header: "Exposure",
                  align: "right",
                  cell: (i) => (
                    <span>
                      <Badge tone={i.exposure_unknown ? "unknown" : "neutral"}>{humanize(i.exposure)}</Badge>
                      <div>
                        <Money usd={i.estimated_exposure_usd} />
                      </div>
                    </span>
                  ),
                  hideBelow: "md",
                },
                {
                  key: "remote",
                  header: "Provider id",
                  cell: (i) =>
                    i.remote_id ? (
                      <span className="ym-notif-detail">{i.remote_id.slice(0, 40)}</span>
                    ) : (
                      <span className="ym-muted" title="No provider id came back, which is why the outcome is unknown">
                        none — reconcile by provider id
                      </span>
                    ),
                  hideBelow: "lg",
                },
                {
                  key: "detail",
                  header: "Detail",
                  cell: (i) => <span className="ym-notif-detail">{i.detail.slice(0, 140)}</span>,
                  hideBelow: "lg",
                },
              ]}
            />
            <p className="ym-hint">
              {d.note} There is deliberately no resubmit control on this panel:
              resending an ambiguous submission is how one incident becomes two
              charges. Re-read the incident, or reconcile it upstream.
            </p>
          </>
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Provider maturity
 * ======================================================================= */

function ProviderMaturityPanel({ maturity }: { maturity: QueryState<MaturityList> }) {
  return (
    <Panel
      title="Provider maturity"
      subtitle="A status recorded after evidence. live_status=UNVERIFIED means nobody has exercised the provider against its real service."
      dense
    >
      <QueryBoundary query={maturity} skeletonRows={5}>
        {(d) => (
          <>
            <Grid min={170} gap="sm">
              <StatTile label="Providers" value={d.count} source="GET /provider-maturity" />
              <StatTile
                label="Production ready"
                value={d.items.filter((r) => r.production_ready).length}
                tone="success"
                hint="Expected to be 0 for this repository."
                source="GET /provider-maturity"
              />
              <StatTile
                label="Simulation only"
                value={d.items.filter((r) => r.simulation_only).length}
                tone={d.items.some((r) => r.simulation_only) ? "warning" : "neutral"}
                hint="A simulation must never be mistaken for a real backend."
                source="GET /provider-maturity"
              />
              <StatTile
                label="Unprobed health"
                value={d.items.filter((r) => r.health === "UNKNOWN").length}
                tone="unknown"
                hint="Health probes are opt-in; an unprobed row is not a healthy row."
                source="GET /provider-maturity"
              />
            </Grid>
            <DataTable
              rows={d.items}
              rowKey={(r) => `${r.provider}-${r.capability}`}
              caption="Provider maturity records"
              maxHeight={480}
              empty="No maturity record"
              emptyHint="The maturity registry returned nothing, so no provider can be claimed production-ready."
              columns={[
                { key: "provider", header: "Provider", cell: (r) => <strong>{humanize(r.provider)}</strong> },
                { key: "capability", header: "Capability", cell: (r) => humanize(r.capability), hideBelow: "md" },
                {
                  key: "live",
                  header: "Live status",
                  cell: (r) => <Badge tone={toneForStatus(r.live_status)}>{humanize(r.live_status)}</Badge>,
                },
                {
                  key: "contract",
                  header: "Contract",
                  cell: (r) => <Badge tone={toneForStatus(r.contract_status)}>{humanize(r.contract_status)}</Badge>,
                  hideBelow: "md",
                },
                {
                  key: "credential",
                  header: "Credential",
                  cell: (r) => (
                    <span>
                      <Badge tone={toneForStatus(r.credential_status)}>{humanize(r.credential_status)}</Badge>
                      {r.simulation_only ? (
                        <Badge tone="warning" title="simulation_only: this provider is a stand-in">
                          simulation
                        </Badge>
                      ) : null}
                    </span>
                  ),
                  hideBelow: "lg",
                },
                {
                  key: "blockers",
                  header: "Blockers",
                  cell: (r) =>
                    r.blockers.length ? (
                      <span className="ym-error">{r.blockers.join("; ")}</span>
                    ) : (
                      <span className="ym-muted">none recorded</span>
                    ),
                  hideBelow: "lg",
                },
                { key: "notes", header: "Why", cell: (r) => r.notes, hideBelow: "lg" },
              ]}
            />
            <p className="ym-hint">{d.note}</p>
          </>
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Capability differences
 * ======================================================================= */

/**
 * Capability differences are stated, not enforced.
 *
 * `backend/app/services/capabilities.py` is the single source: the routers
 * declare `require_workspace_role(...)` and the table mirrors those thresholds.
 * `publish.execute` and `publish.approve` both require `admin`; reads are
 * `viewer`. Everything below is AFFORDANCE — the server answers 403 regardless,
 * and `blockedReason()` says so next to the control it disables.
 */
const CAPABILITY_ROWS: { capability: string; why: string }[] = [
  { capability: "content.read", why: "Reads. The floor on nearly every route." },
  { capability: "content.write", why: "Content mutation." },
  { capability: "reviews.approve", why: "Review decisions." },
  { capability: "publish.approve", why: "Approving a publication." },
  { capability: "publish.execute", why: "Executing a publish, and connecting an account." },
  { capability: "brand.manage", why: "Brand management." },
  { capability: "providers.manage", why: "Provider credential management." },
  { capability: "operations.view", why: "Operational dashboards." },
];

function CapabilityPanel() {
  const { capabilitiesKnown, capabilities } = useSession();
  return (
    <Panel
      title="What this workspace may attempt"
      subtitle={
        capabilitiesKnown
          ? "Derived server-side by GET /auth/me. Affordance only — the backend enforces every one of these."
          : "The server did not report a capability list, so every control is offered and the server answers 403 or 200."
      }
      dense
    >
      <Grid min={230} gap="sm">
        {CAPABILITY_ROWS.map((row) => {
          const allowed = can(row.capability);
          return (
            <div key={row.capability} className={cx("ym-stat", allowed ? "ym-tone-success" : "ym-tone-neutral")}>
              <div className="ym-stat-label">{row.capability}</div>
              <div className="ym-stat-value">{allowed ? "allowed" : "not allowed"}</div>
              <div className="ym-stat-hint">{row.why}</div>
              {allowed ? null : <div className="ym-stat-source">{blockedReason(row.capability)}</div>}
            </div>
          );
        })}
      </Grid>
      {capabilitiesKnown && capabilities.length === 0 ? (
        <p className="ym-error">
          The server reported an empty capability list for this workspace, so
          every mutating control on this screen is hidden. Reads still work.
        </p>
      ) : null}
    </Panel>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

export function Distribution() {
  const { workspaceId, workspace } = useSession();
  const [tab, setTab] = useState("state");

  const accounts = useWsQuery<SocialAccountList>("/publishing/accounts");
  const jobs = useWsQuery<PublishingJobList>("/publishing/jobs?limit=100");
  const posts = useWsQuery<PublishedPostList>("/publishing/posts?limit=100");
  const caps = useWsQuery<DistributionCapabilityList>("/distribution/capabilities");
  const profiles = useWsQuery<PlatformProfileList>("/distribution/platforms");
  const calendar = useWsQuery<CalendarPayload>("/planner/calendar?days=30");
  const maturity = useWsQuery<MaturityList>("/provider-maturity");
  const incidents = useWsQuery<PaidIncidentList>("/provider-maturity/incidents?limit=50");

  /* `/system/health` is process configuration, not workspace state, so it is
     read through `api` rather than the workspace-scoped client. It is held in
     local state because `useQuery` is typed around `wsApi`, and the failure is
     kept as an error string so the readiness tiles say UNAVAILABLE instead of
     reading a healthy-looking default. */
  const [health, setHealth] = useState<{ data: SystemHealth | null; error: string | null }>({
    data: null,
    error: null,
  });
  const [healthTick, setHealthTick] = useState(0);
  useEffect(() => {
    let alive = true;
    api("GET", "/system/health").then(
      (r) => {
        if (alive) setHealth({ data: r as SystemHealth, error: null });
      },
      (e: unknown) => {
        if (alive) setHealth({ data: null, error: e instanceof Error ? e.message : String(e) });
      },
    );
    return () => {
      alive = false;
    };
  }, [healthTick]);

  const handoff = useMemo(() => {
    const ids = new Set<string>();
    for (const c of caps.data?.items ?? []) if (c.user_handoff) ids.add(c.platform);
    return ids;
  }, [caps.data]);

  const modeCounts = useMemo<ModeCounts | null>(() => {
    if (posts.data === null) return null;
    const counts = emptyModeCounts();
    for (const p of posts.data.items) counts[resolvePostMode(p, handoff.has(p.platform))] += 1;
    return counts;
  }, [posts.data, handoff]);

  const failedJobCount = useMemo(
    () =>
      jobs.data === null
        ? null
        : jobs.data.items.filter((j) => FAILED_JOB.includes((j.status ?? "").toUpperCase())).length,
    [jobs.data],
  );

  const reloadAll = () => {
    accounts.reload();
    jobs.reload();
    posts.reload();
    caps.reload();
    profiles.reload();
    calendar.reload();
    maturity.reload();
    incidents.reload();
    setHealthTick((t) => t + 1);
  };

  if (!workspaceId) {
    return (
      <>
        <PageHeader
          title="Distribution"
          description="Accounts, readiness, platform variants, schedule, publish state and failures."
        />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to distribute"
            description="Accounts, readiness and delivery history are workspace-scoped. Select a workspace to load them."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Distribution"
        description={
          workspace?.name
            ? `${workspace.name} — the last screen before a post leaves the building.`
            : "The last screen before a post leaves the building."
        }
        actions={<Button onClick={reloadAll}>Refresh all</Button>}
      />

      <Panel title="At a glance" dense>
        <Grid min={175} gap="sm">
          <StatTile
            label="Connected accounts"
            value={accounts.data === null ? undefined : accounts.data.items.length}
            unavailable={accounts.data === null}
            source="GET /publishing/accounts"
          />
          <StatTile
            label="Live publications"
            value={modeCounts === null ? undefined : modeCounts.LIVE}
            unavailable={modeCounts === null}
            tone="live"
            hint="A real platform API returned a remote pointer."
            source="GET /publishing/posts"
          />
          <StatTile
            label="Mock publications"
            value={modeCounts === null ? undefined : modeCounts.MOCK}
            unavailable={modeCounts === null}
            tone="mock"
            hint="Nothing was sent."
            source="GET /publishing/posts"
          />
          <StatTile
            label="Handoff publications"
            value={modeCounts === null ? undefined : modeCounts.HANDOFF}
            unavailable={modeCounts === null}
            tone="handoff"
            hint="Prepared; a human still has to publish."
            source="GET /publishing/posts + /distribution/capabilities"
          />
          <StatTile
            label="Mode not reported"
            value={modeCounts === null ? undefined : modeCounts.UNAVAILABLE}
            unavailable={modeCounts === null}
            tone="unknown"
            hint="No mock flag and no remote pointer — undecidable, not live."
            source="GET /publishing/posts"
          />
          <StatTile
            label="Failed deliveries"
            value={failedJobCount === null ? undefined : failedJobCount}
            unavailable={failedJobCount === null}
            tone={failedJobCount ? "danger" : "neutral"}
            hint="A confirmed rejection. An ambiguous submit is NOT counted here."
            source="GET /publishing/jobs"
          />
          <StatTile
            label="Scheduled entries"
            value={calendar.data === null ? undefined : calendar.data.entries.length}
            unavailable={calendar.data === null}
            tone="info"
            hint="Next 30 days."
            source="GET /planner/calendar"
          />
          <StatTile
            label="Paid incidents"
            value={incidents.data === null ? undefined : incidents.data.count}
            unavailable={incidents.data === null}
            tone={incidents.data && incidents.data.count ? "warning" : "neutral"}
            hint="May already have been billed. Never retried from here."
            source="GET /provider-maturity/incidents"
          />
          <StatTile
            label="Handoff platforms"
            value={caps.data === null ? undefined : handoff.size}
            unavailable={caps.data === null}
            tone="handoff"
            hint="From HANDOFF_PLATFORMS, not a name check."
            source="GET /distribution/capabilities"
          />
          <StatTile
            label="Publish path"
            value={
              health.error
                ? undefined
                : health.data === null
                  ? undefined
                  : health.data.mocks?.publishing
                    ? "MOCK"
                    : "REAL"
            }
            unavailable={health.error !== null || health.data === null}
            tone={health.data?.mocks?.publishing ? "mock" : "neutral"}
            hint={
              health.error
                ? "GET /system/health failed"
                : health.data?.mocks?.publishing
                  ? "MOCK_PUBLISHING is on — nothing reaches a platform."
                  : "No global mock-publishing flag."
            }
            source="GET /system/health"
          />
        </Grid>
      </Panel>

      <Tabs
        tabs={[
          { id: "state", label: "Publish state", count: posts.data?.items.length },
          { id: "accounts", label: "Accounts", count: accounts.data?.items.length },
          { id: "platforms", label: "Platforms", count: caps.data?.items.length },
          { id: "schedule", label: "Schedule", count: calendar.data?.entries.length },
          { id: "providers", label: "Providers & money", count: incidents.data?.count },
        ]}
        active={tab}
        onChange={setTab}
      />

      {tab === "state" ? (
        <>
          <PublishStatePanel
            posts={posts}
            jobs={jobs}
            handoff={handoff}
            registryError={caps.error}
          />
          <PaidIncidentsPanel incidents={incidents} />
        </>
      ) : null}

      {tab === "accounts" ? (
        <>
          <AccountsPanel accounts={accounts} health={health} onConnected={reloadAll} />
          <CapabilityPanel />
        </>
      ) : null}

      {tab === "platforms" ? <PlatformsPanel caps={caps} profiles={profiles} /> : null}

      {tab === "schedule" ? <SchedulePanel calendar={calendar} /> : null}

      {tab === "providers" ? (
        <>
          <PaidIncidentsPanel incidents={incidents} />
          <ProviderMaturityPanel maturity={maturity} />
        </>
      ) : null}
    </>
  );
}

export default Distribution;
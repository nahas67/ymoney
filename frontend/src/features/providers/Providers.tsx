/* Providers — the honest maturity table, and nothing that looks like a secret.
 *
 *   GET  /provider-maturity                      the whole registry (no credentials)
 *   GET  /provider-maturity/summary              counts per axis + what is ready
 *   GET  /provider-maturity/tts/qualification    the TTS verdicts and the donors
 *   GET  /provider-maturity                      + THIS workspace's credential STATE
 *   GET  /provider-maturity/summary              per-workspace credential counts
 *   GET  /provider-maturity/{provider}           one full record
 *   GET  /connections                            credential configuration status
 *   GET  /distribution/capabilities              the publishing path per platform
 *   GET  /intelligence/routing/health            the routing provider slots
 *
 * THE LADDER IS CLOSED AND EXACT
 *
 * `providers/maturity.py` states the eight values once and validates every
 * stored axis against them in `__post_init__`. Those eight are the ONLY
 * maturity values this screen can print. `maturityBadge` therefore does not
 * `humanize` whatever arrives: an unrecognised string renders as
 * "NOT IN LADDER" with no value printed, so a future backend cannot smuggle a
 * ninth label ("PRODUCTION_READY", most likely) onto this screen by adding a
 * field. `maturity.STATES` is a closed set on purpose: "production ready" is a
 * conclusion over four axes, not a fact about one provider.
 *
 * HEALTH IS A SEPARATE AXIS AND DEFAULTS TO UNKNOWN
 *
 * `ProviderMaturity.health` defaults to `HEALTH_UNKNOWN` and `probe_health`
 * only returns a real answer for an IMPLEMENTED TTS provider whose adapter was
 * actually asked. Everything else is UNKNOWN -- which is not healthy, and is not
 * rendered with the success colour.
 *
 * A MOCK PROVIDER IS NOT A PROVIDER
 *
 * `simulation_only` means the adapter fabricates its output. It is rendered as
 * SIMULATED in the `mock` tone, never as a working one, and it always
 * contributes a production blocker.
 *
 * NO CREDENTIAL VALUE, EVER
 *
 * `resolved_credential_status` is a STATE WORD -- CONFIGURED / NOT_CONFIGURED /
 * UNRESOLVED / NOT_REQUIRED -- and the module's own docstring says a leaked
 * fingerprint makes brute-forcing a short key cheap, so it is treated as the
 * secret. `GET /connections` goes further and ships a `masked` value per key;
 * this screen does not declare that field at all, for the same reason `masked`
 * is a prefix and a non-secret key's `masked` is its RAW value. Only
 * `configured` (a boolean) and `source` (env / workspace / unset) are rendered.
 */

import { useMemo, useState } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Grid,
  ModeBadge,
  PageHeader,
  Panel,
  QueryBoundary,
  StatTile,
  Tabs,
  humanize,
  type Column,
  type Tone,
} from "../../design-system/primitives";
import { useQuery, useWsQuery, type QueryState } from "../../api/queries";
import { api } from "../../lib/api";
import { blockedReason, can, useSession } from "../../state/session";

/* ==========================================================================
 * The ladder — transcribed verbatim from `providers/maturity.py`.
 * ======================================================================= */

export const MATURITY_LADDER = [
  "IMPLEMENTED",
  "CONTRACT_TESTED",
  "LIVE_VERIFIED",
  "UNVERIFIED",
  "UNAVAILABLE",
  "BLOCKED_LICENSE",
  "BLOCKED_COMMERCIAL_TERMS",
  "EXTERNAL_LIMITATION",
] as const;

export type MaturityState = (typeof MATURITY_LADDER)[number];

/** Is this one of the eight? Used to refuse to print anything else. */
export function isLadderState(value: string | null | undefined): value is MaturityState {
  return MATURITY_LADDER.includes((value ?? "") as MaturityState);
}

/**
 * The tone for a maturity value.
 *
 * LIVE_VERIFIED and CONTRACT_TESTED are deliberately NOT green. A contract test
 * drove an adapter offline and checked it returns the right type -- it says
 * nothing about whether the vendor works, and colouring it as success is how an
 * operator builds a cycle on an adapter that 404s against the real service.
 * Only LIVE_VERIFIED earns the success tone.
 */
export function maturityTone(value: string | null | undefined): Tone {
  const v = (value ?? "").toUpperCase();
  if (v === "LIVE_VERIFIED") return "success";
  if (v === "CONTRACT_TESTED") return "warning";
  if (v === "IMPLEMENTED") return "info";
  if (v === "BLOCKED_LICENSE" || v === "BLOCKED_COMMERCIAL_TERMS") return "danger";
  if (v === "UNAVAILABLE") return "danger";
  if (v === "EXTERNAL_LIMITATION") return "unknown";
  return "neutral";
}

/* ==========================================================================
 * Response shapes — from each router's return statement and `to_dict`.
 * ======================================================================= */

/* `ProviderMaturity.to_dict` */
export type ProviderMaturityRow = {
  provider: string;
  /** One of `maturity.CAPABILITIES`: llm | tts | music | video | image | avatar. */
  capability: string;
  implementation_status: string;
  contract_status: string;
  live_status: string;
  commercial_status: string;
  /** A REQUIREMENT (NOT_REQUIRED | REQUIRED), not an observation. */
  credential_status: string;
  health: string;
  last_verified_at: string;
  /** Credential key NAMES. The module's own comment: "Names, never values." */
  credential_keys: string[];
  simulation_only: boolean;
  notes: string;
  evidence: string[];
  gaps: string[];
  production_ready: boolean;
};

/* `_records(...)` adds these when the route resolved credentials and blockers. */
export type WorkspaceMaturityRow = ProviderMaturityRow & {
  /** CONFIGURED | NOT_CONFIGURED | UNRESOLVED | NOT_REQUIRED. Never a value. */
  resolved_credential_status: string;
  blockers: string[];
};

/* `provider_maturity_router.list_provider_maturity` (global: no credentials). */
export type MaturityList = {
  items: ProviderMaturityRow[];
  count: number;
  capabilities: string[];
  states: string[];
  registry_reviewed_at: string;
  note: string;
};

/* `workspace_maturity_router.workspace_provider_maturity` */
export type WorkspaceMaturityList = {
  workspace_id: string;
  items: WorkspaceMaturityRow[];
  count: number;
  resolved_credential_states: string[];
  note: string;
  credential_summary: {
    by_state: Record<string, number>;
    providers_without_credentials: string[];
  };
};

/* `maturity.status_summary` + the workspace credential summary. */
export type MaturitySummary = {
  registry_reviewed_at: string;
  states: string[];
  capabilities: string[];
  total: number;
  by_capability: Record<string, number>;
  implementation_status: Record<string, number>;
  contract_status: Record<string, number>;
  live_status: Record<string, number>;
  commercial_status: Record<string, number>;
  /** Expected to be EMPTY for this repository. */
  production_ready: { provider: string; capability: string }[];
  not_production_ready: Record<string, unknown>;
  credential_summary?: {
    by_state: Record<string, number>;
    providers_without_credentials: string[];
  };
  note: string;
};

/* `tts_qualification` */
export type TtsQualification = {
  implemented: {
    provider: string;
    label: string;
    adapter: string;
    implementation_status: string;
    contract_status: string;
    live_status: string;
    commercial_status: string;
    qualification_labels: string[];
    simulation_only: boolean;
    /** Key NAMES, never values. */
    credential_keys: string[];
    gaps: string[];
  }[];
  donor_candidates: Record<string, unknown>[];
  probeable: string[];
  note: string;
};

/* `connections_router.list_connections`.
 * `masked` exists on the wire and is DELIBERATELY NOT DECLARED. */
export type ConnectionRow = {
  key: string;
  label: string;
  secret: boolean;
  hint: string;
  /** boolean only. A key is either present or it is not. */
  configured: boolean;
  source: string;
};

export type ConnectionsList = { items: ConnectionRow[] };

/* `distribution_router.capabilities` */
export type DistributionCapability = {
  platform: string;
  capabilities: string[];
  publish_mode: string;
  direct_publish: boolean;
  user_handoff: boolean;
};
export type DistributionCapabilities = { items: DistributionCapability[] };

/* `intelligence_routing.routing_health` */
export type RoutingHealth = { providers: Record<string, boolean> };

/* ==========================================================================
 * Grouping — the requested order, plus anything the registry actually has.
 * ======================================================================= */

/**
 * The requested group order, mapped onto the registry's capability ids.
 *
 * `maturity.CAPABILITIES` is `llm, tts, music, video, image, avatar` -- it has
 * no `intelligence` and no `publishing`. Those two groups are therefore served
 * by the real data that DOES exist for them (the routing registry's slots, and
 * the publisher registry's publish path), each labelled as not being a maturity
 * record. Nothing is invented and nothing is silently dropped: a capability the
 * registry has but this map does not name gets its own group at the end.
 */
export const GROUP_ORDER = [
  "llm",
  "tts",
  "music",
  "image",
  "video",
  "intelligence",
  "publishing",
] as const;

export type GroupId = (typeof GROUP_ORDER)[number];

/** Registry capability id -> group id. `avatar` is deliberately unmapped. */
const CAPABILITY_GROUP: Record<string, GroupId> = {
  llm: "llm",
  tts: "tts",
  music: "music",
  image: "image",
  video: "video",
};

export function groupForCapability(capability: string): GroupId | "other" {
  return CAPABILITY_GROUP[capability] ?? "other";
}

/* ==========================================================================
 * Derived display helpers
 * ======================================================================= */

/**
 * One maturity value, or "NOT IN LADDER".
 *
 * The offending string is NOT printed -- not in the body, not in the tooltip.
 * `maturity.STATES` is closed and the backend validates on write, so reaching
 * this branch means the contract broke; the honest response is to say so
 * without letting an unknown label borrow the authority of a real one.
 */
export function maturityBadge(value: string | null | undefined) {
  if (!isLadderState(value)) {
    return (
      <Badge tone="unknown" dot title="Not one of the eight registered maturity states; the value is withheld rather than printed.">
        NOT IN LADDER
      </Badge>
    );
  }
  return (
    <Badge tone={maturityTone(value)} dot>
      {value}
    </Badge>
  );
}

/** Health is its own axis; UNKNOWN is not a pass. */
export function healthBadge(health: string | null | undefined) {
  const h = (health ?? "").toUpperCase();
  if (h === "UNKNOWN" || !h) {
    return (
      <Badge tone="unknown" dot title="No probe was run. An unprobed provider is not a healthy one.">
        HEALTH UNKNOWN
      </Badge>
    );
  }
  if (h === "OK") return <Badge tone="success" dot>HEALTH OK</Badge>;
  if (h === "DEGRADED") return <Badge tone="warning" dot>HEALTH DEGRADED</Badge>;
  return <Badge tone="danger" dot>HEALTH DOWN</Badge>;
}

/** Credential STATE, never a value. */
export function credentialBadge(state: string | null | undefined) {
  const s = (state ?? "").toUpperCase();
  if (s === "CONFIGURED") return <Badge tone="success">CONFIGURED</Badge>;
  if (s === "NOT_CONFIGURED") return <Badge tone="warning">NOT CONFIGURED</Badge>;
  /* UNRESOLVED is its own case: the resolver failed, which is NOT the same as
   * "there is no key". Collapsing them would report a resolver fault as a
   * missing credential. */
  if (s === "UNRESOLVED") {
    return (
      <Badge tone="unknown" title="The resolver failed. That is not the same answer as NOT_CONFIGURED.">
        UNRESOLVED
      </Badge>
    );
  }
  if (s === "NOT_REQUIRED") return <Badge tone="neutral">NOT REQUIRED</Badge>;
  return <Badge tone="unknown">NOT REPORTED</Badge>;
}

function when(value: string | null | undefined) {
  return value ? (
    value.replace("Z", " UTC").replace("T", " ")
  ) : (
    <span className="ym-muted">never verified</span>
  );
}

function ReadFailure<T>({ name, path, query }: { name: string; path: string; query: QueryState<T> }) {
  if (!query.error) return null;
  return (
    <p className="ym-error" role="alert">
      {name} could not be read from {path} ({query.error}). Its rows read
      UNAVAILABLE rather than an empty provider list — an unreachable registry is
      not evidence that no provider exists.
    </p>
  );
}

/* ==========================================================================
 * At a glance
 * ======================================================================= */

function AtAGlance({
  rows,
  summary,
  incidents,
}: {
  rows: QueryState<WorkspaceMaturityList>;
  summary: QueryState<MaturitySummary>;
  incidents: QueryState<{ unknown_exposure_count: number }>;
}) {
  const items = rows.data?.items ?? null;

  const counts = useMemo(() => {
    if (items === null) return null;
    const out: Record<string, number> = {};
    for (const row of items) {
      for (const axis of ["implementation_status", "contract_status", "live_status", "commercial_status"]) {
        const key = `${axis}:${row[axis as keyof WorkspaceMaturityRow]}`;
        out[key] = (out[key] ?? 0) + 1;
      }
    }
    return out;
  }, [items]);

  const liveVerified = counts?.["live_status:LIVE_VERIFIED"] ?? null;
  const simulated = items === null ? null : items.filter((r) => r.simulation_only).length;
  const ready = summary.data ? summary.data.production_ready.length : null;

  return (
    <Panel
      title="At a glance"
      subtitle="Four independent axes. None of them is a readiness verdict, and `production_ready` is derived server-side rather than stored."
      dense
    >
      <Grid min={195} gap="sm">
        <StatTile
          label="Providers in the registry"
          value={items?.length}
          unavailable={items === null}
          source="GET /provider-maturity"
        />
        <StatTile
          label="Live-verified"
          value={liveVerified}
          unavailable={liveVerified === null}
          tone={liveVerified === null ? "neutral" : liveVerified > 0 ? "success" : "unknown"}
          hint="Nobody has exercised these against their real service unless this is non-zero."
          source="GET /provider-maturity"
        />
        <StatTile
          label="Contract-tested only"
          value={counts?.["contract_status:CONTRACT_TESTED"]}
          unavailable={counts === null}
          tone={counts === null ? "neutral" : "warning"}
          hint="An offline test drove the adapter. It does not mean the vendor works."
          source="GET /provider-maturity"
        />
        <StatTile
          label="Simulated adapters"
          value={simulated}
          unavailable={simulated === null}
          tone={simulated === null ? "neutral" : "mock"}
          hint="These fabricate output. A result from them is not a result from a service."
          source="GET /provider-maturity"
        />
        <StatTile
          label="Production-ready"
          value={ready}
          unavailable={ready === null}
          tone={ready === null ? "neutral" : ready > 0 ? "success" : "unknown"}
          hint="Expected to be zero. A long ready list is the bug this registry exists to catch."
          source="GET /provider-maturity/summary"
        />
        <StatTile
          label="Credentials unresolved"
          value={rows.data?.credential_summary.by_state.UNRESOLVED}
          unavailable={rows.data === null}
          tone="unknown"
          hint="A resolver fault, not a missing key. The two are reported separately."
          source="GET /provider-maturity"
        />
        <StatTile
          label="Without credentials"
          value={rows.data?.credential_summary.providers_without_credentials.length}
          unavailable={rows.data === null}
          tone={rows.data === null ? "neutral" : "warning"}
          source="GET /provider-maturity"
        />
        <StatTile
          label="Unknown monetary exposure"
          value={incidents.data?.unknown_exposure_count}
          unavailable={incidents.data === null}
          tone={incidents.data === null ? "neutral" : "unknown"}
          source="GET /provider-maturity/incidents"
        />
      </Grid>
      <ReadFailure name="The workspace maturity table" path="/provider-maturity" query={rows} />
      <ReadFailure name="The maturity summary" path="/provider-maturity/summary" query={summary} />
    </Panel>
  );
}

/* ==========================================================================
 * A maturity record, expanded
 * ======================================================================= */

function MaturityDetail({ row }: { row: WorkspaceMaturityRow }) {
  return (
    <>
      <Grid min={170} gap="sm">
        <StatTile label="Implementation" value={<span>{maturityBadge(row.implementation_status)}</span>} source="GET /provider-maturity" />
        <StatTile label="Contract" value={<span>{maturityBadge(row.contract_status)}</span>} source="GET /provider-maturity" />
        <StatTile label="Live" value={<span>{maturityBadge(row.live_status)}</span>} source="GET /provider-maturity" />
        <StatTile label="Commercial" value={<span>{maturityBadge(row.commercial_status)}</span>} source="GET /provider-maturity" />
        <StatTile label="Credential" value={<span>{credentialBadge(row.resolved_credential_status)}</span>} tone="neutral" hint="A state word. Never a value, a length, or a digest." source="GET /provider-maturity" />
        <StatTile label="Health" value={<span>{healthBadge(row.health)}</span>} tone="neutral" source="GET /provider-maturity" />
        <StatTile
          label="Last verified"
          value={row.last_verified_at ? when(row.last_verified_at) : "never verified"}
          tone={row.last_verified_at ? "neutral" : "unknown"}
          source="GET /provider-maturity"
        />
        <StatTile
          label="Simulated"
          value={row.simulation_only ? "SIMULATED" : "no"}
          tone={row.simulation_only ? "mock" : "success"}
          hint={row.simulation_only ? "This adapter fabricates its output." : undefined}
          source="GET /provider-maturity"
        />
      </Grid>
      <p className="ym-notif-detail">{row.notes}</p>
      {row.gaps.length > 0 ? (
        <p className="ym-error">
          Declared gaps: {row.gaps.join("; ")}. An empty gap list means the author
          claimed full coverage, which is its own claim.
        </p>
      ) : null}
      {row.blockers.length > 0 ? (
        <div className="ym-notif-detail">
          <strong>Why this is not production-ready</strong>
          <ul>
            {row.blockers.map((b) => (
              <li key={b}>{b}</li>
            ))}
          </ul>
        </div>
      ) : (
        <p className="ym-hint">The registry reports no blocker for this provider.</p>
      )}
      {row.credential_keys.length > 0 ? (
        <p className="ym-hint">
          Credential keys this provider needs — <em>names only, values are never
          returned by the API</em>: {row.credential_keys.join(", ")}.
        </p>
      ) : null}
    </>
  );
}

/* ==========================================================================
 * A maturity group
 * ======================================================================= */

function MaturityGroup({
  group,
  items,
  selected,
  onSelect,
  query,
}: {
  group: GroupId | "other";
  items: WorkspaceMaturityRow[];
  selected: string | null;
  onSelect: (provider: string) => void;
  query: QueryState<WorkspaceMaturityList>;
}) {
  const columns: Column<WorkspaceMaturityRow>[] = [
    { key: "provider", header: "Provider", cell: (r) => <strong>{r.provider}</strong> },
    { key: "impl", header: "Implementation", cell: (r) => maturityBadge(r.implementation_status) },
    { key: "contract", header: "Contract", cell: (r) => maturityBadge(r.contract_status), hideBelow: "md" },
    { key: "live", header: "Live", cell: (r) => maturityBadge(r.live_status) },
    { key: "commercial", header: "Commercial", cell: (r) => maturityBadge(r.commercial_status), hideBelow: "md" },
    { key: "credential", header: "Credential", cell: (r) => credentialBadge(r.resolved_credential_status) },
    { key: "health", header: "Health", cell: (r) => healthBadge(r.health), hideBelow: "lg" },
    {
      key: "sim",
      header: "Output",
      cell: (r) =>
        r.simulation_only ? (
          <Badge tone="mock" dot title="Fabricates output.">
            SIMULATED
          </Badge>
        ) : (
          <span className="ym-muted">real</span>
        ),
    },
    {
      key: "ready",
      header: "Ready",
      cell: (r) =>
        r.production_ready ? (
          <Badge tone="success">derived yes</Badge>
        ) : (
          <Badge tone="unknown">{r.blockers.length} blocker(s)</Badge>
        ),
      hideBelow: "lg",
    },
  ];

  const title =
    group === "other"
      ? "Other registry capabilities"
      : humanize(group === "tts" ? "TTS" : group);

  const extra =
    group === "other"
      ? "A capability the registry declares that this screen's grouping does not name. Shown rather than dropped."
      : undefined;

  return (
    <Panel title={title} subtitle={extra} dense>
      <QueryBoundary query={query} skeletonRows={4}>
        {() => (
          <DataTable
            rows={items}
            columns={columns}
            rowKey={(r) => `${r.capability}:${r.provider}`}
            caption={`${title} providers`}
            onRowClick={(r) => onSelect(r.provider)}
            empty={`No ${title} provider record`}
            emptyHint="The maturity registry declares no record for this capability. That is a statement about the registry, not about the vendor."
          />
        )}
      </QueryBoundary>
      {selected && items.some((r) => r.provider === selected) ? (
        <MaturityDetail row={items.find((r) => r.provider === selected) as WorkspaceMaturityRow} />
      ) : null}
    </Panel>
  );
}

/* ==========================================================================
 * Publishing + intelligence: real data, explicitly not maturity records
 * ======================================================================= */

function PublishingGroup({
  distribution,
}: {
  distribution: QueryState<DistributionCapabilities>;
}) {
  const columns: Column<DistributionCapability>[] = [
    { key: "platform", header: "Platform", cell: (p) => <strong>{humanize(p.platform)}</strong> },
    {
      key: "path",
      header: "Publish path",
      /* ModeBadge is correct HERE: `publish_mode` is the publication-mode
       * vocabulary (USER_HANDOFF / DIRECT_PUBLISH and their resolved values),
       * and LIVE / MOCK / HANDOFF get three distinct tones by design. */
      cell: (p) => <ModeBadge mode={p.publish_mode} />,
    },
    {
      key: "direct",
      header: "Direct publish",
      cell: (p) => (p.direct_publish ? <Badge tone="live">yes</Badge> : <Badge tone="handoff">no</Badge>),
    },
    {
      key: "handoff",
      header: "Human handoff",
      cell: (p) => (p.user_handoff ? <Badge tone="handoff">yes</Badge> : <Badge tone="neutral">no</Badge>),
    },
    {
      key: "caps",
      header: "Capabilities",
      cell: (p) =>
        p.capabilities.length ? (
          <span>
            {p.capabilities.map((c) => (
              <Badge key={c} tone="neutral">
                {c}
              </Badge>
            ))}
          </span>
        ) : (
          <span className="ym-muted">none declared</span>
        ),
      hideBelow: "md",
    },
  ];

  return (
    <Panel
      title="Publishing"
      subtitle="`maturity.CAPABILITIES` has no `publishing` capability, so this group is served by the publisher registry rather than by a maturity record. Publish path comes from there, never inferred."
      dense
    >
      <QueryBoundary query={distribution} skeletonRows={6}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(p) => p.platform}
            caption="Publishing path per platform"
            maxHeight={420}
            empty="No publishing platform registered"
            emptyHint="The registry names no platform, so nothing can be published and no publish mode can be claimed."
          />
        )}
      </QueryBoundary>
    </Panel>
  );
}

function IntelligenceGroup({ routing }: { routing: QueryState<RoutingHealth> }) {
  const rows = useMemo(
    () => (routing.data ? Object.entries(routing.data.providers ?? {}) : []),
    [routing.data],
  );

  return (
    <Panel
      title="Intelligence"
      subtitle="`maturity.CAPABILITIES` has no `intelligence` capability. What exists is the routing registry's provider slots — the actual health of the model providers. Slot health is a runtime registry fact, not a maturity verdict."
      dense
    >
      <QueryBoundary query={routing} skeletonRows={3}>
        {() => (
          <DataTable
            rows={rows}
            columns={[
              { key: "slot", header: "Provider slot", cell: ([slot]) => <strong>{slot}</strong> },
              {
                key: "health",
                header: "Slot health",
                cell: ([, ok]) =>
                  ok ? (
                    <Badge tone="success" dot>
                      HEALTHY
                    </Badge>
                  ) : (
                    <Badge tone="danger" dot>
                      DOWN
                    </Badge>
                  ),
              },
              {
                key: "maturity",
                header: "Maturity record",
                cell: () => (
                  <span className="ym-muted" title="No capability named `intelligence` exists in the maturity registry.">
                    none exists
                  </span>
                ),
              },
            ]}
            rowKey={([slot]) => slot}
            caption="Routing provider slots"
            empty="No routing provider slot"
            emptyHint="The router declares no slots, so no model request can be routed at all."
          />
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * TTS qualification
 * ======================================================================= */

function TtsQualificationPanel({ query }: { query: QueryState<TtsQualification> }) {
  const columns: Column<TtsQualification["implemented"][number]>[] = [
    { key: "provider", header: "Adapter", cell: (q) => <strong>{q.provider}</strong> },
    { key: "label", header: "Label", cell: (q) => q.label, hideBelow: "md" },
    { key: "impl", header: "Implementation", cell: (q) => maturityBadge(q.implementation_status) },
    { key: "contract", header: "Contract", cell: (q) => maturityBadge(q.contract_status) },
    { key: "live", header: "Live", cell: (q) => maturityBadge(q.live_status) },
    { key: "commercial", header: "Commercial", cell: (q) => maturityBadge(q.commercial_status), hideBelow: "md" },
    {
      key: "sim",
      header: "Output",
      cell: (q) =>
        q.simulation_only ? (
          <Badge tone="mock" dot>
            SIMULATED
          </Badge>
        ) : (
          <span className="ym-muted">real</span>
        ),
    },
    {
      key: "labels",
      header: "Qualification",
      cell: (q) =>
        q.qualification_labels.length ? (
          <span>
            {q.qualification_labels.map((l) => (
              <Badge key={l} tone="info">
                {l}
              </Badge>
            ))}
          </span>
        ) : (
          <span className="ym-muted">none</span>
        ),
      hideBelow: "lg",
    },
    {
      key: "gaps",
      header: "Gaps",
      cell: (q) => (q.gaps.length ? <span className="ym-notif-detail">{q.gaps.join("; ")}</span> : <span className="ym-muted">none declared</span>),
      hideBelow: "lg",
    },
  ];

  return (
    <Panel
      title="TTS qualification"
      subtitle="GET /provider-maturity/tts/qualification. Included because `MERGE` in the technology matrix reads as 'done' once enough time passes."
      dense
    >
      <QueryBoundary query={query} skeletonRows={6}>
        {(d) => (
          <>
            <p className="ym-hint">{d.note}</p>
            <DataTable
              rows={d.implemented ?? []}
              columns={columns}
              rowKey={(q) => q.provider}
              caption="TTS adapter qualification"
              empty="No TTS adapter recorded"
              emptyHint="No adapter exists, so TTS has no provider at all — which is different from an adapter that exists and is unverified."
            />
            {d.donor_candidates.length > 0 ? (
              <>
                <h3 className="ym-panel-title">Donor candidates — proposed, never built</h3>
                <DataTable
                  rows={d.donor_candidates}
                  columns={[
                    {
                      key: "candidate",
                      header: "Candidate",
                      cell: (c) => (
                        <span className="ym-notif-detail">{JSON.stringify(c).slice(0, 180)}</span>
                      ),
                    },
                  ]}
                  rowKey={(c, i) => String((c as { provider?: unknown }).provider ?? i)}
                  caption="TTS donor candidates"
                  empty="No donor candidate"
                  emptyHint="No candidate is proposed."
                />
              </>
            ) : null}
            <p className="ym-hint">
              Probeable adapters: {d.probeable.length ? d.probeable.join(", ") : "none"}.
            </p>
          </>
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Credentials: configuration status only
 * ======================================================================= */

function CredentialsPanel({
  connections,
  registry,
}: {
  connections: QueryState<ConnectionsList>;
  registry: QueryState<WorkspaceMaturityList>;
}) {
  const manage = can("providers.manage");
  const blocked = blockedReason("providers.manage");

  const columns: Column<ConnectionRow>[] = [
    { key: "label", header: "Setting", cell: (c) => <strong>{c.label}</strong> },
    {
      key: "key",
      header: "Key",
      cell: (c) => <span className="ym-notif-detail">{c.key}</span>,
      hideBelow: "md",
    },
    {
      key: "kind",
      header: "Kind",
      cell: (c) =>
        c.secret ? (
          <Badge tone="unknown" title="A value is stored. It is never displayed, not even masked.">
            SECRET
          </Badge>
        ) : (
          <Badge tone="neutral">setting</Badge>
        ),
    },
    {
      key: "configured",
      header: "Configured",
      /* A boolean. The API also returns a `masked` value per key, which this
       * screen does not declare: for a non-secret key `masked` IS the raw value,
       * and for a secret it is a fingerprint — and the maturity module says a
       * leaked fingerprint makes brute-forcing a short key cheap. */
      cell: (c) =>
        c.configured ? (
          <Badge tone="success" dot>
            yes
          </Badge>
        ) : (
          <Badge tone="warning" dot>
            no
          </Badge>
        ),
    },
    { key: "source", header: "Source", cell: (c) => <Badge tone="neutral">{humanize(c.source || "unset")}</Badge> },
    { key: "hint", header: "Hint", cell: (c) => <span className="ym-notif-detail">{c.hint || "—"}</span>, hideBelow: "lg" },
  ];

  return (
    <Panel
      title="Credential configuration"
      subtitle="Status only. No value, no length, no digest, no masked prefix — for any key, secret or not."
      dense
    >
      {blocked ? (
        <p className="ym-error" role="alert">
          {blocked} GET /connections needs an admin role, so the configuration
          status below is UNAVAILABLE. The per-provider credential STATE above
          still renders: it is a viewer-level read and answers CONFIGURED /
          NOT_CONFIGURED without revealing anything.
        </p>
      ) : null}
      <QueryBoundary query={connections} skeletonRows={8}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(c) => c.key}
            caption="Credential configuration status"
            maxHeight={520}
            empty="No credential setting registered"
            emptyHint="The registry declares no manageable credential key."
          />
        )}
      </QueryBoundary>
      {registry.data ? (
        <p className="ym-hint">
          Resolved credential states for this workspace:{" "}
          {registry.data.resolved_credential_states.join(", ")} —{" "}
          {registry.data.note}
        </p>
      ) : null}
    </Panel>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

type Tab = "llm" | "tts" | "music" | "image" | "video" | "intelligence" | "publishing" | "other" | "credentials";

export default function Providers() {
  const { workspaceId, workspace } = useSession();
  const [tab, setTab] = useState<Tab>("llm");
  const [selected, setSelected] = useState<string | null>(null);

  /* Global registry: no workspace scope, and therefore NO credential
   * resolution — the router deliberately refuses it, because a global answer
   * printed on a tenant-free route would read as "this deployment has no key",
   * which is not what it means. */
  const globalList = useQueryGlobal<MaturityList>("/provider-maturity");
  const summary = useQueryGlobal<MaturitySummary>("/provider-maturity/summary");
  const ttsQualification = useQueryGlobal<TtsQualification>("/provider-maturity/tts/qualification");

  const registry = useWsQuery<WorkspaceMaturityList>("/provider-maturity");
  const connections = useWsQuery<ConnectionsList>("/connections", { enabled: can("providers.manage") });
  const distribution = useWsQuery<DistributionCapabilities>("/distribution/capabilities");
  const routing = useWsQuery<RoutingHealth>("/intelligence/routing/health");
  const incidents = useWsQuery<{ unknown_exposure_count: number }>("/provider-maturity/incidents");

  const items = registry.data?.items ?? [];

  const grouped = useMemo(() => {
    const out: Record<string, WorkspaceMaturityRow[]> = {};
    for (const row of items) {
      const key = groupForCapability(row.capability);
      (out[key] ??= []).push(row);
    }
    return out;
  }, [items]);

  const unmapped = useMemo(
    () => items.filter((r) => groupForCapability(r.capability) === "other"),
    [items],
  );

  const reloadAll = () => {
    registry.reload();
    summary.reload();
    ttsQualification.reload();
    globalList.reload();
    connections.reload();
    distribution.reload();
    routing.reload();
    incidents.reload();
  };

  const select = (provider: string) =>
    setSelected((current) => (current === provider ? null : provider));

  const maturityTabs: { id: Tab; label: string; count?: number }[] = [
    { id: "llm", label: "LLM", count: grouped.llm?.length },
    { id: "tts", label: "TTS", count: grouped.tts?.length },
    { id: "music", label: "Music", count: grouped.music?.length },
    { id: "image", label: "Image", count: grouped.image?.length },
    { id: "video", label: "Video", count: grouped.video?.length },
    { id: "intelligence", label: "Intelligence" },
    { id: "publishing", label: "Publishing" },
    ...(unmapped.length > 0 ? [{ id: "other" as Tab, label: "Other", count: unmapped.length }] : []),
    { id: "credentials", label: "Credentials" },
  ];

  if (!workspaceId) {
    return (
      <>
        <PageHeader
          title="Providers"
          description="Maturity, credential status, and what each provider can actually do."
        />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to assess"
            description="Credential resolution is workspace-scoped. Select a workspace to see which credentials it holds; the global registry would still answer, but it deliberately carries no credential verdict."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Providers"
        description={
          workspace?.name
            ? `${workspace.name} — what is implemented, what was contract-tested, and what has been exercised against a real service.`
            : "What is implemented, what was contract-tested, and what has been exercised against a real service."
        }
        actions={<Button onClick={reloadAll}>Refresh</Button>}
      />

      <AtAGlance rows={registry} summary={summary} incidents={incidents} />

      <Panel title="The ladder" subtitle="These eight values are the only maturity states the system can hold." dense>
        <Grid min={180} gap="sm">
          {MATURITY_LADDER.map((state) => (
            <StatTile
              key={state}
              label={humanize(state)}
              value={maturityBadge(state)}
              tone={maturityTone(state)}
              source="providers/maturity.py STATES"
            />
          ))}
        </Grid>
        {summary.data ? <p className="ym-hint">{summary.data.note}</p> : null}
        {globalList.data ? <p className="ym-hint">{globalList.data.note}</p> : null}
      </Panel>

      <Tabs tabs={maturityTabs} active={tab} onChange={(id) => setTab(id as Tab)} />

      {tab === "llm" ? (
        <MaturityGroup group="llm" items={grouped.llm ?? []} selected={selected} onSelect={select} query={registry} />
      ) : null}
      {tab === "tts" ? (
        <>
          <MaturityGroup group="tts" items={grouped.tts ?? []} selected={selected} onSelect={select} query={registry} />
          <TtsQualificationPanel query={ttsQualification} />
        </>
      ) : null}
      {tab === "music" ? (
        <MaturityGroup group="music" items={grouped.music ?? []} selected={selected} onSelect={select} query={registry} />
      ) : null}
      {tab === "image" ? (
        <MaturityGroup group="image" items={grouped.image ?? []} selected={selected} onSelect={select} query={registry} />
      ) : null}
      {tab === "video" ? (
        <MaturityGroup group="video" items={grouped.video ?? []} selected={selected} onSelect={select} query={registry} />
      ) : null}
      {tab === "intelligence" ? <IntelligenceGroup routing={routing} /> : null}
      {tab === "publishing" ? <PublishingGroup distribution={distribution} /> : null}
      {tab === "other" ? (
        <MaturityGroup group="other" items={unmapped} selected={selected} onSelect={select} query={registry} />
      ) : null}
      {tab === "credentials" ? <CredentialsPanel connections={connections} registry={registry} /> : null}
    </>
  );
}

/** A registry route with no workspace prefix, under `/api/v1`. */
function useQueryGlobal<T>(path: string): QueryState<T> {
  return useQuery<T>(() => api("GET", path));
}
/* Intelligence — what the DecisionEngine actually did, and what it cost.
 *
 *   GET  /intelligence/decisions/log             the decision audit log
 *   GET  /intelligence/decisions/shadow-report   shadow agreement / latency / cost
 *   GET  /intelligence/routing/health            provider-slot health
 *   GET  /intelligence/routing/log               routing picks with their reason
 *   GET  /intelligence/routing/chains            routed chains, leg by leg
 *   GET  /intelligence/verification/ledger       verifier verdicts + digest chain
 *   GET  /provider-maturity/incidents            paid submissions with unknown fate
 *
 * THREE RULES THIS SCREEN EXISTS TO ENFORCE
 *
 * 1. NO PROMPT TEXT. `_record_dto` in `api/v1/intelligence_decisions.py` returns
 *    `input` and `output` verbatim. Those are the prompt and the model's answer
 *    -- exactly the two fields that may carry a private instruction, or a secret
 *    a model was told to repeat back. Neither is rendered anywhere on this
 *    screen: not in a table, not in a tooltip, not in a detail row. The
 *    `DecisionRecord` type below does not even declare them, so a field the
 *    screen cannot name cannot be leaked by it. Every column is chosen to be
 *    answerable without the payload: which provider, which model, how long, how
 *    much, did it fall back and why.
 *
 * 2. AMBIGUITY IS NOT A WARNING. `SUBMISSION_UNKNOWN` means the provider may have
 *    accepted AND billed a request. `toneForStatus` maps it to the `unknown`
 *    tone for exactly this reason, so every state on this screen is drawn through
 *    that map and never through a hand-picked "warning". An operator who reads
 *    an ambiguous paid submission as an ordinary failure retries it, and that is
 *    how one invoice becomes two.
 *
 * 3. NOTHING HERE IS RETRYABLE. There is no retry control on this screen at all.
 *    `paid_executor.RetrySafety` is the only thing that may declare a paid submit
 *    safe to resend, and `retry_safe` is rendered as data so a human (or a CLI)
 *    decides. A UI-level "try again" cannot know, and guessing wrong bills the
 *    workspace twice.
 *
 * UNAVAILABLE, NOT ZERO. Every figure below is `<StatTile unavailable />` when
 * its endpoint failed or has not landed. A decision engine reporting zero
 * decisions during a database outage is indistinguishable from a workspace that
 * has decided nothing, and those two demand opposite responses.
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
  Select,
  StatTile,
  StatusBadge,
  Tabs,
  humanize,
  toneForStatus,
  type Column,
  type Tone,
} from "../../design-system/primitives";
import { useWsQuery, type QueryState } from "../../api/queries";
import { useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes — every field below is transcribed from a router's own
 * return statement or a dataclass `to_dict`. Nothing here is inferred from an
 * example payload, and nothing here is inferred at all.
 * ======================================================================= */

/* `intelligence_decisions._record_dto`.
 * `input` and `output` exist on the wire and are DELIBERATELY NOT DECLARED. */
export type DecisionRecord = {
  id: string;
  kind: string;
  /** DISABLED | SHADOW | ASSISTED | PRIMARY (engine/intelligence/decision.py MODES) */
  mode: string;
  requested_provider: string;
  actual_provider: string;
  model: string;
  latency_ms: number | null;
  cost_usd: number | null;
  /** Why the requested provider was not the one that answered. */
  fallback_reason: string;
  /** null when the run was not a shadow comparison. */
  agree: boolean | null;
  created_at: string | null;
};

export type DecisionLog = { items: DecisionRecord[] };

/* `engine/intelligence/shadow.shadow_report` */
export type ShadowBucket = {
  total: number;
  agreed: number;
  /** null at zero samples -- a rate over an empty set is not 0%. */
  agreement_rate: number | null;
};
export type ShadowReport = {
  workspace_id: string;
  kind: string | null;
  total: number;
  agreed: number;
  disagreed: number;
  agreement_rate: number | null;
  avg_latency_ms: number | null;
  /* NOT FIXED (Work 16.5.7 §8): `backend/app/engine/intelligence/shadow.py` was
   * outside the permitted write scope, so `sum(costs)` over an EMPTY run list
   * still answers $0.000000 — which reads as "shadow runs are free", the
   * opposite of what one is. Typed `number` ON PURPOSE: the endpoint never sends
   * null today, and widening the type would let the compiler accept a value it
   * cannot produce. Recorded in docs/ANALYTICS_HONESTY_AUDIT.json. */
  total_cost_usd: number;
  by_kind: Record<string, ShadowBucket>;
};

/* `intelligence_routing.routing_health` -> `ModelCapabilityRegistry.health()` */
export type RoutingHealth = { providers: Record<string, boolean> };

/* `ModelRouter._record` -- one routing pick. The router logs the decision, not
 * the prompt, so this entry has no payload field at all. */
export type RoutingLogEntry = {
  at: string;
  workspace_id: string;
  task_type: string;
  tier: string;
  model: string;
  remote: boolean;
  reason: string;
  fallbacks: string[];
};
export type RoutingLog = { entries: RoutingLogEntry[] };

/* `ChainLeg.to_dict` (frozen dataclass -> asdict). */
export type ChainLeg = {
  tier: string;
  /** `ExecutionTarget`: "local" | "remote" | "auto" -- where the leg RAN. */
  target: string;
  outcome: string;
  /** Whether reaching this target can incur a billable request. */
  paid: boolean;
  attempt: number;
  paid_attempt: number;
  model: string;
  model_source: string;
  estimated_usd: number;
  fell_through: boolean;
  reason: string;
};

/* `ChainRecord.to_dict`. */
export type ChainRecord = {
  workspace_id: string;
  task_type: string;
  policy: Record<string, unknown>;
  legs: ChainLeg[];
  /** Why the chain stopped. Non-empty means it did not simply succeed. */
  stop_reason: string;
  paid_legs: number;
  estimated_exposure_usd: number;
  at: string;
  succeeded_tier: string;
};
export type RoutingChains = { chains: ChainRecord[]; note: string };

/* `intelligence_evidence._record_dto` */
export type VerificationRecord = {
  id: string;
  workspace_id: string;
  kind: string;
  subject_id: string;
  execution_status: string;
  verification_status: string;
  checks: unknown[];
  digest: string;
  prev_digest: string;
  created_at: string;
};
export type VerificationLedger = {
  items: VerificationRecord[];
  /** `ledger.verify_chain` -- recomputed over every row on read. */
  chain: { ok: boolean; count: number; broken_at: string | null };
};

/* `providers._incident_row` -- the paid-submission incident vocabulary. */
export type PaidIncident = {
  incident_id: string;
  source: string;
  provider: string;
  operation: string;
  attempted_at: string;
  /** The provider's own id, when one came back. The reconciliation handle. */
  remote_id: string;
  state: string;
  display_state: string;
  /** `CostOutcome`, including UNKNOWN_EXPOSURE. */
  exposure: string;
  estimated_exposure_usd: number | null;
  exposure_unknown: boolean;
  /** `paid_executor.Reconciliation`, derived server-side, not re-derived here. */
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
 * Derived helpers -- pure and exported so the rules are testable, not implied
 * ======================================================================= */

/** Human label for a routing leg's execution target. Never inferred from `paid`. */
export function targetLabel(target: string | null | undefined): string {
  const t = (target ?? "").toLowerCase();
  if (t === "remote") return "REMOTE GATEWAY";
  if (t === "local") return "LOCAL PROCESS";
  if (t === "auto") return "AUTO (per tier)";
  return "UNREPORTED";
}

/**
 * The tone an incident's state is drawn with.
 *
 * `toneForStatus` and nothing else. The temptation is to hand-pick "warning" for
 * `SUBMISSION_UNKNOWN`; that is the exact confusion the `unknown` tone exists to
 * prevent, so the map is used as-is and this function exists only to make the
 * choice visible and testable.
 */
export function incidentTone(state: string | null | undefined): Tone {
  return toneForStatus(state);
}

/**
 * Whether the backend proved an incident safe to re-send.
 *
 * False for anything it did not prove, including a row whose fields are absent.
 * The UI never infers this.
 */
export function isResubmittable(
  incident: Pick<PaidIncident, "retry_safe" | "may_resubmit">,
): boolean {
  return incident.retry_safe === true && incident.may_resubmit === true;
}

/** `null` renders "unknown"; `0` renders $0.0000. A missing cost is not free. */
function money(value: number | null | undefined): React.ReactNode {
  return <Money usd={typeof value === "number" && Number.isFinite(value) ? value : null} />;
}

function pct(value: number | null | undefined): React.ReactNode {
  return value === null || value === undefined ? (
    <span className="ym-muted" title="No samples: a rate over an empty set is not zero">
      UNAVAILABLE
    </span>
  ) : (
    `${(value * 100).toFixed(1)}%`
  );
}

function when(value: string | null | undefined): React.ReactNode {
  return value ? (
    value.replace("Z", " UTC").replace("T", " ")
  ) : (
    <span className="ym-muted">—</span>
  );
}

/* The prompt and the model's answer are never rendered. This says so in the
 * row, so an operator does not go looking for a field that is withheld. */
function WithheldPayload() {
  return (
    <span
      className="ym-muted"
      title="The record carries a prompt and a model answer. Neither is fetched into this screen's types or rendered."
    >
      withheld
    </span>
  );
}

function ReadFailure<T>({
  name,
  path,
  query,
}: {
  name: string;
  path: string;
  /* Generic because `QueryState<T>` is invariant in `T` -- `setData` both
   * consumes and produces a `T` -- so it is not assignable to `unknown`. */
  query: QueryState<T>;
}) {
  if (!query.error) return null;
  return (
    <p className="ym-error" role="alert">
      {name} could not be read from {path} ({query.error}). Every figure that
      depends on it reads UNAVAILABLE rather than zero.
    </p>
  );
}

/* ==========================================================================
 * At a glance
 * ======================================================================= */

function AtAGlance({
  log,
  shadow,
  health,
  chains,
  incidents,
}: {
  log: QueryState<DecisionLog>;
  shadow: QueryState<ShadowReport>;
  health: QueryState<RoutingHealth>;
  chains: QueryState<RoutingChains>;
  incidents: QueryState<PaidIncidents>;
}) {
  const decisionCost = useMemo(
    () =>
      log.data === null
        ? null
        : log.data.items.reduce((sum, r) => sum + (typeof r.cost_usd === "number" ? r.cost_usd : 0), 0),
    [log.data],
  );

  const downSlots = useMemo(() => {
    if (health.data === null) return null;
    return Object.entries(health.data.providers ?? {})
      .filter(([, ok]) => !ok)
      .map(([slot]) => slot);
  }, [health.data]);

  const paidExposure = useMemo(
    () =>
      chains.data === null
        ? null
        : chains.data.chains.reduce(
            (sum, c) => sum + (typeof c.estimated_exposure_usd === "number" ? c.estimated_exposure_usd : 0),
            0,
          ),
    [chains.data],
  );

  const unknownFate = useMemo(
    () =>
      incidents.data === null
        ? null
        : incidents.data.items.filter((i) => i.state === "SUBMISSION_UNKNOWN").length,
    [incidents.data],
  );

  const fallbackCount = useMemo(
    () =>
      log.data === null
        ? null
        : log.data.items.filter((r) => (r.fallback_reason ?? "") !== "").length,
    [log.data],
  );

  return (
    <Panel
      title="At a glance"
      subtitle="Every tile names the endpoint it came from. A failed or pending read is UNAVAILABLE, never 0."
      dense
    >
      <Grid min={190} gap="sm">
        <StatTile
          label="Decisions recorded"
          value={log.data?.items.length}
          unavailable={log.data === null}
          source="GET /intelligence/decisions/log"
        />
        <StatTile
          label="Decision cost"
          value={money(decisionCost)}
          unavailable={decisionCost === null}
          source="GET /intelligence/decisions/log"
        />
        <StatTile
          label="Fell back"
          value={fallbackCount}
          unavailable={fallbackCount === null}
          tone={fallbackCount && fallbackCount > 0 ? "warning" : "neutral"}
          hint="The requested provider was not the one that answered."
          source="GET /intelligence/decisions/log"
        />
        <StatTile
          label="Shadow agreement"
          value={pct(shadow.data?.agreement_rate)}
          unavailable={shadow.data === null}
          hint="No samples is UNAVAILABLE, not 0%."
          source="GET /intelligence/decisions/shadow-report"
        />
        <StatTile
          label="Unhealthy provider slots"
          value={downSlots === null ? undefined : downSlots.length}
          unavailable={downSlots === null}
          tone={downSlots === null ? "neutral" : downSlots.length > 0 ? "danger" : "success"}
          source="GET /intelligence/routing/health"
        />
        <StatTile
          label="Estimated paid exposure"
          value={paidExposure === null ? undefined : <Money usd={paidExposure} tone="unknown" />}
          unavailable={paidExposure === null}
          tone={paidExposure === null ? "neutral" : "unknown"}
          hint="What these chains say may already have been billed."
          source="GET /intelligence/routing/chains"
        />
        <StatTile
          label="Submissions of unknown fate"
          value={unknownFate}
          unavailable={unknownFate === null}
          tone={unknownFate === null ? "neutral" : "unknown"}
          hint="May already be an invoice. Never shown as FAILED, never retryable here."
          source="GET /provider-maturity/incidents"
        />
        <StatTile
          label="Unknown-exposure entries"
          value={incidents.data?.unknown_exposure_count}
          unavailable={incidents.data === null}
          tone={incidents.data === null ? "neutral" : "unknown"}
          source="GET /provider-maturity/incidents"
        />
      </Grid>
      {downSlots && downSlots.length > 0 ? (
        <p className="ym-error">
          Down provider slots: {downSlots.join(", ")}. Routing to a down slot is
          what produces a fallback; the reason is on each routing entry.
        </p>
      ) : null}
      <ReadFailure name="The decision log" path="/intelligence/decisions/log" query={log} />
      <ReadFailure
        name="The shadow report"
        path="/intelligence/decisions/shadow-report"
        query={shadow}
      />
      <ReadFailure name="Routing health" path="/intelligence/routing/health" query={health} />
      <ReadFailure name="The chain log" path="/intelligence/routing/chains" query={chains} />
      <ReadFailure name="The incident list" path="/provider-maturity/incidents" query={incidents} />
    </Panel>
  );
}

/* ==========================================================================
 * Decision log
 * ======================================================================= */

function DecisionLogPanel({ query }: { query: QueryState<DecisionLog> }) {
  const [kind, setKind] = useState("");
  const rows = query.data?.items ?? [];
  const kinds = useMemo(() => Array.from(new Set(rows.map((r) => r.kind))).sort(), [rows]);
  const filtered = kind === "" ? rows : rows.filter((r) => r.kind === kind);

  const columns: Column<DecisionRecord>[] = [
    { key: "when", header: "Recorded", cell: (r) => when(r.created_at), width: "170px" },
    { key: "kind", header: "Decision", cell: (r) => <strong>{humanize(r.kind)}</strong> },
    {
      key: "mode",
      header: "Mode",
      cell: (r) => (
        <Badge tone={r.mode === "PRIMARY" ? "info" : "neutral"} title="PRIMARY lets the model's answer decide.">
          {humanize(r.mode)}
        </Badge>
      ),
    },
    {
      key: "provider",
      header: "Requested → answered by",
      cell: (r) => (
        <span>
          <span className="ym-muted">{r.requested_provider || "none requested"}</span> →{" "}
          <strong>{r.actual_provider || "UNAVAILABLE"}</strong>
        </span>
      ),
    },
    {
      key: "model",
      header: "Model",
      cell: (r) =>
        r.model ? r.model : (
          <span className="ym-muted" title="The router resolved no concrete provider model.">
            UNRESOLVED
          </span>
        ),
      hideBelow: "md",
    },
    {
      key: "fallback",
      header: "Why it fell back",
      cell: (r) =>
        r.fallback_reason ? (
          <Badge tone="warning" title={r.fallback_reason}>
            {r.fallback_reason.slice(0, 46)}
          </Badge>
        ) : (
          <span className="ym-muted">no fallback</span>
        ),
      hideBelow: "lg",
    },
    {
      key: "latency",
      header: "Latency",
      align: "right",
      cell: (r) =>
        r.latency_ms === null || r.latency_ms === undefined ? (
          <span className="ym-muted">UNAVAILABLE</span>
        ) : (
          `${r.latency_ms} ms`
        ),
      hideBelow: "md",
    },
    { key: "cost", header: "Cost", align: "right", cell: (r) => money(r.cost_usd) },
    {
      key: "agree",
      header: "Agrees",
      cell: (r) =>
        r.agree === null || r.agree === undefined ? (
          <span className="ym-muted">not a shadow run</span>
        ) : (
          <Badge tone={r.agree ? "success" : "warning"}>{r.agree ? "agrees" : "disagrees"}</Badge>
        ),
      hideBelow: "lg",
    },
    { key: "payload", header: "Prompt / answer", cell: () => <WithheldPayload />, hideBelow: "lg" },
  ];

  return (
    <Panel
      title="Decision audit log"
      subtitle="Provider, model, latency, cost and the fallback reason. The prompt and the model's answer are not rendered anywhere on this screen."
      actions={
        <Select
          label="Kind"
          aria-label="Filter by decision kind"
          value={kind}
          onChange={(e) => setKind(e.target.value)}
        >
          <option value="">All kinds</option>
          {kinds.map((k) => (
            <option key={k} value={k}>
              {humanize(k)}
            </option>
          ))}
        </Select>
      }
      dense
    >
      <QueryBoundary query={query} skeletonRows={8}>
        {() => (
          <DataTable
            rows={filtered}
            columns={columns}
            rowKey={(r) => r.id}
            caption="Decision engine audit log"
            maxHeight={520}
            empty={kind === "" ? "No decision recorded yet" : `No ${humanize(kind).toLowerCase()} decision recorded`}
            emptyHint="A row appears when the DecisionEngine answers. SHADOW mode still runs the model and records the row, but its answer does not decide."
          />
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Shadow comparison
 * ======================================================================= */

function ShadowPanel({ query }: { query: QueryState<ShadowReport> }) {
  const rows = useMemo(() => {
    const byKind = query.data?.by_kind ?? {};
    return Object.entries(byKind).map(([kind, b]) => ({ kind, ...b }));
  }, [query.data]);

  return (
    <Panel
      title="Shadow comparison"
      subtitle="In SHADOW mode the model runs and is recorded, but the deterministic answer still decides. Agreement is the only evidence that would justify trusting it later."
      dense
    >
      <QueryBoundary query={query} skeletonRows={4}>
        {(d) => (
          <>
            <Grid min={175} gap="sm">
              <StatTile label="Shadow runs" value={d.total} source="GET /intelligence/decisions/shadow-report" />
              <StatTile label="Agreed" value={d.agreed} tone="success" source="GET /intelligence/decisions/shadow-report" />
              <StatTile
                label="Disagreed"
                value={d.disagreed}
                tone={d.disagreed > 0 ? "warning" : "neutral"}
                source="GET /intelligence/decisions/shadow-report"
              />
              <StatTile
                label="Agreement rate"
                value={pct(d.agreement_rate)}
                hint="No samples is UNAVAILABLE, not 0%."
                source="GET /intelligence/decisions/shadow-report"
              />
              <StatTile
                label="Average latency"
                value={
                  d.avg_latency_ms === null || d.avg_latency_ms === undefined ? (
                    <span className="ym-muted">UNAVAILABLE</span>
                  ) : (
                    `${d.avg_latency_ms} ms`
                  )
                }
                source="GET /intelligence/decisions/shadow-report"
              />
              <StatTile
                label="Shadow spend"
                /* NOT FIXED (§8, out of scope): $0.0000 here can mean "no
                 * shadow run exists" as well as "shadow runs are free". Stated
                 * rather than hidden — the number is shown because it is what
                 * the endpoint sends, and the caveat travels with it. */
                value={money(d.total_cost_usd)}
                hint="A shadow run costs money even though it changes nothing. Known fabrication: $0.0000 also means no shadow run exists."
                source="GET /intelligence/decisions/shadow-report"
              />
            </Grid>
            <DataTable
              rows={rows}
              columns={[
                { key: "kind", header: "Decision kind", cell: (r) => humanize(r.kind) },
                { key: "total", header: "Runs", align: "right", cell: (r) => r.total },
                { key: "agreed", header: "Agreed", align: "right", cell: (r) => r.agreed },
                { key: "rate", header: "Agreement", align: "right", cell: (r) => pct(r.agreement_rate) },
              ]}
              rowKey={(r) => r.kind}
              caption="Shadow agreement by decision kind"
              empty="No shadow run recorded"
              emptyHint="A comparison only happens while the workspace decision mode is SHADOW. In PRIMARY mode the model's answer wins and there is nothing to compare it against."
            />
          </>
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Routing
 * ======================================================================= */

function RoutingPanel({
  health,
  log,
  chains,
}: {
  health: QueryState<RoutingHealth>;
  log: QueryState<RoutingLog>;
  chains: QueryState<RoutingChains>;
}) {
  const healthColumns: Column<[string, boolean]>[] = [
    { key: "slot", header: "Provider slot", cell: ([slot]) => <strong>{slot}</strong> },
    {
      key: "health",
      header: "Health",
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
      key: "effect",
      header: "What this state does",
      cell: ([, ok]) =>
        ok ? (
          <span className="ym-muted">used until it fails, then recorded as a fallback</span>
        ) : (
          <span>the router skips it and records a fallback reason</span>
        ),
      hideBelow: "md",
    },
  ];

  const logColumns: Column<RoutingLogEntry>[] = [
    { key: "at", header: "At", cell: (e) => when(e.at), width: "190px" },
    { key: "task", header: "Task", cell: (e) => humanize(e.task_type) },
    { key: "tier", header: "Tier", cell: (e) => <Badge tone="info">{e.tier || "UNREPORTED"}</Badge> },
    {
      key: "model",
      header: "Model",
      cell: (e) =>
        e.model ? e.model : (
          <span className="ym-muted" title="No concrete provider model was resolved.">
            UNRESOLVED
          </span>
        ),
    },
    {
      key: "where",
      header: "Ran",
      cell: (e) => (
        <Badge
          tone={e.remote ? "live" : "neutral"}
          title={e.remote ? "Against a remote provider gateway." : "Inside this process."}
        >
          {e.remote ? "REMOTE" : "LOCAL"}
        </Badge>
      ),
    },
    {
      key: "reason",
      header: "Why this tier",
      cell: (e) => <span className="ym-notif-detail">{e.reason || "—"}</span>,
      hideBelow: "lg",
    },
    {
      key: "fallbacks",
      header: "Fallback order",
      cell: (e) =>
        (e.fallbacks ?? []).length ? (
          <span>
            {e.fallbacks.map((f) => (
              <Badge key={f} tone="neutral">
                {f}
              </Badge>
            ))}
          </span>
        ) : (
          <span className="ym-muted">none</span>
        ),
      hideBelow: "md",
    },
  ];

  const legColumns: Column<ChainLeg>[] = [
    { key: "tier", header: "Tier", cell: (l) => <strong>{l.tier || "—"}</strong> },
    { key: "target", header: "Execution target", cell: (l) => targetLabel(l.target) },
    {
      key: "paid",
      header: "Billable leg",
      cell: (l) =>
        l.paid ? (
          <Badge tone="unknown" title="Reaching this target can incur a billed request.">
            PAID
          </Badge>
        ) : (
          <Badge tone="neutral">no charge</Badge>
        ),
    },
    {
      key: "attempt",
      header: "Attempt",
      align: "right",
      cell: (l) => `${l.attempt}${l.paid_attempt ? ` (paid ${l.paid_attempt})` : ""}`,
    },
    {
      key: "model",
      header: "Model",
      cell: (l) =>
        l.model ? l.model : <span className="ym-muted">{l.model_source || "UNRESOLVED"}</span>,
      hideBelow: "md",
    },
    {
      key: "outcome",
      header: "Outcome",
      cell: (l) => <Badge tone={toneForStatus(l.outcome)}>{humanize(l.outcome)}</Badge>,
    },
    {
      key: "est",
      header: "Est. USD",
      align: "right",
      cell: (l) => <Money usd={l.estimated_usd} tone={l.paid ? "unknown" : undefined} />,
    },
    {
      key: "reason",
      header: "Why",
      cell: (l) =>
        l.reason ? <span className="ym-notif-detail">{l.reason}</span> : <span className="ym-muted">—</span>,
      hideBelow: "lg",
    },
  ];

  const chainRows = chains.data?.chains ?? [];

  const chainColumns: Column<ChainRecord>[] = [
    { key: "at", header: "At", cell: (c) => when(c.at), width: "190px" },
    { key: "task", header: "Task", cell: (c) => humanize(c.task_type) },
    {
      key: "succeeded",
      header: "Succeeded on",
      cell: (c) =>
        c.succeeded_tier ? (
          <Badge tone="success">{c.succeeded_tier}</Badge>
        ) : (
          <Badge tone="unknown" title="No tier succeeded; the request did not complete.">
            NO TIER SUCCEEDED
          </Badge>
        ),
    },
    { key: "legs", header: "Legs", align: "right", cell: (c) => c.legs.length },
    {
      key: "paid",
      header: "Billable legs",
      align: "right",
      cell: (c) => <Badge tone={c.paid_legs > 0 ? "unknown" : "neutral"}>{c.paid_legs}</Badge>,
    },
    {
      key: "exposure",
      header: "Est. exposure",
      align: "right",
      cell: (c) => (
        <Money
          usd={typeof c.estimated_exposure_usd === "number" ? c.estimated_exposure_usd : null}
          tone={c.paid_legs > 0 ? "unknown" : undefined}
        />
      ),
    },
    {
      key: "stop",
      header: "Stopped because",
      cell: (c) =>
        c.stop_reason ? (
          <Badge tone="warning">{c.stop_reason.slice(0, 60)}</Badge>
        ) : (
          <span className="ym-muted">no stop reason</span>
        ),
      hideBelow: "lg",
    },
  ];

  return (
    <>
      <Panel
        title="Provider-slot health"
        subtitle="GET /intelligence/routing/health. A slot reports the registry's view of that provider, not a fresh latency probe."
        dense
      >
        <QueryBoundary query={health} skeletonRows={3}>
          {(d) => (
            <DataTable
              rows={Object.entries(d.providers ?? {})}
              columns={healthColumns}
              rowKey={([slot]) => slot}
              caption="Routing provider slot health"
              empty="No provider slot reported"
              emptyHint="The routing registry declares no slots, so nothing can be routed."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Routing picks"
        subtitle="Which tier each task got and the rule that chose it. The router logs the decision, not the prompt."
        dense
      >
        <QueryBoundary query={log} skeletonRows={6}>
          {(d) => (
            <DataTable
              rows={d.entries ?? []}
              columns={logColumns}
              rowKey={(e, i) => `${e.at}-${i}`}
              caption="Routing log"
              maxHeight={360}
              empty="No routing decision yet"
              emptyHint="The router writes a row each time it picks a tier for this workspace. The log is in-process and bounded; it is not the durable record of what was billed."
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Routed chains, leg by leg"
        subtitle={
          chains.data?.note ??
          "GET /intelligence/routing/chains. A chain records what the walk actually cost: every leg, its execution target, its outcome, and why it stopped."
        }
        dense
      >
        <QueryBoundary query={chains} skeletonRows={5}>
          {(d) => (
            <>
              <DataTable
                rows={d.chains ?? []}
                columns={chainColumns}
                rowKey={(c, i) => `${c.at}-${i}`}
                caption="Routing chains"
                empty="No chain recorded"
                emptyHint="A chain is written when a routed request walks its legs because one failed. A request that resolved to a single tier on the first try may leave no chain."
              />
              {(d.chains ?? []).map((c, i) => (
                <div key={`${c.at}-${i}`}>
                  <p className="ym-notif-detail">
                    Chain at {when(c.at)} — {humanize(c.task_type)} — {c.paid_legs} billable
                    leg(s) — {c.stop_reason ? `stopped: ${c.stop_reason}` : "no stop reason"}
                  </p>
                  <DataTable
                    rows={c.legs ?? []}
                    columns={legColumns}
                    rowKey={(l, j) => `${c.at}-${l.tier}-${j}`}
                    caption="Legs of one routing chain"
                    empty="No leg recorded for this chain"
                    emptyHint="A chain with no leg recorded cannot be explained. Treat it as an incident, not a no-op."
                  />
                </div>
              ))}
            </>
          )}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Evidence + verifier
 * ======================================================================= */

function EvidencePanel({ query }: { query: QueryState<VerificationLedger> }) {
  const chain = query.data?.chain;

  const columns: Column<VerificationRecord>[] = [
    { key: "at", header: "Recorded", cell: (r) => when(r.created_at), width: "190px" },
    { key: "kind", header: "Check", cell: (r) => humanize(r.kind) },
    {
      key: "subject",
      header: "Subject",
      cell: (r) => <span className="ym-notif-detail">{r.subject_id.slice(0, 12)}</span>,
    },
    { key: "exec", header: "Execution", cell: (r) => <StatusBadge status={r.execution_status} /> },
    { key: "verify", header: "Verification", cell: (r) => <StatusBadge status={r.verification_status} /> },
    {
      key: "checks",
      header: "Checks",
      align: "right",
      cell: (r) =>
        Array.isArray(r.checks) ? (
          r.checks.length
        ) : (
          <span className="ym-muted">UNAVAILABLE</span>
        ),
      hideBelow: "md",
    },
    {
      key: "digest",
      header: "Digest",
      cell: (r) =>
        r.digest ? (
          <span className="ym-notif-detail" title={`chained to ${r.prev_digest}`}>
            {r.digest.slice(0, 12)}
          </span>
        ) : (
          <span className="ym-muted">—</span>
        ),
      hideBelow: "lg",
    },
  ];

  return (
    <Panel
      title="Verifier ledger"
      subtitle="Every verification is hash-chained to the one before it, so an edited or removed row is detectable on read."
      dense
    >
      <Grid min={180} gap="sm">
        <StatTile
          label="Records"
          value={query.data?.items.length}
          unavailable={query.data === null}
          source="GET /intelligence/verification/ledger"
        />
        <StatTile
          label="Chain intact"
          value={chain === undefined ? undefined : chain.ok ? "yes" : "BROKEN"}
          unavailable={chain === undefined}
          tone={chain === undefined ? "neutral" : chain.ok ? "success" : "danger"}
          hint={
            chain && !chain.ok
              ? `Recomputed mismatch first seen at record ${chain.broken_at}.`
              : "Recomputed over every row on each read."
          }
          source="GET /intelligence/verification/ledger"
        />
      </Grid>
      <QueryBoundary query={query} skeletonRows={6}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(r) => r.id}
            caption="Verification ledger"
            maxHeight={420}
            empty="No verification recorded"
            emptyHint="A row appears when a verification check runs. A chain that verifies over zero rows is a real answer, not a pass."
          />
        )}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * Paid submissions
 * ======================================================================= */

function IncidentsPanel({ query }: { query: QueryState<PaidIncidents> }) {
  const columns: Column<PaidIncident>[] = [
    { key: "when", header: "Attempted", cell: (i) => when(i.attempted_at), width: "190px" },
    { key: "provider", header: "Provider", cell: (i) => i.provider },
    {
      key: "op",
      header: "Operation",
      cell: (i) => <span className="ym-notif-detail">{i.operation}</span>,
      hideBelow: "md",
    },
    {
      key: "state",
      header: "State",
      /* `incidentTone` IS `toneForStatus`, so SUBMISSION_UNKNOWN lands on the
       * `unknown` tone and cannot be mistaken for a warning or a failure. */
      cell: (i) => (
        <Badge tone={incidentTone(i.state)} dot title={i.note || undefined}>
          {i.state}
        </Badge>
      ),
    },
    {
      key: "exposure",
      header: "Exposure",
      cell: (i) =>
        i.exposure_unknown ? (
          <Badge tone="unknown" title="Nobody can price this call.">
            {i.exposure}
          </Badge>
        ) : (
          money(i.estimated_exposure_usd)
        ),
    },
    {
      key: "action",
      header: "Recommended action",
      cell: (i) => <Badge tone="info">{humanize(i.recommended_action)}</Badge>,
    },
    {
      /* The reconciliation handle. An ambiguous paid submit is settled against
       * the provider's own id, never by sending the request again. */
      key: "remote",
      header: "Remote id",
      cell: (i) =>
        i.remote_id ? (
          <span className="ym-notif-detail">{i.remote_id.slice(0, 18)}</span>
        ) : (
          <span
            className="ym-muted"
            title="No provider id came back, so there is nothing to reconcile against."
          >
            none returned
          </span>
        ),
      hideBelow: "md",
    },
    {
      key: "resend",
      header: "Safe to resend",
      cell: (i) =>
        isResubmittable(i) ? (
          <Badge tone="success" title="The backend proved this submit was never delivered.">
            SAFE
          </Badge>
        ) : (
          <Badge
            tone="unknown"
            title="No proof it was undelivered. Resending risks a second charge."
          >
            NO — RECONCILE
          </Badge>
        ),
    },
    {
      key: "detail",
      header: "Evidence",
      cell: (i) =>
        i.detail ? (
          <span className="ym-notif-detail">{i.detail.slice(0, 80)}</span>
        ) : (
          <span className="ym-muted">—</span>
        ),
      hideBelow: "lg",
    },
  ];

  return (
    <Panel
      title="Paid submissions with an unknown fate"
      subtitle="SUBMISSION_UNKNOWN means the provider may have accepted and billed the request. There is deliberately no retry control here: only the backend's own retry_safe verdict can authorise a resend, and an ambiguous submit has none."
      dense
    >
      <Grid min={190} gap="sm">
        <StatTile
          label="Open incidents"
          value={query.data?.count}
          unavailable={query.data === null}
          source="GET /provider-maturity/incidents"
        />
        <StatTile
          label="Of unknown exposure"
          value={query.data?.unknown_exposure_count}
          unavailable={query.data === null}
          tone={query.data === null ? "neutral" : "unknown"}
          hint="An accepted call nobody can price."
          source="GET /provider-maturity/incidents"
        />
      </Grid>
      <QueryBoundary query={query} skeletonRows={5}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            columns={columns}
            rowKey={(i) => i.incident_id}
            caption="Paid submission incidents"
            maxHeight={440}
            empty="No paid submission is awaiting reconciliation"
            emptyHint="This list holds only submissions whose fate is genuinely ambiguous, plus accepted calls with an unpriceable amount. A confirmed rejection is a FAILED job and is deliberately not here."
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

type Tab = "decisions" | "routing" | "evidence" | "paid";

export default function Intelligence() {
  const { workspaceId, workspace } = useSession();
  const [tab, setTab] = useState<Tab>("decisions");

  const log = useWsQuery<DecisionLog>("/intelligence/decisions/log?limit=50");
  const shadow = useWsQuery<ShadowReport>("/intelligence/decisions/shadow-report");
  const health = useWsQuery<RoutingHealth>("/intelligence/routing/health");
  const routingLog = useWsQuery<RoutingLog>("/intelligence/routing/log");
  const chains = useWsQuery<RoutingChains>("/intelligence/routing/chains");
  const ledger = useWsQuery<VerificationLedger>("/intelligence/verification/ledger");
  const incidents = useWsQuery<PaidIncidents>("/provider-maturity/incidents");

  const reloadAll = () => {
    log.reload();
    shadow.reload();
    health.reload();
    routingLog.reload();
    chains.reload();
    ledger.reload();
    incidents.reload();
  };

  const tabs: { id: Tab; label: string; count?: number }[] = [
    { id: "decisions", label: "Decisions" },
    { id: "routing", label: "Routing" },
    { id: "evidence", label: "Evidence" },
    {
      id: "paid",
      label: "Paid submissions",
      count: incidents.data?.unknown_exposure_count,
    },
  ];

  if (!workspaceId) {
    return (
      <>
        <PageHeader
          title="Intelligence"
          description="Decision records, routing, evidence, and paid exposure."
        />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to inspect"
            description="Every figure here is workspace-scoped. Select a workspace to load its decision and routing records."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Intelligence"
        description={
          workspace?.name
            ? `${workspace.name} — what the DecisionEngine did, what it cost, and what may already be billed.`
            : "What the DecisionEngine did, what it cost, and what may already be billed."
        }
        actions={
          <Button onClick={reloadAll}>Refresh</Button>
        }
      />

      <AtAGlance
        log={log}
        shadow={shadow}
        health={health}
        chains={chains}
        incidents={incidents}
      />

      <Tabs tabs={tabs} active={tab} onChange={(id) => setTab(id as Tab)} />

      {tab === "decisions" ? (
        <>
          <DecisionLogPanel query={log} />
          <ShadowPanel query={shadow} />
        </>
      ) : null}
      {tab === "routing" ? (
        <RoutingPanel health={health} log={routingLog} chains={chains} />
      ) : null}
      {tab === "evidence" ? <EvidencePanel query={ledger} /> : null}
      {tab === "paid" ? <IncidentsPanel query={incidents} /> : null}
    </>
  );
}
/* Planner — the editorial operating system's control surface.
 *
 * SIX VIEWS, ONE IDEA: an idea is only as good as the evidence behind it, and a
 * screen that flattens "we saw this demand" together with "a model suggested
 * this" is how a guess gets scheduled as if it were a measurement.
 *
 *   GET  /planner/opportunities   scored opportunities + basis + the WHY
 *   GET  /opportunities           the SAME rows, with lifecycle + recommendation
 *   GET  /planner/signals         observed signals + their evidence ids
 *   GET  /planner/plans           plans and their items
 *   GET  /planner/calendar        placed entries + capacity vs committed
 *   GET  /planner/policy          the autonomy table, as data
 *   GET  /inbox/autonomy          COMMUNITY autonomy (a different vocabulary)
 *   POST /planner/plan            run a cycle (preview writes nothing)
 *   POST /planner/capacity        declare what the workspace can produce
 *   POST /planner/items/{id}/…    approve | reject | research_more |
 *                                campaign | schedule
 *
 * TWO ENDPOINTS, ONE ROW. `/planner/opportunities` carries the provenance
 * (basis, evidence, freshness, dedupe, the factor record and the one-line WHY)
 * but NOT `lifecycle`; `/opportunities` carries `lifecycle` and `recommendation`
 * but NOT the provenance. They read the same `Opportunity` table, so they are
 * joined on `id` — and where one side is missing, the field renders UNAVAILABLE
 * rather than being copied from the other as if both had reported it.
 *
 * BASIS IS A CORRECTNESS SIGNAL, NOT DECORATION.
 * `engine/planning/opportunities.py` defines the three bases and the rule that
 * matters: RECOMMENDED means *there is no evidence this demand exists*, and its
 * score is capped (RECOMMENDED_SCORE_CAP = 0.35) so an eloquent suggestion can
 * never outrank measured demand. So the three render as three different things
 * with different structure, not as one badge with three labels:
 *
 *   OBSERVED     success tone, an EVIDENCE table with resolvable signal ids
 *   INFERRED     info tone,    the FACTOR record, split measured / unmeasured
 *   RECOMMENDED  warning tone, a REFUSAL panel — no evidence, capped score
 *
 * A missing signal is UNAVAILABLE, never 0: `scoring.factors[*].value` is
 * literally `null` when a factor had no data, and a factor with no data
 * contributed 0 to the score — which is not the same claim as "demand is zero".
 */

import { useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Field,
  Grid,
  Modal,
  Money,
  PageHeader,
  Panel,
  QueryBoundary,
  Select,
  StatTile,
  StatusBadge,
  Tabs,
  Textarea,
  Toggle,
  humanize,
  type Column,
  type Tone,
} from "../../design-system/primitives";
import { useMutation, useWsQuery } from "../../api/queries";
import { wsApi } from "../../lib/api";
import { useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes
 *
 * Typed from the FastAPI routers, NOT from openapi.json (whose response
 * schemas for these paths are empty objects).
 * ======================================================================= */

/* engine/planning/opportunities.py `_factor()` */
export type ScoringFactor = {
  factor: string;
  /** null when the factor had NO data — not when the data was zero. */
  value: number | null;
  weight: number;
  measured: boolean;
  contribution: number;
  why: string;
};

/* engine/planning/opportunities.py `OpportunityScore.to_dict()` */
export type OpportunityScoring = {
  total: number;
  basis: string;
  factors: Record<string, ScoringFactor>;
  notes: string[];
};

/* engine/planning/opportunities.py `build_opportunity()` evidence rows.
   Falls back to `draft.evidence`, whose shape is caller-defined, so every
   field is optional here rather than asserted. */
export type OpportunityEvidence = {
  signal_id?: string;
  source?: string;
  topic?: string;
  observed_at?: string;
  freshness?: string;
  confidence?: number;
  evidence_ids?: string[];
  recurrence?: number;
};

/* api/v1/planner.py `list_opportunities` */
export type PlannerOpportunity = {
  id: string;
  topic: string;
  basis: string;
  basis_meaning: string;
  angle: string;
  audience: string;
  platforms: string[];
  format: string;
  score: number;
  confidence: number;
  freshness: string;
  brand_fit: number;
  evidence: OpportunityEvidence[];
  competition_evidence: Record<string, unknown>;
  estimated_effort_hours: number;
  estimated_cost_usd: number;
  /** NEW | RELATED | DUPLICATE | SATURATED | "" */
  dedupe_verdict: string;
  dedupe_reason: string;
  plan_item_id: string | null;
  scoring: OpportunityScoring | null;
  why: string;
};

export type PlannerOpportunityList = {
  workspace_id: string;
  count: number;
  opportunities: PlannerOpportunity[];
  forbidden_claims: string[];
  note: string;
};

/* api/v1/content.py `list_opportunities` (the /opportunities router) */
export type TrendOpportunity = {
  id: string;
  topic: string;
  source: string;
  score: number;
  components: Record<string, unknown>;
  /** CREATE_NOW | PRODUCE | SKIP | WAIT */
  recommendation: string;
  /** EMERGING | RISING | PEAK | DECLINING | EVERGREEN | UNKNOWN */
  lifecycle: string;
  confidence: number;
  selected: boolean;
  skipped_reason: string;
  source_url: string | null;
  created_at: string;
};

export type TrendOpportunityList = { total: number; items: TrendOpportunity[] };

/* api/v1/planner.py `list_signals` */
export type PlannerSignal = {
  id: string;
  source: string;
  topic: string;
  topic_key: string;
  external_ref: string;
  observed_at: string;
  /** FRESH | AGING | STALE */
  freshness: string;
  scope: string;
  confidence: number;
  status: string;
  evidence_ids: string[];
  recurrence: number;
  /** null until the SAME topic is observed twice — one observation has no rate. */
  velocity: number | null;
  usable_as_demand: boolean;
};

export type PlannerSignalList = {
  workspace_id: string;
  count: number;
  sources: string[];
  signals: PlannerSignal[];
  note: string;
};

/* api/v1/planner.py `_item_dict` */
export type PlanItem = {
  id: string;
  opportunity_id: string | null;
  campaign_id: string | null;
  schedule_entry_id: string | null;
  content_format: string;
  angle: string;
  platforms: string[];
  priority: number;
  target_date: string;
  estimated_cost_usd: number;
  dependencies: string[];
  status: string;
  blocked_reason: string;
  why: Record<string, unknown>;
};

/* api/v1/planner.py `list_plans` */
export type Plan = {
  id: string;
  horizon_days: number;
  goals: string[];
  platforms: string[];
  budget_usd: number;
  /** A COMMITTED ESTIMATE, not an invoice — see models/planning.py. */
  spent_usd: number;
  budget_remaining: number;
  autonomy: string;
  constraints: Record<string, unknown>;
  status: string;
  item_count: number;
  items: PlanItem[];
};

export type PlanList = { workspace_id: string; count: number; plans: Plan[] };

/* api/v1/planner.py `calendar` */
export type PlannerCalendarEntry = {
  id: string;
  platform: string;
  run_at: string;
  status: string;
  content_item_id: string | null;
  campaign_id: string | null;
};

export type CapacityBlock = {
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

export type CommittedLedger = {
  locale: string;
  /** pool -> committed units */
  committed: Record<string, number>;
  committed_cost: number;
  item_count: number;
};

export type PlannerCalendar = {
  workspace_id: string;
  days: number;
  entries: PlannerCalendarEntry[];
  capacity: CapacityBlock;
  committed: CommittedLedger;
  /** pool -> remaining room; null means UNBOUNDED (nothing was declared). */
  remaining: Record<string, number | null>;
  plan_item_count: number;
  note: string;
};

/* models/planning.py `ProductionCapacity.RATE_UNITS` */
export const CAPACITY_POOLS = [
  "shorts",
  "ugc",
  "localization",
  "render",
  "review",
  "longform",
] as const;
export type CapacityPool = (typeof CAPACITY_POOLS)[number];

/** Which declared rate column scales a pool's allowance to the horizon. */
const POOL_RATE_LABEL: Record<CapacityPool, string> = {
  shorts: "shorts/day",
  ugc: "ugc/day",
  localization: "localization/day",
  render: "render hours/day",
  review: "review slots/day",
  longform: "longform/week",
};

/* api/v1/planner.py `policy` -> engine/planning/autonomy.py describe_autonomy */
export type AutonomyFlagRow = {
  rank: number;
  suggests: boolean;
  creates_plan_items: boolean;
  creates_campaign_drafts: boolean;
  starts_research: boolean;
  schedules: boolean;
  advances_production: boolean;
  /** ALWAYS false. Publishing is refused at every planning mode. */
  publishes: boolean;
  note: string;
};

export type PlannerPolicy = {
  modes: string[];
  actions: string[];
  table: Record<string, AutonomyFlagRow>;
  publishes: boolean;
  note: string;
};

/* api/v1/inbox.py `get_autonomy` — COMMUNITY vocabulary, NOT planning. */
export type CommunityAutonomy = {
  autonomy: {
    mode: string;
    classes: string[];
    caps: Record<string, unknown>;
  };
  modes: string[];
  classes: string[];
  defaults: Record<string, unknown>;
};

/* api/v1/planner.py `run_plan` -> engine/planning/engine.py PlanningResult */
export type PlanRunResult = {
  plan_id: string;
  items: {
    id: string;
    opportunity_id: string | null;
    topic: string;
    format: string;
    platforms: string[];
    priority: number;
    status: string;
    blocked_reason: string;
    target_date: string;
    estimated_cost_usd: number;
    why: Record<string, unknown>;
  }[];
  blocked: Record<string, unknown>[];
  suggestions: Record<string, unknown>[];
  notes: string[];
  autonomy: string;
  preview: boolean;
  /** Always false. No planning mode can publish. */
  publishes: boolean;
};

export type PlanItemActionResult = {
  id: string;
  status: string;
  reason?: string;
  blocked_reason?: string;
  schedule_entry_id?: string;
  publishes?: boolean;
};

/* ==========================================================================
 * Basis — the load-bearing distinction
 * ======================================================================= */

const BASIS_TONE: Record<string, Tone> = {
  OBSERVED: "success",
  INFERRED: "info",
  RECOMMENDED: "warning",
};

const BASIS_MEANING: Record<string, string> = {
  OBSERVED: "the demand itself was seen in evidence",
  INFERRED: "derived from evidence by scoring",
  RECOMMENDED: "an AI suggestion with no measurement",
};

/** A value the module refuses to make. An unknown basis is never guessed. */
export const RECOMMENDED_SCORE_CAP = 0.35;

function basisTone(basis: string | null | undefined): Tone {
  return BASIS_TONE[(basis ?? "").toUpperCase()] ?? "unknown";
}

function BasisBadge({ basis }: { basis: string | null | undefined }) {
  const key = (basis ?? "").toUpperCase();
  return (
    <Badge
      tone={basisTone(key)}
      title={BASIS_MEANING[key] ?? `unrecognised basis: ${basis ?? "none"}`}
    >
      {key || "NO BASIS"}
    </Badge>
  );
}

function freshnessTone(freshness: string | null | undefined): Tone {
  const f = (freshness ?? "").toUpperCase();
  if (f === "FRESH") return "success";
  if (f === "AGING") return "warning";
  if (f === "STALE") return "danger";
  return "unknown";
}

/** DUPLICATE and SATURATED are the two verdicts that block planning. */
function dedupeTone(verdict: string | null | undefined): Tone {
  const v = (verdict ?? "").toUpperCase();
  if (v === "NEW") return "success";
  if (v === "RELATED") return "info";
  if (v === "DUPLICATE") return "danger";
  if (v === "SATURATED") return "warning";
  return "unknown";
}

function isoDay(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("Z", "").slice(0, 10);
}

function isoMoment(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("Z", " UTC").replace("T", " ").slice(0, 16);
}

function factors(scoring: OpportunityScoring | null): ScoringFactor[] {
  if (!scoring?.factors) return [];
  return Object.values(scoring.factors);
}

/* ==========================================================================
 * The three basis lanes
 *
 * These are three components on purpose. A single row that swapped a label
 * would still let a reader mistake a suggestion for an observation.
 * ======================================================================= */

function FactorTable({
  rows,
  caption,
  empty,
  emptyHint,
}: {
  rows: ScoringFactor[];
  caption: string;
  /** An unset pool is UNBOUNDED, not zero -- so the empty copy must be able to
   * say that rather than implying a measurement of zero. */
  empty?: string;
  emptyHint?: string;
}) {
  return (
    <DataTable
      rows={rows}
      rowKey={(f) => f.factor}
      caption={caption}
      maxHeight={280}
      empty={empty ?? `No ${caption.toLowerCase()} were recorded`}
      emptyHint={
        emptyHint ?? "The scoring record stored with this opportunity has no such factor."
      }
      columns={[
        { key: "factor", header: "Factor", cell: (f) => <strong>{humanize(f.factor)}</strong> },
        {
          key: "value",
          header: "Value",
          align: "right",
          cell: (f) =>
            f.value === null ? (
              <span className="ym-muted">no data</span>
            ) : (
              f.value.toFixed(3)
            ),
        },
        { key: "weight", header: "Weight", align: "right", cell: (f) => f.weight.toFixed(2) },
        {
          key: "contribution",
          header: "Contribution",
          align: "right",
          cell: (f) => (f.measured ? f.contribution.toFixed(4) : <span className="ym-muted">0 — not measured</span>),
        },
        { key: "why", header: "Why", cell: (f) => <span className="ym-notif-detail">{f.why || "—"}</span> },
      ]}
    />
  );
}

function EvidenceTable({ rows }: { rows: OpportunityEvidence[] }) {
  return (
    <DataTable
      rows={rows}
      rowKey={(e, i) => e.signal_id ?? `${e.source ?? "source"}-${i}`}
      caption="Evidence backing this opportunity"
      maxHeight={260}
      empty="No evidence backs this opportunity"
      emptyHint="No signal resolved to an evidence id, so nothing here was measured."
      columns={[
        {
          key: "source",
          header: "Source",
          cell: (e) => <Badge tone="info">{e.source ?? "unnamed source"}</Badge>,
        },
        { key: "observed", header: "Observed", cell: (e) => isoMoment(e.observed_at) },
        {
          key: "freshness",
          header: "Freshness",
          cell: (e) => (e.freshness ? <Badge tone={freshnessTone(e.freshness)}>{humanize(e.freshness)}</Badge> : <span className="ym-muted">—</span>),
        },
        {
          key: "confidence",
          header: "Confidence",
          align: "right",
          cell: (e) => (typeof e.confidence === "number" ? e.confidence.toFixed(2) : <span className="ym-muted">—</span>),
        },
        {
          key: "recurrence",
          header: "Recurrence",
          align: "right",
          cell: (e) => (typeof e.recurrence === "number" ? e.recurrence : <span className="ym-muted">—</span>),
        },
        {
          key: "evidence",
          header: "Evidence ids",
          cell: (e) => {
            const ids = e.evidence_ids ?? [];
            if (ids.length === 0) return <span className="ym-muted">none</span>;
            return (
              <span className="ym-notif-detail">
                {ids.slice(0, 4).join(", ")}
                {ids.length > 4 ? ` +${ids.length - 4} more` : ""}
              </span>
            );
          },
        },
      ]}
    />
  );
}

/** OBSERVED: the demand was seen. Evidence is the primary artefact. */
function ObservedLane({ opp }: { opp: PlannerOpportunity }) {
  return (
    <Panel
      title="Evidence — the demand was seen"
      subtitle="This opportunity is OBSERVED: the demand itself appears in the records below."
      dense
    >
      <p className="ym-hint">
        An observation is not a demand estimate. Recurrence counts DISTINCT
        observations; one mention is an anecdote, not a trend.
      </p>
      <EvidenceTable rows={opp.evidence ?? []} />
    </Panel>
  );
}

/** INFERRED: derived by scoring. The factor record is the artefact. */
function InferredLane({ opp }: { opp: PlannerOpportunity }) {
  const all = factors(opp.scoring);
  const measured = all.filter((f) => f.measured);
  const unmeasured = all.filter((f) => !f.measured);
  return (
    <>
      <Panel
        title="Derivation — inferred from evidence by scoring"
        subtitle="This opportunity is INFERRED: it is an inference about evidence, not the evidence itself."
        dense
      >
        {all.length === 0 ? (
          <EmptyState
            title="No factor record was stored"
            description="The scoring record for this opportunity is missing, so the score cannot be explained. The score is reported; its derivation is UNAVAILABLE."
          />
        ) : (
          <>
            <p className="ym-hint">
              {measured.length} of {all.length} factors were MEASURED. A factor
              with no data contributes 0 and says so — that is not the same
              claim as the demand being zero.
            </p>
            <FactorTable rows={measured} caption="Measured factors" />
          </>
        )}
      </Panel>
      <Panel title="Unmeasured factors — contributed nothing" dense>
        <FactorTable
          rows={unmeasured}
          caption="Unmeasured factors"
          empty="Every factor was measured"
          emptyHint="Nothing was left out of this score."
        />
      </Panel>
      <ObservedEvidenceOpp opp={opp} />
    </>
  );
}

/**
 * RECOMMENDED: an AI suggestion. Rendered as a REFUSAL to treat it as demand,
 * which is what the backend actually does — it caps the score so a suggestion
 * cannot outrank measured demand, and it may never be scheduled as demand.
 */
function RecommendedLane({ opp }: { opp: PlannerOpportunity }) {
  const capped = (opp.score ?? 0) >= RECOMMENDED_SCORE_CAP;
  return (
    <>
      <Panel
        title="AI recommendation — not a measurement"
        subtitle="This opportunity is RECOMMENDED: there is NO evidence this demand exists."
      >
        <div className="ym-notif-item ym-notif-warning">
          <div className="ym-notif-title">Do not read this as demand</div>
          <div className="ym-notif-detail">
            A RECOMMENDED idea is capped at {RECOMMENDED_SCORE_CAP}
            {capped ? " — and this score is at that cap" : ""} so an eloquent
            suggestion cannot outrank measured demand. It may be planned, but
            only with human eyes on it.
          </div>
        </div>
        <div className="ym-notif-item ym-notif-unknown">
          <div className="ym-notif-title">Measurement</div>
          <div className="ym-notif-detail">
            {(opp.evidence ?? []).length === 0
              ? "No signal resolved to an evidence id. There is nothing behind this score."
              : `${(opp.evidence ?? []).length} evidence row(s) are attached, but the backend classified this idea RECOMMENDED, so the evidence does not establish the demand.`}
          </div>
        </div>
      </Panel>
      <Panel title="Unmeasured factors — contributed nothing" dense>
        <FactorTable
          rows={factors(opp.scoring).filter((f) => !f.measured)}
          caption="Unmeasured factors"
          empty="Every factor was measured"
          emptyHint="Nothing was left out of this score."
        />
      </Panel>
      {opp.scoring?.notes?.length ? (
        <Panel title="Scoring notes" dense>
          <ul>
            {(opp.scoring?.notes ?? []).map((n, i) => (
              <li key={i} className="ym-notif-detail">
                {n}
              </li>
            ))}
          </ul>
        </Panel>
      ) : null}
    </>
  );
}

/** The evidence rows of a non-OBSERVED opportunity, under its own heading. */
function ObservedEvidenceOpp({ opp }: { opp: PlannerOpportunity }) {
  return (
    <Panel title="Signals this was derived from" dense>
      <EvidenceTable rows={opp.evidence ?? []} />
    </Panel>
  );
}

function BasisLanes({ opp }: { opp: PlannerOpportunity }) {
  const basis = (opp.basis ?? "").toUpperCase();
  if (basis === "OBSERVED") return <ObservedLane opp={opp} />;
  if (basis === "RECOMMENDED") return <RecommendedLane opp={opp} />;
  if (basis === "INFERRED") return <InferredLane opp={opp} />;
  return (
    <Panel title="Unrecognised basis" subtitle={`basis: ${opp.basis || "missing"}`}>
      <EmptyState
        title="This row's basis is not one the planner defines"
        description={`The planner defines OBSERVED, INFERRED and RECOMMENDED. "${opp.basis}" is treated as unknown: it is not assumed to be a measurement, and it is not assumed to be a suggestion.`}
      />
    </Panel>
  );
}

/* ==========================================================================
 * Opportunity detail
 * ======================================================================= */

function OpportunityDetail({
  opp,
  trend,
  forbiddenClaims,
  onClose,
}: {
  opp: PlannerOpportunity;
  trend: TrendOpportunity | null;
  forbiddenClaims: string[];
  onClose: () => void;
}) {
  return (
    <>
      <PageHeader
        breadcrumb={<button className="ym-crumb" onClick={onClose}>Planner</button>}
        title={opp.topic || opp.id}
        description={opp.basis_meaning || BASIS_MEANING[(opp.basis ?? "").toUpperCase()] || "No basis meaning was reported."}
        actions={<Button onClick={onClose}>Close</Button>}
      />

      <Panel title="Provenance" dense>
        <Grid min={170} gap="sm">
          <StatTile label="Basis" value={<BasisBadge basis={opp.basis} />} source="GET /planner/opportunities" />
          <StatTile
            label="Lifecycle"
            value={trend ? humanize(trend.lifecycle) : undefined}
            unavailable={!trend}
            hint={trend ? undefined : "GET /opportunities did not return this row"}
            source="GET /opportunities"
          />
          <StatTile
            label="Recommendation"
            value={trend ? humanize(trend.recommendation) : undefined}
            unavailable={!trend}
            source="GET /opportunities"
          />
          <StatTile label="Freshness" value={opp.freshness ? humanize(opp.freshness) : "UNAVAILABLE"} unavailable={!opp.freshness} tone={freshnessTone(opp.freshness)} source="GET /planner/opportunities" />
          <StatTile
            label="Dedupe verdict"
            value={opp.dedupe_verdict ? humanize(opp.dedupe_verdict) : "UNAVAILABLE"}
            unavailable={!opp.dedupe_verdict}
            tone={dedupeTone(opp.dedupe_verdict)}
            hint={opp.dedupe_reason || undefined}
            source="GET /planner/opportunities"
          />
          <StatTile label="Score" value={opp.score.toFixed(4)} source="GET /planner/opportunities" />
          <StatTile label="Confidence" value={opp.confidence.toFixed(2)} source="GET /planner/opportunities" />
          <StatTile label="Est. effort" value={`${opp.estimated_effort_hours}h`} source="estimate, not a measurement" />
        </Grid>
        {opp.dedupe_verdict === "DUPLICATE" || opp.dedupe_verdict === "SATURATED" ? (
          <div className="ym-notif-item ym-notif-warning">
            <div className="ym-notif-title">
              {opp.dedupe_verdict === "DUPLICATE" ? "Blocked as a duplicate" : "Blocked as saturated"}
            </div>
            <div className="ym-notif-detail">
              {opp.dedupe_reason || "The backend recorded no reason for this verdict."}
            </div>
          </div>
        ) : null}
      </Panel>

      <Panel title="Why this score" dense>
        <p className="ym-notif-detail">{opp.why || "The backend recorded no WHY for this score."}</p>
        <Grid min={200} gap="sm">
          <StatTile label="Format" value={opp.format ? humanize(opp.format) : "UNAVAILABLE"} unavailable={!opp.format} />
          <StatTile label="Audience" value={opp.audience || "UNAVAILABLE"} unavailable={!opp.audience} />
          <StatTile
            label="Platforms"
            value={(opp.platforms ?? []).length
              ? (opp.platforms ?? []).join(", ")
              : "UNAVAILABLE"}
            unavailable={(opp.platforms ?? []).length === 0}
          />
          <StatTile label="Angle" value={opp.angle || "UNAVAILABLE"} unavailable={!opp.angle} />
          <StatTile label="Estimated cost" value={<Money usd={opp.estimated_cost_usd} />} hint="Estimate only — not an invoice" />
          <StatTile
            label="Planned as"
            value={opp.plan_item_id ? opp.plan_item_id.slice(0, 8) : "Not planned"}
            hint={opp.plan_item_id ? undefined : "No plan item references this opportunity."}
          />
        </Grid>
      </Panel>

      <BasisLanes opp={opp} />

      <Panel title="Claims this planner refuses to make" dense>
        <p className="ym-hint">
          These are the claims the scoring module is written to never emit
          ({forbiddenClaims.length} recorded server-side). If a screen shows one,
          it was invented.
        </p>
        {forbiddenClaims.length === 0 ? (
          <EmptyState
            title="The forbidden-claim list is UNAVAILABLE"
            description="This response carried no forbidden_claims list, so the screen cannot assert which claims the planner refuses."
          />
        ) : (
          <div>
            {forbiddenClaims.map((c) => (
              <Badge key={c} tone="danger">
                {c}
              </Badge>
            ))}
          </div>
        )}
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Views
 * ======================================================================= */

function OpportunitiesView() {
  const planner = useWsQuery<PlannerOpportunityList>("/planner/opportunities");
  const trend = useWsQuery<TrendOpportunityList>("/opportunities?limit=200");
  const [basisFilter, setBasisFilter] = useState("");
  const [selected, setSelected] = useState<string | null>(null);

  const trendById = useMemo(() => {
    const map = new Map<string, TrendOpportunity>();
    for (const row of trend.data?.items ?? []) map.set(row.id, row);
    return map;
  }, [trend.data]);

  const rows = useMemo(() => {
    const all = planner.data?.opportunities ?? [];
    return basisFilter ? all.filter((o) => (o.basis ?? "").toUpperCase() === basisFilter) : all;
  }, [planner.data, basisFilter]);

  const counts = useMemo(() => {
    const all = planner.data?.opportunities ?? [];
    return {
      observed: all.filter((o) => (o.basis ?? "").toUpperCase() === "OBSERVED").length,
      inferred: all.filter((o) => (o.basis ?? "").toUpperCase() === "INFERRED").length,
      recommended: all.filter((o) => (o.basis ?? "").toUpperCase() === "RECOMMENDED").length,
      unknown: all.filter(
        (o) => !["OBSERVED", "INFERRED", "RECOMMENDED"].includes((o.basis ?? "").toUpperCase()),
      ).length,
      blocked: all.filter((o) => ["DUPLICATE", "SATURATED"].includes((o.dedupe_verdict ?? "").toUpperCase()))
        .length,
    };
  }, [planner.data]);

  const chosen = useMemo(
    () => (selected ? (planner.data?.opportunities ?? []).find((o) => o.id === selected) ?? null : null),
    [selected, planner.data],
  );

  const columns: Column<PlannerOpportunity>[] = [
    {
      key: "topic",
      header: "Topic",
      cell: (o) => (
        <span>
          {o.topic || o.id.slice(0, 8)}
          {o.angle ? <span className="ym-notif-detail"> — {o.angle}</span> : null}
        </span>
      ),
    },
    { key: "basis", header: "Basis", cell: (o) => <BasisBadge basis={o.basis} /> },
    {
      key: "lifecycle",
      header: "Lifecycle",
      cell: (o) => {
        const row = trendById.get(o.id);
        return row ? humanize(row.lifecycle) : <span className="ym-muted">UNAVAILABLE</span>;
      },
    },
    {
      key: "freshness",
      header: "Freshness",
      cell: (o) =>
        o.freshness ? (
          <Badge tone={freshnessTone(o.freshness)}>{humanize(o.freshness)}</Badge>
        ) : (
          <span className="ym-muted">UNAVAILABLE</span>
        ),
    },
    {
      key: "dedupe",
      header: "Dedupe",
      cell: (o) =>
        o.dedupe_verdict ? (
          <Badge tone={dedupeTone(o.dedupe_verdict)} title={o.dedupe_reason || undefined}>
            {humanize(o.dedupe_verdict)}
          </Badge>
        ) : (
          <span className="ym-muted">UNAVAILABLE</span>
        ),
    },
    {
      key: "evidence",
      header: "Evidence",
      align: "right",
      cell: (o) => {
        const n = o.evidence.length;
        // Zero evidence rows is a REAL zero (the list came back empty) — this is
        // not the "field missing" case, which renders UNAVAILABLE below.
        return n > 0 ? (
          <Badge tone="info">{n}</Badge>
        ) : (o.basis ?? "").toUpperCase() === "RECOMMENDED" ? (
          <Badge tone="warning">none — a suggestion</Badge>
        ) : (
          <Badge tone="danger">0</Badge>
        );
      },
    },
    { key: "score", header: "Score", align: "right", cell: (o) => o.score.toFixed(4) },
  ];

  if (chosen) {
    return (
      <OpportunityDetail
        opp={chosen}
        trend={trendById.get(chosen.id) ?? null}
        forbiddenClaims={planner.data?.forbidden_claims ?? []}
        onClose={() => setSelected(null)}
      />
    );
  }

  return (
    <>
      <Panel title="Opportunities by basis" subtitle={planner.data?.note} dense>
        <Grid min={170} gap="sm">
          <StatTile label="Observed" value={planner.data === null ? undefined : counts.observed} unavailable={planner.data === null} tone="success" source="GET /planner/opportunities" />
          <StatTile label="Inferred" value={planner.data === null ? undefined : counts.inferred} unavailable={planner.data === null} tone="info" source="GET /planner/opportunities" />
          <StatTile label="Recommended" value={planner.data === null ? undefined : counts.recommended} unavailable={planner.data === null} tone="warning" hint="Suggestions, not measurements" source="GET /planner/opportunities" />
          <StatTile label="Unrecognised basis" value={planner.data === null ? undefined : counts.unknown} unavailable={planner.data === null} tone={counts.unknown > 0 ? "warning" : "neutral"} hint="Not counted as measured demand" source="GET /planner/opportunities" />
          <StatTile label="Duplicate or saturated" value={planner.data === null ? undefined : counts.blocked} unavailable={planner.data === null} tone={counts.blocked > 0 ? "warning" : "neutral"} source="dedupe_verdict" />
          <StatTile label="Lifecycle coverage" value={trend.data === null ? undefined : `${trendById.size}/${planner.data?.count ?? 0}`} unavailable={trend.data === null || planner.data === null} hint="Rows returned by GET /opportunities" />
        </Grid>
        <div className="ym-grid ym-grid--md" style={{ gridTemplateColumns: "minmax(180px, 260px) auto", alignItems: "end" }}>
          <Select label="Basis" value={basisFilter} onChange={(e) => setBasisFilter(e.target.value)}>
            <option value="">All bases</option>
            <option value="OBSERVED">Observed — seen in evidence</option>
            <option value="INFERRED">Inferred — derived by scoring</option>
            <option value="RECOMMENDED">Recommended — no measurement</option>
          </Select>
          <Button variant="ghost" onClick={() => setBasisFilter("")} disabled={!basisFilter}>
            Clear
          </Button>
        </div>
      </Panel>

      <Panel title="Scored opportunities" subtitle="Select a row for its evidence, factor record and lifecycle." dense>
        <QueryBoundary query={planner} skeletonRows={6}>
          {() => (
            <DataTable
              rows={rows}
              columns={columns}
              rowKey={(o) => o.id}
              caption="Scored opportunities"
              maxHeight={640}
              empty={basisFilter ? `No ${basisFilter.toLowerCase()} opportunity` : "No opportunity scored yet"}
              emptyHint={
                basisFilter
                  ? "The backend returned no opportunity with this basis. Clear the filter to see the others."
                  : "An opportunity appears here once the planner clusters signals into a topic. Nothing has been scored for this workspace."
              }
              onRowClick={(o) => setSelected(o.id)}
            />
          )}
        </QueryBoundary>
        {trend.error ? (
          <p className="ym-error">
            Lifecycle is UNAVAILABLE: GET /opportunities failed ({trend.error}). The basis, evidence and
            factor record above are unaffected — they come from a different endpoint.
          </p>
        ) : null}
      </Panel>
    </>
  );
}

function SignalsView() {
  const signals = useWsQuery<PlannerSignalList>("/planner/signals");
  return (
    <QueryBoundary query={signals} skeletonRows={5}>
      {(d) => (
        <DataTable
          rows={d.signals ?? []}
          rowKey={(s) => s.id}
          caption="Observed signals"
          maxHeight={520}
          empty="No signal observed"
          emptyHint="A signal is one observation with its evidence. None has been ingested for this workspace, so no opportunity can be OBSERVED yet."
          columns={[
            { key: "topic", header: "Topic", cell: (s) => s.topic },
            { key: "source", header: "Source", cell: (s) => <Badge tone="info">{s.source}</Badge> },
            { key: "observed", header: "Observed at", cell: (s) => isoMoment(s.observed_at) },
            { key: "freshness", header: "Freshness", cell: (s) => <Badge tone={freshnessTone(s.freshness)}>{humanize(s.freshness)}</Badge> },
            {
              key: "velocity",
              header: "Velocity",
              align: "right",
              cell: (s) =>
                // A topic seen ONCE has no measurable rate. null is the
                // honest answer and must not render as 0.
                s.velocity === null ? (
                  <span className="ym-muted" title="A single observation has no rate">
                    UNAVAILABLE
                  </span>
                ) : (
                  s.velocity.toFixed(3)
                ),
            },
            { key: "recurrence", header: "Recurrence", align: "right", cell: (s) => s.recurrence },
            {
              key: "evidence",
              header: "Evidence ids",
              cell: (s) => (s.evidence_ids.length ? s.evidence_ids.join(", ") : <span className="ym-muted">none</span>),
            },
            {
              key: "usable",
              header: "Usable as demand",
              cell: (s) => (
                <Badge tone={s.usable_as_demand ? "success" : "neutral"}>
                  {s.usable_as_demand ? "Yes" : "No"}
                </Badge>
              ),
            },
          ]}
        />
      )}
    </QueryBoundary>
  );
}

function CalendarViewSummary({ days }: { days: number }) {
  const navigate = useNavigate();
  const calendar = useWsQuery<PlannerCalendar>(`/planner/calendar?days=${days}`);
  return (
    <>
      <Panel
        title="Open the interactive calendar"
        actions={<Button variant="primary" onClick={() => navigate("/calendar")}>Go to /calendar</Button>}
      >
        <p className="ym-hint">
          Day / week / month, with real reschedule through
          <code> PATCH /calendar/&#123;entry_id&#125;</code> and cancellation
          through <code>DELETE /calendar/&#123;entry_id&#125;</code>. Those two
          controls are only offered for entries the backend will actually
          reschedule.
        </p>
      </Panel>
      <Panel title={`Planner view of the next ${days} days`} dense>
        <QueryBoundary query={calendar} skeletonRows={5}>
          {(d) => (
            <DataTable
              rows={d.entries ?? []}
              rowKey={(e) => e.id}
              caption="Placed schedule entries"
              maxHeight={420}
              empty="Nothing placed in this window"
              emptyHint="No ScheduleEntry exists for this workspace in the selected horizon. Scheduling writes a canonical entry, so an empty calendar means nothing was scheduled."
              columns={[
                { key: "run_at", header: "Run at (UTC)", cell: (e) => isoMoment(e.run_at) },
                { key: "platform", header: "Platform", cell: (e) => humanize(e.platform) },
                { key: "status", header: "State", cell: (e) => <StatusBadge status={e.status} /> },
                {
                  key: "campaign",
                  header: "Campaign",
                  cell: (e) => (e.campaign_id ? e.campaign_id.slice(0, 8) : <span className="ym-muted">—</span>),
                },
                {
                  key: "content",
                  header: "Content",
                  cell: (e) =>
                    e.content_item_id ? (
                      e.content_item_id.slice(0, 8)
                    ) : (
                      <span className="ym-muted" title="No ContentItem has been produced for this entry yet">
                        none produced
                      </span>
                    ),
                },
              ]}
            />
          )}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* --- plan details ---------------------------------------------------------- */

type ItemAction = "approve" | "reject" | "research_more" | "campaign" | "schedule";

function PlanDetailsView() {
  const plans = useWsQuery<PlanList>("/planner/plans");
  const [pending, setPending] = useState<{ item: PlanItem; action: ItemAction } | null>(null);
  const [reason, setReason] = useState("");
  const [lastResult, setLastResult] = useState<string | null>(null);

  const action = useMutation<{ item: PlanItem; action: ItemAction; reason: string }, PlanItemActionResult>(
    async ({ item, action: verb, reason: why }) => {
      const body = { autonomy: "AUTONOMOUS", allowed_actions: [], reason: why, max_daily_spend_usd: 0 };
      switch (verb) {
        case "approve":
          return wsApi.post(`/planner/items/${item.id}/approve`, body) as Promise<PlanItemActionResult>;
        case "reject":
          return wsApi.post(`/planner/items/${item.id}/reject`, body) as Promise<PlanItemActionResult>;
        case "research_more":
          return wsApi.post(`/planner/items/${item.id}/research_more`, body) as Promise<PlanItemActionResult>;
        case "campaign":
          return wsApi.post(`/planner/items/${item.id}/campaign`, body) as Promise<PlanItemActionResult>;
        case "schedule":
          return wsApi.post(`/planner/items/${item.id}/schedule`, body) as Promise<PlanItemActionResult>;
      }
    },
    {
      onSuccess: (result, args) => {
        setLastResult(
          `${args.action} → ${result.status}${result.schedule_entry_id ? ` (entry ${result.schedule_entry_id})` : ""}${result.publishes === false ? " · does not publish" : ""}`,
        );
        setPending(null);
        setReason("");
        plans.reload();
      },
    },
  );

  const closeModal = () => {
    setPending(null);
    setReason("");
    action.reset();
  };

  /* reject REQUIRES a reason (planner.py raises 422 without one), so the modal
     refuses to submit without it rather than letting the server 422. */
  const reasonRequired = pending?.action === "reject";

  return (
    <>
      <Panel title="Plans and their items" subtitle="Newest first. Item actions are the backend's own endpoints." dense>
        <QueryBoundary query={plans} skeletonRows={6}>
          {(d) =>
            (d.plans ?? []).length === 0 ? (
              <EmptyState
                title="No plan has been run"
                description="Run a planning cycle below. At RECOMMEND the planner returns suggestions and persists nothing; APPROVAL or AUTONOMOUS may write plan items."
              />
            ) : (
              (d.plans ?? []).map((plan) => (
                <div key={plan.id} style={{ marginBottom: "var(--space-4)" }}>
                  <Panel
                    title={`Horizon ${plan.horizon_days}d`}
                    subtitle={`autonomy ${plan.autonomy} · ${plan.item_count} item(s)`}
                    dense
                    actions={<StatusBadge status={plan.status} />}
                  >
                    <Grid min={170} gap="sm">
                      <StatTile label="Budget" value={<Money usd={plan.budget_usd} />} source="plan.budget_usd" />
                      <StatTile
                        label="Committed estimate"
                        value={<Money usd={plan.spent_usd} />}
                        hint="An estimate committed against the plan, not an invoice."
                        source="plan.spent_usd"
                      />
                      <StatTile label="Remaining" value={<Money usd={plan.budget_remaining} />} tone={plan.budget_remaining <= 0 ? "warning" : "neutral"} source="budget_usd - spent_usd" />
                      <StatTile
                        label="Goals"
                        value={(plan.goals ?? []).length ? (plan.goals ?? []).join(" · ") : "UNAVAILABLE"}
                        unavailable={(plan.goals ?? []).length === 0}
                      />
                    </Grid>
                    <DataTable
                      rows={plan.items ?? []}
                      rowKey={(i) => i.id}
                      caption={`Plan items for ${plan.id}`}
                      maxHeight={420}
                      empty="This plan has no items"
                      emptyHint="The plan exists but nothing was written into it. That is what RECOMMEND and DISABLED do by design."
                      columns={[
                        { key: "angle", header: "Item", cell: (i) => i.angle || i.id.slice(0, 8) },
                        { key: "format", header: "Format", cell: (i) => humanize(i.content_format) },
                        { key: "status", header: "Status", cell: (i) => <StatusBadge status={i.status} /> },
                        {
                          key: "blocked",
                          header: "Blocker",
                          cell: (i) => (i.blocked_reason ? <Badge tone="danger">{i.blocked_reason}</Badge> : <span className="ym-muted">—</span>),
                        },
                        { key: "target", header: "Target date", cell: (i) => isoDay(i.target_date), hideBelow: "md" },
                        {
                          key: "platforms",
                          header: "Platforms",
                          cell: (i) => (i.platforms.length ? i.platforms.join(", ") : <span className="ym-muted">—</span>),
                          hideBelow: "lg",
                        },
                        { key: "cost", header: "Est. cost", align: "right", cell: (i) => <Money usd={i.estimated_cost_usd} />, hideBelow: "lg" },
                        {
                          key: "actions",
                          header: "Actions",
                          cell: (i) => (
                            <div style={{ display: "flex", gap: "var(--space-1)", flexWrap: "wrap" }}>
                              <Button size="sm" onClick={() => setPending({ item: i, action: "approve" })} disabled={i.status !== "IDEA"}>
                                Approve
                              </Button>
                              <Button size="sm" onClick={() => setPending({ item: i, action: "research_more" })}>
                                More research
                              </Button>
                              <Button size="sm" onClick={() => setPending({ item: i, action: "campaign" })}>
                                Campaign draft
                              </Button>
                              <Button size="sm" onClick={() => setPending({ item: i, action: "schedule" })} disabled={i.status !== "PLANNED"}>
                                Schedule
                              </Button>
                              <Button size="sm" variant="ghost" onClick={() => setPending({ item: i, action: "reject" })}>
                                Reject
                              </Button>
                            </div>
                          ),
                        },
                      ]}
                    />
                  </Panel>
                </div>
              ))
            )
          }
        </QueryBoundary>
      </Panel>

      <RunPlanPanel />

      <Modal
        open={pending !== null}
        onClose={closeModal}
        title={pending ? `${humanize(pending.action)} plan item` : "Plan item action"}
        footer={
          <>
            <Button variant="ghost" onClick={closeModal}>
              Cancel
            </Button>
            <Button
              variant="primary"
              loading={action.pending}
              disabled={reasonRequired && reason.trim().length === 0}
              onClick={() => {
                if (!pending) return;
                void action.run({ item: pending.item, action: pending.action, reason: reason.trim() });
              }}
            >
              {pending ? humanize(pending.action) : "Confirm"}
            </Button>
          </>
        }
      >
        {pending ? (
          <>
            <p className="ym-hint">{pending.item.angle || pending.item.id}</p>
            {pending.action === "reject" ? (
              <>
                <Textarea
                  label="Reason (required)"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
                <p className="ym-hint">
                  The backend returns 422 for a rejection with no reason.
                </p>
              </>
            ) : (
              <>
                <Textarea
                  label="Note (optional)"
                  value={reason}
                  onChange={(e) => setReason(e.target.value)}
                />
                <p className="ym-hint">Stored with the item's WHY record.</p>
              </>
            )}
            {pending.action === "schedule" ? (
              <div className="ym-notif-item ym-notif-warning">
                <div className="ym-notif-title">Scheduling does not publish</div>
                <div className="ym-notif-detail">
                  This writes a ScheduleEntry through the existing store and marks
                  the item SCHEDULED. The Scheduler still owns dispatch and the
                  publication approval path still owns publishing.
                </div>
              </div>
            ) : null}
            {pending.action === "campaign" ? (
              <div className="ym-notif-item ym-notif-info">
                <div className="ym-notif-title">Creating a DRAFT, not publishing</div>
                <div className="ym-notif-detail">
                  The trend → campaign flow is idempotent: re-running it reuses
                  the existing draft rather than making a second one.
                </div>
              </div>
            ) : null}
            {action.error ? <p className="ym-error">{action.error}</p> : null}
          </>
        ) : null}
      </Modal>

      {lastResult ? (
        <Panel title="Last action" dense>
          <p className="ym-notif-detail">{lastResult}</p>
        </Panel>
      ) : null}
    </>
  );
}

function RunPlanPanel() {
  const [horizon, setHorizon] = useState(30);
  const [autonomy, setAutonomy] = useState("RECOMMEND");
  const [goals, setGoals] = useState("");
  const [preview, setPreview] = useState(true);
  const [result, setResult] = useState<PlanRunResult | null>(null);

  const run = useMutation<void, PlanRunResult>(() =>
    wsApi.post("/planner/plan", {
      horizon_days: horizon,
      goals: goals.split("\n").map((g) => g.trim()).filter(Boolean),
      autonomy,
      allowed_actions: [],
      preview,
    }) as Promise<PlanRunResult>,
  );

  return (
    <Panel
      title="Run a planning cycle"
      subtitle="POST /planner/plan — the autonomy mode decides what is actually written."
    >
      <Grid min={170} gap="sm">
        <Field
          label="Horizon (days)"
          type="number"
          min={1}
          max={365}
          value={horizon}
          onChange={(e) => setHorizon(Math.max(1, Number(e.target.value) || 1))}
        />
        <Select label="Autonomy" value={autonomy} onChange={(e) => setAutonomy(e.target.value)}>
          <option value="DISABLED">Disabled — compute, persist nothing</option>
          <option value="RECOMMEND">Recommend — suggestions only</option>
          <option value="APPROVAL">Approval — may write plan items</option>
          <option value="AUTONOMOUS">Autonomous — full planning gate</option>
        </Select>
        <Toggle
          checked={preview}
          onChange={setPreview}
          label={preview ? "Preview only — writes nothing" : "Persist — writes plan items"}
        />
      </Grid>
      <Textarea
        label="Goals (one per line)"
        value={goals}
        onChange={(e) => setGoals(e.target.value)}
      />
      <div style={{ display: "flex", gap: "var(--space-2)" }}>
        <Button
          variant="primary"
          loading={run.pending}
          onClick={() => void run.run().then((r) => r && setResult(r))}
        >
          {preview ? "Preview cycle" : "Run and persist"}
        </Button>
      </div>
      {!preview && autonomy === "RECOMMEND" ? (
        <p className="ym-hint">
          RECOMMEND persists nothing by design. The suggestions still come back.
        </p>
      ) : null}
      {run.error ? <p className="ym-error">{run.error}</p> : null}
      {result ? (
        <>
          <Grid min={170} gap="sm">
            <StatTile label="Items" value={result.items.length} source="POST /planner/plan" />
            <StatTile
              label="Blocked"
              value={(result.blocked ?? []).length}
              tone={(result.blocked ?? []).length ? "warning" : "neutral"} source="POST /planner/plan" />
            <StatTile label="Suggestions" value={result.suggestions.length} source="POST /planner/plan" />
            <StatTile
              label="Publishes"
              value={result.publishes ? "yes" : "no — never"}
              tone={result.publishes ? "danger" : "success"}
              hint="No planning mode can publish."
              source="POST /planner/plan"
            />
          </Grid>
          {result.notes.length ? (
            <ul>
              {result.notes.map((n, i) => (
                <li key={i} className="ym-notif-detail">
                  {n}
                </li>
              ))}
            </ul>
          ) : null}
        </>
      ) : null}
    </Panel>
  );
}

/* --- capacity -------------------------------------------------------------- */

function CapacityView({ days }: { days: number }) {
  const calendar = useWsQuery<PlannerCalendar>(`/planner/calendar?days=${days}`);
  const [locale, setLocale] = useState("");
  const [shorts, setShorts] = useState("");
  const [ugc, setUgc] = useState("");
  const [localization, setLocalization] = useState("");
  const [renderHours, setRenderHours] = useState("");
  const [reviewSlots, setReviewSlots] = useState("");
  const [longform, setLongform] = useState("");
  const [notes, setNotes] = useState("");
  const [saved, setSaved] = useState<string | null>(null);

  const declare = useMutation<void, { locale: string; is_unbounded: boolean }>(() =>
    wsApi.post("/planner/capacity", {
      locale,
      shorts_per_day: Number(shorts) || 0,
      ugc_per_day: Number(ugc) || 0,
      localization_per_day: Number(localization) || 0,
      render_hours_per_day: Number(renderHours) || 0,
      review_slots_per_day: Number(reviewSlots) || 0,
      longform_per_week: Number(longform) || 0,
      notes,
    }) as Promise<{ locale: string; is_unbounded: boolean }>,
    {
      onSuccess: (r) => {
        setSaved(
          r.is_unbounded
            ? "Saved. Every pool is 0, so this locale is UNBOUNDED — the planner will not refuse work for it."
            : `Saved for locale "${r.locale || "default"}".`,
        );
        calendar.reload();
      },
    },
  );

  return (
    <>
      <Panel
        title={`Capacity vs committed over ${days} days`}
        subtitle="Rates are scaled to the horizon before they are compared with a committed COUNT."
        dense
      >
        <QueryBoundary query={calendar} skeletonRows={4}>
          {(d) => (
            <>
              {!d.capacity.declared ? (
                <div className="ym-notif-item ym-notif-unknown">
                  <div className="ym-notif-title">No capacity is declared for this locale</div>
                  <div className="ym-notif-detail">
                    An unset pool is UNBOUNDED, not zero. The planner refuses to
                    invent a limit the operator never set, so the remaining
                    figures below read “unbounded” rather than 0.
                  </div>
                </div>
              ) : null}
              <DataTable
                rows={CAPACITY_POOLS.map((pool) => ({
                  pool,
                  committed: d.committed.committed[pool] ?? 0,
                  remaining: Object.prototype.hasOwnProperty.call(d.remaining, pool)
                    ? d.remaining[pool]
                    : undefined,
                }))}
                rowKey={(r) => r.pool}
                caption="Capacity pools"
                empty="No capacity pools are declared"
                emptyHint="The planner never invents a pool the operator did not set."
                columns={[
                  { key: "pool", header: "Pool", cell: (r) => <strong>{humanize(r.pool)}</strong> },
                  { key: "rate", header: "Declared rate", cell: (r) => POOL_RATE_LABEL[r.pool] },
                  { key: "committed", header: "Committed", align: "right", cell: (r) => r.committed },
                  {
                    key: "remaining",
                    header: `Remaining over ${days}d`,
                    align: "right",
                    cell: (r) => {
                      if (r.remaining === undefined) {
                        return (
                          <span className="ym-muted" title="The backend returned no entry for this pool">
                            UNAVAILABLE
                          </span>
                        );
                      }
                      if (r.remaining === null) {
                        return <Badge tone="unknown">unbounded</Badge>;
                      }
                      return <Badge tone={r.remaining > 0 ? "success" : "danger"}>{r.remaining.toFixed(2)}</Badge>;
                    },
                  },
                ]}
              />
              <Grid min={170} gap="sm">
                <StatTile label="Locale" value={d.capacity.locale || "default (all locales)"} source="capacity.locale" />
                <StatTile label="Plan items counted" value={d.plan_item_count} source="planner/calendar" />
                <StatTile
                  label="Cost committed"
                  value={<Money usd={d.committed.committed_cost} />}
                  hint="Estimate committed against plan items."
                  source="committed.committed_cost"
                />
                <StatTile label="Capacity notes" value={d.capacity.notes || "UNAVAILABLE"} unavailable={!d.capacity.notes} source="capacity.notes" />
              </Grid>
              <p className="ym-hint">{d.note}</p>
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel title="Declare production capacity" subtitle="POST /planner/capacity — admin only. 0 means UNDECLARED, not zero.">
        <Grid min={160} gap="sm">
          <Field label="Locale" value={locale} onChange={(e) => setLocale(e.target.value)} hint="A row is scoped per locale, so one market's limits are not spent on another's work." />
          <Field label="Shorts / day" type="number" min={0} step="0.5" value={shorts} onChange={(e) => setShorts(e.target.value)} />
          <Field label="UGC / day" type="number" min={0} step="0.5" value={ugc} onChange={(e) => setUgc(e.target.value)} />
          <Field label="Localization / day" type="number" min={0} step="0.5" value={localization} onChange={(e) => setLocalization(e.target.value)} />
          <Field label="Render hours / day" type="number" min={0} step="0.5" value={renderHours} onChange={(e) => setRenderHours(e.target.value)} />
          <Field label="Review slots / day" type="number" min={0} step="0.5" value={reviewSlots} onChange={(e) => setReviewSlots(e.target.value)} />
          <Field label="Longform / week" type="number" min={0} step="0.5" value={longform} onChange={(e) => setLongform(e.target.value)} />
        </Grid>
        <Textarea label="Notes" value={notes} onChange={(e) => setNotes(e.target.value)} />
        <Button variant="primary" loading={declare.pending} onClick={() => void declare.run()}>
          Save capacity
        </Button>
        {declare.error ? <p className="ym-error">{declare.error}</p> : null}
        {saved ? <p className="ym-hint">{saved}</p> : null}
      </Panel>
    </>
  );
}

/* --- autonomy -------------------------------------------------------------- */

const AUTONOMY_FLAGS: { key: keyof Omit<AutonomyFlagRow, "rank" | "note">; label: string }[] = [
  { key: "suggests", label: "Suggest" },
  { key: "creates_plan_items", label: "Create plan items" },
  { key: "creates_campaign_drafts", label: "Create campaign drafts" },
  { key: "starts_research", label: "Start research" },
  { key: "schedules", label: "Schedule" },
  { key: "advances_production", label: "Advance production" },
  { key: "publishes", label: "PUBLISH" },
];

function AutonomyView() {
  const policy = useWsQuery<PlannerPolicy>("/planner/policy");
  const community = useWsQuery<CommunityAutonomy>("/inbox/autonomy");

  return (
    <>
      <Panel title="Planning autonomy" subtitle="Rendered from the backend's own gate table — nothing here is hand-ranked." dense>
        <QueryBoundary query={policy} skeletonRows={4}>
          {(d) => (
            <>
              <div className="ym-notif-item ym-notif-info">
                <div className="ym-notif-title">Planning autonomy is not publishing autonomy</div>
                <div className="ym-notif-detail">{d.note}</div>
              </div>
              <DataTable
                rows={(d.modes ?? []).map((mode) => ({ mode, row: d.table?.[mode] }))}
                rowKey={(r) => r.mode}
                caption="Autonomy gate table"
                empty="No autonomy mode was returned"
                emptyHint="GET /planner/policy returned an empty mode list."
                columns={[
                  { key: "mode", header: "Mode", cell: (r) => <strong>{humanize(r.mode)}</strong> },
                  { key: "rank", header: "Rank", align: "right", cell: (r) => (r.row ? r.row.rank : "UNAVAILABLE") },
                  ...AUTONOMY_FLAGS.map<Column<{ mode: string; row: AutonomyFlagRow | undefined }>>((f) => ({
                    key: f.key,
                    header: f.label,
                    cell: (r) => {
                      if (!r.row) return <span className="ym-muted">UNAVAILABLE</span>;
                      const allowed = r.row[f.key];
                      return (
                        <Badge tone={allowed ? "success" : "neutral"}>
                          {allowed ? "allowed" : "refused"}
                        </Badge>
                      );
                    },
                  })),
                ]}
              />
              <Grid min={170} gap="sm">
                <StatTile
                  label="Can any mode publish?"
                  value={d.publishes ? "yes" : "no — PUBLISH is refused at every mode"}
                  tone={d.publishes ? "danger" : "success"}
                  source="GET /planner/policy"
                />
                <StatTile label="Modes declared" value={(d.modes ?? []).length} source="policy.modes" />
                <StatTile label="Gated actions" value={(d.actions ?? []).length} source="policy.actions" />
              </Grid>
            </>
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Community inbox autonomy — a DIFFERENT vocabulary"
        subtitle="GET /inbox/autonomy. These modes govern community replies, not planning."
      >
        <p className="ym-hint">
          Planning autonomy is {`DISABLED / RECOMMEND / APPROVAL / AUTONOMOUS`}.
          Community autonomy is a separate table with separate modes. They are
          shown apart on purpose: merging two autonomy vocabularies into one
          dropdown is how a “reply automatically” switch ends up driving
          publishing.
        </p>
        <QueryBoundary query={community} skeletonRows={3}>
          {(d) => (
            <Grid min={190} gap="sm">
              <StatTile label="Mode" value={humanize(d.autonomy.mode)} source="GET /inbox/autonomy" />
              <StatTile
                label="Auto-reply classes"
                value={(d.autonomy?.classes ?? []).length ? (d.autonomy?.classes ?? []).join(", ") : "None"}
                hint="Nothing is auto-replied until a class is listed."
                source="autonomy.classes"
              />
              <StatTile label="Modes" value={(d.modes ?? []).join(" · ")} source="autonomy.modes" />
              <StatTile
                label="Reply caps"
                value={Object.entries(d.autonomy.caps ?? {})
                  .map(([k, v]) => `${k}: ${String(v)}`)
                  .join(" · ") || "UNAVAILABLE"}
                unavailable={Object.keys(d.autonomy.caps ?? {}).length === 0}
                source="autonomy.caps"
              />
            </Grid>
          )}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* --- budget ---------------------------------------------------------------- */

function BudgetView() {
  const plans = useWsQuery<PlanList>("/planner/plans");
  const run = useWsQuery<PlannerOpportunityList>("/planner/opportunities");

  return (
    <>
      <Panel title="Budget across plans" dense>
        <QueryBoundary query={plans} skeletonRows={4}>
          {(d) => {
            const totals = d.plans.reduce(
              (acc, p) => ({
                budget: acc.budget + p.budget_usd,
                committed: acc.committed + p.spent_usd,
                remaining: acc.remaining + p.budget_remaining,
              }),
              { budget: 0, committed: 0, remaining: 0 },
            );
            return (
              <>
                <Grid min={170} gap="sm">
                  <StatTile label="Declared budget" value={<Money usd={totals.budget} />} source="Σ plan.budget_usd" />
                  <StatTile
                    label="Committed estimate"
                    value={<Money usd={totals.committed} />}
                    hint="spent_usd is the estimate committed against the plan, not a settled invoice."
                    source="Σ plan.spent_usd"
                  />
                  <StatTile label="Remaining" value={<Money usd={totals.remaining} />} tone={totals.remaining <= 0 ? "warning" : "neutral"} source="Σ budget_remaining" />
                  <StatTile
                    label="Actual invoiced spend"
                    unavailable
                    hint="No planner endpoint reports an invoice. The renderer writes its own ledger; this screen does not guess one."
                    source="not exposed by GET /planner/*"
                  />
                </Grid>
                <DataTable
                  rows={d.plans ?? []}
                  rowKey={(p) => p.id}
                  caption="Budget per plan"
                  maxHeight={360}
                  empty="No plan, so no budget"
                  emptyHint="Budget is a property of a plan. Run a planning cycle to create one."
                  columns={[
                    { key: "id", header: "Plan", cell: (p) => p.id.slice(0, 8) },
                    { key: "autonomy", header: "Autonomy", cell: (p) => humanize(p.autonomy) },
                    { key: "budget", header: "Budget", align: "right", cell: (p) => <Money usd={p.budget_usd} /> },
                    { key: "spent", header: "Committed", align: "right", cell: (p) => <Money usd={p.spent_usd} /> },
                    {
                      key: "remaining",
                      header: "Remaining",
                      align: "right",
                      cell: (p) => (
                        <Badge tone={p.budget_remaining > 0 ? "success" : "warning"}>
                          <Money usd={p.budget_remaining} />
                        </Badge>
                      ),
                    },
                    { key: "items", header: "Items", align: "right", cell: (p) => p.item_count, hideBelow: "md" },
                  ]}
                />
              </>
            );
          }}
        </QueryBoundary>
      </Panel>

      <Panel title="Estimated cost of scored opportunities" dense>
        <QueryBoundary query={run} skeletonRows={3}>
          {(d) => (
            <>
              <Grid min={170} gap="sm">
                <StatTile
                  label="Σ estimated cost"
                  value={<Money usd={d.opportunities.reduce((a, o) => a + (o.estimated_cost_usd || 0), 0)} />}
                  hint="Sum of per-opportunity ESTIMATES. Not committed, not invoiced."
                  source="GET /planner/opportunities"
                />
                <StatTile
                  label="Σ estimated effort"
                  value={`${d.opportunities.reduce((a, o) => a + (o.estimated_effort_hours || 0), 0).toFixed(1)}h`}
                  source="GET /planner/opportunities"
                />
                <StatTile label="Opportunities costed" value={d.count} source="planner.count" />
              </Grid>
              <p className="ym-hint">
                A budget gate is enforced before any planner action when the
                request declares a ceiling. A plan with a 0 ceiling declared no
                ceiling at all — that is not a zero budget.
              </p>
            </>
          )}
        </QueryBoundary>
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

type ViewId = "opportunities" | "calendar" | "plans" | "capacity" | "autonomy" | "budget";

const VIEWS: { id: ViewId; label: string }[] = [
  { id: "opportunities", label: "Opportunities" },
  { id: "calendar", label: "Editorial Calendar" },
  { id: "plans", label: "Plan details" },
  { id: "capacity", label: "Capacity" },
  { id: "autonomy", label: "Autonomy" },
  { id: "budget", label: "Budget" },
];

export function Planner() {
  const { workspaceId, workspace } = useSession();
  const [view, setView] = useState<ViewId>("opportunities");
  const [days, setDays] = useState(30);

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Planner" description="Signals → opportunities → plans → schedule." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to plan"
            description="The planner is workspace-scoped. Select a workspace to load its opportunities, capacity and autonomy."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Planner"
        description={workspace?.name ? `${workspace.name} — signals to schedule, with the evidence attached.` : "Signals to schedule, with the evidence attached."}
        actions={
          <Select
            label="Horizon"
            value={String(days)}
            onChange={(e) => setDays(Number(e.target.value) || 30)}
          >
            <option value="7">Next 7 days</option>
            <option value="14">Next 14 days</option>
            <option value="30">Next 30 days</option>
          </Select>
        }
      />

      <Tabs tabs={VIEWS} active={view} onChange={(id) => setView(id as ViewId)} />

      {view === "opportunities" ? <OpportunitiesView /> : null}
      {view === "calendar" ? <CalendarViewSummary days={days} /> : null}
      {view === "plans" ? <PlanDetailsView /> : null}
      {view === "capacity" ? <CapacityView days={days} /> : null}
      {view === "autonomy" ? <AutonomyView /> : null}
      {view === "budget" ? <BudgetView /> : null}

      <Panel title="Signals behind these opportunities" subtitle="An observation, never a demand estimate." dense>
        <SignalsView />
      </Panel>
    </>
  );
}

export default Planner;
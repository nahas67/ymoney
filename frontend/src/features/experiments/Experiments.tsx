/* Experiments — a hypothesis, its arms, and what the analysis actually found.
 *
 *   GET  /experiments                        list (workspace-scoped, newest first)
 *   GET  /experiments/{id}                   one experiment
 *   POST /experiments                        create a DRAFT
 *   POST /experiments/{id}/start             DRAFT -> RUNNING
 *   POST /experiments/{id}/analyze           RUNNING|INSUFFICIENT_DATA -> analysed
 *   POST /experiments/{id}/cancel            -> CANCELLED
 *
 * ---------------------------------------------------------------------------
 * WHY THIS IS NOT THE PERFORMANCE SCREEN
 * ---------------------------------------------------------------------------
 * `pages/Performance.tsx` put campaign rollups, retention curves, creative
 * comparisons, experiments and lessons on one screen under one heading, and
 * that conflation is the bug this feature replaces. A PERFORMANCE REPORT says
 * what happened. An EXPERIMENT is a claim someone is testing, with arms, a
 * sample gate, and a verdict the backend may refuse to issue. Collapsing the
 * two makes a correlation read as a tested conclusion.
 *
 * So this screen shows exactly what the backend computed for one experiment and
 * refuses to say more:
 *
 *   - NO RESULT IS NOT "NO EFFECT". `Experiment.result_json` defaults to `{}`.
 *     An empty result means the analysis has never run. It is rendered as
 *     UNAVAILABLE, never as "no difference found".
 *   - NO LESSON BELOW THE SAMPLE GATE. `analyze_experiment` writes
 *     INSUFFICIENT_DATA when `total_samples < minimum_sample`, and
 *     `engine/planning/feedback.py::derive_lesson` refuses to emit a lesson
 *     below `MIN_SAMPLE` with no real effect size. The same rule is honoured
 *     here: an under-sampled experiment shows NO LESSON and the count that
 *     blocked it.
 *   - NO CONFIDENCE SCORE. `Experiment.confidence` is a literal string the
 *     backend writes — `"95% CI excludes zero"` on COMPLETED, `"n/a"`
 *     otherwise. It is rendered verbatim as a label. This screen never derives
 *     a probability, a p-value or a lift that `result_json` does not contain.
 *   - NO MEAN WITHOUT SAMPLES. `_mean([])` returns 0.0, so an arm with n === 0
 *     reports a mean of 0. That is a placeholder, and it renders UNAVAILABLE.
 *   - NO CONFIDENCE INTERVAL WITHOUT n >= 30/ARM. `_welch_interval` returns
 *     `ci_low`/`ci_high` as null plus a stated reason when the assumption is
 *     unmet. The interval renders UNAVAILABLE with that reason attached.
 */

import { useState, type ReactNode } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  Field,
  Grid,
  Modal,
  PageHeader,
  Panel,
  QueryBoundary,
  Select,
  StatTile,
  Tabs,
  Textarea,
  cx,
  humanize,
  type Column,
  type Tone,
} from "../../design-system/primitives";
import { useMutation, useWsQuery } from "../../api/queries";
import { wsApi } from "../../lib/api";
import { blockedReason, can, useSession } from "../../state/session";

/* ==========================================================================
 * Response shapes — `api/v1/experiments.py::_dto`, verbatim
 * ======================================================================= */

/* `models/experiment.py::EXPERIMENT_KINDS` — the backend rejects anything else. */
export const EXPERIMENT_KINDS = [
  "HOOK",
  "TITLE",
  "THUMBNAIL",
  "VOICE",
  "CAPTION_STYLE",
  "VIDEO_DURATION",
  "CTA",
  "MUSIC",
  "BROLL_DENSITY",
  "POSTING_TIME",
] as const;

/* `engine/performance/experiments.py::METRICS` — PostMetric columns the backend
   will read for an arm. An unknown metric is a 422 at create time. */
export const EXPERIMENT_METRICS = [
  "views",
  "likes",
  "comments",
  "shares",
  "saves",
  "watch_time_seconds",
  "avg_view_duration_seconds",
  "completion_rate",
  "ctr",
  "followers_gained",
] as const;

/* `models/experiment.py::EXPERIMENT_STATUSES` */
export const EXPERIMENT_STATUSES = [
  "DRAFT",
  "RUNNING",
  "INSUFFICIENT_DATA",
  "COMPLETED",
  "INCONCLUSIVE",
  "CANCELLED",
] as const;

/* `_welch_interval()` return value, spread into each result arm. */
export type ArmStats = {
  variant_ref: string;
  descriptor: string;
  arm: string;
  n: number;
  /** 0.0 when n === 0 — `_mean([])`. Not a measurement. */
  mean: number;
  abs_diff: number;
  /** null when the control mean is 0. */
  rel_diff: number | null;
  /** null unless n >= MIN_ARM_N (30) per arm AND std_err > 0. */
  ci_low: number | null;
  ci_high: number | null;
  significant: boolean;
  /** The backend's own stated blocker. "" when the interval was computed. */
  reason: string;
};

export type ExperimentResult = {
  primary_metric?: string;
  control?: { variant_ref: string; n: number; mean: number };
  arms?: ArmStats[];
  /** null when nothing beat control. */
  winner?: string | null;
  reason?: string;
  minimum_sample?: number;
  total_samples?: number;
  analyzed_at?: string;
};

export type ExperimentVariant = { variant_ref: string; descriptor: string };

export type ExperimentRow = {
  id: string;
  workspace_id: string;
  kind: string;
  hypothesis: string;
  /** `{variant_ref}` — the control arm's platform-variant reference. */
  control: { variant_ref?: string };
  variants: ExperimentVariant[];
  platform: string;
  primary_metric: string;
  secondary_metrics: string[];
  minimum_sample: number;
  status: string;
  /** `{}` when no analysis has ever run. */
  result: ExperimentResult;
  /** Literal backend label: "" | "n/a" | "95% CI excludes zero". */
  confidence: string;
  started_at: string | null;
  ended_at: string | null;
  created_at: string;
  updated_at: string;
};

export type ExperimentList = { total: number; items: ExperimentRow[] };

/* ==========================================================================
 * Honesty helpers
 * ======================================================================= */

/** No measurement exists. Never 0, never "no effect". */
function Unavailable({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <span className="ym-muted" title={title}>
      {children ?? "UNAVAILABLE"}
    </span>
  );
}

/** Has any analysis ever run? `result_json` defaults to `{}`. */
export function hasResult(x: ExperimentRow): boolean {
  return !!x.result && Object.keys(x.result).length > 0;
}

/**
 * The sample gate, from the backend's own numbers.
 *
 * `analyze_experiment` sets INSUFFICIENT_DATA when `total_samples <
 * minimum_sample`. This is the same refusal as
 * `feedback.py::derive_lesson`'s `MIN_SAMPLE`: below it, no lesson exists.
 */
export function lessonWithheld(x: ExperimentRow): { withheld: boolean; why: string } {
  if (!hasResult(x)) {
    return { withheld: true, why: "No analysis has run, so no lesson exists. This is not evidence of no effect." };
  }
  const total = x.result.total_samples ?? 0;
  const floor = x.result.minimum_sample ?? x.minimum_sample;
  if (total < floor) {
    return {
      withheld: true,
      why: `${total} sample(s) across arms, below this experiment's minimum_sample of ${floor}. A lesson below the gate would be noise.`,
    };
  }
  if (x.status === "INCONCLUSIVE") {
    return {
      withheld: true,
      why: x.result.reason || "The analysis found no significant effect. No lesson is drawn from an inconclusive result.",
    };
  }
  if (x.status !== "COMPLETED") {
    return {
      withheld: true,
      why: `Status is ${humanize(x.status)}. The backend issues a verdict only from COMPLETED, and an unfinished run has no lesson.`,
    };
  }
  return { withheld: false, why: "" };
}

/** The backend's confidence label, rendered as a label and never as a score. */
function ConfidenceBadge({ value }: { value: string }) {
  if (!value) {
    return (
      <Badge tone="unknown" title="The backend has written no confidence label for this experiment.">
        NO LABEL
      </Badge>
    );
  }
  if (value === "n/a") {
    return (
      <Badge tone="neutral" title="The backend recorded n/a: no confidence interval was computed.">
        n/a
      </Badge>
    );
  }
  return (
    <Badge tone="info" title="A literal label the backend wrote. Not a probability computed here.">
      {value}
    </Badge>
  );
}

const STATUS_TONES: Record<string, Tone> = {
  DRAFT: "neutral",
  RUNNING: "info",
  /* Not enough data is actionable — it says "analyze again after more posts". */
  INSUFFICIENT_DATA: "warning",
  COMPLETED: "success",
  /* A real answer, just not a positive one. */
  INCONCLUSIVE: "neutral",
  CANCELLED: "neutral",
};

function ExperimentStatus({ status }: { status: string }) {
  return (
    <Badge tone={STATUS_TONES[status] ?? "neutral"} dot>
      {humanize(status)}
    </Badge>
  );
}

/* ==========================================================================
 * Actions — permission-aware, backend still authoritative
 * ======================================================================= */

/* `experiments_router` declares `require_workspace_role("member")` on create,
   start, cancel and analyze. `content.write` is the member-tier capability in
   `services/capabilities.py`, so that is the affordance checked here. */
const MEMBER_WRITE = "content.write";

type ActionName = "start" | "analyze" | "cancel";

function ExperimentActions({
  experiment,
  onDone,
}: {
  experiment: ExperimentRow;
  onDone: () => void;
}) {
  /* Hooks first, unconditionally: these read session context. */
  const allowed = can(MEMBER_WRITE);
  const blocked = blockedReason(MEMBER_WRITE);

  const act = useMutation<string, ExperimentRow>(
    (action) => wsApi.post(`/experiments/${experiment.id}/${action}`, {}) as Promise<ExperimentRow>,
    { onSuccess: onDone },
  );

  const actions: { name: ActionName; label: string; variant?: "primary" | "secondary" | "ghost" }[] = [];
  /* The state machine is the backend's; these transitions mirror it exactly. */
  if (experiment.status === "DRAFT") actions.push({ name: "start", label: "Start", variant: "primary" });
  if (experiment.status === "RUNNING") {
    actions.push({ name: "analyze", label: "Analyze", variant: "primary" });
    actions.push({ name: "cancel", label: "Cancel", variant: "ghost" });
  }
  /* Re-analysis is explicitly allowed from INSUFFICIENT_DATA once data arrives. */
  if (experiment.status === "INSUFFICIENT_DATA") {
    actions.push({ name: "analyze", label: "Analyze again", variant: "primary" });
    actions.push({ name: "cancel", label: "Cancel", variant: "ghost" });
  }

  if (actions.length === 0) {
    return (
      <span className="ym-muted">
        {humanize(experiment.status)} — no transition is offered from this state.
      </span>
    );
  }

  return (
    <span>
      {actions.map((a) => (
        <Button
          key={a.name}
          size="sm"
          variant={a.variant ?? "secondary"}
          disabled={!allowed}
          loading={act.pending}
          title={blocked ?? `POST /experiments/${experiment.id}/${a.name}`}
          onClick={() => void act.run(a.name)}
        >
          {a.label}
        </Button>
      ))}
      {blocked ? <span className="ym-hint">{blocked}</span> : null}
      {act.error ? <span className="ym-error">{act.error}</span> : null}
    </span>
  );
}

/* ==========================================================================
 * Create
 * ======================================================================= */

function CreateExperimentModal({ onClose, onDone }: { onClose: () => void; onDone: () => void }) {
  const [kind, setKind] = useState<string>("HOOK");
  const [hypothesis, setHypothesis] = useState("");
  const [controlRef, setControlRef] = useState("");
  const [variantRef, setVariantRef] = useState("");
  const [descriptor, setDescriptor] = useState("");
  const [platform, setPlatform] = useState("");
  const [primaryMetric, setPrimaryMetric] = useState("views");
  const [minimumSample, setMinimumSample] = useState("60");

  const create = useMutation<void, ExperimentRow>(
    () =>
      wsApi.post("/experiments", {
        kind,
        hypothesis: hypothesis.trim(),
        control: { variant_ref: controlRef.trim() },
        variants: variantRef.trim() ? [{ variant_ref: variantRef.trim(), descriptor: descriptor.trim() }] : [],
        platform: platform.trim(),
        primary_metric: primaryMetric,
        minimum_sample: Math.max(2, Number(minimumSample) || 60),
      }) as Promise<ExperimentRow>,
    { onSuccess: onDone },
  );

  return (
    <Modal
      open
      onClose={onClose}
      title="Create a draft experiment"
      width={640}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            variant="primary"
            loading={create.pending}
            disabled={!controlRef.trim() || !variantRef.trim()}
            onClick={() => void create.run()}
          >
            POST /experiments
          </Button>
        </>
      }
    >
      <p className="ym-hint">
        A draft records a HYPOTHESIS and its arms. Creating one measures nothing,
        proves nothing, and publishes nothing.
      </p>
      <Textarea
        label="Hypothesis"
        value={hypothesis}
        onChange={(e) => setHypothesis(e.target.value)}
        placeholder="Question-style hooks lift 3s retention against number hooks…"
      />
      <Grid min={170} gap="sm">
        <Select label="Kind" value={kind} onChange={(e) => setKind(e.target.value)}>
          {EXPERIMENT_KINDS.map((k) => (
            <option key={k} value={k}>
              {humanize(k)}
            </option>
          ))}
        </Select>
        <Select label="Primary metric" value={primaryMetric} onChange={(e) => setPrimaryMetric(e.target.value)}>
          {EXPERIMENT_METRICS.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </Select>
        <Field
          label="Control arm ref"
          value={controlRef}
          onChange={(e) => setControlRef(e.target.value)}
          hint="A platform-variant reference. Required by the backend."
        />
        <Field
          label="Variant arm ref"
          value={variantRef}
          onChange={(e) => setVariantRef(e.target.value)}
          hint="Must differ from the control ref; the backend refuses duplicates."
        />
        <Field label="Variant descriptor" value={descriptor} onChange={(e) => setDescriptor(e.target.value)} placeholder="What makes this arm different…" />
        <Field label="Platform (optional)" value={platform} onChange={(e) => setPlatform(e.target.value)} placeholder="youtube" />
        <Field
          label="Minimum sample"
          type="number"
          min={2}
          value={minimumSample}
          onChange={(e) => setMinimumSample(e.target.value)}
          hint="2 or more. Below the total, the analysis returns INSUFFICIENT_DATA and no lesson."
        />
      </Grid>
      {create.error ? <p className="ym-error">{create.error}</p> : null}
    </Modal>
  );
}

/* ==========================================================================
 * Detail
 * ======================================================================= */

function ResultPanel({ experiment }: { experiment: ExperimentRow }) {
  const result = experiment.result ?? {};
  const arms = result.arms ?? [];
  const control = result.control;
  const total = result.total_samples;
  const floor = result.minimum_sample ?? experiment.minimum_sample;

  const armRows: Array<{ label: string; ref: string; descriptor: string; stats: ArmStats | null; control: { n: number; mean: number } | null }> = [
    {
      label: "control",
      ref: control?.variant_ref ?? experiment.control?.variant_ref ?? "",
      descriptor: "The existing cut, unmodified.",
      stats: null,
      control: control ? { n: control.n, mean: control.mean } : null,
    },
    ...arms.map((a) => ({
      label: a.arm,
      ref: a.variant_ref,
      descriptor: a.descriptor,
      stats: a,
      control: null,
    })),
  ];

  const columns: Column<(typeof armRows)[number]>[] = [
    { key: "arm", header: "Arm", cell: (r) => <strong>{r.label}</strong> },
    { key: "ref", header: "Variant ref", cell: (r) => (r.ref ? <code>{r.ref}</code> : <Unavailable title="No arm reference was recorded." />) },
    { key: "desc", header: "Descriptor", cell: (r) => r.descriptor || <span className="ym-muted">—</span>, hideBelow: "md" },
    {
      key: "n",
      header: "Sample",
      align: "right",
      cell: (r) => {
        const n = r.stats ? r.stats.n : (r.control?.n ?? 0);
        return (
          <span>
            {n}
            {n < 30 ? (
              <Badge tone="warning" title="The backend needs n >= 30 per arm before it computes a confidence interval.">
                &lt;30
              </Badge>
            ) : null}
          </span>
        );
      },
    },
    {
      key: "mean",
      header: `Mean ${result.primary_metric ?? experiment.primary_metric}`,
      align: "right",
      // `_mean([])` returns 0.0. An arm with no samples has NO mean.
      cell: (r) => {
        const n = r.stats ? r.stats.n : (r.control?.n ?? 0);
        const mean = r.stats ? r.stats.mean : (r.control?.mean ?? 0);
        return n === 0 ? <Unavailable title="No metric samples in this arm — `_mean([])` returned 0.0, which is not a measurement." /> : mean;
      },
    },
    {
      key: "lift",
      header: "Relative difference",
      align: "right",
      cell: (r) => {
        if (!r.stats) return <span className="ym-muted">baseline</span>;
        if (r.stats.rel_diff === null || r.stats.rel_diff === undefined) {
          return <Unavailable title="The control arm's mean is 0, so a relative difference has no value." />;
        }
        const v = r.stats.rel_diff * 100;
        return (
          <Badge tone={v > 0 ? "success" : v < 0 ? "danger" : "neutral"}>
            {v > 0 ? "+" : ""}
            {v.toFixed(1)}%
          </Badge>
        );
      },
      hideBelow: "md",
    },
    {
      key: "ci",
      header: "95% CI",
      align: "right",
      cell: (r) => {
        if (!r.stats) return <span className="ym-muted">—</span>;
        if (r.stats.ci_low === null || r.stats.ci_high === null) {
          return <Unavailable title={r.stats.reason || "The backend computed no interval for this arm."} />;
        }
        return (
          <span>
            [{r.stats.ci_low.toFixed(2)}, {r.stats.ci_high.toFixed(2)}]
            {r.stats.significant ? (
              <Badge tone="success" title="The interval excludes zero.">
                excludes 0
              </Badge>
            ) : (
              <Badge tone="neutral" title="The interval includes zero: no significant difference.">
                includes 0
              </Badge>
            )}
          </span>
        );
      },
    },
  ];

  return (
    <Grid min={240} gap="md">
      <div>
        <DataTable
          rows={armRows}
          columns={columns}
          rowKey={(r) => `${r.label}:${r.ref}`}
          caption="Experiment arms"
          maxHeight={420}
          empty="No arms recorded"
          emptyHint="The backend requires at least one variant arm at create time."
        />
      </div>
      <div>
        <Grid min={170} gap="sm">
          <StatTile
            label="Samples across arms"
            value={total ?? <Unavailable title="No analysis has run." />}
            unavailable={total === undefined}
            tone={total !== undefined && floor !== undefined && total < floor ? "warning" : "neutral"}
            hint={floor !== undefined ? `Gate: minimum_sample = ${floor}.` : undefined}
            source="POST /experiments/{id}/analyze · result.total_samples"
          />
          <StatTile
            label="Winner"
            value={result.winner ? <code>{result.winner}</code> : <Unavailable title="The analysis named no winner." />}
            unavailable={!result.winner}
            hint="Only a COMPLETED experiment with an interval excluding zero names a winner."
            source="result.winner"
          />
          <StatTile
            label="Analysed at"
            value={result.analyzed_at ? result.analyzed_at.replace("T", " ").replace("Z", " UTC") : <Unavailable title="Never analysed." />}
            unavailable={!result.analyzed_at}
            source="result.analyzed_at"
          />
          <StatTile
            label="Confidence"
            value={<ConfidenceBadge value={experiment.confidence} />}
            hint="A literal backend label. No probability is computed on this screen."
            source="Experiment.confidence"
          />
        </Grid>
        {result.reason ? (
          <p className={cx("ym-hint")} title="The backend's own stated conclusion or blocker.">
            <strong>Backend says:</strong> {result.reason}
          </p>
        ) : null}
      </div>
    </Grid>
  );
}

function LessonPanel({ experiment }: { experiment: ExperimentRow }) {
  const { withheld, why } = lessonWithheld(experiment);
  const result = experiment.result ?? {};
  const total = result.total_samples;
  const floor = result.minimum_sample ?? experiment.minimum_sample;

  if (withheld) {
    return (
      <>
        <StatTile
          label="Lesson"
          unavailable
          tone="warning"
          hint="No lesson is emitted below the sample gate."
          source="engine/planning/feedback.py::derive_lesson"
        />
        <p className="ym-hint">{why}</p>
        <p className="ym-hint">
          A lesson needs at least {floor ?? experiment.minimum_sample} measured sample(s)
          {total !== undefined ? ` (this run had ${total})` : ""} AND a real effect size.
          Below that, one post is noise and drawing a conclusion from it is the
          failure this screen exists to prevent.
        </p>
      </>
    );
  }

  return (
    <>
      <StatTile
        label="Lesson"
        value={<Badge tone="success">ISSUED</Badge>}
        hint={`From ${total} measured sample(s) against a gate of ${floor}.`}
        source="result.reason — the backend's own sentence"
      />
      <p className="ym-hint">
        <strong>Backend says:</strong> {result.reason}
      </p>
      <p className="ym-hint">
        This is an observed difference on the content that was measured. It is not
        a forecast for future content, and it is not a guarantee.
      </p>
    </>
  );
}

function ExperimentDetail({
  experiment,
  onMutated,
}: {
  experiment: ExperimentRow;
  onMutated: () => void;
}) {
  const detail = useWsQuery<ExperimentRow>(`/experiments/${experiment.id}`);
  const reloadBoth = () => {
    detail.reload();
    onMutated();
  };
  /* Prefer the detail read; fall back to the list row while it loads. */
  const x = detail.data ?? experiment;

  return (
    <>
      <Panel
        title={x.hypothesis || "Untitled experiment"}
        subtitle={`${humanize(x.kind)}${x.platform ? ` · ${x.platform}` : ""} · ${x.primary_metric}`}
        dense
      >
        <Grid min={170} gap="sm">
          <StatTile label="Status" value={<ExperimentStatus status={x.status} />} source="Experiment.status" />
          <StatTile label="Control arm" value={<code>{x.control?.variant_ref || "—"}</code>} source="control.variant_ref" />
          <StatTile
            label="Variant arms"
            value={(x.variants ?? []).length}
            hint={(x.variants ?? []).length === 0 ? "No variant arm was declared." : undefined}
            source="variants"
          />
          <StatTile
            label="Secondary metrics"
            value={
              (x.secondary_metrics ?? []).length === 0 ? (
                <Unavailable title="None declared." />
              ) : (
                (x.secondary_metrics ?? []).join(", ")
              )
            }
            unavailable={(x.secondary_metrics ?? []).length === 0}
            source="secondary_metrics"
          />
          <StatTile label="Minimum sample" value={x.minimum_sample} hint="The backend's own gate." source="minimum_sample" />
          <StatTile
            label="Confidence"
            value={<ConfidenceBadge value={x.confidence} />}
            hint="Literal backend label."
            source="Experiment.confidence"
          />
          <StatTile
            label="Started"
            value={x.started_at ? x.started_at.replace("T", " ").replace("Z", " UTC") : <Unavailable title="Never started." />}
            unavailable={!x.started_at}
            source="started_at"
          />
          <StatTile
            label="Ended"
            value={x.ended_at ? x.ended_at.replace("T", " ").replace("Z", " UTC") : <Unavailable title="Still open." />}
            unavailable={!x.ended_at}
            source="ended_at"
          />
        </Grid>
        {detail.error ? (
          <p className="ym-error">
            The detail read failed ({detail.error}); this is the row from the list,
            which may be stale.
          </p>
        ) : null}
      </Panel>

      <Panel title="Actions" subtitle={`POST /experiments/${x.id}/{start,analyze,cancel} — the backend owns every transition`} dense>
        <ExperimentActions experiment={x} onDone={reloadBoth} />
      </Panel>

      <Panel title="Result" subtitle="What the analysis computed — nothing more" dense>
        {hasResult(x) ? (
          <ResultPanel experiment={x} />
        ) : (
          <>
            <Grid min={240} gap="sm">
              <StatTile
                label="Result"
                unavailable
                hint="result_json is empty: no analysis has ever run for this experiment."
                source="Experiment.result_json"
              />
            </Grid>
            <p className="ym-hint">
              An absent result is <strong>not</strong> a null finding. It means the
              question has not been tested yet, and it says nothing about whether
              the variant works.
            </p>
          </>
        )}
      </Panel>

      <Panel title="Lesson" subtitle="Only a completed analysis above the sample gate yields one" dense>
        <LessonPanel experiment={x} />
      </Panel>
    </>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

export function Experiments() {
  const { workspaceId, workspace } = useSession();
  const [creating, setCreating] = useState(false);
  const [statusFilter, setStatusFilter] = useState("");
  const [openId, setOpenId] = useState<string | null>(null);
  const list = useWsQuery<ExperimentList>("/experiments");

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Experiments" description="A hypothesis, its arms, and a verdict the backend may refuse to issue." />
        <Panel title="No workspace selected">
          <EmptyState title="Nothing to test" description="Experiments are workspace-scoped. Select a workspace first." />
        </Panel>
      </>
    );
  }

  const items = list.data?.items ?? null;
  const rows = items
    ? statusFilter
      ? items.filter((x) => x.status === statusFilter)
      : items
    : null;

  const summary = items
    ? {
        total: items.length,
        running: items.filter((x) => x.status === "RUNNING").length,
        insufficient: items.filter((x) => x.status === "INSUFFICIENT_DATA").length,
        completed: items.filter((x) => x.status === "COMPLETED").length,
        inconclusive: items.filter((x) => x.status === "INCONCLUSIVE").length,
      }
    : null;

  const columns: Column<ExperimentRow>[] = [
    {
      key: "hypothesis",
      header: "Hypothesis",
      cell: (x) => <strong>{x.hypothesis || "— no hypothesis recorded —"}</strong>,
    },
    { key: "kind", header: "Kind", cell: (x) => <Badge tone="neutral">{humanize(x.kind)}</Badge>, hideBelow: "md" },
    { key: "status", header: "Status", cell: (x) => <ExperimentStatus status={x.status} /> },
    {
      key: "arms",
      header: "Arms",
      align: "right",
      cell: (x) => 1 + (x.variants ?? []).length,
      hideBelow: "md",
    },
    {
      key: "sample",
      header: "Sample",
      align: "right",
      cell: (x) => {
        if (!hasResult(x)) return <Unavailable title="No analysis has run." />;
        const total = x.result.total_samples ?? 0;
        const floor = x.result.minimum_sample ?? x.minimum_sample;
        return (
          <span>
            {total}
            {total < floor ? (
              <Badge tone="warning" title="Below this experiment's minimum_sample gate.">
                &lt;{floor}
              </Badge>
            ) : null}
          </span>
        );
      },
    },
    {
      key: "confidence",
      header: "Confidence",
      cell: (x) => <ConfidenceBadge value={x.confidence} />,
      hideBelow: "lg",
    },
    {
      key: "lesson",
      header: "Lesson",
      cell: (x) =>
        lessonWithheld(x).withheld ? (
          <Badge tone="neutral" title={lessonWithheld(x).why}>
            none yet
          </Badge>
        ) : (
          <Badge tone="success" title={lessonWithheld(x).why}>
            issued
          </Badge>
        ),
    },
  ];

  return (
    <>
      <PageHeader
        title="Experiments"
        description={
          workspace?.name
            ? `${workspace.name} — a tested question, not a performance report. No analysis run yet means the result is unavailable, not "no effect".`
            : 'A tested question, not a performance report. No analysis run yet means the result is unavailable, not "no effect".'
        }
        actions={
          <Button variant="primary" onClick={() => setCreating(true)}>
            New draft
          </Button>
        }
      />

      <Panel title="At a glance" dense>
        <Grid min={160} gap="sm">
          <StatTile
            label="Experiments"
            value={summary ? summary.total : undefined}
            unavailable={summary === null}
            source="GET /experiments"
          />
          <StatTile label="Running" value={summary ? summary.running : undefined} unavailable={summary === null} tone="info" source="GET /experiments" />
          <StatTile
            label="Insufficient data"
            value={summary ? summary.insufficient : undefined}
            unavailable={summary === null}
            tone={summary && summary.insufficient > 0 ? "warning" : "neutral"}
            hint="Below the sample gate. Re-analyze once more posts are measured."
            source="GET /experiments"
          />
          <StatTile label="Completed" value={summary ? summary.completed : undefined} unavailable={summary === null} tone="success" source="GET /experiments" />
          <StatTile
            label="Inconclusive"
            value={summary ? summary.inconclusive : undefined}
            unavailable={summary === null}
            hint="A real answer with no significant effect — not a failure and not a win."
            source="GET /experiments"
          />
        </Grid>
      </Panel>

      {openId && items ? (
        <SelectedExperiment id={openId} items={items} onMutated={list.reload} />
      ) : null}

      <Panel title="Experiments" dense>
        <Tabs
          tabs={[
            { id: "", label: "All" },
            ...EXPERIMENT_STATUSES.map((s) => ({ id: s, label: humanize(s) })),
          ]}
          active={statusFilter}
          onChange={setStatusFilter}
        />
        <QueryBoundary
          query={list}
          skeletonRows={5}
          empty={statusFilter ? `No ${humanize(statusFilter).toLowerCase()} experiment` : "No experiment yet"}
          emptyHint="A draft records a hypothesis and its arms. It measures nothing until content is published and analysed."
        >
          {() => (
            <DataTable
              rows={rows ?? []}
              columns={columns}
              rowKey={(x) => x.id}
              caption="Creative experiments"
              maxHeight={560}
              empty={statusFilter ? `No ${humanize(statusFilter).toLowerCase()} experiment` : "No experiment yet"}
              emptyHint="Create a draft to state a hypothesis the backend can later test."
              onRowClick={(x) => setOpenId(x.id === openId ? null : x.id)}
            />
          )}
        </QueryBoundary>
      </Panel>

      {creating ? (
        <CreateExperimentModal
          onClose={() => setCreating(false)}
          onDone={() => {
            setCreating(false);
            list.reload();
          }}
        />
      ) : null}
    </>
  );
}

/**
 * Resolve the selected experiment from the loaded list.
 *
 * A row that vanished (deleted, or filtered out by the active status tab) is
 * reported as such rather than rendered from a fabricated stub object.
 */
function SelectedExperiment({
  id,
  items,
  onMutated,
}: {
  id: string;
  items: ExperimentRow[];
  onMutated: () => void;
}) {
  const found = items.find((x) => x.id === id);
  if (!found) {
    return (
      <Panel title="Experiment not in the loaded list">
        <EmptyState
          title="This experiment is no longer listed"
          description={`${id} is not in the current result set. It may have been removed, or the active status filter excludes it.`}
          action={<Button onClick={onMutated}>Reload the list</Button>}
        />
      </Panel>
    );
  }
  return <ExperimentDetail experiment={found} onMutated={onMutated} />;
}

export default Experiments;

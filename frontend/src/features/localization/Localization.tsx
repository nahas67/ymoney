/* Localization — translation runs, glossary, dubbing, lip-sync, voices, budget.
 *
 * Endpoints (every one workspace-scoped, so `wsApi`):
 *
 *   GET    /localization                    localization_router.list_localizations
 *   GET    /localization/{id}               localization_router.get_localization
 *   GET    /localization/{id}/qc            localization_router.get_localization_qc
 *   POST   /localization/run                localization_router.run_localization
 *   GET    /localization/glossary           localization_router.list_glossary
 *   POST   /localization/glossary           localization_router.add_glossary_term
 *   DELETE /localization/glossary/{term_id} localization_router.delete_glossary_term
 *   GET    /content?limit=100               content_router.list_content  (run sources)
 *   GET    /jobs?limit=200                  jobs_router.list_jobs         (run state)
 *   GET    /assets/dub/status               assets_router.dub_status
 *   POST   /assets/dub/dry-run              assets_router.dub_dry_run
 *   GET    /dubbing/plans                   dubbing_plans_router.list_plans_route
 *   POST   /dubbing/plans                   dubbing_plans_router.create_plan
 *   GET    /lipsync/health                  lipsync_router.health
 *   GET    /lipsync/jobs                    lipsync_router.list_jobs
 *   POST   /lipsync/jobs/{id}/cancel        lipsync_router.cancel_job
 *   GET    /media-intel/speakers            media_intel_speech_router.list_speakers
 *   GET    /media-intel/speakers/aliases    media_intel_speech_router.list_speaker_aliases
 *   GET    /costs                           costs_router.cost_summary
 *
 * FOUR RULES THIS SCREEN ENFORCES
 *
 * 1. UNAVAILABLE IS NOT ZERO. `dub_status` reports boolean capability flags; a
 *    flag the provider never sent is not `false`. Every tile here is driven off
 *    the loaded payload, and `unavailable` is set whenever the payload is absent
 *    OR the key is missing. Nothing infers a number.
 *
 * 2. A BLOCKED PROVIDER IS NEVER PRESENTED AS AVAILABLE.
 *    `lipsync/health` returns `{status, available, detail, remediation}` where
 *    `available` is true for BOTH `available` AND `degraded`. A degraded provider
 *    is therefore NOT green here: `lipSyncTone` degrades to `warning` and the
 *    remediation text is shown verbatim. The dubbing lane is worse — a missing
 *    `ffmpeg`/`llm`/`tts` key means the check was never made, so it renders as
 *    UNAVAILABLE, never as "not ready".
 *
 * 3. NO GENERIC RETRY ON A PAID OPERATION. `POST /localization/run` spends LLM
 *    translation budget and `POST /lipsync/jobs` spends GPU time; the backend
 *    keeps an unresolved translation as an UNKNOWN EXPOSURE on purpose
 *    (`providers/dubbing.py:410`). A "Retry" button on either would be a
 *    double-charge invitation, so a refusal renders the server's own detail and
 *    nothing else. Reads, which cost nothing, may be retried.
 *
 * 4. THE LOCALE LIST COMES FROM THE BACKEND. There is no language table in this
 *    file. `dub_status.languages` is `sorted(LANG_LOCALES)` — the exact map
 *    `add_glossary_term` validates a `target_languages` value against — so the
 *    picker cannot offer a locale the server would reject with 422.
 *
 * The capability gate is AFFORDANCE ONLY (Work 16.5.2 §2). `content.write` is
 * the table's minimum-role-MEMBER capability, which is what every mutating route
 * below declares via `require_workspace_role("member")`. The server remains
 * authoritative and answers 403 regardless.
 *
 * NOT WIRED, ON PURPOSE: `POST /jobs/{id}/cancel`. It is the only mutation here
 * that requires ADMIN, and `services/capabilities.py` has no capability whose
 * minimum role is admin without also meaning publishing (`publish.approve`).
 * Hiding it behind a mislabelled capability would be a lie, and the run/job state
 * below is readable at viewer level, so cancellation is left to an operator with
 * the right affordance.
 */

import { useMemo, useState, type ReactNode } from "react";
import {
  Badge,
  Button,
  DataTable,
  DestructiveButton,
  EmptyState,
  ErrorState,
  Field,
  Grid,
  Money,
  Modal,
  PageHeader,
  Panel,
  QueryBoundary,
  Select,
  Skeleton,
  StatTile,
  StatusBadge,
  Tabs,
  Toggle,
  humanize,
  toneForStatus,
  type Column,
  type Tone,
} from "../../design-system/primitives";
import { useMutation, useQuery, useWsQuery, type QueryState } from "../../api/queries";
import { ApiError, wsApi } from "../../lib/api";
import { blockedReason, can, useSession } from "../../state/session";

/** `services/capabilities.py::_MINIMUM_ROLE["content.write"] === "member"`. */
const WRITE = "content.write";

/* ==========================================================================
 * Response shapes — typed from the routers, not from the OpenAPI spec.
 *
 * Most of these routes declare no `response_model`, so the spec does not
 * describe the body. Each type below names the function that builds it.
 * ======================================================================= */

/* ---- models/localization.py QC_STATUSES ---- */
export type LocalizationQcStatus = "PASS" | "PASS_WITH_WARNINGS" | "REVIEW_REQUIRED" | "FAIL";

/* localization_router._qc_summary() — the newest report per run, trimmed. */
export type QcSummary = {
  id: string;
  status: string;
  created_at: string;
  counts: Record<string, number>;
};

/* localization_router._dto() over models/localization.py::LocalizedContent. */
export type LocalizationRun = {
  id: string;
  workspace_id: string;
  source_content_id: string;
  child_content_id: string | null;
  timeline_id: string | null;
  language: string;
  locale: string;
  translation_version: number;
  /** models/localization.py::LOCALIZATION_STATUSES */
  status: string;
  error: string;
  qc: QcSummary | null;
  stages: LocalizationStage[];
  warnings: unknown[];
  repairs: unknown[];
  /** lineage_json["costs"] — COUNTERS, not money. See CostsPanel. */
  costs: Record<string, number>;
  created_at: string;
  updated_at: string;
};

/* engine/localization/pipeline.py::_stage() appends these. */
export type LocalizationStage = { stage: string; status: string; detail: string };

export type LocalizationRunList = { total: number; items: LocalizationRun[] };

/* engine/localization/pipeline.py::_execute() writes these lineage keys. */
export type LocalizationLineage = {
  root_content_id?: string;
  child_content_id?: string;
  timeline_id?: string;
  language?: string;
  locale?: string;
  source_language?: string;
  translation_version?: number;
  qc_status?: string;
  speakers?: string[];
  /** speaker id -> target voice id (pipeline._resolve_voices). */
  voice_plan?: Record<string, string>;
  timing?: Record<string, unknown>;
  costs?: Record<string, number>;
  repairs?: unknown[];
  warnings?: unknown[];
};

/* GET /localization/{id} is _dto() plus the raw `lineage` dict. */
export type LocalizationRunDetail = LocalizationRun & { lineage: LocalizationLineage };

/* engine/localization/quality.py::_check() rows, inside LocalizationQCReport.checks_json. */
export type LocalizationQcCheck = { name: string; status: string; detail: string };

/* GET /localization/{id}/qc = fixed keys + **checks_json (the whole evaluate()). */
export type LocalizationQcReport = {
  id: string;
  localized_content_id: string;
  language: string;
  status: string;
  created_at: string;
  checks?: LocalizationQcCheck[];
  counts?: Record<string, number>;
  source_language?: string;
  translation_version?: number;
};

/* localization_router.add_glossary_term() → _glossary_dto(GlossaryTerm). */
export type GlossaryTerm = {
  id: string;
  term: string;
  replacement: string;
  target_languages: string[];
  kind: string;
  case_sensitive: boolean;
  created_at: string;
};
export type GlossaryList = { total: number; items: GlossaryTerm[] };

/* localization_router.RunBody response. */
export type RunQueuedResult = {
  queued: boolean;
  job_id: string | null;
  items: { id: string; language: string; locale: string; status: string; translation_version: number }[];
};

/* models/localization.py::GLOSSARY_KINDS — the only kinds the route accepts. */
export const GLOSSARY_KINDS = ["brand", "product", "terminology", "pronunciation"] as const;

/* content_router.list_content → _serialize_content (topic/status is all we use). */
export type ContentRow = { id: string; topic: string; status: string };
export type ContentList = { items: ContentRow[] };

/* services/jobs.py::_to_dict(job). */
export type JobRow = {
  id: string;
  type: string;
  status: string;
  priority: number;
  retry_count: number;
  last_error: string;
  payload: Record<string, unknown>;
  claimed_by: string;
  created_at: string;
};
export type JobList = { items: JobRow[] };

/* providers/dubbing.py::dub_status() via assets_router.dub_status. */
export type DubStatus = {
  ffmpeg?: boolean;
  llm?: boolean;
  tts?: boolean;
  tts_provider?: string;
  /** sorted(LANG_LOCALES) — the canonical locale vocabulary. */
  languages?: string[];
  ready?: boolean;
};

/* providers/dubbing.py::dry_run_dub() via assets_router.dub_dry_run. */
export type DubDryRunCheck = { step: string; status: "passed" | "failed" | "warn"; detail: string };
export type DubDryRun = { ok: boolean; checks: DubDryRunCheck[]; warns: DubDryRunCheck[] };

/* lipsync_router._plan_dto() over the DubbingPlan row. */
export type DubbingPlanRow = {
  id: string;
  workspace_id: string;
  source_ref: string;
  target_language: string;
  status: string;
  needs_review: boolean;
  review_count: number;
  created_at: string | null;
  plan: DubbingPlan;
};
export type DubbingPlanList = { total: number; items: DubbingPlanRow[] };

/* engine/dubbing/plan.py DubbingPlan.model_dump(mode="json"). */
export type DubbingPlan = {
  target_language?: string;
  source_ref?: string;
  speakers?: SpeakerPlan[];
  segments?: SegmentPlan[];
  status?: string;
  needs_review?: boolean;
  review_count?: number;
  notes?: string[];
};
export type SpeakerPlan = {
  speaker_id: string;
  source_voice: string;
  target_voice: string;
  language: string;
  speaking_rate: number;
  pronunciation_rules?: Record<string, string>;
};
export type SegmentPlan = {
  index: number;
  speaker_id: string;
  start: number;
  end: number;
  text?: string;
  target_text?: string;
  audio_seconds?: number | null;
  planned_rate?: number;
  needs_review?: boolean;
  review_reason?: string;
  voice_review?: boolean;
};

/* engine/lipsync/base.py::ProviderHealth.to_dict() + LocalWorkerQueue.stats(). */
export type LipSyncHealth = {
  provider?: string;
  /** HEALTH_AVAILABLE | HEALTH_DEGRADED | HEALTH_UNAVAILABLE (lower case). */
  status?: string;
  available?: boolean;
  detail?: string;
  remediation?: string;
  checks?: Record<string, unknown>;
  queue?: Record<string, unknown>;
};

/* engine/lipsync/rows.py::job_dto(LipSyncJob). */
export type LipSyncJob = {
  id: string;
  workspace_id: string;
  provider: string;
  status: string;
  /** base.py EXECUTION_OUTCOMES — the money fact `status` must not carry. */
  execution_outcome: string;
  /** base.py COST_OUTCOMES. UNKNOWN_EXPOSURE is the ambiguous case. */
  cost_outcome: string;
  progress: number;
  error: string;
  result_asset_ref: string;
  cost: Record<string, unknown>;
  video_ref: string;
  audio_ref: string;
  adapter_job_id: string;
  created_at: string | null;
  updated_at: string | null;
};
export type LipSyncJobList = { total: number; items: LipSyncJob[] };

/* media_intel_speech_router.list_speakers + engine/intel/alignment.list_speakers. */
export type SpeakerRow = {
  speaker_id: string;
  segments: number;
  start_s: number | null;
  end_s: number | null;
  total_seconds: number;
  word_count: number;
  label: string;
  named: boolean;
};
export type SpeakerList = { items: SpeakerRow[]; count: number; anonymous: boolean; note: string };

/* engine/intel/alignment._alias_dto(SpeakerAlias). */
export type SpeakerAliasRow = {
  id: string;
  asset_id: string | null;
  run_id: string | null;
  speaker_id: string;
  label: string;
  created_by: string | null;
};
export type SpeakerAliasList = { items: SpeakerAliasRow[]; count: number };

/* misc.py::costs_router.cost_summary.
 *
 * HONESTY (Work 16.5.7 §8): `spent_last_24h_usd` is nullable — null for an empty
 * window and for a window holding an UNKNOWN_EXPOSURE row. `within_budget` /
 * `remaining_usd` are the BUDGET GATE and stay non-null: an unpriceable exposure
 * consumes headroom rather than creating it.
 *
 * The published contract agrees: `openapi.json` declares this `anyOf
 * [number, null]`. */
export type CostSummary = {
  last_24h_by_category: Record<string, number>;
  spent_last_24h_usd: number | null;
  spent_last_24h_unknown_exposure_rows: number;
  daily_budget_usd: number;
  per_video_budget_usd: number;
  within_budget: boolean;
  remaining_usd: number;
};

/* ==========================================================================
 * Tones
 * ======================================================================= */

/**
 * QC verdicts get their own map, not `toneForStatus`.
 *
 * `QC_STATUSES` contains PASS / PASS_WITH_WARNINGS / REVIEW_REQUIRED / FAIL and
 * `toneForStatus` maps PASS and PASS_WITH_WARNINGS to `neutral` — a clean report
 * would read as "no information". REVIEW_REQUIRED is a human decision, not a
 * failure and not a pass, so it gets `unknown` (the design system's own
 * "ambiguity is its own category" tone) rather than a warning.
 */
export function qcTone(status: string | null | undefined): Tone {
  switch ((status ?? "").toUpperCase()) {
    case "PASS":
      return "success";
    case "PASS_WITH_WARNINGS":
      return "warning";
    case "REVIEW_REQUIRED":
      return "unknown";
    case "FAIL":
      return "danger";
    default:
      return "neutral";
  }
}

/**
 * `degraded` is `available === true` in the backend, and reporting it green is
 * exactly the "blocked provider shown as available" bug. It is a warning here.
 */
export function lipSyncTone(health: LipSyncHealth | null): Tone {
  if (health === null) return "neutral";
  if (health.status === "degraded") return "warning";
  if (health.status === "unavailable") return "danger";
  if (health.available === true) return "success";
  if (health.available === false) return "danger";
  return "neutral";
}

/* ==========================================================================
 * Small helpers
 * ======================================================================= */

function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("Z", " UTC").replace("T", " ");
}

/** A tri-state provider flag: measured true / measured false / never reported. */
function flag(value: boolean | undefined): { ok: boolean | null } {
  return { ok: typeof value === "boolean" ? value : null };
}

function FlagTile({ label, value, source }: { label: string; value: boolean | undefined; source: string }) {
  const { ok } = flag(value);
  return (
    <StatTile
      label={label}
      unavailable={ok === null}
      tone={ok === null ? "neutral" : ok ? "success" : "danger"}
      value={ok === null ? undefined : ok ? "YES" : "NO"}
      hint={ok === null ? "The provider did not report this check." : undefined}
      source={source}
    />
  );
}

/**
 * Alias for a 404 that means "no such report yet".
 *
 * The QC route raises 404 with `detail="qc report not found"` when the pipeline
 * has not written one. That is a real, settled answer — "none exists" — so it is
 * resolved to `null` data rather than raised, and the panel then renders an
 * explicit UNAVAILABLE tile. Every OTHER status still throws into
 * `QueryState.error`, so a broken read stays an alert and never becomes an
 * absence.
 */
function useQcReport(runId: string | null): QueryState<LocalizationQcReport | null> {
  return useQuery<LocalizationQcReport | null>(
    async () => {
      if (!runId) return null;
      try {
        return (await wsApi.get(`/localization/${runId}/qc`)) as LocalizationQcReport;
      } catch (err) {
        if (err instanceof ApiError && err.status === 404) return null;
        throw err;
      }
    },
    { enabled: runId !== null, deps: [runId] },
  );
}

/** The `localization.run` job that owns this run row, if one is still active. */
const ACTIVE_JOB_STATUSES = ["QUEUED", "WAITING", "RUNNING", "RETRYING"];

function activeJobFor(runId: string, jobs: JobRow[] | null): JobRow | null {
  for (const job of jobs ?? []) {
    if (job.type !== "localization.run") continue;
    if (!ACTIVE_JOB_STATUSES.includes((job.status ?? "").toUpperCase())) continue;
    const ids = job.payload?.localized_ids;
    if (Array.isArray(ids) && ids.includes(runId)) return job;
  }
  return null;
}

/* ==========================================================================
 * Read surfaces
 * ======================================================================= */

/** A list read that must not collapse a failure into "nothing here". */
function ReadPanel<T>({
  title,
  subtitle,
  query,
  skeletonRows,
  children,
}: {
  title: string;
  subtitle?: string;
  query: QueryState<T>;
  skeletonRows?: number;
  children: (data: T) => ReactNode;
}) {
  return (
    <Panel title={title} subtitle={subtitle}>
      <QueryBoundary query={query} skeletonRows={skeletonRows}>
        {children}
      </QueryBoundary>
    </Panel>
  );
}

/* ==========================================================================
 * New-run modal — PAID, so no retry affordance anywhere inside
 * ======================================================================= */

function NewRunModal({ onClose, onQueued }: { onClose: () => void; onQueued: () => void }) {
  const content = useWsQuery<ContentList>("/content?limit=100");
  /* The locale list is the server's, from `dub_status.languages`. */
  const dub = useWsQuery<DubStatus>("/assets/dub/status");
  const [sourceId, setSourceId] = useState("");
  const [languages, setLanguages] = useState<string[]>([]);
  const [locales, setLocales] = useState("");
  const [sourceLanguage, setSourceLanguage] = useState("en");

  const localesFromServer = useMemo(() => {
    const list = dub.data?.languages;
    return Array.isArray(list) ? list : null;
  }, [dub.data]);

  const queue = useMutation<void, RunQueuedResult>(
    () =>
      wsApi.post("/localization/run", {
        source_content_id: sourceId,
        target_languages: languages,
        locales: parseLocalePairs(locales),
        glossary: [],
        voice_prefs: {},
        translation_version: 1,
        source_language: sourceLanguage,
      }) as Promise<RunQueuedResult>,
    { onSuccess: onQueued },
  );

  const toggle = (code: string) =>
    setLanguages((current) =>
      current.includes(code) ? current.filter((l) => l !== code) : [...current, code],
    );

  return (
    <Modal
      open
      onClose={onClose}
      title="Queue a localization run"
      width={660}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          <Button
            variant="primary"
            loading={queue.pending}
            disabled={!sourceId || languages.length === 0 || localesFromServer === null}
            onClick={() => void queue.run()}
          >
            POST /localization/run
          </Button>
        </>
      }
    >
      <p className="ym-hint">
        Translation is a <strong>paid</strong> operation: each 20-segment batch
        reserves LLM budget, and an unresolved batch is kept as an unknown
        exposure on purpose. This form therefore reports a refusal as a refusal
        and offers no retry — pressing it again is a second billable request.
      </p>

      <Select
        label="Source content"
        value={sourceId}
        onChange={(e) => setSourceId(e.target.value)}
      >
        <option value="">Select a content item…</option>
        {(content.data?.items ?? []).map((c) => (
          <option key={c.id} value={c.id}>
            {c.topic || c.id.slice(0, 8)} — {humanize(c.status)}
          </option>
        ))}
      </Select>
      {content.error ? (
        <p className="ym-error">Could not load content items: {content.error}</p>
      ) : null}

      <div>
        <span className="ym-label">Target languages</span>
        {localesFromServer === null ? (
          dub.error ? (
            <p className="ym-error">
              The locale vocabulary is unavailable ({dub.error}). No language can
              be chosen, because the server would reject an unknown code with 422.
            </p>
          ) : (
            <Skeleton rows={2} height={28} />
          )
        ) : (
          <>
            <div>
              {localesFromServer.map((code) => (
                <Button
                  key={code}
                  size="sm"
                  variant={languages.includes(code) ? "primary" : "secondary"}
                  onClick={() => toggle(code)}
                >
                  {code}
                </Button>
              ))}
            </div>
            <p className="ym-hint">
              {languages.length} selected. Sourced from{" "}
              <code>GET /assets/dub/status</code> → <code>LANG_LOCALES</code>, the
              same table <code>add_glossary_term</code> validates against.
            </p>
          </>
        )}
      </div>

      <Field
        label="Locales (optional)"
        value={locales}
        onChange={(e) => setLocales(e.target.value)}
        hint="Comma-separated code=locale pairs, e.g. es=es-MX, pt=pt-BR. Blank leaves the locale column empty."
      />
      <Field
        label="Source language"
        value={sourceLanguage}
        maxLength={10}
        onChange={(e) => setSourceLanguage(e.target.value)}
        hint="ISO-639-1 code. The backend default is “en”."
      />

      {queue.error ? (
        /* No retry button. The paid-execution contract forbids one. */
        <div className="ym-error" role="alert">
          <strong>The run was refused.</strong> {queue.error}
          <br />
          Nothing was re-sent. Raise the workspace budget or fix the source in
          Settings, then queue a fresh run deliberately.
        </div>
      ) : null}
    </Modal>
  );
}

/** `es=es-MX, pt:pt-BR` → `{ es: "es-MX", pt: "pt-BR" }`. Malformed pairs are dropped. */
function parseLocalePairs(raw: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const chunk of raw.split(",")) {
    const [code, locale] = chunk.trim().toLowerCase().split(/[:=]/);
    if (code && locale) out[code] = locale;
  }
  return out;
}

/* ==========================================================================
 * Runs tab
 * ======================================================================= */

function RunsTab({
  runs,
  jobs,
  selected,
  onSelect,
  onNew,
}: {
  runs: QueryState<LocalizationRunList>;
  jobs: QueryState<JobList>;
  selected: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  const write = can(WRITE);
  const reason = blockedReason(WRITE);

  const columns: Column<LocalizationRun>[] = [
    {
      key: "language",
      header: "Language",
      cell: (r) => (
        <span>
          <strong>{(r.language || "?").toUpperCase()}</strong>
          {r.locale ? <span className="ym-notif-detail"> · {r.locale}</span> : null}
        </span>
      ),
    },
    { key: "status", header: "Run status", cell: (r) => <StatusBadge status={r.status} /> },
    {
      key: "qc",
      header: "QC verdict",
      cell: (r) =>
        r.qc === null ? (
          <span className="ym-muted" title="No QC report has been written for this run">
            UNAVAILABLE
          </span>
        ) : (
          <Badge tone={qcTone(r.qc.status)}>{humanize(r.qc.status)}</Badge>
        ),
    },
    {
      key: "version",
      header: "Version",
      align: "right",
      cell: (r) => (r.translation_version >= 1 ? r.translation_version : <span className="ym-muted">—</span>),
    },
    {
      key: "job",
      header: "Job",
      cell: (r) => {
        const job = activeJobFor(r.id, jobs.data?.items ?? null);
        if (!job) {
          return (
            <span className="ym-muted" title="No active localization.run job owns this row">
              none active
            </span>
          );
        }
        return (
          <span>
            <Badge tone={toneForStatus(job.status)} dot>
              {humanize(job.status)}
            </Badge>{" "}
            <span className="ym-notif-detail">{job.claimed_by ? `by ${job.claimed_by}` : "unclaimed"}</span>
          </span>
        );
      },
      hideBelow: "md",
    },
    {
      key: "stages",
      header: "Stages",
      align: "right",
      cell: (r) => (
        <span title={r.stages.map((s) => `${s.stage}: ${s.status}`).join(" → ") || "No stages recorded yet"}>
          {r.stages.length}
        </span>
      ),
      hideBelow: "lg",
    },
    {
      key: "costs",
      header: "Pipeline counters",
      align: "right",
      cell: (r) => {
        const chars = r.costs?.translation_chars;
        return typeof chars === "number" ? (
          <span title="Characters translated. This is a COUNTER, not a spend figure — money is on GET /costs.">
            {chars.toLocaleString()} chars
          </span>
        ) : (
          <span className="ym-muted" title="No translation_chars counter was recorded">
            UNAVAILABLE
          </span>
        );
      },
      hideBelow: "lg",
    },
    { key: "updated", header: "Updated", cell: (r) => when(r.updated_at || r.created_at), hideBelow: "md" },
  ];

  return (
    <>
      <Panel
        title="Localization runs"
        subtitle="One row per (source content, target language). PENDING → RUNNING → READY, with the QC verdict on the report."
        dense
        actions={
          write ? (
            <Button variant="primary" onClick={onNew}>
              New run
            </Button>
          ) : (
            <Badge tone="neutral" title={reason ?? undefined}>
              BLOCKED
            </Badge>
          )
        }
      >
        {reason ? (
          /* Rendered whenever there IS a reason — including when the control is
           * hidden. Gating on `write && reason` would suppress the explanation
           * in exactly the case that needs it. */
          <p className="ym-hint">{reason}</p>
        ) : null}
        <QueryBoundary query={runs} skeletonRows={6}>
          {(d) => (
            <DataTable
              rows={d.items ?? []}
              columns={columns}
              rowKey={(r) => r.id}
              caption="Localization runs"
              maxHeight={520}
              empty="No localization run yet"
              emptyHint="A run appears here after POST /localization/run prepares the PENDING rows for each target language."
              onRowClick={(r) => onSelect(r.id)}
            />
          )}
        </QueryBoundary>
      </Panel>

      <Panel
        title="Queue jobs"
        subtitle="Read-only. POST /jobs/{id}/cancel requires admin and no capability in services/capabilities.py means “cancel a job”, so it is not offered here."
        dense
      >
        <QueryBoundary query={jobs} skeletonRows={3}>
          {(d) => {
            const locJobs = (d.items ?? []).filter((j) => j.type === "localization.run");
            return (
              <DataTable
                rows={locJobs}
                rowKey={(j) => j.id}
                caption="Localization queue jobs"
                maxHeight={300}
                empty="No localization.run job"
                emptyHint="Nothing has been enqueued for translation in this workspace."
                columns={[
                  { key: "type", header: "Job", cell: (j) => <code>{j.id.slice(0, 8)}</code> },
                  { key: "status", header: "Status", cell: (j) => <StatusBadge status={j.status} /> },
                  {
                    key: "runs",
                    header: "Runs",
                    align: "right",
                    cell: (j) => {
                      const ids = j.payload?.localized_ids;
                      return Array.isArray(ids) ? ids.length : <span className="ym-muted">—</span>;
                    },
                  },
                  {
                    key: "owner",
                    header: "Owner",
                    cell: (j) => j.claimed_by || <span className="ym-muted">unclaimed</span>,
                    hideBelow: "md",
                  },
                  {
                    key: "retries",
                    header: "Retries",
                    align: "right",
                    cell: (j) => j.retry_count,
                    hideBelow: "lg",
                  },
                  {
                    key: "error",
                    header: "Last error",
                    cell: (j) => (j.last_error ? <span className="ym-error">{j.last_error}</span> : <span className="ym-muted">—</span>),
                    hideBelow: "md",
                  },
                  { key: "created", header: "Enqueued", cell: (j) => when(j.created_at), hideBelow: "lg" },
                ]}
              />
            );
          }}
        </QueryBoundary>
      </Panel>

      {selected ? <RunDetail runId={selected} jobs={jobs} /> : null}
    </>
  );
}

/* ==========================================================================
 * Run detail: lineage, stages, QC report, voice assignments
 * ======================================================================= */

function RunDetail({ runId, jobs }: { runId: string; jobs: QueryState<JobList> }) {
  const detail = useWsQuery<LocalizationRunDetail>(`/localization/${runId}`, { deps: [runId] });
  const qc = useQcReport(runId);
  const job = useMemo(() => activeJobFor(runId, jobs.data?.items ?? null), [runId, jobs.data]);

  return (
    <Panel title={`Run ${runId.slice(0, 8)}`} subtitle="Lineage, stages, synchronization QC and the speaker → voice assignment this run produced.">
      <QueryBoundary query={detail} skeletonRows={5}>
        {(run) => {
          const lineage = run.lineage ?? {};
          return (
            <Grid min={300}>
              <div>
                <Grid min={140} gap="sm">
                  <StatTile label="Run status" value={humanize(run.status)} tone={toneForStatus(run.status)} source="GET /localization/{id}" />
                  <StatTile
                    label="QC verdict"
                    value={run.qc === null ? undefined : humanize(run.qc.status)}
                    unavailable={run.qc === null}
                    tone={qcTone(run.qc?.status)}
                    hint={run.qc === null ? "No report row exists for this run yet." : undefined}
                    source="GET /localization/{id}"
                  />
                  <StatTile
                    label="Source segments"
                    value={run.qc?.counts?.source_segments ?? undefined}
                    unavailable={typeof run.qc?.counts?.source_segments !== "number"}
                    source="LocalizationQCReport.checks_json.counts"
                  />
                  <StatTile
                    label="Target segments"
                    value={run.qc?.counts?.target_segments ?? undefined}
                    unavailable={typeof run.qc?.counts?.target_segments !== "number"}
                    hint={
                      typeof run.qc?.counts?.source_segments === "number" &&
                      typeof run.qc?.counts?.target_segments === "number" &&
                      run.qc.counts.source_segments !== run.qc.counts.target_segments
                        ? `Does not match the source count (${run.qc.counts.source_segments}) — a desynchronization signal.`
                        : undefined
                    }
                    source="LocalizationQCReport.checks_json.counts"
                  />
                  <StatTile
                    label="Timeline"
                    value={run.timeline_id ? run.timeline_id.slice(0, 8) : undefined}
                    unavailable={!run.timeline_id}
                    tone={run.timeline_id ? "info" : "neutral"}
                    hint={run.timeline_id ? undefined : "No timeline id was recorded; nothing to open in the editor."}
                    source="LocalizedContent.timeline_id"
                  />
                  <StatTile
                    label="Derived content item"
                    value={run.child_content_id ? run.child_content_id.slice(0, 8) : undefined}
                    unavailable={!run.child_content_id}
                    source="LocalizedContent.child_content_id"
                  />
                </Grid>

                {run.error ? (
                  <div className="ym-error" role="alert">
                    <strong>The pipeline failed.</strong> {run.error}
                  </div>
                ) : null}

                {job ? (
                  <p className="ym-hint">
                    Owned by job <code>{job.id.slice(0, 8)}</code> ({humanize(job.status)})
                    {job.last_error ? ` — ${job.last_error}` : ""}.
                  </p>
                ) : null}
              </div>

              <div>
                <h3 className="ym-label">Stages</h3>
                {run.stages.length === 0 ? (
                  <EmptyState
                    title="No stages recorded"
                    description="The pipeline writes a stage row at each boundary; a run that failed before the first stage has none."
                  />
                ) : (
                  <DataTable
                    rows={run.stages}
                    rowKey={(s, i) => `${s.stage}-${i}`}
                    caption="Pipeline stages"
                    empty="No stage recorded"
                    columns={[
                      { key: "stage", header: "Stage", cell: (s) => <strong>{humanize(s.stage)}</strong> },
                      { key: "status", header: "Result", cell: (s) => <Badge tone={toneForStatus(s.status)}>{humanize(s.status)}</Badge> },
                      { key: "detail", header: "Detail", cell: (s) => s.detail || <span className="ym-muted">—</span> },
                    ]}
                  />
                )}

                <h3 className="ym-label">Speaker → voice assignment</h3>
                {lineage.speakers === undefined ? (
                  <StatTile
                    label="Voice assignments"
                    unavailable
                    hint="This run has no lineage yet, so no speaker list was written."
                    source="GET /localization/{id} → lineage"
                  />
                ) : lineage.speakers.length === 0 ? (
                  <EmptyState title="No speakers detected" description="The source carries a single unnamed narrator; the pipeline assigned no per-speaker voices." />
                ) : (
                  <DataTable
                    rows={lineage.speakers}
                    rowKey={(sp) => sp}
                    caption="Speaker to voice assignment"
                    empty="No speaker"
                    columns={[
                      { key: "speaker", header: "Speaker", cell: (sp) => <code>{sp}</code> },
                      {
                        key: "voice",
                        header: "Target voice",
                        cell: (sp) => {
                          const voice = lineage.voice_plan?.[sp];
                          return voice ? <code>{voice}</code> : <span className="ym-muted" title="Resolved at synthesis time from the TTS provider">auto-match</span>;
                        },
                      },
                    ]}
                  />
                )}

                {lineage.warnings && lineage.warnings.length > 0 ? (
                  <div className="ym-error" role="alert">
                    <strong>{lineage.warnings.length} pipeline warning(s).</strong>
                    <ul>
                      {lineage.warnings.map((w, i) => (
                        <li key={i}>{String(w)}</li>
                      ))}
                    </ul>
                  </div>
                ) : null}
              </div>
            </Grid>
          );
        }}
      </QueryBoundary>

      <h3 className="ym-label">Synchronization QC report</h3>
      {qc.loading ? (
        <Skeleton rows={3} />
      ) : qc.error ? (
        /* A real failure stays an alert. It never becomes "no report". */
        <ErrorState title="QC report could not be read" message={qc.error} onRetry={qc.reload} />
      ) : qc.data === null ? (
        <StatTile
          label="QC report"
          unavailable
          hint="GET /localization/{id}/qc answered 404: no report row exists yet. The pipeline writes one when the run finishes."
          source="GET /localization/{id}/qc"
        />
      ) : (
        <QcReport report={qc.data} />
      )}
    </Panel>
  );
}

function QcReport({ report }: { report: LocalizationQcReport }) {
  const checks = Array.isArray(report.checks) ? report.checks : [];
  const flagged = checks.filter((c) => (c.status ?? "").toLowerCase() !== "pass");
  return (
    <>
      <div>
        <Badge tone={qcTone(report.status)}>{humanize(report.status)}</Badge>
        <span className="ym-notif-detail"> · written {when(report.created_at)}</span>
      </div>
      <Grid min={140} gap="sm">
        {["source_segments", "target_segments", "glossary_terms", "warnings", "reviews", "failures"].map((k) => {
          const v = report.counts?.[k];
          return (
            <StatTile
              key={k}
              label={humanize(k)}
              value={typeof v === "number" ? v : undefined}
              unavailable={typeof v !== "number"}
              tone={k === "failures" && typeof v === "number" && v > 0 ? "danger" : "neutral"}
              source="checks_json.counts"
            />
          );
        })}
      </Grid>
      {checks.length === 0 ? (
        <EmptyState title="Report carries no check rows" description="checks_json has no `checks` array, so nothing was verified for this run." />
      ) : flagged.length === 0 ? (
        <p className="ym-hint">All {checks.length} checks passed.</p>
      ) : (
        <DataTable
          rows={flagged}
          rowKey={(c, i) => `${c.name}-${i}`}
          caption="QC checks that did not pass"
          empty="No failing check"
          columns={[
            { key: "status", header: "Result", cell: (c) => <Badge tone={qcCheckTone(c.status)}>{humanize(c.status)}</Badge> },
            { key: "name", header: "Check", cell: (c) => <code>{c.name}</code> },
            { key: "detail", header: "Detail", cell: (c) => c.detail || <span className="ym-muted">—</span> },
          ]}
        />
      )}
    </>
  );
}

/** quality.py::rollup_status uses PASS / WARN / REVIEW / FAIL. */
function qcCheckTone(status: string | null | undefined): Tone {
  switch ((status ?? "").toLowerCase()) {
    case "pass":
      return "success";
    case "warn":
      return "warning";
    case "review":
      return "unknown";
    case "fail":
      return "danger";
    default:
      return "neutral";
  }
}

/* ==========================================================================
 * Glossary tab
 * ======================================================================= */

function GlossaryTab() {
  const list = useWsQuery<GlossaryList>("/localization/glossary");
  const write = can(WRITE);
  const reason = blockedReason(WRITE);
  const [term, setTerm] = useState("");
  const [replacement, setReplacement] = useState("");
  const [kind, setKind] = useState<string>("terminology");
  const [targetLanguages, setTargetLanguages] = useState("");
  const [caseSensitive, setCaseSensitive] = useState(false);

  const add = useMutation<void, GlossaryTerm>(
    () =>
      wsApi.post("/localization/glossary", {
        term: term.trim(),
        replacement: replacement.trim(),
        target_languages: targetLanguages
          .split(",")
          .map((s) => s.trim().toLowerCase())
          .filter(Boolean),
        kind,
        case_sensitive: caseSensitive,
      }) as Promise<GlossaryTerm>,
    {
      onSuccess: () => {
        setTerm("");
        setReplacement("");
        setTargetLanguages("");
        setCaseSensitive(false);
        list.reload();
      },
    },
  );

  const remove = useMutation<string, { deleted: string }>(
    (termId) => wsApi.del(`/localization/glossary/${termId}`) as Promise<{ deleted: string }>,
    { onSuccess: () => list.reload() },
  );

  return (
    <>
      <Panel
        title="Add a glossary term"
        subtitle="Terms that must survive translation. An empty replacement keeps the source term verbatim — correct for brands and proper nouns."
      >
        {write ? (
          <>
            <Grid min={220} gap="sm">
              <Field label="Term" value={term} maxLength={200} onChange={(e) => setTerm(e.target.value)} />
              <Field label="Replacement" value={replacement} maxLength={200} onChange={(e) => setReplacement(e.target.value)} hint="Leave blank to keep the term as-is." />
              <Select label="Kind" value={kind} onChange={(e) => setKind(e.target.value)}>
                {GLOSSARY_KINDS.map((k) => (
                  <option key={k} value={k}>
                    {humanize(k)}
                  </option>
                ))}
              </Select>
              <Field
                label="Target languages"
                value={targetLanguages}
                onChange={(e) => setTargetLanguages(e.target.value)}
                hint="Comma-separated ISO-639-1 codes. Empty applies to every language."
              />
            </Grid>
            <Toggle checked={caseSensitive} onChange={setCaseSensitive} label="Case sensitive" />
            <Button
              variant="primary"
              loading={add.pending}
              disabled={term.trim().length === 0}
              onClick={() => void add.run()}
            >
              POST /localization/glossary
            </Button>
            {add.error ? (
              <p className="ym-error" role="alert">
                {add.error}
              </p>
            ) : null}
          </>
        ) : (
          <EmptyState title="Glossary editing is blocked" description={reason ?? undefined} />
        )}
      </Panel>

      <ReadPanel
        title="Workspace glossary"
        subtitle="Shared across every run in this workspace."
        query={list}
        skeletonRows={5}
      >
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            rowKey={(t) => t.id}
            caption="Glossary terms"
            empty="No glossary term yet"
            emptyHint="Add brand, product, terminology and pronunciation entries so a translation cannot mangle the words that carry the brand."
            columns={[
              { key: "kind", header: "Kind", cell: (t) => <Badge tone="neutral">{humanize(t.kind)}</Badge> },
              { key: "term", header: "Term", cell: (t) => <code>{t.term}</code> },
              {
                key: "replacement",
                header: "Replacement",
                cell: (t) =>
                  t.replacement ? <code>{t.replacement}</code> : <span className="ym-muted">kept verbatim</span>,
              },
              {
                key: "langs",
                header: "Languages",
                cell: (t) =>
                  t.target_languages.length === 0 ? (
                    <span className="ym-muted">all</span>
                  ) : (
                    <span>
                      {t.target_languages.map((l) => (
                        <Badge key={l} tone="info">
                          {l}
                        </Badge>
                      ))}
                    </span>
                  ),
                hideBelow: "md",
              },
              {
                key: "case",
                header: "Case",
                cell: (t) => (t.case_sensitive ? <Badge tone="warning">sensitive</Badge> : <span className="ym-muted">insensitive</span>),
                hideBelow: "lg",
              },
              { key: "created", header: "Added", cell: (t) => when(t.created_at), hideBelow: "md" },
              ...(write
                ? [
                    {
                      key: "actions",
                      header: "",
                      align: "right" as const,
                      cell: (t: GlossaryTerm) => (
                        <DestructiveButton
                          confirmLabel={`Delete “${t.term}”? Later runs will no longer protect it.`}
                          onConfirm={() => void remove.run(t.id)}
                          disabled={remove.pending}
                        >
                          Delete
                        </DestructiveButton>
                      ),
                    },
                  ]
                : []),
            ]}
          />
        )}
      </ReadPanel>
    </>
  );
}

/* ==========================================================================
 * Dubbing tab: provider maturity + plans
 * ======================================================================= */

function DubbingTab() {
  const status = useWsQuery<DubStatus>("/assets/dub/status");
  const plans = useWsQuery<DubbingPlanList>("/dubbing/plans");
  const write = can(WRITE);
  const reason = blockedReason(WRITE);
  const [dryRunOpen, setDryRunOpen] = useState(false);

  return (
    <>
      <Panel
        title="Dubbing pipeline maturity"
        subtitle="providers/dubbing.py::dub_status(). A missing flag means the check was never made, so it renders UNAVAILABLE rather than “not ready”."
      >
        <Grid min={150} gap="sm">
          <StatTile
            label="Pipeline ready"
            unavailable={typeof status.data?.ready !== "boolean"}
            tone={status.data?.ready === true ? "success" : status.data?.ready === false ? "danger" : "neutral"}
            value={typeof status.data?.ready === "boolean" ? (status.data.ready ? "READY" : "NOT READY") : undefined}
            hint="Requires ffmpeg, an LLM and a non-mock TTS provider together."
            source="GET /assets/dub/status"
          />
          <FlagTile label="ffmpeg" value={status.data?.ffmpeg} source="GET /assets/dub/status" />
          <FlagTile label="LLM provider" value={status.data?.llm} source="GET /assets/dub/status" />
          <FlagTile label="TTS provider" value={status.data?.tts} source="GET /assets/dub/status" />
          <StatTile
            label="TTS provider name"
            value={status.data?.tts_provider || undefined}
            unavailable={!status.data?.tts_provider}
            tone="neutral"
            source="GET /assets/dub/status"
          />
          <StatTile
            label="Supported locales"
            value={status.data?.languages?.length ?? undefined}
            unavailable={!Array.isArray(status.data?.languages)}
            hint="LANG_LOCALES — also the vocabulary the glossary validates against."
            source="GET /assets/dub/status"
          />
        </Grid>
        {status.error ? (
          <div role="alert">
            <ErrorState title="Dubbing status unavailable" message={status.error} onRetry={status.reload} />
          </div>
        ) : null}
        {status.data?.tts === false && status.data.tts_provider === "mock" ? (
          <p className="ym-error" role="alert">
            The TTS provider resolves to <strong>mock</strong>, so a dub would be a
            simulation. <code>dry_run_dub</code> fails the <code>tts</code> step for
            exactly this reason — this is not a usable dubbing backend.
          </p>
        ) : null}
        {write ? (
          <Button onClick={() => setDryRunOpen(true)}>Capability dry-run</Button>
        ) : (
          <p className="ym-hint">{reason}</p>
        )}
      </Panel>

      <ReadPanel
        title="Dubbing plans"
        subtitle="Speaker-aware plans built from cue sheets. A plan is a proposal: nothing is voiced until a render is requested."
        query={plans}
        skeletonRows={4}
      >
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            rowKey={(p) => p.id}
            caption="Dubbing plans"
            maxHeight={420}
            empty="No dubbing plan yet"
            emptyHint="A plan appears here after POST /dubbing/plans builds one from a cue sheet."
            columns={[
              { key: "id", header: "Plan", cell: (p) => <code>{p.id.slice(0, 8)}</code> },
              {
                key: "language",
                header: "Target",
                cell: (p) => p.target_language ? <Badge tone="info">{p.target_language}</Badge> : <span className="ym-muted">—</span>,
              },
              { key: "status", header: "Status", cell: (p) => <StatusBadge status={p.status} /> },
              {
                key: "speakers",
                header: "Speakers",
                align: "right",
                cell: (p) => p.plan?.speakers?.length ?? <span className="ym-muted">—</span>,
                hideBelow: "md",
              },
              {
                key: "segments",
                header: "Segments",
                align: "right",
                cell: (p) => p.plan?.segments?.length ?? <span className="ym-muted">—</span>,
                hideBelow: "md",
              },
              {
                key: "voices",
                header: "Voice assignments",
                cell: (p) => {
                  const speakers = p.plan?.speakers ?? [];
                  if (speakers.length === 0) return <span className="ym-muted">none planned</span>;
                  return (
                    <span>
                      {speakers.map((s) => (
                        <Badge key={s.speaker_id} tone={s.target_voice ? "info" : "warning"} title={s.target_voice || "No target voice — the assembler would fall back"}>
                          {s.speaker_id}
                          {s.target_voice ? ` → ${s.target_voice}` : " → unset"}
                        </Badge>
                      ))}
                    </span>
                  );
                },
              },
              {
                key: "review",
                header: "Review",
                align: "right",
                cell: (p) =>
                  p.needs_review ? (
                    <Badge tone="unknown">{p.review_count} flagged</Badge>
                  ) : (
                    <span className="ym-muted" title="needs_review is false on the stored plan">
                      0
                    </span>
                  ),
                hideBelow: "lg",
              },
              { key: "created", header: "Saved", cell: (p) => when(p.created_at), hideBelow: "md" },
            ]}
          />
        )}
      </ReadPanel>

      {dryRunOpen ? (
        <DubDryRunModal
          languages={status.data?.languages ?? null}
          onClose={() => setDryRunOpen(false)}
        />
      ) : null}
    </>
  );
}

/**
 * `POST /assets/dub/dry-run` — the OpenCreator `--dry-run` pattern.
 *
 * It is declared `viewer` and never writes, downloads, transcribes or calls a
 * provider, so it is safe to run from a screen and it is NOT treated as a paid
 * action. Always 200: failures come back as check rows, never as exceptions.
 */
function DubDryRunModal({ languages, onClose }: { languages: string[] | null; onClose: () => void }) {
  const [target, setTarget] = useState(languages?.[0] ?? "es");
  const [source, setSource] = useState("");
  const [voice, setVoice] = useState("");
  const [result, setResult] = useState<DubDryRun | null>(null);

  /* The route always answers 200 — a failed check is a row, not an exception —
   * so the result is held in state and the mutation error stays separate. */
  const run = useMutation<void, DubDryRun>(
    () =>
      wsApi.post("/assets/dub/dry-run", {
        source: source.trim(),
        target_lang: target,
        voice: voice.trim(),
        srt: "",
        bilingual: true,
        portrait: true,
      }) as Promise<DubDryRun>,
    { onSuccess: setResult },
  );

  return (
    <Modal open onClose={onClose} title="Dub capability dry-run" width={700}>
      <p className="ym-hint">
        Validates command shape and the capability matrix only. No download, no
        transcription, no LLM call, no TTS, no ffmpeg — it never spends anything.
      </p>
      <Grid min={200} gap="sm">
        <Field
          label="Source"
          value={source}
          onChange={(e) => setSource(e.target.value)}
          hint="An https URL or a path on this machine. Neither existing ⇒ the source step fails."
        />
        <Field label="Target language" value={target} onChange={(e) => setTarget(e.target.value)} />
        <Field label="Voice (optional)" value={voice} onChange={(e) => setVoice(e.target.value)} />
      </Grid>
      <Button
        variant="primary"
        loading={run.pending}
        onClick={() => {
          setResult(null);
          void run.run();
        }}
      >
        POST /assets/dub/dry-run
      </Button>
      {run.error ? (
        <p className="ym-error" role="alert">
          {run.error}
        </p>
      ) : null}
      {result ? (
        <>
          <p>
            <Badge tone={result.ok ? "success" : "danger"}>
              {result.ok ? "ALL CHECKS PASSED" : "NOT RUNNABLE"}
            </Badge>
          </p>
          <DataTable
            rows={result.checks ?? []}
            rowKey={(c) => c.step}
            caption="Dry-run checks"
            empty="No check rows returned"
            columns={[
              { key: "step", header: "Step", cell: (c) => <strong>{humanize(c.step)}</strong> },
              {
                key: "status",
                header: "Result",
                cell: (c) => (
                  <Badge tone={c.status === "passed" ? "success" : c.status === "warn" ? "warning" : "danger"}>
                    {humanize(c.status)}
                  </Badge>
                ),
              },
              { key: "detail", header: "Detail", cell: (c) => c.detail || <span className="ym-muted">—</span> },
            ]}
          />
        </>
      ) : null}
    </Modal>
  );
}

/* ==========================================================================
 * Lip-sync tab
 * ======================================================================= */

function LipSyncTab() {
  const health = useWsQuery<LipSyncHealth>("/lipsync/health");
  const jobs = useWsQuery<LipSyncJobList>("/lipsync/jobs");
  const write = can(WRITE);
  const reason = blockedReason(WRITE);
  const [video, setVideo] = useState("");
  const [audio, setAudio] = useState("");
  const [provider, setProvider] = useState("");

  /* PAID: the submit either enqueues a GPU job or is refused with 503. No retry. */
  const submit = useMutation<void, LipSyncJob>(
    () =>
      wsApi.post("/lipsync/jobs", {
        video_ref: video.trim(),
        audio_ref: audio.trim(),
        provider: provider.trim(),
        opts: {},
      }) as Promise<LipSyncJob>,
    {
      onSuccess: () => {
        setVideo("");
        setAudio("");
        jobs.reload();
      },
    },
  );
  const cancel = useMutation<string, LipSyncJob>(
    (jobId) => wsApi.post(`/lipsync/jobs/${jobId}/cancel`, {}) as Promise<LipSyncJob>,
    { onSuccess: () => jobs.reload() },
  );

  const tone = lipSyncTone(health.data);

  return (
    <>
      <Panel
        title="Lip-sync provider maturity"
        subtitle="lipsync/base.py ProviderHealth. `available` is true for degraded too, so degraded is rendered as a warning here rather than as ready."
      >
        <Grid min={150} gap="sm">
          <StatTile
            label="Provider"
            value={health.data?.provider || undefined}
            unavailable={!health.data?.provider}
            source="GET /lipsync/health"
          />
          <StatTile
            label="Availability"
            unavailable={typeof health.data?.available !== "boolean"}
            tone={tone}
            value={
              health.data?.status
                ? health.data.status.toUpperCase()
                : typeof health.data?.available === "boolean"
                  ? health.data.available
                    ? "AVAILABLE"
                    : "UNAVAILABLE"
                  : undefined
            }
            hint={
              health.data?.status === "degraded"
                ? "Degraded: the provider answers but is not fully ready. Not the same as available."
                : undefined
            }
            source="GET /lipsync/health"
          />
          <StatTile
            label="Detail"
            value={health.data?.detail || undefined}
            unavailable={!health.data?.detail}
            source="GET /lipsync/health"
          />
          <StatTile
            label="Queue depth"
            value={readNumber(health.data?.queue, "pending") ?? readNumber(health.data?.queue, "active")}
            unavailable={readNumber(health.data?.queue, "pending") === undefined && readNumber(health.data?.queue, "active") === undefined}
            tone="info"
            source="LocalWorkerQueue.stats()"
          />
          <StatTile
            label="Unresolved exposure"
            value={jobs.data ? jobs.data.items.filter((j) => j.cost_outcome === "UNKNOWN_EXPOSURE").length : undefined}
            unavailable={jobs.data === null}
            tone="unknown"
            hint="A job whose submit may already have been billed and whose amount nobody knows. Never retried from this screen."
            source="GET /lipsync/jobs → cost_outcome"
          />
        </Grid>
        {health.error ? (
          <ErrorState title="Lip-sync health unavailable" message={health.error} onRetry={health.reload} />
        ) : null}
        {health.data && health.data.available !== true && health.data.remediation ? (
          <p className="ym-error" role="alert">
            <strong>Not available.</strong> {health.data.remediation}
          </p>
        ) : null}
      </Panel>

      <Panel
        title="Submit a lip-sync job"
        subtitle="Fails closed: with no ready provider the route answers 503 with remediation and creates no row."
      >
        {write ? (
          <>
            <Grid min={200} gap="sm">
              <Field label="Video ref" value={video} maxLength={1024} onChange={(e) => setVideo(e.target.value)} />
              <Field label="Audio ref" value={audio} maxLength={1024} onChange={(e) => setAudio(e.target.value)} />
              <Field
                label="Provider (optional)"
                value={provider}
                maxLength={40}
                onChange={(e) => setProvider(e.target.value)}
                hint="Blank uses the active queue provider. Naming another one switches the queue for new jobs."
              />
            </Grid>
            <Button
              variant="primary"
              loading={submit.pending}
              disabled={video.trim().length === 0 || audio.trim().length === 0}
              onClick={() => void submit.run()}
            >
              POST /lipsync/jobs
            </Button>
            {submit.error ? (
              /* No retry. A GPU submit may already have been billed. */
              <div className="ym-error" role="alert">
                <strong>Submission refused.</strong> {submit.error}
                <br />
                No retry is offered: the backend classifies an ambiguous submit as
                an unknown exposure precisely so it is not repeated blind.
              </div>
            ) : null}
          </>
        ) : (
          <EmptyState title="Submitting is blocked" description={reason ?? undefined} />
        )}
      </Panel>

      <ReadPanel title="Lip-sync jobs" query={jobs} skeletonRows={4}>
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            rowKey={(j) => j.id}
            caption="Lip-sync jobs"
            maxHeight={460}
            empty="No lip-sync job yet"
            emptyHint="A job appears here only after a submit that the provider accepted."
            columns={[
              { key: "job", header: "Job", cell: (j) => <code>{j.id.slice(0, 8)}</code> },
              { key: "status", header: "Status", cell: (j) => <StatusBadge status={j.status} /> },
              {
                key: "execution",
                header: "Execution outcome",
                cell: (j) =>
                  j.execution_outcome ? (
                    <Badge tone={j.execution_outcome === "SUBMISSION_UNKNOWN" ? "unknown" : toneForStatus(j.execution_outcome)}>
                      {humanize(j.execution_outcome)}
                    </Badge>
                  ) : (
                    <span className="ym-muted" title="The row has no execution_outcome column value">not set</span>
                  ),
              },
              {
                key: "cost",
                header: "Cost outcome",
                cell: (j) =>
                  j.cost_outcome ? (
                    <Badge tone={j.cost_outcome === "UNKNOWN_EXPOSURE" ? "unknown" : j.cost_outcome === "ACTUAL" ? "neutral" : "warning"}>
                      {humanize(j.cost_outcome)}
                    </Badge>
                  ) : (
                    <span className="ym-muted" title="No cost_outcome was recorded">not set</span>
                  ),
              },
              { key: "provider", header: "Provider", cell: (j) => j.provider || <span className="ym-muted">—</span>, hideBelow: "md" },
              {
                key: "refs",
                header: "Video → audio",
                cell: (j) => (
                  <span className="ym-notif-detail">
                    <code>{j.video_ref || "—"}</code> → <code>{j.audio_ref || "—"}</code>
                  </span>
                ),
                hideBelow: "lg",
              },
              {
                key: "progress",
                header: "Progress",
                align: "right",
                cell: (j) => {
                  const active = ["QUEUED", "RUNNING"].includes((j.status ?? "").toUpperCase());
                  /* Progress is only meaningful while the job is active; a
                   * terminal job's progress field was never measured to 1. */
                  return active ? `${Math.round(j.progress * 100)}%` : <span className="ym-muted">—</span>;
                },
                hideBelow: "md",
              },
              {
                key: "result",
                header: "Result",
                cell: (j) => (j.result_asset_ref ? <code>{j.result_asset_ref}</code> : <span className="ym-muted">none</span>),
                hideBelow: "md",
              },
              {
                key: "error",
                header: "Error",
                cell: (j) => (j.error ? <span className="ym-error">{j.error}</span> : <span className="ym-muted">—</span>),
                hideBelow: "lg",
              },
              ...(write
                ? [
                    {
                      key: "actions",
                      header: "",
                      align: "right" as const,
                      cell: (j: LipSyncJob) => {
                        const active = ["QUEUED", "RUNNING"].includes((j.status ?? "").toUpperCase());
                        if (!active) return <span className="ym-muted">terminal</span>;
                        return (
                          <Button size="sm" loading={cancel.pending} onClick={() => void cancel.run(j.id)}>
                            Cancel
                          </Button>
                        );
                      },
                    },
                  ]
                : []),
            ]}
          />
        )}
      </ReadPanel>
    </>
  );
}

function readNumber(source: Record<string, unknown> | undefined, key: string): number | undefined {
  const raw = source?.[key];
  return typeof raw === "number" ? raw : undefined;
}

/* ==========================================================================
 * Voice tab
 * ======================================================================= */

function VoiceTab() {
  const speakers = useWsQuery<SpeakerList>("/media-intel/speakers");
  const aliases = useWsQuery<SpeakerAliasList>("/media-intel/speakers/aliases");

  return (
    <>
      <Panel
        title="Anonymous speakers"
        subtitle="media-intel/speakers. There is no gender, age, race or identity field in the contract and none is inferred here."
      >
        <QueryBoundary query={speakers} skeletonRows={4}>
          {(d) => (
            <>
              <p className="ym-hint">{d.note}</p>
              <DataTable
                rows={d.items ?? []}
                rowKey={(s) => s.speaker_id}
                caption="Anonymous speakers"
                maxHeight={360}
                empty="No speaker detected"
                emptyHint="Speakers appear after a diarization run over an asset in this workspace."
                columns={[
                  { key: "speaker", header: "Speaker id", cell: (s) => <code>{s.speaker_id}</code> },
                  {
                    key: "label",
                    header: "Operator label",
                    cell: (s) =>
                      s.label ? (
                        <span>
                          {s.label} <Badge tone={s.named ? "info" : "neutral"}>{s.named ? "named" : "auto"}</Badge>
                        </span>
                      ) : (
                        <span className="ym-muted">unnamed</span>
                      ),
                  },
                  { key: "segments", header: "Segments", align: "right", cell: (s) => s.segments },
                  { key: "words", header: "Words", align: "right", cell: (s) => s.word_count, hideBelow: "md" },
                  {
                    key: "window",
                    header: "Window",
                    cell: (s) =>
                      s.start_s === null || s.end_s === null ? (
                        <span className="ym-muted">not measured</span>
                      ) : (
                        <span className="ym-notif-detail">
                          {s.start_s.toFixed(1)}s → {s.end_s.toFixed(1)}s
                        </span>
                      ),
                    hideBelow: "lg",
                  },
                  {
                    key: "seconds",
                    header: "Speaking seconds",
                    align: "right",
                    cell: (s) => s.total_seconds.toFixed(1),
                    hideBelow: "md",
                  },
                ]}
              />
            </>
          )}
        </QueryBoundary>
      </Panel>

      <ReadPanel
        title="Operator speaker labels"
        subtitle="The only place a speaker acquires a name. An alias is an operator's word, not a detected attribute."
        query={aliases}
        skeletonRows={3}
      >
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            rowKey={(a) => a.id}
            caption="Speaker aliases"
            empty="No speaker label yet"
            emptyHint="A label appears here once an operator names an anonymous speaker."
            columns={[
              { key: "speaker", header: "Speaker id", cell: (a) => <code>{a.speaker_id}</code> },
              { key: "label", header: "Label", cell: (a) => a.label || <span className="ym-muted">—</span> },
              {
                key: "scope",
                header: "Scope",
                cell: (a) =>
                  a.run_id ? (
                    <Badge tone="info">run {a.run_id.slice(0, 8)}</Badge>
                  ) : a.asset_id ? (
                    <Badge tone="info">asset {a.asset_id.slice(0, 8)}</Badge>
                  ) : (
                    <Badge tone="neutral">workspace</Badge>
                  ),
                hideBelow: "md",
              },
              { key: "by", header: "Set by", cell: (a) => a.created_by || <span className="ym-muted">—</span>, hideBelow: "lg" },
            ]}
          />
        )}
      </ReadPanel>
    </>
  );
}

/* ==========================================================================
 * Budget
 * ======================================================================= */

function CostSummaryPanel() {
  const costs = useWsQuery<CostSummary>("/costs");
  return (
    <Panel
      title="Cost and budget"
      subtitle="misc.py::costs_router.cost_summary. This ledger — not a pipeline counter — is where money is reported."
    >
      <Grid min={150} gap="sm">
        <StatTile
          label="Spent, last 24h"
          /* HONESTY (§8): a null total renders UNAVAILABLE. The gate tiles below
           * keep their verdict; this one is a measurement. */
          unavailable={costs.data === null || costs.data.spent_last_24h_usd === null}
          tone="neutral"
          value={costs.data ? <Money usd={costs.data.spent_last_24h_usd} /> : undefined}
          hint={
            costs.data && costs.data.spent_last_24h_usd === null
              ? `${costs.data.spent_last_24h_unknown_exposure_rows} cost row(s) record an exposure nobody can price.`
              : undefined
          }
          source="GET /costs"
        />
        <StatTile
          label="Remaining vs daily cap"
          unavailable={costs.data === null}
          tone={costs.data === null ? "neutral" : costs.data.remaining_usd > 0 ? "success" : "danger"}
          value={costs.data ? <Money usd={costs.data.remaining_usd} /> : undefined}
          source="GET /costs"
        />
        <StatTile
          label="Daily budget"
          unavailable={costs.data === null}
          value={costs.data ? <Money usd={costs.data.daily_budget_usd} /> : undefined}
          source="GET /costs"
        />
        <StatTile
          label="Per-video budget"
          unavailable={costs.data === null}
          value={costs.data ? <Money usd={costs.data.per_video_budget_usd} /> : undefined}
          source="GET /costs"
        />
        <StatTile
          label="Within budget"
          unavailable={typeof costs.data?.within_budget !== "boolean"}
          tone={costs.data?.within_budget === true ? "success" : costs.data?.within_budget === false ? "danger" : "neutral"}
          value={typeof costs.data?.within_budget === "boolean" ? (costs.data.within_budget ? "YES" : "NO") : undefined}
          hint="A refused paid operation names this, not the provider."
          source="GET /costs"
        />
      </Grid>
      {costs.error ? (
        <ErrorState title="Cost summary unavailable" message={costs.error} onRetry={costs.reload} />
      ) : null}
      {costs.data && Object.keys(costs.data.last_24h_by_category).length > 0 ? (
        <DataTable
          rows={Object.entries(costs.data.last_24h_by_category).map(([k, v]) => ({ k, v }))}
          rowKey={(r) => r.k}
          caption="Spend by category, last 24 hours"
          empty="No category"
          columns={[
            { key: "category", header: "Category", cell: (r) => humanize(r.k) },
            { key: "amount", header: "Amount", align: "right", cell: (r) => <Money usd={r.v} /> },
          ]}
        />
      ) : costs.data ? (
        <p className="ym-hint">No spend recorded in the last 24 hours for this workspace.</p>
      ) : null}
    </Panel>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

const TABS = [
  { id: "runs", label: "Runs" },
  { id: "glossary", label: "Glossary" },
  { id: "dubbing", label: "Dubbing" },
  { id: "lipsync", label: "Lip-sync" },
  { id: "voice", label: "Voice" },
] as const;

export function Localization() {
  const { workspaceId, workspace } = useSession();
  const [tab, setTab] = useState<string>("runs");
  const [selected, setSelected] = useState<string | null>(null);
  const [newRun, setNewRun] = useState(false);

  const runs = useWsQuery<LocalizationRunList>("/localization");
  const jobs = useWsQuery<JobList>("/jobs?limit=200");

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="Localization" description="Translation, glossary, dubbing, lip-sync and voice assignment." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to show"
            description="Every route here is workspace-scoped. Select a workspace to load its localization state."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Localization"
        description={
          workspace?.name
            ? `${workspace.name} — translate, dub, lip-sync, with the QC verdict behind every run.`
            : "Translate, dub, lip-sync, with the QC verdict behind every run."
        }
        actions={
          <>
            <Button onClick={() => { runs.reload(); jobs.reload(); }}>Refresh</Button>
            {can(WRITE) ? (
              <Button variant="primary" onClick={() => setNewRun(true)}>
                New run
              </Button>
            ) : null}
          </>
        }
      />

      <CostSummaryPanel />

      <Tabs tabs={TABS.map((t) => ({ id: t.id, label: t.label }))} active={tab} onChange={setTab} />

      {tab === "runs" ? (
        <RunsTab runs={runs} jobs={jobs} selected={selected} onSelect={setSelected} onNew={() => setNewRun(true)} />
      ) : null}
      {tab === "glossary" ? <GlossaryTab /> : null}
      {tab === "dubbing" ? <DubbingTab /> : null}
      {tab === "lipsync" ? <LipSyncTab /> : null}
      {tab === "voice" ? <VoiceTab /> : null}

      {newRun ? (
        <NewRunModal
          onClose={() => setNewRun(false)}
          onQueued={() => {
            setNewRun(false);
            runs.reload();
            jobs.reload();
          }}
        />
      ) : null}
    </>
  );
}

export default Localization;

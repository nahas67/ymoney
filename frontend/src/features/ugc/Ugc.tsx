/* UGC Studio — format → script → talent → assets → voice → music → timeline → QC → render.
 *
 * Endpoints (every one workspace-scoped, so `wsApi`):
 *
 *   GET  /ugc/presets                ugc_router.list_presets
 *   GET  /ugc/projects               ugc_router.list_projects
 *   GET  /ugc/projects/{id}          ugc_router.get_project
 *   POST /ugc/projects               ugc_router.create_project          (paid)
 *   POST /ugc/projects/{id}/render   ugc_router.render_project          (paid, QC-gated)
 *   GET  /avatars/health             avatars_router.get_avatar_health
 *   GET  /avatars                    avatars_router.list_avatars
 *   POST /avatars                    avatars_router.create_avatar
 *   POST /avatars/{id}/authorize     avatars_router.authorize_avatar
 *   POST /avatars/render             avatars_router.render_avatar_route  (paid, consent-gated)
 *
 * THE PRESET LIST IS NOT IN THIS FILE.
 *
 * There is no hardcoded preset table here. `engine/ugc/pipeline.py` owns
 * `UGC_PRESETS` and `PRESET_DEFAULTS`, `ugc_router.list_presets` returns them
 * verbatim (`{key, hook_type, duration_seconds, format}`), and
 * `create_project` answers 422 listing the registered presets for an unknown key.
 * A local copy would be a second business rule that drifts the moment a preset is
 * added — so the picker is built from the response, and an empty response is an
 * empty picker with the reason on screen, never a fabricated fallback.
 *
 * THE STAGES ARE THE BACKEND'S STAGES.
 *
 * `UGCVideoPipeline.run()` runs exactly: brief → audience → hook → script →
 * presenter → voice → product_assets → broll → cta → music → timeline → qc. Each
 * stage writes its own `lineage_json` key, so the workflow strip below reads
 * those keys and marks a stage UNAVAILABLE when its key was never written — which
 * is the honest answer for a DRAFT, for a run that died before that stage, and
 * for a backend that changed. Nothing is inferred from the project's status.
 *
 * THE TALENT STAGE HAS NO LINEAGE KEY, SO IT IS NOT FAKED.
 *
 * `stage_presenter()` resolves the presenter into `self.presenter` and saves only
 * `self.lineage`, so the resolved avatar never lands in `lineage_json`. What IS
 * durable is the brief's request (`brief.presenter.avatar_profile_id` or
 * `brief.avatar_profile_id`) and `GET /avatars`. So the talent step joins those
 * two: the requested profile id against the canonical profile list, and the
 * profile's real `consent_state`. A requested id with no matching profile reads
 * UNAVAILABLE, never "no talent".
 *
 * NO GENERIC RETRY ON PAID WORK.
 *
 * `POST /ugc/projects` runs the whole pipeline (LLM script, TTS narration, music
 * generation, timeline) and `POST /ugc/projects/{id}/render` spends render time;
 * `POST /avatars/render` refuses with 403 before any provider work when consent is
 * not authorized. A "Retry" on any of those turns a deliberate refusal into a
 * second charge, so every paid failure here renders the server's own detail and
 * no retry control. Reads may be retried — they cost nothing.
 *
 * Consent is a gate, not a warning: `require_authorized` raises inside
 * `stage_presenter` BEFORE voice and avatar work, so a pending profile fails the
 * run at the talent step rather than producing a render nobody may publish.
 */

import { useMemo, useState, type ReactNode } from "react";
import {
  Badge,
  Button,
  DataTable,
  EmptyState,
  ErrorState,
  Field,
  Grid,
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
import { useMutation, useWsQuery, type QueryState } from "../../api/queries";
import { wsApi } from "../../lib/api";
import { blockedReason, can, useSession } from "../../state/session";

/** `services/capabilities.py::_MINIMUM_ROLE["content.write"] === "member"`. */
const WRITE = "content.write";

/* ==========================================================================
 * Response shapes — typed from the routers.
 *
 * Neither UGC route declares a `response_model`, so the OpenAPI spec does not
 * describe these bodies. Each type names the function that builds it.
 * ======================================================================= */

/* ugc_router.list_presets: `{"key": key, **PRESET_DEFAULTS[key]}`. */
export type UgcPreset = {
  key: string;
  hook_type?: string;
  duration_seconds?: number;
  format?: string;
};
export type UgcPresetList = { total: number; items: UgcPreset[]; workspace_id: string };

/* ugc_router._project_dto(UgcProjectRow). */
export type UgcProject = {
  id: string;
  workspace_id: string;
  preset: string;
  /** DRAFT | RUNNING | READY | REVIEW_REQUIRED | BLOCKED | FAILED | RENDERED. */
  status: string;
  brief: Record<string, unknown>;
  timeline_id: string;
  render_asset_ref: string;
  qc: UgcQc;
  lineage: UgcLineage;
  created_at: string;
  updated_at: string;
};
export type UgcProjectList = { total: number; items: UgcProject[] };

/* GET /ugc/projects/{id} adds the asset resolution and the editor affordance. */
export type UgcProjectDetail = UgcProject & {
  product_assets: { resolved: ResolvedAsset[]; unresolved: UnresolvedAsset[] };
  open_in_editor: boolean;
};

/* engine/ugc/assets.py::resolve_product_assets */
export type ResolvedAsset = {
  asset_id: string;
  ref: string;
  storage_key: string;
  type: string;
  role: string;
};
export type UnresolvedAsset = { entry: unknown; reason: string };

/* engine/ugc/qc.py::UGCQCReport.to_dict() — checks is a NAME -> {status, detail}. */
export type UgcQc = {
  status: string;
  checks: Record<string, { status: string; detail: string }>;
  preset?: string;
  report_type?: string;
  /** Only on a refused regenerate(); see UGCVideoPipeline.regenerate. */
  regeneration?: {
    status: string;
    refused: boolean;
    reason: string;
    expected_manifest_hash: string;
    current_manifest_hash: string;
    expected_version: number;
    current_version: number;
    timeline_id: string;
  };
};

/* engine/ugc/qc.py::QC_STATUSES */
export function ugcQcTone(status: string | null | undefined): Tone {
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

/** The lineage keys `UGCVideoPipeline` writes, one per stage. */
export type UgcLineage = {
  /* brief / audience */
  preset?: string;
  topic?: string;
  audience?: string;
  strategy?: Record<string, unknown>;
  /* hook / script */
  hook?: string;
  script?: string;
  script_source?: string;
  brand_voice?: string;
  /* voice */
  voice?: { asset_id: string | null; duration: number | null; words: number | null }[];
  /* music */
  music?: { track?: string; generated?: boolean; reason?: string; provider?: string };
  music_brand_error?: string;
  /* product assets */
  product_assets?: { resolved?: ResolvedAsset[]; unresolved?: UnresolvedAsset[] };
  /* b-roll */
  broll_plan?: { index: number | string; query?: string }[];
  broll_error?: string;
  broll_fetched?: { refs?: unknown[]; errors?: Record<string, string> };
  /* cta */
  cta?: { text: string; spoken: boolean };
  /* presenter */
  presenter_error?: string;
  /* timeline */
  timeline_id?: string;
  content_item_id?: string;
  generated_version?: number;
  manifest_hash?: string;
  /* failure */
  error?: string;
};

/* ugc_router.create_project — the `run: false` draft form. */
export type CreateDraftResult = { project: UgcProject; ran: false };
/* ...and the `run: true` form, which additionally reports what the run produced. */
export type CreateRunResult = {
  project: UgcProject;
  ran: true;
  timeline_id: string | null;
  qc: UgcQc;
  script: string;
};
export type CreateProjectResult = CreateDraftResult | CreateRunResult;

/* UGCVideoPipeline.render() */
export type UgcRenderResult = {
  project: UgcProject;
  render: {
    asset_id: string;
    storage_key: string;
    duration_seconds: number | null;
    timeline_id: string;
    qc: UgcQc;
  };
};

/* engine/avatar/service.py::profile_dto(AvatarProfileRow). */
export type AvatarProfile = {
  id: string;
  workspace_id: string;
  name: string;
  /** AvatarProfile.to_json() — the row's profile_json, not a column set. */
  profile: {
    name: string;
    source_asset_ref: string;
    voice_ref: string;
    expression_preset: string;
    motion_preset: string;
    framing: string;
    background: string;
    language: string;
    brand_association: string;
    provider: string;
    status: string;
  };
  source_asset_ref: string;
  /** CONSENT_STATES: authorized | pending | revoked. */
  consent_state: string;
  consent: {
    state: string;
    source: string;
    granted_by: string;
    granted_at: string;
    authorization_evidence: Record<string, unknown>;
    statement: string;
  };
  provider: string;
  status: string;
  created_at: string;
};
export type AvatarProfileList = { total: number; items: AvatarProfile[] };

/* engine/avatar/profile.py::avatar_health() + avatars_router workspace_id. */
export type AvatarHealth = {
  provider: string;
  ready: boolean;
  detail: string;
  consent_required: boolean;
  consent_states: string[];
  health: Record<string, unknown>;
  capabilities: Record<string, unknown>;
  workspace_id: string;
};

/* engine/avatar/service.py::render_profile_output() */
export type AvatarRenderResult = {
  output_id: string;
  asset_id: string;
  storage_key: string;
  duration: number;
  backend: string;
  provider: string;
  is_mock: boolean;
  consent_state: string;
  timeline_id: string;
  content_item_id: string;
  qc: { status: string; checks: Record<string, unknown>; flags?: string[] };
};

/* ==========================================================================
 * Stage vocabulary — the pipeline's own order, not a UI invention
 * ======================================================================= */

type StageId =
  | "brief"
  | "audience"
  | "hook"
  | "script"
  | "presenter"
  | "voice"
  | "product_assets"
  | "broll"
  | "cta"
  | "music"
  | "timeline"
  | "qc";

type StageState = "done" | "failed" | "warn" | "absent";

type Stage = { id: StageId; label: string; state: StageState; detail: string };

const STAGE_ORDER: { id: StageId; label: string }[] = [
  { id: "brief", label: "Format" },
  { id: "audience", label: "Audience" },
  { id: "hook", label: "Hook" },
  { id: "script", label: "Script" },
  { id: "presenter", label: "Talent" },
  { id: "voice", label: "Voice" },
  { id: "product_assets", label: "Assets" },
  { id: "broll", label: "B-roll" },
  { id: "cta", label: "CTA" },
  { id: "music", label: "Music" },
  { id: "timeline", label: "Timeline" },
  { id: "qc", label: "QC" },
];

/**
 * Read each stage from the lineage key that stage wrote.
 *
 * `absent` is a real, visible outcome and not an error: a DRAFT has no lineage
 * at all, and a run that raised at `voice` has no `cta` key. Reporting those as
 * "not reached" is the only truthful option — the project status alone cannot say
 * which stage stopped it.
 *
 * `music` is special because `{}` is a DELIBERATE answer: `stage_music` records
 * `{"generated": false, "reason": …}` when the policy declines, and a missing bed
 * must never read as a missing stage.
 */
export function readStages(
  lineage: UgcLineage,
  qc: UgcQc,
  presenter: { requested: string; resolved: boolean; consent: string | null },
): Stage[] {
  const has = (key: keyof UgcLineage): boolean => lineage[key] !== undefined && lineage[key] !== null;
  const failed = typeof lineage.error === "string" && lineage.error.length > 0;

  return STAGE_ORDER.map(({ id, label }) => {
    let state: StageState;
    let detail: string;

    switch (id) {
      case "brief":
        state = has("topic") ? "done" : "absent";
        detail = typeof lineage.topic === "string" ? lineage.topic : "No topic was recorded.";
        break;
      case "audience":
        state = has("audience") ? "done" : "absent";
        detail = typeof lineage.audience === "string" ? lineage.audience : "";
        break;
      case "hook":
        state = has("hook") ? "done" : "absent";
        detail = typeof lineage.hook === "string" ? lineage.hook : "";
        break;
      case "script":
        state = has("script") ? "done" : "absent";
        detail =
          lineage.script_source === "fallback"
            ? "Deterministic claim-free fallback — no script agent was reachable."
            : typeof lineage.script_source === "string"
              ? `Source: ${lineage.script_source}.`
              : "";
        break;
      case "presenter":
        if (presenter.requested === "") {
          state = "done";
          detail = "No presenter requested; product assets carry the visuals.";
        } else if (!presenter.resolved) {
          state = "warn";
          detail = `Requested profile ${presenter.requested} is not in this workspace's profile list.`;
        } else if (presenter.consent !== "authorized") {
          state = "failed";
          detail = `Consent is “${presenter.consent ?? "unknown"}”; require_authorized refuses the run before any voice or avatar work.`;
        } else {
          state = "done";
          detail = `Authorized profile ${presenter.requested}.`;
        }
        break;
      case "voice":
        state = has("voice") && (lineage.voice ?? []).length > 0 ? "done" : "absent";
        detail = `${(lineage.voice ?? []).length} narrated segment(s)${lineage.brand_voice ? ` via the brand-approved voice ${lineage.brand_voice}` : ""}.`;
        break;
      case "product_assets": {
        const resolved = lineage.product_assets?.resolved ?? [];
        const unresolved = lineage.product_assets?.unresolved ?? [];
        if (!has("product_assets")) {
          state = "absent";
          detail = "";
        } else if (unresolved.length > 0) {
          state = "failed";
          detail = `${unresolved.length} reference(s) did not resolve to a workspace MediaAsset.`;
        } else {
          state = "done";
          detail = `${resolved.length} reference(s) resolved.`;
        }
        break;
      }
      case "broll":
        if (typeof lineage.broll_error === "string" && lineage.broll_error) {
          state = "warn";
          detail = `Planning failed: ${lineage.broll_error}`;
        } else {
          state = has("broll_plan") ? "done" : "absent";
          detail = `${(lineage.broll_plan ?? []).length} planned shot(s).`;
        }
        break;
      case "cta":
        state = has("cta") ? ((lineage.cta?.spoken ?? false) ? "done" : "warn") : "absent";
        detail = lineage.cta
          ? `${lineage.cta.text || "(no CTA text)"}${lineage.cta.spoken ? " — narrated" : " — written but not narrated"}`
          : "";
        break;
      case "music":
        if (!has("music")) {
          state = "absent";
          detail = "";
        } else if (lineage.music?.generated === true) {
          state = "done";
          detail = `Generated via ${lineage.music.provider ?? "the configured provider"}.`;
        } else {
          /* generated:false is a recorded policy decision, not a missing stage. */
          state = "warn";
          detail = lineage.music?.reason ? `No bed — ${lineage.music.reason}` : "No bed was generated.";
        }
        break;
      case "timeline":
        state = has("timeline_id") ? "done" : "absent";
        detail =
          lineage.manifest_hash && typeof lineage.generated_version === "number"
            ? `Version ${lineage.generated_version}, manifest ${String(lineage.manifest_hash).slice(0, 12)}.`
            : "";
        break;
      case "qc":
        state = qc.status ? (ugcQcTone(qc.status) === "danger" ? "failed" : ugcQcTone(qc.status) === "unknown" ? "warn" : "done") : "absent";
        detail = qc.status ? `Verdict ${qc.status} across ${Object.keys(qc.checks ?? {}).length} check(s).` : "";
        break;
    }

    /* A run that raised leaves `lineage.error`; nothing after the failing stage
     * was written, so those keys are genuinely absent rather than empty. */
    if (failed && state === "absent" && id !== "qc") {
      detail = `Not reached — the run stopped with: ${lineage.error}`;
    }
    return { id, label, state, detail };
  });
}

function stageTone(state: StageState): Tone {
  switch (state) {
    case "done":
      return "success";
    case "warn":
      return "warning";
    case "failed":
      return "danger";
    case "absent":
      return "neutral";
  }
}

function StageStrip({ stages }: { stages: Stage[] }) {
  /* Inline styles only: no new CSS class is introduced, so nothing has to be
   * added to the shared stylesheet for this strip to render. */
  return (
    <ol
      style={{
        listStyle: "none",
        margin: "0 0 12px",
        padding: 0,
        display: "grid",
        gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))",
        gap: 8,
      }}
    >
      {stages.map((s) => (
        <li
          key={s.id}
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 4,
            padding: 8,
            border: "1px solid var(--seam)",
            borderRadius: 8,
            background: "var(--bg-inset)",
          }}
        >
          <span>
            <Badge tone={stageTone(s.state)} dot>
              {s.label}
            </Badge>{" "}
            <span className="ym-notif-detail">{s.state}</span>
          </span>
          <span className="ym-hint">{s.state === "absent" ? "not recorded" : s.detail || "—"}</span>
        </li>
      ))}
    </ol>
  );
}

/* ==========================================================================
 * Helpers
 * ======================================================================= */

function when(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("Z", " UTC").replace("T", " ");
}

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

/** The avatar profile a brief asks for, resolved against the canonical list. */
function presenterFrom(brief: Record<string, unknown>, profiles: AvatarProfile[] | null) {
  const raw = brief?.presenter;
  const nested = raw && typeof raw === "object" ? (raw as Record<string, unknown>).avatar_profile_id : undefined;
  const id = typeof nested === "string" ? nested : typeof brief?.avatar_profile_id === "string" ? brief.avatar_profile_id : "";
  if (id === "") return { requested: "", resolved: false, consent: null as string | null };
  const match = (profiles ?? []).find((p) => p.id === id) ?? null;
  return { requested: id, resolved: match !== null, consent: match ? match.consent_state : null };
}

/* ==========================================================================
 * Create project modal — PAID when `run` is checked
 * ======================================================================= */

function CreateProjectModal({
  presets,
  presetsError,
  profiles,
  onClose,
  onCreated,
}: {
  presets: UgcPresetList | null;
  presetsError: string | null;
  profiles: AvatarProfile[] | null;
  onClose: () => void;
  onCreated: (id: string) => void;
}) {
  const [preset, setPreset] = useState("");
  const [topic, setTopic] = useState("");
  const [audience, setAudience] = useState("");
  const [tone, setTone] = useState("");
  const [hook, setHook] = useState("");
  const [features, setFeatures] = useState("");
  const [assets, setAssets] = useState("");
  const [variants, setVariants] = useState("");
  const [presenterId, setPresenterId] = useState("");
  const [run, setRun] = useState(true);

  const items = presets?.items ?? [];
  const chosen = items.find((p) => p.key === preset) ?? null;
  const chosenProfile = (profiles ?? []).find((p) => p.id === presenterId) ?? null;

  const create = useMutation<void, CreateProjectResult>(
    () =>
      wsApi.post("/ugc/projects", {
        preset,
        brief: {
          topic: topic.trim(),
          audience: audience.trim(),
          tone: tone.trim(),
          hook: hook.trim(),
          features: features.split(",").map((s) => s.trim()).filter(Boolean),
          product_assets: assets.split(",").map((s) => s.trim()).filter(Boolean),
          variants: variants.split(",").map((s) => s.trim()).filter(Boolean),
          ...(presenterId ? { presenter: { avatar_profile_id: presenterId } } : {}),
        },
        run,
      }) as Promise<CreateProjectResult>,
    {
      onSuccess: (result) => {
        if (result.project?.id) onCreated(result.project.id);
        else onClose();
      },
    },
  );

  return (
    <Modal
      open
      onClose={onClose}
      title="New UGC project"
      width={720}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          <Button
            variant="primary"
            loading={create.pending}
            disabled={preset === "" || topic.trim() === "" || items.length === 0}
            onClick={() => void create.run()}
          >
            POST /ugc/projects
          </Button>
        </>
      }
    >
      {items.length === 0 ? (
        presetsError ? (
          <p className="ym-error" role="alert">
            The preset vocabulary is unavailable ({presetsError}). No preset can be
            selected, because the server answers 422 for a key it does not know.
          </p>
        ) : (
          <Skeleton rows={2} height={30} />
        )
      ) : (
        <>
          <Select label="Preset" value={preset} onChange={(e) => setPreset(e.target.value)}>
            <option value="">Select a preset…</option>
            {items.map((p) => (
              <option key={p.key} value={p.key}>
                {humanize(p.key)}
              </option>
            ))}
          </Select>
          <p className="ym-hint">
            {items.length} presets registered by <code>engine/ugc/pipeline.py</code> and
            served by <code>GET /ugc/presets</code>. Nothing is hardcoded here.
          </p>
        </>
      )}

      {chosen ? (
        <div className="ym-hint">
          <strong>{humanize(chosen.key)}:</strong> {chosen.format ?? "no format described"}{" "}
          {typeof chosen.duration_seconds === "number" ? `· ${chosen.duration_seconds}s` : ""}{" "}
          {chosen.hook_type ? `· hook: ${chosen.hook_type}` : ""}
        </div>
      ) : null}

      <Grid min={220} gap="sm">
        <Field
          label="Topic / product name"
          value={topic}
          onChange={(e) => setTopic(e.target.value)}
          hint="Required. The pipeline never invents a product, feature or claim that the brief did not state."
        />
        <Field label="Audience" value={audience} onChange={(e) => setAudience(e.target.value)} />
        <Field label="Tone" value={tone} onChange={(e) => setTone(e.target.value)} />
        <Field label="Hook (optional)" value={hook} onChange={(e) => setHook(e.target.value)} />
        <Field
          label="Features"
          value={features}
          onChange={(e) => setFeatures(e.target.value)}
          hint="Comma-separated. Sourced into brief.product.features for the fallback script and the unsupported-claims check."
        />
        <Field
          label="Product asset refs"
          value={assets}
          onChange={(e) => setAssets(e.target.value)}
          hint="Comma-separated MediaAsset ids or storage keys. A ref that is not a workspace MediaAsset is reported unresolved and FAILS QC."
        />
        <Field label="Variants" value={variants} onChange={(e) => setVariants(e.target.value)} />
        <Select
          label="Presenter (avatar profile)"
          value={presenterId}
          onChange={(e) => setPresenterId(e.target.value)}
        >
          <option value="">None — product assets carry the visuals</option>
          {(profiles ?? []).map((p) => (
            <option key={p.id} value={p.id}>
              {p.name || p.id.slice(0, 8)} — consent: {p.consent_state}
            </option>
          ))}
        </Select>
      </Grid>

      {chosenProfile && chosenProfile.consent_state !== "authorized" ? (
        <p className="ym-error" role="alert">
          <strong>Consent is “{chosenProfile.consent_state}”.</strong>{" "}
          <code>stage_presenter</code> calls <code>require_authorized</code> before any
          voice or avatar work, so this run will raise and the project will be marked
          FAILED. Authorize the profile on the Talent tab first.
        </p>
      ) : null}

      <Toggle
        checked={run}
        onChange={setRun}
        label="Run the pipeline now (uncheck to save a DRAFT)"
      />
      <p className="ym-hint">
        {run
          ? "Running spends money: an LLM script call, per-segment TTS narration, and possibly a generated music bed. A refusal is reported as a refusal — there is no retry button."
          : "Saving a DRAFT writes one row and spends nothing. Nothing is generated until a run is requested."}
      </p>

      {create.error ? (
        <div className="ym-error" role="alert">
          <strong>The project was refused.</strong> {create.error}
          <br />
          {run
            ? "Nothing is re-sent automatically. Fix the brief or the budget, then decide deliberately to run again."
            : "Nothing was created."}
        </div>
      ) : null}
    </Modal>
  );
}

/* ==========================================================================
 * Projects tab
 * ======================================================================= */

function ProjectsTab({
  presets,
  projects,
  profiles,
  selected,
  onSelect,
  onNew,
}: {
  presets: QueryState<UgcPresetList>;
  projects: QueryState<UgcProjectList>;
  profiles: QueryState<AvatarProfileList>;
  selected: string | null;
  onSelect: (id: string) => void;
  onNew: () => void;
}) {
  const write = can(WRITE);
  const reason = blockedReason(WRITE);

  const columns: Column<UgcProject>[] = [
    {
      key: "preset",
      header: "Preset",
      cell: (p) => (
        <span>
          <strong>{humanize(p.preset)}</strong>
          {typeof p.lineage?.topic === "string" && p.lineage.topic ? (
            <span className="ym-notif-detail"> — {p.lineage.topic.slice(0, 70)}</span>
          ) : null}
        </span>
      ),
    },
    { key: "status", header: "Status", cell: (p) => <StatusBadge status={p.status} /> },
    {
      key: "qc",
      header: "QC verdict",
      cell: (p) =>
        p.qc?.status ? (
          <Badge tone={ugcQcTone(p.qc.status)}>{humanize(p.qc.status)}</Badge>
        ) : (
          <span className="ym-muted" title="No QC report on this project row">UNAVAILABLE</span>
        ),
    },
    {
      key: "timeline",
      header: "Timeline",
      cell: (p) =>
        p.timeline_id ? <code>{p.timeline_id.slice(0, 8)}</code> : <span className="ym-muted">none</span>,
      hideBelow: "md",
    },
    {
      key: "render",
      header: "Render",
      cell: (p) =>
        p.render_asset_ref ? <code>{p.render_asset_ref}</code> : <span className="ym-muted">not rendered</span>,
      hideBelow: "lg",
    },
    { key: "created", header: "Created", cell: (p) => when(p.created_at), hideBelow: "md" },
  ];

  return (
    <>
      <Panel
        title="UGC projects"
        subtitle="One row per generation. DRAFT → RUNNING → READY, or BLOCKED when QC fails and the render is refused."
        dense
        actions={
          write ? (
            <Button variant="primary" onClick={onNew}>
              New project
            </Button>
          ) : (
            <Badge tone="neutral" title={reason ?? undefined}>
              BLOCKED
            </Badge>
          )
        }
      >
        {reason ? (
          /* Shown whenever there IS a reason, including when the control itself is
           * hidden — gating on `write && reason` would suppress the explanation
           * in exactly the case that needs it. */
          <p className="ym-hint">{reason}</p>
        ) : null}
        <QueryBoundary query={projects} skeletonRows={5}>
          {(d) => (
            <DataTable
              rows={d.items ?? []}
              columns={columns}
              rowKey={(p) => p.id}
              caption="UGC projects"
              maxHeight={440}
              empty="No UGC project yet"
              emptyHint="A project appears here after POST /ugc/projects saves a DRAFT or runs the pipeline."
              onRowClick={(p) => onSelect(p.id)}
            />
          )}
        </QueryBoundary>
      </Panel>

      <ReadPanel
        title="Registered presets"
        subtitle="engine/ugc/pipeline.py UGC_PRESETS + PRESET_DEFAULTS, served verbatim. An unknown key is refused with 422 by create_project."
        query={presets}
        skeletonRows={3}
      >
        {(d) => (
          <DataTable
            rows={d.items ?? []}
            rowKey={(p: UgcPreset) => p.key}
            caption="UGC presets"
            empty="No preset registered"
            emptyHint="The backend returned an empty preset list, so no UGC project can be created. That is a backend state, not a client fallback."
            columns={[
              { key: "key", header: "Preset", cell: (p: UgcPreset) => <code>{p.key}</code> },
              { key: "format", header: "Format", cell: (p: UgcPreset) => p.format ?? <span className="ym-muted">—</span> },
              {
                key: "duration",
                header: "Duration",
                align: "right",
                cell: (p: UgcPreset) =>
                  typeof p.duration_seconds === "number" ? (
                    `${p.duration_seconds}s`
                  ) : (
                    <span className="ym-muted" title="PRESET_DEFAULTS has no duration for this preset">UNAVAILABLE</span>
                  ),
              },
              {
                key: "hook",
                header: "Hook type",
                cell: (p: UgcPreset) =>
                  p.hook_type ? <Badge tone="info">{humanize(p.hook_type)}</Badge> : <span className="ym-muted">—</span>,
                hideBelow: "md",
              },
            ]}
          />
        )}
      </ReadPanel>

      {selected ? <ProjectDetail projectId={selected} profiles={profiles} /> : null}
    </>
  );
}

/* ==========================================================================
 * Project detail: the workflow strip, QC, brief, render
 * ======================================================================= */

function ProjectDetail({
  projectId,
  profiles,
}: {
  projectId: string;
  profiles: QueryState<AvatarProfileList>;
}) {
  const detail = useWsQuery<UgcProjectDetail>(`/ugc/projects/${projectId}`, { deps: [projectId] });
  const write = can(WRITE);
  const reason = blockedReason(WRITE);
  const [outName, setOutName] = useState("ugc.mp4");
  const [rendered, setRendered] = useState<UgcRenderResult | null>(null);

  /* PAID + QC-gated. 409 means the QC gate refused; 422 is a pipeline error.
   * Neither gets a retry: the render may already have burned time. */
  const render = useMutation<void, UgcRenderResult>(
    () =>
      wsApi.post(`/ugc/projects/${projectId}/render`, { out_name: outName }) as Promise<UgcRenderResult>,
    { onSuccess: setRendered },
  );

  return (
    <Panel title={`Project ${projectId.slice(0, 8)}`} subtitle="The pipeline's own stages, read from its own lineage.">
      <QueryBoundary query={detail} skeletonRows={6}>
        {(p) => {
          const presenter = presenterFrom(p.brief ?? {}, profiles.data?.items ?? null);
          const stages = readStages(p.lineage ?? {}, p.qc ?? { status: "", checks: {} }, presenter);
          const blocked = (p.qc?.status ?? "").toUpperCase() === "FAIL";
          const regeneration = p.qc?.regeneration;

          return (
            <>
              <div>
                <Badge tone={toneForStatus(p.status)}>{humanize(p.status)}</Badge>{" "}
                <Badge tone={ugcQcTone(p.qc?.status)}>QC {p.qc?.status ? humanize(p.qc.status) : "not run"}</Badge>{" "}
                <code>{p.preset}</code>
              </div>

              <h3 className="ym-label">Workflow</h3>
              <StageStrip stages={stages} />

              <Grid min={160} gap="sm">
                <StatTile
                  label="QC checks"
                  value={Object.keys(p.qc?.checks ?? {}).length}
                  unavailable={!p.qc?.status}
                  source="GET /ugc/projects/{id} → qc.checks"
                />
                <StatTile
                  label="Product assets resolved"
                  value={p.product_assets?.resolved?.length}
                  unavailable={p.product_assets === undefined}
                  tone={p.product_assets?.unresolved?.length ? "danger" : "neutral"}
                  hint={
                    p.product_assets?.unresolved?.length
                      ? `${p.product_assets.unresolved.length} reference(s) are not workspace MediaAssets — the QC asset check fails on these.`
                      : undefined
                  }
                  source="GET /ugc/projects/{id} → product_assets"
                />
                <StatTile
                  label="Narrated segments"
                  value={p.lineage?.voice?.length}
                  unavailable={p.lineage?.voice === undefined}
                  source="lineage_json.voice"
                />
                <StatTile
                  label="Music bed"
                  unavailable={p.lineage?.music === undefined}
                  tone={p.lineage?.music?.generated === true ? "success" : "neutral"}
                  value={
                    p.lineage?.music?.generated === true
                      ? "GENERATED"
                      : p.lineage?.music?.generated === false
                        ? "NONE"
                        : undefined
                  }
                  hint={p.lineage?.music?.reason}
                  source="lineage_json.music"
                />
                <StatTile
                  label="Rendered asset"
                  value={p.render_asset_ref || undefined}
                  unavailable={!p.render_asset_ref}
                  tone="info"
                  source="UgcProjectRow.render_asset_ref"
                />
                <StatTile
                  label="Editor"
                  unavailable={!p.open_in_editor}
                  tone={p.open_in_editor ? "info" : "neutral"}
                  value={p.open_in_editor ? `timeline ${p.timeline_id.slice(0, 8)}` : undefined}
                  hint={p.open_in_editor ? undefined : "No timeline exists, so there is nothing to open."}
                  source="GET /ugc/projects/{id} → open_in_editor"
                />
              </Grid>

              {typeof p.lineage?.error === "string" && p.lineage.error ? (
                <div className="ym-error" role="alert">
                  <strong>The pipeline raised.</strong> {p.lineage.error}
                </div>
              ) : null}

              {regeneration?.refused ? (
                <div className="ym-error" role="alert">
                  <strong>A regeneration was refused.</strong> {regeneration.reason}{" "}
                  (expected manifest <code>{String(regeneration.expected_manifest_hash).slice(0, 12)}</code> at
                  version {regeneration.expected_version}; the live timeline is{" "}
                  <code>{String(regeneration.current_manifest_hash).slice(0, 12)}</code> at version{" "}
                  {regeneration.current_version}.)
                </div>
              ) : null}

              <h3 className="ym-label">QC checks</h3>
              <QcChecks qc={p.qc} />

              <h3 className="ym-label">Brief</h3>
              {Object.keys(p.brief ?? {}).length === 0 ? (
                <EmptyState title="Empty brief" description="The project row carries no brief fields." />
              ) : (
                <DataTable
                  rows={Object.entries(p.brief ?? {}).map(([k, v]) => ({ k, v }))}
                  rowKey={(r) => r.k}
                  caption="Brief fields"
                  empty="Empty brief"
                  columns={[
                    { key: "field", header: "Field", cell: (r) => <code>{r.k}</code> },
                    {
                      key: "value",
                      header: "Value",
                      cell: (r) => <span>{renderBriefValue(r.v)}</span>,
                    },
                  ]}
                />
              )}

              {p.product_assets?.unresolved?.length ? (
                <>
                  <h3 className="ym-label">Unresolved asset references</h3>
                  <DataTable
                    rows={p.product_assets.unresolved}
                    rowKey={(u, i) => `${u.reason}-${i}`}
                    caption="Unresolved product assets"
                    empty="None unresolved"
                    columns={[
                      { key: "ref", header: "Reference", cell: (u) => <code>{JSON.stringify(u.entry)}</code> },
                      { key: "reason", header: "Reason", cell: (u) => <Badge tone="warning">{humanize(u.reason)}</Badge> },
                    ]}
                  />
                </>
              ) : null}

              <h3 className="ym-label">Render</h3>
              {write ? (
                <>
                  <Field
                    label="Output name"
                    value={outName}
                    maxLength={120}
                    onChange={(e) => setOutName(e.target.value)}
                    hint="Sent as UgcRenderBody.out_name; the server defaults it to ugc.mp4."
                  />
                  <Button
                    variant="primary"
                    loading={render.pending}
                    disabled={blocked}
                    onClick={() => void render.run()}
                  >
                    POST /ugc/projects/{projectId.slice(0, 8)}/render
                  </Button>
                  {blocked ? (
                    <p className="ym-hint">
                      The control is disabled because QC is FAIL. The server would
                      answer <strong>409</strong> — the gate exists so an unreviewed cut
                      is never shipped.
                    </p>
                  ) : null}
                </>
              ) : (
                <EmptyState title="Rendering is blocked" description={reason ?? undefined} />
              )}
              {render.error ? (
                <div className="ym-error" role="alert">
                  <strong>The render was refused.</strong> {render.error}
                  <br />
                  No retry is offered: a render spends time and, when the QC gate
                  refuses, the refusal is the decision. Read the detail and act on it.
                </div>
              ) : null}
              {rendered ? (
                <p>
                  <Badge tone="success">RENDERED</Badge>{" "}
                  <code>{rendered.render.asset_id}</code>{" "}
                  <span className="ym-notif-detail">
                    {typeof rendered.render.duration_seconds === "number"
                      ? `${rendered.render.duration_seconds.toFixed(2)}s`
                      : "duration not reported"}{" "}
                    · QC {humanize(rendered.render.qc?.status)}
                  </span>
                </p>
              ) : null}
            </>
          );
        }}
      </QueryBoundary>
    </Panel>
  );
}

function renderBriefValue(value: unknown): string {
  if (typeof value === "string") return value;
  if (Array.isArray(value)) return value.length === 0 ? "(empty list)" : value.map((v) => String(v)).join(", ");
  if (value && typeof value === "object") return JSON.stringify(value);
  return String(value);
}

/** `qc.checks` is a NAME -> {status, detail} map; failing checks lead. */
function QcChecks({ qc }: { qc: UgcQc | undefined }) {
  const entries = Object.entries(qc?.checks ?? {});
  if (!qc?.status) {
    return <StatTile label="QC report" unavailable hint="No QC report has been written for this project." source="GET /ugc/projects/{id}" />;
  }
  if (entries.length === 0) {
    return (
      <EmptyState
        title="Report carries no checks"
        description={`qc.status is “${qc.status}” but qc.checks is empty, so nothing was actually verified.`}
      />
    );
  }
  const flagged = entries.filter(([, v]) => (v?.status ?? "").toLowerCase() !== "pass");
  const shown = flagged.length > 0 ? flagged : entries;
  return (
    <>
      {flagged.length === 0 ? (
        <p className="ym-hint">All {entries.length} checks passed.</p>
      ) : (
        <p className="ym-hint">
          {flagged.length} of {entries.length} checks need attention; only those are shown.
        </p>
      )}
      <DataTable
        rows={shown.map(([name, v]) => ({ name, status: v?.status ?? "", detail: v?.detail ?? "" }))}
        rowKey={(r) => r.name}
        caption="UGC QC checks"
        maxHeight={340}
        empty="No check rows"
        columns={[
          {
            key: "status",
            header: "Result",
            cell: (r) => (
              <Badge tone={r.status === "fail" ? "danger" : r.status === "warning" ? "warning" : "success"}>
                {humanize(r.status)}
              </Badge>
            ),
          },
          { key: "name", header: "Check", cell: (r) => <code>{r.name}</code> },
          { key: "detail", header: "Detail", cell: (r) => r.detail || <span className="ym-muted">—</span> },
        ]}
      />
    </>
  );
}

/* ==========================================================================
 * Talent tab: avatars + consent + provider health
 * ======================================================================= */

function TalentTab() {
  const health = useWsQuery<AvatarHealth>("/avatars/health");
  const list = useWsQuery<AvatarProfileList>("/avatars");
  const write = can(WRITE);
  const reason = blockedReason(WRITE);
  const [creating, setCreating] = useState(false);
  const [rendered, setRendered] = useState<AvatarRenderResult | null>(null);
  const [renderTarget, setRenderTarget] = useState<{ id: string; audio: string }>({ id: "", audio: "" });
  const [authorizing, setAuthorizing] = useState<AvatarProfile | null>(null);

  /* PAID: an avatar render consumes provider time. No retry on refusal. */
  const renderAvatar = useMutation<void, AvatarRenderResult>(
    () =>
      wsApi.post("/avatars/render", {
        profile_id: renderTarget.id,
        audio_ref: renderTarget.audio.trim(),
        opts: {},
        timeline: true,
      }) as Promise<AvatarRenderResult>,
    { onSuccess: setRendered },
  );

  return (
    <>
      <Panel
        title="Avatar provider maturity"
        subtitle="engine/avatar/profile.py::avatar_health(). A not-ready provider is rendered as not ready — a blocked backend is never presented as available."
      >
        <Grid min={160} gap="sm">
          <StatTile
            label="Provider"
            value={health.data?.provider || undefined}
            unavailable={!health.data?.provider}
            source="GET /avatars/health"
          />
          <StatTile
            label="Backend ready"
            unavailable={typeof health.data?.ready !== "boolean"}
            tone={health.data?.ready === true ? "success" : health.data?.ready === false ? "danger" : "neutral"}
            value={typeof health.data?.ready === "boolean" ? (health.data.ready ? "READY" : "NOT READY") : undefined}
            hint="POST /avatars/render answers 503 with remediation while this is false."
            source="GET /avatars/health"
          />
          <StatTile
            label="Detail"
            value={health.data?.detail || undefined}
            unavailable={!health.data?.detail}
            source="GET /avatars/health"
          />
          <StatTile
            label="Consent required"
            unavailable={typeof health.data?.consent_required !== "boolean"}
            tone={health.data?.consent_required === true ? "warning" : "neutral"}
            value={typeof health.data?.consent_required === "boolean" ? (health.data.consent_required ? "YES" : "NO") : undefined}
            hint="When true, an unauthorized profile is refused with 403 before any provider work."
            source="GET /avatars/health"
          />
          <StatTile
            label="Authorized profiles"
            value={list.data ? list.data.items.filter((p) => p.consent_state === "authorized").length : undefined}
            unavailable={list.data === null}
            tone="info"
            source="GET /avatars"
          />
          <StatTile
            label="Pending consent"
            value={list.data ? list.data.items.filter((p) => p.consent_state === "pending").length : undefined}
            unavailable={list.data === null}
            tone="warning"
            hint="A pending profile blocks both its own render and any UGC run that presents it."
            source="GET /avatars"
          />
        </Grid>
        {health.error ? (
          <ErrorState title="Avatar health unavailable" message={health.error} onRetry={health.reload} />
        ) : null}
        {health.data?.ready === false ? (
          <p className="ym-error" role="alert">
            <strong>This backend cannot render.</strong>{" "}
            {health.data.detail || "No detail was reported."} Configure an avatar
            backend under Settings → Connections; the render route answers 503 until
            then, so nothing here pretends otherwise.
          </p>
        ) : null}
        {health.data?.capabilities && Object.keys(health.data.capabilities).length > 0 ? (
          <p className="ym-hint">
            {Object.entries(health.data.capabilities)
              .map(([k, v]) => `${k}: ${String(v)}`)
              .join(" · ")}
          </p>
        ) : null}
      </Panel>

      <Panel
        title="Avatar profiles"
        subtitle="Consent starts pending on every profile. Authorization records where the consent came from."
        dense
        actions={
          write ? (
            <Button variant="primary" onClick={() => setCreating(true)}>
              New profile
            </Button>
          ) : (
            <Badge tone="neutral" title={reason ?? undefined}>
              BLOCKED
            </Badge>
          )
        }
      >
        {rendered ? (
          <p>
            <Badge tone={rendered.qc?.status?.toUpperCase() === "FAIL" ? "danger" : "success"}>
              QC {humanize(rendered.qc?.status)}
            </Badge>{" "}
            <code>{rendered.asset_id}</code>{" "}
            {rendered.is_mock ? (
              <Badge tone="warning">mock backend — not a real render</Badge>
            ) : (
              <span className="ym-notif-detail">
                {rendered.backend} · {rendered.duration.toFixed(2)}s
              </span>
            )}
            {rendered.timeline_id ? <code> · timeline {rendered.timeline_id.slice(0, 8)}</code> : null}
          </p>
        ) : null}
        {renderAvatar.error ? (
          <div className="ym-error" role="alert">
            <strong>The avatar render was refused.</strong> {renderAvatar.error}
            <br />
            No retry is offered. A 403 means consent is not authorized — a human
            decision, not a transient failure — and a 503 means no backend is
            configured, which pressing the button again cannot change.
          </div>
        ) : null}

        <QueryBoundary query={list} skeletonRows={4}>
          {(d) => (
            <DataTable
              rows={d.items ?? []}
              rowKey={(p) => p.id}
              caption="Avatar profiles"
              empty="No avatar profile yet"
              emptyHint="A profile appears here after POST /avatars. Consent starts pending and rendering stays refused until it is authorized."
              columns={[
                { key: "name", header: "Profile", cell: (p) => p.name || <code>{p.id.slice(0, 8)}</code> },
                {
                  key: "consent",
                  header: "Consent",
                  cell: (p) => (
                    <Badge tone={p.consent_state === "authorized" ? "success" : p.consent_state === "revoked" ? "danger" : "warning"}>
                      {p.consent_state}
                    </Badge>
                  ),
                },
                { key: "status", header: "Status", cell: (p) => <StatusBadge status={p.status} /> },
                {
                  key: "source",
                  header: "Portrait asset",
                  cell: (p) => <code>{p.source_asset_ref}</code>,
                  hideBelow: "md",
                },
                {
                  key: "voice",
                  header: "Voice ref",
                  cell: (p) => p.profile?.voice_ref ? <code>{p.profile.voice_ref}</code> : <span className="ym-muted">none</span>,
                  hideBelow: "lg",
                },
                {
                  key: "grant",
                  header: "Consent evidence",
                  cell: (p) =>
                    p.consent?.source ? (
                      <span className="ym-notif-detail">
                        {p.consent.source}
                        {p.consent.granted_by ? ` · ${p.consent.granted_by}` : ""}
                      </span>
                    ) : (
                      <span className="ym-muted">none recorded</span>
                    ),
                  hideBelow: "md",
                },
                { key: "created", header: "Created", cell: (p) => when(p.created_at), hideBelow: "lg" },
                ...(write
                  ? [
                      {
                        key: "actions",
                        header: "",
                        align: "right" as const,
                        cell: (p: AvatarProfile) =>
                          p.consent_state === "authorized" ? (
                            <Button
                              size="sm"
                              onClick={() => {
                                setAuthorizing(null);
                                setRenderTarget({ id: p.id, audio: "" });
                              }}
                            >
                              Render…
                            </Button>
                          ) : (
                            <Button
                              size="sm"
                              onClick={() => {
                                setRenderTarget({ id: "", audio: "" });
                                setAuthorizing(p);
                              }}
                            >
                              Authorize…
                            </Button>
                          ),
                      },
                    ]
                  : []),
              ]}
            />
          )}
        </QueryBoundary>
      </Panel>

      {renderTarget.id !== "" ? (
        <Panel
          title={`Render ${renderTarget.id.slice(0, 8)}`}
          subtitle="consent-gated: the route refuses with 403 before any provider work unless consent_state is authorized."
        >
          <Grid min={220} gap="sm">
            <Field
              label="Audio ref"
              value={renderTarget.audio}
              maxLength={1024}
              onChange={(e) => setRenderTarget((t) => ({ ...t, audio: e.target.value }))}
              hint="MediaAsset id of the narration to lip-sync against."
            />
          </Grid>
          <Button
            variant="primary"
            loading={renderAvatar.pending}
            disabled={renderTarget.audio.trim() === ""}
            onClick={() => void renderAvatar.run()}
          >
            POST /avatars/render
          </Button>
          <Button variant="ghost" onClick={() => setRenderTarget({ id: "", audio: "" })}>
            Close
          </Button>
        </Panel>
      ) : null}

      {authorizing ? (
        <AuthorizePanel
          profile={authorizing}
          onDone={() => {
            setAuthorizing(null);
            list.reload();
          }}
        />
      ) : null}

      {creating ? (
        <CreateProfileModal
          onClose={() => setCreating(false)}
          onCreated={() => {
            setCreating(false);
            list.reload();
          }}
        />
      ) : null}
    </>
  );
}

function CreateProfileModal({ onClose, onCreated }: { onClose: () => void; onCreated: () => void }) {
  const [name, setName] = useState("");
  const [source, setSource] = useState("");
  const [voice, setVoice] = useState("");
  const [language, setLanguage] = useState("en");
  const [background, setBackground] = useState("studio");

  const create = useMutation<void, AvatarProfile>(
    () =>
      wsApi.post("/avatars", {
        name: name.trim(),
        source_asset_ref: source.trim(),
        voice_ref: voice.trim(),
        language,
        background,
      }) as Promise<AvatarProfile>,
    { onSuccess: onCreated },
  );

  return (
    <Modal
      open
      onClose={onClose}
      title="New avatar profile"
      width={620}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Close
          </Button>
          <Button
            variant="primary"
            loading={create.pending}
            disabled={source.trim() === ""}
            onClick={() => void create.run()}
          >
            POST /avatars
          </Button>
        </>
      }
    >
      <p className="ym-hint">
        Consent starts <strong>pending</strong>. Nothing renders until an operator
        authorizes the portrait with a recorded source — that is the point of the
        field below, not a formality.
      </p>
      <Grid min={200} gap="sm">
        <Field label="Name" value={name} maxLength={160} onChange={(e) => setName(e.target.value)} />
        <Field
          label="Portrait asset ref"
          value={source}
          maxLength={1024}
          onChange={(e) => setSource(e.target.value)}
          hint="Required — the workspace MediaAsset holding the portrait."
        />
        <Field label="Voice ref (optional)" value={voice} maxLength={1024} onChange={(e) => setVoice(e.target.value)} />
        <Field label="Language" value={language} maxLength={10} onChange={(e) => setLanguage(e.target.value)} />
        <Field label="Background" value={background} maxLength={80} onChange={(e) => setBackground(e.target.value)} />
      </Grid>
      {create.error ? (
        <p className="ym-error" role="alert">
          {create.error}
        </p>
      ) : null}
    </Modal>
  );
}

function AuthorizePanel({ profile, onDone }: { profile: AvatarProfile; onDone: () => void }) {
  const [source, setSource] = useState("");
  const [grantedBy, setGrantedBy] = useState("");
  const [statement, setStatement] = useState("");

  const authorize = useMutation<void, AvatarProfile>(
    () =>
      wsApi.post(`/avatars/${profile.id}/authorize`, {
        source: source.trim(),
        authorization_evidence: { recorded_by: "ymoney-studio", consent_source: source.trim() },
        granted_by: grantedBy.trim(),
        statement: statement.trim(),
      }) as Promise<AvatarProfile>,
    { onSuccess: onDone },
  );

  return (
    <Panel title={`Authorize ${profile.name || profile.id.slice(0, 8)}`} subtitle="Record where the consent came from. An operator's word is the record; the server keeps it verbatim.">
      <Grid min={220} gap="sm">
        <Field
          label="Consent source"
          value={source}
          maxLength={400}
          onChange={(e) => setSource(e.target.value)}
          hint="Required by AvatarAuthorize.source — a signed release id, a contract, a URL."
        />
        <Field label="Granted by" value={grantedBy} maxLength={200} onChange={(e) => setGrantedBy(e.target.value)} />
        <Field label="Statement (optional)" value={statement} maxLength={1000} onChange={(e) => setStatement(e.target.value)} />
      </Grid>
      <Button
        variant="primary"
        loading={authorize.pending}
        disabled={source.trim() === ""}
        onClick={() => void authorize.run()}
      >
        POST /avatars/{profile.id.slice(0, 8)}/authorize
      </Button>
      {authorize.error ? (
        <p className="ym-error" role="alert">
          {authorize.error}
        </p>
      ) : null}
    </Panel>
  );
}

/* ==========================================================================
 * Screen
 * ======================================================================= */

const TABS = [
  { id: "projects", label: "Projects" },
  { id: "talent", label: "Talent" },
] as const;

export function Ugc() {
  const { workspaceId, workspace } = useSession();
  const [tab, setTab] = useState<string>("projects");
  const [selected, setSelected] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);

  const presets = useWsQuery<UgcPresetList>("/ugc/presets");
  const projects = useWsQuery<UgcProjectList>("/ugc/projects");
  const profiles = useWsQuery<AvatarProfileList>("/avatars");

  const counts = useMemo(() => {
    if (projects.data === null) return null;
    const items = projects.data.items;
    return {
      total: items.length,
      ready: items.filter((p) => (p.status ?? "").toUpperCase() === "READY").length,
      blocked: items.filter((p) => (p.status ?? "").toUpperCase() === "BLOCKED").length,
      rendered: items.filter((p) => p.render_asset_ref !== "").length,
    };
  }, [projects.data]);

  if (!workspaceId) {
    return (
      <>
        <PageHeader title="UGC Studio" description="Format, script, talent, assets, voice, music, timeline, QC, render." />
        <Panel title="No workspace selected">
          <EmptyState
            title="Nothing to show"
            description="UGC projects and avatar profiles are workspace-scoped. Select a workspace to load them."
          />
        </Panel>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="UGC Studio"
        description={
          workspace?.name
            ? `${workspace.name} — the UGC workflow with the backend's own stages and QC verdicts.`
            : "The UGC workflow with the backend's own stages and QC verdicts."
        }
        actions={
          <>
            <Button
              onClick={() => {
                presets.reload();
                projects.reload();
                profiles.reload();
              }}
            >
              Refresh
            </Button>
            {can(WRITE) ? (
              <Button variant="primary" onClick={() => setCreating(true)}>
                New project
              </Button>
            ) : null}
          </>
        }
      />

      <Panel title="At a glance" dense>
        <Grid min={170} gap="sm">
          <StatTile
            label="Projects"
            value={counts?.total}
            unavailable={counts === null}
            source="GET /ugc/projects"
          />
          <StatTile
            label="Ready"
            value={counts?.ready}
            unavailable={counts === null}
            tone="success"
            hint="QC PASS or PASS_WITH_WARNINGS maps the project to READY."
            source="GET /ugc/projects"
          />
          <StatTile
            label="Blocked by QC"
            value={counts?.blocked}
            unavailable={counts === null}
            tone="danger"
            hint="A FAIL verdict. The render route answers 409 for these."
            source="GET /ugc/projects"
          />
          <StatTile
            label="Rendered"
            value={counts?.rendered}
            unavailable={counts === null}
            tone="info"
            source="GET /ugc/projects → render_asset_ref"
          />
          <StatTile
            label="Presets registered"
            value={presets.data?.items.length}
            unavailable={presets.data === null}
            hint="Read from the backend; nothing is hardcoded in this screen."
            source="GET /ugc/presets"
          />
          <StatTile
            label="Avatar profiles"
            value={profiles.data?.items.length}
            unavailable={profiles.data === null}
            source="GET /avatars"
          />
        </Grid>
        {projects.error ? (
          <div role="alert">
            <ErrorState title="Projects could not be read" message={projects.error} onRetry={projects.reload} />
          </div>
        ) : null}
        {presets.error ? (
          <div role="alert">
            <ErrorState title="Presets could not be read" message={presets.error} onRetry={presets.reload} />
          </div>
        ) : null}
        {profiles.error ? (
          <div role="alert">
            <ErrorState title="Avatar profiles could not be read" message={profiles.error} onRetry={profiles.reload} />
          </div>
        ) : null}
      </Panel>

      <Tabs tabs={TABS.map((t) => ({ id: t.id, label: t.label }))} active={tab} onChange={setTab} />

      {tab === "projects" ? (
        <ProjectsTab
          presets={presets}
          projects={projects}
          profiles={profiles}
          selected={selected}
          onSelect={setSelected}
          onNew={() => setCreating(true)}
        />
      ) : null}
      {tab === "talent" ? <TalentTab /> : null}

      {creating ? (
        <CreateProjectModal
          presets={presets.data}
          presetsError={presets.error}
          profiles={profiles.data?.items ?? null}
          onClose={() => setCreating(false)}
          onCreated={(id) => {
            setCreating(false);
            setSelected(id);
            projects.reload();
            presets.reload();
          }}
        />
      ) : null}
    </>
  );
}

export default Ugc;

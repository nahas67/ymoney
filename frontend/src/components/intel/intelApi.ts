/* Work 12 FE lane -- the media-intelligence wire contract.
 *
 * EVERY type and endpoint in this file is transcribed from the mounted
 * routers, not guessed:
 *
 *   providers / runs      api/v1/media_intel.py
 *   audio enhancement     api/v1/media_intel_audio.py + engine/intel/audio_enhance.py
 *   silence / fillers     api/v1/media_intel_edits.py + engine/intel/silence_fillers.py
 *   qc                    api/v1/media_intel_qc.py + engine/intel/qc.py
 *   faces                 api/v1/media_intel_faces.py
 *   masks / active-speaker api/v1/media_intel_visual.py
 *   reframe / background  api/v1/media_intel_reframe.py + engine/intel/reframe.py
 *
 * This module holds no React so both panels and the shared bits can use it.
 */

import { wsApi } from "../../lib/api";

/* ------------------------------------------------------------------ *
 * vocabularies (mirrored from the engine, one list each)
 * ------------------------------------------------------------------ */

/** `engine/intel/audio_enhance.py::STAGES`. */
export const ENHANCE_STAGES = [
  "denoise",
  "dereverb",
  "voice_isolation",
  "silence_detection",
  "filler_detection",
  "breath_click_detection",
  "loudness_normalization",
  "music_ducking",
] as const;

/** `engine/intel/audio_enhance.py::STAGE_STATUSES`. */
export type StageStatus = "applied" | "skipped" | "unavailable" | "failed";

/** `engine/intel/silence_fillers.py::proposal_dto["kind"]`. */
export type ProposalKind = "REMOVE_RANGE" | "SHORTEN_RANGE" | "KEEP";

/** `DecideRequest.decision` -- a closed vocabulary (422 on anything else). */
export type ProposalDecision = "keep" | "remove" | "shorten";

/** `engine/intel/reframe.py::LAYOUTS` (contracts §11). */
export const REFRAME_LAYOUTS = [
  "ACTIVE_SPEAKER",
  "SPLIT_SCREEN",
  "TWO_SHOT",
  "GRID",
  "HOST_GUEST",
  "PODCAST_DYNAMIC",
] as const;

/** `engine/intel/reframe.py::TARGET_ASPECTS`. */
export const TARGET_ASPECTS = ["9:16", "4:5", "1:1"] as const;

/** `engine/intel/reframe.py::BACKGROUND_OPS`. */
export const BACKGROUND_OPS = [
  "background_blur",
  "background_replace",
  "person_mask",
  "object_mask",
] as const;

/** `engine/intel/qc.py::QC_VERDICTS`. */
export type QcVerdict = "PASS" | "PASS_WITH_WARNINGS" | "REVIEW_REQUIRED" | "FAIL";

/** `engine/intel/qc.py::CHECK_STATUSES`. */
export type CheckStatus = "OK" | "WARN" | "VIOLATION" | "UNKNOWN";

/**
 * `engine/intel/active_speaker.py::UNRESOLVED_REASONS`. A row carries one of
 * these when it is UNRESOLVED; the panel renders it verbatim and NEVER
 * substitutes a speaker for it.
 */
export const UNRESOLVED_REASONS = [
  "no_diarization",
  "no_face_track",
  "ambiguous_tie",
  "low_overlap",
  "low_confidence",
] as const;

/* ------------------------------------------------------------------ *
 * providers (GET /media-intel/providers)
 * ------------------------------------------------------------------ */

export type ProviderHealth = {
  available: boolean;
  reason: string;
  version: string;
  mode: string;
  detail?: Record<string, unknown>;
};

export type ProviderLicense = {
  code_license: string;
  code_license_url?: string | null;
  model_license: string;
  model_license_url?: string | null;
  model_gated: boolean;
  commercial_use: string;
  audited_on?: string | null;
  notes?: string | null;
};

export type IntelProvider = {
  key: string;
  kinds: string[];
  health: ProviderHealth;
  capabilities: Record<string, unknown>;
  license: ProviderLicense;
  resources: {
    gpu: boolean;
    vram_mb: number;
    ram_mb: number;
    model_bytes: number;
    cpu_seconds_per_audio_minute: number;
    notes?: string;
  };
  kind?: string;
  chain?: string[];
  selected?: boolean;
  commercial_blocked?: boolean;
};

export type ProviderListing = {
  items: IntelProvider[];
  kinds: string[];
  commercial_mode: boolean;
  available: string[];
};

export function listProviders(): Promise<ProviderListing> {
  return wsApi.get("/media-intel/providers") as Promise<ProviderListing>;
}

/**
 * The provider that a capability would resolve to, or the first honest reason it
 * is dark. `registry.resolve` returns the provider only when a chain member is
 * installed, so an absent entry IS the unavailability signal -- the panel must
 * disable the action and show `health.reason`, never pretend.
 */
export function providerFor(listing: ProviderListing | null, kind: string): IntelProvider | null {
  if (!listing) return null;
  return listing.items.find((p) => p.kind === kind && p.selected) ?? null;
}

/**
 * Every reason the chain for `kind` is dark, as one sentence. An EMPTY string
 * means the capability resolves (at least one chain member is available) --
 * the panel must not disable the action in that case, and must not enable it
 * when this is non-empty.
 */
export function unavailableReasons(listing: ProviderListing | null, kind: string): string {
  if (!listing) return "";
  const dark = listing.items.filter((p) => p.kind === kind && !p.health.available);
  if (!dark.length) return "";
  return dark.map((p) => `${p.key}: ${p.health.reason || "not available"}`).join("; ");
}

/** True when the capability has a provider at all (chain resolved). */
export function capabilityAvailable(listing: ProviderListing | null, kind: string): boolean {
  return Boolean(providerFor(listing, kind));
}

/* ------------------------------------------------------------------ *
 * runs (GET /media-intel/runs/{id}) -- services/media_intel_runs.py::run_dto
 * ------------------------------------------------------------------ */

export type IntelRun = {
  id: string;
  workspace_id: string;
  asset_id: string;
  kind: string;
  provider_key: string;
  model_version: string;
  params: Record<string, unknown>;
  status: string;
  progress: number;
  chunks_total: number;
  chunks_done: number;
  cancel_requested: boolean;
  started_at: string | null;
  finished_at: string | null;
  processing_ms: number;
  gpu_ms: number;
  cost_micros: number;
  warnings: string[];
  metrics: Record<string, any>;
  error_code: string;
  output_asset_id: string | null;
  requested_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  terminal: boolean;
  retryable: boolean;
  cache_hit: boolean;
};

export type RunSummary = { items: IntelRun[] };

export function listRuns(query: { asset_id?: string; kind?: string; status?: string } = {}): Promise<RunSummary> {
  const q = new URLSearchParams();
  if (query.asset_id) q.set("asset_id", query.asset_id);
  if (query.kind) q.set("kind", query.kind);
  if (query.status) q.set("status", query.status);
  const suffix = q.toString() ? `?${q.toString()}` : "";
  return wsApi.get(`/media-intel/runs${suffix}`) as Promise<RunSummary>;
}

export function getRun(runId: string): Promise<IntelRun> {
  return wsApi.get(`/media-intel/runs/${encodeURIComponent(runId)}`) as Promise<IntelRun>;
}

export function retryRun(runId: string): Promise<IntelRun> {
  return wsApi.post(`/media-intel/runs/${encodeURIComponent(runId)}/retry`, {}) as Promise<IntelRun>;
}

/* ------------------------------------------------------------------ *
 * audio enhancement (POST /media-intel/audio/enhance)
 * ------------------------------------------------------------------ */

export type EnhanceStageRow = {
  stage: string;
  status: StageStatus;
  method?: string;
  reason?: string;
  result?: Record<string, unknown> | null;
};

export type EnhanceResponse = {
  run: IntelRun;
  stages: EnhanceStageRow[];
  analysis: Record<string, any>;
  before_after: { before?: Record<string, any>; after?: Record<string, any>; deltas?: Record<string, any>; measured?: boolean };
  claims: { name?: string; value?: unknown; method?: string; confidence?: string }[];
  manifest: {
    pipeline?: string;
    provider?: string;
    provider_health?: Record<string, any>;
    license?: Record<string, any>;
    model_version?: string;
    stages?: EnhanceStageRow[];
    stages_applied?: string[];
    stages_unavailable?: { stage: string; reason: string }[];
    stages_failed?: { stage: string; reason: string }[];
    stages_skipped?: { stage: string; reason: string }[];
    processing_ms?: number;
    derived?: boolean;
    [k: string]: unknown;
  };
  warnings: string[];
  cache_hit: boolean;
  job?: Record<string, unknown>;
  stages_supported?: string[];
};

export function enhanceAudio(
  assetId: string,
  stages: string[],
  extra: { force?: boolean } = {}
): Promise<EnhanceResponse> {
  return wsApi.post("/media-intel/audio/enhance", {
    asset_id: assetId,
    stages,
    force: Boolean(extra.force),
  }) as Promise<EnhanceResponse>;
}

export function getEnhanceRun(runId: string): Promise<{
  run: IntelRun;
  stages: EnhanceStageRow[];
  before_after: Record<string, any>;
  claims: unknown[];
  manifest: Record<string, any>;
  providers: { stage: string; available: boolean; reason: string; provider: string }[];
}> {
  return wsApi.get(`/media-intel/audio/enhance/${encodeURIComponent(runId)}`) as Promise<any>;
}

/* ------------------------------------------------------------------ *
 * proposals + time map (api/v1/media_intel_edits.py)
 * ------------------------------------------------------------------ */

export type Proposal = {
  id: string;
  workspace_id: string;
  project_id: string | null;
  asset_id: string;
  run_id: string;
  kind: ProposalKind;
  start_s: number;
  end_s: number;
  duration_s: number;
  reason: string;
  confidence: number | null;
  status: string;
  decision: ProposalDecision | null;
  operations: Record<string, any>[];
  evidence: Record<string, any>;
  decided_by: string | null;
  decided_at: string | null;
  created_at: string | null;
};

/** `engine/intel/silence_fillers.py::TimeMap.to_dict`. */
export type TimeMap = {
  policy_id: string;
  segments: { start_s: number; end_s: number; shift_s: number }[];
  removals: { start_s: number; end_s: number }[];
  source_duration_s: number;
  output_duration_s: number;
  removed_duration_s: number;
  removal_ratio: number;
  id?: string;
  asset_id?: string;
  created_at?: string | null;
  mapped?: {
    time?: { source_s: number; edited_s: number; inverse_s: number };
    range?: {
      source: { start_s: number; end_s: number };
      edited_start_s?: number;
      edited_end_s?: number;
      [k: string]: unknown;
    };
  };
};

/** `POST /audio/silence` and `POST /audio/fillers` share this envelope. */
export type DetectionResponse = {
  run: IntelRun;
  cache_hit: boolean;
  applied: boolean;
  proposals: Proposal[];
  counts_by_reason?: Record<string, number>;
  policy?: Record<string, any>;
  policy_id?: string;
  auto_apply?: boolean;
  text_source?: {
    source: string;
    available: boolean;
    unit: string;
    reason: string;
    units: number;
    lexicon_version: string;
  };
  removal_ratio?: { planned: number; cap: number; triggered: boolean; reason?: string };
  qc?: { required: boolean; reason: string; triggered?: boolean };
  detection?: { measured: boolean; reason: string; ranges?: unknown[]; [k: string]: unknown };
  metrics?: Record<string, any>;
  events?: { kind: string }[];
};

export function detectSilence(
  assetId: string,
  opts: { timelineId?: string; force?: boolean } = {}
): Promise<DetectionResponse> {
  return wsApi.post("/media-intel/audio/silence", {
    asset_id: assetId,
    timeline_id: opts.timelineId ?? null,
    force: Boolean(opts.force),
  }) as Promise<DetectionResponse>;
}

export function detectFillers(
  assetId: string,
  opts: { timelineId?: string; force?: boolean } = {}
): Promise<DetectionResponse> {
  return wsApi.post("/media-intel/audio/fillers", {
    asset_id: assetId,
    timeline_id: opts.timelineId ?? null,
    force: Boolean(opts.force),
  }) as Promise<DetectionResponse>;
}

export function listProposals(query: { asset_id?: string; run_id?: string; status?: string }): Promise<{ items: Proposal[]; total: number }> {
  const q = new URLSearchParams();
  if (query.asset_id) q.set("asset_id", query.asset_id);
  if (query.run_id) q.set("run_id", query.run_id);
  if (query.status) q.set("status", query.status);
  return wsApi.get(`/media-intel/proposals?${q.toString()}`) as Promise<{ items: Proposal[]; total: number }>;
}

export function decideProposal(proposalId: string, decision: ProposalDecision): Promise<Proposal> {
  return wsApi.post(`/media-intel/proposals/${encodeURIComponent(proposalId)}/decide`, { decision }) as Promise<Proposal>;
}

/**
 * `POST /proposals/apply` -- the QC-gated canonical Work 02 save.
 *
 * The route builds the operation batch and submits it through
 * `api.v1.timelines.apply_timeline_operations`, i.e. the SAME function the
 * editor's `POST /timelines/{id}/operations` uses, so `base_version` is the
 * same optimistic-concurrency gate. It answers 409 when no QC verdict exists,
 * when the verdict is FAIL without a recorded override, or when `override` is
 * set without one.
 */
export function applyProposals(body: {
  timeline_id: string;
  base_version: number;
  proposal_ids: string[];
  asset_id?: string;
  override?: boolean;
}): Promise<{
  applied: number;
  operations: Record<string, any>[];
  proposals: Proposal[];
  proposal_ids: string[];
  skipped: string[];
  policy: Record<string, any>;
  policy_id: string;
  time_map: TimeMap;
  mapped_scenes: number;
  qc: QcApplyDecision[];
  timeline: { id: string; version: number; [k: string]: unknown };
}> {
  return wsApi.post("/media-intel/proposals/apply", body) as Promise<any>;
}

export function getTimeMap(assetId: string, policyId?: string): Promise<TimeMap> {
  const q = new URLSearchParams({ asset_id: assetId });
  if (policyId) q.set("policy_id", policyId);
  return wsApi.get(`/media-intel/time-map?${q.toString()}`) as Promise<TimeMap>;
}

/* ------------------------------------------------------------------ *
 * qc (api/v1/media_intel_qc.py + engine/intel/qc.py)
 * ------------------------------------------------------------------ */

export type QcCheck = {
  name: string;
  ok: boolean;
  verdict: CheckStatus;
  severity: string;
  measured: unknown;
  threshold: unknown;
  comparator: string;
  reason: string;
  method: string;
  evidence: Record<string, any>;
};

export type QcResult = {
  id: string;
  workspace_id?: string;
  run_id: string;
  kind: string;
  verdict: QcVerdict;
  checks: QcCheck[];
  failures: string[];
  review_reasons: string[];
  warnings: string[];
  unknown?: string[];
  overrides?: QcOverride[];
  override?: QcOverride | null;
  apply_requires_override?: boolean;
  measured?: Record<string, unknown>;
  persisted?: boolean;
  created_at?: string | null;
  events_emitted?: string[];
};

export type QcOverride = {
  id?: string;
  by?: string;
  by_user?: string;
  reason?: string;
  checks?: string[];
  created_at?: string | null;
};

export type QcListing = {
  items: QcResult[];
  latest: Record<string, QcResult>;
  run_id: string;
  verdicts: string[];
};

/** The per-run decision the apply route echoes back (`qc_decisions`). */
export type QcApplyDecision = {
  run_id?: string;
  result_id?: string;
  kind?: string;
  verdict: QcVerdict;
  allowed: boolean;
  override_used?: boolean;
  failures?: string[];
};

export function getQc(runId: string): Promise<QcListing> {
  return wsApi.get(`/media-intel/qc/${encodeURIComponent(runId)}`) as Promise<QcListing>;
}

/** `POST /qc/{run_id}/run` -- the step the apply gate demands first. */
export function runQc(runId: string, body: { kind?: "audio" | "visual"; max_removal_ratio?: number } = {}): Promise<QcResult> {
  return wsApi.post(`/media-intel/qc/${encodeURIComponent(runId)}/run`, body) as Promise<QcResult>;
}

/** `POST /qc/{run_id}/override` -- who + why, both required server-side. */
export function recordQcOverride(
  runId: string,
  body: { kind?: string; reason: string; checks?: string[] }
): Promise<QcResult & { apply_decision: QcApplyDecision }> {
  return wsApi.post(`/media-intel/qc/${encodeURIComponent(runId)}/override`, body) as Promise<any>;
}

/* ------------------------------------------------------------------ *
 * faces / masks / active speaker (api/v1/media_intel_*.py)
 * ------------------------------------------------------------------ */

export type FaceTrack = {
  id: string;
  run_id: string;
  track_id: string;
  start_s: number;
  end_s: number;
  sample_count: number;
  confidence_max: number | null;
  truncated: boolean;
  reentry_count: number;
  samples?: { t_s: number; x: number; y: number; w: number; h: number; confidence: number | null }[];
  unresolved_crossing?: boolean;
};

export function trackFaces(
  assetId: string,
  opts: { sample_fps?: number; force?: boolean } = {}
): Promise<{ run: IntelRun; items: FaceTrack[]; cache_hit: boolean; reason?: string }> {
  return wsApi.post("/media-intel/face-tracks", {
    asset_id: assetId,
    sample_fps: opts.sample_fps ?? 2,
    force: Boolean(opts.force),
  }) as Promise<any>;
}

export function getFaceTracks(runId: string): Promise<{ run: IntelRun; items: FaceTrack[] }> {
  return wsApi.get(`/media-intel/face-tracks/${encodeURIComponent(runId)}`) as Promise<any>;
}

export type MaskItem = {
  id: string;
  run_id: string;
  kind: string;
  format: string;
  width: number | null;
  height: number | null;
  area_ratio: number | null;
  provider_key?: string;
  model_version?: string;
  mask_asset_id?: string | null;
  [k: string]: unknown;
};

export function createMasks(
  assetId: string,
  opts: { labels?: string[]; force?: boolean } = {}
): Promise<{
  run: IntelRun;
  cache_hit: boolean;
  items: MaskItem[];
  unavailable?: boolean;
  reason?: string;
  provider_reasons?: Record<string, string>;
  warnings?: string[];
}> {
  return wsApi.post("/media-intel/masks", {
    asset_id: assetId,
    labels: opts.labels ?? ["PERSON"],
    force: Boolean(opts.force),
  }) as Promise<any>;
}

export type ActiveSpeakerRow = {
  id: string;
  run_id: string;
  workspace_id: string;
  asset_id: string;
  /** anonymous `SPEAKER_00`-style label, or null when the row is UNRESOLVED */
  speaker_id: string | null;
  face_track_id: string | null;
  start_s: number;
  end_s: number;
  confidence: number | null;
  status: "RESOLVED" | "UNRESOLVED";
  reason: string;
};

export function mapActiveSpeaker(
  assetId: string,
  opts: { diarization_run_id?: string; face_run_id?: string; use_motion?: boolean; force?: boolean } = {}
): Promise<{
  run: IntelRun;
  cache_hit: boolean;
  items: ActiveSpeakerRow[];
  evidence_sources?: string[];
  warnings?: string[];
  thresholds?: Record<string, number>;
}> {
  return wsApi.post("/media-intel/active-speaker", {
    asset_id: assetId,
    diarization_run_id: opts.diarization_run_id ?? null,
    face_run_id: opts.face_run_id ?? null,
    use_motion: Boolean(opts.use_motion),
    force: Boolean(opts.force),
  }) as Promise<any>;
}

export function getActiveSpeaker(runId: string): Promise<{
  run: IntelRun;
  items: ActiveSpeakerRow[];
  resolved: number;
  unresolved: number;
}> {
  return wsApi.get(`/media-intel/active-speaker/${encodeURIComponent(runId)}`) as Promise<any>;
}

/* ------------------------------------------------------------------ *
 * reframe / keyframes / background (api/v1/media_intel_reframe.py)
 * ------------------------------------------------------------------ */

export type ReframeKeyframe = {
  id: string;
  plan_id: string;
  workspace_id: string;
  t_s: number;
  x: number;
  y: number;
  scale: number;
  rect: Record<string, number | boolean | string>;
  confidence: number | null;
  reason: string;
  source: string;
  transition: boolean;
  operator_edited: boolean;
  created_at: string | null;
  updated_at: string | null;
};

export type ReframePlan = {
  id: string;
  workspace_id: string;
  run_id: string | null;
  source_asset_id: string;
  layout: string;
  aspect: string;
  strategy: string;
  meta: Record<string, any>;
  geometry: Record<string, any>;
  jitter: Record<string, any>;
  slots: Record<string, any>[];
  ops: Record<string, any>[];
  editable: boolean;
  keyframe_count: number;
  keyframes: ReframeKeyframe[];
  created_at: string | null;
  updated_at: string | null;
  preview?: Record<string, any> | null;
  baked?: boolean;
};

/**
 * `POST /reframe` -- the plan is stored as EDITABLE keyframes, never baked into
 * the media, so this call mutates no timeline and needs no QC verdict.
 */
export function planReframe(body: {
  asset_id: string;
  aspect: string;
  layout: string;
  evidence_run_id?: string | null;
  participants?: number | null;
  grid_rows?: number | null;
  tracks?: string[];
  preview?: boolean;
}): Promise<ReframePlan> {
  return wsApi.post("/media-intel/reframe", {
    ...body,
    tracks: body.tracks ?? [],
  }) as Promise<ReframePlan>;
}

export function getReframe(planId: string): Promise<ReframePlan> {
  return wsApi.get(`/media-intel/reframe/${encodeURIComponent(planId)}`) as Promise<ReframePlan>;
}

/** `PATCH /reframe/keyframes/{id}` -- the previous values land in history. */
export function patchKeyframe(
  keyframeId: string,
  patch: { t_s?: number; x?: number; y?: number; scale?: number; confidence?: number; reason?: string; rect?: Record<string, unknown> }
): Promise<ReframeKeyframe & { history_entry: { before: Record<string, unknown>; after: Record<string, unknown>; by?: string } }> {
  return wsApi.patch(`/media-intel/reframe/keyframes/${encodeURIComponent(keyframeId)}`, patch) as Promise<any>;
}

export type BackgroundResult = {
  status: "COMPLETED" | "UNAVAILABLE";
  operation: string;
  rendered: boolean;
  reason: string;
  source_unchanged?: boolean;
  output_asset_id?: string | null;
  mask?: Record<string, any>;
  required?: Record<string, any>;
};

export function runBackground(body: {
  asset_id: string;
  operation: string;
  mask_kind?: string;
  mask_asset_id?: string | null;
  background_asset_id?: string | null;
  blur_strength?: number;
  aspect?: string;
}): Promise<BackgroundResult> {
  return wsApi.post("/media-intel/background", body) as Promise<BackgroundResult>;
}

/* ------------------------------------------------------------------ *
 * error parsing
 * ------------------------------------------------------------------ */

export type ApiErrorLike = { status: number; message: string };

/** `engine/intel/qc.py::QCApplyBlocked.as_dict()` -- the 409 of /proposals/apply. */
export type QcBlockedDetail = {
  verdict: string;
  run_id: string;
  result_id: string;
  kind: string;
  failures: string[];
  needs_override: boolean;
  detail: string;
};

/** `api/v1/timelines.py` -- the Work 02 stale-version 409. */
export type VersionConflictDetail = {
  error: string;
  expected_version: number;
  actual_version: number;
};

export type IntelError = {
  status: number;
  /** the server's own words, never a rewritten summary */
  message: string;
  kind: "qc_blocked" | "version_conflict" | "plain";
  qc: QcBlockedDetail | null;
  conflict: VersionConflictDetail | null;
};

/**
 * `lib/api.ts` stringifies a dict `detail` into `ApiError.message` (api.ts:81),
 * so a 409 arrives as JSON text. Parse it back -- the same idea as
 * `Editor.tsx::parseConflict` / `Reviews.tsx::conflictToast` -- and keep the
 * server's own sentence in `message` either way. A plain-string detail falls
 * through untouched; nothing is ever swallowed or replaced with a guess.
 */
export function parseIntelError(e: unknown): IntelError {
  const status = Number((e as ApiErrorLike)?.status ?? 0);
  const raw = String((e as ApiErrorLike)?.message ?? e ?? "request failed");
  let parsed: any = null;
  try {
    parsed = JSON.parse(raw);
  } catch {
    parsed = null;
  }
  if (parsed && typeof parsed === "object" && typeof parsed.verdict === "string" && "needs_override" in parsed) {
    return {
      status,
      message: String(parsed.detail ?? raw),
      kind: "qc_blocked",
      qc: parsed as QcBlockedDetail,
      conflict: null,
    };
  }
  if (parsed && typeof parsed === "object" && typeof parsed.expected_version === "number") {
    return {
      status,
      message: String(parsed.error ?? raw),
      kind: "version_conflict",
      qc: null,
      conflict: parsed as VersionConflictDetail,
    };
  }
  return { status, message: raw, kind: "plain", qc: null, conflict: null };
}

/** The plainest possible message for a 404 that means "nothing yet". */
export function isMissing(e: unknown): boolean {
  return Number((e as ApiErrorLike)?.status ?? 0) === 404;
}

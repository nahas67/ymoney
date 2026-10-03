# YMONEY Work 12 contracts — Advanced Audio + Visual Intelligence

Normative spec for Work 12. This file is the lock: lanes build to it, audits
compare against it, deviations are recorded in §21 (not silently absorbed).

Baseline: Work 11 verified — 1296 tests (1283 fast / 13 slow), 0 failed,
frontend build green, `origin/main` = `212d409`.

## 0. Scope / non-goals

In scope: provider abstractions for media intelligence, word alignment +
diarization, anonymous speaker identity + operator aliases, audio enhancement
pipeline, noise-reduction adapter isolation, silence/filler proposals,
timeline integration via canonical Work 02 operations, face + multi-face
tracking, segmentation masks, active-speaker mapping, smart reframing +
multi-speaker layouts, background tools, processing manifests, editor UI,
caching/resumability/GPU concurrency, QC.

Out of scope (never build): rebuild of timeline, editor shell, rendering,
localization/dubbing, avatars, campaigns, BrandDNA, memory, collaboration,
export center. Real-identity face recognition is forbidden. Installing heavy
ML packages into the app venv is forbidden (see §1.3). Work 13 is NOT
executed in this effort.

Invariants that must hold everywhere:

- **Derived only.** Every intelligence operation writes NEW derived assets.
  Originals are never overwritten, never mutated in place, never re-encoded
  destructively in the source file.
- **Honest unavailability.** No model/provider installed ⇒ the capability
  reports `UNAVAILABLE` with a reason. Never fabricate speakers, faces, masks,
  word timings, or confidence.
- **No sensitive inference.** Speaker IDs are anonymous (`SPEAKER_00`). The
  system never infers or stores gender, race, age, identity, or any sensitive
  attribute. Operator labels are human input, stored separately.
- **Boot without models.** `from app.main import app` must succeed with zero
  ML packages installed. The CI venv is exactly that environment.
- **No new runtime dependencies.** stdlib + ffmpeg only. Optional heavy
  backends are imported lazily inside adapters and probed, never imported at
  module import time.

## 1. Architecture

### 1.1 Provider contract

`backend/app/engine/intel/providers/base.py` defines `MediaIntelProvider`:

```python
@dataclass(frozen=True)
class ResourceSpec:
    gpu: bool = False
    vram_mb: int = 0
    ram_mb: int = 0
    model_bytes: int = 0
    cpu_seconds_per_audio_minute: float = 0.0
    notes: str = ""

@dataclass(frozen=True)
class LicenseInfo:
    code_license: str              # SPDX id or "UNKNOWN"
    code_license_url: str
    model_license: str             # SPDX id, "SEE_MODEL_CARD", or "UNKNOWN"
    model_license_url: str = ""
    model_gated: bool = False
    commercial_use: str            # PERMITTED | REVIEW_REQUIRED | PROHIBITED | UNVERIFIED
    audited_on: str = ""           # ISO date of the license audit
    notes: str = ""

@dataclass(frozen=True)
class ProviderHealth:
    available: bool
    reason: str = ""               # required when unavailable
    version: str = ""
    mode: str = ""                 # "local" | "remote" | "subprocess"
    detail: dict

@dataclass(frozen=True)
class ProviderResult:
    ok: bool
    artifacts: dict                # kind -> {asset_id?, path?, payload}
    metrics: dict
    warnings: list[str]
    error: str = ""                # generic; never leaks internals
```

Every provider implements:

| Method | Contract |
|---|---|
| `health() -> ProviderHealth` | never raises, never imports a heavy model eagerly, returns a reason when unavailable |
| `capabilities() -> dict` | honest feature/limit map (formats, sample rates, max duration, stages, aspects) |
| `resource_requirements() -> ResourceSpec` | used for scheduling + cost |
| `license_info() -> LicenseInfo` | code AND model terms, per §1.4 |
| `run(request, *, progress, should_cancel, deadline)` | cooperative: calls `progress(0..1)`, polls `should_cancel()`, respects `deadline`; raises `ProviderCancelled` / `ProviderTimeout` / `ProviderUnavailable` |
| `cost(spec) -> dict` | GPU/CPU seconds + cost micros for the work performed |

Registry (`providers/registry.py`): ordered provider chains per capability
(`alignment`, `diarization`, `speech_activity`, `enhancement`, `denoise`,
`face_tracking`, `segmentation`, `active_speaker`, `reframe`). `resolve(kind)`
returns the first healthy provider or `None` with an aggregated reason.
Provider implementations live in `providers/impl/` (whisperx_alignment,
pyannote_diarization, ffmpeg_speech_activity, ffmpeg_enhancement,
rnnoise_denoise, mediapipe_faces, sam2_segmentation, motion_reframe).

### 1.2 Worker boundary

Heavy models run in workers, never inside request handlers. Engines call the
job layer (`services/jobs.py`, existing `enqueue/retry/cancel` + handlers) and
return run ids. Handlers are registered at import time (guarded). The
audio/visual engines are pure orchestration + measurement so they run
in-process in tests without a worker.

### 1.3 Optional backends

Adapters import heavy packages **inside `health()`/factory** only:

```python
def _load_backend(self):
    try:
        import mediapipe  # noqa: PLC0415
    except Exception as exc:            # pragma: no cover - env dependent
        raise ProviderUnavailable("mediapipe not installed") from exc
```

CI venv has none of them installed, so every gate run proves the
boot-without-models + honest-unavailable paths.

### 1.4 License discipline (code AND model)

- Each provider reports code license and **model** license separately.
- `commercial_use` ∈ `PERMITTED | REVIEW_REQUIRED | PROHIBITED | UNVERIFIED`.
- A workspace running in commercial mode (`settings.commercial_mode = true`)
  refuses providers whose `commercial_use != PERMITTED`, with the reason
  surfaced to the API/UI. Refusal is enforced in the registry, not per call.
- `docs/oss/MEDIA_INTEL_LICENSES.md` records the audit: component, code
  license + URL, model license + URL, gated?, commercial use, audited date,
  unverified items. Anything unverifiable stays `UNVERIFIED`/`REVIEW_REQUIRED`
  — never asserted as permissive.

Audit verdicts (2026-09-29, from that file) that adapters MUST encode — the
audit exists precisely because code license ≠ model license:

| Provider | Code | Model terms | `commercial_use` |
|---|---|---|---|
| `whisperx_alignment` | BSD-2-Clause | per-language aligner: `en` MIT; **`fr/de/es/it` CC BY-NC 4.0**; other HF aligners per-model; bundled VAD `pytorch_model.bin` **UNVERIFIED** | `REVIEW_REQUIRED`; **per-language map** hard-blocks NC languages |
| `pyannote_diarization` | MIT | 3.0/3.1 + segmentation-3.0 MIT but **gated**; embedding `wespeaker` **CC-BY-4.0** (attribution); `community-1` CC-BY-4.0 | `REVIEW_REQUIRED` |
| `mediapipe_faces` | Apache-2.0 | `.task`/`.tflite` bundles **UNVERIFIED** (Kaggle TF.js listing is secondary) | `REVIEW_REQUIRED` |
| `sam2_segmentation` | Apache-2.0 | Apache-2.0, ungated checkpoints | **`PERMITTED`** |
| `rnnoise_denoise` | BSD-3-Clause | weights by inclusion in `rnn_data.c` | `PERMITTED` (note) |
| `ffmpeg_*` local providers | LGPL core + build-dependent GPL | n/a | `REVIEW_REQUIRED` (external process; operator distribution decision) |

Rules added: `PROHIBITED` is blocked in **every** mode (a non-commercial term
cannot be laundered through a non-commercial mode); a `PROHIBITED` per-language
aligner must refuse that language specifically rather than the whole provider;
gated models degrade honestly without a token (no fabricated diarization).
OpenCV Haar cascades are deliberately NOT used — per-file Intel/contributor
licenses, not Apache-2.0.

## 2. Migration 0029 — `0029_media_intelligence.py`

Append-only after `0028_collaboration`. 15 tables, all Work 12, no edits to
earlier migrations.

| Table | Purpose | Key columns |
|---|---|---|
| `media_intel_runs` | run + processing manifest | `workspace_id, asset_id, kind, provider_key, model_version, params_json, params_hash, asset_checksum, status, progress, chunks_total, chunks_done, cancel_requested, started_at, finished_at, processing_ms, gpu_ms, cost_micros, warnings_json, metrics_json, error_code, output_asset_id, requested_by` |
| `media_intel_cache` | cache index | UNIQUE `(workspace_id, cache_key)`, `cache_key, run_id, asset_checksum, provider_key, model_version, params_hash` |
| `media_intel_chunks` | resumability | UNIQUE `(run_id, idx)`, `idx, start_s, end_s, input_checksum, status` |
| `diarization_segments` | speaker / speech-activity segments | `run_id, workspace_id, asset_id, speaker_id, kind(SPEAKER|SPEECH_ACTIVITY), start_s, end_s, confidence` |
| `media_intel_words` | word alignment | `run_id, workspace_id, asset_id, idx, word, start_s, end_s, speaker_id NULL, confidence NULL` |
| `speaker_aliases` | operator labels | `workspace_id, asset_id NULL, run_id NULL, speaker_id, label, created_by` |
| `face_tracks` | anonymous tracks | `run_id, workspace_id, asset_id, track_id, start_s, end_s, confidence_max, sample_count, truncated, reentry_count` |
| `face_track_samples` | per-sample boxes | `run_id, workspace_id, track_id, t_s, x, y, w, h, confidence, landmarks_json NULL` |
| `mask_assets` | mask file refs (never blobs) | `run_id, workspace_id, input_asset_id, kind, mask_asset_id, format, width, height, area_ratio, provider_key, model_version, checksum` |
| `active_speaker_map` | speaker↔face | `run_id, workspace_id, asset_id, speaker_id NULL, face_track_id NULL, start_s, end_s, confidence, status(RESOLVED|UNRESOLVED), reason` |
| `edit_proposals` | non-destructive cut proposals | `workspace_id, project_id, asset_id, run_id, kind(REMOVE_RANGE|SHORTEN_RANGE|KEEP), start_s, end_s, reason, confidence, status, decision, ops_json, decided_by, decided_at` |
| `audio_time_maps` | source↔edited mapping | `workspace_id, asset_id, policy_id, segments_json, created_by` |
| `reframe_plans` | layout plans | `run_id, workspace_id, source_asset_id, layout, aspect, strategy, meta_json` |
| `reframe_keyframes` | EDITABLE keyframes | `plan_id, workspace_id, t_s, x, y, scale, rect_json, confidence, reason, source` |
| `intel_qc_results` | QC verdicts | `workspace_id, run_id, kind(audio|visual), verdict, checks_json` |

Plus additive columns on `media_assets`: `parent_asset_id NULL`,
`derivation_json NULL` (lineage for derived assets). Additive only.

Status enum for runs: `PENDING | RUNNING | COMPLETED | FAILED | CANCELLED |
UNAVAILABLE`. `UNAVAILABLE` is a first-class terminal state (no provider).

## 3. Runs, cache, resumability, GPU concurrency

- `cache_key = sha256(asset_checksum | provider_key | model_version |
  canonical_params_json)`. On request: look up `media_intel_cache`; a hit
  returns the prior run with `cache_hit = true` and **no recompute**
  (`force=true` creates a new run instead).
- Chunked processing: `chunks_total` from a configurable chunk seconds
  (default 600). `media_intel_chunks` records per-chunk status +
  `input_checksum`. Resume re-runs only non-`COMPLETED` chunks whose checksum
  still matches; a changed chunk input invalidates that chunk only.
- `gpu_semaphore(limit)` — DB-backed slot counter
  (`settings.max_concurrent_gpu_jobs`, default 1): acquire/release around GPU
  work, per-workspace, with timeout. CPU-only providers bypass it.
- Cancellation: `cancel_requested` is polled by chunk loop and passed to
  providers via `should_cancel`; a cancelled run ends `CANCELLED` with partial
  chunk state intact for resume.
- Cost accounting: `gpu_ms`, `processing_ms`, `cost_micros` written per run
  using the existing costs service; exposed in ops/QC payloads.
- **Emission ordering (integration rule, from Lane A's first run):**
  `record_event` and `track_cost` each open their own `session_scope()`.
  Calling them mid-transaction makes the second connection hit
  `OperationalError` on the same SQLite file (observed: "MEDIA_INTEL_RUN_
  CREATED failed: OperationalError"). Engines must therefore surface
  emissions (returned list or injected `emit` callable) and the ROUTE emits
  after `db.commit()` — mirroring Work 11. Report every new event kind to the
  orchestrator for the `WEBHOOK_EVENTS` allowlist.

## 4. Word alignment + diarization

Pipeline: `audio → ASR (existing transcript pipeline) → word timestamps →
diarization → speaker segments → word↔speaker mapping`.

- `align_words(...)`: word rows only when the provider genuinely returns word
  timings. If only segment-level ASR exists, words are **not** invented —
  response reports `words_available=false, reason=...`.
- `map_words_to_speakers(...)`: assign `speaker_id` by maximal temporal
  overlap; words in gaps stay `NULL` (never nearest-guessed across a long gap).
- Diarization unavailable ⇒ `speaker_id` stays `NULL` and
  `speakers_resolved=false` + reason. Speech-activity segments
  (`kind=SPEECH_ACTIVITY`, `speaker_id=NULL`) may still be produced by the
  ffmpeg VAD adapter — they are **not** speakers.
- Adapters: `whisperx_alignment` (optional import), `pyannote_diarization`
  (optional import + token probe), `ffmpeg_speech_activity` (real, local,
  `silencedetect`/`astats`).

## 5. Speaker identity

- IDs are `SPEAKER_00`, `SPEAKER_01`, ... assigned per run by first
  appearance, stable within the run.
- Operator alias: `POST /media-intel/speakers/aliases {asset_id?, run_id?,
  speaker_id, label}` — label is human input ("Host"). Aliases are the ONLY
  name source; nothing infers a name.
- No sensitive inference: no gender/race/age/identity fields anywhere; API
  response shape test asserts the vocabulary; alias rows record `created_by`.
- Alias mapping is consumable by Work 07 dubbing (documented shape:
  `{speaker_id, label, segments: [{start_s, end_s}], words: [...]}`).

## 6. Audio enhancement pipeline

`AudioEnhancementPipeline` — every stage independently toggleable, ordered,
each reports `applied | skipped | unavailable(reason) | failed(reason)`:

| Stage | Method | Real/labels |
|---|---|---|
| `denoise` | `afftdn` (ffmpeg) or RNNoise adapter | real filter; method recorded |
| `dereverb` | adapter-gated | honest `UNAVAILABLE` without a provider |
| `voice_isolation` | band-pass (`highpass`+`lowpass`)+`afftdn` | label: `band_isolation` (NOT neural isolation) |
| `silence_detection` | `silencedetect` measurement | analysis, not a filter |
| `filler_detection` | transcript+PCM heuristics | see §7 |
| `breath_click_detection` | PCM window analysis (stdlib `wave`+`array`) | `method=heuristic_pcm`, confidence reported |
| `loudness_normalization` | `loudnorm` 2-pass (EBU R128) | real; LUFS before/after |
| `music_ducking` | `sidechaincompress` | real |
| `compression` | `acompressor` | real |
| `limiting` | `alimiter` (+ `loudnorm` TP) | real |

Output = **new** derived asset; original untouched. Manifest stored on the
run (§2 `media_intel_runs` + `media_assets.derivation_json`): provider,
model/version, input/output asset, params, processing time, GPU time,
warnings, quality metrics. Before/after metrics required: duration, peak
dBFS, LUFS, clipped-sample count, processing latency. No perceptual quality
claim without a measured metric — claims field must stay empty unless a
measured basis exists.

## 7. Silence + filler editing (non-destructive)

Detect: long silence/dead air, filler words (documented lexicon + stutter/
repeated-phrase patterns), repeated false starts.

- Output `edit_proposals` with `kind ∈ REMOVE_RANGE | SHORTEN_RANGE | KEEP`,
  `reason` code, `confidence`, `status=PROPOSED`. **Nothing is applied
  automatically** (`policy.auto_apply` default OFF).
- Policy: `min_silence_s`, `keep_padding_s`, `filler_policy`, `max_removal_ratio`
  — decides `decision ∈ keep|remove|shorten` per proposal; exceeding
  `max_removal_ratio` forces keep + QC `REVIEW_REQUIRED`.
- Time map: `build_time_map(policy_id)` → ordered segments
  `{src_start, src_end, out_start, out_end}` persisted in `audio_time_maps`;
  helpers `map_time(t)` / `map_range(a,b)` for captions, scenes, and UI.
- Apply = canonical Work 02 operations only (the existing
  `POST /timelines/{id}/operations` with `base_version`): cut audio clips and
  shift caption/scene clips that overlap removed ranges. Never a second
  audio-edit timeline.

## 8. Face tracking (+ multi-face)

- `face_tracks` = session-local anonymous tracks (`FT_00`…), never identities.
- Samples at configurable fps (default 2) with `x,y,w,h,confidence` and
  `landmarks_json` only when the provider supplies landmarks. Cap
  `max_samples_per_track` (default 2000) with `truncated` flag — honest.
- Association: IoU + size/center gating; stable IDs when reliable. Uncertain
  crossings create a NEW track and set `reentry_count`/`unresolved_crossing`
  rather than silently merging identities.
- Multi-face: 1, 2, 3+ participants, temporary exit/re-entry, overlapping
  detections — all covered by tests.
- Adapters: `mediapipe_faces` (optional import; injectable detector backend for
  deterministic tests), no silent OpenCV-free fallback pretending to detect.

## 9. Segmentation / object tracking

- `SegmentationProvider` (SAM2 adapter, optional import) + injectable backend.
- Masks are written as files under `STORAGE_ROOT` (PNG/RLE JSON) and
  referenced by `mask_assets.mask_asset_id` — **never** raw masks in DB rows.
- Uses: person/object masks, background replacement, background blur,
  subject-aware crop, tracked overlays. Original media unchanged.
- Provider unavailable ⇒ capability `UNAVAILABLE` with reason; no fallback
  that produces bad masks.

## 10. Active speaker mapping

`ActiveSpeakerMapper` combines diarization segments + face-track windows +
optional motion evidence (frame-difference energy via ffmpeg, labelled
`method=motion_energy`).

- Output rows: `{speaker_id?, face_track_id?, start_s, end_s, confidence,
  status}`. `status=RESOLVED` requires sufficient evidence (overlap ratio +
  optional motion support above thresholds).
- Insufficient/tied evidence ⇒ `UNRESOLVED` with `reason`
  (`no_face_track`, `ambiguous_tie`, `low_overlap`, `no_diarization`,
  `low_confidence`). **Never guess.** Silent guessing = FAILED.

## 11. Smart reframing + layouts

- `SmartReframeProvider` supports `16:9 → 9:16 | 4:5 | 1:1`.
- Priority: 1) active speaker (resolved only) → 2) important subject (salient
  face: area + centrality) → 3) configured focal point → 4) safe fallback crop
  (center crop inside a safe area). `strategy` recorded per keyframe source.
- Output = **editable** `reframe_keyframes` rows (t, x, y, scale, rect,
  confidence, reason, source). Crop is NOT baked into renders.
- Layouts produce rect sets (editable) for: `ACTIVE_SPEAKER, SPLIT_SCREEN,
  TWO_SHOT, GRID, HOST_GUEST, PODCAST_DYNAMIC`. Speaker A active → crop A;
  B begins → transition (keyframe ramp, `transition_s`), never a hard jump.
- Optional preview render (ffmpeg crop/pad/split) → derived asset, `slow` test.
- Jitter control: max movement/sec clamp + smoothing; violations surface in QC.

## 12. Background tools

Using masks when a provider is available: background blur, background replace,
person mask, object mask. Originals unchanged. Provider unavailable ⇒
`UNAVAILABLE` (no silent bad masks). Ops recorded as derived assets with
lineage.

## 13. QC

`AudioIntelligenceQC` / `VisualIntelligenceQC`; verdicts
`PASS | PASS_WITH_WARNINGS | REVIEW_REQUIRED | FAIL`.

Audio checks: duration drift, clipping (peak/true-peak), missing audio,
excessive removed speech (vs `max_removal_ratio`), transcript mismatch
(word-count/word-rate drift vs source transcript).

Visual checks: missing subject (face coverage below threshold), unstable crop
(jitter), face lost mid-track, mask failure (empty/tiny mask), excessive crop
movement, black frames (mean luma).

Rules: hard violations ⇒ `FAIL`; heuristic-only evidence or policy overrides ⇒
`REVIEW_REQUIRED`; minor ⇒ `PASS_WITH_WARNINGS`. Applying a `FAIL` plan
requires an explicit override capability; verdict + per-check detail stored in
`intel_qc_results`.

## 14. API surface (`/api/v1/media-intel/*`)

Providers: `GET /providers` (health/capabilities/license), `GET /providers/{key}`.
Alignment: `POST /alignments`, `GET /alignments/{run_id}`, `GET /words`.
Diarization: `POST /diarization`, `GET /diarization/{run_id}`.
Speakers: `GET /speakers`, `GET/POST/DELETE /speakers/aliases`.
Audio: `POST /audio/enhance`, `GET /audio/enhance/{run_id}`,
`POST /audio/silence`, `POST /audio/fillers`, `GET /proposals`,
`POST /proposals/{id}/decide`, `POST /proposals/apply`, `GET /time-map`.
Visual: `POST /face-tracks`, `GET /face-tracks/{run_id}`, `POST /masks`,
`GET /masks/{run_id}`, `POST /active-speaker`, `GET /active-speaker/{run_id}`,
`POST /reframe`, `GET /reframe/{plan_id}`, `PATCH /reframe/keyframes/{id}`,
`POST /background`.
QC: `GET /qc/{run_id}`.
Runs: `GET /runs`, `GET /runs/{id}`, `POST /runs/{id}/cancel`, `POST /runs/{id}/retry`.

Conventions: workspace scoping on every route (foreign id → **404**, never
403), RBAC via existing `assert_capability` (read → view, mutate → edit,
override QC → manage), `{items: [...]}` envelopes, generic 500s, job kinds
registered at import: `MEDIA_INTEL_ALIGN, MEDIA_INTEL_DIARIZE,
MEDIA_INTEL_ENHANCE, MEDIA_INTEL_SILENCE, MEDIA_INTEL_FILLERS,
MEDIA_INTEL_FACE_TRACK, MEDIA_INTEL_SEGMENT, MEDIA_INTEL_ACTIVE_SPEAKER,
MEDIA_INTEL_REFRAME, MEDIA_INTEL_BACKGROUND`.

## 15. Frontend (Editor)

Audio Intelligence panel: Enhance Voice, Remove Noise, Detect Silence, Detect
Fillers, Normalize, proposal preview (list with KEEP/REMOVE/SHORTEN, reasons,
confidence, source↔edited mapping), Apply (uses canonical ops), Undo.
Visual Intelligence panel: Track Subject, Auto Reframe, Speaker Layout,
Background Blur, Background Replace; keyframe editor for plans; layout preview.

Rules: proposed changes are previewed before any timeline mutation; every
action shows provider/model/license provenance + cache hit + QC verdict;
read-only states when a provider is unavailable (never a fake success);
undo/versioning through the existing timeline save/operations flow.

## 16. Test matrix (§19 of the work order → files)

| Required test | File | Lane |
|---|---|---|
| word alignment | test_speech_alignment.py | B |
| diarization | test_speech_alignment.py | B |
| speaker mapping | test_speech_alignment.py | B |
| no sensitive inference | test_speech_alignment.py | B |
| provider unavailable | test_media_intel_providers.py | A |
| audio derived-asset lineage | test_audio_enhancement.py | C |
| denoise adapter | test_audio_enhancement.py | C |
| silence detection | test_silence_fillers.py | D |
| filler proposals | test_silence_fillers.py | D |
| timeline sync after cuts | test_silence_fillers.py | D |
| face tracking | test_face_tracking.py | E |
| multi-face tracking | test_face_tracking.py | E |
| active-speaker mapping | test_active_speaker.py | F |
| unresolved speaker state | test_active_speaker.py | F |
| segmentation masks | test_segmentation.py | F |
| smart 9:16 crop | test_reframe_layouts.py | G |
| speaker switching | test_reframe_layouts.py | G |
| split-screen layout | test_reframe_layouts.py | G |
| cache reuse | test_media_intel_providers.py | A |
| worker cancellation | test_media_intel_runs.py | H |
| GPU concurrency | test_media_intel_runs.py | H |
| workspace isolation | test_media_intel_api.py | A/H |
| QC failures | test_intel_qc.py | H |
| editor operation compatibility | test_silence_fillers.py + test_media_intel_api.py | D/H |
| Work 07 dubbing consumes mapping | test_speech_alignment.py | B |
| license metadata honesty | test_media_intel_providers.py | A |
| boot without ML packages | test_media_intel_providers.py | A |

`@pytest.mark.slow` = real ffmpeg media (real lavfi audio/video, real
loudnorm/silencedetect passes, real preview render).
`@pytest.mark.live` = requires real model backends (skipped in CI).

## 17. Gates

1. Per-lane batteries standalone.
2. Fast `-m "not slow"`; slow `-m slow`; full ruff `F,I,SIM,UP` (19 baseline,
   zero in Work 12 files); `scripts/gen_postman.py` + `test_postman`;
   `npm run build`; fresh migration replay (29 applied / REPLAY_NOOP) +
   hygiene; app import + OpenAPI probe (330 + new paths); dependency audit
   (no new runtime deps); `docs/oss/MEDIA_INTEL_LICENSES.md` complete with no
   silent `UNVERIFIED`→permitted promotions.

## 21. Wave status + deltas (newest first)

- **2026-09-30 W12 CLOSED** — all 9 lanes verified and integrated. Gates:
  fast **1687 passed / 0 failed** (378s), slow **67 passed / 0 failed**
  (300s) = **1754 total, 0 failed** (+458 vs Work 11's 1296); fresh replay
  **29 applied / REPLAY_NOOP**; ruff `F,I,SIM,UP` = 19 repo baseline / **0 in
  Work 12 files**; Postman **60 folders / 414 requests** + drift test;
  `npm run build` **exit 0**; OpenAPI **365 paths** (+35).
  Work 12 test inventory: **447 test functions** across 11 new files
  (providers 29, runs 36, speech 60, audio 63, silence/fillers 56, faces 40,
  QC 42, segmentation 27, active-speaker 28, reframe/layouts 48, fixtures 17).
  New code: `engine/intel` 23 files / 16 670 lines, migration `0029` 423
  lines, 15 tables, 9 routers.
- **2026-09-30 two real cross-lane defects found at integration and fixed by
  the orchestrator** (both would have silently corrupted QC verdicts):
  1. *Unit mismatch* — `qc.subject_coverage` intersected Lane E's **pixel**
     face boxes against a **normalized** crop rect, so a plan that visibly
     contains its subject scored `coverage=0.0` → spurious `missing_subject`
     **HARD FAIL**. Fixed: the sample unit is auto-detected and converted with
     the `source_width`/`source_height` already read off `plan.meta_json`; with
     no frame size QC reports **no evidence** rather than a fabricated 0.0.
     Lane H's own test fixture was also wrong (normalized samples against a
     declared 1920x1080 frame) and was corrected to real pixels.
  2. *Wrong aspect fallback* — `crop_rect_at` used
     `h = w * (target_w/target_h) * (src_w/src_h)`, which collapses to `h = w`
     and yields a 160x90 px crop (the source's own 16:9 shape) where a 9:16
     crop is 160x180. Corrected to the real normalized relation
     `h = w * (src_w/src_h) / (target_w/target_h)`, clamping the height at
     full frame so `width = 1/scale` stays authoritative (`scale=1.0` still
     means the whole frame). A first attempt that shrank the width to preserve
     aspect exactly was walked back: it silently redefined `scale=1.0`.
  Three stale test expectations that encoded the old geometry were updated
  with their derivations, and the "known deviation" test now asserts the
  **fixed** behaviour in both unit directions.

- **2026-09-30 cross-lane contracts found at integration** (each was implicit
  and would have surfaced as a silent wrong-value bug):
  1. *Emission ordering* — see §3.
  2. *Keyframe anchor* — QC reads `reframe_keyframes.x/.y` as the **crop
     centre** (`scale` = zoom, `rect_json` preferred), not top-left; relayed
     to lane G mid-flight.
  3. *Face-sample geometry* — `face_track_samples` are in raw **frame pixel
     space**, never normalised; `unresolved_crossing` exists only in
     `runs.metrics_json`, never as a column. Relayed to lanes F and G.
  4. *Availability* — lane A's blanket "nothing available in CI" assertions
     were amended once real ffmpeg providers landed: model-backed providers
     must be unavailable **with a reason**; local ffmpeg providers may
     resolve.
  5. *Apply path* — FE does not replay previewed ops through `commitOps`: the
     backend's `POST /proposals/apply` is the only QC-gated writer and refuses
     submitted preview ops, so the UI adopts the server doc and pushes the
     same undo entry `commitOps` would.
- **2026-09-30 accepted deviations (orchestrator acked)**: lane D's apply
  gate is **strict** — no QC verdict means 409, and `override=true` without a
  *recorded* override is still 409, so the flow is
  `detect → decide → QC → apply`. Lane F surfaces crossing-flagged tracks as
  **provenance only** (no status or confidence penalty) because no contract
  threshold exists — inventing one is the fabricated rule the module exists to
  prevent.

- **2026-09-30 W12 lanes verified (7 of 9 landed + integrated)**: A
  foundation 70 fast + 1 slow · FIXTURES 12 + 5 · B speech 59 + 1 · C audio
  51 + 12 · D silence/fillers 52 + 4 · E faces 31 + 9 · F segmentation +
  active-speaker 51 + 4 · H QC 35 + 7 · FE build exit 0. Combined in one
  process: **390 passed, 0 failed** (the flake lane B saw was its own run
  racing F/G writes, not order-sensitivity).
- **Orchestrator integration**: 9 routers mounted (**365 OpenAPI paths**,
  +35 media-intel), 15 `MEDIA_INTEL_*` webhook kinds whitelisted, settings
  `max_concurrent_gpu_jobs=1` / `commercial_mode=False`, a `main.py`
  idempotent job-registration seam that never blocks boot, Postman
  regenerated (**60 folders / 414 requests**, drift test green), ruff clean.
- **2026-09-29 environment facts locked in** (CI = honest-unavailable proof):
  venv has **zero ML packages** (no numpy/torch/cv2/mediapipe/pyannote/
  whisperx) and no pip module; ffmpeg 8.1.1 present. Filter availability:
  `afftdn`, `arnndn`, `anlmdn`, `loudnorm`, `ebur128`, `volumedetect`,
  `astats`, `silencedetect`, `sidechaincompress`, `acompressor`, `alimiter`,
  `highpass`, `lowpass`, `deesser`, `dialoguenhance`, `superequalizer`,
  `firequalizer` present; **`rnnoise` ABSENT** (so the denoise adapter's
  honest-unavailable path is exercised for real in CI). Real-media spike
  verified: `silencedetect` parses `silence_start: 3` / `silence_end:
  5.000021 | silence_duration: 2.000021`; `ebur128` summary yields I/LRA/Peak;
  `astats` yields peak dB + sample count.
- **2026-09-29 integration contracts resolved from the repo survey**
  (deviations from my §2/§7 sketch, recorded here as the lock):
  * Derived-asset lineage = new additive `media_assets.parent_asset_id` +
    `derivation_json` columns **plus** the existing `meta_json` lineage
    convention (14 existing sites) — both written.
  * Provider layer mirrors the two existing precedents: ABC + frozen
    dataclasses from `engine/intelligence/providers/base.py`, fail-closed
    `Health(available, remediation)` + never-raising `build_provider()` from
    `engine/lipsync/{base,factory}.py`; GPU slot semaphore mirrors
    `engine/lipsync/worker.py`'s VRAM semaphore.
  * `providers/clips.py` already owns ASR (faster-whisper tiny.en) + face
    tracking (mediapipe) + a 1-face x-center reframe. Work 12 **promotes**
    that logic into adapters; lane E owns that file to avoid duplication.
  * Timeline mutations ride canonical Work 02 ops only:
    `engine/timeline_ops.OP_TYPES` = add_item, delete_item, move_item,
    trim_item, split_item, duplicate_item, move_to_track, update_transform,
    update_volume, update_speed, update_text, update_caption — cuts become
    split_item+delete_item+move_item; reframe plans become update_transform.
    No second audio-edit timeline.
  * Work 07 dubbing bridge: `engine/dubbing/plan.py::build_plan(
    cues_with_speakers, target_lang, voice_map, glossary, ...)` returns
    `DubbingPlan{speakers: [SpeakerPlan(speaker_id, target_voice, ...)],
    segments: [SegmentPlan(index, speaker_id, start, end, text, ...)]}`;
    `DEFAULT_SINGLE_SPEAKER="speaker_1"`. The speaker mapping must be
    convertible into `cues_with_speakers` (Cue has `speaker: str`).
  * New event kinds must be whitelisted in `services/webhooks.WEBHOOK_EVENTS`
    or they are silently dropped; orchestrator applies lane-reported kinds.
  * New job handlers need an explicit import side effect in `main.py`
    (`services/jobs.register_handler` raises on duplicate types) —
    orchestrator owns `api/v1/__init__.py` + `main.py` + `webhooks.py` edits.
- **2026-09-29 W12 started** (this file). Baseline Work 11 verified: 1296
  tests / 0 failed. Deltas: none above.

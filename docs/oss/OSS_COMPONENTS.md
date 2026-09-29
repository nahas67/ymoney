# YMONEY OSS Components Inventory

Required by the Master OSS Work Plan OSS Repository Rule. Every row records repository, purpose, pinned version, code license, model license, integration method, runtime/GPU needs, env vars, limitations, replacement strategy.

## In-use adapters (vendored code: none)

| Component | Repository | Purpose | Pinned version | Code license | Model license | Integration | Modified | Runtime | GPU | Env vars | Limitations | Replacement |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| Video engine (local) | n/a (own FFmpeg pipeline; techniques adapted from MoneyPrinterTurbo, not vendored) | short render | n/a | own | n/a | `providers/video_engine/ffmpeg_avatar.py` | own code | ffmpeg + python | no | `VIDEO_ENGINE=ffmpeg_avatar` | single-track assembly, no editor | swap via `BaseVideoEngine` |
| Video engine (service) | `harry0703/MoneyPrinterTurbo` (HTTP API only) | remote render | server-side, unpinned — PIN BEFORE PROD | check server deployment's license | server-side | `providers/video_engine/mpt.py` HTTP adapter | unmodified (no vendored code; `MoneyPrinterTurbo/` worktree copy deleted) | reachable HTTP base URL | server-side | `MPT_BASE_URL`, `MPT_TIMEOUT` | no local inference; listing best-effort | any `BaseVideoEngine` impl |
| Trend sources | various public APIs/RSS/JSON | opportunity discovery | live APIs | API ToS each | n/a | `providers/trends/` registry | own code | outbound HTTPS | no | per-source keys | rate limits; Reddit/HN shape drift | registry add/remove |
| Publishers | YouTube Data API v3, TikTok Content Posting API, Meta Graph (FB/IG), Upload-Post relay | distribution | official APIs | API ToS | n/a | `providers/publishers/` + `factory.py` | own code | outbound HTTPS | no | OAuth tokens via `ApiCredential` (encrypted) | per-platform quotas/review | provider adapter swap |
| Analytics | YouTube Analytics API, TikTok Display, Meta Graph | metrics | official APIs | API ToS | n/a | `providers/analytics/` | own code | outbound HTTPS | no | OAuth/API keys | token scopes; sampling | provider adapter swap |
| TTS | edge-tts (own), ElevenLabs API | narration | `edge-tts` pinned in `uv.lock`; ElevenLabs REST | check `edge-tts` license before commercial scale | n/a (voices are service-side) | `providers/tts.py` | unmodified libs | outbound HTTPS | no | `ELEVENLABS_API_KEY` / settings key | ElevenLabs is paid per char | any TTS provider impl |
| Images | Pexels API + generative fallback | thumbnails/B-roll stills | official API | Pexels license (attribution appreciated, not required) | n/a | `providers/images*.py` | own code | outbound HTTPS | no | `PEXELS_API_KEY` | stock relevance varies | any image provider impl |
| LLM | configurable OpenAI-compatible endpoint | research/strategy/script/SEO/decision assist | endpoint-side | endpoint ToS | n/a | `providers/llm.py` | own code | outbound HTTPS | no | `LLM_*` settings | cost/latency; mock in tests only | any LLM endpoint |
| Transcription | faster-whisper/whisper (local) + yt-dlp ingest | repurpose pipeline | pinned in `uv.lock` | check model weights license independently | **weights license must be recorded here before prod** | `providers/clips.py`, agents/repurpose | own code | python + ffmpeg | optional | — | word-timestamp accuracy varies | WhisperX adapter (Phase 4) |

## Wan2.2 video generation (evaluated 2026-09-24, NOT integrated)

Repository:
https://github.com/Wan-Video/Wan2.2

Purpose (future):
Short generated B-roll / atmospheric inserts / image-to-video establishing
shots — never full-video diffusion (spec rule).

Evaluation:
- Code license: Apache-2.0 (repo LICENSE.txt, verified via GitHub page).
- Model weights license: Apache 2.0 (README "License Agreement" section).
- GPU floor: TI2V-5B needs 24GB VRAM (RTX 4090 class); 14B needs 80GB.
  No GPU exists in this dev/CI environment — benchmarking impossible here.
- Verdict: license-clean but NOT integrated. Integration requires the GPU
  worker architecture (isolated workers, VRAM accounting, cancellation,
  CPU/API fallback) from a later phase. The `ai_video` asset kind already
  raises a descriptive error pointing here, so the fallback chain is honest.

Pinned version/commit:
none (not integrated - do not add before GPU workers exist).

Used directly in core domain:
No.

Replacement strategy:
Remote video-generation API or a smaller local model behind the same
`VisualAssetProvider` interface.

## Pending evaluation (DO NOT integrate before the 10-step rule)

For each: inspect README → LICENSE → release/commit → maintenance → advisories → model license → isolated prototype → benchmark → interface → pin + document.

## OpenTimelineIO

Repository:
https://github.com/AcademySoftwareFoundation/OpenTimelineIO

Purpose:
Timeline interchange and editorial model adapter.

Integration:
Adapter only (`backend/app/engine/otio_adapter.py` — the sole module importing
opentimelineio; `backend/app/engine/timeline.py` stays dependency-free).

Pinned version/commit:
opentimelineio==0.18.1 (installed 2026-09-23 via uv; added to
`backend/pyproject.toml` dependencies).

License:
Apache-2.0 (verified in installed package metadata).

Used directly in core domain:
No.

Replacement strategy:
YMONEY canonical Timeline remains independent.

## wavesurfer.js

Repository:
https://github.com/katspaugh/wavesurfer.js

Purpose:
Waveform visualization for narration/music/SFX in the browser editor
(editor presentation only — never stored in domain models).

Pinned version/commit:
wavesurfer.js@7.12.12 (`frontend/package.json`).

License:
BSD-3-Clause (verified via `npm view`).

Used directly in core domain:
No — `frontend/src/pages/Editor.tsx` waveform panel only.

Replacement strategy:
Any waveform renderer behind the same panel; backend unaffected.

## React Timeline Editor (evaluated, NOT integrated)

Repository:
https://github.com/xzdarcy/react-timeline-editor

Evaluation (2026-09-23):
package is not published on the npm registry (404, unpublished 2022);
installing from git would bypass version pinning and audit. Combined with the
domain-model risk (its internal state would need a lossy two-way mapping),
the decision is a purpose-built editor bound 1:1 to the canonical Timeline
via `frontend/src/editor/adapters/timelineAdapter.ts`.

License:
Unverified (no registry metadata to check) — another reason not to integrate.

Replacement strategy:
Re-evaluate only if a maintained, pinned, license-clean release appears;
the adapter boundary (`timelineAdapter.ts`) is where it would bind.

- [ ] `xzdarcy/react-timeline-editor` — timeline UI foundation only; internal state must map to canonical Timeline, never become the backend format. License: verify (MIT believed — confirm).
- [ ] `katspaugh/wavesurfer.js` — audio waveform/regions in editor. License: BSD-3-Clause (verify).
- [ ] `ffmpegwasm/ffmpeg.wasm` — client preview/trim/proxy only; never authoritative render. License: verify (MIT believed — confirm).
- [ ] `m-bain/whisperX` — word timestamps + alignment. License: verify + model weights licenses separately.
- [ ] `pyannote/pyannote-audio` — diarization. License: MIT for code but **pretrained pipelines have their own terms** — verify before prod.
- [ ] `xiph/rnnoise` — noise suppression. License: BSD-3-Clause (verify).
- [ ] `google-ai-edge/mediapipe` — face/pose/landmarks. License: Apache-2.0 (verify); bundled models each carry terms — record.
- [ ] `facebookresearch/sam2` — segmentation. License: Apache-2.0 (verify); checkpoints separate — record.
- [x] `Wan-Video/Wan2.2` — EVALUATED 2026-09-24 (see record above): Apache-2.0 code + weights, but no GPU here; NOT integrated until GPU workers exist.
- [ ] `browser-use/jev-ultrafast` (verified EXISTS — see table above) — read-only browser research fallback. Requires Jev credentials; never for platform login where an API exists; never for purchases/publishing.
- [ ] `jkudish/jev-mcp` (verified EXISTS, MIT — see table above) vs `itsmostafa/typesafe-mcp` — evaluate both, choose ONE unless clearly distinct needs. Dev-tooling; backend uses native `DecisionEngine` interface.

## Claude decision tooling (evaluated 2026-09-25, NOT integrated — Work 05 Lane A)

READ-ONLY evaluation via webfetch of the repos (no dependencies added, no
code vendored). The backend keeps the native `DecisionEngine` interface
(`backend/app/engine/intelligence/`); remote MCP paths stay behind the
credential-gated `ClaudeDecisionProvider` stub, which reports UNAVAILABLE
until credentials exist and never fails startup.

| Repository | Purpose | Verification | Verdict |
|---|---|---|---|
| `browser-use/jev-ultrafast` | read-only browser research fallback | 2026-09-25: repo EXISTS and is active (Jev picks operation+target per cycle, small LLM for text; demo needs `TYPESAFE_API_KEY` + text-model key). NOTE: the slug `browser-use/claude-ultrafast` 404s — that name does not exist; the correct repo is `browser-use/jev-ultrafast` | NOT integrated — Lane C's browser agent uses a provider-neutral driver interface; jev-ultrafast stays an optional future real-driver candidate, never for platform login where an API exists |
| `jkudish/jev-mcp` | MCP bridge: 11 typed Jev judgment tools (verify/screen/noul/find/rerank/classify/decide/compare/extract/review/gate) | 2026-09-25: repo EXISTS and is active (requires Node 22+, `TYPESAFE_API_KEY` or OpenRouter/Cloudflare/Vercer gateway key); **License: MIT** (stated in repo). NOTE: the slug `jkudish/claude-mcp` 404s — that name does not exist; the correct repo is `jkudish/jev-mcp` | NOT integrated — closest fit to DecisionEngine primitives, but needs API key + isolated prototype per the 10-step rule; re-evaluate only then, and choose ONE MCP bridge |
| `itsmostafa/typesafe-mcp` | TypeSafe Jev MCP: typed boolean/choice/score judgments with probabilities for agents | 2026-09-25: repo EXISTS and is active (install via `install.sh`, `TYPESAFE_API_KEY` required, `evaluate setup mcp` wires Claude Code/Desktop/Codex; license NOT verified — check repo LICENSE before any use) | NOT integrated — closest fit to DecisionEngine primitives, but needs API key + isolated prototype + license check per the 10-step rule; re-evaluate only then, and choose ONE MCP bridge |

Replacement strategy: deterministic + local providers cover all decision
primitives offline; the LLM provider covers remote assist. Claude/MCP
remains optional future tooling, never a runtime dependency.
- [x] `TMElyralab/MuseTalk` — EVALUATED 2026-09-28 (Work 07 Lane B — see full record below): MIT code + MIT model weights (HF `license:mit`, commercial OK) — NOT blocked for production; adapter ships behind config, fail-closed without a GPU.
- [x] `KlingAIResearch/LivePortrait` — evaluated 2026-09-28 (Work 07): code MIT, but InsightFace detection MODELS are non-commercial-research-only → BLOCKED-for-commercial-production until the detector is replaced (MediaPipe/OpenCV path); full record at the end of this file. Never deployed.
- [ ] `apache/age` — OPTIONAL graph evaluation only, behind `KnowledgeGraphProvider`. Postgres stays source of truth unless benchmarks justify change.
- [ ] Dev-only (never runtime deps): `qkal/Canny`, `ellipsis-dev/blink`, `tamaratran/fast-jev-compaction`, `GhalebDweikat/winnow`, `devagrawal09/jev-review`, `0xNatoshi/jev-codex-router` (only if Codex env + measurable win).

## Context-budget + Claude provider tooling (evaluated 2026-09-25, concepts adopted — Work 05 Lane B)

READ-ONLY evaluation via webfetch of READMEs + LICENSE files (no
dependencies added, no code vendored, no Claude hooks copied). YMONEY ships
native implementations (`backend/app/engine/intelligence/`); the concepts
below informed the local design only.

| Repository | Purpose | License (verified 2026-09-25) | Verdict |
|---|---|---|---|
| `GhalebDweikat/winnow` | calibrated context sieve: judge-per-block keep/hide + summary stub + recall key; error-gate + uncertainty-keep safety rules | MIT (LICENSE file fetched) | REFERENCE-ONLY — keep/hide + recall-stub + never-hide-errors concepts adopted into `context_budget.py` (deterministic keyword heuristics, no judge model, no network); the plugin/hook/sidecar itself is NOT integrated (needs Jev credentials + Claude Code function hooks) |
| `tamaratran/fast-jev-compaction` | compaction without summarization: score tool calls/results, drop stale, keep everything else verbatim; newest messages pinned | MIT (LICENSE file fetched) | REFERENCE-ONLY — verbatim-keep + pinning concepts adopted into `context_budget.py` (`PREDEFINED_PINNED_CATEGORIES`, exact-slice `chunk` + `recall`); the npm package/Claude hook is NOT integrated (needs `TYPESAFE_API_KEY`, early-access function hooks) |
| `browser-use/jev-ultrafast` (note: the name `browser-use/claude-ultrafast` 404s — this is the correct repo) | fastest/cheapest web agent: Jev picks operation+target per cycle, small LLM writes text only | MIT (LICENSE file fetched 2026-09-25) | NOT integrated — Lane C's `BrowserIntelligenceAgent` uses a provider-neutral driver interface with a recording backend; jev-ultrafast stays an optional future real-driver candidate behind the same interface, never for platform login where an API exists |
| `jkudish/jev-mcp` | MCP bridge candidate | VERIFIED EXISTS 2026-09-25 (MIT, active, 11 typed tools) — see table above; integration still needs API key + prototype | REJECTED for now — correct repo verified, but no credentials and no isolated prototype; re-evaluate per 10-step rule |
| `itsmostafa/typesafe-mcp` | TypeSafe Jev MCP: typed boolean/choice/score judgments with probabilities (`evaluate setup mcp`, `TYPESAFE_API_KEY` required) | MIT (license badge on README; LICENSE file not re-fetched) | NOT integrated — closest fit to decision primitives, but needs API key + isolated prototype + the 10-step rule; re-evaluate only then, and choose ONE MCP bridge |

Replacement strategy: deterministic local heuristics cover all context-budget
and routing primitives offline; the OpenAI-compatible LLM provider covers
remote assist. Claude/MCP/browser-agent packages remain optional future
tooling, never runtime dependencies.

## Explicitly NOT integrated (per master spec)

`fhshaik/typesafe-mario`, `RomanSlack/jev-drone`, `emrickgarrett/OneVOneJev`, `jarrodwatts/jev-trader`, `irfndi/prism-liquidity-agent`, `monteduro/killmyidea`, `AkashPriyadarshii/jev-curate` (until datasets exist), `lahfir/agent-desktop` (future eval only), `jexp/neo4jev` (reference only, no Neo4j coupling).

## MuseTalk lip-sync (evaluated 2026-09-28 — Work 07 Lane B)

Repository: https://github.com/TMElyralab/MuseTalk

Purpose: audio-driven lip-sync for dubbed/voice-swapped video — the
candidate behind Work 07 Lane B's replaceable lip-sync layer
(`backend/app/engine/lipsync/`).

Pinned ref:
commit `0a89dec45a0192b824e3cf4daf96c239440c5ed8` (2025-09-26,
"feat: update download_weights.bat (#372)"). The repo has NO GitHub
releases/tags (`GET /releases` returns `[]`); versioning is by README
milestones — MuseTalk 1.0 (2024-04-02) and MuseTalk 1.5 (2025-03-28,
with training code released 2025-04-05).

License (exact strings, verified 2026-09-28 by webfetch of `README.md` +
`LICENSE`):

- Code: **MIT** — `LICENSE` opens with "MIT License / Copyright (c) 2024
  Tencent Music Entertainment Group"; README "Disclaimer/License" item 1:
  *"code: The code of MuseTalk is released under the MIT License. There is
  no limitation for both academic and commercial usage."*
- Model weights: **MIT** — HuggingFace model card `TMElyralab/MuseTalk`
  carries tag `license:mit` (`cardData.license = "mit"`); README item 2:
  *"model: The trained model are available for any purpose, even
  commercially."*
- Transitive components named in `LICENSE`: sd-vae-ft-mse (MIT), OpenAI
  whisper (MIT), face-parsing.PyTorch (MIT), DWpose (Apache-2.0),
  face-alignment (BSD-3-Clause), S3FD (license text included in LICENSE);
  syncnet comes from ByteDance LatentSync — verify its own terms before
  depending on it. Each dependency's license governs its own use.
- README item 4: the repo's **testdata** is "available for non-commercial
  research purposes only" — test data only; YMONEY never uses it and it
  does not restrict code/weights.
- Commercial usability: YES for code and weights (MIT, explicit commercial
  grant) — keep the MIT copyright notice. Not BLOCKED-for-production.

Dependency / runtime requirements (README, verified 2026-09-28):

- Python 3.10, CUDA 11.7/11.8, PyTorch 2.0.1 (+torchvision 0.15.2),
  `requirements.txt`, MMLab stack (`mmcv==2.0.1`, `mmdet==3.1.0`,
  `mmpose==1.1.0`, mmengine via mim), ffmpeg on PATH.
- GPU mandatory — no CPU path. Author-tested floor: RTX 3050 Ti Laptop
  **4GB VRAM** with `--use_float16` (~5 min for an 8s clip); real-time
  30fps+ claimed on Tesla V100. Input video recommended 25fps (training
  fps); face region 256x256.
- Weights: HF `TMElyralab/MuseTalk` (musetalk + musetalkV15) plus
  sd-vae-ft-mse, whisper-tiny, DWPose, syncnet/LatentSync,
  face-parse-bisent, resnet18.
- Training (NOT needed for inference): 32–85GB VRAM/GPU (H20-class).

Maintenance (checked 2026-09-28): last code commit 2025-09-26 (~12 months
old), no releases; HF weight artifacts last modified 2026-09-22 (6 days
before this evaluation) and the issue/PR flow is alive (PR #372 merged).
README notes third-party integrations (ComfyUI) are NOT verified or
maintained by the team.

Verdict: **production-conditional (research-grade but usable)** — license
clean, inference path mature (1.5 + realtime mode + open training code),
but it is a research codebase with documented limits (single-frame jitter,
256x256 face region, identity-preservation gaps), a heavy pinned
Python/CUDA stack and a hard GPU requirement. YMONEY ships it ONLY behind
config as an isolated subprocess adapter (`LIPSYNC_PROVIDER=musetalk`,
`LIPSYNC_MUSE_COMMAND`, `LIPSYNC_MUSE_MODEL_DIR` in
`backend/app/engine/lipsync/musetalk.py`) — never a startup dependency,
never in-request. On a GPU-less box `health()` reports `unavailable` with
remediation, `submit()` fails closed, and the default stays
`UnavailableAdapter` (`LIPSYNC_PROVIDER=auto`).

Used directly in core domain: No — reached only through the
`LipSyncProvider` ABC in `backend/app/engine/lipsync/`; no vendored
MuseTalk code and no new YMONEY dependency.

Replacement strategy: `ExternalAdapter` (any HTTP lip-sync worker via
`LIPSYNC_EXTERNAL_BASE_URL`) or any other provider behind the same
`LipSyncProvider` ABC; the `lipsync_jobs` table and routes are
provider-agnostic.

## Avatar provider evaluation — LivePortrait (evaluated 2026-09-28, NOT deployed)

READ-ONLY evaluation via webfetch of repo `README.md` + `LICENSE` (no
dependencies added, no code vendored, no weights downloaded). Candidate
behind Work 07 Lane C's replaceable avatar layer
(`backend/app/engine/avatar/profile.py` → `AvatarProvider` ABC).

Pinned ref: `KlingAIResearch/LivePortrait` default branch `main`,
evaluated 2026-09-28 (repo has no annotated releases; versioning by
README "Updates" milestones — initial release 2024-07-04, Animals model
2025-01-01). Weights: HF `KlingTeam/LivePortrait` (`pretrained_weights/`).

License (exact strings, verified 2026-09-28 from the `LICENSE` file):

- Code: **MIT** — `LICENSE` opens with "MIT License / Copyright (c) 2024
  Kuaishou Visual Generation and Interaction Center".
- **InsightFace (face detection): code MIT, MODELS non-commercial** —
  `LICENSE` states verbatim: *"The code of InsightFace is released under
  the MIT License. The models of InsightFace are for non-commercial
  research purposes only."* and *"If you want to use the LivePortrait
  project for commercial purposes, you should remove and replace
  InsightFace's detection models to fully comply with the MIT license."*
- Animals mode additionally requires `X-Pose` (IDEA-Research) with a
  compiled CUDA op — another dependency whose terms must be verified
  per-use.
- Ethics section of README: acknowledges deepfake risk and disclaims
  legal responsibility for generated results — consistent with YMONEY's
  consent-gated avatar design (authorization required before render).

Dependency / runtime requirements (README, verified 2026-09-28): Python
3.10, PyTorch + CUDA (tested 11.1/11.8/12.1; Windows notes recommend
CUDA 11.8), `requirements.txt`, ffmpeg; GPU strongly recommended (macOS
MPS fallback ~20x slower than RTX 4090); Humans mode works without
X-Pose.

Verdict: **BLOCKED-for-commercial-production as-is** — the default
InsightFace detection weights are non-commercial-only, which is exactly
the restricted-dependency case Work 07 requires replacing before
commercial use (candidate replacements: MediaPipe/OpenCV detector path —
already noted in the evaluation checklist; `ComfyUI-LivePortraitKJ`
documents a MediaPipe substitution approach as prior art). Until a
commercially-licensed detector replaces InsightFace AND the replacement
is re-evaluated, LivePortrait stays **unshipped**: no dependency, no
weights, adapter config-gated behind `AVATAR_PROVIDER` with the default
`UnavailableAdapter` fail-closed path.

Used directly in core domain: No — reached only through the
`AvatarProvider` ABC; no vendored LivePortrait code and no new YMONEY
dependency. Existing production avatar lanes (SadTalker/WavLip, with
their own `license_notes`) remain unchanged behind `providers/avatar.py`.

## json-render (evaluated 2026-09-28 — Work 08 Lane C, NOT adopted)

READ-ONLY evaluation via webfetch of the GitHub README + `LICENSE`
(no dependencies added, no code vendored, no packages installed).

Repository: https://github.com/vercel-labs/json-render (docs: json-render.dev)

Purpose: "Generative UI" framework — you define a **catalog** of approved
components/actions (Zod prop schemas) + a renderer registry; the model emits
JSON specs constrained to that catalog and the renderer draws them
(React/Vue/Svelte/Solid/RN/Remotion/PDF/email/terminal/Three.js), with
streaming (`SpecStream`), dynamic props (`$state`/`$cond`/`$template`),
actions, state watchers, devtools.

License (verified 2026-09-28): **Apache-2.0** — README "License" section and
the repo `LICENSE` file ("Copyright 2025 Vercel Inc."). License is NOT a
blocker; the Apache patent grant is fine for commercial use.

Maintenance (GitHub API, 2026-09-28): created 2026-01-14, last push
2026-09-25 (3 days before evaluation), 18,355 stars / 973 forks / 59
watchers, 118 open issues, TypeScript, `vercel-labs` org, README badge
"LABS-PRODUCT" — i.e. actively developed but explicitly a labs/experimental
product with no stability guarantee (README still documents unreleased
"experimental" APIs).

Dependency weight: the useful surface is `@json-render/core` +
`@json-render/react` (+ `zod`, schema helpers), but the catalog/renderer
model assumes the JS package family owns the catalog definition, the AI
system-prompt generation (`catalog.prompt()`) and the rendering layer.
Adoption would push YMONEY's creative command catalog (currently
backend-owned) into a TS dependency tree with peer deps (React version
coupling, Radix/Tailwind for the shadcn preset), and the repo publishes 30+
interlinked packages that move together — a large upgrade surface for one
renderer.

Verdict: **NOT adopted — schema-driven approved-component approach is
implemented INTERNALLY instead.** Reasoning:

1. Fit: YMONEY does not need generative UI from arbitrary prompts. It needs
   an *approved-component* contract for creative commands: the backend
   validates commands against a fixed catalog and the frontend renders only
   that catalog. That is json-render's core idea minus the JS ownership.
2. Existing internal implementation (same pattern, no new dep):
   `backend/app/engine/creative/commands.py` (Lane B's typed command catalog:
   `APPROVED_COMPONENTS` + per-command field/component specs —
   the single source of truth, validated server-side) consumed by
   `backend/app/engine/creative/commands.py` / `director.py`, rendered by the
   React frontend through the `/api/v1/creative/*` routes. **No arbitrary JS
   from the model** — the model may only emit catalog commands, the backend
   rejects anything else, and the UI renders fixed components. Brand hard
   constraints are enforced server-side by `engine/brand` policy, not by the
   renderer.
3. Cost/benefit: adopting would split the catalog's source of truth across
   Python and TS, add a labs-grade dependency family (fast churn, 118 open
   issues, experimental APIs) and couple the frontend build to a third-party
   renderer, while delivering little beyond what the internal catalog +
   renderer already provide.
4. Re-evaluate if: YMONEY ever needs LLM-authored UI beyond fixed creative
   commands (multi-framework render surface, streaming specs, interactive
   state) — the bind point would be the frontend renderer for
   `/creative/*` responses; Apache-2.0 makes adoption legally cheap later.

Replacement strategy: internal catalog stays authoritative
(`engine/creative/catalog.py` → API → React components); any future
generative-UI need binds behind the same command/response contract.

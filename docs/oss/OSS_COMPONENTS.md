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
- [ ] `TMElyralab/MuseTalk` — lip-sync candidate. License + model weights: verify commercial compatibility.
- [ ] `KlingAIResearch/LivePortrait` — portrait animation candidate. License + weights: verify; replace restricted detectors with MediaPipe/OpenCV path. Record in `docs/oss/MODEL_LICENSES.md` (to be created at Phase 11).
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

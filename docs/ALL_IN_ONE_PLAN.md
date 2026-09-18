# YMONEY All-in-One Plan — free engines, link-to-shorts, and every feature the platform needs

**Date:** 2026-09-19 · **Status:** plan (research-verified, not yet built)
**Rule:** every addition is FREE/OSS-first, CPU-default, GPU-optional behind capability probes
(the same pattern as `/repurpose/status`, `tts_provider_status`, engine `health()`).

## 0. What we already ship (26 features, 12 agents) — do not rebuild

Trend discovery (6 sources) · scoring v2 + virality · Decision Engine WHY · research briefs +
claim tracking · strategy · script variants · hook ranking · ffmpeg_avatar render · MPT adapter ·
13-dim QC · per-platform SEO · direct publishers (YT/TikTok/FB/IG) + relay · 4 analytics
providers · learning patterns · persistent memory · calendar sweep + best-times · asset uploads ·
cover remake · word-timed captions · BGM bed · Safety Center · SSE activity · Telegram remote ·
4-platform OAuth · token-signed public links · approval hold · AI/finance disclosures.

## 1. OSS landscape (verified Sept 2026)

### 1.1 Free video engines (all behind `BaseVideoEngine`)

| Engine | OSS pick | License | HW | Role |
|---|---|---|---|---|
| Stock/composite | **ffmpeg_avatar (own)** | own | CPU | default real render — keep |
| Link-to-shorts | **OpenShorts-style pipeline** (own, MIT-inspired) | own | CPU | URL → viral clips (see §3) |
| AI B-roll | **Wan 2.1 1.3B** (T2V) | Apache-2.0 | ~8 GB VRAM | custom scene clips |
| AI B-roll (fast) | **LTX-Video 0.9.5** | LTX license | ~16 GB | fastest image-to-video |
| AI B-roll (quality) | **Wan 2.2 / HunyuanVideo** | Apache-2.0 / Tencent | 24 GB+ | quality tier (later) |
| Avatar presenter | **SadTalker** (photo+audio) | open | ~8 GB | talking-head default |
| Avatar (quality) | **MuseTalk** (real-time latent) | open | 16 GB+ | quality tier |
| Avatar (fast) | **Wav2Lip** | open | low | quick lip-sync fixes |
| Programmatic motion | **Remotion / MotionCanvas** | OSS (license-check) | CPU/Node | LATER: template motion graphics |

Avoid for server use: AGPL-3.0 code (ViralMint) unless process-isolated; CC-BY-NC weights
(Fish Speech, F5-TTS) — non-commercial, skip.

### 1.2 Free voices (all behind `BaseTTSProvider`)

| Voice | License | HW | Role |
|---|---|---|---|
| edge / kokoro / mock | — / Apache-2.0 | CPU | keep (default) |
| **Chatterbox-Turbo** (Resemble, 0.5B) | MIT | gaming GPU | NEW default clone voice — blind-test beats ElevenLabs 65/25 |
| **Qwen3-TTS** (Apache-2.0, 3-sec clone, 10 langs) | Apache-2.0 | GPU | NEW multilingual + cloning |
| **Piper** (active fork) | GPL-3.0 | edge/CPU | tiny-device fallback |

### 1.3 Link-to-shorts stack (all CPU, all free)

yt-dlp (intake, have) → faster-whisper word timestamps (have) → **PySceneDetect** (scene
boundaries, NEW) → **MediaPipe/object face-track reframe** (NEW, OpenShorts pattern) →
LLM viral-moment rank (have LLM layer) → FFmpeg cut/crop/captions (have) →
silence-strip + hook-first reorder (NEW).

### 1.4 Free supporting OSS

- Captions: faster-whisper (have) + caption **presets** (own ASS styles: word-pop/karaoke/minimal).
- Stock: Pexels photo+video (have) · Pixabay/Coverr/Mixkit (later keys).
- BGM: local allowlist (have) + loudness chain (have).
- Upscale/restore (later): Real-ESRGAN, GFPGAN/CodeFormer.
- Dubbing (later): SeamlessM4T / Coqui (license-check per use).

## 2. Feature inventory — all-in-one needs 47 features (26 have + 21 add)

### A. Engines & media (have 4 → add 5)

| # | Feature | OSS | New agent |
|---|---|---|---|
| A1 | Link-to-shorts engine (URL→clips) | §1.3 stack | Link Miner, Repurpose Editor |
| A2 | AI B-roll scene source | Wan 2.1 1.3B / LTX-0.9.5 | B-roll Researcher |
| A3 | Avatar presenter engine | SadTalker → MuseTalk | Avatar Director |
| A4 | Voice clone + multi-voice + emotion tags | Chatterbox / Qwen3-TTS | Voice Designer |
| A5 | Caption presets + safe-zone overlay | own ASS presets | Caption Stylist |

### B. Editor & brand (have 3 → add 6)

| # | Feature | Notes |
|---|---|---|
| B1 | Viral-moment rank (score+hook+reason per clip) | LLM over transcript; feeds selection |
| B2 | Face-tracked 9:16 reframe + scene detect | PySceneDetect + tracking crop |
| B3 | Silence strip + filler removal | extend vision silent-section detect → cut |
| B4 | Hook A/B preview + title variants | generate 2–3, preview side-by-side |
| B5 | Brand kits (fonts/colors/logo/watermark/caption style) | workspace-scoped, honored by render |
| B6 | B-roll override UI (swap any scene's visual) | per-scene picker: stock / AI / upload |

### C. Publish & distribute (have 6 → add 3)

| # | Feature | Notes |
|---|---|---|
| C1 | Spec preflight validator (duration/resolution/size/codec per platform) | fail-closed before upload |
| C2 | First-comment pack + pinned CTA (generated; auto-post where scopes allow) | SEOAgent extension |
| C3 | Distribution planner (fills calendar from best-times + caps) | Scheduler agent |

### D. Intelligence (have 8 → add 4)

| # | Feature | Notes |
|---|---|---|
| D1 | Competitor channel tracking + trend-jacking alerts | Competitor Analyst |
| D2 | New sources: TikTok Creative Center, YT Trending, keyword difficulty | trend provider ports |
| D3 | Retention-curve ingestion (hook×topic interactions, fatigue decay) | LearningAgent extension |
| D4 | Reused-content risk score (Content-ID/YPP safety) | Compliance Officer |

### E. Trust & compliance (have 4 → add 2)

| # | Feature | Notes |
|---|---|---|
| E1 | Compliance Officer gate (disclosures + preflight + risk → HUMAN_REVIEW) | split out of QC |
| E2 | Per-video audit export (WHAT/WHY/render/publish evidence bundle) | one-click transparency |

### F. Scale & reach (have 1 → add 1 now, 2 later)

| # | Feature | Notes |
|---|---|---|
| F1 | Redis/Celery + Postgres E2E + GPU worker lane | documented path → real |
| F2–F3 | *(later)* mobile companion, white-label workspaces | — |

## 3. Agent roster: 12 → 22 (10 new, all `BaseAgent` + registry + enable/disable)

| # | Agent | Owns | OSS/tools |
|---|---|---|---|
| 13 | **Link Miner** (discovery) | URL → transcript → viral moments + score/hook/reason | yt-dlp, faster-whisper, LLM rank |
| 14 | **Repurpose Editor** (creation) | moment → 9:16 assembly (reframe, captions, BGM) | PySceneDetect, MediaPipe crop, FFmpeg |
| 15 | **B-roll Researcher** (creation) | per-scene visual: stock → AI → upload | Pexels, Wan/LTX lane |
| 16 | **Avatar Director** (production) | presenter photo + voice → lip-sync clip | SadTalker/MuseTalk lane |
| 17 | **Voice Designer** (production) | voice pick/clone, emotion tags, multi-voice dialogue | Chatterbox/Qwen3-TTS |
| 18 | **Caption Stylist** (production) | preset, safe zone, readability | ASS preset library |
| 19 | **Compliance Officer** (governance) | disclosures, spec preflight, risk score → HUMAN_REVIEW | policy engine |
| 20 | **Competitor Analyst** (intelligence) | channel tracking, trend-jack alerts | new trend ports |
| 21 | **Scheduler** (distribution) | best-time calendar fill within caps | sweep + best-times |
| 22 | **Dubb­ing Localizer** (later) | multilingual voice + caption tracks | SeamlessM4T-class |

Existing agents gain: Producer (new engine lanes), QC (holds line while Compliance splits
out), SEO (first-comment/pinned/title variants), Learning (retention curves).

## 4. Build order (smallest shippable slices, CPU-first)

1. **E1 link-to-shorts**: `providers/clips.py` → scene detect + face reframe + viral rank;
   Link Miner + Repurpose Editor agents; caption preset #1. *Unblocks URL→Shorts.*
2. **E2 voice**: Chatterbox + Qwen3-TTS providers; Voice Designer; emotion tags; multi-voice.
3. **E3 avatar**: SadTalker lane + Avatar Director; `VIDEO_ENGINE=avatar` option.
4. **E4 AI B-roll**: Wan 2.1-1.3B lane + B-roll Researcher; B-roll override UI.
5. **E5 trust**: Compliance Officer + spec preflight + reused-content score; audit export.
6. **E6 brains**: Competitor Analyst + TT-Creative/YT-Trending sources + retention curves + Scheduler.
7. **E7 scale**: Redis/Celery + Postgres E2E + GPU worker lane; brand kits + hook A/B UI.

Every slice: workspace-isolation test + idempotency test + simulation E2E + suite green +
frontend typecheck/build (per YMONEY_V2_ARCHITECTURE verification gates).

## 5. GPU tiering (never required, always probed)

- **CPU (default)**: everything today + link-to-shorts + presets. Zero new hardware.
- **8 GB**: Wan-1.3B B-roll, SadTalker avatars, Chatterbox voices.
- **16 GB+**: LTX B-roll speed, MuseTalk quality, Qwen3 multilingual.
- Engines report `health()`/`version()`/`capabilities()`; UI shows per-engine readiness
  exactly like the video-engine page does now.

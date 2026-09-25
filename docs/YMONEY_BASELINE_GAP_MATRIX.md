# YMONEY Baseline Gap Matrix

Audited 2026-09-23 against the live repository (not documentation claims).
Every status below cites executing code paths. Statuses: ✅ COMPLETE ·
🟢 WORKING · 🟡 PARTIAL · 🔵 NEXT · 🔴 MISSING · ⚠️ BROKEN/BLOCKED ·
🧪 NEEDS VERIFICATION · ♻️ REFACTOR REQUIRED.

## 🎥 Video Creation

✅ Short-form creation
Evidence:
- `backend/app/providers/video_engine/ffmpeg_avatar.py` (local FFmpeg render)
- `backend/app/providers/video_engine/mpt.py` (MoneyPrinterTurbo HTTP adapter)
- `backend/app/engine/agents/production.py` (Video Producer agent)

🟡 Long-form generation
Evidence:
- short pipeline stages exist and run; no chapter planner, no scene graph,
  no voice/visual continuity across chapters.
Missing: `LongFormVideoPipeline`, chapter architecture, long-form scene graph.

## ✂️ Repurposing

🟢 Long → short virality scoring + reframe
Evidence:
- `backend/app/engine/agents/repurpose.py`, `backend/app/providers/clips.py`
Missing: diarization, word timestamps, multi-face tracking, active-speaker
framing, split-screen/podcast layouts (WhisperX/pyannote/MediaPipe behind
interfaces — all pending license verification).

## 🎞 Editor / Timeline

🟢 Canonical Timeline model + engine + routes (Work 01)
Evidence:
- `backend/app/models/timeline.py`, `backend/app/engine/timeline.py`,
  `backend/app/engine/otio_adapter.py` (real OTIO 0.18.1),
  `backend/app/api/v1/timelines.py`, `TimelinesPanel.tsx` in ContentDetail.
🔴 Professional multi-track editor (split/trim/keyframes/waveform/autosave).

## 🎙 Audio / 🌍 Localization / 🧑‍🎤 Avatar

🟢 TTS (edge-tts + ElevenLabs), voice designer, dubbing dry-run + pipeline,
    SadTalker/Wav2Lip/server avatar lanes.
Missing: `AudioEnhancementPipeline` (derived assets, A/B), glossary-aware
localization 2.0, `AvatarProvider` interface + MuseTalk/LivePortrait eval
(license audit first — see `docs/oss/OSS_COMPONENTS.md`).

## 📡 Publishing

🟢 YouTube / TikTok / Instagram / Facebook + idempotent `PublishingJob`s,
    scheduler, approval hold (`require_approval_before_publish`).
Missing: LinkedIn, X, Threads, Pinterest, Snapchat, Bluesky (`SocialPublisher`
adapters — official APIs only).

## 📊 Analytics / 🧪 Experiments / 🧠 Learning

🟢 Metric snapshots, analytics providers, learning patterns w/ confidence +
    sample size, semantic memory retrieve.
Missing: retention→scene mapping, `ExperimentEngine`, scene-level creative
learning, knowledge graph, social inbox, collaboration.

## 🤖 Autonomy / 🧠 Decision

🟢 22 agents + durable job engine + `engine/decision.py` NEXT-BEST-ACTION
    (PRODUCE/WAIT/SKIP/RESEARCH_MORE/HUMAN_REVIEW) with WHY payloads.
Missing: unified `DecisionEngine` interface (boolean/choose/rank/verify/route),
browser intelligence, AI Creative Director, generative UI catalog, BrandDNA.

## 🧹 Hygiene findings (all verified, none destructive)

- ♻️ `MoneyPrinterTurbo/` vendored copy deleted from worktree (HTTP adapter is
  the only integration; deletions uncommitted, nothing imports the old path).
- ✅ 0003 migration pair documented + locked by `test_migrations_hygiene.py`
  (append-only rule; renames forbidden).
- ✅ No GPL/AGPL code in runtime; pending OSS rows carry license checklists.
- ✅ No secrets in git (`.env` gitignored; credentials encrypted via
  `ApiCredential`/settings key allowlist).
- ⚠️ `timestamptz` migration deferred — needs live Postgres (SQLite naive +
  UTC-only backend is documented behavior).
- ⚠️ Slowest tests are real optimization targets, not flakes: motion status
  probe ~46s, compliance preflight ~60s.

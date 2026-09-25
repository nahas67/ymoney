# YMONEY Master OSS Work Plan — P0 Foundation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Establish the P0 platform foundation slice: baseline audit, migration hygiene, OSS inventory docs, master checkpoint, and the canonical editorial Timeline model with OTIO interchange and RenderManifest — without breaking any working system.

**Architecture:** Additive-only backend slice. New `ContentTimeline` ORM model + pure-function `engine/timeline.py` (create/validate/OTIO-export/OTIO-import/from-video/manifest/versioning) + migration `0013` (IF NOT EXISTS). Existing `VideoEngine` interface untouched; timeline *describes* renders, never replaces the render path.

**Tech Stack:** FastAPI + SQLAlchemy (SQLite test / Postgres prod), React/Vite frontend (untouched this slice), pytest, ruff.

**Spec:** User master prompt `YMONEY — MASTER OSS WORK PLAN` (2026-09-23): Phases 1–24, P0 order §MASTER IMPLEMENTATION ORDER, execution rules §1–20, OSS repository rule, checkpoint format, definition of done.

## Global Constraints

- Inspect existing code before changing architecture; preserve every working feature unless replacing with demonstrably superior implementation.
- Adapters/interfaces around every major OSS dependency; OSS projects are components, never the architecture.
- Never blindly copy an external repository into YMONEY; pin production dependencies to known versions/commits.
- Check code license AND model-weight license independently; no GPL/AGPL/non-commercial in commercial runtime without explicit approval.
- Never commit API keys, credentials, cookies, OAuth tokens, or `.env` secrets.
- Mocks permitted in tests only; production paths use real implementations or explicitly report unavailability.
- Destructive/financial/publishing/account operations stay deterministic and permission-controlled.
- No phase marked complete without tests/build/type-check/lint/E2E evidence.
- Prefer extending existing YMONEY components over duplicate systems.
- Migrations are append-only; never rename/edit a shipped migration — new migration instead (per `backend/app/migrations/versions/README.md`).

## Review Focus

- A timeline whose `tracks_json` contains overlapping clips on one track should be rejected by validation with a named track/clip in the error, not silently accepted.
- A `from_video` import for a video with no duration must still produce a valid timeline (fallback duration) rather than crashing the editor load path.
- An OTIO export/import round-trip must preserve clip order, durations, and track kinds exactly — any lossy field must be documented, never silent.
- A `0013` migration replayed on a database that already has the table (via `create_all`) must be a no-op, not an error.
- A workspace-scoped timeline read from another workspace must be denied (isolation), even though this slice adds no HTTP routes.

---

### Task 1: Baseline audit artifacts (docs only)

**Files:**
- Create: `docs/oss/OSS_COMPONENTS.md`
- Create: `docs/YMONEY_MASTER_CHECKPOINT.md`
- Modify: none (read-only task)

**Interfaces:**
- Consumes: repo layout, `ARCHITECTURE.md`, migration list, model class list, provider registry.
- Produces: gap matrix (checkpoint) + OSS inventory table consumed by every later integration task.

- [ ] **Step 1: Write `docs/oss/OSS_COMPONENTS.md`** with one row per current adapter (trend sources, publishers, analytics, TTS, images, video engines incl. MoneyPrinterTurbo HTTP) plus a PENDING section for each OSS repo named in the master spec with license-verification checklist. No code changes.
- [ ] **Step 2: Write `docs/YMONEY_MASTER_CHECKPOINT.md`** with the exact status convention (✅/🟢/🟡/🔵/🔴/⚠️/🧪/♻️/❌), per-phase table, gap matrix (have vs missing vs adapter-ready), files-changed/tests/build/verification placeholders marked honestly.
- [ ] **Step 3: Verify docs render** — `python -c "import pathlib; [print(p) for p in ['docs/oss/OSS_COMPONENTS.md','docs/YMONEY_MASTER_CHECKPOINT.md'] if pathlib.Path(p).exists()]"`. Expected: both paths printed.

### Task 2: Migration hygiene lock (no renames)

**Files:**
- Create: `backend/tests/test_migrations_hygiene.py`
- Modify: none.

**Interfaces:**
- Consumes: `app.migrations.runner.load_migrations`.
- Produces: regression guard consumed by all future migration tasks.

- [ ] **Step 1: Write the failing test**

```python
"""Migrations are append-only; the 0003 pair is the single allowlisted exception."""
from __future__ import annotations


def test_no_new_colliding_sequence_prefixes():
    from app.migrations.runner import load_migrations

    mods = load_migrations()
    seen: dict[str, list[str]] = {}
    for name, _ in mods:
        prefix = name.split("_", 1)[0]
        if prefix.isdigit():
            seen.setdefault(prefix, []).append(name)
    collisions = {p: n for p, n in seen.items() if len(n) > 1}
    assert collisions == {"0003": ["0003_video_progress", "0003_video_thumbnails"]}, collisions


def test_migrations_apply_twice_without_error():
    from app.db import session_scope
    from app.migrations.runner import run_migrations

    with session_scope() as s:
        assert run_migrations(s) == []  # replay is a no-op
```

- [ ] **Step 2: Run test to verify it passes** (guard documents current state; fails if anyone adds a new collision)

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_migrations_hygiene.py -q`
Expected: PASS (2 passed)

- [ ] **Step 3: Commit**

```bash
git add backend/tests/test_migrations_hygiene.py docs/oss/OSS_COMPONENTS.md docs/YMONEY_MASTER_CHECKPOINT.md
git commit -m "test: migration hygiene lock + P0 audit docs"
```

### Task 3: Canonical Timeline model + migration 0013

**Files:**
- Create: `backend/app/models/timeline.py`
- Modify: `backend/app/models/__init__.py`
- Create: `backend/app/migrations/versions/0013_content_timelines.py`
- Test: `backend/tests/test_timeline_foundation.py` (written in Task 4, covers this task)

**Interfaces:**
- Consumes: `app.models.base.PKMixin/TimestampMixin`, `app.db.Base`.
- Produces: `ContentTimeline` ORM class (`workspace_id`, `content_item_id nullable`, `video_id nullable`, `name`, `fps`, `duration_seconds`, `tracks_json`, `version`, `parent_timeline_id nullable` self-FK for undo/version history) consumed by Task 4 engine functions.

- [ ] **Step 1: Write the failing test** (see Task 4 Step 1 — persistence round-trip through `session_scope`, asserts `tracks_json` survives and `parent_timeline_id` links versions).
- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_timeline_foundation.py -q`
Expected: FAIL with "ContentTimeline not defined" (or import error)

- [ ] **Step 3: Write minimal implementation**

```python
"""Canonical editorial timeline: the single source of truth for long videos,
shorts, manual editor, AI Creative Director, and the render engine."""
from __future__ import annotations
from sqlalchemy import JSON, Float, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from app.db import Base
from app.models.base import PKMixin, TimestampMixin


class ContentTimeline(Base, PKMixin, TimestampMixin):
    __tablename__ = "content_timelines"
    __table_args__ = (
        Index("ix_timeline_ws", "workspace_id"),
        Index("ix_timeline_content", "content_item_id"),
        Index("ix_timeline_video", "video_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True)
    content_item_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    video_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    name: Mapped[str] = mapped_column(String(200), default="main")
    fps: Mapped[float] = mapped_column(Float, default=30.0)
    duration_seconds: Mapped[float] = mapped_column(Float, default=0.0)
    tracks_json: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    parent_timeline_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
```

plus migration `0013_content_timelines.py` with `CREATE TABLE IF NOT EXISTS content_timelines (...)` and `CREATE INDEX IF NOT EXISTS ...`, plus `__init__.py` re-export.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_timeline_foundation.py tests/test_migrations_hygiene.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/models/timeline.py backend/app/models/__init__.py backend/app/migrations/versions/0013_content_timelines.py
git commit -m "feat: canonical ContentTimeline model + 0013 migration"
```

### Task 4: Timeline engine (pure functions) + OTIO + manifest + from_video

**Files:**
- Create: `backend/app/engine/timeline.py`
- Test: `backend/tests/test_timeline_foundation.py`

**Interfaces:**
- Consumes: `ContentTimeline.tracks_json` shape: `{"tracks": [{"id","kind","name","clips":[{"id","name","start","duration","source":{}, "effects":[]}]}]}`. Track kinds: `video|broll|avatar|text|caption|voice|music|sfx`.
- Produces: `create_empty`, `add_clip`, `validate_timeline` (raises `TimelineValidationError` naming track+clip on overlap/negative/unknown-kind), `to_otio_dict` (OTIO-compatible `Timeline` dict), `from_otio_dict` (inverse), `timeline_from_video` (single-clip import w/ duration fallback 5.0s), `shorts_representation` (9:16/16:9/1:1/4:5 aspect check), `render_manifest` (clips flattened w/ absolute paths + total duration + fps + hash), `save_version` (copies row, bumps version, links parent). No new dependencies — OTIO dict is plain JSON; real `opentimelineio` lib binds later behind these functions.

- [ ] **Step 1: Write the failing test**

```python
def test_otio_roundtrip_preserves_order_and_duration():
    from app.engine.timeline import add_clip, create_empty, from_otio_dict, to_otio_dict
    t = create_empty("ws", duration_seconds=30.0)
    add_clip(t, track="video", clip_id="c1", name="A", start=0.0, duration=10.0)
    add_clip(t, track="video", clip_id="c2", name="B", start=10.0, duration=20.0)
    back = from_otio_dict(to_otio_dict(t))
    clips = back["tracks"][0]["clips"]
    assert [c["id"] for c in clips] == ["c1", "c2"]
    assert [c["duration"] for c in clips] == [10.0, 20.0]


def test_validation_rejects_overlap_with_named_error():
    import pytest
    from app.engine.timeline import TimelineValidationError, add_clip, create_empty, validate_timeline
    t = create_empty("ws")
    add_clip(t, track="video", clip_id="c1", name="A", start=0.0, duration=10.0)
    add_clip(t, track="video", clip_id="c2", name="B", start=5.0, duration=5.0)
    with pytest.raises(TimelineValidationError, match="video.*c2"):
        validate_timeline(t)


def test_from_video_without_duration_uses_fallback():
    from app.engine.timeline import timeline_from_video
    t = timeline_from_video("ws", video_id="v1", duration_seconds=None, aspect="9:16")
    assert t["duration_seconds"] == 5.0
    assert t["tracks"][0]["clips"][0]["source"]["video_id"] == "v1"


def test_render_manifest_hash_stable():
    from app.engine.timeline import add_clip, create_empty, render_manifest
    t = create_empty("ws", duration_seconds=10.0)
    add_clip(t, track="voice", clip_id="n1", name="narr", start=0.0, duration=10.0)
    assert render_manifest(t)["manifest_hash"] == render_manifest(t)["manifest_hash"]


def test_persistence_and_version_history(db_session=None):
    # persistence round-trip + parent-linked version (uses session_scope)
    from app.db import session_scope
    from app.engine.timeline import create_empty, save_version
    from app.models import ContentTimeline
    with session_scope() as s:
        row = ContentTimeline(workspace_id="ws-t", name="main",
                              duration_seconds=8.0, tracks_json=create_empty("ws-t"))
        s.add(row)
        s.flush()
        tid = row.id
        v2 = save_version(s, tid, label="hook swap")
    with session_scope() as s:
        row2 = s.get(ContentTimeline, v2)
        assert row2.version == 2 and row2.parent_timeline_id == tid
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_timeline_foundation.py -q`
Expected: FAIL with "engine.timeline not defined" (or import error)

- [ ] **Step 3: Write minimal implementation** — `backend/app/engine/timeline.py` per Interfaces above (~150 lines, stdlib only: `copy`, `hashlib`, `json`).
- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && .venv/Scripts/python -m pytest tests/test_timeline_foundation.py tests/test_migrations_hygiene.py tests/test_approval.py -q`
Expected: PASS

Run: `cd backend && .venv/Scripts/python -m ruff check app/models/timeline.py app/engine/timeline.py app/migrations/versions/0013_content_timelines.py tests/test_timeline_foundation.py tests/test_migrations_hygiene.py --output-format concise`
Expected: clean

- [ ] **Step 5: Commit**

```bash
git add backend/app/engine/timeline.py backend/tests/test_timeline_foundation.py
git commit -m "feat: canonical timeline engine (OTIO interchange, manifest, versioning)"
```

## File structure (this slice)

```text
docs/oss/OSS_COMPONENTS.md            # OSS inventory + pending-license checklist
docs/YMONEY_MASTER_CHECKPOINT.md      # checkpoint + gap matrix (this slice: Phase 1 partial)
docs/superpowers/plans/2026-09-23-ymoney-master-oss.md  # this file
backend/app/models/timeline.py        # ContentTimeline ORM (new)
backend/app/models/__init__.py        # re-export (modify)
backend/app/migrations/versions/0013_content_timelines.py  # additive (new)
backend/app/engine/timeline.py        # pure timeline functions (new)
backend/tests/test_timeline_foundation.py  # 5+ tests (new)
backend/tests/test_migrations_hygiene.py   # 2 tests (new)
```

Later phases get their own plans (one per subsystem): P0-editor, P0-longform, P0-repurpose2, P0-audio, P0-retention, P1-decision-engine, P1-browser-intel, P1-creative-director, P1-brand-dna, P1-campaign, P1-experiments, P2-graph-memory, P2-inbox-collab, P2-exports.

## Self-review

1. **Spec coverage (this slice):** Phase 1 checkpoint items covered: canonical model ✅, OTIO conversion ✅ (dict-level, lib binding later), tracks ✅, DB persistence ✅, import existing videos ✅ (`timeline_from_video`), shorts representation ✅ (`shorts_representation`), RenderManifest ✅, existing engine untouched ✅, tests ✅. Editor load/save + undo/redo state: persistence + `save_version` parent links cover the *state* half; editor UI is explicitly NEXT (own plan). Undo/redo UI, Premiere/DaVinci export adapters: NEXT with reasons recorded in checkpoint.
2. **Placeholder scan:** no TBD/TODO; error handling is concrete (`TimelineValidationError` with track+clip naming); tests contain literal assertions.
3. **Type consistency:** `tracks` shape defined once in Task 4 Interfaces and reused by Task 3 model docstring; `save_version(s, tid, label)` signature matches test call.
4. **Review Focus:** five failure modes listed above, each pinned to a test in Task 4 (overlap → validation test; missing duration → fallback test; OTIO loss → round-trip test; replay → hygiene test; cross-workspace → documented as route-level, no routes added this slice so no test possible — honest gap in checkpoint).

## Execution Handoff

Plan saved to `docs/superpowers/plans/2026-09-23-ymoney-master-oss.md`. Standing instruction from the user is autonomous execution without permission prompts, so this plan executes immediately slice-by-slice; each slice reports files changed, test/build evidence, and remaining gaps per the checkpoint format.

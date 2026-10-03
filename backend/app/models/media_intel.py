"""Work 12 media-intelligence foundation: runs, cache, chunks, derived facts.

Tables (mirrored idempotently in migrations/versions/0029_media_intelligence.py):

  * media_intel_runs      -- one processing manifest per intelligence run
                             (status/progress/chunks/cancel/cost/metrics/manifest)
  * media_intel_cache     -- UNIQUE (workspace_id, cache_key) index -> prior run
  * media_intel_chunks    -- resumability ledger, one row per chunk (idx)
  * diarization_segments  -- SPEAKER | SPEECH_ACTIVITY segments
  * media_intel_words     -- word-alignment rows
  * speaker_aliases       -- operator labels for anonymous speaker ids
  * face_tracks           -- session-local anonymous face tracks
  * face_track_samples    -- per-sample boxes (+ landmarks when supplied)
  * mask_assets           -- mask FILE references (never blobs)
  * active_speaker_map    -- speaker <-> face windows
  * edit_proposals        -- non-destructive cut proposals
  * audio_time_maps       -- source <-> edited time mapping
  * reframe_plans         -- layout plans
  * reframe_keyframes     -- EDITABLE keyframes
  * intel_qc_results      -- QC verdicts (audio | visual)

Plus two ADDITIVE columns on the existing ``media_assets`` row (Work 12
lineage): ``parent_asset_id`` (self FK) and ``derivation_json``.

Design rules (contracts §0 invariants):

  * Isolation: every workspace-scoped table carries ``workspace_id`` and every
    service query filters on it; a foreign id reads as 404, never 403.
  * Derived only: a run points at its source ``asset_id`` and, when it produced
    media, at a NEW ``output_asset_id`` whose ``parent_asset_id`` is the source.
    No intelligence table ever rewrites or mutates the original media bytes.
  * Append-only where it matters: ``diarization_segments``, ``media_intel_words``,
    ``face_track_samples`` and ``intel_qc_results`` are written once per run and
    never edited in place; re-running a capability creates a new run. Run rows
    themselves carry a deliberate state machine (PENDING -> RUNNING -> one
    terminal state) and a cancel flag, so their history is the audit trail.
  * Honest emptiness: nullable evidence columns (``speaker_id`` on a word,
    ``confidence``, ``landmarks_json``, ``mask_asset_id``) stay NULL when the
    provider genuinely could not produce them. Nothing is ever guessed,
    interpolated or filled with a placeholder.
  * NO sensitive inference: speaker ids are anonymous ``SPEAKER_00``/``FT_00``
    session labels and operator aliases are the only human naming. There is no
    gender/race/age/identity/biometric column anywhere in this module, and
    ``speaker_aliases`` (human input) is stored apart from the segment rows.
  * No downgrade: migration 0029 is append-only, so the ORM is the only way to
    remove a column; that is deliberate and matches 0027/0028.
  * Vocabulary: every enum is a plain string column (never a python enum) so the
    stored rows stay readable and later lanes can add a value without a
    migration. The ``*_STATUSES``/``*_KINDS`` tuples below are the contract
    vocabulary; the API and the services validate against them.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    false,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base
from app.models.base import PKMixin, TimestampMixin

# ---------------------------------------------------------------------------
# vocabularies (contracts §2) -- plain strings, never python enums
# ---------------------------------------------------------------------------

#: media_intel_runs.status -- UNAVAILABLE is a first-class TERMINAL state
#: (no provider could serve the request); it is never recorded as FAILED.
RUN_STATUSES: tuple[str, ...] = (
    "PENDING",
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELLED",
    "UNAVAILABLE",
)
#: run states that can never transition again.
TERMINAL_RUN_STATUSES: frozenset[str] = frozenset(
    {"COMPLETED", "FAILED", "CANCELLED", "UNAVAILABLE"}
)
#: run states a retry may act on.
RETRYABLE_RUN_STATUSES: frozenset[str] = frozenset(
    {"FAILED", "CANCELLED", "UNAVAILABLE"}
)
#: media_intel_chunks.status
CHUNK_STATUSES: tuple[str, ...] = ("PENDING", "RUNNING", "COMPLETED", "FAILED", "SKIPPED")
#: diarization_segments.kind -- SPEECH_ACTIVITY rows are VAD measurements, NOT
#: speakers (contracts §4); they always carry speaker_id = NULL.
SEGMENT_KINDS: tuple[str, ...] = ("SPEAKER", "SPEECH_ACTIVITY")
#: active_speaker_map.status -- UNRESOLVED is honest, a guess is not.
ACTIVE_SPEAKER_STATUSES: tuple[str, ...] = ("RESOLVED", "UNRESOLVED")
#: edit_proposals.kind / .status / .decision
PROPOSAL_KINDS: tuple[str, ...] = ("REMOVE_RANGE", "SHORTEN_RANGE", "KEEP")
PROPOSAL_STATUSES: tuple[str, ...] = ("PROPOSED", "DECIDED", "APPLIED", "REJECTED")
PROPOSAL_DECISIONS: tuple[str, ...] = ("keep", "remove", "shorten")
#: intel_qc_results.kind / .verdict (contracts §13)
QC_KINDS: tuple[str, ...] = ("audio", "visual")
QC_VERDICTS: tuple[str, ...] = (
    "PASS",
    "PASS_WITH_WARNINGS",
    "REVIEW_REQUIRED",
    "FAIL",
)


class MediaIntelRun(Base, PKMixin, TimestampMixin):
    """One intelligence run: the processing manifest AND the state machine.

    Status flow: ``PENDING -> RUNNING -> COMPLETED | FAILED | CANCELLED |
    UNAVAILABLE``. ``cancel_requested`` is polled by the chunk loop and handed
    to providers via ``should_cancel``; a cancelled run keeps its partial chunk
    state so a resume can continue. ``metrics_json``/``warnings_json`` plus the
    processing/gpu/cost columns are the provenance the UI shows for every panel
    action (contracts §6/§15).
    """

    __tablename__ = "media_intel_runs"
    __table_args__ = (
        Index("ix_mi_runs_ws_status", "workspace_id", "status"),
        Index("ix_mi_runs_ws_created", "workspace_id", "created_at"),
        Index("ix_mi_runs_ws_kind", "workspace_id", "kind"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    #: the SOURCE asset; never mutated by this run (derived-only invariant).
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    #: capability kind, e.g. "alignment" | "diarization" | "face_tracking"
    kind: Mapped[str] = mapped_column(String(40), default="")
    provider_key: Mapped[str] = mapped_column(String(60), default="")
    model_version: Mapped[str] = mapped_column(String(120), default="")
    params_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    #: sha256 of canonical_params_json -- part of the cache key.
    params_hash: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    asset_checksum: Mapped[str] = mapped_column(String(128), default="", server_default=text("''"))
    status: Mapped[str] = mapped_column(
        String(20), default="PENDING", server_default=text("'PENDING'"), index=True
    )
    #: integer percent 0..100 (the Job queue reports the same unit).
    progress: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    chunks_total: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    chunks_done: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=false()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    processing_ms: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    gpu_ms: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    cost_micros: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    warnings_json: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    metrics_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    #: generic code only ("PROVIDER_UNAVAILABLE", "CANCELLED", ...); never a stack.
    error_code: Mapped[str] = mapped_column(String(60), default="", server_default=text("''"))
    #: NEW derived asset produced by the run (NULL for pure analysis).
    output_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True
    )
    requested_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class MediaIntelCache(Base, PKMixin, TimestampMixin):
    """Workspace-scoped cache index: cache_key -> the run that produced it.

    ``cache_key = sha256(asset_checksum | provider_key | model_version |
    canonical_params_json)`` (contracts §3). A hit returns the prior run and
    performs NO recompute; ``force=true`` inserts a new run and a new row
    (UNIQUE (workspace_id, cache_key) is cleared by pointing the row at the
    newest run, keeping the key space honest).
    """

    __tablename__ = "media_intel_cache"
    __table_args__ = (
        UniqueConstraint("workspace_id", "cache_key", name="uq_mi_cache_ws_key"),
        Index("ix_mi_cache_run", "run_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    cache_key: Mapped[str] = mapped_column(String(64), index=True)
    run_id: Mapped[str] = mapped_column(
        ForeignKey("media_intel_runs.id", ondelete="CASCADE")
    )
    asset_checksum: Mapped[str] = mapped_column(String(128), default="", server_default=text("''"))
    provider_key: Mapped[str] = mapped_column(String(60), default="", server_default=text("''"))
    model_version: Mapped[str] = mapped_column(String(120), default="", server_default=text("''"))
    params_hash: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))


class MediaIntelChunk(Base, PKMixin, TimestampMixin):
    """One resumable chunk of a run (contracts §3).

    Resume re-runs only non-``COMPLETED`` chunks whose ``input_checksum`` still
    matches: a changed chunk input invalidates THAT chunk only, never the run.
    """

    __tablename__ = "media_intel_chunks"
    __table_args__ = (
        UniqueConstraint("run_id", "idx", name="uq_mi_chunk_run_idx"),
        Index("ix_mi_chunks_run_status", "run_id", "status"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("media_intel_runs.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer, default=0)
    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float] = mapped_column(Float, default=0.0)
    #: sha256 over the chunk's own inputs; a mismatch invalidates the chunk.
    input_checksum: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    status: Mapped[str] = mapped_column(
        String(20), default="PENDING", server_default=text("'PENDING'")
    )


class DiarizationSegment(Base, PKMixin, TimestampMixin):
    """One speaker OR speech-activity segment (append-only, per run).

    ``kind='SPEECH_ACTIVITY'`` rows are ffmpeg VAD measurements and MUST carry
    ``speaker_id = NULL``: they are not speakers (contracts §4). A
    ``kind='SPEAKER'`` row exists only when a real diarizer produced it.
    """

    __tablename__ = "diarization_segments"
    __table_args__ = (
        Index("ix_diar_run_start", "run_id", "start_s"),
        Index("ix_diar_ws_asset", "workspace_id", "asset_id"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("media_intel_runs.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    #: anonymous per-run label; NULL for SPEECH_ACTIVITY rows.
    speaker_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    kind: Mapped[str] = mapped_column(
        String(20), default="SPEAKER", server_default=text("'SPEAKER'")
    )
    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class MediaIntelWord(Base, PKMixin, TimestampMixin):
    """One aligned word (append-only, per run).

    Words exist ONLY when a provider genuinely returned word timings; segment
    level ASR never invents them (``words_available=false`` in that case).
    ``speaker_id`` stays NULL when the word falls in a diarization gap -- never
    nearest-guessed across a long gap (contracts §4).
    """

    __tablename__ = "media_intel_words"
    __table_args__ = (
        UniqueConstraint("run_id", "idx", name="uq_mi_word_run_idx"),
        Index("ix_mi_words_run_start", "run_id", "start_s"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("media_intel_runs.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    idx: Mapped[int] = mapped_column(Integer, default=0)
    word: Mapped[str] = mapped_column(String(200), default="", server_default=text("''"))
    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float] = mapped_column(Float, default=0.0)
    speaker_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)


class SpeakerAlias(Base, PKMixin, TimestampMixin):
    """Operator label for an anonymous speaker id ("Host", "Guest").

    Human input only -- the ONLY naming source in the system. Nothing infers a
    name, and there is no demographic/biometric column (contracts §5). Scoped
    by workspace plus optionally an asset or a run, so a per-video label never
    leaks into another asset's dubbing handoff (Work 07 shape:
    ``{speaker_id, label, segments, words}``).
    """

    __tablename__ = "speaker_aliases"
    __table_args__ = (
        Index("ix_sp_alias_ws_speaker", "workspace_id", "speaker_id"),
        Index("ix_sp_alias_asset", "asset_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), nullable=True
    )
    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_intel_runs.id", ondelete="CASCADE"), nullable=True
    )
    speaker_id: Mapped[str] = mapped_column(String(20), default="", index=True)
    label: Mapped[str] = mapped_column(String(120), default="", server_default=text("''"))
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class FaceTrack(Base, PKMixin, TimestampMixin):
    """One session-local anonymous face track (``FT_00``, ``FT_01``, ...).

    Tracks are NEVER identities: no embedding, no name, no demographic, and no
    cross-video linking (contracts §8). An uncertain crossing starts a NEW track
    and bumps ``reentry_count`` instead of silently merging two people.
    ``truncated`` is set when ``sample_count`` hit the provider's cap so the UI
    can say the evidence is partial rather than implying full coverage.
    """

    __tablename__ = "face_tracks"
    __table_args__ = (
        UniqueConstraint("run_id", "track_id", name="uq_face_track_run_label"),
        Index("ix_face_tracks_run_start", "run_id", "start_s"),
        Index("ix_face_tracks_ws_asset", "workspace_id", "asset_id"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("media_intel_runs.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    #: session-local label (FT_00) -- anonymous by construction.
    track_id: Mapped[str] = mapped_column(String(20), default="", server_default=text("''"))
    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float] = mapped_column(Float, default=0.0)
    confidence_max: Mapped[float | None] = mapped_column(Float, nullable=True)
    sample_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    truncated: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
    reentry_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class FaceTrackSample(Base, PKMixin, TimestampMixin):
    """One sampled detection box of a face track (append-only).

    ``track_id`` references the ``face_tracks`` ROW (its primary key);
    ``track_label`` denormalises the ``FT_00`` session label so a consumer can
    group samples without a join. ``landmarks_json`` is filled ONLY when the
    provider actually returned landmarks -- NULL otherwise, never synthesised.
    """

    __tablename__ = "face_track_samples"
    __table_args__ = (
        Index("ix_face_samples_track", "track_id"),
        Index("ix_face_samples_run_t", "run_id", "t_s"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("media_intel_runs.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    track_id: Mapped[str] = mapped_column(
        ForeignKey("face_tracks.id", ondelete="CASCADE")
    )
    track_label: Mapped[str] = mapped_column(String(20), default="", server_default=text("''"))
    t_s: Mapped[float] = mapped_column(Float, default=0.0)
    x: Mapped[float] = mapped_column(Float, default=0.0)
    y: Mapped[float] = mapped_column(Float, default=0.0)
    w: Mapped[float] = mapped_column(Float, default=0.0)
    h: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    landmarks_json: Mapped[list | None] = mapped_column(JSON, nullable=True)


class MaskAsset(Base, PKMixin, TimestampMixin):
    """A reference to a mask FILE produced under STORAGE_ROOT (never a blob).

    ``mask_asset_id`` points at the ``media_assets`` row of the written file
    (PNG / RLE JSON). When no provider is available there is simply no row: no
    fallback ever writes a bad mask (contracts §9).
    """

    __tablename__ = "mask_assets"
    __table_args__ = (
        Index("ix_mask_assets_run", "run_id"),
        Index("ix_mask_assets_ws_input", "workspace_id", "input_asset_id"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("media_intel_runs.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    input_asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    #: e.g. PERSON | OBJECT | BACKGROUND | SUBJECT
    kind: Mapped[str] = mapped_column(String(32), default="", server_default=text("''"))
    mask_asset_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_assets.id", ondelete="SET NULL"), nullable=True
    )
    #: PNG | RLE_JSON
    format: Mapped[str] = mapped_column(String(16), default="PNG", server_default=text("'PNG'"))
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    area_ratio: Mapped[float | None] = mapped_column(Float, nullable=True)
    provider_key: Mapped[str] = mapped_column(String(60), default="", server_default=text("''"))
    model_version: Mapped[str] = mapped_column(String(120), default="", server_default=text("''"))
    checksum: Mapped[str] = mapped_column(String(128), default="", server_default=text("''"))


class ActiveSpeakerMap(Base, PKMixin, TimestampMixin):
    """One speaker <-> face window (contracts §10).

    ``status='RESOLVED'`` requires sufficient evidence (overlap ratio plus
    optional motion support above threshold). Tied or thin evidence is written
    as ``UNRESOLVED`` with a machine reason (``no_face_track``,
    ``ambiguous_tie``, ``low_overlap``, ``no_diarization``, ``low_confidence``)
    -- a silent guess is a FAILED run, never a plausible row.
    """

    __tablename__ = "active_speaker_map"
    __table_args__ = (
        Index("ix_asm_run_start", "run_id", "start_s"),
        Index("ix_asm_ws_asset", "workspace_id", "asset_id"),
    )

    run_id: Mapped[str] = mapped_column(
        ForeignKey("media_intel_runs.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    speaker_id: Mapped[str | None] = mapped_column(String(20), nullable=True)
    face_track_id: Mapped[str | None] = mapped_column(
        ForeignKey("face_tracks.id", ondelete="CASCADE"), nullable=True
    )
    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float] = mapped_column(Float, default=0.0)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="UNRESOLVED", server_default=text("'UNRESOLVED'"), index=True
    )
    reason: Mapped[str] = mapped_column(String(60), default="", server_default=text("''"))


class EditProposal(Base, PKMixin, TimestampMixin):
    """A proposed (never auto-applied) cut.

    ``status='PROPOSED'`` is the default and ``policy.auto_apply`` is OFF by
    default, so nothing is ever applied without a human decision (contracts §7).
    ``ops_json`` holds the canonical Work 02 operations the apply step would
    submit -- the ONLY way a silence/filler edit reaches a timeline.
    """

    __tablename__ = "edit_proposals"
    __table_args__ = (
        Index("ix_edit_prop_ws_status", "workspace_id", "status"),
        Index("ix_edit_prop_asset", "asset_id"),
        Index("ix_edit_prop_run", "run_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    project_id: Mapped[str | None] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("media_intel_runs.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(
        String(20), default="KEEP", server_default=text("'KEEP'")
    )
    start_s: Mapped[float] = mapped_column(Float, default=0.0)
    end_s: Mapped[float] = mapped_column(Float, default=0.0)
    reason: Mapped[str] = mapped_column(String(60), default="", server_default=text("''"))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    status: Mapped[str] = mapped_column(
        String(16), default="PROPOSED", server_default=text("'PROPOSED'")
    )
    decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    ops_json: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))
    decided_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class AudioTimeMap(Base, PKMixin, TimestampMixin):
    """Source <-> edited time mapping for one asset + policy.

    ``segments_json`` is the ordered
    ``[{src_start, src_end, out_start, out_end}, ...]`` list that captions,
    scenes and the UI re-time with (contracts §7).
    """

    __tablename__ = "audio_time_maps"
    __table_args__ = (
        Index("ix_time_map_ws_asset", "workspace_id", "asset_id"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    policy_id: Mapped[str] = mapped_column(String(64), default="", server_default=text("''"))
    segments_json: Mapped[list] = mapped_column(
        JSON, default=list, server_default=text("'[]'")
    )
    created_by: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class ReframePlan(Base, PKMixin, TimestampMixin):
    """A smart-reframe / layout plan (crop is never baked into renders).

    ``layout`` is one of the contracts §11 layouts
    (ACTIVE_SPEAKER, SPLIT_SCREEN, TWO_SHOT, GRID, HOST_GUEST,
    PODCAST_DYNAMIC); ``aspect`` is e.g. "9:16"; ``strategy`` records the
    priority that produced the keyframes.
    """

    __tablename__ = "reframe_plans"
    __table_args__ = (
        Index("ix_reframe_plans_run", "run_id"),
        Index("ix_reframe_plans_ws_source", "workspace_id", "source_asset_id"),
    )

    run_id: Mapped[str | None] = mapped_column(
        ForeignKey("media_intel_runs.id", ondelete="CASCADE"), nullable=True, index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    source_asset_id: Mapped[str] = mapped_column(
        ForeignKey("media_assets.id", ondelete="CASCADE"), index=True
    )
    layout: Mapped[str] = mapped_column(
        String(32), default="ACTIVE_SPEAKER", server_default=text("'ACTIVE_SPEAKER'")
    )
    aspect: Mapped[str] = mapped_column(String(12), default="9:16", server_default=text("'9:16'"))
    strategy: Mapped[str] = mapped_column(String(40), default="", server_default=text("''"))
    meta_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))


class ReframeKeyframe(Base, PKMixin, TimestampMixin):
    """One EDITABLE crop keyframe of a plan (contracts §11).

    ``source`` records which priority produced it (active_speaker | salient_face
    | focal_point | fallback_crop | operator), ``reason`` the machine code, and
    ``confidence`` the evidence strength. Operators may edit any row; an
    operator edit is recorded by ``source='operator'`` and is never overwritten
    by a re-plan.
    """

    __tablename__ = "reframe_keyframes"
    __table_args__ = (
        Index("ix_reframe_kf_plan_t", "plan_id", "t_s"),
        Index("ix_reframe_kf_ws", "workspace_id"),
    )

    plan_id: Mapped[str] = mapped_column(
        ForeignKey("reframe_plans.id", ondelete="CASCADE"), index=True
    )
    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    t_s: Mapped[float] = mapped_column(Float, default=0.0)
    x: Mapped[float] = mapped_column(Float, default=0.0)
    y: Mapped[float] = mapped_column(Float, default=0.0)
    scale: Mapped[float] = mapped_column(Float, default=1.0)
    rect_json: Mapped[dict] = mapped_column(JSON, default=dict, server_default=text("'{}'"))
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    reason: Mapped[str] = mapped_column(String(60), default="", server_default=text("''"))
    source: Mapped[str] = mapped_column(String(40), default="", server_default=text("''"))


class IntelQCResult(Base, PKMixin, TimestampMixin):
    """One QC verdict (audio | visual) with its per-check detail (append-only).

    ``verdict`` is one of PASS | PASS_WITH_WARNINGS | REVIEW_REQUIRED | FAIL;
    ``checks_json`` is the ``[{name, status, detail, measured, threshold}, ...]``
    evidence behind it. Hard violations are FAIL, heuristic-only evidence or a
    policy override is REVIEW_REQUIRED, minor issues are PASS_WITH_WARNINGS
    (contracts §13).
    """

    __tablename__ = "intel_qc_results"
    __table_args__ = (
        Index("ix_qc_run", "run_id"),
        Index("ix_qc_ws_kind", "workspace_id", "kind"),
    )

    workspace_id: Mapped[str] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    run_id: Mapped[str] = mapped_column(
        ForeignKey("media_intel_runs.id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(16), default="audio", server_default=text("'audio'"))
    verdict: Mapped[str] = mapped_column(
        String(24), default="PASS", server_default=text("'PASS'")
    )
    checks_json: Mapped[list] = mapped_column(JSON, default=list, server_default=text("'[]'"))


__all__ = [
    "ACTIVE_SPEAKER_STATUSES",
    "ActiveSpeakerMap",
    "AudioTimeMap",
    "CHUNK_STATUSES",
    "DiarizationSegment",
    "EditProposal",
    "FaceTrack",
    "FaceTrackSample",
    "IntelQCResult",
    "MaskAsset",
    "MediaIntelCache",
    "MediaIntelChunk",
    "MediaIntelRun",
    "MediaIntelWord",
    "PROPOSAL_DECISIONS",
    "PROPOSAL_KINDS",
    "PROPOSAL_STATUSES",
    "QC_KINDS",
    "QC_VERDICTS",
    "RETRYABLE_RUN_STATUSES",
    "RUN_STATUSES",
    "ReframeKeyframe",
    "ReframePlan",
    "SEGMENT_KINDS",
    "SpeakerAlias",
    "TERMINAL_RUN_STATUSES",
]

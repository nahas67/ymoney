"""Upgrade 0029: media intelligence foundation (Work 12 Lane A).

Tables (mirrors app/models/media_intel.py):

  media_intel_runs, media_intel_cache, media_intel_chunks,
  diarization_segments, media_intel_words, speaker_aliases, face_tracks,
  face_track_samples, mask_assets, active_speaker_map, edit_proposals,
  audio_time_maps, reframe_plans, reframe_keyframes, intel_qc_results

plus two ADDITIVE columns on the existing ``media_assets`` table
(``parent_asset_id``, ``derivation_json``) for derived-asset lineage.

Append-only and idempotent: ``CREATE TABLE IF NOT EXISTS`` + guarded
``CREATE INDEX IF NOT EXISTS``, so it replays as a no-op even when the ORM
already created the tables (``Base.metadata.create_all`` runs first in the
runner). No existing table is dropped or rewritten; the only mutation of an
older table is two guarded ``ALTER TABLE ... ADD COLUMN`` calls.

Two documented deviations from the contracts §2 sketch:

  * every table also carries ``updated_at`` because all 15 models use the
    mandated ``PKMixin`` + ``TimestampMixin`` pair (same note as 0028);
  * ``derivation_json`` is added as ``NOT NULL DEFAULT '{}'`` (the sketch said
    NULL) so it matches the ORM and every asset row has a lineage dict.

The additive-column guard now goes through the shared ``app.migrations.ddl``
helper instead of a second hand-rolled ``inspector.get_columns`` check. The old
inline guard was already a catalog read rather than a ``try/except`` around the
ALTER, so it was replay-safe on both backends -- but on SQLite a failed
statement leaves the transaction usable, which is what made the ``try/except``
idiom that other migrations here used look safe. On PostgreSQL it ABORTS the
transaction and every later statement fails with 25P02. A catalog read cannot
fail, so nothing is caught and nothing is poisoned.

Every index column below was checked against the ORM in ``app/models/media_intel.py``.
That check is not cosmetic: SQLite short-circuits ``CREATE INDEX IF NOT EXISTS``
on the index NAME and never validates the column list, so a wrong column
spelling silently did nothing, while PostgreSQL resolves columns first and
raises 42703.
"""

from __future__ import annotations

from app.migrations.ddl import add_columns_if_missing, table_exists


def upgrade(session) -> None:
    from sqlalchemy import text

    # -- additive media_assets columns (guarded: create_all may have made them)
    if table_exists(session, "media_assets"):
        add_columns_if_missing(
            session,
            "media_assets",
            [
                ("parent_asset_id", "VARCHAR(36)"),
                ("derivation_json", "JSON NOT NULL DEFAULT '{}'"),
            ],
        )
        session.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_media_assets_parent_asset_id "
                "ON media_assets (parent_asset_id)"
            )
        )

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS media_intel_runs (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            asset_id VARCHAR(36) NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            kind VARCHAR(40) NOT NULL DEFAULT '',
            provider_key VARCHAR(60) NOT NULL DEFAULT '',
            model_version VARCHAR(120) NOT NULL DEFAULT '',
            params_json JSON NOT NULL DEFAULT '{}',
            params_hash VARCHAR(64) NOT NULL DEFAULT '',
            asset_checksum VARCHAR(128) NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
            progress INTEGER NOT NULL DEFAULT 0,
            chunks_total INTEGER NOT NULL DEFAULT 0,
            chunks_done INTEGER NOT NULL DEFAULT 0,
            cancel_requested BOOLEAN NOT NULL DEFAULT FALSE,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            processing_ms INTEGER NOT NULL DEFAULT 0,
            gpu_ms INTEGER NOT NULL DEFAULT 0,
            cost_micros INTEGER NOT NULL DEFAULT 0,
            warnings_json JSON NOT NULL DEFAULT '[]',
            metrics_json JSON NOT NULL DEFAULT '{}',
            error_code VARCHAR(60) NOT NULL DEFAULT '',
            output_asset_id VARCHAR(36) REFERENCES media_assets(id) ON DELETE SET NULL,
            requested_by VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_media_intel_runs_workspace_id ON media_intel_runs (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_media_intel_runs_asset_id ON media_intel_runs (asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_media_intel_runs_status ON media_intel_runs (status)",
        "CREATE INDEX IF NOT EXISTS ix_media_intel_runs_created_at ON media_intel_runs (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_mi_runs_ws_status ON media_intel_runs (workspace_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_mi_runs_ws_created ON media_intel_runs (workspace_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_mi_runs_ws_kind ON media_intel_runs (workspace_id, kind)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS media_intel_cache (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            cache_key VARCHAR(64) NOT NULL,
            run_id VARCHAR(36) NOT NULL REFERENCES media_intel_runs(id) ON DELETE CASCADE,
            asset_checksum VARCHAR(128) NOT NULL DEFAULT '',
            provider_key VARCHAR(60) NOT NULL DEFAULT '',
            model_version VARCHAR(120) NOT NULL DEFAULT '',
            params_hash VARCHAR(64) NOT NULL DEFAULT '',
            CONSTRAINT uq_mi_cache_ws_key UNIQUE (workspace_id, cache_key)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_media_intel_cache_workspace_id ON media_intel_cache (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_media_intel_cache_cache_key ON media_intel_cache (cache_key)",
        "CREATE INDEX IF NOT EXISTS ix_media_intel_cache_created_at ON media_intel_cache (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_mi_cache_run ON media_intel_cache (run_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS media_intel_chunks (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            run_id VARCHAR(36) NOT NULL REFERENCES media_intel_runs(id) ON DELETE CASCADE,
            idx INTEGER NOT NULL DEFAULT 0,
            start_s FLOAT NOT NULL DEFAULT 0,
            end_s FLOAT NOT NULL DEFAULT 0,
            input_checksum VARCHAR(64) NOT NULL DEFAULT '',
            status VARCHAR(20) NOT NULL DEFAULT 'PENDING',
            CONSTRAINT uq_mi_chunk_run_idx UNIQUE (run_id, idx)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_media_intel_chunks_run_id ON media_intel_chunks (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_media_intel_chunks_created_at ON media_intel_chunks (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_mi_chunks_run_status ON media_intel_chunks (run_id, status)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS diarization_segments (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            run_id VARCHAR(36) NOT NULL REFERENCES media_intel_runs(id) ON DELETE CASCADE,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            asset_id VARCHAR(36) NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            speaker_id VARCHAR(20),
            kind VARCHAR(20) NOT NULL DEFAULT 'SPEAKER',
            start_s FLOAT NOT NULL DEFAULT 0,
            end_s FLOAT NOT NULL DEFAULT 0,
            confidence FLOAT
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_diarization_segments_run_id ON diarization_segments (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_diarization_segments_workspace_id ON diarization_segments (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_diarization_segments_asset_id ON diarization_segments (asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_diarization_segments_created_at ON diarization_segments (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_diar_run_start ON diarization_segments (run_id, start_s)",
        "CREATE INDEX IF NOT EXISTS ix_diar_ws_asset ON diarization_segments (workspace_id, asset_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS media_intel_words (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            run_id VARCHAR(36) NOT NULL REFERENCES media_intel_runs(id) ON DELETE CASCADE,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            asset_id VARCHAR(36) NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            idx INTEGER NOT NULL DEFAULT 0,
            word VARCHAR(200) NOT NULL DEFAULT '',
            start_s FLOAT NOT NULL DEFAULT 0,
            end_s FLOAT NOT NULL DEFAULT 0,
            speaker_id VARCHAR(20),
            confidence FLOAT,
            CONSTRAINT uq_mi_word_run_idx UNIQUE (run_id, idx)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_media_intel_words_run_id ON media_intel_words (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_media_intel_words_workspace_id ON media_intel_words (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_media_intel_words_asset_id ON media_intel_words (asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_media_intel_words_created_at ON media_intel_words (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_mi_words_run_start ON media_intel_words (run_id, start_s)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS speaker_aliases (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            asset_id VARCHAR(36) REFERENCES media_assets(id) ON DELETE CASCADE,
            run_id VARCHAR(36) REFERENCES media_intel_runs(id) ON DELETE CASCADE,
            speaker_id VARCHAR(20) NOT NULL DEFAULT '',
            label VARCHAR(120) NOT NULL DEFAULT '',
            created_by VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_speaker_aliases_workspace_id ON speaker_aliases (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_speaker_aliases_speaker_id ON speaker_aliases (speaker_id)",
        "CREATE INDEX IF NOT EXISTS ix_speaker_aliases_created_at ON speaker_aliases (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_sp_alias_ws_speaker ON speaker_aliases (workspace_id, speaker_id)",
        "CREATE INDEX IF NOT EXISTS ix_sp_alias_asset ON speaker_aliases (asset_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS face_tracks (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            run_id VARCHAR(36) NOT NULL REFERENCES media_intel_runs(id) ON DELETE CASCADE,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            asset_id VARCHAR(36) NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            track_id VARCHAR(20) NOT NULL DEFAULT '',
            start_s FLOAT NOT NULL DEFAULT 0,
            end_s FLOAT NOT NULL DEFAULT 0,
            confidence_max FLOAT,
            sample_count INTEGER NOT NULL DEFAULT 0,
            truncated BOOLEAN NOT NULL DEFAULT FALSE,
            reentry_count INTEGER NOT NULL DEFAULT 0,
            CONSTRAINT uq_face_track_run_label UNIQUE (run_id, track_id)
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_face_tracks_run_id ON face_tracks (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_face_tracks_workspace_id ON face_tracks (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_face_tracks_asset_id ON face_tracks (asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_face_tracks_created_at ON face_tracks (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_face_tracks_run_start ON face_tracks (run_id, start_s)",
        "CREATE INDEX IF NOT EXISTS ix_face_tracks_ws_asset ON face_tracks (workspace_id, asset_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS face_track_samples (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            run_id VARCHAR(36) NOT NULL REFERENCES media_intel_runs(id) ON DELETE CASCADE,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            track_id VARCHAR(36) NOT NULL REFERENCES face_tracks(id) ON DELETE CASCADE,
            track_label VARCHAR(20) NOT NULL DEFAULT '',
            t_s FLOAT NOT NULL DEFAULT 0,
            x FLOAT NOT NULL DEFAULT 0,
            y FLOAT NOT NULL DEFAULT 0,
            w FLOAT NOT NULL DEFAULT 0,
            h FLOAT NOT NULL DEFAULT 0,
            confidence FLOAT,
            landmarks_json JSON
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_face_track_samples_run_id ON face_track_samples (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_face_track_samples_workspace_id ON face_track_samples (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_face_track_samples_created_at ON face_track_samples (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_face_samples_track ON face_track_samples (track_id)",
        "CREATE INDEX IF NOT EXISTS ix_face_samples_run_t ON face_track_samples (run_id, t_s)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS mask_assets (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            run_id VARCHAR(36) NOT NULL REFERENCES media_intel_runs(id) ON DELETE CASCADE,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            input_asset_id VARCHAR(36) NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            kind VARCHAR(32) NOT NULL DEFAULT '',
            mask_asset_id VARCHAR(36) REFERENCES media_assets(id) ON DELETE SET NULL,
            format VARCHAR(16) NOT NULL DEFAULT 'PNG',
            width INTEGER,
            height INTEGER,
            area_ratio FLOAT,
            provider_key VARCHAR(60) NOT NULL DEFAULT '',
            model_version VARCHAR(120) NOT NULL DEFAULT '',
            checksum VARCHAR(128) NOT NULL DEFAULT ''
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_mask_assets_run_id ON mask_assets (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_mask_assets_workspace_id ON mask_assets (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_mask_assets_input_asset_id ON mask_assets (input_asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_mask_assets_created_at ON mask_assets (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_mask_assets_ws_input ON mask_assets (workspace_id, input_asset_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS active_speaker_map (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            run_id VARCHAR(36) NOT NULL REFERENCES media_intel_runs(id) ON DELETE CASCADE,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            asset_id VARCHAR(36) NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            speaker_id VARCHAR(20),
            face_track_id VARCHAR(36) REFERENCES face_tracks(id) ON DELETE CASCADE,
            start_s FLOAT NOT NULL DEFAULT 0,
            end_s FLOAT NOT NULL DEFAULT 0,
            confidence FLOAT,
            status VARCHAR(16) NOT NULL DEFAULT 'UNRESOLVED',
            reason VARCHAR(60) NOT NULL DEFAULT ''
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_active_speaker_map_run_id ON active_speaker_map (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_active_speaker_map_workspace_id ON active_speaker_map (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_active_speaker_map_asset_id ON active_speaker_map (asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_active_speaker_map_status ON active_speaker_map (status)",
        "CREATE INDEX IF NOT EXISTS ix_active_speaker_map_created_at ON active_speaker_map (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_asm_run_start ON active_speaker_map (run_id, start_s)",
        "CREATE INDEX IF NOT EXISTS ix_asm_ws_asset ON active_speaker_map (workspace_id, asset_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS edit_proposals (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            project_id VARCHAR(36) REFERENCES projects(id) ON DELETE CASCADE,
            asset_id VARCHAR(36) NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            run_id VARCHAR(36) NOT NULL REFERENCES media_intel_runs(id) ON DELETE CASCADE,
            kind VARCHAR(20) NOT NULL DEFAULT 'KEEP',
            start_s FLOAT NOT NULL DEFAULT 0,
            end_s FLOAT NOT NULL DEFAULT 0,
            reason VARCHAR(60) NOT NULL DEFAULT '',
            confidence FLOAT,
            status VARCHAR(16) NOT NULL DEFAULT 'PROPOSED',
            decision VARCHAR(16),
            ops_json JSON NOT NULL DEFAULT '[]',
            decided_by VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL,
            decided_at TIMESTAMP
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_edit_proposals_workspace_id ON edit_proposals (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_edit_proposals_asset_id ON edit_proposals (asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_edit_proposals_run_id ON edit_proposals (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_edit_proposals_created_at ON edit_proposals (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_edit_prop_ws_status ON edit_proposals (workspace_id, status)",
        "CREATE INDEX IF NOT EXISTS ix_edit_prop_asset ON edit_proposals (asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_edit_prop_run ON edit_proposals (run_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS audio_time_maps (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            asset_id VARCHAR(36) NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            policy_id VARCHAR(64) NOT NULL DEFAULT '',
            segments_json JSON NOT NULL DEFAULT '[]',
            created_by VARCHAR(36) REFERENCES users(id) ON DELETE SET NULL
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_audio_time_maps_workspace_id ON audio_time_maps (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_audio_time_maps_asset_id ON audio_time_maps (asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_audio_time_maps_created_at ON audio_time_maps (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_time_map_ws_asset ON audio_time_maps (workspace_id, asset_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS reframe_plans (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            run_id VARCHAR(36) REFERENCES media_intel_runs(id) ON DELETE CASCADE,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            source_asset_id VARCHAR(36) NOT NULL REFERENCES media_assets(id) ON DELETE CASCADE,
            layout VARCHAR(32) NOT NULL DEFAULT 'ACTIVE_SPEAKER',
            aspect VARCHAR(12) NOT NULL DEFAULT '9:16',
            strategy VARCHAR(40) NOT NULL DEFAULT '',
            meta_json JSON NOT NULL DEFAULT '{}'
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_reframe_plans_run_id ON reframe_plans (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_reframe_plans_workspace_id ON reframe_plans (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_reframe_plans_source_asset_id ON reframe_plans (source_asset_id)",
        "CREATE INDEX IF NOT EXISTS ix_reframe_plans_created_at ON reframe_plans (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_reframe_plans_ws_source ON reframe_plans (workspace_id, source_asset_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS reframe_keyframes (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            plan_id VARCHAR(36) NOT NULL REFERENCES reframe_plans(id) ON DELETE CASCADE,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            t_s FLOAT NOT NULL DEFAULT 0,
            x FLOAT NOT NULL DEFAULT 0,
            y FLOAT NOT NULL DEFAULT 0,
            scale FLOAT NOT NULL DEFAULT 1,
            rect_json JSON NOT NULL DEFAULT '{}',
            confidence FLOAT,
            reason VARCHAR(60) NOT NULL DEFAULT '',
            source VARCHAR(40) NOT NULL DEFAULT ''
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_reframe_keyframes_plan_id ON reframe_keyframes (plan_id)",
        "CREATE INDEX IF NOT EXISTS ix_reframe_keyframes_workspace_id ON reframe_keyframes (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_reframe_keyframes_created_at ON reframe_keyframes (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_reframe_kf_plan_t ON reframe_keyframes (plan_id, t_s)",
        "CREATE INDEX IF NOT EXISTS ix_reframe_kf_ws ON reframe_keyframes (workspace_id)",
    ):
        session.execute(text(stmt))

    session.execute(text("""
        CREATE TABLE IF NOT EXISTS intel_qc_results (
            id VARCHAR(36) PRIMARY KEY,
            created_at TIMESTAMP NOT NULL,
            updated_at TIMESTAMP NOT NULL,
            workspace_id VARCHAR(36) NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
            run_id VARCHAR(36) NOT NULL REFERENCES media_intel_runs(id) ON DELETE CASCADE,
            kind VARCHAR(16) NOT NULL DEFAULT 'audio',
            verdict VARCHAR(24) NOT NULL DEFAULT 'PASS',
            checks_json JSON NOT NULL DEFAULT '[]'
        )
    """))
    for stmt in (
        "CREATE INDEX IF NOT EXISTS ix_intel_qc_results_workspace_id ON intel_qc_results (workspace_id)",
        "CREATE INDEX IF NOT EXISTS ix_intel_qc_results_run_id ON intel_qc_results (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_intel_qc_results_created_at ON intel_qc_results (created_at)",
        "CREATE INDEX IF NOT EXISTS ix_qc_run ON intel_qc_results (run_id)",
        "CREATE INDEX IF NOT EXISTS ix_qc_ws_kind ON intel_qc_results (workspace_id, kind)",
    ):
        session.execute(text(stmt))

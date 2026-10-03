"""Speech-intelligence API: alignment, diarization, speakers (Work 12 Lane B).

Mounted once by the orchestrator in ``api/v1/__init__.py``::

    POST   /workspaces/{ws}/media-intel/alignments              member + edit cap
    GET    /workspaces/{ws}/media-intel/alignments/{run_id}     viewer
    GET    /workspaces/{ws}/media-intel/words                   viewer
    POST   /workspaces/{ws}/media-intel/diarization             member + edit cap
    GET    /workspaces/{ws}/media-intel/diarization/{run_id}    viewer
    GET    /workspaces/{ws}/media-intel/speakers                viewer
    GET    /workspaces/{ws}/media-intel/speakers/aliases        viewer
    POST   /workspaces/{ws}/media-intel/speakers/aliases        member + edit cap
    DELETE /workspaces/{ws}/media-intel/speakers/aliases/{id}   member + edit cap

The routes are the WORKER BOUNDARY, not the workers: contracts §1.2 says heavy
models run in a worker, so these routes drive the pure orchestration + measurement
engine in-process (which is what the engines are for) and the ``MEDIA_INTEL_ALIGN``
/ ``MEDIA_INTEL_DIARIZE`` job kinds stay the path for a real worker. Both paths
call the same :mod:`app.engine.intel.alignment` code, so the semantics are
identical.

Rules this module keeps (contracts §14 + the Work 11 conventions):

* Workspace scoping on every route: a foreign run/alias/asset id is **404**,
  never 403, and the 404 is raised BEFORE the capability check so it can never be
  used as an oracle.
* Floors are the existing ``require_workspace_role`` dependencies; the project
  capability is the EXISTING ``edit_timeline`` name from the locked Work 11
  matrix (``services/project_auth`` would reject an invented name with 422).
* **No sensitive inference.** The response vocabulary contains only anonymous
  ``speaker_id`` values and OPERATOR labels. There is no gender, race, age,
  identity or demographic field to fill in -- the shape itself forbids it.
* **Honest emptiness.** A dark capability returns the run in its terminal
  ``UNAVAILABLE`` state with the reason; ``words_available``/``speakers_resolved``
  are computed from stored rows, never assumed.
* **Emission ordering** (contracts §3): the engine never calls
  ``record_event``/``track_cost`` mid-transaction. Each route commits FIRST and
  only then publishes the events the payload surfaced.
* ``{"items": [...]}`` envelopes, hand-written dict DTOs, short details, and a
  generic 500 that logs the detail server-side only.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.config import settings
from app.db import get_db
from app.engine.intel import alignment as speech_engine
from app.models import MediaAsset, SpeakerAlias, User, Workspace
from app.services import media_intel_runs as runs_service
from app.services.auth_service import get_current_user, require_workspace_role
from app.services.events import record_event
from app.services.project_auth import assert_capability

media_intel_speech_router = APIRouter(
    prefix="/workspaces/{workspace_id}/media-intel", tags=["media-intel-speech"]
)
logger = logging.getLogger("ymoney.media_intel")

#: existing project capability a speech mutation needs (Work 11 matrix; NOT a new
#: name -- ``project_auth.assert_capability`` rejects anything else with 422)
MUTATION_CAPABILITY = "edit_timeline"

#: how many events one route may publish (a runaway engine cannot flood the feed)
MAX_EVENTS_PER_RESPONSE = 20


# ---------------------------------------------------------------------------
# error policy (mirrors api/v1/exports.py and api/v1/media_intel.py)
# ---------------------------------------------------------------------------


def _short(exc: BaseException, limit: int = 180) -> str:
    """One-line, truncated message for deliberate domain errors."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 422):
    """Uniform error policy for one route body.

    ``HTTPException`` passes through (401/403/404/409/422); ``ValueError``
    becomes ``value_error`` with a short detail; anything else is logged on
    ``ymoney.media_intel`` and surfaced as a generic 500 that echoes nothing.
    """

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=value_error,
                                    detail=_short(exc)) from None
            except Exception:  # noqa: BLE001 -- deliberate catch-all at the API edge
                logger.exception("media-intel speech route failed: %s",
                                 getattr(fn, "__name__", fn))
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


def _check(db: Session, ws: Workspace, user: User, **kwargs) -> None:
    """``assert_capability`` at the route edge; bad args -> 422, 403/404 pass."""
    try:
        assert_capability(db, ws, user, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=_short(exc)) from None


def _commercial_mode() -> bool:
    """Workspace-wide commercial flag (contracts §1.4), read defensively."""
    return bool(getattr(settings, "commercial_mode", False))


def _load_asset(db: Session, ws: Workspace, asset_id: str) -> MediaAsset:
    """Workspace-scoped asset fetch -> 404 for foreign/missing (never 403)."""
    asset = db.get(MediaAsset, str(asset_id or ""))
    if asset is None or str(asset.workspace_id) != str(ws.id):
        raise HTTPException(status_code=404, detail="asset not found")
    return asset


def _load_run(db: Session, ws: Workspace, run_id: str):
    """Workspace-scoped run fetch -> 404 for foreign/missing (never 403)."""
    row = runs_service.get_run(db, ws.id, str(run_id or ""))
    if row is None:
        raise HTTPException(status_code=404, detail="run not found")
    return row


def _alias_target(db: Session, ws: Workspace, *, asset_id: str, run_id: str,
                  speaker_id: str) -> tuple[str, str]:
    """The ``(target_type, target_id)`` pair to narrow an alias mutation with.

    Prefers the real asset (from the body, else from the referenced run) so a
    project-linked asset keeps its project-role gate. A workspace-wide alias has
    no asset, so it is keyed by its own stable scope; that target simply has no
    project links, which is the documented unlinked fallback in
    ``project_auth`` -- the route's ``member`` floor stays the enforcement line.
    """
    if asset_id:
        return "asset", str(asset_id)
    if run_id:
        run = _load_run(db, ws, run_id)
        if run.asset_id:
            return "asset", str(run.asset_id)
    return "speaker_alias", f"{speaker_id}|{asset_id or ''}|{run_id or ''}"


def _emit(ws_id: str, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Publish the events the engine surfaced -- AFTER ``db.commit()``.

    ``record_event`` opens its own session, so calling it before the commit is
    what produced Lane A's ``OperationalError`` on SQLite (contracts §3). Every
    publish is best-effort: telemetry never fails a request.
    """
    published: list[dict[str, Any]] = []
    for event in list(events or [])[:MAX_EVENTS_PER_RESPONSE]:
        kind = str(event.get("kind") or "")
        if not kind:
            continue
        try:
            record_event(
                ws_id, kind, str(event.get("message") or kind)[:200],
                level=str(event.get("level") or "info"),
                source="media_intel",
                data=dict(event.get("data") or {}),
            )
        except Exception as exc:  # noqa: BLE001 - telemetry must not break a call
            logger.warning("media-intel speech event %s failed: %s", kind,
                           type(exc).__name__)
        published.append({"kind": kind})
    return published


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class AlignmentRequestBody(BaseModel):
    asset_id: str = Field(min_length=1, max_length=36)
    language: str = Field(default="en", max_length=16)
    force: bool = False
    params: dict | None = None


class DiarizationRequestBody(BaseModel):
    asset_id: str = Field(min_length=1, max_length=36)
    force: bool = False
    model: str | None = Field(default=None, max_length=160)
    #: also record the local ffmpeg VAD measurement (kind=SPEECH_ACTIVITY)
    include_speech_activity: bool = True
    params: dict | None = None


class SpeakerAliasBody(BaseModel):
    speaker_id: str = Field(min_length=1, max_length=20)
    label: str = Field(min_length=1, max_length=120)
    asset_id: str | None = Field(default=None, max_length=36)
    run_id: str | None = Field(default=None, max_length=36)


# ---------------------------------------------------------------------------
# alignment (contracts §14)
# ---------------------------------------------------------------------------


@media_intel_speech_router.post("/alignments", summary="Align words for an asset")
@_guard()
def create_alignment(
    body: AlignmentRequestBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    asset = _load_asset(db, ws, body.asset_id)  # 404 first: never a capability oracle
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="asset", target_id=asset.id)
    payload = speech_engine.align_words(
        db, ws, asset,
        language=body.language,
        params=body.params or {},
        commercial_mode=_commercial_mode(),
        force=bool(body.force),
        requested_by=str(user.id) if user else None,
    )
    db.commit()  # durable state first, telemetry second (contracts §3)
    return {**payload, "events": _emit(ws.id, payload.get("events") or [])}


@media_intel_speech_router.get("/alignments/{run_id}", summary="Alignment run + words")
@_guard()
def get_alignment(
    run_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    row = _load_run(db, ws, run_id)
    return speech_engine.run_result(db, ws.id, row)


@media_intel_speech_router.get("/words", summary="Aligned words")
@_guard()
def list_words(
    run_id: str | None = Query(default=None, max_length=36),
    asset_id: str | None = Query(default=None, max_length=36),
    limit: int = Query(default=2000, ge=1, le=20_000),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    if asset_id:
        _load_asset(db, ws, asset_id)
    if run_id:
        _load_run(db, ws, run_id)
    items = speech_engine.list_words(db, ws.id, run_id=run_id, asset_id=asset_id,
                                     limit=limit)
    return {"items": items, "count": len(items)}


# ---------------------------------------------------------------------------
# diarization (contracts §14)
# ---------------------------------------------------------------------------


@media_intel_speech_router.post("/diarization", summary="Diarize an asset's speakers")
@_guard()
def create_diarization(
    body: DiarizationRequestBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    asset = _load_asset(db, ws, body.asset_id)
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type="asset", target_id=asset.id)
    payload = speech_engine.diarize(
        db, ws, asset,
        model=str(body.model or ""),
        include_speech_activity=bool(body.include_speech_activity),
        params=body.params or {},
        commercial_mode=_commercial_mode(),
        force=bool(body.force),
        requested_by=str(user.id) if user else None,
    )
    db.commit()
    return {**payload, "events": _emit(ws.id, payload.get("events") or [])}


@media_intel_speech_router.get("/diarization/{run_id}", summary="Diarization run + segments")
@_guard()
def get_diarization(
    run_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    row = _load_run(db, ws, run_id)
    return speech_engine.run_result(db, ws.id, row)


# ---------------------------------------------------------------------------
# speakers + operator aliases (contracts §5)
# ---------------------------------------------------------------------------


@media_intel_speech_router.get("/speakers", summary="Anonymous speakers for an asset")
@_guard()
def list_speakers(
    asset_id: str | None = Query(default=None, max_length=36),
    run_id: str | None = Query(default=None, max_length=36),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    if asset_id:
        _load_asset(db, ws, asset_id)
    if run_id:
        _load_run(db, ws, run_id)
    items = speech_engine.list_speakers(db, ws.id, asset_id=asset_id, run_id=run_id)
    return {
        "items": items,
        "count": len(items),
        "anonymous": True,
        "note": "speaker_id is an anonymous per-run label; names are operator aliases",
    }


@media_intel_speech_router.get("/speakers/aliases", summary="Operator speaker labels")
@_guard()
def list_speaker_aliases(
    asset_id: str | None = Query(default=None, max_length=36),
    run_id: str | None = Query(default=None, max_length=36),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db: Session = Depends(get_db),
):
    items = speech_engine.list_aliases(db, ws.id, asset_id=asset_id, run_id=run_id)
    return {"items": items, "count": len(items)}


@media_intel_speech_router.post("/speakers/aliases", summary="Name an anonymous speaker")
@_guard()
def create_speaker_alias(
    body: SpeakerAliasBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    # Both scopes are validated as 404 BEFORE the capability check.
    if body.asset_id:
        _load_asset(db, ws, body.asset_id)
    if body.run_id:
        _load_run(db, ws, body.run_id)
    target_type, target_id = _alias_target(
        db, ws, asset_id=body.asset_id, run_id=body.run_id, speaker_id=body.speaker_id
    )
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type=target_type, target_id=target_id)
    row = speech_engine.create_alias(
        db, ws, speaker_id=body.speaker_id, label=body.label,
        asset_id=body.asset_id, run_id=body.run_id,
        created_by=str(user.id) if user else None,
    )
    db.commit()
    _emit(ws.id, [{
        "kind": speech_engine.EVENT_SPEAKER_ALIAS_SET,
        "message": f"speaker alias set for {row['speaker_id']}",
        "data": {"speaker_id": row["speaker_id"], "alias_id": row["id"],
                 "asset_id": row["asset_id"], "run_id": row["run_id"]},
    }])
    return row


@media_intel_speech_router.delete("/speakers/aliases/{alias_id}",
                                  summary="Remove an operator speaker label")
@_guard()
def delete_speaker_alias(
    alias_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    row = db.get(SpeakerAlias, str(alias_id or ""))
    if row is None or str(row.workspace_id) != str(ws.id):
        raise HTTPException(status_code=404, detail="alias not found")
    target_type, target_id = _alias_target(
        db, ws, asset_id=str(row.asset_id or ""), run_id=str(row.run_id or ""),
        speaker_id=str(row.speaker_id or ""),
    )
    _check(db, ws, user, capability=MUTATION_CAPABILITY,
           target_type=target_type, target_id=target_id)
    speaker_id = str(row.speaker_id or "")
    if not speech_engine.delete_alias(db, ws.id, alias_id):
        raise HTTPException(status_code=404, detail="alias not found")
    db.commit()
    _emit(ws.id, [{
        "kind": speech_engine.EVENT_SPEAKER_ALIAS_REMOVED,
        "message": f"speaker alias removed for {speaker_id}",
        "data": {"speaker_id": speaker_id, "alias_id": str(alias_id)},
    }])
    return {"deleted": True, "id": str(alias_id), "speaker_id": speaker_id}


__all__ = [
    "MAX_EVENTS_PER_RESPONSE",
    "MUTATION_CAPABILITY",
    "media_intel_speech_router",
]

"""Creative Director HTTP surface (Work 08 Lane B).

Routes (all workspace-scoped; a row/timeline from another workspace is 404,
never a hint that it exists):

    POST /workspaces/{ws}/creative/parse            NL → typed commands (no mutation)
    POST /workspaces/{ws}/creative/preview          diff + estimates (timeline untouched)
    POST /workspaces/{ws}/creative/apply            versioned apply (409 when stale)
    POST /workspaces/{ws}/creative/undo/{timeline}  restore the prior version
    GET  /workspaces/{ws}/creative/commands         audit ledger
    GET  /workspaces/{ws}/creative/catalog          approved commands + components
    POST /workspaces/{ws}/creative/validate-schema  generative UI schema gate (422)

Safety rails enforced here (never left to the UI):
  * an unknown component / command type (``EvalJS``, ``ScriptTag``) is rejected
    with 422 by ``validate-schema`` — the document is never evaluated;
  * parse/preview require `viewer`, apply/undo require `member`;
  * a stale preview (timeline version moved, or manually edited since preview)
    refuses with 409 REVIEW_REQUIRED, and a cross-workspace target is 404.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_db
from app.engine.creative.commands import (
    APPROVED_COMPONENTS,
    COMMAND_STATUSES,
    command_catalog,
    commands_to_dicts,
    schema_document_errors,
)
from app.engine.creative.commands import (
    CommandError as PayloadError,
)
from app.engine.creative.director import CreativeDirector, CreativeError, resolve_policy
from app.models import ContentTimeline, CreativeCommandRow, Workspace
from app.services.auth_service import get_current_user, require_workspace_role

creative_router = APIRouter(prefix="/workspaces/{workspace_id}/creative",
                            tags=["creative"])

TimelineDep = Depends(require_workspace_role("viewer"))
MemberDep = Depends(require_workspace_role("member"))


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------


class ParseBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000)
    context: dict = Field(default_factory=dict)
    persist: bool = True


class PreviewBody(BaseModel):
    commands: list = Field(default_factory=list, min_length=1)
    text_input: str = Field(default="", max_length=4000)


class ApplyBody(BaseModel):
    commands: list = Field(default_factory=list)
    preview_id: str | None = None
    base_version: int | None = None
    approve: bool = False
    text_input: str = Field(default="", max_length=4000)


class SchemaBody(BaseModel):
    payload: object = None


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _row_dto(row: CreativeCommandRow) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "timeline_id": row.timeline_id or "",
        "actor": row.actor,
        "source_user_id": row.source_user_id,
        "text_input": row.text_input or "",
        "commands": list(row.commands_json or []),
        "change_set": dict(row.change_set_json or {}),
        "status": row.status,
        "parent_version": row.parent_version,
        "result": dict(row.result_json or {}),
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else "",
    }


def _timeline_404_if_foreign(ws_id: str, db, timeline_id: str) -> None:
    """Isolation: a timeline id outside this workspace is 404, always."""
    if not timeline_id:
        return
    row = db.get(ContentTimeline, timeline_id)
    if row is None or row.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="timeline not found")


def _target_timeline_id(commands: list, context: dict | None = None) -> str:
    for item in commands or []:
        if isinstance(item, dict):
            tid = str((item.get("target") or {}).get("timeline_id") or "")
            if tid:
                return tid
        else:
            tid = str(getattr(item, "timeline_id", "") or "")
            if tid:
                return tid
    return str((context or {}).get("timeline_id") or "")


def _raise(exc: CreativeError) -> None:
    raise HTTPException(status_code=exc.status_code, detail=exc.detail)


# ---------------------------------------------------------------------------
# parse / preview / apply / undo
# ---------------------------------------------------------------------------


@creative_router.post("/parse", summary="Natural language → typed creative commands")
def parse_text(
    body: ParseBody,
    ws: Workspace = TimelineDep,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    _timeline_404_if_foreign(ws.id, db, str((body.context or {}).get("timeline_id") or ""))
    try:
        cmds = CreativeDirector.parse(
            db, ws, body.text, context=dict(body.context or {}),
            actor="user", user_id=user.id, persist=bool(body.persist))
    except PayloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    dicts = commands_to_dicts(cmds)
    source = "deterministic"
    if not dicts:
        source = "none"
    return {"workspace_id": ws.id, "text": body.text, "commands": dicts,
            "total": len(dicts), "source": source, "actor": "user",
            "user_id": user.id}


@creative_router.post("/preview", summary="Diff + estimates (timeline untouched)")
def preview_commands(
    body: PreviewBody,
    ws: Workspace = TimelineDep,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    _timeline_404_if_foreign(ws.id, db, _target_timeline_id(body.commands))
    try:
        change_set = CreativeDirector.preview(
            db, ws, body.commands, actor="user", user_id=user.id,
            text_input=body.text_input)
    except PayloadError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CreativeError as exc:
        _raise(exc)
    change_set["commands"] = commands_to_dicts(body.commands)
    return change_set


@creative_router.post("/apply", summary="Apply commands via the canonical version system")
def apply_commands(
    body: ApplyBody,
    ws: Workspace = MemberDep,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    if body.preview_id:
        record = db.get(CreativeCommandRow, body.preview_id)
        if record is None or record.workspace_id != ws.id:
            raise HTTPException(status_code=404, detail="preview not found")
    else:
        record = None
    timeline_id = _target_timeline_id(body.commands) or (
        str(record.timeline_id) if record is not None else "")
    _timeline_404_if_foreign(ws.id, db, timeline_id)
    try:
        result = CreativeDirector.apply(
            db, ws, body.commands, actor="user", user_id=user.id,
            approve=bool(body.approve), base_version=body.base_version,
            preview_record_id=body.preview_id, text_input=body.text_input)
    except PayloadError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CreativeError as exc:
        db.rollback()
        _raise(exc)
    return result


@creative_router.post("/undo/{timeline_id}", summary="Restore the version apply replaced")
def undo_timeline(
    timeline_id: str,
    ws: Workspace = MemberDep,
    user=Depends(get_current_user),
    db=Depends(get_db),
):
    _timeline_404_if_foreign(ws.id, db, timeline_id)
    try:
        return CreativeDirector.undo(db, ws, timeline_id, actor="user", user_id=user.id)
    except PayloadError as exc:
        db.rollback()
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CreativeError as exc:
        db.rollback()
        _raise(exc)


# ---------------------------------------------------------------------------
# audit + catalog + schema gate
# ---------------------------------------------------------------------------


@creative_router.get("/commands", summary="Audit ledger (parse/preview/apply history)")
def list_commands(
    ws: Workspace = TimelineDep,
    db=Depends(get_db),
    status_filter: str | None = Query(default=None, alias="status"),
    timeline_id: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
):
    q = select(CreativeCommandRow).where(CreativeCommandRow.workspace_id == ws.id)
    if status_filter:
        if status_filter not in COMMAND_STATUSES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown status '{status_filter}' "
                       f"(expected one of {list(COMMAND_STATUSES)})")
        q = q.where(CreativeCommandRow.status == status_filter)
    if timeline_id:
        q = q.where(CreativeCommandRow.timeline_id == timeline_id)
    rows = db.scalars(
        q.order_by(CreativeCommandRow.created_at.desc(),
                   CreativeCommandRow.id.desc()).limit(limit)).all()
    return {"total": len(rows), "items": [_row_dto(r) for r in rows]}


@creative_router.get("/catalog", summary="Approved command types + component catalog")
def get_catalog(
    ws: Workspace = TimelineDep,
    db=Depends(get_db),
):
    policy = resolve_policy(db, ws)
    return {**command_catalog(policy), "workspace_id": ws.id}


@creative_router.post("/validate-schema", summary="Generative-UI schema gate (never executed)")
def validate_schema(
    body: SchemaBody,
    ws: Workspace = TimelineDep,
    db=Depends(get_db),
):
    errors = schema_document_errors(body.payload)
    if errors:
        # the document is rejected before anything could render or run it
        raise HTTPException(status_code=422,
                            detail={"error": "SCHEMA_REJECTED", "errors": errors})
    return {"ok": True, "errors": [], "components": list(APPROVED_COMPONENTS),
            "commands": list(command_catalog(resolve_policy(db, ws))["commands"])}


__all__ = ["creative_router"]

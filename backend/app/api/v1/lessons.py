"""Scoped performance lessons API (Work 06 Lane C).

- GET    /workspaces/{id}/lessons (filter by scope dims / status / kind)
- POST   /workspaces/{id}/lessons/{lesson_id}/disable
- GET    /workspaces/{id}/lessons/{lesson_id}/evidence
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select

from app.db import get_db
from app.engine.performance.learning import effective_status, is_lesson_row, normalize_scope
from app.models import LearningPattern, Workspace
from app.services.auth_service import require_workspace_role

lessons_router = APIRouter(prefix="/workspaces/{workspace_id}/lessons", tags=["lessons"])


def _lesson_dto(row: LearningPattern) -> dict:
    ev = dict(row.evidence_json or {})
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "pattern_key": row.pattern_key,
        "metric": ev.get("metric", ""),
        "description": row.description,
        "scope": ev.get("scope") or {},
        "effect": ev.get("effect") or {},
        "confidence": row.confidence,
        "sample_size": row.sample_size,
        "evidence_ids": list(ev.get("evidence_ids") or []),
        "status": effective_status(row),
        "generation": int(ev.get("generation") or 1),
        "created_at": ev.get("created_at", ""),
        "last_validated_at": ev.get("last_validated_at", ""),
        "active": row.active,
    }


def _matches_filters(row: LearningPattern, scope_filter: dict, status: str, kind: str) -> bool:
    ev = dict(row.evidence_json or {})
    if scope_filter:
        lesson_scope = dict(ev.get("scope") or {})
        for dim, val in scope_filter.items():
            if val and str(lesson_scope.get(dim) or "") != val:
                return False
    if status and effective_status(row) != status:
        return False
    if kind:
        lesson_kinds = ((ev.get("effect") or {}).get("kinds")) or []
        if lesson_kinds and kind not in lesson_kinds:
            return False
    return True


@lessons_router.get("", summary="List scoped performance lessons")
def list_lessons(
    platform: str | None = Query(default=None, max_length=40),
    topic: str | None = Query(default=None, max_length=120),
    content_format: str | None = Query(default=None, max_length=40),
    status: str | None = Query(default=None, max_length=20),
    kind: str | None = Query(default=None, max_length=20),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    scope_filter = normalize_scope({
        "platform": platform or "",
        "content_format": content_format or "",
        "topic": topic or "",
    })
    scope_filter = {k: v for k, v in scope_filter.items() if v}
    rows = db.scalars(
        select(LearningPattern).where(LearningPattern.workspace_id == ws.id)
        .order_by(LearningPattern.updated_at.desc())
    ).all()
    items = [
        _lesson_dto(r) for r in rows
        if is_lesson_row(r) and _matches_filters(r, scope_filter, status or "", kind or "")
    ]
    return {"items": items}


def _get_lesson(db, ws: Workspace, lesson_id: str) -> LearningPattern:
    row = db.get(LearningPattern, lesson_id)
    if row is None or row.workspace_id != ws.id or not is_lesson_row(row):
        raise HTTPException(status_code=404, detail="lesson not found")
    return row


@lessons_router.get("/{lesson_id}/evidence", summary="Lesson evidence trail")
def lesson_evidence(
    lesson_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    row = _get_lesson(db, ws, lesson_id)
    ev = dict(row.evidence_json or {})
    return {
        "id": row.id,
        "pattern_key": row.pattern_key,
        "metric": ev.get("metric", ""),
        "scope": ev.get("scope") or {},
        "effect": ev.get("effect") or {},
        "evidence_ids": list(ev.get("evidence_ids") or []),
        "sample_size": row.sample_size,
        "confidence": row.confidence,
        "status": effective_status(row),
        "conflicts": list(ev.get("conflicts") or []),
        "supersedes": ev.get("supersedes"),
        "superseded_by": ev.get("superseded_by"),
    }


@lessons_router.post("/{lesson_id}/disable", summary="Disable a lesson (history preserved)")
def disable_lesson(
    lesson_id: str,
    ws: Workspace = Depends(require_workspace_role("admin")),
    db=Depends(get_db),
):
    row = _get_lesson(db, ws, lesson_id)
    row.active = False
    ev = dict(row.evidence_json or {})
    ev["status"] = "disabled"
    row.evidence_json = ev
    db.commit()
    return _lesson_dto(row)

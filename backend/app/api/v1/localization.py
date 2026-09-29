"""Localization endpoints (Work 07 Lane A): runs, status, QC, glossary.

Every route is workspace-scoped: a row from another workspace is a 404, never
a hint that it exists. ``POST /run`` prepares PENDING rows and enqueues the
``localization.run`` job; the handler runs the pipeline with the standard
``jobs.check_cancelled`` cancellation points between stages.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import select

from app.db import get_db, session_scope
from app.engine.localization import LocalizationError, prepare_localizations
from app.engine.localization.pipeline import LocalizationPipeline
from app.models import (
    ContentItem,
    GlossaryTerm,
    LocalizationQCReport,
    LocalizedContent,
    Workspace,
)
from app.models.localization import GLOSSARY_KINDS
from app.providers.dubbing import LANG_LOCALES
from app.services import jobs as jobs_service
from app.services.auth_service import require_workspace_role

localization_router = APIRouter(prefix="/workspaces/{workspace_id}/localization",
                                tags=["localization"])


# ---------------------------------------------------------------------------
# bodies
# ---------------------------------------------------------------------------

class GlossaryBody(BaseModel):
    term: str = Field(min_length=1, max_length=200)
    replacement: str = Field(default="", max_length=200)
    target_languages: list[str] = Field(default_factory=list)
    kind: str = "terminology"
    case_sensitive: bool = False


class RunBody(BaseModel):
    source_content_id: str = Field(min_length=1, max_length=36)
    target_languages: list[str] = Field(min_length=1, max_length=10)
    locales: dict[str, str] = Field(default_factory=dict)
    glossary: list[GlossaryBody] = Field(default_factory=list)
    voice_prefs: dict = Field(default_factory=dict)
    translation_version: int = Field(default=1, ge=1, le=999)
    source_language: str = Field(default="en", max_length=10)


# ---------------------------------------------------------------------------
# dto helpers
# ---------------------------------------------------------------------------

def _qc_summary(db, localized_ids: list[str]) -> dict[str, dict]:
    if not localized_ids:
        return {}
    rows = db.scalars(
        select(LocalizationQCReport)
        .where(LocalizationQCReport.localized_content_id.in_(localized_ids))
        .order_by(LocalizationQCReport.created_at.desc())
    ).all()
    out: dict[str, dict] = {}
    for row in rows:
        if row.localized_content_id in out:
            continue
        checks = dict(row.checks_json or {})
        counts = checks.get("counts") or {}
        out[row.localized_content_id] = {
            "id": row.id, "status": row.status,
            "created_at": row.created_at.isoformat() + "Z",
            "counts": counts,
        }
    return out


def _dto(row: LocalizedContent, qc: dict | None = None) -> dict:
    lineage = dict(row.lineage_json or {})
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "source_content_id": row.source_content_id,
        "child_content_id": row.child_content_id,
        "timeline_id": row.timeline_id,
        "language": row.language,
        "locale": row.locale,
        "translation_version": row.translation_version,
        "status": row.status,
        "error": row.error or "",
        "qc": qc,
        "stages": lineage.get("stages") or [],
        "warnings": lineage.get("warnings") or [],
        "repairs": lineage.get("repairs") or [],
        "costs": lineage.get("costs") or {},
        "created_at": row.created_at.isoformat() + "Z",
        "updated_at": row.updated_at.isoformat() + "Z",
    }


def _get(ws_id: str, localized_id: str, db) -> LocalizedContent:
    row = db.get(LocalizedContent, localized_id)
    if row is None or row.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="localization not found")
    return row


# ---------------------------------------------------------------------------
# glossary (declared before /{localized_id} so "glossary" is never an id)
# ---------------------------------------------------------------------------

@localization_router.get("/glossary", summary="List workspace glossary terms")
def list_glossary(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    rows = db.scalars(select(GlossaryTerm)
                      .where(GlossaryTerm.workspace_id == ws.id)
                      .order_by(GlossaryTerm.term)).all()
    return {"total": len(rows), "items": [_glossary_dto(r) for r in rows]}


def _glossary_dto(row: GlossaryTerm) -> dict:
    return {
        "id": row.id, "term": row.term, "replacement": row.replacement or "",
        "target_languages": list(row.target_languages or []),
        "kind": row.kind or "terminology",
        "case_sensitive": bool(row.case_sensitive),
        "created_at": row.created_at.isoformat() + "Z",
    }


@localization_router.post("/glossary", summary="Add a glossary term")
def add_glossary_term(
    body: GlossaryBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    if body.kind not in GLOSSARY_KINDS:
        raise HTTPException(status_code=422,
                            detail=f"kind must be one of {list(GLOSSARY_KINDS)}")
    langs = [str(x).strip().lower() for x in body.target_languages if str(x).strip()]
    unknown = [x for x in langs if x not in LANG_LOCALES]
    if unknown:
        raise HTTPException(status_code=422,
                            detail=f"unsupported target language(s): {unknown}")
    row = GlossaryTerm(
        workspace_id=ws.id, term=body.term.strip(),
        replacement=body.replacement.strip(),
        target_languages=langs, kind=body.kind,
        case_sensitive=bool(body.case_sensitive))
    db.add(row)
    db.commit()
    db.refresh(row)
    from app.services.events import record_event

    record_event(ws.id, "glossary.added",
                 f"Glossary term '{row.term[:60]}' added", level="info",
                 source="studio", data={"term_id": row.id, "kind": row.kind})
    return _glossary_dto(row)


@localization_router.delete("/glossary/{term_id}", summary="Delete a glossary term")
def delete_glossary_term(
    term_id: str,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    row = db.get(GlossaryTerm, term_id)
    if row is None or row.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="glossary term not found")
    db.delete(row)
    db.commit()
    return {"deleted": term_id}


# ---------------------------------------------------------------------------
# runs
# ---------------------------------------------------------------------------

@localization_router.post("/run", summary="Kick off a localization run")
def run_localization(
    body: RunBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    source = db.get(ContentItem, body.source_content_id)
    if source is None or source.workspace_id != ws.id:
        raise HTTPException(status_code=404, detail="content not found")
    try:
        rows = prepare_localizations(
            db, workspace_id=ws.id, source_content_id=body.source_content_id,
            target_languages=body.target_languages,
            locales=dict(body.locales),
            glossary_overlay=[g.model_dump() for g in body.glossary],
            voice_prefs=dict(body.voice_prefs),
            translation_version=body.translation_version,
            source_language=body.source_language)
    except LocalizationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    import time as _time

    job_id = jobs_service.enqueue(
        "localization.run",
        {"localized_ids": [r.id for r in rows]},
        workspace_id=ws.id, priority=60,
        idempotency_key=f"localization-{ws.id}-{_time.time_ns()}")

    from app.services.events import record_event

    record_event(
        ws.id, "localization.queued",
        f"Localization queued for {', '.join(r.language for r in rows)}",
        level="info", source="studio",
        data={"source_content_id": body.source_content_id,
              "localized_ids": [r.id for r in rows], "job_id": job_id})
    return {"queued": job_id is not None, "job_id": job_id,
            "items": [{"id": r.id, "language": r.language, "locale": r.locale,
                       "status": r.status,
                       "translation_version": r.translation_version}
                      for r in rows]}


@localization_router.get("", summary="List localization runs")
def list_localizations(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    rows = db.scalars(
        select(LocalizedContent)
        .where(LocalizedContent.workspace_id == ws.id)
        .order_by(LocalizedContent.created_at.desc())
        .limit(100)).all()
    qc = _qc_summary(db, [r.id for r in rows])
    return {"total": len(rows),
            "items": [_dto(r, qc.get(r.id)) for r in rows]}


@localization_router.get("/{localized_id}", summary="Localization status + QC")
def get_localization(
    localized_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    row = _get(ws.id, localized_id, db)
    qc = _qc_summary(db, [row.id]).get(row.id)
    body = _dto(row, qc)
    body["lineage"] = dict(row.lineage_json or {})
    return body


@localization_router.get("/{localized_id}/qc", summary="QC report for a run")
def get_localization_qc(
    localized_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    row = _get(ws.id, localized_id, db)
    report = db.scalars(
        select(LocalizationQCReport)
        .where(LocalizationQCReport.localized_content_id == row.id,
               LocalizationQCReport.workspace_id == ws.id)
        .order_by(LocalizationQCReport.created_at.desc())).first()
    if report is None:
        raise HTTPException(status_code=404, detail="qc report not found")
    return {"id": report.id, "localized_content_id": row.id,
            "language": row.language, "status": report.status,
            "created_at": report.created_at.isoformat() + "Z",
            **dict(report.checks_json or {})}


# ---------------------------------------------------------------------------
# job handler
# ---------------------------------------------------------------------------

@jobs_service.handler("localization.run")
def handle_localization_run(ctx) -> dict:
    """Run every prepared row for this job; cancellable between stages."""
    payload = ctx.payload or {}
    ids = list(payload.get("localized_ids") or [])
    ws_id = ctx.workspace_id or ""
    results: list[dict] = []
    failed: list[dict] = []
    with session_scope() as s:
        rows = [s.get(LocalizedContent, lid) for lid in ids]
        rows = [r for r in rows if r is not None and r.workspace_id == ws_id]
        if not rows:
            return {"ok": False, "error": "localization rows not found"}
        for row in rows:
            try:
                pipeline = LocalizationPipeline(
                    s, localized_content_id=row.id, workspace_id=ws_id,
                    job_ctx=ctx)
                results.append(pipeline.run())
            except jobs_service._Cancelled:
                raise  # job-level cancellation: rows already marked CANCELLED
            except LocalizationError as exc:
                failed.append({"localized_content_id": row.id,
                               "language": row.language, "error": str(exc)[:300]})
            except Exception as exc:  # noqa: BLE001 - one language must not kill the rest
                failed.append({"localized_content_id": row.id,
                               "language": row.language,
                               "error": f"{type(exc).__name__}: {exc}"[:300]})

    # events only AFTER the session commits (no nested-write stall)
    if ws_id and results:
        from app.services.events import record_event

        for res in results:
            record_event(
                ws_id, "localization.completed",
                f"Localized to {res['language']} "
                f"({res['segments']} segment(s), QC {res['qc_status']})",
                level="success" if res["qc_status"] in ("PASS", "PASS_WITH_WARNINGS")
                else "warning",
                source="localization",
                data={"localized_content_id": res["localized_content_id"],
                      "timeline_id": res["timeline_id"],
                      "qc_status": res["qc_status"]})
    return {"ok": not failed, "completed": len(results), "failed": failed,
            "results": results}

"""Canonical timeline endpoints (editor load/save, versions, manifest, OTIO)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import func, select

from app.db import get_db
from app.engine import timeline as tl
from app.engine.timeline import TimelineValidationError
from app.models import ContentTimeline, Workspace
from app.services.auth_service import require_workspace_role

timelines_router = APIRouter(prefix="/workspaces/{workspace_id}/timelines", tags=["timelines"])


class TimelineCreate(BaseModel):
    name: str = "main"
    fps: float = 30.0
    duration_seconds: float = 0.0
    aspect: str = "9:16"
    content_item_id: str | None = None
    video_id: str | None = None
    tracks: list | None = None


class TimelineUpdate(BaseModel):
    name: str | None = None
    duration_seconds: float | None = None
    tracks: list | None = None
    # optimistic concurrency: the version the editor last loaded (required)
    base_version: int


class VersionCreate(BaseModel):
    label: str = ""


class FromVideo(BaseModel):
    video_id: str
    duration_seconds: float | None = None
    aspect: str = "9:16"
    file_path: str = ""


def _dto(row: ContentTimeline) -> dict:
    tracks = (row.tracks_json or {}).get("tracks", [])
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "content_item_id": row.content_item_id,
        "video_id": row.video_id,
        "name": row.name,
        "fps": row.fps,
        "duration_seconds": row.duration_seconds,
        "aspect_ratio": (row.tracks_json or {}).get("aspect_ratio", "9:16"),
        "tracks": tracks,
        "version": row.version,
        "parent_timeline_id": row.parent_timeline_id,
        "created_at": row.created_at.isoformat() + "Z",
        "updated_at": row.updated_at.isoformat() + "Z",
    }


def _get(ws_id: str, timeline_id: str, db) -> ContentTimeline:
    row = db.get(ContentTimeline, timeline_id)
    if row is None or row.workspace_id != ws_id:
        raise HTTPException(status_code=404, detail="timeline not found")
    return row


def _checked_doc(name: str, fps: float, duration: float, aspect: str, tracks: list | None) -> dict:
    if tracks is None:
        return tl.create_empty("ws", duration_seconds=duration, fps=fps, aspect=aspect)
    doc = {"name": name, "tracks": tracks, "duration_seconds": duration,
           "fps": fps, "aspect_ratio": aspect}
    try:
        tl.validate_timeline(doc)
    except TimelineValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return doc


@timelines_router.post("", summary="Create an empty timeline")
def create_timeline(
    body: TimelineCreate,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    from app.services.events import record_event

    doc = _checked_doc(body.name, body.fps, body.duration_seconds, body.aspect, body.tracks)
    row = ContentTimeline(workspace_id=ws.id, content_item_id=body.content_item_id,
                          video_id=body.video_id, name=body.name, fps=body.fps,
                          duration_seconds=doc.get("duration_seconds", body.duration_seconds),
                          tracks_json=doc)
    db.add(row)
    db.commit()
    db.refresh(row)
    record_event(ws.id, "timeline.created", f"Created timeline '{body.name[:60]}'",
                 level="info", source="editor", data={"timeline_id": row.id})
    return _dto(row)


@timelines_router.get("", summary="List workspace timelines")
def list_timelines(
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
    content_item_id: str | None = Query(default=None),
):
    total = db.scalar(select(func.count()).select_from(ContentTimeline)
                      .where(ContentTimeline.workspace_id == ws.id)) or 0
    q = select(ContentTimeline).where(ContentTimeline.workspace_id == ws.id)
    if content_item_id:
        q = q.where(ContentTimeline.content_item_id == content_item_id)
        total = db.scalar(select(func.count()).select_from(ContentTimeline).where(
            ContentTimeline.workspace_id == ws.id,
            ContentTimeline.content_item_id == content_item_id)) or 0
    rows = db.scalars(q.order_by(ContentTimeline.created_at.desc()).limit(100)).all()
    return {"total": total, "items": [_dto(r) for r in rows]}


@timelines_router.post("/from-video", summary="Import an existing render as a timeline")
def from_video(
    body: FromVideo,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    doc = tl.timeline_from_video(ws.id, video_id=body.video_id,
                                 duration_seconds=body.duration_seconds,
                                 aspect=body.aspect, file_path=body.file_path)
    row = ContentTimeline(workspace_id=ws.id, video_id=body.video_id, name="import",
                          fps=doc.get("fps", 30.0),
                          duration_seconds=doc.get("duration_seconds", 0.0), tracks_json=doc)
    db.add(row)
    db.commit()
    db.refresh(row)
    return _dto(row)


@timelines_router.get("/{timeline_id}", summary="Load a timeline (editor load)")
def get_timeline(
    timeline_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    return _dto(_get(ws.id, timeline_id, db))


@timelines_router.put("/{timeline_id}", summary="Save timeline tracks (editor save)")
def update_timeline(
    timeline_id: str,
    body: TimelineUpdate,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    row = _get(ws.id, timeline_id, db)
    # optimistic concurrency — same gate (and same detail shape) as /operations
    tip = tl.tip_version(db, timeline_id) or row
    current_version = int(getattr(tip, "version", None) or 1)
    # only the tip row is writable: a save aimed at any other family row would
    # rewrite history (append-only), so it is reported as stale either way.
    if int(body.base_version) != current_version or row.id != tip.id:
        raise HTTPException(status_code=409, detail={
            "error": "stale timeline version — reload latest",
            "expected_version": current_version,
            "actual_version": int(body.base_version),
        })
    current = dict(row.tracks_json or {})
    doc = _checked_doc(body.name or row.name, row.fps,
                       body.duration_seconds if body.duration_seconds is not None
                       else row.duration_seconds,
                       current.get("aspect_ratio", "9:16"),
                       body.tracks if body.tracks is not None
                       else current.get("tracks"))
    if body.name:
        row.name = body.name
    row.duration_seconds = doc.get("duration_seconds", row.duration_seconds)
    row.tracks_json = doc
    # version bump mirrors /operations: the saved row becomes the new tip
    row.version = current_version + 1
    db.commit()
    db.refresh(row)
    from app.services.events import record_event

    record_event(ws.id, "timeline.updated", f"Saved timeline '{row.name[:60]}'",
                 level="info", source="editor",
                 data={"timeline_id": row.id, "version": row.version})
    return _dto(row)


@timelines_router.post("/{timeline_id}/versions", summary="Save a version (undo history)")
def create_version(
    timeline_id: str,
    body: VersionCreate,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    _get(ws.id, timeline_id, db)
    try:
        new_id = tl.save_version(db, timeline_id, label=body.label)
    except TimelineValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    from app.services.events import record_event

    record_event(ws.id, "timeline.updated", f"Saved timeline version ({body.label[:40]})",
                 level="info", source="editor",
                 data={"timeline_id": new_id, "parent_id": timeline_id})
    return _dto(db.get(ContentTimeline, new_id))


@timelines_router.get("/{timeline_id}/manifest", summary="Render manifest for a timeline")
def get_manifest(
    timeline_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    row = _get(ws.id, timeline_id, db)
    doc = dict(row.tracks_json or {})
    doc.setdefault("fps", row.fps)
    doc.setdefault("duration_seconds", row.duration_seconds)
    try:
        manifest = tl.render_manifest(doc)
    except TimelineValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from app.services.events import record_event

    record_event(ws.id, "timeline.render_manifest_created",
                 f"Manifest for '{row.name[:60]}' ({manifest['clip_count']} clips)",
                 level="info", source="editor",
                 data={"timeline_id": row.id, "manifest_hash": manifest["manifest_hash"]})
    return manifest


@timelines_router.get("/{timeline_id}/otio", summary="OTIO-compatible export")
def get_otio(
    timeline_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    row = _get(ws.id, timeline_id, db)
    doc = dict(row.tracks_json or {})
    doc.setdefault("fps", row.fps)
    doc.setdefault("duration_seconds", row.duration_seconds)
    try:
        out = tl.to_otio_dict(doc)
    except TimelineValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    from app.services.events import record_event

    record_event(ws.id, "otio.exported", f"OTIO export for '{row.name[:60]}'",
                 level="info", source="editor", data={"timeline_id": row.id})
    return out


class SceneBody(BaseModel):
    title: str = ""
    script_segment: str = ""
    narration: str = ""
    visual_intent: str = ""
    start_seconds: float = 0.0
    end_seconds: float = 0.0
    assets: list = Field(default_factory=list)
    captions: list = Field(default_factory=list)


class ScenesReplaceBody(BaseModel):
    content_item_id: str | None = None
    scenes: list[SceneBody] = Field(default_factory=list)


def _scene_dto(sc) -> dict:
    return {
        "id": sc.id, "workspace_id": sc.workspace_id,
        "content_item_id": sc.content_item_id, "timeline_id": sc.timeline_id,
        "index": sc.index, "title": sc.title,
        "script_segment": sc.script_segment, "narration": sc.narration,
        "visual_intent": sc.visual_intent,
        "start_seconds": sc.start_seconds, "end_seconds": sc.end_seconds,
        "assets": sc.assets_json or [], "captions": sc.captions_json or [],
    }


@timelines_router.get("/{timeline_id}/scenes", summary="Scenes for a timeline")
def list_scenes(
    timeline_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    from app.models.assets import Scene

    _get(ws.id, timeline_id, db)
    rows = db.scalars(select(Scene).where(
        Scene.workspace_id == ws.id, Scene.timeline_id == timeline_id
    ).order_by(Scene.index).limit(200)).all()
    return {"total": len(rows), "scenes": [_scene_dto(sc) for sc in rows]}


@timelines_router.post("/{timeline_id}/scenes", summary="Replace all scenes for a timeline")
def replace_scenes(
    timeline_id: str,
    body: ScenesReplaceBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    from app.models.assets import Scene

    row = _get(ws.id, timeline_id, db)
    if (body.content_item_id and row.content_item_id
            and row.content_item_id != body.content_item_id):
        raise HTTPException(status_code=422, detail="scene content_item_id mismatches timeline")
    content_item_id = body.content_item_id or row.content_item_id
    for i, sc in enumerate(body.scenes):
        if not sc.end_seconds > sc.start_seconds:
            raise HTTPException(
                status_code=422,
                detail=f"scene {i} ('{sc.title[:40]}') has non-positive range",
            )
    old = db.scalars(select(Scene).where(
        Scene.workspace_id == ws.id, Scene.timeline_id == timeline_id)).all()
    for sc in old:
        db.delete(sc)
    db.flush()
    out = []
    for i, sc in enumerate(body.scenes):
        obj = Scene(workspace_id=ws.id, content_item_id=content_item_id,
                    timeline_id=timeline_id, index=i, title=sc.title[:200],
                    script_segment=sc.script_segment, narration=sc.narration,
                    visual_intent=sc.visual_intent,
                    start_seconds=sc.start_seconds, end_seconds=sc.end_seconds,
                    assets_json=list(sc.assets), captions_json=list(sc.captions))
        db.add(obj)
        out.append(obj)
    db.commit()
    for obj in out:
        db.refresh(obj)
    return {"total": len(out), "scenes": [_scene_dto(sc) for sc in out]}


class OperationsBody(BaseModel):
    base_version: int
    operations: list = Field(default_factory=list)


@timelines_router.post("/{timeline_id}/operations", summary="Apply typed edit operations (stale-safe)")
def apply_timeline_operations(
    timeline_id: str,
    body: OperationsBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    from app.engine import scene_sync as sync_mod
    from app.engine.timeline_ops import TimelineOpError, apply_operations
    from app.services.events import record_event

    row = _get(ws.id, timeline_id, db)
    if int(body.base_version) != int(row.version or 1):
        raise HTTPException(status_code=409, detail={
            "error": "stale timeline version — reload latest",
            "expected_version": row.version, "actual_version": row.version,
        })
    doc = dict(row.tracks_json or {})
    doc.setdefault("fps", row.fps)
    doc.setdefault("duration_seconds", row.duration_seconds)
    try:
        new_doc = apply_operations(doc, list(body.operations or []))
    except TimelineOpError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    row.tracks_json = new_doc
    row.duration_seconds = new_doc.get("duration_seconds", row.duration_seconds)
    row.version = int(row.version or 1) + 1
    resynced = sync_mod.resync_scene_ranges(
        session=db, workspace_id=ws.id, timeline_id=row.id, tracks_doc=new_doc)
    db.commit()
    db.refresh(row)
    record_event(ws.id, "timeline.updated",
                 f"Applied {len(body.operations or [])} operation(s) to '{row.name[:60]}'",
                 level="info", source="editor",
                 data={"timeline_id": row.id, "version": row.version,
                       "resynced_scenes": resynced})
    return {**_dto(row), "applied": len(body.operations or []),
            "resynced_scenes": resynced}


@timelines_router.get("/{timeline_id}/versions", summary="Version history for a timeline")
def versions_list(
    timeline_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    from app.engine.timeline import list_versions

    _get(ws.id, timeline_id, db)
    try:
        root, family = list_versions(db, timeline_id)
    except TimelineValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"root_id": root.id,
            "versions": [{**_dto(r), "is_tip": r.id == timeline_id} for r in family]}


def _doc_of(row: ContentTimeline) -> dict:
    """Stored tracks_json + row fallbacks, the shape every timeline GET uses."""
    doc = dict(row.tracks_json or {})
    doc.setdefault("fps", row.fps)
    doc.setdefault("duration_seconds", row.duration_seconds)
    return doc


def _brand_snapshot_before(db, workspace_id: str, at) -> object | None:
    """Latest BrandDNA policy snapshot saved at or before `at` (ledger read)."""
    from app.models import BrandEffectiveConfig

    q = (select(BrandEffectiveConfig)
         .where(BrandEffectiveConfig.workspace_id == workspace_id,
                BrandEffectiveConfig.created_at <= at)
         .order_by(BrandEffectiveConfig.created_at.desc(),
                   BrandEffectiveConfig.id.desc())
         .limit(1))
    return db.scalars(q).first()


def _dna_of(snapshot) -> dict:
    payload = dict(snapshot.effective_json or {})
    effective = payload.get("effective")
    return dict(effective) if isinstance(effective, dict) else payload


def _brand_diff(db, workspace_id: str, before_row: ContentTimeline,
                after_row: ContentTimeline) -> dict:
    """BrandDNA diff for two version rows — never fabricated.

    A snapshot can only be tied to a version by save time: the ledger records
    policy resolutions (workspace/campaign), not timeline versions. When no
    snapshot exists at or before one of the two versions, `available` is false
    and no brand data is invented.
    """
    from app.engine.timeline_diff import diff_brand_snapshots

    snap_before = _brand_snapshot_before(db, workspace_id, before_row.created_at)
    snap_after = _brand_snapshot_before(db, workspace_id, after_row.created_at)
    if snap_before is None or snap_after is None:
        return {"available": False,
                "reason": "no brand snapshot saved at or before one of these versions"}
    dna_before, dna_after = _dna_of(snap_before), _dna_of(snap_after)
    if not dna_before or not dna_after:
        return {"available": False, "reason": "brand snapshot carries no DNA document"}
    return {
        "available": True,
        "selection": "latest brand snapshot saved at or before each version",
        "from": {"snapshot_id": snap_before.id, "dna_version": snap_before.dna_version},
        "to": {"snapshot_id": snap_after.id, "dna_version": snap_after.dna_version},
        "diff": diff_brand_snapshots(dna_before, dna_after),
    }


@timelines_router.get("/{timeline_id}/diff",
                      summary="Semantic diff between two timeline versions")
def diff_timeline_versions(
    timeline_id: str,
    from_version: int = Query(..., description="version number to diff from"),
    to_version: int = Query(..., description="version number to diff to"),
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    from app.engine.timeline import list_versions, manifest_hash_of
    from app.engine.timeline_diff import diff_timeline_docs

    _get(ws.id, timeline_id, db)
    try:
        _, family = list_versions(db, timeline_id)
    except TimelineValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    by_version: dict[int, ContentTimeline] = {}
    for row in family:  # family is ordered: the newest row wins a version tie
        by_version[int(row.version or 1)] = row
    before = by_version.get(int(from_version))
    after = by_version.get(int(to_version))
    if before is None:
        raise HTTPException(status_code=404,
                            detail=f"version {int(from_version)} not found")
    if after is None:
        raise HTTPException(status_code=404,
                            detail=f"version {int(to_version)} not found")
    doc_before, doc_after = _doc_of(before), _doc_of(after)
    try:
        hash_before = manifest_hash_of(doc_before)
        hash_after = manifest_hash_of(doc_after)
    except TimelineValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return {
        "from": {"version": int(from_version), "manifest_hash": hash_before},
        "to": {"version": int(to_version), "manifest_hash": hash_after},
        "diff": diff_timeline_docs(doc_before, doc_after),
        "brand_diff": _brand_diff(db, ws.id, before, after),
    }


class RestoreBody(BaseModel):
    label: str = ""



@timelines_router.post("/{timeline_id}/versions/{version_id}/restore",
                       summary="Restore a version onto a new tip (append-only)")
def version_restore(
    timeline_id: str,
    version_id: str,
    body: RestoreBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    from app.engine.timeline import restore_version
    from app.services.events import record_event

    _get(ws.id, timeline_id, db)
    try:
        new_id = restore_version(db, timeline_id, version_id)
    except TimelineValidationError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    db.commit()
    record_event(ws.id, "timeline.updated", f"Restored version ({body.label[:40]})",
                 level="info", source="editor",
                 data={"timeline_id": new_id, "restored_from": version_id})
    return _dto(db.get(ContentTimeline, new_id))


class RenderBody(BaseModel):
    out_name: str = "edit.mp4"


@timelines_router.post("/{timeline_id}/render", summary="Render the edited timeline to MP4")
def render_edited_timeline(
    timeline_id: str,
    body: RenderBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db=Depends(get_db),
):
    import mimetypes
    import shutil
    import time

    from app.models.assets import MediaAsset
    from app.providers.video_engine.timeline_render import TimelineRenderError, render_timeline
    from app.services.events import record_event
    from app.services.storage import STORAGE_ROOT

    row = _get(ws.id, timeline_id, db)
    doc = dict(row.tracks_json or {})
    doc.setdefault("fps", row.fps)
    doc.setdefault("duration_seconds", row.duration_seconds)
    try:
        out = render_timeline(ws.id, db, doc, out_name=body.out_name or "edit.mp4")
    except TimelineRenderError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
    dest_dir = STORAGE_ROOT / ws.id
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = f"timeline_{row.id[:8]}_{int(time.time())}.mp4"
    dest = dest_dir / safe
    shutil.move(out["path"], dest)
    asset = MediaAsset(workspace_id=ws.id, type="video", origin="render",
                       provider="timeline_render", storage_key=safe,
                       mime_type=mimetypes.guess_type(safe)[0] or "video/mp4",
                       duration_seconds=out.get("duration_seconds"),
                       width=out.get("width"), height=out.get("height"),
                       file_size=dest.stat().st_size,
                       meta_json={"timeline_id": row.id,
                                  "timeline_version": row.version,
                                  "warnings": out.get("warnings", [])})
    db.add(asset)
    db.commit()
    db.refresh(asset)
    record_event(ws.id, "timeline.rendered", f"Rendered '{row.name[:60]}' to MP4",
                 level="success", source="editor",
                 data={"timeline_id": row.id, "asset_id": asset.id,
                       "duration": out.get("duration_seconds")})
    return {"asset_id": asset.id, "storage_key": safe,
            "duration_seconds": out.get("duration_seconds"),
            "width": out.get("width"), "height": out.get("height"),
            "warnings": out.get("warnings", [])}


@timelines_router.get("/{timeline_id}/export/otio", summary="Download validated .otio")
def export_otio(
    timeline_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    from fastapi.responses import Response

    from app.engine import otio_adapter as oa

    row = _get(ws.id, timeline_id, db)
    doc = dict(row.tracks_json or {})
    doc.setdefault("fps", row.fps)
    doc.setdefault("duration_seconds", row.duration_seconds)
    try:
        filename, media_type, content = oa.export_timeline(doc, "otio")
    except TimelineValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(content=content.encode("utf-8"), media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@timelines_router.get("/{timeline_id}/export/fcpxml", summary="Final Cut Pro XML (verified only)")
def export_fcpxml(
    timeline_id: str,
    ws: Workspace = Depends(require_workspace_role("viewer")),
    db=Depends(get_db),
):
    from fastapi.responses import Response

    from app.engine import otio_adapter as oa

    row = _get(ws.id, timeline_id, db)
    info = oa.export_formats().get("fcpxml", {})
    if info.get("status") != "AVAILABLE":
        raise HTTPException(status_code=501, detail={
            "status": "NOT_AVAILABLE",
            "reason": info.get("reason", "fcpxml unavailable")})
    doc = dict(row.tracks_json or {})
    doc.setdefault("fps", row.fps)
    doc.setdefault("duration_seconds", row.duration_seconds)
    try:
        filename, media_type, content = oa.export_timeline(doc, "fcpxml")
    except TimelineValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(content=content.encode("utf-8"), media_type=media_type,
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})

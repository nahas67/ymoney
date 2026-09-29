"""Project archive (Work 11 Lane L) -- contracts §10.

Two honest modes, one code path for collection:

``MANIFEST_ONLY``
    A single JSON document describing the project and everything linked
    into it. No bytes are produced beyond that document.

``PORTABLE_ARCHIVE``
    A stdlib ``zipfile`` archive with the same document plus the text
    artifacts: ``manifest.json``, ``project.json``, ``timelines/*.json``
    (full docs AND their version lists), ``captions/*.srt|vtt``,
    ``scripts/*.json``, ``research/*.json``, ``branddna.json``,
    ``assets.json``, ``lineage.json``, ``exports.json``.

**Media binaries are manifest-only entries.** ``assets.json`` records
id, name, size, checksum and mime for every asset the project reaches,
but the bytes are NOT copied into the zip. This is deliberate and
documented in the manifest itself (``media_included: false`` with a
``media_note``), because a portable archive that silently omits the
heavy files while looking complete is worse than one that says so.

**Secrets are structurally excluded, not filtered.** The collector only
ever reads an explicit allow-list of columns/tables (projects, targets,
content, timelines, scenes, assets metadata, brand DNA, exports,
reviews, comments, lineage). It never touches ``ApiCredential``,
``WebhookSubscription.secret_enc``, connector ``config_json``, provider
tokens, workspace settings or ``.env`` files, so a credential cannot
enter an archive even if a future contributor widens a query. The
manifest carries an ``excluded`` block naming those classes so the
reader knows they were dropped on purpose.
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import zipfile
from datetime import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    BrandDNARow,
    Comment,
    ContentItem,
    ContentTimeline,
    ExportJob,
    LocalizedContent,
    MediaAsset,
    Project,
    ProjectArchive,
    ProjectMember,
    ProjectTarget,
    Review,
    User,
    VideoVariant,
)
from app.models.base import utcnow

logger = logging.getLogger("ymoney.collab")

MANIFEST_ONLY = "MANIFEST_ONLY"
PORTABLE_ARCHIVE = "PORTABLE_ARCHIVE"
ARCHIVE_MODES: tuple[str, ...] = (MANIFEST_ONLY, PORTABLE_ARCHIVE)

#: Honest statement shipped inside every manifest about the binaries.
MEDIA_NOTE = (
    "Media binaries are NOT included in PORTABLE_ARCHIVE. assets.json lists "
    "id, name, size, checksum and mime for every referenced asset; fetch the "
    "bytes from the owning workspace storage if you need them."
)

#: Classes the collector never reads. Named in the manifest for honesty.
EXCLUDED_CLASSES: tuple[str, ...] = (
    "ApiCredential (provider keys)",
    "WebhookSubscription.secret_enc (signing secrets)",
    "SourceConnector.config_json (connector credentials)",
    "workspace settings_json (secrets)",
    ".env files",
    "assets outside the project's linked targets",
)

#: zip member name -> nothing; kept as a constant so tests can assert the
#: exact portable layout without duplicating it.
PORTABLE_LAYOUT: tuple[str, ...] = (
    "manifest.json",
    "project.json",
    "timelines/",
    "captions/",
    "scripts/",
    "research/",
    "branddna.json",
    "assets.json",
    "lineage.json",
    "exports.json",
)

_MAX_CAPTION_CLIPS = 5000


class ArchiveError(ValueError):
    """Domain error for archive misuse (routes map to 404/409/422)."""


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _iso(value: datetime | None) -> str:
    return (value.isoformat() + "Z") if value else ""


def _dumps(payload: Any) -> bytes:
    """Deterministic, human-readable JSON bytes (sorted keys)."""
    return json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False).encode("utf-8")


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_project(db: Session, workspace_id: str, project_id: str) -> Project:
    """Workspace-scoped project fetch; foreign/missing read as 404."""
    row = db.get(Project, str(project_id or ""))
    if row is None or row.workspace_id != workspace_id:
        raise ArchiveError("project not found")
    return row


def get_archive(db: Session, workspace_id: str, archive_id: str) -> ProjectArchive:
    """Workspace-scoped archive fetch; foreign/missing read as 404."""
    row = db.get(ProjectArchive, str(archive_id or ""))
    if row is None or row.workspace_id != workspace_id:
        raise ArchiveError("archive not found")
    return row


def _target_rows(db: Session, project_id: str) -> list[ProjectTarget]:
    return list(
        db.scalars(
            select(ProjectTarget)
            .where(ProjectTarget.project_id == project_id)
            .order_by(ProjectTarget.created_at)
        ).all()
    )


def _collect_content(db: Session, workspace_id: str, targets: list[ProjectTarget]):
    """Content items + localizations reachable from the linked targets.

    Every lookup is workspace-filtered: a link row pointing at a row in
    another workspace yields nothing (isolation by construction).
    """
    content_ids: list[str] = []
    localization_ids: list[str] = []
    for row in targets:
        if row.target_type == "content" and row.target_id not in content_ids:
            content_ids.append(row.target_id)
        elif row.target_type == "localization" and row.target_id not in localization_ids:
            localization_ids.append(row.target_id)
    # a linked content item also drags in its own localizations
    for loc in db.scalars(
        select(LocalizedContent).where(
            LocalizedContent.workspace_id == workspace_id,
            LocalizedContent.source_content_id.in_(content_ids or [""]),
        )
    ).all():
        if loc.id not in localization_ids:
            localization_ids.append(loc.id)
    items = [
        row
        for row in db.scalars(
            select(ContentItem).where(
                ContentItem.workspace_id == workspace_id, ContentItem.id.in_(content_ids or [""])
            )
        ).all()
    ]
    localizations = [
        row
        for row in db.scalars(
            select(LocalizedContent).where(
                LocalizedContent.workspace_id == workspace_id,
                LocalizedContent.id.in_(localization_ids or [""]),
            )
        ).all()
    ]
    # derived children of a linked item (lineage closure, one hop is enough
    # for a snapshot and keeps the walk bounded)
    for item in items:
        for child in db.scalars(
            select(ContentItem).where(
                ContentItem.workspace_id == workspace_id,
                ContentItem.parent_content_id == item.id,
            )
        ).all():
            if child.id not in {row.id for row in items}:
                items.append(child)
    return items, localizations


def _collect_timelines(db: Session, workspace_id: str, targets: list[ProjectTarget],
                       content_ids: list[str]) -> list[ContentTimeline]:
    """Linked timelines + the timelines of the linked content items."""
    ids: list[str] = [
        row.target_id for row in targets if row.target_type == "timeline"
    ]
    if content_ids:
        for row in db.scalars(
            select(ContentTimeline).where(
                ContentTimeline.workspace_id == workspace_id,
                ContentTimeline.content_item_id.in_(content_ids),
            )
        ).all():
            if row.id not in ids:
                ids.append(row.id)
    if not ids:
        return []
    return list(
        db.scalars(
            select(ContentTimeline).where(
                ContentTimeline.workspace_id == workspace_id,
                ContentTimeline.id.in_(ids),
            )
        ).all()
    )


def _asset_ids_from_timeline(row: ContentTimeline) -> list[str]:
    out: list[str] = []
    for track in (row.tracks_json or {}).get("tracks", []) or []:
        for clip in track.get("clips", []) or []:
            value = str((clip.get("source") or {}).get("asset_id") or "")
            if value and value not in out:
                out.append(value)
    return out


def _asset_ids_from_scenes(db: Session, workspace_id: str, content_ids: list[str]) -> list[str]:
    if not content_ids:
        return []
    from app.models import Scene

    out: list[str] = []
    for row in db.scalars(
        select(Scene).where(
            Scene.workspace_id == workspace_id, Scene.content_item_id.in_(content_ids)
        )
    ).all():
        for entry in row.assets_json or []:
            value = (
                str(entry.get("asset_id") or "")
                if isinstance(entry, dict)
                else str(entry or "")
            )
            if value and value not in out:
                out.append(value)
    return out


def _asset_manifest(db: Session, workspace_id: str, asset_ids: list[str]) -> list[dict]:
    """id / name / size / checksum / mime for each reachable asset.

    ``name`` is derived from the storage key (a path RELATIVE to the
    workspace dir), never an absolute path. Assets outside this
    workspace are dropped -- workspace isolation, not a filter.
    """
    if not asset_ids:
        return []
    entries: list[dict] = []
    for row in db.scalars(
        select(MediaAsset).where(
            MediaAsset.workspace_id == workspace_id, MediaAsset.id.in_(asset_ids)
        )
    ).all():
        key = str(row.storage_key or "")
        entries.append(
            {
                "id": row.id,
                "name": key.rsplit("/", 1)[-1] or row.id,
                "storage_key": key,
                "size_bytes": int(row.file_size or 0),
                "checksum": row.checksum or "",
                "mime_type": row.mime_type or "",
                "type": row.type or "",
                "origin": row.origin or "",
                "included": False,  # binaries are manifest-only, always
            }
        )
    entries.sort(key=lambda item: item["id"])
    return entries


def _version_family(db: Session, row: ContentTimeline) -> list[dict]:
    """Every saved version of one timeline (the full doc per version)."""
    family: list[ContentTimeline] = []
    if row.parent_timeline_id:
        # walk to the root, then breadth-first the family
        root = row
        seen_ids: set[str] = set()
        while root.parent_timeline_id and root.parent_timeline_id not in seen_ids:
            seen_ids.add(root.parent_timeline_id)
            parent = db.get(ContentTimeline, root.parent_timeline_id)
            if parent is None or parent.workspace_id != row.workspace_id:
                break
            root = parent
        family = [root]
        queue = [root.id]
        while queue:
            parent_id = queue.pop(0)
            for child in db.scalars(
                select(ContentTimeline).where(ContentTimeline.parent_timeline_id == parent_id)
            ).all():
                if child.id not in {r.id for r in family}:
                    family.append(child)
                    queue.append(child.id)
    else:
        family = [row]
    family.sort(key=lambda r: (int(r.version or 0), str(r.created_at)))
    out: list[dict] = []
    for version in family:
        doc = version.tracks_json or {}
        out.append(
            {
                "id": version.id,
                "version": version.version,
                "fps": version.fps,
                "duration_seconds": version.duration_seconds,
                "created_at": _iso(version.created_at),
                "is_tip": version.id == row.id,
                "document": doc,
                "manifest_hash": _doc_hash(doc),
            }
        )
    return out


def _doc_hash(doc: dict) -> str:
    try:
        from app.engine.timeline import manifest_hash_of

        return manifest_hash_of(doc or {})
    except Exception:  # noqa: BLE001 -- hashing is best effort, never fatal
        return _sha256(_dumps(doc or {}))[:32]


def _cues_from_timeline(row: ContentTimeline) -> list[dict]:
    """Caption clips of a timeline as ordered cues (stdlib only)."""
    cues: list[dict] = []
    for track in (row.tracks_json or {}).get("tracks", []) or []:
        if track.get("kind") != "caption":
            continue
        for clip in track.get("clips", []) or []:
            text = str(
                (clip.get("text") or {}).get("content") or clip.get("name") or ""
            ).strip()
            if not text:
                continue
            start = float(clip.get("start") or 0.0)
            cues.append(
                {
                    "index": len(cues) + 1,
                    "start": start,
                    "end": start + float(clip.get("duration") or 0.0),
                    "text": text,
                }
            )
            if len(cues) >= _MAX_CAPTION_CLIPS:
                return sorted(cues, key=lambda cue: (cue["start"], cue["index"]))
    return sorted(cues, key=lambda cue: (cue["start"], cue["index"]))


def _ts(seconds: float, *, sep: str = ",") -> str:
    ms = max(0, int(round(float(seconds) * 1000)))
    hours, rem = divmod(ms, 3_600_000)
    minutes, rem = divmod(rem, 60_000)
    secs, millis = divmod(rem, 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}{sep}{millis:03d}"


def _srt(cues: list[dict]) -> str:
    blocks = [
        f"{cue['index']}\n{_ts(cue['start'])} --> {_ts(cue['end'])}\n{cue['text']}"
        for cue in cues
    ]
    return "\n\n".join(blocks) + ("\n" if blocks else "")


def _vtt(cues: list[dict]) -> str:
    blocks = [
        f"{_ts(cue['start'], sep='.')} --> {_ts(cue['end'], sep='.')}\n{cue['text']}"
        for cue in cues
    ]
    return "WEBVTT\n\n" + "\n\n".join(blocks) + ("\n" if blocks else "")


def _brand_dna(db: Session, workspace_id: str) -> dict:
    """The effective BrandDNA snapshot: workspace default first."""
    row = db.scalar(
        select(BrandDNARow)
        .where(BrandDNARow.workspace_id == workspace_id, BrandDNARow.brand_id.is_(None))
        .order_by(BrandDNARow.updated_at.desc())
    )
    if row is None:
        row = db.scalar(
            select(BrandDNARow)
            .where(BrandDNARow.workspace_id == workspace_id)
            .order_by(BrandDNARow.updated_at.desc())
        )
    if row is None:
        return {"available": False, "reason": "no BrandDNA row for this workspace"}
    return {
        "available": True,
        "brand_id": row.brand_id,
        "dna": dict(row.dna_json or {}),
        "updated_at": _iso(row.updated_at),
    }


# ---------------------------------------------------------------------------
# document
# ---------------------------------------------------------------------------


def _project_document(db: Session, project: Project, targets: list[ProjectTarget]) -> dict:
    members = db.scalars(
        select(ProjectMember).where(ProjectMember.project_id == project.id)
    ).all()
    return {
        "id": project.id,
        "workspace_id": project.workspace_id,
        "name": project.name,
        "description": project.description or "",
        "status": project.status,
        "created_by": project.created_by,
        "created_at": _iso(project.created_at),
        "updated_at": _iso(project.updated_at),
        "archived_at": _iso(project.archived_at),
        "members": [
            {"user_id": row.user_id, "role": row.role, "created_at": _iso(row.created_at)}
            for row in sorted(members, key=lambda r: str(r.created_at))
        ],
        "targets": [
            {
                "target_type": row.target_type,
                "target_id": row.target_id,
                "created_at": _iso(row.created_at),
            }
            for row in targets
        ],
    }


def _content_documents(items: list[ContentItem]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for item in items:
        out[item.id] = {
            "id": item.id,
            "topic": item.topic,
            "status": item.status,
            "derivation_type": item.derivation_type,
            "parent_content_id": item.parent_content_id,
            "root_content_id": item.root_content_id,
            "lineage_version": item.lineage_version,
            "tags": list(item.tags_json or []),
            "created_at": _iso(item.created_at),
        }
    return out


def _script_documents(db: Session, content_ids: list[str]) -> dict[str, dict]:
    """Script text of each linked content item's variants."""
    if not content_ids:
        return {}
    out: dict[str, dict] = {}
    for variant in db.scalars(
        select(VideoVariant).where(VideoVariant.content_item_id.in_(content_ids))
    ).all():
        if variant.content_item_id is None:
            continue
        bucket = out.setdefault(variant.content_item_id, {"variants": []})
        bucket["variants"].append(
            {
                "id": variant.id,
                "label": variant.label,
                "hook": variant.hook or "",
                "script": variant.script or "",
                "visual_plan": dict(variant.visual_plan_json or {}),
                "selected": bool(variant.selected),
            }
        )
    for bucket in out.values():
        bucket["variants"].sort(key=lambda v: str(v["id"]))
    return out


def _research_documents(items: list[ContentItem]) -> dict[str, dict]:
    return {
        item.id: {
            "research": dict(item.research_json or {}),
            "strategy": dict(item.strategy_json or {}),
        }
        for item in items
    }


def _lineage_document(items: list[ContentItem], localizations: list[LocalizedContent]) -> dict:
    return {
        "content": [
            {
                "id": item.id,
                "topic": item.topic,
                "parent_content_id": item.parent_content_id,
                "root_content_id": item.root_content_id,
                "derivation_type": item.derivation_type,
                "lineage_version": item.lineage_version,
            }
            for item in items
        ],
        "localizations": [
            {
                "id": row.id,
                "source_content_id": row.source_content_id,
                "child_content_id": row.child_content_id,
                "timeline_id": row.timeline_id,
                "language": row.language,
                "locale": row.locale,
                "translation_version": row.translation_version,
                "status": row.status,
            }
            for row in localizations
        ],
    }


def _export_documents(db: Session, workspace_id: str,
                      target_ids: set[str]) -> list[dict]:
    """Export metadata (never artifact bytes, never secrets)."""
    rows = db.scalars(
        select(ExportJob)
        .where(ExportJob.workspace_id == workspace_id)
        .order_by(ExportJob.created_at)
    ).all()
    out: list[dict] = []
    for row in rows:
        if target_ids and row.target_id not in target_ids:
            continue
        out.append(
            {
                "id": row.id,
                "format": row.format,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "state": row.state,
                "progress": row.progress,
                "artifact_asset_id": row.artifact_asset_id,
                "checksum": row.checksum or "",
                "error": row.error or "",
                "attempt": row.attempt,
                "created_at": _iso(row.created_at),
                "finished_at": _iso(row.finished_at),
            }
        )
    return out


def _collaboration_documents(db: Session, workspace_id: str,
                             project_id: str, target_ids: set[str]) -> dict:
    reviews = [
        {
            "id": row.id,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "title": row.title,
            "state": row.state,
            "bound_version": row.bound_version,
            "stale": bool(row.stale),
            "created_by": row.created_by,
            "created_at": _iso(row.created_at),
        }
        for row in db.scalars(
            select(Review).where(
                Review.workspace_id == workspace_id, Review.project_id == project_id
            )
        ).all()
    ]
    comments = [
        {
            "id": row.id,
            "target_type": row.target_type,
            "target_id": row.target_id,
            "body": row.body,
            "author_id": row.author_id,
            "version_ref": row.version_ref,
            "resolved_at": _iso(row.resolved_at) or None,
            "created_at": _iso(row.created_at),
        }
        for row in db.scalars(
            select(Comment).where(
                Comment.workspace_id == workspace_id, Comment.project_id == project_id
            )
        ).all()
    ]
    return {"reviews": reviews, "comments": comments, "target_count": len(target_ids)}


def build_document(db: Session, workspace_id: str, project: Project, *,
                   mode: str) -> dict:
    """The full archive document (shared by both modes)."""
    targets = _target_rows(db, project.id)
    items, localizations = _collect_content(db, workspace_id, targets)
    content_ids = [item.id for item in items]
    timelines = _collect_timelines(db, workspace_id, targets, content_ids)

    asset_ids: list[str] = [
        row.target_id for row in targets if row.target_type == "ugc_asset"
    ]
    for row in timelines:
        for value in _asset_ids_from_timeline(row):
            if value not in asset_ids:
                asset_ids.append(value)
    for value in _asset_ids_from_scenes(db, workspace_id, content_ids):
        if value not in asset_ids:
            asset_ids.append(value)

    timeline_docs: dict[str, dict] = {}
    captions: dict[str, list[dict]] = {}
    for row in timelines:
        timeline_docs[row.id] = {
            "id": row.id,
            "name": row.name,
            "content_item_id": row.content_item_id,
            "version": row.version,
            "fps": row.fps,
            "duration_seconds": row.duration_seconds,
            "created_at": _iso(row.created_at),
            "document": row.tracks_json or {},
            "manifest_hash": _doc_hash(row.tracks_json or {}),
            "versions": _version_family(db, row),
        }
        captions[row.id] = _cues_from_timeline(row)

    target_ids = {row.target_id for row in targets}
    document = {
        "schema": "ymoney.project_archive.v1",
        "mode": mode,
        "generated_at": _iso(utcnow()),
        "project": _project_document(db, project, targets),
        "content": _content_documents(items),
        "timelines": timeline_docs,
        "captions": captions,
        "scripts": _script_documents(db, content_ids),
        "research": _research_documents(items),
        "branddna": _brand_dna(db, workspace_id),
        "assets": _asset_manifest(db, workspace_id, asset_ids),
        "lineage": _lineage_document(items, localizations),
        "exports": _export_documents(db, workspace_id, target_ids),
        "collaboration": _collaboration_documents(db, workspace_id, project.id, target_ids),
        "counts": {
            "targets": len(targets),
            "content_items": len(items),
            "timelines": len(timelines),
            "assets": len(asset_ids),
            "localizations": len(localizations),
        },
        "media_included": False,
        "media_note": MEDIA_NOTE,
        "excluded": list(EXCLUDED_CLASSES),
    }
    return document


# ---------------------------------------------------------------------------
# modes
# ---------------------------------------------------------------------------


def manifest_bytes(document: dict) -> bytes:
    """MANIFEST_ONLY payload: the single JSON document."""
    return _dumps(document)


def portable_bytes(document: dict) -> bytes:
    """PORTABLE_ARCHIVE payload: a stdlib zip of the documented layout."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        bundle.writestr("manifest.json", _dumps(document))
        bundle.writestr("project.json", _dumps(document.get("project") or {}))
        bundle.writestr("branddna.json", _dumps(document.get("branddna") or {}))
        bundle.writestr("assets.json", _dumps(document.get("assets") or []))
        bundle.writestr("lineage.json", _dumps(document.get("lineage") or {}))
        bundle.writestr("exports.json", _dumps(document.get("exports") or []))
        bundle.writestr(
            "collaboration.json", _dumps(document.get("collaboration") or {})
        )
        for timeline_id, payload in sorted((document.get("timelines") or {}).items()):
            bundle.writestr(f"timelines/{timeline_id}.json", _dumps(payload))
        for timeline_id, cues in sorted((document.get("captions") or {}).items()):
            if not cues:
                continue
            bundle.writestr(f"captions/{timeline_id}.srt", _srt(cues).encode("utf-8"))
            bundle.writestr(f"captions/{timeline_id}.vtt", _vtt(cues).encode("utf-8"))
        for content_id, payload in sorted((document.get("scripts") or {}).items()):
            bundle.writestr(f"scripts/{content_id}.json", _dumps(payload))
        for content_id, payload in sorted((document.get("research") or {}).items()):
            bundle.writestr(f"research/{content_id}.json", _dumps(payload))
    return buffer.getvalue()


def build_payload(document: dict, mode: str) -> tuple[bytes, str, str]:
    """(bytes, filename, media_type) for the requested mode."""
    if mode == PORTABLE_ARCHIVE:
        return (
            portable_bytes(document),
            f"{document.get('project', {}).get('id', 'project')}-portable.zip",
            "application/zip",
        )
    return (
        manifest_bytes(document),
        f"{document.get('project', {}).get('id', 'project')}-manifest.json",
        "application/json",
    )


# ---------------------------------------------------------------------------
# public API
# ---------------------------------------------------------------------------


def create_archive(db: Session, workspace_id: str, project_id: str, *,
                   mode: str, user: User) -> dict:
    """Build the archive, persist the row, mark the project ARCHIVED.

    ``user`` is the acting :class:`User` (its id is the audit actor).
    Raises ``ArchiveError`` (a ``ValueError``) for an unknown mode.
    """
    if mode not in ARCHIVE_MODES:
        raise ArchiveError(f"unknown archive mode {mode!r}")
    project = load_project(db, workspace_id, project_id)
    document = build_document(db, workspace_id, project, mode=mode)
    payload, filename, media_type = build_payload(document, mode)
    row = ProjectArchive(
        workspace_id=workspace_id,
        project_id=project.id,
        mode=mode,
        state="COMPLETE",
        manifest_json=document,
        artifact_path=filename,
        checksum=_sha256(payload),
        size_bytes=len(payload),
        created_by=user.id,
        finished_at=utcnow(),
    )
    db.add(row)
    db.flush()
    project.status = "ARCHIVED"
    project.archived_at = utcnow()
    db.flush()
    logger.info(
        "project archived: project=%s mode=%s bytes=%d", project.id, mode, len(payload)
    )
    return archive_dto(row)


def unarchive(db: Session, workspace_id: str, project_id: str) -> dict:
    """Return a project to ACTIVE so ``edit_project`` is allowed again."""
    project = load_project(db, workspace_id, project_id)
    if project.status != "ARCHIVED":
        raise ArchiveError("project is not archived")
    project.status = "ACTIVE"
    project.archived_at = None
    db.flush()
    return {"id": project.id, "status": project.status}


def assert_project_active(project: Project) -> None:
    """Raise 409-worthy ``ArchiveError`` when a project is archived.

    THE edit-freeze gate for contracts §10 ("archived project still
    readable but edit_project -> 409 until unarchived"). It lives here,
    next to the code that sets ``status``, so the invariant has exactly
    one owner: every mutating route that resolves an ``edit_*`` project
    capability calls this before applying the change, and
    ``api/v1/projects.py::update_project`` (lane F) is the call site.

    Reads deliberately do NOT call it -- an archived project stays
    readable, which is the whole point of archiving rather than deleting.
    """
    if str(project.status or "ACTIVE") == "ARCHIVED":
        raise ArchiveError("project is archived - unarchive before editing")


def download_bytes(db: Session, workspace_id: str, archive_id: str) -> tuple[bytes, str, str]:
    """(bytes, filename, media_type) for a stored archive.

    The payload is REBUILT from the persisted manifest rather than read
    back from disk: a portable archive is derived data, and rebuilding it
    keeps the download honest even if the byte blob was lost. Rows in a
    FAILED state have nothing to serve -> ``ArchiveError`` (409).
    """
    row = get_archive(db, workspace_id, archive_id)
    if row.state != "COMPLETE":
        raise ArchiveError(f"archive is {row.state}, nothing to download")
    document = dict(row.manifest_json or {})
    if not document:
        raise ArchiveError("archive has no manifest")
    return build_payload(document, row.mode)


def archive_dto(row: ProjectArchive) -> dict:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "project_id": row.project_id,
        "mode": row.mode,
        "state": row.state,
        "checksum": row.checksum or "",
        "size_bytes": int(row.size_bytes or 0),
        "error": row.error or None,
        "counts": dict((row.manifest_json or {}).get("counts") or {}),
        "media_included": False,
        "created_by": row.created_by,
        "created_at": _iso(row.created_at),
        "finished_at": _iso(row.finished_at) or None,
        "download_url": (
            f"/api/v1/workspaces/{row.workspace_id}/projects/"
            f"{row.project_id}/archives/{row.id}/download"
        ),
    }


__all__ = [
    "ARCHIVE_MODES",
    "EXCLUDED_CLASSES",
    "MANIFEST_ONLY",
    "MEDIA_NOTE",
    "PORTABLE_ARCHIVE",
    "PORTABLE_LAYOUT",
    "ArchiveError",
    "archive_dto",
    "assert_project_active",
    "build_document",
    "build_payload",
    "create_archive",
    "download_bytes",
    "get_archive",
    "load_project",
    "manifest_bytes",
    "portable_bytes",
    "unarchive",
]

"""Avatar persistence + render service (Work 07 Lane C).

Glue between the typed records (`engine.avatar.profile`) and the rows
(`models.avatar`): create/list profiles, explicit authorize/revoke, and
`render_profile_output()` — the ONE render path used by the API and by the
UGC presenter stage.

SAFETY (hard requirements):
  * consent is checked BEFORE any file/backend work (`require_authorized`,
    then again inside `AvatarProvider.render`) — no bypass anywhere.
  * every output row stores source asset + consent snapshot + provider +
    backend + driving audio + QC report in `lineage_json`.
  * every output becomes a `MediaAsset` + a canonical `ContentTimeline` on a
    `ContentItem` (voice + avatar tracks) so the editor can open it — never a
    flattened, uneditable file.
"""

from __future__ import annotations

import shutil
import time
from typing import Any

from app.engine.avatar.profile import (
    EXPR_PRESETS,
    FRAMINGS,
    MOTION_PRESETS,
    AvatarProfile,
    ConsentMetadata,
    get_provider,
    require_authorized,
)
from app.providers.avatar import AvatarError


class AvatarServiceError(Exception):
    """Profile not found / bad input (mapped to 404/422 by the routes)."""


# ---------------------------------------------------------------------------
# profiles
# ---------------------------------------------------------------------------


def _validate_profile_payload(payload: dict) -> dict:
    data = dict(payload or {})
    if not str(data.get("source_asset_ref") or "").strip():
        raise AvatarServiceError("source_asset_ref is required (upload the "
                                 "portrait/driving video under Assets first)")
    expr = str(data.get("expression_preset") or "neutral")
    if expr not in EXPR_PRESETS:
        raise AvatarServiceError(
            f"unknown expression_preset '{expr}' ({', '.join(EXPR_PRESETS)})")
    motion = str(data.get("motion_preset") or "subtle")
    if motion not in MOTION_PRESETS:
        raise AvatarServiceError(
            f"unknown motion_preset '{motion}' ({', '.join(MOTION_PRESETS)})")
    framing = str(data.get("framing") or "medium_closeup")
    if framing not in FRAMINGS:
        raise AvatarServiceError(f"unknown framing '{framing}' ({', '.join(FRAMINGS)})")
    return data


def create_profile(session: Any, workspace_id: str, payload: dict) -> Any:
    """Insert a profile with consent_state='pending' (never authorized by default)."""
    from app.models.avatar import AvatarProfileRow

    data = _validate_profile_payload(payload)
    profile = AvatarProfile(
        id="", workspace_id=workspace_id,
        name=str(data.get("name") or "")[:160],
        source_asset_ref=str(data.get("source_asset_ref") or ""),
        voice_ref=str(data.get("voice_ref") or ""),
        expression_preset=str(data.get("expression_preset") or "neutral"),
        motion_preset=str(data.get("motion_preset") or "subtle"),
        framing=str(data.get("framing") or "medium_closeup"),
        background=str(data.get("background") or "studio")[:80],
        language=str(data.get("language") or "en")[:10],
        brand_association=str(data.get("brand_association") or "")[:120],
        provider=str(data.get("provider") or "")[:40],
        status="active",
        consent=ConsentMetadata(state="pending"),
    )
    row = AvatarProfileRow(
        workspace_id=workspace_id,
        name=profile.name,
        profile_json=profile.to_json(),
        source_asset_ref=profile.source_asset_ref,
        consent_state="pending",
        consent_json=profile.consent.to_json(),
        provider=profile.provider,
        status="active",
    )
    session.add(row)
    session.flush()
    return row


def get_profile(session: Any, workspace_id: str, profile_id: str) -> Any:
    """Workspace-scoped fetch: a foreign profile is NOT FOUND, never a hint."""
    from app.models.avatar import AvatarProfileRow

    row = session.get(AvatarProfileRow, profile_id)
    if row is None or row.workspace_id != workspace_id:
        raise AvatarServiceError(f"avatar profile '{profile_id}' not found")
    return row


def list_profiles(session: Any, workspace_id: str) -> list:
    from sqlalchemy import select

    from app.models.avatar import AvatarProfileRow

    return list(session.scalars(
        select(AvatarProfileRow)
        .where(AvatarProfileRow.workspace_id == workspace_id)
        .order_by(AvatarProfileRow.created_at.desc())
    ).all())


def authorize_profile(session: Any, workspace_id: str, profile_id: str, *,
                      source: str, authorization_evidence: dict | None = None,
                      granted_by: str = "", statement: str = "") -> Any:
    """Flip consent_state → 'authorized' ONLY with concrete evidence.

    Refuses without source AND authorization_evidence — a bare "trust me"
    payload can never unlock rendering (no consent bypass).
    """
    row = get_profile(session, workspace_id, profile_id)
    evidence = dict(authorization_evidence or {})
    consent = ConsentMetadata(
        state="authorized",
        source=str(source or "").strip(),
        granted_by=str(granted_by or ""),
        granted_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        authorization_evidence=evidence,
        statement=str(statement or ""),
    )
    missing = consent.missing_for_authorization()
    if missing:
        raise AvatarServiceError(
            "authorization refused: missing " + ", ".join(missing)
            + " — supply the ownership/permission record (source + evidence)")
    row.consent_state = "authorized"
    row.consent_json = consent.to_json()
    session.flush()
    return row


def revoke_profile(session: Any, workspace_id: str, profile_id: str) -> Any:
    """consent_state → 'revoked'. Future renders refuse; history is kept."""
    row = get_profile(session, workspace_id, profile_id)
    consent = ConsentMetadata.from_json(dict(row.consent_json or {}))
    consent.state = "revoked"
    row.consent_state = "revoked"
    row.consent_json = consent.to_json()
    session.flush()
    return row


# ---------------------------------------------------------------------------
# render
# ---------------------------------------------------------------------------


def _store_output(workspace_id: str, src_path: str, stem: str) -> tuple[str, str]:
    """Copy the clip into workspace storage. Returns (storage_key, absolute)."""
    from app.services.storage import STORAGE_ROOT

    dest_dir = STORAGE_ROOT / workspace_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in stem)[:60]
    dest = dest_dir / f"avatar_{safe}_{int(time.time())}.mp4"
    if dest.exists():
        dest.unlink()
    shutil.copyfile(src_path, dest)
    return dest.name, str(dest)


def _content_item(session: Any, workspace_id: str, title: str) -> Any:
    from app.models import ContentItem

    item = ContentItem(workspace_id=workspace_id, topic=title[:400],
                       status="PRODUCTION", derivation_type="other")
    session.add(item)
    session.flush()
    return item


def build_avatar_timeline(session: Any, workspace_id: str, *,
                          content_item: Any, name: str,
                          audio_asset_id: str, output_asset_id: str,
                          duration: float) -> Any:
    """Canonical ContentTimeline: voice + avatar tracks, fully editable.

    Same `validate_timeline` contract the editor API uses — the output opens
    in the editor as clips, never as one flattened file.
    """
    from app.engine.timeline import add_clip, create_empty, render_manifest, validate_timeline
    from app.models import ContentTimeline

    doc = create_empty(workspace_id, aspect="9:16")
    dur = max(1.0, float(duration or 0.0))
    if audio_asset_id:
        add_clip(doc, track="voice", clip_id="voice_0", name="driving audio",
                 start=0.0, duration=dur, source={"asset_id": audio_asset_id})
    add_clip(doc, track="avatar", clip_id="avatar_0", name=name[:80] or "presenter",
             start=0.0, duration=dur, source={"asset_id": output_asset_id})
    add_clip(doc, track="text", clip_id="presenter_label",
             name=f"presenter: {name[:60]}", start=0.0, duration=min(3.0, dur),
             text={"content": name[:120], "size": 56})
    validate_timeline(doc)
    row = ContentTimeline(
        workspace_id=workspace_id,
        content_item_id=content_item.id,
        name=(name or "avatar output")[:200],
        fps=30.0,
        duration_seconds=float(doc.get("duration_seconds") or dur),
        tracks_json=doc,
        version=1,
    )
    session.add(row)
    session.flush()
    return row, render_manifest(doc)["manifest_hash"]


def render_profile_output(session: Any, workspace_id: str, profile_id: str, *,
                          audio_ref: str, opts: dict | None = None,
                          timeline: bool = True) -> dict:
    """Consent-gated render: profile → provider → MediaAsset → output row.

    Returns {asset_id, output_id, duration, backend, provider, qc,
             timeline_id?, content_item_id?, storage_key}. Raises
    `AvatarConsentError` when consent_state != 'authorized'.
    """
    from app.models.assets import MediaAsset
    from app.models.avatar import AvatarOutputRow

    row = get_profile(session, workspace_id, profile_id)
    profile = AvatarProfile.from_row(row)
    # 1) consent — hard gate before any file or backend work
    require_authorized(profile, action="avatar render")
    if not audio_ref:
        raise AvatarServiceError("audio_ref is required (driving voice asset)")

    # 2) provider (Unavailable fallback fails closed with remediation)
    provider = get_provider(profile.provider)
    result = provider.render(profile, audio_ref, opts or {})
    if not result.path:
        raise AvatarError("avatar provider returned no output file")

    # 3) output → MediaAsset (never flattened: a timeline follows below)
    storage_key, abs_path = _store_output(workspace_id, result.path,
                                          profile.name or profile.id[:8])
    asset = MediaAsset(
        workspace_id=workspace_id, type="avatar", origin="render",
        provider=result.provider, storage_key=storage_key,
        mime_type="video/mp4", duration_seconds=result.duration or None,
        meta_json={"profile_id": profile.id, "backend": result.backend,
                   "consent_state": profile.consent_state,
                   "is_mock": bool(result.is_mock)},
    )
    session.add(asset)
    session.flush()

    # 4) QC (file probes degrade to warnings; consent is enforced by lineage)
    from app.engine.ugc.qc import run_avatar_qc

    audio_duration = None
    from app.models.assets import MediaAsset as _MA

    audio_row = session.get(_MA, audio_ref)
    if audio_row is not None and audio_row.workspace_id == workspace_id:
        audio_duration = audio_row.duration_seconds
    lineage = dict(result.lineage)
    lineage.update({
        "workspace_id": workspace_id,
        "output_asset_id": asset.id,
        "storage_key": storage_key,
        "audio_asset_ref": audio_ref,
        "source_asset_ref": profile.source_asset_ref,
        "consent": profile.consent.to_json(),
        "provider": result.provider,
        "backend": result.backend,
        "license_notes": provider.health().get("license_notes", {}),
    })
    report = run_avatar_qc(output_path=abs_path, audio_duration=audio_duration,
                           lineage=lineage)
    lineage["qc"] = report.to_dict()

    # 5) editable timeline on a ContentItem (unless caller opts out)
    timeline_id = ""
    content_item_id = ""
    if timeline:
        item = _content_item(session, workspace_id,
                             f"Avatar: {profile.name or profile.id}")
        trow, manifest_hash = build_avatar_timeline(
            session, workspace_id, content_item=item,
            name=profile.name or "presenter",
            audio_asset_id=audio_ref, output_asset_id=asset.id,
            duration=result.duration or 0.0)
        timeline_id = trow.id
        content_item_id = item.id
        lineage["timeline_id"] = timeline_id
        lineage["content_item_id"] = content_item_id
        lineage["manifest_hash"] = manifest_hash

    out = AvatarOutputRow(
        workspace_id=workspace_id,
        profile_id=profile.id,
        output_asset_ref=asset.id,
        lineage_json=lineage,
        status="ready" if report.status != "FAIL" else "failed",
    )
    session.add(out)
    session.flush()
    return {"output_id": out.id, "asset_id": asset.id,
            "storage_key": storage_key, "duration": result.duration or 0.0,
            "backend": result.backend, "provider": result.provider,
            "is_mock": bool(result.is_mock), "consent_state": profile.consent_state,
            "timeline_id": timeline_id, "content_item_id": content_item_id,
            "qc": report.to_dict()}


def profile_dto(row: Any) -> dict:
    from app.engine.avatar.profile import AvatarProfile

    profile = AvatarProfile.from_row(row)
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "name": row.name,
        "profile": profile.to_json(),
        "source_asset_ref": row.source_asset_ref,
        "consent_state": row.consent_state,
        "consent": dict(row.consent_json or {}),
        "provider": row.provider,
        "status": row.status,
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else "",
    }


def output_dto(row: Any) -> dict:
    lineage = dict(row.lineage_json or {})
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "profile_id": row.profile_id,
        "output_asset_ref": row.output_asset_ref,
        "status": row.status,
        "provider": lineage.get("provider", ""),
        "backend": lineage.get("backend", ""),
        "consent": lineage.get("consent", {}),
        "source_asset_ref": lineage.get("source_asset_ref", ""),
        "timeline_id": lineage.get("timeline_id", ""),
        "content_item_id": lineage.get("content_item_id", ""),
        "qc": lineage.get("qc", {}),
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else "",
    }


__all__ = [
    "AvatarServiceError",
    "authorize_profile",
    "build_avatar_timeline",
    "create_profile",
    "get_profile",
    "list_profiles",
    "output_dto",
    "profile_dto",
    "render_profile_output",
    "revoke_profile",
]

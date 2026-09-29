"""UGC product-asset resolution: user uploads → timeline visuals.

HARD RULE: only workspace-bound `MediaAsset` uploads are accepted. The
pipeline never generates a product image, screenshot, logo or demo — if the
brief references something that is not in the workspace, it is reported as
unresolved and QC fails the project instead of inventing a placeholder.
"""

from __future__ import annotations

from typing import Any

_ALLOWED_TYPES = ("image", "video", "generated_image", "generated_video",
                  "thumbnail", "other")


def _entry_parts(entry: Any) -> tuple[str, str]:
    """(ref, role) from a brief product_assets entry (str or dict)."""
    if isinstance(entry, str):
        return entry.strip(), ""
    if not isinstance(entry, dict):
        return "", ""
    ref = ""
    for key in ("asset_id", "ref", "storage_key", "id", "path"):
        val = entry.get(key)
        if val:
            ref = str(val)
            break
    role = str(entry.get("role") or "")
    return ref, role


def _infer_role(ref: str, asset_type: str, declared: str) -> str:
    if declared:
        return declared
    low = (ref or "").lower()
    if "logo" in low:
        return "logo"
    if "screenshot" in low or "screen" in low:
        return "screenshot"
    if "demo" in low:
        return "demo"
    if asset_type in ("video", "generated_video"):
        return "product_video"
    return "product_image"


def resolve_product_assets(session: Any, workspace_id: str,
                           entries: list | None) -> tuple[list[dict], list[dict]]:
    """(resolved, unresolved) for brief.product_assets.

    A ref resolves only when a MediaAsset with that id/storage_key exists in
    THIS workspace — cross-workspace refs never resolve.
    """
    from app.models.assets import MediaAsset

    resolved: list[dict] = []
    unresolved: list[dict] = []
    for entry in entries or []:
        ref, role = _entry_parts(entry)
        if not ref:
            unresolved.append({"entry": entry, "reason": "no asset reference"})
            continue
        row = session.get(MediaAsset, ref) if ref else None
        if row is None or row.workspace_id != workspace_id:
            row = session.query(MediaAsset).filter(
                MediaAsset.workspace_id == workspace_id,
                MediaAsset.storage_key == ref,
            ).first()
        if row is None or row.workspace_id != workspace_id:
            unresolved.append({"entry": entry,
                               "reason": "not a workspace MediaAsset"})
            continue
        if row.type not in _ALLOWED_TYPES:
            unresolved.append({"entry": entry,
                               "reason": f"asset type '{row.type}' is not visual"})
            continue
        resolved.append({
            "asset_id": row.id,
            "ref": ref,
            "storage_key": row.storage_key,
            "type": row.type,
            "role": _infer_role(f"{row.storage_key} {ref}", row.type, role),
        })
    return resolved, unresolved


__all__ = ["resolve_product_assets"]

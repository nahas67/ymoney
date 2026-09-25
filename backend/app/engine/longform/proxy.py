"""Proxy media: lightweight editor proxies; final renders use originals.

Architecture: Original → Proxy Job → Editor Proxy (720p, fast decode).
The proxy row carries meta.original_asset_id; the timeline renderer only
ever resolves the asset_id referenced by a clip, so finals stay full-quality
unless an operator explicitly cuts a proxy into the timeline.
"""

from __future__ import annotations


class ProxyError(Exception):
    pass


def proxy_for(session, workspace_id: str, asset_id: str):
    """Existing proxy row, or generate one. Returns the proxy MediaAsset."""
    from app.models.assets import MediaAsset

    src = session.get(MediaAsset, asset_id)
    if src is None or src.workspace_id != workspace_id:
        raise ProxyError(f"asset '{asset_id}' not found")
    if (src.meta_json or {}).get("original_asset_id"):
        return src  # already a proxy
    existing = session.query(MediaAsset).filter(
        MediaAsset.workspace_id == workspace_id,
        MediaAsset.storage_key.like(f"%proxy_{src.id[:8]}%")).first()
    if existing and _exists(workspace_id, existing.storage_key):
        return existing
    return generate_proxy(session, workspace_id, asset_id)


def generate_proxy(session, workspace_id: str, asset_id: str):
    """Render a 720p seek-friendly proxy next to the original."""
    import shutil
    import subprocess

    from app.models.assets import MediaAsset
    from app.services.storage import STORAGE_ROOT, probe_metadata

    src = session.get(MediaAsset, asset_id)
    if src is None or src.workspace_id != workspace_id:
        raise ProxyError(f"asset '{asset_id}' not found")
    if (src.type or "") not in ("video",):
        raise ProxyError(f"proxies only for video assets, not '{src.type}'")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise ProxyError("ffmpeg missing — cannot generate proxy")
    original = (STORAGE_ROOT / workspace_id / src.storage_key.lstrip("/"))
    if not original.exists():
        raise ProxyError("original file missing on disk")
    outdir = STORAGE_ROOT / workspace_id / "proxies"
    outdir.mkdir(parents=True, exist_ok=True)
    name = f"proxy_{src.id[:8]}.mp4"
    proc = subprocess.run(
        [ffmpeg, "-y", "-i", str(original),
         "-vf", "scale=720:-2,fps=30,format=yuv420p",
         "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
         "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
         str(outdir / name)], capture_output=True, text=True, timeout=300)
    if proc.returncode != 0:
        raise ProxyError(f"proxy render failed: {(proc.stderr or '')[-200:]}")
    meta = probe_metadata(outdir / name)
    row = MediaAsset(
        workspace_id=workspace_id, type="video", origin="proxy",
        provider="proxy_media", storage_key=f"proxies/{name}",
        mime_type="video/mp4", duration_seconds=meta.get("duration_seconds"),
        width=meta.get("width"), height=meta.get("height"),
        file_size=(outdir / name).stat().st_size,
        meta_json={"original_asset_id": src.id,
                   "original_key": src.storage_key})
    session.add(row)
    session.flush()
    return row


def _exists(workspace_id: str, key: str) -> bool:
    from app.services.storage import STORAGE_ROOT

    return (STORAGE_ROOT / workspace_id / (key or "").lstrip("/")).exists()

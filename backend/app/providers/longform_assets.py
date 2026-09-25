"""Visual asset providers for long-form scenes (Work 03).

Business logic chooses KINDS; these adapters fetch bytes. Chain per scene:
primary → fallbacks, every fallback recorded with its reason. Silent
degradation is forbidden: when nothing resolves, AssetProviderError names
what was tried.
"""

from __future__ import annotations

import hashlib


class AssetProviderError(Exception):
    pass


class VisualAssetProvider:
    name = "base"

    def fetch(self, session, project, scene, beat: dict):
        """Return a persisted MediaAsset row (workspace-scoped)."""
        raise NotImplementedError


def get_asset_provider(kind: str) -> VisualAssetProvider:
    providers = {"stock": StockVideoProvider(), "local": LocalAssetProvider(),
                 "ai_image": GeneratedImageProvider(), "graphic": GraphicProvider()}
    if kind == "ai_video":
        raise AssetProviderError(
            "ai_video unavailable: no GPU worker deployed (Wan2.2 unevaluated — "
            "see docs/oss/OSS_COMPONENTS.md). Falling back per scene plan.")
    try:
        return providers[kind]
    except KeyError:
        raise AssetProviderError(f"unknown asset kind '{kind}'") from None


def _register(session, *, workspace_id: str, type: str, origin: str,
              provider: str, storage_key: str, mime: str = "",
              duration: float | None = None, width: int | None = None,
              height: int | None = None, meta: dict | None = None):
    from app.models.assets import MediaAsset

    existing = session.query(MediaAsset).filter(
        MediaAsset.workspace_id == workspace_id,
        MediaAsset.storage_key == storage_key).first()
    if existing:
        return existing
    row = MediaAsset(workspace_id=workspace_id, type=type, origin=origin,
                     provider=provider, storage_key=storage_key, mime_type=mime,
                     duration_seconds=duration, width=width, height=height,
                     meta_json=meta or {})
    session.add(row)
    session.flush()
    return row


def _pick(candidates: list, scene, beat: dict, usage: dict | None = None):
    """Deterministic least-used pick (stable reruns, automatic variety)."""
    if not candidates:
        raise AssetProviderError("no candidates available")
    key = f"{scene.id}:{beat.get('index', 0)}"
    scored = []
    for c in candidates:
        cid = c.id if hasattr(c, "id") else str(c)
        uses = (usage or {}).get(cid, 0)
        h = int(hashlib.sha256(f"{key}:{cid}".encode()).hexdigest(), 16) % 1000
        scored.append((uses, h, c))
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[0][2]


class LocalAssetProvider(VisualAssetProvider):
    """Reuse workspace uploads + previously registered media (zero cost)."""
    name = "local"

    def fetch(self, session, project, scene, beat: dict):
        from app.models.assets import MediaAsset
        from app.services.storage import STORAGE_ROOT

        ws = project.workspace_id
        rows = session.query(MediaAsset).filter(
            MediaAsset.workspace_id == ws,
            MediaAsset.type.in_(["video", "image"])).all()
        live = [r for r in rows if (STORAGE_ROOT / ws / r.storage_key.lstrip("/")).exists()]
        # plus raw uploads not yet registered
        updir = STORAGE_ROOT / ws / "uploads"
        if updir.exists():
            for f in sorted(updir.iterdir()):
                if f.is_file() and f.suffix.lower() in (".mp4", ".png", ".jpg", ".jpeg"):
                    key = f"uploads/{f.name}"
                    live.append(_register(
                        session, workspace_id=ws,
                        type="video" if f.suffix.lower() == ".mp4" else "image",
                        origin="upload", provider="local", storage_key=key,
                        mime="video/mp4" if f.suffix.lower() == ".mp4" else "image/png"))
        if not live:
            raise AssetProviderError("no local workspace media found")
        plan_usage = ((project.asset_plan_json or {}).get("usage")) or {}
        return _pick(live, scene, beat, plan_usage)


class StockVideoProvider(VisualAssetProvider):
    """Pexels stock via the existing B-roll engine (needs PEXELS_API_KEY)."""
    name = "stock"

    def fetch(self, session, project, scene, beat: dict):
        from app.providers import broll as broll_mod

        query = beat.get("query") or project.topic
        try:
            found = broll_mod.search_stock(query, per_page=6)
        except Exception as exc:
            raise AssetProviderError(f"stock search failed: {exc}") from exc
        if not found:
            raise AssetProviderError(f"no stock results for '{query[:60]}'")
        plan_usage = ((project.asset_plan_json or {}).get("usage")) or {}
        pick = _pick(found, scene, beat, plan_usage)
        try:
            stored = broll_mod.fetch_stock_clip(
                pick.video_id, project.workspace_id,
                "16:9" if project.aspect_ratio == "16:9" else "9:16")
        except Exception as exc:
            raise AssetProviderError(f"stock fetch failed: {exc}") from exc
        return _register(session, workspace_id=project.workspace_id, type="video",
                         origin="stock", provider="pexels", storage_key=_rel(stored),
                         mime="video/mp4", meta={"query": query,
                                                 "pexels_id": pick.video_id})


def _rel(path: str) -> str:
    # fetch_stock_clip returns workspace-absolute or relative paths; normalize
    # to a workspace-relative key for MediaAsset storage.
    text = str(path).replace("\\", "/")
    marker = "/data/videos/"
    if marker in text:
        text = text.split(marker, 1)[1]
        parts = text.split("/", 1)
        return parts[1] if len(parts) == 2 else parts[0]
    return text.lstrip("/")


class GeneratedImageProvider(VisualAssetProvider):
    """AI stills via the image provider layer (needs provider key)."""
    name = "ai_image"

    def fetch(self, session, project, scene, beat: dict):
        from app.providers.images import ImageProviderError, get_image_provider
        from app.services.storage import LocalStorage

        prompt = f"{project.topic}. {(scene.visual_intent or '')[:160]}"
        try:
            provider = get_image_provider()
            blobs = provider.generate(prompt, size="1280x720", n=1)
        except ImageProviderError as exc:
            raise AssetProviderError(f"image generation unavailable: {exc}") from exc
        if not blobs:
            raise AssetProviderError("image provider returned no candidates")
        stored = LocalStorage().save_media(
            project.workspace_id, blobs[0],
            f"lf_{scene.index}_{beat.get('index', 0)}.png")
        return _register(session, workspace_id=project.workspace_id, type="image",
                         origin="generated", provider=provider.name,
                         storage_key=_rel(stored), mime="image/png",
                         width=1280, height=720, meta={"prompt": prompt[:300]})


class GraphicProvider(VisualAssetProvider):
    """Deterministic ffmpeg cards (data/quote/title). Always available where
    ffmpeg exists — the terminal fallback that keeps renders honest."""
    name = "graphic"

    def fetch(self, session, project, scene, beat: dict):
        import shutil
        import subprocess

        from app.services.storage import STORAGE_ROOT

        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            raise AssetProviderError("ffmpeg missing — cannot render graphic fallback")
        from app.providers.video_engine.timeline_render import resolve_font

        font = resolve_font()
        if not font:
            raise AssetProviderError("no render font for graphic fallback")
        ws = project.workspace_id
        outdir = STORAGE_ROOT / ws / "graphics"
        outdir.mkdir(parents=True, exist_ok=True)
        name = f"lf_{scene.index}_{beat.get('index', 0)}.png"
        title = (scene.title or project.topic)[:60].replace(":", " -").replace("'", "")
        font_esc = font.replace("\\", "\\\\").replace(":", "\\:")
        cmd = [ffmpeg, "-y", "-f", "lavfi", "-i", "color=0x0b1220:1280x720",
               "-frames:v", "1", "-vf",
               f"drawtext=fontfile='{font_esc}':text='{title}':fontsize=54:"
               f"fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2", str(outdir / name)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise AssetProviderError(f"graphic render failed: {(proc.stderr or '')[-200:]}")
        return _register(session, workspace_id=ws, type="image", origin="generated",
                         provider="graphic", storage_key=f"graphics/{name}",
                         mime="image/png", width=1280, height=720,
                         meta={"title": title})

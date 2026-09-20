"""B-roll sourcing lane (E4): stock video fetch + AI clip generation.

Backends for AI generation (`broll.ai_backend`: server|wan|ltx|synth):
  server — generic HTTP text/image-to-video ({prompt,seconds,aspect} → mp4);
  wan    — native Wan 2.1 (Apache-2.0, ~8 GB VRAM) via diffusers, guarded;
  ltx    — native LTX-Video via diffusers, guarded;
  synth  — ffmpeg testsrc placeholder, honestly labeled (pipeline testing).

Stock (Pexels Videos API, same free key as photos) is the CPU default and
needs no GPU. Everything fails closed with remediation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from app.services.storage import STORAGE_ROOT

CACHE_DIR = Path("data/broll_cache")


class BrollError(Exception):
    pass


def _cred(key: str, env_attr: str = "") -> str:
    from app.core.config import settings

    try:
        from app.services.provider_settings import get_credential

        val, _src = get_credential(key)
        if val:
            return val
    except Exception:
        pass
    return getattr(settings, env_attr, "") or "" if env_attr else ""


def broll_ai_backend() -> str:
    return (_cred("broll.ai_backend", "broll_ai_backend") or "server").lower()


def broll_ai_base_url() -> str:
    return _cred("broll.ai_base_url", "broll_ai_base_url")


def _pexels_key() -> str:
    key, _src = "", ""
    try:
        from app.services.provider_settings import get_credential

        key, _src = get_credential("pexels.api_key")
    except Exception:
        pass
    if not key:
        from app.core.config import settings as _cfg

        key = _cfg.pexels_api_key
    return key or ""


def ffmpeg_present() -> bool:
    return bool(shutil.which("ffmpeg"))


def _have_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def broll_status() -> dict:
    ai = broll_ai_backend()
    native_ok = {
        "wan": _have_module("diffusers"),
        "ltx": _have_module("diffusers"),
        "synth": ffmpeg_present(),
    }.get(ai, None)
    base = broll_ai_base_url()
    if ai == "server":
        ai_ready, ai_detail = bool(base), ("renderer URL configured" if base
                                           else "broll.ai_base_url not configured")
    elif ai in ("wan", "ltx"):
        ai_ready = bool(native_ok and ffmpeg_present())
        ai_detail = ("diffusers present" if native_ok
                     else "diffusers not installed — pip install diffusers torch")
    elif ai == "synth":
        ai_ready, ai_detail = ffmpeg_present(), "ffmpeg placeholder clips"
    else:
        ai_ready, ai_detail = False, f"unknown AI backend '{ai}' (server|wan|ltx|synth)"
    return {
        "stock": bool(_pexels_key()),
        "stock_detail": "Pexels key configured" if _pexels_key() else "pexels.api_key not configured",
        "ai_backend": ai,
        "ai_ready": ai_ready,
        "ai_detail": ai_detail,
        "ffmpeg": ffmpeg_present(),
        "ready": bool(_pexels_key() or ai_ready),
    }


@dataclass
class StockCandidate:
    video_id: str
    preview: str
    duration: float | None
    author: str
    page_url: str


def search_stock(query: str, per_page: int = 6, orientation: str = "portrait") -> list[StockCandidate]:
    """Search Pexels video catalog (metadata only — no bytes moved)."""
    import httpx

    key = _pexels_key()
    if not key:
        raise BrollError("Pexels key not configured — add pexels.api_key under Settings → Connections")
    if not (query or "").strip():
        raise BrollError("query is required")
    resp = httpx.get(
        "https://api.pexels.com/videos/search",
        params={"query": query.strip()[:120], "per_page": max(1, min(per_page, 12)),
                "orientation": orientation if orientation in ("portrait", "landscape") else "portrait"},
        headers={"Authorization": key},
        timeout=20,
    )
    try:
        resp.raise_for_status()
    except httpx.HTTPError as exc:
        raise BrollError(f"Pexels search failed: {exc}") from exc
    out: list[StockCandidate] = []
    for v in (resp.json().get("videos") or []):
        pics = v.get("image") or ""
        out.append(StockCandidate(
            video_id=str(v.get("id", "")),
            preview=pics,
            duration=float(v.get("duration") or 0) or None,
            author=str((v.get("user") or {}).get("name", "")),
            page_url=str(v.get("url", "")),
        ))
    return out


def _best_mp4(video: dict, target_h: int) -> str:
    best = None
    for f in video.get("video_files") or []:
        if f.get("file_type") != "video/mp4" or not f.get("link"):
            continue
        hgt = int(f.get("height") or 0)
        if best is None or abs(hgt - target_h) < abs(int(best.get("height") or 0) - target_h):
            best = f
    return str(best.get("link", "")) if best else ""


def fetch_stock_clip(video_id: str, workspace_id: str, aspect: str = "9:16") -> str:
    """Download one Pexels video by id into the workspace boundary."""
    import httpx

    key = _pexels_key()
    if not key:
        raise BrollError("Pexels key not configured — add pexels.api_key under Settings → Connections")
    dest_dir = STORAGE_ROOT / workspace_id / "broll"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"pexels-{video_id}.mp4"
    if dest.exists() and dest.stat().st_size > 50_000:
        return str(dest)  # cache hit before any network
    try:
        resp = httpx.get(f"https://api.pexels.com/videos/videos/{video_id}",
                         headers={"Authorization": key}, timeout=20)
        resp.raise_for_status()
        video = resp.json()
    except httpx.HTTPError as exc:
        raise BrollError(f"Pexels lookup failed: {exc}") from exc
    target_h = 1920 if aspect == "9:16" else 1080
    link = _best_mp4(video, target_h)
    if not link:
        raise BrollError("no downloadable mp4 found for that video")
    try:
        dl = httpx.get(link, timeout=300, follow_redirects=True)
        dl.raise_for_status()
    except httpx.HTTPError as exc:
        raise BrollError(f"clip download failed: {exc}") from exc
    if len(dl.content) < 50_000:
        raise BrollError("downloaded clip is suspiciously small — retry")
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(dl.content)
    tmp.replace(dest)
    return str(dest)


def generate_clip(prompt: str, workspace_id: str, seconds: float = 4.0,
                  aspect: str = "9:16", image_ref: str = "") -> str:
    """AI-generate one B-roll clip via the configured AI backend."""
    if not (prompt or "").strip():
        raise BrollError("prompt is required")
    backend = broll_ai_backend()
    if backend == "server":
        return _server_generate(prompt.strip(), workspace_id, seconds, aspect, image_ref)
    if backend in ("wan", "ltx"):
        return _native_generate(backend, prompt.strip(), workspace_id, seconds, aspect, image_ref)
    if backend == "synth":
        return _synth_clip(prompt.strip(), workspace_id, seconds, aspect)
    raise BrollError(f"unknown AI backend '{backend}' (server|wan|ltx|synth)")


def _store_broll(workspace_id: str, src: Path, stem: str) -> str:
    dest_dir = STORAGE_ROOT / workspace_id / "broll"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{stem[:40]}.mp4"
    if dest.exists():
        dest.unlink()
    shutil.copyfile(src, dest)
    return str(dest)


def _server_generate(prompt: str, workspace_id: str, seconds: float,
                     aspect: str, image_ref: str) -> str:
    import httpx

    base = broll_ai_base_url()
    if not base:
        raise BrollError("AI server selected but broll.ai_base_url is not configured — "
                         "set it under Settings → Connections (B-roll) or switch backends")
    body: dict = {"prompt": prompt, "seconds": seconds, "aspect": aspect}
    if image_ref:
        from app.services.storage import managed_path

        ref = managed_path(workspace_id, image_ref)
        if ref and ref.exists():
            body["image_url"] = str(ref)
    try:
        resp = httpx.post(f"{base.rstrip('/')}/generate", json=body, timeout=1800)
    except httpx.HTTPError as exc:
        raise BrollError(f"AI renderer unreachable at {base}: {type(exc).__name__}") from exc
    if resp.status_code != 200:
        raise BrollError(f"AI renderer returned HTTP {resp.status_code}: {resp.text[:200]}")
    tmp = Path(f"data/broll_cache/server-{workspace_id}.mp4")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    if "video" in resp.headers.get("content-type", ""):
        tmp.write_bytes(resp.content)
    else:
        try:
            url = resp.json().get("video_url", "")
        except ValueError as exc:
            raise BrollError("AI renderer returned neither video nor video_url") from exc
        if not url:
            raise BrollError("AI renderer returned no video_url")
        try:
            dl = httpx.get(url, timeout=1800)
            dl.raise_for_status()
        except httpx.HTTPError as exc:
            raise BrollError(f"AI result download failed: {exc}") from exc
        tmp.write_bytes(dl.content)
    dur = _probe_duration(tmp)
    if dur <= 0:
        raise BrollError("AI renderer returned an unreadable video")
    h = hashlib.sha256(prompt.encode()).hexdigest()[:10]
    return _store_broll(workspace_id, tmp, f"ai-{h}")


def _native_generate(backend: str, prompt: str, workspace_id: str,
                     seconds: float, aspect: str, image_ref: str) -> str:
    if not _have_module("diffusers") or not _have_module("torch"):
        raise BrollError(f"{backend} backend needs diffusers + torch (GPU) — pip install "
                         "diffusers torch, or point broll.ai_base_url at a render server")
    if not ffmpeg_present():
        raise BrollError("ffmpeg not found — install it to finalize AI clips")
    try:
        import torch
        from diffusers import DiffusionPipeline  # type: ignore

        model_id = ("Wan-AI/Wan2.1-T2V-1.3B-Diffusers" if backend == "wan"
                    else "Lightricks/LTX-Video")
        pipe = DiffusionPipeline.from_pretrained(
            model_id, torch_dtype=torch.float16 if torch.cuda.is_available() else torch.float32)
        if torch.cuda.is_available():
            pipe.to("cuda")
        frames = pipe(prompt=prompt, num_frames=max(8, int(seconds * 8)),
                      height=512, width=288 if aspect == "9:16" else 512).frames[0]
    except Exception as exc:
        raise BrollError(f"{backend} generation failed: {type(exc).__name__}: {str(exc)[:200]}") from exc
    tmp = Path(f"data/broll_cache/{backend}-{workspace_id}.mp4")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    try:
        from diffusers.utils import export_to_video  # type: ignore

        export_to_video(frames, str(tmp), fps=8)
    except Exception as exc:
        raise BrollError(f"{backend} export failed: {exc}") from exc
    h = hashlib.sha256(prompt.encode()).hexdigest()[:10]
    return _store_broll(workspace_id, tmp, f"{backend}-{h}")


def _synth_clip(prompt: str, workspace_id: str, seconds: float, aspect: str) -> str:
    """Honestly labeled ffmpeg placeholder (pipeline testing, never production)."""
    size = "1080x1920" if aspect == "9:16" else "1920x1080"
    tmp = Path(f"data/broll_cache/synth-{workspace_id}.mp4")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "quiet", "-f", "lavfi", "-i",
         f"testsrc=size={size}:rate=30:duration={max(1.0, min(seconds, 10.0))}",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(tmp)],
        capture_output=True, timeout=120,
    )
    if proc.returncode != 0 or not tmp.exists():
        raise BrollError("synth clip render failed")
    h = hashlib.sha256(prompt.encode()).hexdigest()[:10]
    stored = _store_broll(workspace_id, tmp, f"synth-{h}")
    logger.warning(f"[broll] placeholder synth clip for '{prompt[:40]}' → {stored}")
    return stored


@dataclass
class SceneVisual:
    index: int
    query: str
    prompt: str
    source: str  # stock | ai
    license: str = ""


def plan_scenes(topic: str, keywords: list[str], n_scenes: int,
                workspace_id: str = "") -> list[SceneVisual]:
    """Per-scene visual plan: stock query + AI prompt each (LLM-enriched or template)."""
    n = max(1, min(n_scenes, 8))
    kws = [k for k in (keywords or []) if k][:6] or [w for w in topic.split()[:4]]
    prompts = _llm_scene_prompts(topic, kws, n, workspace_id) or _template_prompts(topic, kws, n)
    plan: list[SceneVisual] = []
    for i in range(n):
        kw = kws[i % len(kws)]
        plan.append(SceneVisual(
            index=i,
            query=f"{topic} {kw}"[:120],
            prompt=prompts[i] if i < len(prompts) else f"{topic}, {kw}, cinematic b-roll",
            source="stock" if i % 2 == 0 else "ai",
            license="Pexels license (stock) / generated (ai)",
        ))
    return plan


def _template_prompts(topic: str, kws: list[str], n: int) -> list[str]:
    shots = ["wide establishing shot", "close-up detail", "dynamic motion",
             "aerial view", "macro texture", "golden-hour exterior",
             "modern interior", "night city lights"]
    return [f"{topic}, {kws[i % len(kws)]}, {shots[i % len(shots)]}, vertical cinematic b-roll"
            for i in range(n)]


def _llm_scene_prompts(topic: str, kws: list[str], n: int, workspace_id: str) -> list[str] | None:
    try:
        from app.providers import llm as llm_mod

        if not llm_mod.llm_available():
            return None
        import json as _json

        res = llm_mod.complete_json(
            system=(f"Write {n} distinct vertical cinematic B-roll shot prompts for a short "
                    f"video. Each: subject + action + light/mood, under 25 words. "
                    f"Return JSON: {{\"prompts\": [...]}}."),
            user=_json.dumps({"topic": topic, "keywords": kws}),
            workspace_id=workspace_id,
            tier="cheap",
            temperature=0.7,
            max_tokens=600,
        )
        prompts = [str(p) for p in (res.get("prompts") or []) if str(p).strip()]
        return prompts[:n] or None
    except Exception as exc:
        logger.info(f"[broll] LLM scene prompts unavailable ({type(exc).__name__}); templates")
        return None


def _probe_duration(path: Path) -> float:
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", str(path)],
            capture_output=True, text=True, timeout=30,
        )
        import json as _json

        return float(_json.loads(out.stdout or "{}").get("format", {}).get("duration", 0.0))
    except Exception:
        return 0.0


__all__ = [
    "BrollError",
    "SceneVisual",
    "StockCandidate",
    "broll_ai_backend",
    "broll_ai_base_url",
    "broll_status",
    "fetch_stock_clip",
    "ffmpeg_present",
    "generate_clip",
    "plan_scenes",
    "search_stock",
]

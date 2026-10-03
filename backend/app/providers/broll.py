"""B-roll sourcing lane (E4): stock video fetch + AI clip generation.

Backends for AI generation (`broll.ai_backend`: server|wan|ltx|synth):
  server — generic HTTP text/image-to-video ({prompt,seconds,aspect} → mp4);
  wan    — native Wan 2.1 (Apache-2.0, ~8 GB VRAM) via diffusers, guarded;
  ltx    — native LTX-Video via diffusers, guarded;
  synth  — ffmpeg testsrc placeholder, honestly labeled (pipeline testing).

Stock (Pexels Videos API, same free key as photos) is the CPU default and
needs no GPU. Everything fails closed with remediation.

Two visual judgements are delegated to `app.engine.broll` instead of being made
here, because this module is I/O and that package is policy: orientation
verification (`aspect`, Work 15.5) and material diversity allocation
(`allocation`, Work 15.5).

Work 15.7: the `server` AI backend is a BILLED GPU render, so its submit runs
once through :class:`~app.services.paid_executor.PaidProviderExecutor`. The
stored artifact name now carries the submission id: it used to be
``sha256(prompt)[:10]``, which meant a retried prompt SILENTLY OVERWROTE the
file of the render that had already been billed, destroying the only local
evidence that the money was ever spent.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path

from loguru import logger

from app.engine.broll import allocation as alloc_mod
from app.engine.broll.aspect import (
    UNKNOWN,
    frame_kind,
    matches_aspect,
    orientation_of,
    pixel_size,
)
from app.engine.intelligence.sanitize import redact_error_text
from app.services.paid_executor import (
    IdempotencySupport,
    PaidArtifactUndownloadable,
    PaidJobError,
    PaidProviderExecutor,
    PaidSubmissionUnconfirmed,
    RemoteSubmission,
)
from app.services.paid_provider import (
    PaidOperation,
    absorb_paid_failure,
    paid_event,
    paid_operation,
)
from app.services.storage import STORAGE_ROOT

# NOTE: a ``CACHE_DIR = Path("data/broll_cache")`` constant used to sit here,
# declared and never read. Work 15.6 §7 removed it rather than wiring a second
# cache: search-result caching belongs to ``services/media_cache.py``, which has
# the credential-free key, the TTL and the signed-URL refusal this module lacked.

#: Workspace-scoped name of the material usage counter. It is keyed by
#: workspace directory, so one workspace's spending never suppresses another
#: workspace's variety.
USAGE_FILENAME = "material_usage.json"

#: The remote renderer does not report a price. Unpriced is a reason to record
#: an UNKNOWN exposure on an accepted clip, never a reason to book ``$0``.
EST_USD_PER_CLIP = 0.0

#: Ledger category for a generated clip. "video", so a B-roll render costs
#: against the same daily cap as everything else the workspace buys.
COST_CATEGORY = "video"


class BrollError(Exception):
    pass


# ---------------------------------------------------------------------------
# paid-submission wiring (Work 15.9 §3)
# ---------------------------------------------------------------------------


def _paid(operation: str, *, provider: str, workspace_id: str = "",
          estimated_cost: float = 0.0,
          idempotency: IdempotencySupport = IdempotencySupport.UNSUPPORTED,
          ) -> tuple[PaidOperation, PaidProviderExecutor]:
    """One billable B-roll render, on the shared money mechanics.

    Returns ``(paid, executor)``; see ``providers/images.py`` for the shape.
    ``workspace_id`` is explicit because the clip is stored under the workspace:
    the tenant is known, and inferring it from an ambient scope would be a guess
    about somebody's budget.
    """
    paid = paid_operation(
        provider=provider,
        operation=operation,
        workspace_id=str(workspace_id or "").strip(),
        category=COST_CATEGORY,
        estimated_cost=estimated_cost,
        idempotency=idempotency,
        reservation_extra={"lane": "broll"},
    )
    paid.bind(on_event=lambda phase, level, message:
              paid_event(paid, phase, level, message))
    return paid, paid.make_executor()


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
    }.get(ai)
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
    """One catalog hit. ``width``/``height``/``orientation`` are the evidence
    the aspect gate needs; they default to unknown rather than to a guess."""

    video_id: str
    preview: str
    duration: float | None
    author: str
    page_url: str
    width: int = 0
    height: int = 0
    orientation: str = UNKNOWN


def _video_dimensions(video: dict) -> tuple[int, int]:
    """The asset's native pixel size, or ``(0, 0)`` when nobody reported it.

    Catalog records describe one video through many renditions; the largest
    mp4 is the native frame, so that is the one whose orientation matters.
    Pixabay-shaped payloads put the size on the record instead of the variant.
    """
    best = (0, 0)
    best_area = 0
    for f in (video.get("video_files") or []):
        if not isinstance(f, dict):
            continue
        size = (pixel_size(f.get("width")), pixel_size(f.get("height")))
        if size[0] * size[1] > best_area:
            best, best_area = size, size[0] * size[1]
    if best_area:
        return best
    return pixel_size(video.get("width")), pixel_size(video.get("height"))


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
        width, height = _video_dimensions(v)
        out.append(StockCandidate(
            video_id=str(v.get("id", "")),
            preview=pics,
            duration=float(v.get("duration") or 0) or None,
            author=str((v.get("user") or {}).get("name", "")),
            page_url=str(v.get("url", "")),
            width=width,
            height=height,
            # `is_vertical` is the Coverr-shaped fallback for records that ship
            # no dimensions at all; without it the orientation stays UNKNOWN.
            orientation=orientation_of(width, height, v.get("is_vertical")),
        ))
    # Soft prior, not a hard filter: a search that returns only mismatched or
    # unverifiable hits is still better than no plan, and the hard gate lives at
    # rendition choice where the bytes are actually taken. Stable sort, so
    # equally-ranked hits keep the provider's ranking.
    wanted = frame_kind(orientation)
    if wanted != UNKNOWN:
        out.sort(key=lambda c: 0 if c.orientation == wanted else 1)
    return out


def _closest_by_height(files: list[dict], target_h: int) -> str:
    """The mp4 rendition closest to ``target_h`` (legacy height-only pick)."""
    best = None
    for f in files:
        hgt = pixel_size(f.get("height"))
        if best is None or abs(hgt - target_h) < abs(pixel_size(best.get("height")) - target_h):
            best = f
    return str(best.get("link", "")) if best else ""


def _best_mp4(video: dict, target_h: int, aspect: str = "") -> tuple[str, bool]:
    """Pick the mp4 rendition for a frame request. Returns ``(link, verified)``.

    Orientation is re-checked here, at the point the rendition is chosen,
    because the search call's ``orientation`` parameter is a hint: a 9:16 job
    that takes a 16:9 clip does not fail loudly, it crops the subject away.

    ``verified`` is False in exactly two cases — an aspect this module cannot
    classify, and a provider that reported no dimensions anywhere in the
    payload. Both are returned rather than raised because neither is a
    mismatch: there is no evidence to contradict, and refusing would turn a
    provider upgrade into a total outage. Every mismatch (dimensions known,
    orientation wrong) fails closed by returning no link.
    """
    files = [f for f in (video.get("video_files") or [])
             if isinstance(f, dict) and f.get("file_type") == "video/mp4" and f.get("link")]
    if not files:
        return "", False
    if frame_kind(aspect) == UNKNOWN:
        return _closest_by_height(files, target_h), False
    matching: list[dict] = []
    verified_any = False
    for f in files:
        width, height = pixel_size(f.get("width")), pixel_size(f.get("height"))
        is_vertical = f.get("is_vertical")
        if not (width and height) and not isinstance(is_vertical, bool):
            continue  # this rendition carries no orientation evidence at all
        verified_any = True
        if matches_aspect(width, height, aspect, is_vertical=is_vertical):
            matching.append(f)
    if matching:
        return _closest_by_height(matching, target_h), True
    if verified_any:
        return "", True  # orientations are known and none fits the frame
    return _closest_by_height(files, target_h), False


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
    link, verified = _best_mp4(video, target_h, aspect)
    if not link:
        raise BrollError(
            f"no downloadable mp4 matching {aspect} for that video"
            if frame_kind(aspect) != UNKNOWN
            else "no downloadable mp4 found for that video")
    if not verified:
        # Never silent: an unverified orientation is a real risk that the
        # compositor will crop rather than reject, so it is logged, not hidden.
        logger.warning(f"[broll] pexels:{video_id} reported no usable dimensions; "
                       f"rendition for the {aspect} job chosen by height alone "
                       f"(orientation UNVERIFIED)")
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
    """One billed GPU B-roll render. Submitted exactly once, ever.

    ``video_url`` is an artifact, not a job id, so the only retry that is
    safe here is the byte fetch -- and it goes through ``executor.download``.
    """
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
    tmp = Path(f"data/broll_cache/server-{workspace_id}.mp4")
    tmp.parent.mkdir(parents=True, exist_ok=True)
    paid, executor = _paid("broll.server_generate", provider="broll_ai_server",
                           workspace_id=workspace_id,
                           estimated_cost=EST_USD_PER_CLIP)

    def submit(_idempotency_key: str) -> RemoteSubmission:
        resp = httpx.post(f"{base.rstrip('/')}/generate", json=body, timeout=1800)
        if resp.status_code != 200:
            # Route the status through the SAME classifier as a transport
            # failure -- 4xx is a refusal, 5xx may follow a created-and-billed
            # render -- with a REDACTED body, because a provider error page can
            # carry a signed artifact URL. Built by hand rather than through
            # ``raise_for_status()`` so the body can be redacted at all.
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: "
                f"{redact_error_text(str(getattr(resp, 'text', ''))[:200])}",
                request=_request_of(resp, f"{base.rstrip('/')}/generate"),
                response=resp)
        if "video" in resp.headers.get("content-type", ""):
            payload, url = resp.content, ""
        else:
            try:
                url = str(resp.json().get("video_url", "") or "")
            except ValueError as exc:
                raise PaidSubmissionUnconfirmed(
                    provider="broll_ai_server",
                    detail="AI renderer returned neither video nor video_url",
                ) from exc
            if not url:
                raise PaidSubmissionUnconfirmed(
                    provider="broll_ai_server",
                    detail="AI renderer returned no video_url")
            payload = b""
        return RemoteSubmission(
            remote_id=str(resp.headers.get("x-request-id")
                          or resp.headers.get("x-render-id") or ""),
            artifact_path=tmp.name, raw={"url": url, "payload": payload})

    try:
        handle = executor.execute(submit, estimated_cost=EST_USD_PER_CLIP)
        url = str(handle.raw.get("url") or "")
        if url:
            tmp.write_bytes(executor.download(
                lambda: _fetch_clip(url), remote_id=handle.remote_id, url=url,
                attempts=2))
        else:
            tmp.write_bytes(bytes(handle.raw.get("payload") or b""))
    except BrollError:
        raise
    except PaidJobError as exc:
        # Billed-but-unconfirmed must not look like a refused request, and the
        # ledger decision (release vs. keep-and-mark-unknown) is the shared
        # helper's, not this adapter's.
        absorb_paid_failure(paid, exc)
        raise BrollError(str(exc)) from exc
    dur = _probe_duration(tmp)
    if dur <= 0:
        # The renderer was PAID and the bytes are unreadable. The money is gone
        # and unpriced, so the reservation is kept and marked as an unknown
        # exposure rather than released or booked at zero.
        absorb_paid_failure(paid, PaidArtifactUndownloadable(
            provider="broll_ai_server", remote_id=handle.remote_id,
            detail="AI renderer returned an unreadable video"))
        raise PaidArtifactUndownloadable(
            provider="broll_ai_server", remote_id=handle.remote_id,
            detail="AI renderer returned an unreadable video")
    # The renderer reported no price: an UNKNOWN exposure on a real reservation
    # row, never a fabricated $0 clip.
    paid.mark_succeeded(
        amount_unknown=True,
        detail="remote GPU B-roll renderer; the response carries no price")
    h = hashlib.sha256(prompt.encode()).hexdigest()[:10]
    # The submission id keeps a retried prompt from overwriting the file of a
    # render that was ALREADY BILLED: without it, `ai-<prompt hash>` collided
    # and the only local evidence of the first purchase was destroyed.
    suffix = paid.operation_id[:8] or "unknown"
    return _store_broll(workspace_id, tmp, f"ai-{h}-{suffix}")


def _request_of(resp, url: str):
    """The request a response came in on, synthesised when absent.

    ``httpx`` always attaches one; a test double may not, and the status bridge
    needs a request to construct a real ``HTTPStatusError``.
    """
    import httpx

    return getattr(resp, "request", None) or httpx.Request("POST", url)


def _fetch_clip(url: str) -> bytes:
    import httpx

    dl = httpx.get(url, timeout=1800)
    dl.raise_for_status()
    return dl.content


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
    # The keyword that produced this query. It is the allocation *group*: one
    # keyword searched once yields many clips, and it is the clip rotation
    # inside that group that removes the repetition.
    keyword: str = ""
    # Same shape and name as `Scene.performance_json`, so the scene-sync
    # adapter can persist it verbatim and a viewer-facing report of which clip
    # a scene used — and whether that clip was already spent — lands in the
    # column that already exists for scene performance.
    performance_json: dict = field(default_factory=dict)


def plan_scenes(topic: str, keywords: list[str], n_scenes: int,
                workspace_id: str = "") -> list[SceneVisual]:
    """Per-scene visual plan: stock query + AI prompt each (LLM-enriched or template).

    Keywords rotate per *lane*, not per scene index. Scenes alternate stock/AI,
    so indexing keywords by the scene number hands the stock lane the even
    keywords and the AI lane the odd ones — with two keywords and eight scenes
    the stock lane then searches `budget` four times and never searches `coins`
    at all. Indexing by the scene's position inside its own lane gives both
    lanes the whole keyword set.

    A scene plan still cannot guarantee distinct queries: with two keywords and
    eight scenes there are only two search terms to write. That repetition is
    absorbed one level down by `select_stock_clips`, which rotates a different
    *clip* per scene out of each keyword's candidates. Padding the queries with
    topic words to look unique would only add noise to the search.
    """
    n = max(1, min(n_scenes, 8))
    kws = [k for k in (keywords or []) if k][:6] or list(topic.split()[:4])
    prompts = _llm_scene_prompts(topic, kws, n, workspace_id) or _template_prompts(topic, kws, n)
    plan: list[SceneVisual] = []
    for i in range(n):
        stock = i % 2 == 0
        # Position of this scene within its own lane, which is what makes the
        # keyword rotation independent of the alternation.
        lane_index = i // 2 if stock else (i - 1) // 2
        kw = kws[lane_index % len(kws)]
        plan.append(SceneVisual(
            index=i,
            query=f"{topic} {kw}"[:120],
            prompt=prompts[i] if i < len(prompts) else f"{topic}, {kw}, cinematic b-roll",
            source="stock" if stock else "ai",
            license="Pexels license (stock) / generated (ai)",
            keyword=kw,
        ))
    # Intelligence advisory (Work 05, Lane A): shadow-only; plan order stays authoritative.
    try:
        from app.engine.intelligence.integrations import advise_broll_rank

        advise_broll_rank(
            [{"query": p.query, "prompt": p.prompt} for p in plan],
            workspace_id=workspace_id, topic=topic,
        )
    except Exception:
        pass
    return plan


def usage_store_path(workspace_id: str) -> Path:
    """Workspace-scoped location of the material usage counter.

    The counter is a shared resource, so it lives under the workspace's own
    directory: one workspace's spending must never suppress another
    workspace's variety.
    """
    return STORAGE_ROOT / (workspace_id or "") / "broll" / USAGE_FILENAME


def load_source_usage(workspace_id: str) -> dict[str, int]:
    """Read the workspace's material usage counter; a broken file is zero usage."""
    try:
        data = json.loads(usage_store_path(workspace_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): int(v) for k, v in data.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def save_source_usage(workspace_id: str, usage: dict[str, int]) -> None:
    """Persist the counter. Never raises: a lost counter costs variety, not bytes."""
    path = usage_store_path(workspace_id)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(dict(usage), sort_keys=True), encoding="utf-8")
    except (OSError, TypeError, ValueError) as exc:
        logger.debug(f"[broll] material usage not persisted for {workspace_id}: "
                     f"{type(exc).__name__}")


def _aspect_matched(candidates: list[StockCandidate], aspect: str) -> list[StockCandidate]:
    """Candidates whose orientation is verified to fit the frame.

    An empty result means nothing verified: the pool is passed through rather
    than refused, because a wrong-aspect clip is still a cheaper defect than a
    scene with no visual at all — and `fetch_stock_clip` re-checks and refuses
    the mismatched rendition anyway.
    """
    matched = [c for c in candidates
               if matches_aspect(c.width, c.height, aspect)]
    return matched or candidates


def select_stock_clips(plan: list[SceneVisual], workspace_id: str, *, per_page: int = 4,
                       aspect: str = "9:16", fetch: bool = True,
                       source_usage: dict[str, int] | None = None) -> list[SceneVisual]:
    """Resolve one stock clip per stock scene, allocating for diversity.

    Each distinct keyword is searched once, then the pooled candidates are
    handed to the allocator, so four scenes on the same keyword get four
    different clips instead of the same one four times. The usage counter is
    workspace-scoped and only advances for clips that actually landed: a failed
    fetch or download leaves its source unspent, exactly as a failed render
    does in the donor.

    Returns a new plan (the input objects are left alone) whose stock entries
    carry the outcome in ``performance_json`` under the keys
    ``material`` / ``material_reuse`` / ``material_short``. A reused source is
    reported, never hidden: an exhausted keyword group is a content problem
    the operator should see, not a silent loop.
    """
    entries = list(plan or [])
    stock_entries = [s for s in entries if getattr(s, "source", "") == "stock"]
    if not stock_entries:
        return entries
    usage = dict(load_source_usage(workspace_id) if source_usage is None else source_usage)
    resolved: dict[int, SceneVisual] = {}
    # Group by keyword, preserving the plan's order: allocation rotates clips
    # inside a group, it must not reorder the script's keyword sequence.
    groups: dict[str, list[SceneVisual]] = {}
    for scene in stock_entries:
        groups.setdefault(getattr(scene, "keyword", "") or "", []).append(scene)
    for keyword, scenes in groups.items():
        query = scenes[0].query
        try:
            found = search_stock(query, per_page=per_page,
                                 orientation="portrait" if frame_kind(aspect) != "landscape"
                                 else "landscape")
        except BrollError as exc:
            for scene in scenes:
                resolved[scene.index] = _short_report(scene, str(exc)[:160])
            continue
        pool = _aspect_matched(found, aspect)
        clips = [alloc_mod.Clip(source_id=c.video_id, group=keyword,
                                duration=float(c.duration or 0.0), path=c.preview)
                 for c in pool]
        by_id = {c.video_id: c for c in pool}
        for scene in scenes:
            picks = _pick_for_scene(clips, by_id, usage, scene.index, fetch, workspace_id, aspect)
            if picks is None:
                resolved[scene.index] = _short_report(scene, "no usable stock clip")
                continue
            chosen, ref, uses_before = picks
            usage = alloc_mod.record_usage(usage, [chosen.source_id])
            resolved[scene.index] = _used_report(scene, keyword, chosen, by_id, ref,
                                                 uses_before, aspect)
    save_source_usage(workspace_id, usage)
    return [resolved.get(s.index, s) for s in entries]


def _pick_for_scene(clips: list[alloc_mod.Clip], by_id: dict[str, StockCandidate],
                    usage: dict[str, int], index: int, fetch: bool,
                    workspace_id: str, aspect: str) -> tuple[alloc_mod.Clip, str, int] | None:
    """Fill one scene from the ordered pool, walking past unusable clips.

    A failed fetch must neither cost the scene its visual nor spend the source:
    the walk continues to the next candidate, and the caller commits only the
    clip that actually landed. Because the failed source stays unspent, the
    next scene is offered it again — the same behaviour a failed render has in
    the donor, and the reason the counter and the allocator are separate calls.
    """
    for clip in alloc_mod.order_clips(clips, usage):
        uses_before = int(usage.get(clip.source_id, 0))
        if not fetch:
            return clip, "", uses_before
        try:
            return clip, fetch_stock_clip(clip.source_id, workspace_id, aspect), uses_before
        except BrollError as exc:
            logger.warning(f"[broll] scene {index}: clip {clip.source_id} unusable "
                           f"({str(exc)[:120]}); source left unspent for another scene")
    return None


def _page_url_without_query(value: str) -> str:
    """Public page URL with its query removed.

    Stock providers hand back page URLs that may embed a signed, key-bound
    token. The page identity is the scheme/host/path; the query is a secret
    with a short life, so it is never persisted into scene metadata.
    """
    from urllib.parse import urlsplit, urlunsplit

    text = str(value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if not parts.scheme or not parts.netloc:
        return ""
    # Userinfo is credentials, not identity.
    host = parts.hostname or ""
    if parts.port:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, host, parts.path, "", ""))


def _material_block(keyword: str, clip: alloc_mod.Clip, candidate: StockCandidate | None,
                    ref: str, uses_before: int, aspect: str) -> dict:
    return {
        "source": "stock",
        "keyword": keyword,
        "clip_id": clip.source_id,
        "ref": ref,
        "uses_before": int(uses_before),
        "reused": uses_before > 0,
        "orientation": candidate.orientation if candidate is not None else UNKNOWN,
        "aspect": aspect,
        # Provenance (Work 15.6 §7). Captured from the provider at search time
        # but previously discarded here, so a finished scene could not answer
        # "who made this clip and where did it come from" -- which is the whole
        # point of recording a stock source.
        "author": (candidate.author or "") if candidate is not None else "",
        # A provider page URL can carry a signed, key-bound query string, so it
        # is stored stripped of its query rather than verbatim.
        "page_url": _page_url_without_query(candidate.page_url) if candidate is not None else "",
    }


def _used_report(scene: SceneVisual, keyword: str, clip: alloc_mod.Clip,
                 by_id: dict[str, StockCandidate], ref: str,
                 uses_before: int, aspect: str) -> SceneVisual:
    """Stamp the resolved clip and its reuse evidence onto the scene.

    ``uses_before`` is read from the counter before this scene's own increment,
    so it is the honest "already spent elsewhere" count and never a self-count.
    """
    perf = {"material": _material_block(keyword, clip, by_id.get(clip.source_id),
                                        ref, uses_before, aspect)}
    if uses_before > 0:
        perf["material_reuse"] = {
            "code": alloc_mod.REUSE_WARNING_CODE,
            "source_id": clip.source_id,
            "uses_before": uses_before,
        }
    return replace(scene, performance_json=perf)


def _short_report(scene: SceneVisual, reason: str) -> SceneVisual:
    perf = dict(getattr(scene, "performance_json", {}) or {})
    perf["material_short"] = {"code": alloc_mod.SHORT_WARNING_CODE, "reason": reason}
    return replace(scene, performance_json=perf)


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
    "EST_USD_PER_CLIP",
    "USAGE_FILENAME",
    "BrollError",
    "SceneVisual",
    "StockCandidate",
    "broll_ai_backend",
    "broll_ai_base_url",
    "broll_status",
    "fetch_stock_clip",
    "ffmpeg_present",
    "generate_clip",
    "load_source_usage",
    "plan_scenes",
    "save_source_usage",
    "search_stock",
    "select_stock_clips",
    "usage_store_path",
]

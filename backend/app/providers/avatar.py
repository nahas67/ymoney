"""Talking-avatar clip renderer (E3): photo + audio → lip-synced presenter MP4.

Backends, checked in order of `avatar.backend` (server|sadtalker|mock):
  server    — generic HTTP renderer (multipart image + audio → mp4 bytes or
              {video_url}); how hosted SadTalker/MuseTalk services plug in.
  sadtalker — native OpenTalker/SadTalker checkout via its inference script
              (CPU-slow, GPU-recommended); requires SAD_TALKER_DIR with
              checkpoints present.
  mock      — deterministic labeled placeholder for simulation/CI.

MuseTalk plugs in today through the server backend; a native MuseTalk lane
can follow the SadTalker pattern once its checkpoint layout is pinned.
Everything fails closed with remediation — never a silent still image.

Work 15.7: the ``server`` backend is a BILLED GPU render. Its submit runs once
through the shared paid machinery. Work 15.9 §3 moved that machinery into
:mod:`app.services.paid_provider`, so what is left here is only what the helper
must not know: the multipart body, the endpoint, that ``video_url`` is an
ARTIFACT rather than a job id, that a failed artifact download may be retried
because the RENDER may not, and that the remote renderer reports no price.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

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

WORK_DIR = Path("data/avatar")


class AvatarError(Exception):
    pass


# ---------------------------------------------------------------------------
# paid-submission wiring (Work 15.9 §3)
# ---------------------------------------------------------------------------

#: The remote renderer does not report a price. That is a reason to record an
#: UNKNOWN exposure on an accepted render, never a reason to book ``$0``.
EST_USD_PER_RENDER = 0.0

#: Ledger category for a render. "video", so a presenter clip costs against the
#: same daily cap as everything else the workspace buys.
COST_CATEGORY = "video"


def _paid(operation: str, *, provider: str, workspace_id: str = "",
          estimated_cost: float = 0.0,
          idempotency: IdempotencySupport = IdempotencySupport.UNSUPPORTED,
          ) -> tuple[PaidOperation, PaidProviderExecutor]:
    """One billable avatar render, on the shared money mechanics.

    Returns ``(paid, executor)``; see ``providers/images.py`` for the shape.
    ``workspace_id`` is explicit here -- the renderer is driven from a workspace
    asset, so the tenant is known and inferring it from an ambient scope would
    be a guess about somebody's budget.
    """
    paid = paid_operation(
        provider=provider,
        operation=operation,
        workspace_id=str(workspace_id or "").strip(),
        category=COST_CATEGORY,
        estimated_cost=estimated_cost,
        idempotency=idempotency,
        reservation_extra={"lane": "avatar"},
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


def avatar_backend() -> str:
    return (_cred("avatar.backend", "avatar_backend") or "server").lower()


def avatar_base_url() -> str:
    return _cred("avatar.base_url", "avatar_base_url")


def sadtalker_dir() -> str:
    return _cred("avatar.sadtalker_dir", "sadtalker_dir")


def wavlip_dir() -> str:
    return _cred("avatar.wavlip_dir", "wavlip_dir")


WAVLIP_LICENSE_NOTE = ("Wav2Lip weights are LRS2-trained: research/academic/personal "
                       "use only, no commercial use — set WAVLIP_DIR only where that applies.")

# W13 carry-forward (Work 11.5 gap): the note above only DISCLOSED the
# non-commercial terms; nothing refused the provider in commercial mode. This
# mirrors the media-intel registry, where `resolve(commercial_mode=True)`
# skips any provider whose license verdict is not COMMERCIAL_PERMITTED.
WAVLIP_COMMERCIAL_REFUSAL = (
    "wavlip is refused in commercial mode: LRS2-trained weights are "
    "research/academic/personal use only (no commercial use)"
)


def _wavlip_commercial_blocked() -> bool:
    """True when the operator has declared a commercial deployment.

    Read defensively: a settings object without the flag means NOT commercial,
    which keeps every existing non-commercial install working unchanged.
    """
    from app.core.config import settings

    return bool(getattr(settings, "commercial_mode", False))


def ffmpeg_present() -> bool:
    return bool(shutil.which("ffmpeg"))


def _sadtalker_ready() -> tuple[bool, str]:
    d = sadtalker_dir()
    if not d:
        return False, "SAD_TALKER_DIR not configured"
    if not (Path(d) / "inference.py").exists():
        return False, f"inference.py not found under {d}"
    return True, "checkout + inference script present"


def _wavlip_ready() -> tuple[bool, str]:
    # W13 carry-forward: refuse BEFORE any filesystem probe so a commercial
    # deployment can never reach LRS2 weights, even if WAVLIP_DIR is set.
    if _wavlip_commercial_blocked():
        return False, WAVLIP_COMMERCIAL_REFUSAL
    d = wavlip_dir()
    if not d:
        return False, "WAVLIP_DIR not configured"
    base = Path(d)
    if not (base / "inference.py").exists():
        return False, f"inference.py not found under {d}"
    ckpts = sorted((base / "checkpoints").glob("wav2lip*.pth")) if (base / "checkpoints").exists() else []
    if not ckpts:
        return False, f"no wav2lip*.pth checkpoint under {d}/checkpoints"
    return True, f"checkout + {ckpts[0].name} present"


def _lane_ready(name: str) -> bool:
    if name == "server":
        return bool(avatar_base_url())
    if name == "sadtalker":
        return _sadtalker_ready()[0]
    if name == "wavlip":
        return _wavlip_ready()[0]
    return name == "mock"


def avatar_status() -> dict:
    backend = avatar_backend()
    lanes = {name: _lane_ready(name) for name in ("server", "sadtalker", "wavlip", "mock")}
    # W13 carry-forward: expose the refusal explicitly so an operator can see
    # WHY the wavlip lane is dark instead of inferring it from "not configured".
    commercial = _wavlip_commercial_blocked()
    common = {
        "ffmpeg": ffmpeg_present(),
        "lanes": lanes,
        "license_notes": {"wavlip": WAVLIP_LICENSE_NOTE},
        "commercial_mode": commercial,
        "commercial_blocked": {"wavlip": WAVLIP_COMMERCIAL_REFUSAL} if commercial else {},
    }
    if backend == "mock":
        return {"backend": backend, "ready": True,
                "detail": "labeled simulation clips", **common}
    if backend == "sadtalker":
        ok, detail = _sadtalker_ready()
        return {"backend": backend, "ready": bool(ok and ffmpeg_present()),
                "detail": detail, **common}
    if backend == "wavlip":
        ok, detail = _wavlip_ready()
        return {"backend": backend, "ready": bool(ok and ffmpeg_present()),
                "detail": detail, **common}
    base = avatar_base_url()
    return {"backend": "server", "ready": bool(base and ffmpeg_present()),
            "detail": "renderer URL configured" if base else "avatar.base_url not configured",
            **common}


@dataclass
class AvatarClip:
    path: str
    backend: str
    duration: float = 0.0
    is_mock: bool = False


def _workspace_file(workspace_id: str, ref: str, what: str) -> Path:
    from app.services.storage import managed_path

    if not ref:
        raise AvatarError(f"{what} is required")
    resolved = managed_path(workspace_id, ref)
    if not resolved or not resolved.exists():
        raise AvatarError(f"{what} must be a workspace asset — upload it under Assets first")
    return resolved


def render_avatar(image_ref: str, audio_ref: str, workspace_id: str,
                  filename: str | None = None, backend: str = "") -> AvatarClip:
    """Render one talking-head clip from workspace-bound image + audio.

    `backend` optionally overrides the configured lane for this call
    (server|sadtalker|wavlip|mock).
    """
    image = _workspace_file(workspace_id, image_ref, "presenter image")
    audio = _workspace_file(workspace_id, audio_ref, "driving audio")
    if not ffmpeg_present():
        raise AvatarError("ffmpeg not found — install it to render avatars")
    backend = (backend or avatar_backend()).lower()
    if backend == "mock":
        return _mock_clip(workspace_id, filename)
    if backend == "sadtalker":
        return _sadtalker_render(image, audio, workspace_id, filename)
    if backend == "wavlip":
        return _wavlip_render(image, audio, workspace_id, filename)
    if backend == "server":
        return _server_render(image, audio, workspace_id, filename)
    raise AvatarError(f"unknown avatar backend '{backend}' (server|sadtalker|wavlip|mock)")


def _store(workspace_id: str, src: Path, filename: str | None) -> str:
    dest_dir = STORAGE_ROOT / workspace_id
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / (filename or f"avatar-{src.stem[:30]}.mp4")
    if dest.exists():
        dest.unlink()
    shutil.copyfile(src, dest)
    return str(dest)


def _mock_clip(workspace_id: str, filename: str | None) -> AvatarClip:
    """Labeled color-card placeholder (simulation only, never production)."""
    if not ffmpeg_present():
        raise AvatarError("ffmpeg not found — install it to render avatars")
    tmp = WORK_DIR / workspace_id
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / "mock-avatar.mp4"
    proc = subprocess.run(
        ["ffmpeg", "-y", "-v", "quiet",
         "-f", "lavfi", "-i", "color=c=0x1a2b1a:size=1080x1920:rate=30:duration=3",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(out)],
        capture_output=True, timeout=120,
    )
    if proc.returncode != 0 or not out.exists():
        raise AvatarError("mock avatar render failed")
    return AvatarClip(path=_store(workspace_id, out, filename or "mock-avatar.mp4"),
                      backend="mock", duration=3.0, is_mock=True)


def _server_render(image: Path, audio: Path, workspace_id: str,
                   filename: str | None) -> AvatarClip:
    """One billed GPU render. Submitted exactly once, ever.

    ``video_url`` is an ARTIFACT, not a job id: the server hands back either
    the clip inline or a link to it, and neither can be polled or re-fetched
    as a *render*. So the only safe retry here is the byte fetch, and it goes
    through ``executor.download``.
    """
    base = avatar_base_url()
    if not base:
        raise AvatarError("avatar server selected but avatar.base_url is not configured — "
                          "set it under Settings → Connections (Avatar) or use the "
                          "sadtalker/mock backend")
    tmp = WORK_DIR / workspace_id
    tmp.mkdir(parents=True, exist_ok=True)
    out = tmp / "server-avatar.mp4"
    paid, executor = _paid("avatar.server_render", provider="avatar_server",
                           workspace_id=workspace_id,
                           estimated_cost=EST_USD_PER_RENDER)

    def submit(_idempotency_key: str) -> RemoteSubmission:
        import httpx

        with image.open("rb") as fi, audio.open("rb") as fa:
            resp = httpx.post(
                f"{base.rstrip('/')}/render",
                files={"image": (image.name, fi, "image/jpeg"),
                       "audio": (audio.name, fa, "audio/mpeg")},
                timeout=1800,
            )
        if resp.status_code != 200:
            # Same reasoning as providers/broll.py: the status has to reach the
            # paid classifier (4xx refused, 5xx possibly billed), and the body
            # is redacted because a renderer error page can carry a signed URL.
            import httpx as _httpx

            raise _httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: {str(getattr(resp, 'text', ''))[:200]}",
                request=(getattr(resp, "request", None)
                         or _httpx.Request("POST", f"{base.rstrip('/')}/render")),
                response=resp)
        ctype = _header(resp, "content-type")
        url = ""
        if "video" in ctype:
            payload = resp.content
        else:
            try:
                url = str(resp.json().get("video_url", "") or "")
            except ValueError as exc:
                # Billed, and the answer names no artifact at all.
                raise PaidSubmissionUnconfirmed(
                    provider="avatar_server",
                    detail="avatar server returned neither video nor video_url",
                ) from exc
            if not url:
                raise PaidSubmissionUnconfirmed(
                    provider="avatar_server",
                    detail="avatar server returned no video_url")
            payload = b""
        return RemoteSubmission(
            remote_id=_header(resp, "x-request-id")
            or _header(resp, "x-render-id"),
            artifact_path=out.name,
            raw={"url": url, "payload": payload},
        )

    try:
        handle = executor.execute(submit, estimated_cost=EST_USD_PER_RENDER)
        url = str(handle.raw.get("url") or "")
        if url:
            # Already paid for. Retry the FETCH, never the render.
            out.write_bytes(executor.download(
                lambda: _fetch_render(url), remote_id=handle.remote_id, url=url,
                attempts=2))
        else:
            out.write_bytes(bytes(handle.raw.get("payload") or b""))
        if not out.exists() or out.stat().st_size <= 0:
            raise PaidArtifactUndownloadable(
                provider="avatar_server", remote_id=handle.remote_id,
                detail="avatar server accepted the render but delivered no bytes")
    except AvatarError:
        raise
    except PaidJobError as exc:
        # Ambiguous, refused, or billed-but-undeliverable: the ledger decision
        # belongs to the shared helper, and the submission record already
        # carries the state, the remote id and the exposure -- so the message
        # must too.
        absorb_paid_failure(paid, exc)
        raise AvatarError(str(exc)) from exc
    dur = _probe_duration(out)
    # The server reported no price, so the render is an UNKNOWN exposure on a
    # real reservation row: visible with a WHERE clause, never a fabricated $0.
    paid.mark_succeeded(
        amount_unknown=True,
        detail="remote GPU render server; the response carries no price")
    # A billed render must not overwrite the previous billed render's file:
    # the stored name carries the submission id, so both survive.
    stored = filename or f"avatar-server-{paid.operation_id[:8] or 'render'}.mp4"
    return AvatarClip(path=_store(workspace_id, out, stored), backend="server",
                      duration=dur)


def _header(resp, name: str, default: str = "") -> str:
    """One response header, tolerating a thin response object.

    ``httpx`` answers with a mapping; a test double may answer with a plain
    dict or with nothing at all. A vendor request id is a reconciliation
    handle, not a reason to fail a render.
    """
    getter = getattr(getattr(resp, "headers", None), "get", None)
    return str(getter(name, default)) if callable(getter) else default


def _fetch_render(url: str) -> bytes:
    import httpx

    resp = httpx.get(url, timeout=1800)
    resp.raise_for_status()
    return resp.content


def _sadtalker_render(image: Path, audio: Path, workspace_id: str,
                      filename: str | None) -> AvatarClip:
    ok, detail = _sadtalker_ready()
    if not ok:
        raise AvatarError(f"sadtalker backend not ready: {detail} — see Settings → Connections (Avatar)")
    script = str(Path(sadtalker_dir()) / "inference.py")
    result_dir = WORK_DIR / workspace_id / "sadtalker_out"
    result_dir.mkdir(parents=True, exist_ok=True)
    before = set(result_dir.rglob("*.mp4"))
    cmd = ["python", script, "--driven_audio", str(audio), "--source_image", str(image),
           "--result_dir", str(result_dir), "--still", "--preprocess", "full",
           "--enhancer", "gfpgan", "--expression_scale", "1.0"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                              cwd=sadtalker_dir())
    except subprocess.TimeoutExpired as exc:
        raise AvatarError("sadtalker render timed out after 60 minutes") from exc
    if proc.returncode != 0:
        tail = ((proc.stderr or proc.stdout) or "")[-500:]
        raise AvatarError(f"sadtalker render failed: {tail[:300]}")
    fresh = sorted((set(result_dir.rglob("*.mp4")) - before),
                   key=lambda p: p.stat().st_mtime)
    if not fresh:
        raise AvatarError("sadtalker produced no mp4 — check checkpoints under SAD_TALKER_DIR")
    dur = _probe_duration(fresh[-1])
    return AvatarClip(path=_store(workspace_id, fresh[-1], filename), backend="sadtalker",
                      duration=dur)


def _wavlip_render(image: Path, audio: Path, workspace_id: str,
                   filename: str | None) -> AvatarClip:
    """Fast lip-sync fix on existing footage (Wav2Lip native checkout).

    Interface verified against Rudrabha/Wav2Lip: inference.py
    --checkpoint_path/--face/--audio/--outfile. Best on footage where a face
    is present in all frames; ideal after dubbing (new audio, same video).
    """
    ok, detail = _wavlip_ready()
    if not ok:
        raise AvatarError(f"wavlip backend not ready: {detail} — see Settings → Connections (Avatar)")
    base = Path(wavlip_dir())
    ckpts = sorted((base / "checkpoints").glob("wav2lip*.pth"))
    ckpt = next((c for c in ckpts if "gan" in c.name.lower()), ckpts[0])
    out = WORK_DIR / workspace_id / "wavlip_out.mp4"
    out.parent.mkdir(parents=True, exist_ok=True)
    if out.exists():
        out.unlink()
    cmd = ["python", str(base / "inference.py"),
           "--checkpoint_path", str(ckpt),
           "--face", str(image),
           "--audio", str(audio),
           "--outfile", str(out)]
    # NOTE: --face accepts a video file (lip-sync fix on footage) or a still
    # image; image input works best with a frontal portrait.
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=3600,
                              cwd=str(base))
    except subprocess.TimeoutExpired as exc:
        raise AvatarError("wavlip render timed out after 60 minutes") from exc
    if proc.returncode != 0 or not out.exists():
        tail = ((proc.stderr or proc.stdout) or "")[-500:]
        raise AvatarError(f"wavlip render failed: {tail[:300]}")
    dur = _probe_duration(out)
    return AvatarClip(path=_store(workspace_id, out, filename), backend="wavlip",
                      duration=dur)


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
    "EST_USD_PER_RENDER",
    "WAVLIP_LICENSE_NOTE",
    "AvatarClip",
    "AvatarError",
    "avatar_backend",
    "avatar_base_url",
    "avatar_status",
    "ffmpeg_present",
    "render_avatar",
    "sadtalker_dir",
    "wavlip_dir",
]

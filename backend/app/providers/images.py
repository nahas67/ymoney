"""Image provider abstraction — scene image generation.

Providers (selected via IMAGE_PROVIDER):
  pexels        — real stock photography via the Pexels search API. Best when
                  authentic visuals matter more than generated art.
  xkiro         — xKiro async image jobs (free-tier sensenova model, CDN URL
                  results). AI-generated when a PEXELS key is absent.
  pollinations  — keyless free generation via the public pollinations.ai HTTP
                  API. No account, no API key.
  openai_compat — any OpenAI-compatible /images/generations endpoint (self-hosted
                  ComfyUI bridges, LocalAI, or a paid vendor). Fully local when
                  pointed at localhost.
  mock          — simulation-only deterministic placeholder (labeled, no network).

All providers return raw image bytes + format; callers decide how to persist.
This module never writes into workspace storage itself.

Work 15.7: every billable image submit goes through the shared paid executor.
Work 15.9 §3: it goes through :mod:`app.services.paid_provider` instead of a
local copy of the same ~35 lines. What is left here is what the helper must
NOT know -- the request body, the endpoint, the polling loop, the CDN fetch, and
what each provider's HTTP status means. A generated image is not refetchable
for free, so the three things that cost money are decided once: the reservation
is taken BEFORE the POST, the remote job id is persisted the moment acceptance
is known (xKiro returns one and it used to be dropped at the end of the loop),
and a lost response is recorded as UNKNOWN_EXPOSURE rather than as $0.
"""

from __future__ import annotations

import abc
import hashlib
import re
import time

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

#: An image we cannot price is not a free image. Both billable image providers
#: meter against a plan allowance (xKiro's 24h free-image allowance, a vendor
#: metered gateway) and neither reports a price on the response, so the honest
#: estimate is "unknown" -- which the ledger records as UNKNOWN_EXPOSURE.
EST_USD_PER_IMAGE = 0.0

#: Ledger category for a generation. "image", so a scene image costs against the
#: same daily cap as everything else the workspace buys.
COST_CATEGORY = "image"


class ImageProviderError(Exception):
    pass


# ---------------------------------------------------------------------------
# paid-submission wiring (Work 15.9 §3)
# ---------------------------------------------------------------------------


def current_workspace() -> str:
    """Workspace that owns this generation, or "" when there is no scope.

    An empty answer is NOT a licence to spend. Since §1 the shared helper
    refuses an ownerless billable operation before the POST, so "no scope"
    now means "refused", where it used to mean "sent anyway and booked against
    ``workspace_id=""``".
    """
    scope = _scope_key()
    return "" if scope == _GLOBAL_SCOPE else scope


def _paid(operation: str, *, provider: str, workspace_id: str = "",
          estimated_cost: float = 0.0,
          idempotency: IdempotencySupport = IdempotencySupport.UNSUPPORTED,
          ) -> tuple[PaidOperation, PaidProviderExecutor]:
    """One billable image operation, on the shared money mechanics.

    Returns ``(paid, executor)``. The HANDLE owns the money: it reserves before
    the request, persists the remote id the moment acceptance is known, and
    closes the reservation row in place. The EXECUTOR is provider-facing and
    knows only about requests: exactly one submit, bounded polling, and an
    artifact fetch that may be repeated because the artifact is already paid
    for.

    The two are deliberately separate. A handle that also built the request
    would be the generic ``url/payload/status/cancel`` adapter this extraction
    exists to refuse.
    """
    paid = paid_operation(
        provider=provider,
        operation=operation,
        workspace_id=str(workspace_id or current_workspace()).strip(),
        category=COST_CATEGORY,
        estimated_cost=estimated_cost,
        idempotency=idempotency,
        reservation_extra={"lane": "images"},
    )
    paid.bind(on_event=lambda phase, level, message:
              paid_event(paid, phase, level, message))
    return paid, paid.make_executor()


def _header(resp, name: str, default: str = "") -> str:
    """One response header, tolerating a thin response object.

    ``httpx`` answers with a mapping; a test double may answer with a plain
    dict or with nothing at all. A vendor request id is a reconciliation
    handle, not a reason to fail a render.
    """
    getter = getattr(getattr(resp, "headers", None), "get", None)
    return str(getter(name, default)) if callable(getter) else default


class BaseImageProvider(abc.ABC):
    name: str = "base"
    is_mock: bool = False

    @abc.abstractmethod
    def generate(self, prompt: str, *, size: str = "1024x576",
                 n: int = 1) -> list[bytes]:
        """Generate n images for prompt; returns raw image bytes."""

    @abc.abstractmethod
    def healthy(self) -> bool:
        """Cheap reachability/health probe."""


# ---------------------------------------------------------------------------
# pollinations — keyless public endpoint, free
# ---------------------------------------------------------------------------


class PollinationsImageProvider(BaseImageProvider):
    name = "pollinations"

    _URL = "https://image.pollinations.ai/prompt/{prompt}"
    _MAX_PROMPT = 380  # keep URLs sane
    _RETRIES = 3       # free keyless service; intermittent 500s are normal

    def __init__(self, timeout: float = 90.0):
        self.timeout = timeout

    def generate(self, prompt: str, *, size: str = "1024x576",
                 n: int = 1) -> list[bytes]:
        import httpx

        clean = re.sub(r"\s+", " ", (prompt or "").strip())[: self._MAX_PROMPT]
        if not clean:
            raise ImageProviderError("prompt is empty")
        out: list[bytes] = []
        for i in range(max(1, n)):
            # unique seed per image so n>1 does not return identical bytes
            seed = int(hashlib.sha256(f"{clean}:{i}:{time.time()}".encode()).hexdigest()[:8], 16)
            from urllib.parse import quote

            encoded = quote(clean, safe="")  # full path encoding (commas, spaces)
            url = (
                f"https://image.pollinations.ai/prompt/{encoded}"
                f"?width={size.split('x')[0]}&height={size.split('x')[1]}&seed={seed}&nologo=true"
            )
            last_exc: Exception | None = None
            for attempt in range(self._RETRIES):
                try:
                    resp = httpx.get(url, timeout=self.timeout, follow_redirects=True)
                    resp.raise_for_status()
                    ctype = resp.headers.get("content-type", "")
                    if not ctype.startswith("image/"):
                        raise ImageProviderError(
                            f"pollinations returned non-image content-type {ctype!r}"
                        )
                    out.append(resp.content)
                    last_exc = None
                    break
                except ImageProviderError:
                    raise  # non-image content is a hard error, not transient
                except Exception as exc:
                    last_exc = exc
                    if attempt < self._RETRIES - 1:
                        time.sleep(1.5 * (attempt + 1))  # backoff: 1.5s, 3s
            if last_exc is not None:
                raise ImageProviderError(
                    f"pollinations generate failed after {self._RETRIES} attempts: "
                    f"{type(last_exc).__name__}: {last_exc}"
                ) from last_exc
        return out

    def healthy(self) -> bool:
        try:
            import httpx

            resp = httpx.get(
                "https://image.pollinations.ai/models",
                timeout=8.0,
                follow_redirects=True,
            )
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# openai-compatible — self-hosted or vendor /images/generations
# ---------------------------------------------------------------------------


class OpenAICompatImageProvider(BaseImageProvider):
    name = "openai_compat"

    def __init__(self, base_url: str, api_key: str = "", timeout: float = 120.0,
                 model: str = ""):
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self.model = model

    def generate(self, prompt: str, *, size: str = "1024x576",
                 n: int = 1) -> list[bytes]:
        import base64

        import httpx

        if not self.base_url:
            raise ImageProviderError("openai_compat: base_url is not configured")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        body: dict = {"prompt": prompt, "n": max(1, n), "size": size,
                      "response_format": "b64_json"}
        if self.model:
            body["model"] = self.model
        estimate = EST_USD_PER_IMAGE * max(1, n)
        paid, executor = _paid(
            "openai_compat.images_generations", provider="openai_compatible",
            estimated_cost=estimate)

        def submit(_idempotency_key: str) -> RemoteSubmission:
            # ONE billable POST. Everything that can classify a lost response
            # (4xx = refused, 5xx/read timeout = possibly billed) is the
            # executor's job, so this adapter only builds and parses.
            resp = httpx.post(
                f"{self.base_url}/images/generations",
                json=body, headers=headers, timeout=self.timeout,
            )
            if resp.status_code != 200:
                # Same reasoning as providers/broll.py: the status has to reach
                # the paid classifier rather than being flattened into one
                # "generate failed" string.
                raise httpx.HTTPStatusError(
                    f"HTTP {resp.status_code}: {str(getattr(resp, 'text', ''))[:200]}",
                    request=(getattr(resp, "request", None)
                             or httpx.Request("POST", f"{self.base_url}/images/generations")),
                    response=resp)
            data = resp.json()
            items = data.get("data") or []
            if not items:
                # A 2xx with no artifact: billed, unfulfilled, unreconcilable.
                raise PaidSubmissionUnconfirmed(
                    provider=self.name,
                    detail="openai_compat returned 2xx with no images; the "
                           "generation may have been billed",
                )
            return RemoteSubmission(
                remote_id=_header(resp, "x-request-id"),
                artifact_path="inline", raw={"items": items})

        try:
            handle = executor.execute(submit, estimated_cost=estimate)
            out: list[bytes] = []
            for item in handle.raw.get("items") or []:
                if item.get("b64_json"):
                    out.append(base64.b64decode(item["b64_json"]))
                elif item.get("url"):
                    # The image is already PAID for. Fetching its bytes may be
                    # retried; asking the provider for it again would bill
                    # twice -- which is exactly what the pre-15.7 code invited
                    # by raising a bare transport error here.
                    out.append(executor.download(
                        lambda url=str(item["url"]): self._fetch(url),
                        remote_id=handle.remote_id, url=str(item["url"])))
            if not out:
                raise PaidArtifactUndownloadable(
                    provider=self.name, remote_id=handle.remote_id,
                    detail="openai_compat billed the request but returned no "
                           "image bytes")
        except ImageProviderError:
            raise
        except PaidJobError as exc:
            # An ambiguous submit and an undownloadable artifact are both paid
            # states, not provider outages. Whether money is gone is decided
            # ONCE, in the shared helper; the submission record carries the
            # state, the remote id and the exposure, so the message must too.
            absorb_paid_failure(paid, exc)
            raise ImageProviderError(str(exc)) from exc
        # The gateway metered this call and reported no price, so the honest
        # ledger outcome is UNKNOWN_EXPOSURE -- never a "$0 image".
        paid.mark_succeeded(
            amount_unknown=True,
            detail="vendor metered gateway; the response carries no price")
        return out

    def _fetch(self, url: str) -> bytes:
        import httpx

        resp = httpx.get(url, timeout=self.timeout, follow_redirects=True)
        if resp.status_code != 200:
            raise httpx.HTTPStatusError(
                f"HTTP {resp.status_code}: {str(getattr(resp, 'text', ''))[:200]}",
                request=(getattr(resp, "request", None)
                         or httpx.Request("GET", url)),
                response=resp)
        return resp.content

    def healthy(self) -> bool:
        if not self.base_url:
            return False
        try:
            import httpx

            resp = httpx.get(self.base_url, timeout=6.0, follow_redirects=True)
            return resp.status_code < 500
        except Exception:
            return False


# ---------------------------------------------------------------------------
# xkiro — async image jobs, free-tier model, CDN results
# ---------------------------------------------------------------------------


class XkiroImageProvider(BaseImageProvider):
    """xKiro /v1/images/generations — asynchronous job + poll + CDN download.

    Uses the free-tier sensenova model by default (billed as free images
    within the plan's 24h allowance). The API is async: submit returns a job
    id that must be polled until succeeded/failed/blocked. Sizes are limited
    to a fixed set; we snap to the nearest supported one.
    """

    name = "xkiro"

    _SUPPORTED = (256, 512, 1024, 1536, 1792)
    _FREE_MODEL = "sensenova/sensenova-u1.5-lite"
    _POLL_INTERVAL = 4.0
    _POLL_DEADLINE = 240.0

    def __init__(self, base_url: str, api_key: str, model: str = "",
                 timeout: float = 30.0):
        self.base_url = (base_url or "https://api.xkiro.com/v1").rstrip("/")
        self.api_key = api_key or ""
        self.model = model or self._FREE_MODEL
        self.timeout = timeout

    @staticmethod
    def _snap_size(size: str) -> str:
        """Snap any WxH to the nearest supported xKiro size."""
        try:
            w, h = (int(v) for v in size.lower().split("x"))
        except ValueError:
            return "1024x1024"
        landscape = w >= h
        if landscape:
            return "1792x1024" if w / h >= 1.5 else "1536x1024" if w / h >= 1.2 else "1024x1024"
        return "1024x1536" if h / w >= 1.2 else "1024x1024"

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def generate(self, prompt: str, *, size: str = "1024x576",
                 n: int = 1) -> list[bytes]:
        import httpx

        if not self.api_key:
            raise ImageProviderError("xkiro: no API key configured")
        snapped = self._snap_size(size)
        results: list[bytes] = []
        with httpx.Client(timeout=self.timeout) as client:
            for _ in range(max(1, n)):
                paid, executor = _paid(
                    "xkiro.image_job", provider="xkiro",
                    estimated_cost=EST_USD_PER_IMAGE)
                results.append(self._submit_one(paid, executor, client, prompt,
                                                snapped))
                # One billable image per job, and the plan allowance is not
                # priced per request, so the row is an UNKNOWN exposure rather
                # than a fabricated zero.
                paid.mark_succeeded(
                    amount_unknown=True,
                    detail="plan allowance is not priced per request")
        return results

    def _submit_one(self, paid: PaidOperation, executor: PaidProviderExecutor,
                    client, prompt: str, snapped: str) -> bytes:
        """One billable async image job: submit once, poll, download.

        The job id returned by the POST is persisted the moment acceptance is
        known -- before the first poll -- on BOTH the caller's operation and the
        reservation row, so a crash between here and the CDN download leaves a
        recovery handle instead of an unexplained charge.
        """

        def submit(_idempotency_key: str) -> RemoteSubmission:
            resp = client.post(
                f"{self.base_url}/images/generations",
                headers=self._headers(),
                json={
                    "model": self.model,
                    "prompt": prompt,
                    "n": 1,  # API currently requires 1 per job
                    "size": snapped,
                },
            )
            resp.raise_for_status()
            job = resp.json()
            job_id = str(job.get("id") or "")
            if not job_id:
                # Accepted, billed, and unidentifiable: not a rejection.
                raise PaidSubmissionUnconfirmed(
                    provider=self.name,
                    detail=f"xkiro submit returned no job id: {str(job)[:120]}")
            return RemoteSubmission(remote_id=job_id, raw=job)

        try:
            handle = executor.execute(submit, estimated_cost=EST_USD_PER_IMAGE)
            return self._poll_and_download(client, handle.remote_id, prompt,
                                           executor)
        except ImageProviderError as exc:
            # The POST is already accepted, so the plan allowance may be spent
            # even though no image came back. The ledger question -- "may this
            # have been billed?" -- is genuinely unanswerable here and there is
            # no status endpoint that would price it later, so the reservation
            # is KEPT and marked as an unknown exposure. Never released, never
            # booked as zero.
            paid.mark_unknown(f"xkiro accepted job {paid.remote_id or '(unknown)'}"
                              f" but delivered no image: {exc}")
            raise
        except PaidJobError as exc:
            absorb_paid_failure(paid, exc)
            raise ImageProviderError(str(exc)) from exc

    def _poll_and_download(self, client, job_id: str, prompt: str,
                           executor: PaidProviderExecutor) -> bytes:
        """Follow ONE accepted job to its artifact. Never re-submits.

        Polling and the CDN fetch are both safe to repeat, so they go through
        the executor's ``poll_remote`` / ``download``; neither of those can
        issue a second billable POST.
        """
        deadline = time.monotonic() + self._POLL_DEADLINE

        def fetch(phase_timeout: float) -> tuple[str, dict]:
            time.sleep(self._POLL_INTERVAL)
            resp = client.get(
                f"{self.base_url}/images/generations/{job_id}",
                headers=self._headers(), timeout=phase_timeout,
            )
            resp.raise_for_status()
            job = resp.json()
            return str(job.get("status") or "unknown"), job

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                # The job is PAID for and still unknown. Reconciliation handle,
                # not a free failure.
                raise ImageProviderError(
                    f"xkiro job {job_id} timed out after "
                    f"{self._POLL_DEADLINE:.0f}s; it may still complete and is "
                    f"billable -- poll job {job_id} instead of resubmitting")
            status, job = executor.poll_remote(fetch, deadline_seconds=remaining)
            if status in ("succeeded", "failed", "blocked", "cancelled"):
                break
            # A non-terminal answer: keep waiting on the SAME job id.
        if status != "succeeded":
            msg = (job.get("error") or {}).get("message", status)
            raise ImageProviderError(f"xkiro job {status}: {msg}")
        url = ((job.get("data") or [{}])[0].get("url") or "")
        if not url:
            raise ImageProviderError("xkiro job succeeded but no URL returned")
        content = executor.download(
            lambda: self._cdn_fetch(client, url), remote_id=job_id, url=url,
            attempts=2)
        if len(content) < 1024:
            raise PaidArtifactUndownloadable(
                provider=self.name, remote_id=job_id, url=url,
                detail="xkiro CDN image suspiciously small")
        return content

    @staticmethod
    def _cdn_fetch(client, url: str) -> bytes:
        dl = client.get(url, timeout=90.0, follow_redirects=True)
        dl.raise_for_status()
        return dl.content

    def healthy(self) -> bool:
        import httpx

        if not self.api_key:
            return False
        try:
            resp = httpx.get(
                f"{self.base_url}/models",
                params={"modality": "image"},
                headers={"Authorization": f"Bearer {self.api_key}"},
                timeout=10.0,
            )
            return resp.status_code == 200
        except Exception:
            return False


# ---------------------------------------------------------------------------
# mock — simulation only, deterministic placeholder, labeled
# ---------------------------------------------------------------------------


def _gradient_png(width: int, height: int, seed_text: str) -> bytes:
    """Tiny deterministic PNG (no deps beyond stdlib zlib/struct)."""
    import struct
    import zlib

    h = int(hashlib.sha256(seed_text.encode()).hexdigest()[:8], 16)

    def chunk(tag: bytes, data: bytes) -> bytes:
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    rows = []
    for y in range(height):
        row = b"\x00"
        for x in range(width):
            r = (x * 255 // max(1, width)) ^ (h & 0x55)
            g = (y * 255 // max(1, height)) ^ ((h >> 8) & 0x55)
            b = (h >> 16) & 0xFF
            row += bytes((r, g, b))
        rows.append(row)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", ihdr)
            + chunk(b"IDAT", zlib.compress(b"".join(rows)))
            + chunk(b"IEND", b""))


class MockImageProvider(BaseImageProvider):
    name = "mock"
    is_mock = True

    def generate(self, prompt: str, *, size: str = "1024x576",
                 n: int = 1) -> list[bytes]:
        try:
            w, h = (int(v) for v in size.lower().split("x"))
        except ValueError:
            w, h = 640, 360
        w, h = max(16, min(1280, w)), max(16, min(1280, h))
        return [_gradient_png(w, h, f"{prompt}:{i}") for i in range(max(1, n))]

    def healthy(self) -> bool:
        return True


# ---------------------------------------------------------------------------
# factory / status
# ---------------------------------------------------------------------------

_PROVIDERS = {
    "pexels": None,  # lazily imported (separate module avoids an import cycle)
    "xkiro": XkiroImageProvider,
    "pollinations": PollinationsImageProvider,
    "openai_compat": OpenAICompatImageProvider,
    "mock": MockImageProvider,
}

_instances: dict[str, BaseImageProvider] = {}
_GLOBAL_SCOPE = "__global__"


def _scope_key() -> str:
    try:
        from app.services.provider_settings import current_workspace_id

        return current_workspace_id() or _GLOBAL_SCOPE
    except Exception:
        return _GLOBAL_SCOPE


def reset_image_provider(workspace_id: str | None = None) -> None:
    if workspace_id:
        _instances.pop(workspace_id, None)
    else:
        _instances.clear()


def get_image_provider():
    from app.core.config import settings
    from app.services.provider_settings import get_credential

    scope = _scope_key()
    if scope in _instances:
        return _instances[scope]
    name = (settings.image_provider or "pollinations").lower().strip()
    if name == "pexels":
        from app.providers.images_pexels import PexelsImageProvider

        key, _psrc = get_credential("pexels.api_key")
        if not key:
            key = settings.pexels_api_key
        _instances[scope] = PexelsImageProvider(api_key=key or "")
        return _instances[scope]
    cls = _PROVIDERS.get(name)
    if cls is None:
        raise ImageProviderError(
            f"IMAGE_PROVIDER '{name}' is not one of {sorted(_PROVIDERS)}"
        )
    if cls is XkiroImageProvider:
        base_url, _ = get_credential("image.openai_base_url")
        key, _ksrc = get_credential("image.openai_api_key")
        model, _msrc = get_credential("image.openai_model")
        if not key:
            # fall back to the shared LLM key when no dedicated image key set
            key, _ = get_credential("llm.api_key")
        _instances[scope] = XkiroImageProvider(
            base_url=base_url or "https://api.xkiro.com/v1",
            api_key=key or "",
            model=model or "",
        )
    elif cls is PollinationsImageProvider:
        _instances[scope] = PollinationsImageProvider()
    elif cls is OpenAICompatImageProvider:
        base_url, _src = get_credential("image.openai_base_url")
        key, _ksrc = get_credential("image.openai_api_key")
        model, _msrc = get_credential("image.openai_model")
        _instances[scope] = OpenAICompatImageProvider(
            base_url=base_url or "",
            api_key=key or "",
            model=model or "",
        )
    else:
        _instances[scope] = MockImageProvider()
    return _instances[scope]


def image_provider_status() -> dict:
    from app.core.config import settings

    name = (settings.image_provider or "pollinations").lower().strip()
    info: dict = {
        "provider": name,
        "is_mock": name == "mock",
        "healthy": False,
        "error": "",
    }
    try:
        provider = get_image_provider()
        info["healthy"] = provider.healthy()
    except ImageProviderError as exc:
        info["error"] = str(exc)
    except Exception as exc:
        info["error"] = f"{type(exc).__name__}: {exc}"
    return info

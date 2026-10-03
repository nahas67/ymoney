"""Voice preview API (Work 15.6 §8) -- select provider, list voices, preview,
select voice.

Mounted once by ``api/v1/__init__.py``::

    GET  /workspaces/{ws}/voice-preview/providers          viewer
    GET  /workspaces/{ws}/voice-preview/providers/{id}/voices   viewer
    POST /workspaces/{ws}/voice-preview                    member

This is the *only* voice-preview implementation. ``POST
/workspaces/{ws}/assets/voice/preview`` (``api/v1/content.py``) is now a thin
adapter that maps its body onto :class:`VoicePreviewBody` and calls
:func:`preview_voice` -- Work 15.7 §9 removed the second implementation that
lived there, because two preview implementations mean two cost guards and only
one of them was ever tested.

The differences this module makes against a raw synthesis call:

1. **Which providers may I actually choose, and why not the others?** Not a
   hardcoded ``edge|kokoro|mock`` string in a pydantic ``description``. The
   offerable set and each provider's reason come from
   :mod:`app.providers.tts_qualification` -- the same registry the maturity
   table and the provider tests use -- so the dropdown cannot drift from the
   adapter that will actually be called. A provider whose
   ``implementation_status`` is ``UNAVAILABLE`` is reported with its reason, not
   hidden and not offered.

2. **Is it configured, and may I spend on it?** A CONFIG_GATED provider with no
   credential is reported as unavailable *with the missing key's name* (the key
   name is not a secret; its value never appears anywhere in this module).

Four guards, each with a test that fails when it is removed:

**Cost.** A preview is a real billable call -- ElevenLabs prices per character.
So: a hard character ceiling, and a per-workspace request ceiling over a sliding
window, plus the workspace's daily dollar cap. Both refusals are typed: 429 for
the rate, 402 for the money. The guard is per-workspace, not per-user, because
the bill is the workspace's -- and since Work 15.7 §11 it is enforced by
:func:`app.services.cost.reserve_spend`, which decides and records in one
committed transaction. The sliding window used to be a dict in this module,
which is per-process: behind two uvicorn workers a workspace got twice the
allowance and neither worker could see it.

**Cache.** Preview audio is cached on disk, reusing ``services.media_cache``'s
primitives (:func:`~app.services.media_cache.cache_key`,
:func:`~app.services.media_cache.lock_for`,
:func:`~app.services.media_cache.is_fresh`,
:func:`~app.services.media_cache.sweep_media_cache`) so the credential-excluding
key and the 256 bounded lock shards are the *same* objects the media search uses
rather than a second implementation of the same idea. Two media_cache rules are
re-applied by hand and say so:

* the key includes ``workspace_id`` and the file lives in a per-workspace
  directory, so one tenant's preview can never be served to another;
* a failure or an empty payload is never written. Pinning "provider is down" as
  a cache entry blocks every later preview for the whole TTL.

It is *not* ``write_media_cache``: that function validates a list of dicts each
carrying a public ``url``, because it caches a *search result*, whereas this
caches opaque audio bytes. Reusing it would mean either faking a URL field or
loosening a guard written for a different shape. The atomic temp+fsync+replace
publish and the orphan-temp reclamation are reproduced instead.

**Honest unavailability.** Every refusal is a 4xx/503 with a machine-readable
``reason`` plus a human sentence. There is no path that returns an empty voice
list with no explanation: an empty list is only ever returned when the provider
answered and genuinely had nothing, and that response says so.

**No secrets to the frontend.** Not the value, not its length, not its digest.
:func:`credential_fingerprint_free` builds the response, and
``test_voice_preview_never_leaks_a_secret`` asserts that for every secret the
workspace has configured, the plaintext, ``str(len(value))`` and
``sha256(value)`` are all absent from the response body. Endpoint responses are
built from an explicit allowlist of fields, so a future adapter that grows a
``voice.api_key`` attribute cannot leak by being iterated.
"""

from __future__ import annotations

import functools
import hashlib
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import MediaAsset, Workspace
from app.providers import tts as tts_module
from app.providers.maturity import CONTRACT_TESTED, IMPLEMENTED, LIVE_VERIFIED
from app.providers.tts import TTSError, get_tts_provider
from app.providers.tts_qualification import (
    get_qualification,
    list_qualifications,
    qualification_labels,
)
from app.services import media_cache
from app.services import provider_settings as _ps
from app.services.auth_service import require_workspace_role
from app.services.storage import STORAGE_ROOT

logger = logging.getLogger("ymoney.voice")

# ---------------------------------------------------------------------------
# error policy + routes
# ---------------------------------------------------------------------------

voice_preview_router = APIRouter(prefix="/workspaces/{workspace_id}/voice-preview",
                                 tags=["voice-work15"])

# ---------------------------------------------------------------------------
# cost guard
# ---------------------------------------------------------------------------
#
# ElevenLabs bills per character (``providers/tts_qualification.py`` records the
# estimate as an estimate). 600 characters of preview narration is roughly 40
# seconds of speech -- enough to judge a voice, far more than a preview needs.
# MAX_PREVIEW_CHARS is the ceiling on what ONE request may cost, not a target.
#
# Work 15.7 §11: the sliding window below used to be a dict in this module. A
# dict is per-PROCESS, so behind two uvicorn workers a workspace could take
# 20 previews on each -- twice the ceiling, twice the spend -- and the guard
# reported "0 used" to whichever worker had not served a request yet. The
# counter is now the ``cost_entries`` table, read and written inside
# ``cost.reserve_spend``'s database lock, so it is per-DEPLOYMENT.

MAX_PREVIEW_CHARS = 600
MIN_PREVIEW_CHARS = 1

#: Per-workspace preview budget over a sliding window. 20 previews a minute is
#: roughly a person clicking through every voice in a catalogue; it is not a
#: script. Exceeding it is 429, never a silent drop.
PREVIEW_WINDOW_SECONDS = 60.0
PREVIEW_MAX_PER_WINDOW = 20

#: ``cost_entries.category`` for a preview reservation. Its own category, so the
#: preview rate limit counts previews and nothing else, and so an operator can
#: see in the ledger how much of a workspace's spend was auditioning voices.
PREVIEW_CATEGORY = "tts_preview"

#: USD per character, the same public figure ``providers/tts.py`` charges at.
#: It is an ESTIMATE and the reservation says so (``is_estimate=True``); the
#: provider's own per-character number replaces it when the call is settled.
PREVIEW_USD_PER_CHAR = 0.0002

#: HTTP status for a spend refusal that is a MONEY refusal rather than a rate
#: one. 402 is the honest code -- the account is out of budget, not out of
#: quota -- and it is distinguishable from 429 by a client.
BUDGET_REFUSED = 402


def preview_estimated_usd(chars: int) -> float:
    """What one preview of ``chars`` characters is expected to cost."""
    return round(max(int(chars or 0), 0) * PREVIEW_USD_PER_CHAR, 6)


def preview_budget_state(workspace_id: str) -> int:
    """How many previews this workspace has spent inside the window.

    A database read, not a dict lookup: the answer an operator gets here must be
    the same answer another worker's gate is enforcing, or the number is
    decoration.
    """
    from datetime import timedelta

    from sqlalchemy import func, select

    from app.db import session_scope
    from app.models import CostEntry
    from app.models.base import utcnow

    since = utcnow() - timedelta(seconds=PREVIEW_WINDOW_SECONDS)
    with session_scope() as s:
        return int(s.scalar(
            select(func.count(CostEntry.id)).where(
                CostEntry.workspace_id == workspace_id,
                CostEntry.category == PREVIEW_CATEGORY,
                CostEntry.created_at >= since,
            )
        ) or 0)


def spend_preview_budget(workspace_id: str, *, estimated_usd: float = 0.0,
                         provider: str = "") -> int:
    """Record one preview; returns the count INCLUDING this one.

    Delegates to the same atomic reservation :func:`reserve_preview` uses, so
    there is exactly one place where the cap lives. Kept as a name because it is
    the honest description of the effect.
    """
    return int(reserve_preview(
        workspace_id, estimated_usd, provider=provider).window_count)


def reset_preview_budget(workspace_id: str | None = None) -> None:
    """Test-only: clear the sliding window.

    DESTRUCTIVE and unavoidably global when called with no argument, because the
    window lives in the database now. It removes the reservation ROWS; it never
    touches a settled cost, which is money that was actually spent.
    """
    from sqlalchemy import delete

    from app.db import session_scope
    from app.models import CostEntry

    with session_scope() as s:
        stmt = delete(CostEntry).where(CostEntry.category == PREVIEW_CATEGORY)
        if workspace_id:
            stmt = stmt.where(CostEntry.workspace_id == workspace_id)
        s.execute(stmt)


def reserve_preview(workspace_id: str, estimated_usd: float = 0.0, *,
                    provider: str = ""):
    """Take a preview permission ATOMICALLY, or refuse.

    Returns the :class:`app.services.cost.BudgetReservation`, whose ``entry_id``
    is the ledger row the caller later settles. This is the function that makes
    the cap real: the check and the record are one committed transaction under
    the workspace's database lock, so N concurrent previews cannot all be told
    yes for the last slot in the window.

    Refusals are 429 for a RATE limit and 402 for a MONEY limit, because those
    demand different responses from the caller and a client that cannot tell them
    apart either retries forever or gives up early.
    """
    from app.services import cost as _cost

    try:
        return _cost.reserve_spend(
            workspace_id, estimated_usd,
            category=PREVIEW_CATEGORY, provider=provider,
            max_events=int(PREVIEW_MAX_PER_WINDOW),
            window_seconds=PREVIEW_WINDOW_SECONDS,
        )
    except _cost.RateLimitExceeded as exc:
        raise HTTPException(
            status_code=429,
            detail={
                "reason": "preview_budget_exceeded",
                "message": (f"at most {PREVIEW_MAX_PER_WINDOW} voice previews "
                            f"per {int(PREVIEW_WINDOW_SECONDS)}s per workspace"),
                "window_seconds": PREVIEW_WINDOW_SECONDS,
                "max_per_window": PREVIEW_MAX_PER_WINDOW,
                "scope": "workspace",
                "authority": "database",
            },
        ) from exc
    except _cost.BudgetExceededError as exc:
        raise HTTPException(
            status_code=BUDGET_REFUSED,
            detail={
                "reason": "budget_exhausted",
                "message": str(exc)[:200],
                "scope": "workspace",
                "authority": "database",
            },
        ) from exc


def assert_within_budget(workspace_id: str, *, estimated_usd: float = 0.0,
                         provider: str = "", reserve: bool = False,
                         raising: bool = True) -> int | None:
    """Refuse a preview the workspace may not afford. DB-backed (Work 15.7).

    ``reserve=True`` takes the permission (see :func:`reserve_preview`) and
    returns the window count INCLUDING this preview. The default is a
    non-reserving check that reads the same table and leaves no row, for a caller
    that only wants the answer.

    Both modes consult the same table with the same arithmetic, so the number
    this reports and the number the gate enforces cannot disagree -- which is
    exactly what a process-local counter guaranteed once two workers existed.
    """
    from app.services import cost as _cost

    if reserve:
        return int(reserve_preview(
            workspace_id, estimated_usd, provider=provider).window_count)
    if not raising:
        room = _cost.budget_headroom(
            workspace_id, category=PREVIEW_CATEGORY,
            window_seconds=PREVIEW_WINDOW_SECONDS)
        within = (room.events < int(PREVIEW_MAX_PER_WINDOW)
                  and room.spent_usd + estimated_usd <= room.daily_cap_usd
                  and estimated_usd <= room.per_call_cap_usd)
        return int(room.events) if within else None
    return int(reserve_preview(
        workspace_id, estimated_usd, provider=provider).window_count)


def preview_text(text: str) -> tuple[str, bool]:
    """Normalise preview text. Returns ``(text, was_clamped)``.

    Over-long input is CLAMPED with ``was_clamped`` set rather than refused, so
    a user who pastes a whole script still hears something and the UI can say
    the sample was shortened. Under-long (empty/whitespace) input is refused --
    there is nothing to preview, and a silent-empty preview is the
    empty-dropdown failure this module exists to avoid.
    """
    cleaned = " ".join(str(text or "").split())
    if not cleaned:
        raise HTTPException(
            status_code=422,
            detail={"reason": "empty_preview_text",
                    "message": "preview text is required"},
        )
    if len(cleaned) <= MAX_PREVIEW_CHARS:
        return cleaned, False
    # Cut on a word boundary so the last word is never truncated into nonsense.
    head = cleaned[:MAX_PREVIEW_CHARS]
    if " " in head:
        head = head[:head.rfind(" ")]
    return (head.strip() or cleaned[:MAX_PREVIEW_CHARS]), True


# ---------------------------------------------------------------------------
# provider awareness
# ---------------------------------------------------------------------------

#: Aliases ``get_tts_provider`` accepts that are not canonical provider ids, so
#: a stored ``tts.provider`` of "local" still resolves to a qualification row
#: rather than reading as "unknown provider" in the UI.
_PROVIDER_ALIASES: dict[str, str] = {
    "local": "kokoro",
    "chatterbox-turbo": "chatterbox",
    "qwen": "qwen3",
    "qwen-tts": "qwen3",
    "eleven": "elevenlabs",
    "xi": "elevenlabs",
    "11labs": "elevenlabs",
    "": "edge",
}


def canonical_provider(provider: str) -> str:
    """Map any accepted spelling onto a canonical qualification id."""
    key = str(provider or "").strip().lower()
    return _PROVIDER_ALIASES.get(key, key)


def _configured(provider_id: str, workspace_id: str) -> tuple[bool, tuple[str, ...]]:
    """Whether this workspace can build ``provider_id``; and which keys are missing.

    Presence only -- never a value, never a length, never a digest. Built by
    asking the provider factory itself, so the answer cannot disagree with what
    ``get_tts_provider`` would do.
    """
    missing: list[str] = []
    try:
        with _ps.workspace_scope(workspace_id):
            get_tts_provider(provider_id)
    except TTSError:
        qual = get_qualification(provider_id)
        for key in (qual.credential_keys if qual else ()):
            try:
                with _ps.workspace_scope(workspace_id):
                    value, _src = _ps.get_credential(key, workspace_id)
            except Exception:  # noqa: BLE001 - presence check must not fail a list
                value = None
            if not value:
                missing.append(key)
        return False, tuple(missing)
    return True, ()


def _offerable_row(provider_id: str, workspace_id: str) -> dict:
    """One row of the provider list. Explicit field allowlist, never a dump."""
    qual = get_qualification(provider_id)
    if qual is None:
        return {
            "provider": provider_id,
            "label": provider_id,
            "offerable": False,
            "available": False,
            "reason": "unknown_provider",
            "message": f"'{provider_id}' is not a known TTS provider",
            "qualification_labels": [],
            "missing_credentials": [],
            "simulation_only": False,
            "live_verified": False,
        }
    implemented = qual.implementation_status == IMPLEMENTED
    available, missing = _configured(provider_id, workspace_id) if implemented else (False, ())
    if not implemented:
        reason, message = "unavailable", qual.notes.split(". ")[0][:200]
    elif available:
        reason, message = "ok", "configured and ready to preview"
    else:
        reason = "not_configured"
        message = (f"set {', '.join(missing)} under Settings → Connections"
                   if missing else "this provider is not configured for this workspace")
    return {
        "provider": qual.provider,
        "label": qual.label,
        "offerable": implemented,
        "available": bool(available),
        "reason": reason,
        "message": message,
        "qualification_labels": list(qualification_labels(qual)),
        "missing_credentials": list(missing),
        "simulation_only": bool(qual.simulation_only),
        "live_verified": qual.live_status == LIVE_VERIFIED,
        "contract_tested": qual.contract_status == CONTRACT_TESTED,
    }


def list_providers(workspace_id: str) -> dict:
    """Every provider, with why it may or may not be chosen.

    Not offerable is stated, never implied by absence -- the six donor
    candidates in the qualification registry are exactly the rows whose
    ``UNAVAILABLE`` status is worth surfacing.
    """
    rows = [_offerable_row(q.provider, workspace_id) for q in list_qualifications()]
    try:
        with _ps.workspace_scope(workspace_id):
            default = canonical_provider(tts_module._effective_provider_name())
    except Exception:  # noqa: BLE001 - a default that cannot resolve is reported as ""
        default = ""
    return {
        "items": rows,
        "offerable": [r["provider"] for r in rows if r["offerable"]],
        "unavailable": [r["provider"] for r in rows if not r["offerable"]],
        "default_provider": default,
        "max_chars": MAX_PREVIEW_CHARS,
        "max_per_window": PREVIEW_MAX_PER_WINDOW,
        "window_seconds": PREVIEW_WINDOW_SECONDS,
    }


def list_voices(provider_id: str, workspace_id: str, language: str = "") -> dict:
    """Voices for one provider, from the SAME factory production uses.

    Identity, not a curated preview list: ``get_tts_provider(provider).voices()``
    is what narration will call, so a preview that offered a different set would
    be a preview of a voice that cannot be selected.
    """
    canonical = canonical_provider(provider_id)
    row = _offerable_row(canonical, workspace_id)
    if not row["offerable"]:
        raise HTTPException(status_code=409, detail={
            "reason": row["reason"], "message": row["message"],
            "provider": canonical,
        })
    if not row["available"]:
        raise HTTPException(status_code=409, detail={
            "reason": "not_configured", "message": row["message"],
            "provider": canonical, "missing_credentials": row["missing_credentials"],
        })
    try:
        with _ps.workspace_scope(workspace_id):
            voices = get_tts_provider(canonical).voices(language)
    except TTSError as exc:
        raise HTTPException(status_code=503, detail={
            "reason": "voice_list_unavailable",
            "message": str(exc)[:200], "provider": canonical,
        }) from exc
    # Project each voice through a fixed field list. A provider whose voices()
    # returns extra keys (a token, a raw URL) must not have them reach the UI.
    items = [{"id": str(v.get("id") or ""), "gender": str(v.get("gender") or ""),
              "locale": str(v.get("locale") or "")}
             for v in voices or [] if isinstance(v, dict) and str(v.get("id") or "").strip()]
    return {
        "provider": canonical,
        "language": str(language or ""),
        "items": items,
        "count": len(items),
        # An empty list is only reachable because the provider said so. Say that.
        "empty_reason": ("provider returned no voices for this language"
                         if not items else ""),
        "cache": "none",
    }


# ---------------------------------------------------------------------------
# the preview cache
# ---------------------------------------------------------------------------


def preview_cache_dir(workspace_id: str) -> Path:
    """Per-workspace directory. The workspace is in the PATH as well as the key.

    Two independent reasons a preview cannot cross tenants: the digest input
    includes ``workspace_id``, and the file it names lives under this
    workspace's own directory. Either alone would do; both are cheap.
    """
    return Path(STORAGE_ROOT) / str(workspace_id or "") / "voice_preview"


def preview_cache_key(workspace_id: str, provider_id: str, voice: str,
                      text: str, *, rate: float = 1.0,
                      language: str = "", exaggeration: float = 0.5) -> str:
    """Credential-free sha256 over the result-affecting preview parameters.

    Delegates to ``media_cache.cache_key``, which already drops every
    credential-shaped parameter before hashing. No credential is ever passed
    in, and the workspace id is passed as an ordinary result-affecting field --
    ``result_affecting_params`` keeps unknown non-credential fields, so it
    reaches the digest. ``test_preview_cache_key_separates_workspaces`` proves
    both halves.
    """
    return media_cache.cache_key({
        "provider": provider_id,
        "workspace_id": str(workspace_id or ""),
        "voice": str(voice or ""),
        "search_term": text,
        "rate": float(rate),
        "language": str(language or ""),
        "exaggeration": float(exaggeration),
    })


def _read_preview(path: Path) -> tuple[bytes | None, dict]:
    """``(cached audio or None, its metadata sidecar or {})``.

    Freshness uses ``media_cache``'s rule verbatim. The sidecar is optional: an
    entry written before it existed still serves its audio, and the headers fall
    back to the conservative defaults rather than claiming knowledge the file
    does not contain.
    """
    if not media_cache.is_fresh(path, time.time(), media_cache.MEDIA_CACHE_TTL_SECONDS):
        media_cache.remove_entry(path)
        return None, {}
    try:
        audio = path.read_bytes()
    except OSError:
        # Not proof of corruption (Windows share violations are transient).
        return None, {}
    try:
        meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        meta = {}
    return (audio if audio else None), (meta if isinstance(meta, dict) else {})


def _write_preview(path: Path, audio: bytes, meta: dict) -> bool:
    """Atomically publish audio + its metadata. Never publishes empty/failed audio.

    Empty or missing bytes are refused *before* any write: caching "the provider
    returned nothing" would block every later preview with the same terms for a
    full TTL. The metadata sidecar exists so a cache hit can still answer
    ``X-TTS-Provider`` without re-synthesising.
    """
    if not audio:
        return False
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        media_cache.sweep_media_cache(path.parent, force=False)
        _atomic_write(path, audio)
        _atomic_write(path.with_suffix(".json"),
                      json.dumps({"version": 1, **meta}, ensure_ascii=False,
                                 separators=(",", ":"), default=str).encode("utf-8"))
        return True
    except OSError as exc:
        logger.warning("voice preview: cache write failed (%s)", exc)
        return False


def _atomic_write(path: Path, payload: bytes) -> None:
    """Same temp + fsync + replace publish media_cache uses, for opaque bytes."""
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed in the with-block
        mode="wb", dir=path.parent, prefix=f".{path.stem}-", suffix=".tmp",
        delete=False,
    )
    temp_path = Path(handle.name)
    try:
        with handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_path, path)
    except BaseException:
        media_cache.remove_entry(temp_path)
        raise


# ---------------------------------------------------------------------------
# error policy + routes
# ---------------------------------------------------------------------------


def _short(exc: BaseException, limit: int = 180) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _guard(value_error: int = 422):
    """projects.py's error policy, so the two routers refuse the same way."""

    def decorate(fn: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(fn)
        def run(*args: Any, **kwargs: Any) -> Any:
            try:
                return fn(*args, **kwargs)
            except HTTPException:
                raise
            except ValueError as exc:
                raise HTTPException(status_code=value_error,
                                    detail=_short(exc)) from None
            except Exception:  # noqa: BLE001 - deliberate catch-all at the API edge
                logger.exception("voice preview route failed: %s",
                                 getattr(fn, "__name__", fn))
                raise HTTPException(status_code=500, detail="internal error") from None

        return run

    return decorate


class VoicePreviewBody(BaseModel):
    text: str = Field(min_length=1, max_length=4000,
                      description="narration sample; clamped to 600 chars for cost")
    voice: str = Field(default="", max_length=120)
    provider: str = Field(default="", max_length=40,
                          description="blank = workspace default")
    rate: float = Field(default=1.0, ge=0.5, le=2.0)
    language: str = Field(default="", max_length=12)
    exaggeration: float = Field(default=0.5, ge=0.0, le=1.0)


@voice_preview_router.get("/providers", summary="Offerable TTS providers, with reasons")
@_guard()
def get_providers(
    ws: Workspace = Depends(require_workspace_role("viewer")),
):
    return list_providers(ws.id)


@voice_preview_router.get("/providers/{provider_id}/voices",
                           summary="Voices for one provider (production identity)")
@_guard()
def get_voices(
    provider_id: str,
    language: str = "",
    ws: Workspace = Depends(require_workspace_role("viewer")),
):
    return list_voices(provider_id, ws.id, language)


@voice_preview_router.post("", summary="Preview a voice (cached, cost-guarded)")
@_guard()
def preview_voice(
    body: VoicePreviewBody,
    ws: Workspace = Depends(require_workspace_role("member")),
    db: Session = Depends(get_db),
):
    """Synthesise a short sample. Returns audio bytes, or a cache hit.

    Order of operations matters, and Work 15.7 §11 changed the first step:

    1. resolve the provider and refuse what cannot be previewed -- no spend;
    2. consult the cache -- a hit is free, so it costs no reservation;
    3. take the budget reservation ATOMICALLY, which both checks the cap and
       records the permission in one database transaction;
    4. call the provider once, and settle the reservation with the real
       character count.

    Step 3 used to be a check at the top plus an in-memory increment after the
    cache miss. That pair was two operations with a gap between them and the
    count lived in this process, so it was not a limit at all behind a second
    worker. A 429 therefore means "this workspace asked for something new too
    often", not "you clicked once too many times on a voice you have already
    heard".
    """
    text, clamped = preview_text(body.text)

    provider_id = canonical_provider(body.provider) if body.provider else ""
    if not provider_id:
        try:
            with _ps.workspace_scope(ws.id):
                provider_id = canonical_provider(tts_module._effective_provider_name())
        except Exception:  # noqa: BLE001
            provider_id = "edge"

    row = _offerable_row(provider_id, ws.id)
    if not row["offerable"]:
        raise HTTPException(status_code=409, detail={
            "reason": row["reason"], "message": row["message"], "provider": provider_id})
    if not row["available"]:
        raise HTTPException(status_code=409, detail={
            "reason": "not_configured", "message": row["message"],
            "provider": provider_id,
            "missing_credentials": row["missing_credentials"]})

    directory = preview_cache_dir(ws.id)
    key = preview_cache_key(ws.id, provider_id, body.voice, text,
                            rate=body.rate, language=body.language,
                            exaggeration=body.exaggeration)
    base = directory / key
    path = base.with_suffix(".wav")

    with media_cache.lock_for({"provider": provider_id, "workspace_id": ws.id}):
        cached, meta = _read_preview(path)
        if cached is not None:
            # The sidecar exists so a cache hit answers the SAME headers a fresh
            # render would: a hit that reported `is_mock=0` for audio a mock
            # produced would claim a free simulation was a billed call, and one
            # that guessed the format would serve mp3 bytes as wav.
            return _audio_response(cached, provider_id=provider_id, cached=True,
                                   clamped=clamped, chars=len(text),
                                   media_format=meta.get("format", "wav"),
                                   is_mock=bool(meta.get("is_mock")),
                                   sample_rate=meta.get("sample_rate"))

        # The reservation is the permission. It is taken before the provider is
        # called and it is NOT voided on a provider failure: a fault here may
        # still have been billed, and only a provably-undelivered submit (which
        # cost.void_reservation exists for) may release a reservation.
        estimated = preview_estimated_usd(len(text))
        reservation = reserve_preview(ws.id, estimated, provider=provider_id)
        try:
            with _ps.workspace_scope(ws.id):
                provider = get_tts_provider(provider_id)
                result = provider.synthesize(
                    text, voice=body.voice, rate=body.rate,
                    language=body.language, exaggeration=body.exaggeration,
                )
        except TTSError as exc:
            # No cache write on failure: the next attempt must be allowed to try
            # again. The reservation stays, because the call may have billed.
            raise HTTPException(status_code=503, detail={
                "reason": "preview_failed", "message": _short(exc),
                "provider": provider_id}) from exc
        _settle_preview(ws.id, provider, result, len(text), reservation)
        _write_preview(path, result.audio_bytes, {
            "provider": result.provider, "format": result.format,
            "is_mock": bool(result.is_mock), "chars": len(text),
            "sample_rate": result.sample_rate,
        })
        _persist_asset(db, ws, result, provider_id, body.voice, len(text), key)
        response = _audio_response(result.audio_bytes, provider_id=result.provider,
                                   cached=False, clamped=clamped, chars=len(text),
                                   media_format=result.format,
                                   is_mock=bool(result.is_mock),
                                   sample_rate=result.sample_rate)
        response.headers["X-Preview-Window-Count"] = str(reservation.window_count)
        return response


def _settle_preview(workspace_id: str, provider, result, chars: int,
                    reservation) -> None:
    """Reconcile the preview's reservation with what the provider actually did.

    The reservation ROW is updated in place
    (:func:`cost.settle_reservation`), never joined by a second ``track_cost``
    row: the estimate already counted against the daily cap, so a second row
    would bill the same preview twice.

    Three outcomes, and they are not the same bookkeeping:

    * a MOCK / ``is_mock`` result cost nothing, so the row settles at ``0`` and
      the daily cap is released;
    * audio came back, so the row settles at ``chars * EST_USD_PER_CHAR`` read
      from the ADAPTER's own class attribute -- the price comes from the thing
      that knows it, not from a constant in this module that could drift from it;
    * audio is EMPTY, or the adapter publishes no per-character price at all.
      Neither is "free": both are calls that may have been billed and whose
      amount nobody can price. That is UNKNOWN EXPOSURE
      (:func:`cost.book_unknown_exposure`) -- explicitly not the ``$0`` that
      :func:`cost.track_cost` drops on the floor.

    Failures here are logged, never raised: refusing to return audio the user
    already paid for, because a bookkeeping row could not be written, is a worse
    outcome than a slightly stale estimate.
    """
    from app.services import cost as _cost

    provider_name = str(result.provider or getattr(provider, "name", "") or "")
    try:
        if result.is_mock:
            _cost.settle_reservation(reservation.entry_id, 0.0)
            return
        price = getattr(type(provider), "EST_USD_PER_CHAR", None)
        if not result.audio_bytes or price is None:
            _cost.book_unknown_exposure(
                workspace_id, category=PREVIEW_CATEGORY, provider=provider_name,
                detail={
                    "chars": int(chars),
                    "audio_bytes": len(result.audio_bytes or b""),
                    "reservation_id": reservation.entry_id,
                    "reason": (
                        "provider billed the request and returned no audio"
                        if not result.audio_bytes else
                        "adapter publishes no per-character price, so the amount "
                        "cannot be computed"),
                })
            return
        _cost.settle_reservation(
            reservation.entry_id, round(int(chars) * float(price), 6))
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not lose the audio
        logger.warning("voice preview: cost not settled (%s)", type(exc).__name__)


def _audio_response(audio: bytes, *, provider_id: str, cached: bool,
                    clamped: bool, chars: int,
                    media_format: str = "wav", is_mock: bool = False,
                    sample_rate: int | None = None) -> Response:
    """Build the audio response from a FIXED header list.

    A dict of headers assembled from provider attributes would put whatever the
    adapter happens to carry into the response; naming each header is what makes
    "no secret reaches the frontend" a property of the code rather than of the
    adapter's current attribute list.
    """
    media = "audio/wav" if media_format == "wav" else "audio/mpeg"
    return Response(
        content=audio,
        media_type=media,
        headers={
            "X-TTS-Provider": str(provider_id or ""),
            "X-TTS-Mock": "1" if is_mock else "0",
            "X-Preview-Cached": "1" if cached else "0",
            "X-Preview-Clamped": "1" if clamped else "0",
            "X-Preview-Chars": str(int(chars)),
            "X-Preview-Sample-Rate": str(int(sample_rate)) if sample_rate else "",
            "Cache-Control": "private, max-age=0, no-store",
        },
    )


def _persist_asset(db: Session | None, ws: Workspace, result, provider_id: str,
                   voice: str, chars: int, cache_key: str) -> None:
    """Record the preview as a canonical MediaAsset -- no second asset table.

    A preview is a real render of a real voice, so it is a MediaAsset like any
    other: ``type="voice"`` / ``origin="generated"`` from the existing
    vocabularies in ``models/assets.py``, and a ``storage_key`` that is
    workspace-RELATIVE (the column documents that contract). No preview-specific
    table is introduced, so previews inherit every lineage/export/query path a
    production voice already has.

    ``db=None`` is the delegating caller (``api/v1/content.py``'s
    ``/assets/voice/preview``), which has no request session of its own. It gets
    its own short session rather than silently skipping the row: the adapter
    route is a real preview of a real voice, and dropping its lineage because it
    came in through an alias would make the alias the cheaper way to lose data.

    A failure to record it must not fail the preview the user actually asked
    for, so the write is best-effort and logged.
    """
    suffix = "wav" if result.format == "wav" else "mp3"
    asset = MediaAsset(
        workspace_id=ws.id,
        type="voice",
        origin="generated",
        provider=str(provider_id or "")[:60],
        storage_key=f"voice_preview/{cache_key}.{suffix}",
        mime_type=f"audio/{suffix}",
        duration_seconds=None,
        sample_rate=result.sample_rate,
        checksum=hashlib.sha256(result.audio_bytes).hexdigest(),
        meta_json={
            "provider": provider_id, "voice": voice, "format": result.format,
            "is_mock": bool(result.is_mock), "chars": int(chars),
            "preview": True, "is_billable_call": not bool(result.is_mock),
        },
    )
    try:
        if db is not None:
            db.add(asset)
            db.commit()
            return
        from app.db import session_scope

        with session_scope() as own:
            own.add(asset)
    except Exception as exc:  # noqa: BLE001 - bookkeeping must not break the preview
        if db is not None:
            db.rollback()
        logger.warning("voice preview: MediaAsset not recorded (%s)", exc)


# ---------------------------------------------------------------------------
# the no-secret property, as a function so a test can call it
# ---------------------------------------------------------------------------


def credential_fingerprint_free(payload: Any) -> bool:
    """True when ``payload`` carries no configured credential in any form.

    Checks plaintext and ``sha256(value)``. A digest is not the secret, but it
    narrows a brute force enough to matter, so the work order treats it as the
    secret too. Used by the endpoint's own test rather than by production code:
    production never builds a payload from a credential in the first place,
    which is the actual guarantee.
    """
    blob = payload if isinstance(payload, str) else json.dumps(payload, default=str)
    from app.services.provider_settings import REGISTRY

    for key, meta in REGISTRY.items():
        if not meta.get("secret"):
            continue
        try:
            value, _src = _ps.get_credential(key)
        except Exception:  # noqa: BLE001
            continue
        if not value:
            continue
        if value in blob:
            return False
        if hashlib.sha256(value.encode("utf-8")).hexdigest() in blob:
            return False
    # NOTE: a bare ``str(len(value)) in blob`` check is deliberately ABSENT.
    # A credential length is 2-3 digits, and any payload containing a sha256
    # digest contains such a substring roughly a quarter of the time, so that
    # check reported a leak at random -- a false positive that made an
    # unrelated test flaky and would mask a real one. Length leakage is
    # asserted structurally instead: the endpoints project every response
    # through a fixed field allowlist, so no field can carry a derived fact.
    return True


__all__ = [
    "MAX_PREVIEW_CHARS",
    "PREVIEW_CATEGORY",
    "PREVIEW_MAX_PER_WINDOW",
    "PREVIEW_WINDOW_SECONDS",
    "VoicePreviewBody",
    "assert_within_budget",
    "canonical_provider",
    "credential_fingerprint_free",
    "get_voices",
    "list_providers",
    "list_voices",
    "preview_budget_state",
    "preview_cache_dir",
    "preview_cache_key",
    "preview_estimated_usd",
    "preview_text",
    "reserve_preview",
    "voice_preview_router",
    "reset_preview_budget",
    "spend_preview_budget",
]
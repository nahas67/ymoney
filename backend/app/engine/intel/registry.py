"""Ordered provider chains per capability (Work 12 Lane A) -- contracts §1.1.

One registry, nine capability chains::

    alignment  diarization  speech_activity  enhancement  denoise
    face_tracking  segmentation  active_speaker  reframe
    semantic_rerank

Each chain is an ORDERED tuple of provider keys, most-preferred first. A key
maps to ``app.engine.intel.impl.<key>``, which must expose either a module
level ``PROVIDER`` (a :class:`~app.engine.intel.base.MediaIntelProvider`
subclass) or exactly one such class. Nothing is imported until a key is
actually resolved, so a venv without ML packages boots fine and simply reports
every capability as UNAVAILABLE with a reason.

Commercial mode (contracts §1.4): ``resolve(kind, commercial_mode=True)`` skips
any provider whose ``license_info().commercial_use != "PERMITTED"``, with the
reason "license not cleared for commercial use". Refusal lives HERE, not in a
per-call branch, so no lane can forget it.

Thread safety: the chain table is immutable after import; the only mutable
state is the lazy provider cache, guarded by a module lock.
"""

from __future__ import annotations

import importlib
import logging
import threading

from app.engine.intel.base import (
    COMMERCIAL_PERMITTED,
    MediaIntelProvider,
    ProviderHealth,
    safe_health,
)
from app.engine.intel.factory import UnavailableAdapter, build_provider

logger = logging.getLogger("ymoney.intel")

#: capability kind -> ordered provider keys (most preferred first).
#: Contracts §1.1 chain order; a key may appear in several chains (a provider
#: declares its extra chains with ``kinds``).
PROVIDER_CHAINS: dict[str, tuple[str, ...]] = {
    "alignment": ("whisperx_alignment",),
    "diarization": ("pyannote_diarization",),
    # Lane B owns the real silencedetect/astats VAD adapter; until it lands the
    # chain is honestly empty and the capability reports UNAVAILABLE.
    "speech_activity": ("ffmpeg_speech_activity",),
    "enhancement": ("ffmpeg_enhancement",),
    # ffmpeg's afftdn is a real denoiser, so it backs the chain when no neural
    # adapter (RNNoise) is installed.
    "denoise": ("rnnoise_denoise", "ffmpeg_enhancement"),
    "face_tracking": ("mediapipe_faces",),
    "segmentation": ("sam2_segmentation",),
    # Active speaker has no dedicated model provider: its evidence is diarization
    # + face windows + optional motion energy, so the motion provider is the only
    # chain entry. It serves this chain ONLY if it declares
    # ``kinds = ("active_speaker", ...)`` -- otherwise resolve() skips it with
    # "does not serve 'active_speaker'" and the capability stays honestly dark.
    "active_speaker": ("motion_reframe",),
    "reframe": ("motion_reframe",),
    # Work 15.6 §6: OPTIONAL semantic re-ranking, in its OWN chain. It is
    # deliberately not an entry of any media-analysis chain above -- it scores
    # text against text and never touches pixels or audio, so it cannot be
    # resolved for face_tracking / diarization / reframe. With no API key the
    # chain resolves to nothing and callers keep their deterministic order.
    "semantic_rerank": ("semantic_rerank",),
}

#: every capability kind the API/UI may ask for
CAPABILITY_KINDS: tuple[str, ...] = tuple(sorted(PROVIDER_CHAINS))

_IMPL_PACKAGE = "app.engine.intel.impl"

_lock = threading.Lock()
_instances: dict[str, object] = {}


def chain_for(kind: str) -> tuple[str, ...]:
    """Ordered provider keys for ``kind`` (empty tuple for an unknown kind)."""
    return PROVIDER_CHAINS.get(str(kind or "").strip().lower(), ())


def known_keys() -> tuple[str, ...]:
    """Every provider key any chain references, deduplicated and ordered."""
    keys: list[str] = []
    for chain in PROVIDER_CHAINS.values():
        for key in chain:
            if key not in keys:
                keys.append(key)
    return tuple(keys)


def load_provider_class(key: str) -> type[MediaIntelProvider]:
    """Import ``impl.<key>`` and return its provider CLASS.

    Accepts a module level ``PROVIDER`` symbol, else the single
    ``MediaIntelProvider`` subclass defined in that module. Raises
    ``ImportError``/``AttributeError``/``TypeError`` -- callers use
    :func:`build_provider`, which never raises.
    """
    module = importlib.import_module(f"{_IMPL_PACKAGE}.{key}")
    explicit = getattr(module, "PROVIDER", None)
    if isinstance(explicit, type) and issubclass(explicit, MediaIntelProvider):
        return explicit
    candidates = [
        value
        for value in vars(module).values()
        if isinstance(value, type)
        and issubclass(value, MediaIntelProvider)
        and value.__module__ == module.__name__
    ]
    if len(candidates) != 1:
        raise TypeError(
            f"impl.{key} must expose PROVIDER or exactly one MediaIntelProvider"
        )
    return candidates[0]


def get_provider(key: str) -> MediaIntelProvider | UnavailableAdapter:
    """Cached provider instance for ``key`` (never raises).

    Returns an :class:`UnavailableAdapter` carrying the import/construction
    failure as its health reason when the backend is absent -- the honest
    unavailable path, not an exception.
    """
    name = str(key or "").strip()
    with _lock:
        cached = _instances.get(name)
    if cached is not None:
        return cached  # type: ignore[return-value]
    provider = build_provider("", name)
    with _lock:
        _instances.setdefault(name, provider)
        return _instances[name]  # type: ignore[return-value]


def license_block_reason(provider: MediaIntelProvider | UnavailableAdapter) -> str:
    """Why a provider is refused in commercial mode ('' when it is cleared).

    The refusal is enforced in :func:`resolve`; the API reuses this helper to
    explain a ``commercial_blocked`` flag in the provider listing.
    """
    try:
        license_info = provider.license_info()
    except Exception as exc:  # noqa: BLE001 - listing must survive
        return f"license not cleared for commercial use (probe failed: {type(exc).__name__})"
    verdict = str(getattr(license_info, "commercial_use", "") or "")
    if verdict == COMMERCIAL_PERMITTED:
        return ""
    shown = verdict or "UNVERIFIED"
    return f"license not cleared for commercial use ({shown})"


def resolve(
    kind: str,
    *,
    commercial_mode: bool = False,
) -> tuple[MediaIntelProvider | UnavailableAdapter | None, dict[str, str]]:
    """First healthy provider for ``kind`` + the reason each one was skipped.

    Returns ``(provider, reasons)``:

    * ``provider`` is the first chain entry whose ``health().available`` is True
      (or ``None`` when nothing is usable);
    * ``reasons`` maps EVERY skipped provider key to its machine reason, so the
      API/UI can show exactly why a capability is dark.

    A provider whose ``kind`` does not match (or whose extra ``kinds`` do not
    include it) is skipped -- the chain is a preference, the provider's own
    declaration is the contract. Never raises: an adapter that explodes in
    ``health()`` is reported through :func:`~app.engine.intel.base.safe_health`.
    """
    reasons: dict[str, str] = {}
    name = str(kind or "").strip().lower()
    for key in chain_for(name):
        provider = get_provider(key)
        if not isinstance(provider, UnavailableAdapter) and not provider.supports_kind(name):
            reasons[key] = f"provider does not serve '{name}'"
            continue
        health: ProviderHealth = safe_health(provider)
        if not health.available:
            reasons[key] = health.reason or "unavailable"
            continue
        if commercial_mode:
            refusal = license_block_reason(provider)
            if refusal:
                reasons[key] = refusal
                continue
        return provider, reasons
    return None, reasons


def resolve_or_unavailable(
    kind: str,
    *,
    commercial_mode: bool = False,
) -> tuple[MediaIntelProvider | UnavailableAdapter, dict[str, str]]:
    """Like :func:`resolve` but never returns ``None``.

    The fallback is an :class:`UnavailableAdapter` whose reason aggregates the
    per-provider reasons, so a caller always has something to hand to a run.
    """
    provider, reasons = resolve(kind, commercial_mode=commercial_mode)
    if provider is not None:
        return provider, reasons
    detail = "; ".join(f"{key}: {why}" for key, why in reasons.items()) or "no provider configured"
    return UnavailableAdapter(reason=f"no provider for '{kind}' ({detail})"), reasons


def list_providers(*, commercial_mode: bool = False) -> list[dict]:
    """Every provider's health + capabilities + license + resources (contracts §14).

    Ordered by capability kind then chain position, so the UI can render the
    fallback order. Never raises -- ``MediaIntelProvider.to_dict`` already
    degrades defensively and a missing backend is an unavailable adapter.
    """
    selected_ids: set[int] = set()
    for kind in CAPABILITY_KINDS:
        provider, _ = resolve(kind, commercial_mode=commercial_mode)
        if provider is not None:
            selected_ids.add(id(provider))
    seen: set[str] = set()
    items: list[dict] = []
    for kind in CAPABILITY_KINDS:
        for key in chain_for(kind):
            if key in seen:
                continue
            seen.add(key)
            provider = get_provider(key)
            payload = provider.to_dict()
            payload["kind"] = kind
            payload["chain"] = list(chain_for(kind))
            payload["selected"] = id(provider) in selected_ids
            if commercial_mode:
                payload["commercial_blocked"] = bool(license_block_reason(provider))
            items.append(payload)
    return items


def clear_cache() -> None:
    """Drop cached provider instances (tests + a settings reload)."""
    with _lock:
        _instances.clear()


__all__ = [
    "CAPABILITY_KINDS",
    "PROVIDER_CHAINS",
    "chain_for",
    "clear_cache",
    "get_provider",
    "known_keys",
    "license_block_reason",
    "list_providers",
    "load_provider_class",
    "resolve",
    "resolve_or_unavailable",
]

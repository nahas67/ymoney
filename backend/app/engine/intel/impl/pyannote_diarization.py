"""pyannote.audio speaker-diarization adapter (Work 12 Lane B) -- §4 / §1.4.

Provides the ``diarization`` chain. Rules this adapter never bends:

* **Gated without a token => honestly unavailable.** Every pyannote weight is
  gated (click-through + contact info). Without a token the adapter reports
  ``available=False`` with a reason and raises
  :class:`~app.engine.intel.base.ProviderUnavailable` -- it never falls back to
  a heuristic that would INVENT speakers. Fabricated speakers are worse than no
  speakers (contracts §0).
* **Raw labels are never stored.** pyannote emits its own labels; the adapter
  passes them through as opaque ``speaker`` keys and the ENGINE
  (``app.engine.intel.alignment``) maps them to anonymous ``SPEAKER_00`` ids by
  first appearance. No gender, race, age, identity or any other attribute is
  derived, stored or exposed anywhere in this module.
* **Heavy imports inside ``health()``/``run()`` only** (contracts §1.3), so the
  app boots with zero ML packages installed.

License discipline (audit 2026-09-29, ``docs/oss/MEDIA_INTEL_LICENSES.md``):
pyannote.audio CODE is MIT; the 3.0/3.1 pipelines and ``segmentation-3.0`` are
MIT but GATED; the 3.x ``wespeaker`` embedding and ``community-1`` are CC-BY-4.0
(mandatory attribution); the gated ``config.yaml`` component list could not be
read without accepting the gate, so it stays UNVERIFIED and the provider verdict
is ``REVIEW_REQUIRED`` -- never asserted as permissive.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from collections.abc import Callable, Mapping
from typing import Any

from app.engine.intel.base import (
    COMMERCIAL_REVIEW_REQUIRED,
    MODE_LOCAL,
    IntelRequest,
    LicenseInfo,
    MediaIntelProvider,
    ProviderHealth,
    ProviderResult,
    ProviderUnavailable,
    ResourceSpec,
    check_control,
)

logger = logging.getLogger("ymoney.intel")

#: audited on 2026-09-29 (docs/oss/MEDIA_INTEL_LICENSES.md)
AUDITED_ON = "2026-09-29"
CODE_LICENSE = "MIT"
CODE_LICENSE_URL = "https://raw.githubusercontent.com/pyannote/pyannote-audio/develop/LICENSE"

#: the pipeline pyannote/whisperX default to -- CC-BY-4.0 + gated
DEFAULT_MODEL = "pyannote/speaker-diarization-community-1"

#: env vars searched for the Hugging Face token, in order
TOKEN_ENV_VARS: tuple[str, ...] = (
    "PYANNOTE_TOKEN",
    "HF_TOKEN",
    "HUGGINGFACE_TOKEN",
    "HUGGINGFACEHUB_API_TOKEN",
)

#: the audit rows this adapter encodes (model id -> its audited terms)
MODEL_LICENSES: dict[str, dict[str, str]] = {
    "pyannote/speaker-diarization-community-1": {
        "model_license": "CC-BY-4.0",
        "commercial_use": COMMERCIAL_REVIEW_REQUIRED,
        "gated": "yes",
        "note": "CC-BY-4.0 (attribution required); gated; sub-model terms unverified",
    },
    "pyannote/speaker-diarization-3.1": {
        "model_license": "MIT",
        "commercial_use": COMMERCIAL_REVIEW_REQUIRED,
        "gated": "yes",
        "note": "MIT weights but gated (terms + contact info); config.yaml unverified",
    },
    "pyannote/speaker-diarization-3.0": {
        "model_license": "MIT",
        "commercial_use": COMMERCIAL_REVIEW_REQUIRED,
        "gated": "yes",
        "note": "MIT weights but gated (terms + contact info); config.yaml unverified",
    },
    "pyannote/segmentation-3.0": {
        "model_license": "MIT",
        "commercial_use": COMMERCIAL_REVIEW_REQUIRED,
        "gated": "yes",
        "note": "MIT weights but gated; whisperX's bundled VAD copy of it is UNVERIFIED",
    },
    "pyannote/wespeaker-voxceleb-resnet34-LM": {
        "model_license": "CC-BY-4.0",
        "commercial_use": COMMERCIAL_REVIEW_REQUIRED,
        "gated": "no",
        "note": "CC-BY-4.0 embedding used by the 3.x pipelines (attribution required)",
    },
    "pyannote/embedding": {
        "model_license": "MIT",
        "commercial_use": COMMERCIAL_REVIEW_REQUIRED,
        "gated": "yes",
        "note": "legacy embedding, gated",
    },
}

#: components that carry a mandatory attribution obligation
ATTRIBUTION_REQUIRED: tuple[str, ...] = (
    "pyannote/speaker-diarization-community-1",
    "pyannote/wespeaker-voxceleb-resnet34-LM",
)

#: the gated file nobody could read during the audit -> stays unverified
UNVERIFIED_COMPONENT = "config.yaml component list of the gated 3.0/3.1 repos"

NO_BACKEND_REASON = (
    "pyannote diarization needs the optional 'pyannote.audio' stack (torch + "
    "pyannote.audio) on the worker host; it is not installed"
)
NO_TOKEN_REASON = (
    "pyannote weights are gated: accept the model terms on the hub and provide a "
    "Hugging Face token (PYANNOTE_TOKEN / HF_TOKEN) before diarization can run"
)
REMEDIATION = (
    "install pyannote.audio on the worker host, accept the model terms, export a "
    "HF token, then set PYANNOTE_TOKEN"
)

SEGMENTS_KIND = "SPEAKER"
NO_SPEAKERS_REASON = "the diarizer returned no speaker segment for this media"


def _have_module(name: str) -> bool:
    """``importlib.util.find_spec`` probe -- never imports, never raises.

    Same guard style as ``providers/clips.py::_have_module``.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def resolve_token(env: Mapping[str, str] | None = None) -> str:
    """First non-empty Hugging Face token from the known env vars (``""`` if none)."""
    source = env if env is not None else os.environ
    for name in TOKEN_ENV_VARS:
        value = str(source.get(name, "") or "").strip()
        if value:
            return value
    return ""


def _missing_backend() -> str | None:
    """Name of the first missing module, or ``None`` when the stack is present."""
    for module in ("pyannote", "pyannote.audio", "torch"):
        if not _have_module(module):
            return module
    return None


class _PyannoteBackend:
    """Real ``pyannote.audio`` pipeline wrapper.

    Built only after the module probe AND the token probe passed, so the heavy
    import lives here rather than at module scope.
    """

    def __init__(self, model: str, token: str, params: Mapping[str, Any]) -> None:
        from pyannote.audio import Pipeline  # noqa: PLC0415 - optional backend

        self._pipeline = Pipeline.from_pretrained(
            model,
            use_auth_token=token or None,
            device=str(params.get("device") or "cpu"),
        )

    def version(self) -> str:
        import pyannote.audio  # noqa: PLC0415 - optional backend

        return str(getattr(pyannote.audio, "__version__", "") or "unknown")

    def diarize(self, path: str) -> list[dict[str, Any]]:
        """Speaker turns as ``{speaker, start_s, end_s}`` with the RAW labels.

        The raw label is an opaque provider key. The engine turns it into an
        anonymous ``SPEAKER_nn`` id; nothing here interprets it.
        """
        annotation = self._pipeline(str(path))
        turns: list[dict[str, Any]] = []
        for segment, _track_id, label in annotation.itertracks(yield_label=True):
            start = float(getattr(segment.start, "seconds", segment.start))
            end = float(getattr(segment.end, "seconds", segment.end))
            if end <= start:
                continue
            turns.append({
                "speaker": str(label),
                "start_s": round(start, 6),
                "end_s": round(end, 6),
            })
        turns.sort(key=lambda row: (row["start_s"], row["end_s"]))
        return turns


def _load_pyannote(model: str, token: str, params: Mapping[str, Any]) -> Any:
    """Import-time factory. ``None`` (never an exception) when unusable."""
    if _missing_backend() is not None or not token:
        return None
    try:
        return _PyannoteBackend(model, token, params)
    except Exception as exc:  # noqa: BLE001 - a broken install is unavailable
        logger.info("pyannote backend unavailable (%s)", type(exc).__name__)
        return None


def _default_unavailable_reason(model: str, params: Mapping[str, Any]) -> str:
    """The honest blocker for one request, or ``""`` when the stack can serve it.

    Gating is an OPERATIONAL dependency (contracts §1.4 rule 5): without the
    optional packages or without a Hugging Face token the adapter must degrade
    honestly rather than invent speakers. Injectable through the constructor so
    a test double can serve a request without a gated install.
    """
    missing = _missing_backend()
    if missing:
        return f"{NO_BACKEND_REASON} (missing module: {missing})"
    if not resolve_token():
        return NO_TOKEN_REASON
    if not str(params.get("model") or model or "").strip():
        return "no pyannote pipeline model was configured"
    return ""


def _coerce_float(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _field(item: Any, *names: str) -> Any:
    for name in names:
        if isinstance(item, Mapping) and name in item:
            return item[name]
        if hasattr(item, name):
            return getattr(item, name)
    return None


def normalise_turns(raw: Any) -> tuple[list[dict[str, Any]], int]:
    """Provider turns -> ``([{speaker, start_s, end_s}], dropped_count)``.

    A turn survives only with a non-empty label and a positive duration; the
    per-turn ``confidence`` is kept ONLY when the provider reported one (pyannote
    does not, so it stays ``None`` -- never invented).
    """
    out: list[dict[str, Any]] = []
    dropped = 0
    for item in raw or []:
        label = str(_field(item, "speaker", "speaker_id", "label") or "").strip()
        start = _coerce_float(_field(item, "start_s", "start"))
        end = _coerce_float(_field(item, "end_s", "end"))
        if not label or start is None or end is None or end <= start:
            dropped += 1
            continue
        confidence = _coerce_float(_field(item, "confidence"))
        out.append({
            "speaker": label,
            "start_s": round(start, 6),
            "end_s": round(end, 6),
            "confidence": None if confidence is None else round(confidence, 6),
        })
    out.sort(key=lambda row: (row["start_s"], row["end_s"]))
    return out, dropped


def model_license_for(model: str) -> dict[str, str]:
    """Audited terms for one pipeline id (UNVERIFIED for an unknown id)."""
    name = str(model or "").strip()
    entry = MODEL_LICENSES.get(name)
    if entry is None:
        return {
            "model": name,
            "model_license": "UNVERIFIED",
            "commercial_use": COMMERCIAL_REVIEW_REQUIRED,
            "gated": "unknown",
            "note": "pipeline not in the audit table; audit it before commercial use",
        }
    return {"model": name, **entry}


class PyannoteDiarizationProvider(MediaIntelProvider):
    """Speaker diarization through pyannote.audio (optional, gated backend)."""

    key = "pyannote_diarization"
    kind = "diarization"
    kinds = ()

    def __init__(
        self,
        backend_loader: Callable[[str, str, Mapping[str, Any]], Any] | None = None,
        *,
        model: str = DEFAULT_MODEL,
        unavailable_reason: Callable[[str, Mapping[str, Any]], str] | None = None,
    ) -> None:
        self._loader = backend_loader or _load_pyannote
        self._model = str(model or DEFAULT_MODEL)
        self._reason_probe = unavailable_reason or _default_unavailable_reason

    # -- probes ------------------------------------------------------------

    def health(self) -> ProviderHealth:
        try:
            missing = _missing_backend()
            token = resolve_token()
            backend = None if (missing or not token) else self._loader(
                self._model, token, {}
            )
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            return ProviderHealth(
                available=False,
                reason=f"pyannote probe failed: {type(exc).__name__}",
                mode=MODE_LOCAL,
                detail={"remediation": REMEDIATION, "error_type": type(exc).__name__},
            )
        detail = {
            "remediation": REMEDIATION,
            "model": self._model,
            "gated": True,
            "token_present": bool(token),
            "pyannote_audio": _have_module("pyannote.audio"),
            "torch": _have_module("torch"),
        }
        if missing:
            return ProviderHealth(
                available=False,
                reason=NO_BACKEND_REASON,
                mode=MODE_LOCAL,
                detail={**detail, "missing_module": missing},
            )
        if not token:
            return ProviderHealth(
                available=False,
                reason=NO_TOKEN_REASON,
                mode=MODE_LOCAL,
                detail=detail,
            )
        if backend is None:
            return ProviderHealth(
                available=False,
                reason=f"pyannote pipeline '{self._model}' could not be loaded",
                mode=MODE_LOCAL,
                detail=detail,
            )
        try:
            version = str(backend.version() or "unknown")
        except Exception:  # noqa: BLE001 - version is cosmetic
            version = "unknown"
        return ProviderHealth(
            available=True,
            version=version,
            mode=MODE_LOCAL,
            detail=detail,
        )

    def capabilities(self) -> dict:
        return {
            "available": self.health().available,
            "segment_kind": SEGMENTS_KIND,
            "speaker_ids": "anonymous (assigned by the engine, never raw labels)",
            "models": sorted(MODEL_LICENSES),
            "formats": ["wav", "flac", "mp3", "m4a", "mp4", "mkv", "webm", "ogg"],
            "turn_confidence": "provider-dependent (pyannote reports none)",
            "fabricates_speakers": False,
        }

    def resource_requirements(self) -> ResourceSpec:
        return ResourceSpec(
            gpu=False,
            vram_mb=6144,
            ram_mb=6144,
            model_bytes=1_200_000_000,
            cpu_seconds_per_audio_minute=45.0,
            notes="CUDA strongly recommended; CPU works but is slow",
        )

    def license_info(self) -> LicenseInfo:
        terms = model_license_for(self._model)
        return LicenseInfo(
            code_license=CODE_LICENSE,
            code_license_url=CODE_LICENSE_URL,
            model_license=str(terms["model_license"]),
            model_license_url=(
                f"https://huggingface.co/{self._model}"
                if "/" in self._model
                else CODE_LICENSE_URL
            ),
            model_gated=True,
            commercial_use=COMMERCIAL_REVIEW_REQUIRED,
            audited_on=AUDITED_ON,
            notes=(
                "code MIT; 3.0/3.1 + segmentation-3.0 MIT but GATED (terms + contact "
                "info); community-1 and the 3.x wespeaker embedding are CC-BY-4.0 "
                f"(attribution required: {', '.join(ATTRIBUTION_REQUIRED)}); "
                f"{UNVERIFIED_COMPONENT} stays UNVERIFIED"
            ),
        )

    def model_version(self) -> str:
        """Stable model string that participates in the run cache key."""
        return f"pyannote:{self._model}"

    def cost(self, spec: ResourceSpec) -> dict:
        return {
            "gpu_ms": 0,
            "cpu_ms": int(float(spec.cpu_seconds_per_audio_minute) * 1000),
            "cost_micros": 0,
            "billed": False,
            "note": "local compute against gated weights; no vendor bill",
        }

    # -- work --------------------------------------------------------------

    def run(
        self,
        request: IntelRequest,
        *,
        progress: Callable[[float], None],
        should_cancel: Callable[[], bool],
        deadline: float | None,
    ) -> ProviderResult:
        params = dict(request.params or {})
        model = str(params.get("model") or self._model)
        terms = model_license_for(model)
        if bool(params.get("commercial_mode")) and (
            terms["commercial_use"] != "PERMITTED"
        ):
            raise ProviderUnavailable(
                f"pyannote pipeline '{model}' is {terms['commercial_use']} "
                f"({terms['model_license']}); commercial mode refuses it"
            )
        check_control(should_cancel, deadline)
        blocked = str(self._reason_probe(model, params) or "")
        if blocked:
            raise ProviderUnavailable(blocked)
        backend = self._loader(model, resolve_token(), params)
        if backend is None:
            raise ProviderUnavailable(
                f"pyannote pipeline '{model}' could not be loaded (gated weights, "
                "no usable token)"
            )
        progress(0.1)
        raw = backend.diarize(str(request.storage_path or ""))
        check_control(should_cancel, deadline)
        turns, dropped = normalise_turns(raw)
        if not turns:
            raise ProviderUnavailable(NO_SPEAKERS_REASON)
        progress(0.9)
        warnings = [f"{dropped} malformed diarization turn(s) dropped"] if dropped else []
        # The engine is what turns the raw labels into anonymous SPEAKER_nn ids.
        return ProviderResult(
            ok=True,
            artifacts={
                "segments": {
                    "payload": {
                        "kind": SEGMENTS_KIND,
                        "segments": turns,
                        "speakers_raw_count": len({t["speaker"] for t in turns}),
                        "model": model,
                        "model_version": self.model_version(),
                        "license": terms,
                    }
                }
            },
            metrics={
                "segment_count": len(turns),
                "raw_speaker_count": len({t["speaker"] for t in turns}),
                "model": model,
                "model_version": self.model_version(),
            },
            warnings=warnings,
            error="",
        )


#: the registry resolves this symbol (impl/<key> contract)
PROVIDER = PyannoteDiarizationProvider

__all__ = [
    "ATTRIBUTION_REQUIRED",
    "AUDITED_ON",
    "CODE_LICENSE",
    "CODE_LICENSE_URL",
    "DEFAULT_MODEL",
    "MODEL_LICENSES",
    "NO_BACKEND_REASON",
    "NO_SPEAKERS_REASON",
    "NO_TOKEN_REASON",
    "PROVIDER",
    "REMEDIATION",
    "SEGMENTS_KIND",
    "TOKEN_ENV_VARS",
    "UNVERIFIED_COMPONENT",
    "PyannoteDiarizationProvider",
    "model_license_for",
    "normalise_turns",
    "resolve_token",
]

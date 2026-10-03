"""whisperX forced-alignment adapter (Work 12 Lane B) -- contracts §4 / §1.4.

Provides the ``alignment`` chain: ``audio -> ASR -> word timestamps``. Words are
written ONLY when the backend genuinely produced per-word timings; a
segment-level backend reports ``words_available=False`` with a machine reason and
the engine stores ZERO word rows. Interpolating words from segment boundaries
would be a fabrication and is refused here.

Honesty rules honoured (contracts §0/§1.3):

* ``whisperx`` / ``faster_whisper`` / ``torch`` are imported INSIDE ``health()``
  and ``run()`` -- never at module import, so ``from app.main import app`` works
  in a venv with zero ML packages installed;
* no backend => ``health().available is False`` WITH a reason, and ``run()``
  raises :class:`~app.engine.intel.base.ProviderUnavailable`. A caller that
  ignores the exception still receives zero words;
* the ASR/aligner conventions are the ones already in the product
  (``providers/clips.py::_have_module`` / ``whisper_available`` and
  ``providers/video_engine/ffmpeg_avatar.py::_whisper_segments`` with
  ``word_timestamps=True``) -- this adapter does not invent a second style.

License discipline (contracts §1.4, audit 2026-09-29 in
``docs/oss/MEDIA_INTEL_LICENSES.md``): whisperX CODE is BSD-2-Clause, but the
MODEL terms are PER LANGUAGE. ``en`` is MIT (PERMITTED); ``fr/de/es/it`` are the
VoxPopuli torchaudio aligners under **CC BY-NC 4.0** (PROHIBITED); every other
aligner is UNVERIFIED per language; the bundled VAD weights
(``assets/pytorch_model.bin``) are UNVERIFIED. The provider verdict is therefore
``REVIEW_REQUIRED`` and :func:`language_clearance` /
:func:`commercial_clearance` expose the per-language verdicts so commercial mode
can refuse the non-commercial languages specifically instead of the whole
provider.
"""

from __future__ import annotations

import importlib.util
import logging
from collections.abc import Callable, Mapping
from typing import Any

from app.engine.intel.base import (
    COMMERCIAL_PERMITTED,
    COMMERCIAL_PROHIBITED,
    COMMERCIAL_REVIEW_REQUIRED,
    COMMERCIAL_UNVERIFIED,
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
CODE_LICENSE = "BSD-2-Clause"
CODE_LICENSE_URL = "https://raw.githubusercontent.com/m-bain/whisperX/main/LICENSE"

#: the default VAD whisperX bundles with the package
DEFAULT_VAD = "pyannote/segmentation-3.0"
#: VAD weights ship inside the repo with no model card -> unverifiable
VAD_WEIGHTS_LICENSE = COMMERCIAL_UNVERIFIED
VAD_WEIGHTS_NOTE = (
    "whisperX bundles assets/pytorch_model.bin (VAD) with no published model "
    "card; a BSD-2 code repo does not license its weights -- UNVERIFIED"
)

#: per-language aligner terms, verbatim from the audit table.
#: ``commercial_use`` is the verdict for THAT language only.
ALIGNER_LICENSES: dict[str, dict[str, str]] = {
    "en": {
        "aligner": "torchaudio WAV2VEC2_ASR_BASE_960H",
        "model_license": "MIT",
        "commercial_use": COMMERCIAL_PERMITTED,
        "note": "torchaudio states the MIT license covers the pre-trained models too",
    },
    "fr": {
        "aligner": "torchaudio VOXPOPULI_ASR_BASE_10K_FR",
        "model_license": "CC-BY-NC-4.0",
        "commercial_use": COMMERCIAL_PROHIBITED,
        "note": "VoxPopuli aligners are CC BY-NC 4.0 -- NON-COMMERCIAL",
    },
    "de": {
        "aligner": "torchaudio VOXPOPULI_ASR_BASE_10K_DE",
        "model_license": "CC-BY-NC-4.0",
        "commercial_use": COMMERCIAL_PROHIBITED,
        "note": "VoxPopuli aligners are CC BY-NC 4.0 -- NON-COMMERCIAL",
    },
    "es": {
        "aligner": "torchaudio VOXPOPULI_ASR_BASE_10K_ES",
        "model_license": "CC-BY-NC-4.0",
        "commercial_use": COMMERCIAL_PROHIBITED,
        "note": "VoxPopuli aligners are CC BY-NC 4.0 -- NON-COMMERCIAL",
    },
    "it": {
        "aligner": "torchaudio VOXPOPULI_ASR_BASE_10K_IT",
        "model_license": "CC-BY-NC-4.0",
        "commercial_use": COMMERCIAL_PROHIBITED,
        "note": "VoxPopuli aligners are CC BY-NC 4.0 -- NON-COMMERCIAL",
    },
}

#: any other aligner (HF ``DEFAULT_ALIGN_MODELS_HF``) is audited per language
OTHER_ALIGNER = {
    "aligner": "huggingface DEFAULT_ALIGN_MODELS_HF entry",
    "model_license": COMMERCIAL_UNVERIFIED,
    "commercial_use": COMMERCIAL_UNVERIFIED,
    "note": "per-model terms; audit every language a deployment actually ships",
}

#: reason shown when nothing is installed
UNAVAILABLE_REASON = (
    "whisperX alignment needs the optional 'whisperx' (faster-whisper + ctranslate2 "
    "+ torch) stack on the worker host; it is not installed"
)
REMEDIATION = (
    "pip install whisperx on the worker host (the app venv itself never needs it) "
    "and keep the aligner language within the cleared set"
)

#: word timestamps missing from an otherwise successful run
WORDS_UNAVAILABLE_REASON = (
    "the ASR backend returned segment-level timing only; word timestamps were "
    "not produced, so no word rows are written (words are never interpolated)"
)


def _have_module(name: str) -> bool:
    """``importlib.util.find_spec`` probe -- never imports, never raises.

    Same guard style as ``providers/clips.py::_have_module``.
    """
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def normalise_language(language: str | None) -> str:
    """Lower-case, dash-free language tag (``pt-BR`` -> ``ptbr``); default ``en``."""
    text = str(language or "en").strip().lower().replace("-", "").replace("_", "")
    return text or "en"


def language_clearance(language: str | None) -> dict[str, Any]:
    """Per-language aligner clearance (contracts §1.4 finding 1).

    Returns the audited verdict for ONE language::

        {"language", "aligner", "model_license", "commercial_use", "note",
         "audited_on", "known_audited"}

    An unknown language is ``known_audited=False`` with
    ``commercial_use=UNVERIFIED`` -- never assumed permissive.
    """
    code = normalise_language(language)
    entry = ALIGNER_LICENSES.get(code)
    if entry is None:
        entry = OTHER_ALIGNER
        known = False
    else:
        known = True
    return {
        "language": code,
        "aligner": entry["aligner"],
        "model_license": entry["model_license"],
        "commercial_use": entry["commercial_use"],
        "note": entry["note"],
        "audited_on": AUDITED_ON,
        "known_audited": known,
    }


def commercial_clearance(
    languages: list[str] | tuple[str, ...] | None = None,
    *,
    commercial_mode: bool = False,
) -> dict[str, Any]:
    """Which languages may be aligned, and why the others are refused.

    ``PROHIBITED`` (a non-commercial aligner) and ``UNVERIFIED`` are refused in
    commercial mode; the refusal names the LANGUAGE so the whole provider is not
    thrown away. Outside commercial mode every language is runnable and the
    verdict is reported so the operator can see which ones are not cleared.
    """
    codes = [normalise_language(item) for item in (languages or list(ALIGNER_LICENSES))]
    per_language = [language_clearance(code) for code in codes]
    allowed: list[str] = []
    blocked: list[dict[str, str]] = []
    for row in per_language:
        verdict = str(row["commercial_use"])
        if commercial_mode and verdict != COMMERCIAL_PERMITTED:
            blocked.append({
                "language": str(row["language"]),
                "commercial_use": verdict,
                "reason": (
                    f"aligner for '{row['language']}' is {verdict}"
                    f" ({row['model_license']}); commercial mode refuses that "
                    "language specifically -- pick 'en' or audit another aligner"
                ),
            })
        else:
            allowed.append(str(row["language"]))
    return {
        "commercial_mode": bool(commercial_mode),
        "per_language": per_language,
        "allowed": sorted(set(allowed)),
        "blocked": blocked,
        "provider_commercial_use": COMMERCIAL_REVIEW_REQUIRED,
    }


class _WhisperXBackend:
    """Real whisperX pipeline wrapper: ASR -> forced alignment -> word timings.

    Constructed only after the module probe passed, so the heavy imports live
    here rather than at module scope. ``align()`` returns the normalised payload
    the provider hands to the engine.
    """

    def __init__(self, language: str, params: Mapping[str, Any]) -> None:
        import whisperx  # noqa: PLC0415 - optional backend, contracts §1.3

        self._whisperx = whisperx
        self._language = language
        self._asr_model = str(params.get("asr_model") or "tiny")
        self._align_model = str(params.get("align_model") or "")
        self._device = str(params.get("device") or "cpu")
        self._compute_type = str(params.get("compute_type") or "int8")

    def version(self) -> str:
        return str(getattr(self._whisperx, "__version__", "") or "unknown")

    def align(self, path: str) -> dict[str, Any]:
        """Transcribe + force-align; return the provider-neutral payload."""
        device = self._device
        model = self._whisperx.load_model(
            self._asr_model, device, compute_type=self._compute_type
        )
        audio = self._whisperx.load_audio(path)
        result = model.transcribe(audio, batch_size=8, language=self._language or None)
        segments = list(result.get("segments") or [])
        aligned: list[dict[str, Any]] = []
        align_model = None
        if self._align_model:
            align_model, metadata = self._whisperx.load_align_model(
                language_code=self._language, device=device
            )
            aligned = list(
                self._whisperx.align(
                    result["segments"], align_model, metadata, audio, self._device,
                    return_char_alignments=False,
                )
            )
        return {
            "language": self._language,
            "words": aligned if aligned else segments,
            "words_available": bool(aligned),
            "reason": "" if aligned else WORDS_UNAVAILABLE_REASON,
        }


def _load_whisperx(language: str, params: Mapping[str, Any]) -> Any:
    """Import-time factory. ``None`` (never an exception) when not installed."""
    for module in ("whisperx", "faster_whisper", "torch", "ctranslate2"):
        if not _have_module(module):
            return None
    try:
        return _WhisperXBackend(language, params)
    except Exception as exc:  # noqa: BLE001 - a broken install is unavailable
        logger.info("whisperX backend unavailable (%s)", type(exc).__name__)
        return None


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


def as_mapping(item: Any, *names: str) -> Any:
    """Public alias of the shared mapping/object reader (used by the engine)."""
    return _field(item, *names)


def normalise_words(raw: Any) -> tuple[list[dict[str, Any]], str]:
    """Provider payload -> ``([word rows], reason)``.

    A word survives only when the backend gave BOTH a start and an end. Rows are
    sorted by ``start_s`` and an entry whose end precedes its start is dropped
    rather than silently swapped. ``confidence`` is kept only when reported.
    """
    out: list[dict[str, Any]] = []
    dropped = 0
    for item in raw or []:
        token = str(_field(item, "word", "text") or "").strip()
        start = _coerce_float(_field(item, "start_s", "start"))
        end = _coerce_float(_field(item, "end_s", "end"))
        if not token or start is None or end is None:
            dropped += 1
            continue
        if end < start:
            dropped += 1
            continue
        confidence = _coerce_float(_field(item, "confidence", "probability"))
        out.append({
            "word": token[:200],
            "start_s": round(start, 6),
            "end_s": round(end, 6),
            "confidence": None if confidence is None else round(confidence, 6),
        })
    out.sort(key=lambda row: (row["start_s"], row["end_s"]))
    reason = f"{dropped} unaligned token(s) dropped" if dropped else ""
    return out, reason


class WhisperXAlignmentProvider(MediaIntelProvider):
    """Word-level alignment through whisperX (optional backend)."""

    key = "whisperx_alignment"
    kind = "alignment"
    kinds = ()

    def __init__(
        self,
        backend_loader: Callable[[str, Mapping[str, Any]], Any] | None = None,
    ) -> None:
        self._loader = backend_loader or _load_whisperx

    # -- probes ------------------------------------------------------------

    def health(self) -> ProviderHealth:
        try:
            backend = self._loader("en", {})
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            return ProviderHealth(
                available=False,
                reason=f"whisperX probe failed: {type(exc).__name__}",
                mode=MODE_LOCAL,
                detail={"remediation": REMEDIATION, "error_type": type(exc).__name__},
            )
        if backend is None:
            return ProviderHealth(
                available=False,
                reason=UNAVAILABLE_REASON,
                mode=MODE_LOCAL,
                detail={
                    "remediation": REMEDIATION,
                    "whisperx": _have_module("whisperx"),
                    "faster_whisper": _have_module("faster_whisper"),
                    "torch": _have_module("torch"),
                    "word_level": True,
                },
            )
        try:
            version = str(backend.version() or "unknown")
        except Exception:  # noqa: BLE001 - version is cosmetic
            version = "unknown"
        return ProviderHealth(
            available=True,
            version=version,
            mode=MODE_LOCAL,
            detail={"word_level": True, "languages": sorted(ALIGNER_LICENSES)},
        )

    def capabilities(self) -> dict:
        return {
            "available": self.health().available,
            "word_level": True,
            "formats": ["wav", "mp3", "m4a", "mp4", "mkv", "webm", "flac", "ogg"],
            "sample_rates": "any (decoded by ffmpeg/whisperX)",
            "asr_models": ["tiny", "base", "small", "medium", "large-v3"],
            "languages": sorted(ALIGNER_LICENSES),
            "language_clearance": commercial_clearance()["per_language"],
            "max_duration_s": None,  # chunked by the run service
        }

    def resource_requirements(self) -> ResourceSpec:
        return ResourceSpec(
            gpu=False,
            vram_mb=4096,
            ram_mb=4096,
            model_bytes=1_500_000_000,
            cpu_seconds_per_audio_minute=25.0,
            notes="CPU int8 works; a CUDA device cuts the wall clock but is optional",
        )

    def license_info(self) -> LicenseInfo:
        return LicenseInfo(
            code_license=CODE_LICENSE,
            code_license_url=CODE_LICENSE_URL,
            model_license="PER-LANGUAGE (see ALIGNER_LICENSES)",
            model_license_url=(
                "https://github.com/m-bain/whisperX/blob/main/whisperx/alignment.py"
            ),
            model_gated=False,
            commercial_use=COMMERCIAL_REVIEW_REQUIRED,
            audited_on=AUDITED_ON,
            notes=(
                "code BSD-2-Clause; en aligner MIT (PERMITTED); fr/de/es/it VoxPopuli "
                "aligners CC BY-NC 4.0 (PROHIBITED, refused per language); other "
                f"aligners UNVERIFIED per language; bundled VAD ({DEFAULT_VAD}) "
                f"weights {VAD_WEIGHTS_LICENSE} -- {VAD_WEIGHTS_NOTE}"
            ),
        )

    def model_version(self) -> str:
        """Stable model string that participates in the run cache key."""
        health = self.health()
        return f"whisperx+aligner:{health.version if health.available else 'absent'}"

    def cost(self, spec: ResourceSpec) -> dict:
        return {
            "gpu_ms": 0,
            "cpu_ms": int(float(spec.cpu_seconds_per_audio_minute) * 1000),
            "cost_micros": 0,
            "billed": False,
            "note": "local CPU/GPU work; no vendor bill",
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
        language = normalise_language(params.get("language"))
        commercial_mode = bool(params.get("commercial_mode"))
        clearance = language_clearance(language)
        if commercial_mode and clearance["commercial_use"] != COMMERCIAL_PERMITTED:
            raise ProviderUnavailable(
                f"whisperX aligner for '{language}' is "
                f"{clearance['commercial_use']} ({clearance['model_license']}); "
                "commercial mode refuses that language specifically"
            )
        check_control(should_cancel, deadline)
        backend = self._loader(language, params)
        if backend is None:
            raise ProviderUnavailable(UNAVAILABLE_REASON)
        progress(0.1)
        payload = backend.align(str(request.storage_path or ""))
        check_control(should_cancel, deadline)
        words, dropped_note = normalise_words(
            payload.get("words") if isinstance(payload, Mapping) else None
        )
        available = bool(payload.get("words_available")) and bool(words)
        reason = "" if available else str(payload.get("reason") or WORDS_UNAVAILABLE_REASON)
        if available and dropped_note:
            reason = dropped_note
        progress(0.9)
        return ProviderResult(
            ok=True,
            artifacts={
                "words": {
                    "payload": {
                        "words": words,
                        "words_available": available,
                        "reason": reason,
                        "language": str(payload.get("language") or language),
                        "model_version": self.model_version(),
                        "language_clearance": clearance,
                    }
                }
            },
            metrics={
                "word_count": len(words),
                "words_available": available,
                "language": language,
                "model_version": self.model_version(),
            },
            warnings=[reason] if (reason and not available) else [],
            error="",
        )


#: the registry resolves this symbol (impl/<key> contract)
PROVIDER = WhisperXAlignmentProvider

__all__ = [
    "ALIGNER_LICENSES",
    "AUDITED_ON",
    "CODE_LICENSE",
    "CODE_LICENSE_URL",
    "DEFAULT_VAD",
    "OTHER_ALIGNER",
    "PROVIDER",
    "UNAVAILABLE_REASON",
    "VAD_WEIGHTS_LICENSE",
    "VAD_WEIGHTS_NOTE",
    "WhisperXAlignmentProvider",
    "commercial_clearance",
    "language_clearance",
    "normalise_language",
    "normalise_words",
]

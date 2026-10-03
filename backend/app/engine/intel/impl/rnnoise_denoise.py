"""RNNoise denoise adapter (Work 12 Lane C) -- contracts §6 ``denoise`` row.

Adapter isolation, stated plainly: this module is a SELF-CONTAINED adapter
behind the provider contract. Nothing in the renderer, the audio engine, the
exporter or the timeline imports it, and the enhancement pipeline reaches it
ONLY through the registry's ``denoise`` chain::

    denoise chain:  rnnoise_denoise  ->  ffmpeg_enhancement

Swapping RNNoise out (or installing it) is a registry/health question, never a
call-site change. That is the whole point of the abstraction, and the fallback
is real work rather than a fake success: with no neural denoiser installed the
chain continues to ``ffmpeg_enhancement``, whose ``afftdn`` pass actually
rewrites the audio and records ``method="afftdn"``.

The honest state in this repository
-----------------------------------
Neither RNNoise binding is present, so ``health()`` reports
``available=False`` with the reason, and that IS the path CI exercises (finding
#8 in ``docs/oss/MEDIA_INTEL_LICENSES.md``: CI installs none of these backends
precisely so every adapter proves its honest-unavailable branch). Two probes
are run, both real:

1. an importable ``rnnoise`` python binding (the reference C API:
   ``create()`` / ``process_frame(int16[480]) -> int16[480]`` / ``destroy()``),
   applied frame-by-frame with the stdlib ``wave`` module -- no new dependency
   is added to the app venv either way;
2. an ffmpeg ``rnnoise`` filter, which exists in some ffmpeg builds.

When both probes fail, ``run()`` raises ``ProviderUnavailable`` -- it never
returns silence, never re-encodes the input, and never claims a stage it did
not perform.

License (contracts §1.4, verbatim from ``docs/oss/MEDIA_INTEL_LICENSES.md``):
code **BSD-3-Clause**; the weights are compiled into ``src/rnn_data.c`` and
covered by the SAME ``COPYING`` file, so there is no separate affirmative
weights grant -- noted rather than smoothed over. ``commercial_use`` is
therefore ``PERMITTED`` with that note attached; nothing else in the ffmpeg
family may borrow this verdict.
"""

from __future__ import annotations

import array
import contextlib
import logging
import wave
from pathlib import Path

from app.engine.intel.base import (
    COMMERCIAL_PERMITTED,
    MODE_LOCAL,
    CancelFn,
    IntelRequest,
    LicenseInfo,
    MediaIntelProvider,
    ProgressFn,
    ProviderHealth,
    ProviderResult,
    ProviderUnavailable,
    ResourceSpec,
    check_control,
)
from app.engine.intel.ffmpeg_util import ffmpeg_available, run_filter

logger = logging.getLogger("ymoney.intel")

PROVIDER_KEY = "rnnoise_denoise"

#: RNNoise's fixed frame size in samples (48 kHz x 10 ms) -- part of its C API.
FRAME_SAMPLES = 480

AUDITED_ON = "2026-09-29"

#: probe outcomes, cached per process so a health listing costs nothing
_PROBE_CACHE: dict[str, str] = {}


def _probe_binding() -> str:
    """Import the optional ``rnnoise`` python binding.

    Returns ``"python"`` when the reference API is importable, else ``""``.
    The import happens HERE, inside the probe -- never at module import, so
    ``from app.main import app`` works in a venv with zero ML packages.
    """
    if "binding" in _PROBE_CACHE:
        return _PROBE_CACHE["binding"]
    verdict = ""
    try:
        import rnnoise  # noqa: PLC0415 - optional backend, probed not imported
    except Exception as exc:  # noqa: BLE001 - any import failure is "absent"
        logger.debug("rnnoise python binding absent: %s", type(exc).__name__)
    else:
        if callable(getattr(rnnoise, "create", None)) and callable(
            getattr(rnnoise, "process_frame", None)
        ):
            verdict = "python"
        else:
            logger.debug("rnnoise module present but the reference API is not")
    _PROBE_CACHE["binding"] = verdict
    return verdict


def _probe_filter() -> bool:
    """True when THIS ffmpeg build exposes an ``rnnoise`` filter."""
    if "filter" in _PROBE_CACHE:
        return _PROBE_CACHE["filter"] == "yes"
    verdict = False
    try:
        from app.engine.intel.impl.ffmpeg_enhancement import filter_available

        verdict = bool(filter_available("rnnoise"))
    except Exception as exc:  # noqa: BLE001 - probe must never raise
        logger.debug("rnnoise filter probe failed: %s", type(exc).__name__)
    _PROBE_CACHE["filter"] = "yes" if verdict else "no"
    return verdict


def reset_probes() -> None:
    """Drop cached binding probes (tests)."""
    _PROBE_CACHE.clear()


def _unavailable_reason() -> str:
    """The exact text the run/report carries when no RNNoise path exists."""
    return (
        "RNNoise is not available: no importable 'rnnoise' python binding and "
        "this ffmpeg build has no 'rnnoise' filter. Denoise falls back through "
        "the registry chain to ffmpeg afftdn (method recorded as afftdn)."
    )


def denoise_pcm16(source: str | Path, destination: str | Path) -> dict:
    """Run RNNoise over a 16-bit PCM WAV with the stdlib only.

    Frame-by-frame: read 480 samples, ``process_frame``, write 480 samples.
    The last partial frame is passed through unprocessed rather than zero-padded,
    so no artificial tail of silence is ever appended.

    Raises :class:`~app.engine.intel.base.ProviderUnavailable` when the binding
    is missing -- the caller never gets a silently unprocessed copy.
    """
    src = Path(str(source or ""))
    dst = Path(str(destination or ""))
    if not src.exists():
        raise ProviderUnavailable("source media is not readable")
    try:
        import rnnoise  # noqa: PLC0415 - optional backend, probed not imported
    except Exception as exc:  # noqa: BLE001 - absent binding is the honest path
        raise ProviderUnavailable(_unavailable_reason()) from exc

    state = rnnoise.create()
    frames_processed = 0
    try:
        with wave.open(str(src), "rb") as handle:
            if handle.getcomptype() != "NONE" or handle.getsampwidth() != 2:
                raise ProviderUnavailable("RNNoise path requires 16-bit PCM input")
            channels = handle.getnchannels()
            rate = handle.getframerate()
            width = handle.getsampwidth()
            raw = handle.readframes(handle.getnframes())
        pcm = array.array("h")
        pcm.frombytes(raw)
        usable = len(pcm) - (len(pcm) % FRAME_SAMPLES)
        out = array.array("h", pcm)
        for offset in range(0, usable, FRAME_SAMPLES):
            frame = pcm[offset:offset + FRAME_SAMPLES]
            if len(frame) < FRAME_SAMPLES:
                break
            result = rnnoise.process_frame(state, frame)
            out[offset:offset + FRAME_SAMPLES] = array.array("h", result)
            frames_processed += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        with wave.open(str(dst), "wb") as handle:
            handle.setnchannels(channels)
            handle.setsampwidth(width)
            handle.setframerate(rate)
            handle.writeframes(out.tobytes())
    finally:
        with contextlib.suppress(Exception):
            rnnoise.destroy(state)
    return {
        "method": "rnnoise",
        "frames_processed": frames_processed,
        "frame_samples": FRAME_SAMPLES,
        "path": str(dst),
    }


class RnnoiseDenoiseProvider(MediaIntelProvider):
    """Neural (RNNoise) noise reduction, honestly unavailable when absent.

    Serves ONLY the ``denoise`` chain. It deliberately does NOT claim
    ``enhancement``: the enhancement pipeline is a different surface and stays
    with ``ffmpeg_enhancement``, so installing RNNoise cannot silently change
    what "enhance" means for an existing run's cache key.
    """

    key = PROVIDER_KEY
    kind = "denoise"
    kinds = ()

    def health(self) -> ProviderHealth:
        """Never raises. Absent binding => ``available=False`` + the reason."""
        try:
            binding = _probe_binding()
            has_filter = _probe_filter()
            if binding or has_filter:
                return ProviderHealth(
                    available=True,
                    version="rnnoise",
                    mode=MODE_LOCAL,
                    detail={"path": binding or "ffmpeg_filter"},
                )
            return ProviderHealth(
                available=False,
                reason=_unavailable_reason(),
                mode=MODE_LOCAL,
                detail={
                    "python_binding": False,
                    "ffmpeg_filter": False,
                    "ffmpeg_available": ffmpeg_available(),
                    "fallback": "ffmpeg_enhancement (afftdn)",
                    "remediation": (
                        "install an RNNoise binding (pip install rnnoise) on the worker "
                        "host, or run an ffmpeg build that exposes the rnnoise filter. "
                        "Until then denoise is served for real by ffmpeg afftdn, which "
                        "the registry chain resolves automatically."
                    ),
                },
            )
        except Exception as exc:  # noqa: BLE001 - a health probe must not raise
            return ProviderHealth(
                available=False,
                reason=f"RNNoise probe failed: {type(exc).__name__}",
                mode=MODE_LOCAL,
                detail={"error_type": type(exc).__name__},
            )

    def capabilities(self) -> dict:
        binding = _probe_binding()
        available = bool(binding or _probe_filter())
        return {
            "available": available,
            "reason": "" if available else _unavailable_reason(),
            "denoise_only": True,
            "paths": {
                "python_binding": bool(binding),
                "ffmpeg_filter": _probe_filter(),
            },
            "frame_samples": FRAME_SAMPLES,
            "input_requirements": "16-bit PCM WAV (decoded before the RNNoise pass)",
            "derived_only": True,
            "fallback_provider": "ffmpeg_enhancement",
        }

    def resource_requirements(self) -> ResourceSpec:
        return ResourceSpec(
            gpu=False,
            ram_mb=64,
            cpu_seconds_per_audio_minute=2.0,
            notes="single-threaded int16 frame loop; 480 samples per call",
        )

    def license_info(self) -> LicenseInfo:
        """Verbatim from the ``rnnoise`` row of ``docs/oss/MEDIA_INTEL_LICENSES.md``.

        ``PERMITTED`` is the one Work 12 component cleared this way, and the note
        is load-bearing: the weights ride inside ``src/rnn_data.c`` under the same
        ``COPYING``, with no standalone affirmative weights grant.
        """
        return LicenseInfo(
            code_license="BSD-3-Clause",
            code_license_url="https://github.com/xiph/rnnoise/blob/master/COPYING",
            model_license="BSD-3-Clause",
            model_license_url="https://github.com/xiph/rnnoise/blob/master/COPYING",
            model_gated=False,
            commercial_use=COMMERCIAL_PERMITTED,
            audited_on=AUDITED_ON,
            notes=(
                "Weights are compiled into src/rnn_data.c and covered by the same "
                "COPYING (no separate affirmative weights statement) -- permitted, "
                "with that note recorded rather than smoothed over. Not bundled with "
                "the app: the binding is optional and probed at runtime."
            ),
        )

    def cost(self, spec: ResourceSpec) -> dict:
        return {
            "gpu_ms": 0,
            "cpu_ms": int(max(0.0, float(spec.cpu_seconds_per_audio_minute)) * 1000),
            "cost_micros": 0,
            "billed": False,
            "currency": "cpu_only",
        }

    def run(
        self,
        request: IntelRequest,
        *,
        progress: ProgressFn,
        should_cancel: CancelFn,
        deadline: float | None,
    ) -> ProviderResult:
        """Denoise into ``params['output_path']`` or refuse, honestly."""
        params = dict(request.params or {})
        source = str(request.storage_path or "")
        output_path = str(params.get("output_path") or "").strip()
        if not output_path:
            raise ProviderUnavailable("no output_path requested for denoise")
        check_control(should_cancel, deadline)
        progress(0.05)

        if _probe_filter():
            result = run_filter(source, output_path, "rnnoise")
            if not result.get("ok"):
                raise ProviderUnavailable(
                    f"rnnoise filter failed: {str(result.get('stderr_tail') or '')[:200]}"
                )
            progress(1.0)
            return ProviderResult(
                ok=True,
                artifacts={"audio": {"path": output_path, "method": "rnnoise"},
                           "stages": [{"stage": "denoise", "status": "applied",
                                       "method": "rnnoise", "neural": True,
                                       "filters": ["rnnoise"],
                                       "reason": "ffmpeg rnnoise filter"}]},
                metrics={"processing_ms": int(result.get("processing_ms") or 0),
                         "derived": True},
                warnings=[],
            )

        if _probe_binding():
            payload = denoise_pcm16(source, output_path)
            progress(1.0)
            return ProviderResult(
                ok=True,
                artifacts={"audio": {"path": output_path, "method": "rnnoise"},
                           "stages": [{"stage": "denoise", "status": "applied",
                                       "method": "rnnoise", "neural": True, "filters": [],
                                       "reason": f"python binding, "
                                                 f"{payload['frames_processed']} frames",
                                       "measured": payload}]},
                metrics={"processing_ms": 0, "derived": True},
                warnings=[],
            )

        # Neither probe passed: refuse. The registry then continues the chain to
        # ffmpeg_enhancement, whose afftdn pass does the work for real.
        raise ProviderUnavailable(_unavailable_reason())


#: the registry loads ``impl.<key>.PROVIDER`` (contracts §1.1).
PROVIDER = RnnoiseDenoiseProvider


__all__ = [
    "AUDITED_ON",
    "FRAME_SAMPLES",
    "PROVIDER",
    "PROVIDER_KEY",
    "RnnoiseDenoiseProvider",
    "denoise_pcm16",
    "reset_probes",
]

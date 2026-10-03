"""Audio enhancement pipeline (Work 12 Lane C) -- contracts §6.

Ten stages, each independently toggleable, each reporting ONE of
``applied | skipped | unavailable(reason) | failed(reason)``. Output is always
a NEW derived ``MediaAsset``; the source bytes are never rewritten, never
re-encoded in place, and the guard that proves it is structural rather than
aspirational (see :func:`_guard_derived_path`).

Stage -> method (contracts §6, resolved at RUNTIME by probing this ffmpeg
build -- never hardcoded, so a different build honestly reports different
availability):

=================  ==========================================  ====================
stage              method                                       status on this build
=================  ==========================================  ====================
``denoise``        ``afftdn`` (or ``arnndn`` with a model file)  applied
``dereverb``       -- no method exists --                      unavailable
``voice_isolation``band-pass + ``afftdn`` (``band_isolation``)   applied
``silence_detection``  ``silencedetect`` measurement             applied (analysis)
``filler_detection``   the silence/filler lane's engine         unavailable here
``breath_click_detection``  stdlib PCM windows                  applied (heuristic)
``loudness_normalization``  ``loudnorm`` two-pass (EBU R128)    applied
``music_ducking``  ``sidechaincompress``                        applied
``compression``    ``acompressor``                              applied
``limiting``       ``alimiter`` + measured true-peak bound      applied
=================  ==========================================  ====================

The denoise adapter is isolated by construction: the pipeline asks the
``denoise`` CHAIN in the registry, never ``rnnoise_denoise`` by name. When no
neural denoiser is installed the chain continues to ``ffmpeg_enhancement`` and
the stage records ``method="afftdn"`` plus the reason the neural path was
skipped. Renderer and audio engine therefore have no compile-time coupling to
RNNoise at all.

Claims discipline (contracts §6, last line): a perceptual quality claim is
never asserted. :func:`_build_claims` produces entries ONLY where a measured
before/after pair exists, each carrying the metric name and the two numbers
that justify it. With no measurement there is no claim -- the list is empty,
not optimistic.

Emission discipline (contracts §3, Lane A's ordering rule): ``record_event``
and ``track_cost`` each open their own session, so this engine NEVER calls
them mid-transaction. Every event it wants is RETURNED in
``result["events"]`` (and may be handed to an injected ``emit`` callable) for
the ROUTE to publish after ``db.commit()`` -- the pattern of
``engine/collab/reviews.py``.
"""

from __future__ import annotations

import array
import hashlib
import logging
import math
import time
import wave
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.engine.intel import ffmpeg_util
from app.engine.intel.base import (
    IntelRequest,
    ProviderCancelled,
    ProviderTimeout,
    ProviderUnavailable,
    deadline_in,
    safe_health,
)
from app.models import MediaAsset, MediaIntelRun
from app.services import media_intel_runs as runs_service
from app.services import storage as storage_service

logger = logging.getLogger("ymoney.intel")

#: capability kind this engine drives (the registry's ``enhancement`` chain)
CAPABILITY = "enhancement"
#: run ``kind`` stored on ``media_intel_runs.kind``
RUN_KIND = "enhancement"
#: job kind for the worker path (contracts §14); registered at integration.
JOB_KIND = "MEDIA_INTEL_ENHANCE"

#: event kinds this lane reports for the ``WEBHOOK_EVENTS`` allowlist
EVENT_ENHANCE_COMPLETED = "MEDIA_INTEL_AUDIO_ENHANCE_COMPLETED"
EVENT_ENHANCE_PARTIAL = "MEDIA_INTEL_AUDIO_ENHANCE_PARTIAL"
EVENTS: tuple[str, ...] = (EVENT_ENHANCE_COMPLETED, EVENT_ENHANCE_PARTIAL)

#: derived files live under this workspace-relative prefix
DERIVED_PREFIX = "derived/audio-enhance"

#: every stage, in the order the pipeline evaluates them (contracts §6)
STAGES: tuple[str, ...] = (
    "denoise",
    "dereverb",
    "voice_isolation",
    "silence_detection",
    "filler_detection",
    "breath_click_detection",
    "loudness_normalization",
    "music_ducking",
    "compression",
    "limiting",
)

#: the four honest outcomes of a stage
STAGE_STATUSES: tuple[str, ...] = ("applied", "skipped", "unavailable", "failed")

#: stages that produce a measurement only -- they never touch the audio
ANALYSIS_STAGES: frozenset[str] = frozenset(
    {"silence_detection", "filler_detection", "breath_click_detection"}
)

#: default duration-drift tolerance (seconds) for the derived asset
DURATION_TOLERANCE_S = 0.05


class AudioEnhancementError(ValueError):
    """A deliberate, operator-facing enhancement failure (route -> 422)."""


# ---------------------------------------------------------------------------
# stdlib PCM primitives (no numpy, no soundfile -- contracts §0)
# ---------------------------------------------------------------------------


def read_pcm16(path: str | Path) -> dict:
    """Mono float samples + rate for a 16-bit PCM WAV, via ``wave``+``array``.

    Returns ``{"available", "reason", "samples", "rate", "channels", "frames"}``.
    ``available`` is False (with a reason) for a compressed or non-16-bit
    container -- a heuristic that cannot read its input says so instead of
    guessing. Multi-channel input is downmixed by averaging.
    """
    empty = {
        "available": False,
        "reason": "",
        "samples": [],
        "rate": 0,
        "channels": 0,
        "frames": 0,
    }
    target = str(path or "")
    if not target:
        return {**empty, "reason": "no path supplied"}
    try:
        with wave.open(target, "rb") as handle:
            if handle.getcomptype() != "NONE":
                return {**empty, "reason": f"not uncompressed PCM ({handle.getcomptype()})"}
            if handle.getsampwidth() != 2:
                bits = handle.getsampwidth() * 8
                return {**empty, "reason": f"expected 16-bit PCM, got {bits}-bit"}
            channels = handle.getnchannels()
            rate = handle.getframerate()
            raw = handle.readframes(handle.getnframes())
    except (wave.Error, OSError, EOFError) as exc:
        return {**empty, "reason": f"unreadable PCM input ({type(exc).__name__})"}
    pcm = array.array("h")
    pcm.frombytes(raw)
    if channels > 1:
        usable = len(pcm) - (len(pcm) % channels)
        pcm = array.array(
            "h",
            [sum(pcm[i:i + channels]) // channels for i in range(0, usable, channels)],
        )
    return {
        "available": True,
        "reason": "",
        "samples": [value / 32768.0 for value in pcm],
        "rate": rate,
        "channels": channels,
        "frames": len(pcm),
    }


def count_clipped_samples(path: str | Path) -> int | None:
    """Samples pinned at full scale (|s16| >= 32767), or None when unreadable.

    ``None`` means "not measured" -- never 0, because "no clipping" and "could
    not look" are different facts and QC depends on telling them apart.
    """
    target = str(path or "")
    if not target:
        return None
    try:
        with wave.open(target, "rb") as handle:
            if handle.getsampwidth() != 2 or handle.getcomptype() != "NONE":
                return None
            raw = handle.readframes(handle.getnframes())
    except (wave.Error, OSError, EOFError):
        return None
    pcm = array.array("h")
    pcm.frombytes(raw)
    return sum(1 for value in pcm if value >= 32767 or value <= -32768)


def _dbfs(value: float) -> float | None:
    """Linear amplitude -> dBFS, or None for digital silence."""
    magnitude = abs(float(value))
    if magnitude <= 0.0:
        return None
    return round(20.0 * math.log10(magnitude), 3)


def _rms_dbfs(samples: Sequence[float]) -> float | None:
    if not samples:
        return None
    mean_square = sum(value * value for value in samples) / len(samples)
    if mean_square <= 0.0:
        return None
    return round(20.0 * math.log10(math.sqrt(mean_square)), 3)


def measure_audio(path: str | Path) -> dict:
    """Before/after measurement set for ONE file (contracts §6).

    Every field is a MEASUREMENT or ``None``:

    * ``duration_s`` / ``peak_dbfs`` / ``clipped_samples`` / ``rms_dbfs`` --
      ffprobe + ``astats`` + the stdlib PCM reader;
    * ``integrated_lufs`` / ``lra`` / ``true_peak_dbfs`` -- ``ebur128``.

    ``ffmpeg_util`` never raises, so an unreadable file yields a dict of
    ``None`` values rather than an exception; callers can then say "could not
    measure" instead of asserting a level.
    """
    target = str(path or "")
    loudness = ffmpeg_util.measure_loudness(target)
    peaks = ffmpeg_util.measure_peaks(target)
    pcm = read_pcm16(target)
    samples = pcm["samples"] if pcm["available"] else []
    return {
        "path": target,
        "duration_s": ffmpeg_util.duration_seconds(target),
        "peak_dbfs": _round(peaks.get("peak_dbfs")),
        "clipped_samples": count_clipped_samples(target),
        "integrated_lufs": _round(loudness.get("integrated_lufs")),
        "lra": _round(loudness.get("lra")),
        "true_peak_dbfs": _round(loudness.get("true_peak_dbfs")),
        "rms_dbfs": _rms_dbfs(samples),
        "sample_rate": pcm["rate"] or None,
        "channels": pcm["channels"] or None,
        "pcm_readable": bool(pcm["available"]),
        "pcm_reason": pcm["reason"],
    }


def _round(value: Any, digits: int = 3) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), digits)
    except (TypeError, ValueError):
        return None


def before_after(before: dict, after: dict | None) -> dict:
    """Side-by-side measurement block with explicit deltas.

    ``after`` is ``None`` for an analysis-only request (no derived media), and
    every ``*_delta`` then stays ``None`` rather than implying a change.
    """
    block: dict[str, Any] = {"before": dict(before or {})}
    if not after:
        return {**block, "after": None, "deltas": {}, "measured": False}
    deltas: dict[str, float | None] = {}
    for key in ("duration_s", "peak_dbfs", "integrated_lufs", "lra", "true_peak_dbfs",
                 "clipped_samples", "rms_dbfs"):
        old, new = before.get(key), after.get(key)
        deltas[key] = (
            round(float(new) - float(old), 3)
            if isinstance(old, (int, float)) and isinstance(new, (int, float))
            else None
        )
    return {"before": dict(before or {}), "after": dict(after), "deltas": deltas,
            "measured": True}


# ---------------------------------------------------------------------------
# breath / click detection -- stdlib PCM heuristic (method=heuristic_pcm)
# ---------------------------------------------------------------------------

#: honest confidence vocabulary for the heuristic: it is never "high"
CONFIDENCE_HEURISTIC = "low"


def detect_breath_click(
    path: str | Path,
    *,
    window_ms: float = 30.0,
    max_events: int = 200,
    click_ratio: float = 4.0,
    breath_dbfs: float = -45.0,
) -> dict:
    """Windowed PCM analysis for breaths and clicks.

    A CLICK is a window whose peak-to-RMS ratio spikes far above the file's own
    median ratio (a near-impulsive event) -- a within-file comparison, so it
    needs no absolute threshold and cannot be tuned by file loudness.

    A BREATH is a short window that is quiet in absolute terms but still carries
    energy above digital silence, with a high-frequency fraction typical of
    unvoiced frication. Both are *candidates for an editor to look at*.

    This is a HEURISTIC, not a detector of record, and says so:
    ``method="heuristic_pcm"``, ``confidence="low"``, plus ``basis`` text. No
    model is loaded, nothing is inferred about WHO is speaking, and an
    unreadable input returns ``available=False`` with a reason rather than an
    empty-but-successful result.
    """
    pcm = read_pcm16(path)
    if not pcm["available"]:
        return {
            "available": False,
            "reason": pcm["reason"] or "PCM input unreadable",
            "method": "heuristic_pcm",
            "confidence": CONFIDENCE_HEURISTIC,
            "events": [],
        }
    samples: list[float] = pcm["samples"]
    rate = int(pcm["rate"] or 0)
    if rate <= 0 or not samples:
        return {
            "available": False,
            "reason": "no samples in PCM input",
            "method": "heuristic_pcm",
            "confidence": CONFIDENCE_HEURISTIC,
            "events": [],
        }
    window = max(8, int(rate * max(1.0, float(window_ms)) / 1000.0))
    windows: list[tuple[int, list[float], float, float]] = []
    for start in range(0, len(samples), window):
        chunk = samples[start:start + window]
        if len(chunk) < 8:
            continue
        rms = math.sqrt(sum(v * v for v in chunk) / len(chunk))
        peak = max(abs(v) for v in chunk)
        high = sum(1 for v in chunk if abs(v) >= 0.5 * peak)
        windows.append((start, chunk, rms, high / len(chunk)))

    ratios = sorted((peak / rms) if rms > 0 else 0.0 for _, _, rms, _ in windows)
    median_ratio = ratios[len(ratios) // 2] if ratios else 0.0

    events: list[dict] = []
    for start, chunk, rms, hf_fraction in windows:
        t_s = round(start / rate, 3)
        duration_s = round(len(chunk) / rate, 3)
        ratio = (max(abs(v) for v in chunk) / rms) if rms > 0 else 0.0
        level = _dbfs(rms)
        if median_ratio > 0 and ratio >= click_ratio * median_ratio and rms > 0:
            events.append({
                "kind": "click", "start_s": t_s, "end_s": round(t_s + duration_s, 3),
                "level_dbfs": level, "crest_db": _dbfs(ratio) or 0.0,
                "score": round(min(1.0, ratio / (click_ratio * median_ratio)), 3),
            })
        elif (
            level is not None
            and level <= float(breath_dbfs)
            and rms > 0.0
            and hf_fraction >= 0.25
        ):
            events.append({
                "kind": "breath", "start_s": t_s, "end_s": round(t_s + duration_s, 3),
                "level_dbfs": level, "hf_fraction": round(hf_fraction, 3),
                "score": round(min(1.0, hf_fraction), 3),
            })
        if len(events) >= max(1, int(max_events)):
            break

    return {
        "available": True,
        "reason": "",
        "method": "heuristic_pcm",
        "confidence": CONFIDENCE_HEURISTIC,
        "basis": (
            "windowed stdlib PCM analysis: crest-ratio outliers vs the file's own "
            "median (clicks), quiet high-frequency-rich windows (breaths)"
        ),
        "window_ms": float(window_ms),
        "window_count": len(windows),
        "median_crest_ratio": round(median_ratio, 3),
        "breath_count": sum(1 for e in events if e["kind"] == "breath"),
        "click_count": sum(1 for e in events if e["kind"] == "click"),
        "events": events,
    }


# ---------------------------------------------------------------------------
# storage + lineage
# ---------------------------------------------------------------------------


def _storage_root() -> Path:
    return Path(storage_service.STORAGE_ROOT)


def asset_path(workspace_id: str, storage_key: str) -> Path | None:
    """Absolute path of a workspace asset, or None when it escapes the root.

    Two key shapes exist in the wild and both must resolve: an ABSOLUTE (or
    CWD-relative) key, which ``managed_path`` handles directly, and the
    WORKSPACE-relative key the model documents (``validate_storage_key``'s
    shape). The workspace-relative branch is re-checked through
    ``managed_path`` so a traversal attempt fails closed exactly like the first
    branch -- containment is never taken on trust from the key alone.
    """
    key = str(storage_key or "").strip()
    if not workspace_id or not key:
        return None
    direct = storage_service.managed_path(workspace_id, key)
    if direct is not None:
        return direct
    normalized = storage_service.validate_storage_key(workspace_id, key)
    if not normalized:
        return None
    candidate = _storage_root() / workspace_id / normalized
    return storage_service.managed_path(workspace_id, str(candidate))


def _guard_derived_path(workspace_id: str, key: str, source_key: str) -> Path:
    """Validate a derived key and refuse any collision with the source.

    Containment is enforced by ``validate_storage_key`` + ``managed_path`` (the
    storage boundary), and the ``key == source_key`` refusal is the
    never-overwrite guard at the naming level -- on top of the byte-level proof
    the ffmpeg pass itself gives us (``run_filter`` raises on ``dst == src``).
    """
    normalized = storage_service.validate_storage_key(workspace_id, key)
    if not normalized:
        raise AudioEnhancementError("refusing to register a path outside workspace storage")
    if source_key and normalized == str(source_key).replace("\\", "/").lstrip("/"):
        raise AudioEnhancementError(
            "refusing to write the enhanced audio over its source asset (derived-only)"
        )
    return _storage_root() / workspace_id / normalized


def _sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 256), b""):
            digest.update(block)
    return digest.hexdigest()


#: source extensions the derived file may keep; anything else becomes .wav
_KEEP_SUFFIXES: frozenset[str] = frozenset({".wav", ".mp4", ".m4a", ".mp3", ".flac", ".ogg"})


def derived_filename(source_key: str, run_id: str) -> str:
    """Deterministic derived filename: source stem + run id, never the source name.

    The run id in the name is what makes two runs of the same source coexist as
    separate files -- a second run can never collide with the first one's
    output, and no derived file can ever be mistaken for the source.
    """
    source_path = Path(str(source_key or "audio.wav"))
    stem = source_path.stem or "audio"
    safe_stem = "".join(c if (c.isalnum() or c in "-_") else "_" for c in stem)[:60]
    suffix = source_path.suffix.lower()
    extension = suffix if suffix in _KEEP_SUFFIXES else ".wav"
    return f"{safe_stem}_{str(run_id)[:8]}{extension}"


# ---------------------------------------------------------------------------
# stage records
# ---------------------------------------------------------------------------


def _stage_record(stage: str, status: str, reason: str, **extra: Any) -> dict:
    """One honest stage outcome. ``reason`` is mandatory for the negative three."""
    if status not in STAGE_STATUSES:
        raise ValueError(f"unknown stage status {status!r}")
    record = {
        "stage": stage,
        "status": status,
        "reason": str(reason or ""),
        "method": "",
        "filters": [],
        "neural": False,
        "confidence": "",
    }
    record.update(extra)
    if status in ("unavailable", "failed", "skipped") and not record["reason"]:
        raise ValueError(f"stage {stage} is {status} without a reason")
    return record


def normalise_stages(requested: Any) -> tuple[list[str], list[dict]]:
    """Split a stage request into (enabled, skipped-with-reason).

    Accepts a list/tuple/set of names or a ``{name: {params}}`` mapping. An
    unknown name, or a name that is not in the contract vocabulary, is reported
    as ``skipped`` with its reason -- it is never silently ignored, because a
    typo that quietly disables a stage is worse than a visible no-op.
    """
    names: list[str] = []
    if isinstance(requested, (dict, list, tuple, set)):
        names = [str(name) for name in requested]
    elif requested in (None, ""):
        names = []
    else:
        names = [str(requested)]

    enabled: list[str] = []
    skipped: list[dict] = []
    seen: set[str] = set()
    for raw in names:
        name = raw.strip().lower()
        if name in seen:
            continue
        seen.add(name)
        if name not in STAGES:
            skipped.append(_stage_record(raw.strip() or "?", "skipped",
                                         f"unknown stage '{raw}'; the contract "
                                         f"vocabulary is {', '.join(STAGES)}"))
            continue
        if name in enabled:
            continue
        enabled.append(name)
    enabled.sort(key=STAGES.index)
    return enabled, skipped


# ---------------------------------------------------------------------------
# claims (measured basis only -- contracts §6)
# ---------------------------------------------------------------------------


def _build_claims(
    before: dict, after: dict | None, stages: Sequence[dict], params: dict | None = None
) -> list[dict]:
    params = dict(params or {})
    """Claims that a measurement actually supports. Empty when nothing does.

    Every entry names the metric, the two measured numbers and the delta, so a
    reviewer can recompute it. A perceptual statement ("sounds cleaner",
    "improves speech") is never emitted -- there is no measurement for it here,
    and the list stays empty instead.
    """
    claims: list[dict] = []
    applied = {s["stage"] for s in stages if s.get("status") == "applied"}
    if not after or not before:
        return claims

    before_lufs = before.get("integrated_lufs")
    after_lufs = after.get("integrated_lufs")
    target = params.get("target_lufs")
    if "loudness_normalization" in applied and before_lufs is not None and after_lufs is not None:
        # A loudness claim is only made when the measurement shows the level
        # actually MOVED TOWARD the requested target. "It got louder" is not
        # evidence that the normalisation worked, so when it moved away the
        # entry is dropped and the run's warnings carry the drift.
        moved_closer = True
        if isinstance(target, (int, float)):
            moved_closer = abs(after_lufs - float(target)) <= abs(before_lufs - float(target))
        if moved_closer:
            claims.append({
                "claim": "integrated loudness moved toward the requested target",
                "metric": "integrated_lufs",
                "measured": {"before": before_lufs, "after": after_lufs,
                             "target": _round(target), "unit": "LUFS",
                             "delta": round(after_lufs - before_lufs, 2)},
            })

    before_peak = before.get("peak_dbfs")
    after_peak = after.get("peak_dbfs")
    if {"compression", "limiting"} & applied and before_peak is not None \
            and after_peak is not None:
        claims.append({
            "claim": "sample peak level changed by the dynamics stage",
            "metric": "peak_dbfs",
            "measured": {"before": before_peak, "after": after_peak,
                         "delta": round(after_peak - before_peak, 2)},
        })

    before_clip = before.get("clipped_samples")
    after_clip = after.get("clipped_samples")
    if before_clip is not None and after_clip is not None and before_clip != after_clip:
        claims.append({
            "claim": "full-scale sample count changed",
            "metric": "clipped_samples",
            "measured": {"before": before_clip, "after": after_clip,
                         "delta": after_clip - before_clip},
        })

    before_rms = before.get("rms_dbfs")
    after_rms = after.get("rms_dbfs")
    if {"denoise", "voice_isolation"} & applied and before_rms is not None \
            and after_rms is not None:
        claims.append({
            "claim": "broadband RMS level changed by the restoration stage",
            "metric": "rms_dbfs",
            "measured": {"before": before_rms, "after": after_rms,
                         "delta": round(after_rms - before_rms, 2)},
        })
    return claims


# ---------------------------------------------------------------------------
# the pipeline
# ---------------------------------------------------------------------------

EmitFn = Callable[[dict], None]


class AudioEnhancementPipeline:
    """Orchestrates one enhancement run: plan -> apply -> measure -> derive.

    Pure orchestration plus measurement (contracts §1.2), so it runs in-process
    in tests and inside a request handler when ``enqueue`` is not used. The
    provider is resolved through the registry CHAIN, which is what keeps the
    renderer and the audio engine free of any hard coupling to a denoiser.
    """

    def __init__(
        self,
        db: Session,
        *,
        capability: str = CAPABILITY,
        denoise_capability: str = "denoise",
        emit: EmitFn | None = None,
    ) -> None:
        self.db = db
        self.capability = str(capability or CAPABILITY)
        self.denoise_capability = str(denoise_capability or "denoise")
        self._emit = emit

    # -- provider resolution ------------------------------------------------

    def resolve(self) -> tuple[Any, dict[str, str]]:
        """First healthy ``enhancement`` provider + why each candidate was skipped."""
        from app.engine.intel import registry

        provider, reasons = registry.resolve_or_unavailable(self.capability)
        return provider, dict(reasons)

    def resolve_denoise(self) -> tuple[Any, dict[str, str]]:
        """Resolve the DENOISE chain (never a named backend)."""
        from app.engine.intel import registry

        return registry.resolve_or_unavailable(self.denoise_capability)

    def providers_snapshot(self) -> list[dict]:
        """Health/capability/license for the providers this run may use."""
        from app.engine.intel import registry

        keys = []
        for kind in (self.capability, self.denoise_capability):
            for key in registry.chain_for(kind):
                if key not in keys:
                    keys.append(key)
        return [registry.get_provider(key).to_dict() for key in keys]

    # -- run ---------------------------------------------------------------

    def run(
        self,
        ws: Any,
        asset: MediaAsset,
        *,
        stages: Any = None,
        stage_params: dict | None = None,
        params: dict | None = None,
        force: bool = False,
        requested_by: str | None = None,
        emit: EmitFn | None = None,
        enqueue: bool = False,
    ) -> dict:
        """Enhance ``asset`` and return the run DTO + stage matrix + manifest.

        Never raises for a domain outcome: no provider ends in the terminal
        ``UNAVAILABLE`` state, a stage that cannot run ends ``unavailable``/``failed``
        on its own row, and only a genuine bug escapes.
        """
        db = self.db
        emitter = emit or self._emit
        requested_params = dict(params or {})
        if stage_params:
            requested_params["stage_params"] = dict(stage_params)
        enabled, skipped = normalise_stages(stages)
        requested_params["stages"] = enabled
        if not enabled and not skipped:
            raise AudioEnhancementError(
                "no stages requested; pass at least one of: " + ", ".join(STAGES)
            )

        provider, reasons = self.resolve()
        health = safe_health(provider)
        if not health.available:
            return self._unavailable(ws, asset, requested_params, health.reason, reasons)

        run_dto = runs_service.create_run(
            db, ws, kind=RUN_KIND, asset=asset,
            provider_key=str(getattr(provider, "key", "") or "unavailable"),
            model_version=str(getattr(provider, "key", "") or ""),
            params=requested_params, force=force, requested_by=requested_by,
        )
        if run_dto.get("cache_hit"):
            # A cached answer is not new work: no recompute, no cost, no event.
            return self._result(run_dto, db, cache_hit=True, events=[])

        run = db.get(MediaIntelRun, run_dto["id"])
        if run is None:  # pragma: no cover - the row was just flushed
            raise AudioEnhancementError("run row disappeared after creation")
        runs_service.start_run(db, run)

        if enqueue:
            job_id = self._enqueue(ws, run)
            db.flush()
            return self._result(
                runs_service.run_dto(run), db, cache_hit=False,
                job={"id": job_id, "kind": JOB_KIND, "queued": bool(job_id)},
                events=[],
            )

        stage_matrix = list(skipped)
        params_with_output = dict(requested_params)
        started = time.perf_counter()
        warnings: list[str] = []
        analysis: dict[str, Any] = {}
        artifact: dict[str, Any] = {}
        error_code = ""
        try:
            path = self._source_path(ws, asset)
            storage_key = self._derived_key(run.id, asset)
            output_path = _guard_derived_path(ws.id, storage_key, str(asset.storage_key or ""))
            params_with_output["output_path"] = str(output_path)

            result = provider.run(
                IntelRequest(
                    workspace_id=str(ws.id),
                    asset_id=str(asset.id),
                    storage_path=str(path),
                    params=params_with_output,
                    asset_checksum=str(asset.checksum or ""),
                    run_id=str(run.id),
                ),
                progress=lambda fraction: self._progress(run, fraction),
                should_cancel=lambda: bool(run.cancel_requested),
                deadline=deadline_in(float(requested_params.get("timeout_s") or 0) or None),
            )
            warnings.extend(str(w)[:200] for w in (result.warnings or []))
            reported = result.artifacts.get("stages") or []
            stage_matrix = self._merge_stages(skipped, reported, enabled)
            analysis = self._analysis_payload(stage_matrix)
            artifact = dict(result.artifacts.get("audio") or {})
        except ProviderUnavailable as exc:
            runs_service.unavailable_run(db, run, str(exc))
            return self._result(runs_service.run_dto(run), db, events=self._events(
                emitter, ws, run, partial=False, reason=str(exc)))
        except (ProviderCancelled, ProviderTimeout) as exc:
            error_code = "CANCELLED" if isinstance(exc, ProviderCancelled) else "TIMEOUT"
            runs_service.fail_run(db, run, error_code)
            return self._result(runs_service.run_dto(run), db, events=self._events(
                emitter, ws, run, partial=True, reason=str(exc)))
        except (AudioEnhancementError, ValueError) as exc:
            error_code = "INVALID_REQUEST"
            runs_service.fail_run(db, run, error_code)
            return self._result(runs_service.run_dto(run), db, warnings=[str(exc)[:200]],
                                events=self._events(emitter, ws, run, partial=True,
                                                    reason=str(exc)))
        except Exception as exc:  # noqa: BLE001 - a run must always land somewhere
            logger.exception("audio enhancement run %s failed", run.id)
            error_code = "ENHANCE_FAILED"
            runs_service.fail_run(db, run, error_code)
            return self._result(runs_service.run_dto(run), db,
                                events=self._events(emitter, ws, run, partial=True,
                                                    reason=type(exc).__name__))

        processing_ms = int((time.perf_counter() - started) * 1000)
        source_measurement = measure_audio(path)
        output_asset_id = ""
        output_measurement: dict | None = None
        if artifact.get("path"):
            derived = Path(str(artifact["path"]))
            if derived.exists():
                output_asset_id = self._register_asset(
                    ws, asset, run, derived, storage_key, source_measurement,
                    stage_matrix, provider,
                )
                output_measurement = measure_audio(derived)
            else:  # pragma: no cover - the provider reports only written files
                warnings.append("derived audio file is missing; no asset registered")
        comparison = before_after(source_measurement, output_measurement)
        claims = _build_claims(source_measurement, output_measurement, stage_matrix,
                               requested_params)
        limiter_bound = _true_peak_verdict(stage_matrix, output_measurement,
                                           requested_params)
        provider_health = safe_health(provider)
        license_info = _license_dict(provider)
        cost = _cost_dict(provider, processing_ms)
        metrics = self._manifest(
            run, provider, provider_health, license_info, stage_matrix, analysis,
            comparison, claims, cost, processing_ms, output_measurement,
        )
        metrics["true_peak_limit"] = limiter_bound
        if limiter_bound.get("exceeded"):
            warnings.append(
                f"true peak {limiter_bound['measured_true_peak_dbfs']} dBFS exceeds the "
                f"requested ceiling {limiter_bound['ceiling_dbfs']} dBFS"
            )
        if comparison.get("measured") and comparison["deltas"].get("duration_s") is not None:
            drift = abs(float(comparison["deltas"]["duration_s"]))
            if drift > DURATION_TOLERANCE_S:
                warnings.append(
                    f"derived duration drifted {drift:.3f}s from the source "
                    f"(tolerance {DURATION_TOLERANCE_S}s)"
                )
                metrics["duration_drift_s"] = comparison["deltas"]["duration_s"]
        runs_service.complete_run(
            db, run, metrics=metrics, warnings=warnings,
            output_asset_id=output_asset_id or None,
            processing_ms=processing_ms, gpu_ms=0,
            cost_micros=int(cost.get("cost_micros") or 0),
        )
        return self._result(
            runs_service.run_dto(run), db,
            stages=stage_matrix, analysis=analysis, comparison=comparison,
            claims=claims, manifest=metrics, warnings=warnings, events=self._events(
                emitter, ws, run, partial=False,
                reason="; ".join(warnings[:3]) or "",
            ),
        )

    # -- steps --------------------------------------------------------------

    @staticmethod
    def _progress(run: MediaIntelRun, fraction: float) -> None:
        try:
            value = max(0, min(99, int(float(fraction) * 100)))
        except (TypeError, ValueError):  # pragma: no cover - defensive
            value = run.progress or 0
        run.progress = max(int(run.progress or 0), value)

    def _source_path(self, ws: Any, asset: MediaAsset) -> Path:
        path = asset_path(str(ws.id), str(asset.storage_key or ""))
        if path is None:
            raise AudioEnhancementError("source asset path is outside workspace storage")
        if not path.exists():
            raise AudioEnhancementError("source media file is not readable")
        return path

    def _derived_key(self, run_id: str, asset: MediaAsset) -> str:
        return (f"{DERIVED_PREFIX}/{run_id}/"
                f"{derived_filename(str(asset.storage_key or 'audio'), run_id)}")

    def _enqueue(self, ws: Any, run: MediaIntelRun) -> str:
        """Hand the run to the worker (contracts §1.2/§14).

        Enqueueing is best-effort and never fails the request: the run row is
        the durable record, and an operator can retry it from the API. The job
        kind is ``MEDIA_INTEL_ENHANCE``; the handler is registered at
        integration time by the orchestrator, so ``handler_registered`` is
        reported honestly instead of pretending the worker will pick it up.
        """
        try:
            from app.services import jobs as jobs_service

            job_id = jobs_service.enqueue(
                JOB_KIND,
                {"run_id": run.id, "workspace_id": run.workspace_id,
                 "asset_id": run.asset_id, "params": dict(run.params_json or {})},
                workspace_id=run.workspace_id,
                idempotency_key=f"{JOB_KIND}:{run.id}",
            )
        except Exception as exc:  # noqa: BLE001 - enqueue must not break the request
            logger.warning("enhance enqueue failed for run %s: %s", run.id,
                           type(exc).__name__)
            return ""
        return str(job_id or "")

    def _merge_stages(
        self, skipped: list[dict], reported: list[dict], enabled: list[str]
    ) -> list[dict]:
        """Provider stage records + skipped request entries, in contract order.

        Any enabled stage the provider did not report is filled in as
        ``unavailable`` with a reason -- a stage that vanishes from the matrix
        would read as "not requested".
        """
        by_stage: dict[str, dict] = {}
        for record in skipped:
            by_stage[record["stage"]] = dict(record)
        for record in reported:
            if not isinstance(record, dict):
                continue
            stage = str(record.get("stage") or "").strip().lower()
            if stage not in STAGES:
                continue
            status = str(record.get("status") or "").strip().lower()
            by_stage[stage] = _stage_record(
                stage,
                status if status in STAGE_STATUSES else "failed",
                str(record.get("reason") or "provider gave no reason"),
                method=str(record.get("method") or ""),
                filters=list(record.get("filters") or []),
                neural=bool(record.get("neural", False)),
                confidence=str(record.get("confidence") or ""),
                analyzed=str(record.get("analyzed") or ""),
                measured=record.get("measured"),
                result=record.get("result"),
                processing_ms=int(record.get("processing_ms") or 0),
            )
        for stage in enabled:
            by_stage.setdefault(stage, _stage_record(
                stage, "unavailable", "stage was not reported by the provider"))
        ordered: list[dict] = []
        for stage in STAGES:
            if stage in by_stage:
                ordered.append(by_stage[stage])
        return ordered

    @staticmethod
    def _analysis_payload(stages: Sequence[dict]) -> dict:
        """The measurement results of the analysis stages, keyed by stage."""
        payload: dict[str, Any] = {}
        for record in stages:
            if (record["stage"] in ANALYSIS_STAGES and record["status"] == "applied"
                    and record.get("result") is not None):
                payload[record["stage"]] = record["result"]
        return payload

    def _register_asset(
        self,
        ws: Any,
        source: MediaAsset,
        run: MediaIntelRun,
        derived: Path,
        storage_key: str,
        source_measurement: dict,
        stages: list[dict],
        provider: Any,
    ) -> str:
        """Create the NEW derived MediaAsset with full lineage.

        Lineage is written in BOTH places the repo uses: the additive
        ``parent_asset_id``/``derivation_json`` columns AND the conventional
        ``meta_json`` lineage dict. The source row is not touched.
        """
        provider_key = str(getattr(provider, "key", "") or "")
        after = measure_audio(derived)
        size = derived.stat().st_size if derived.exists() else None
        derivation = {
            "source_asset_id": source.id,
            "run_id": run.id,
            "provider": provider_key,
            "kind": RUN_KIND,
            "stages": [
                {"stage": s["stage"], "status": s["status"], "method": s["method"]}
                for s in stages
            ],
            "params": dict(run.params_json or {}),
            "processing_ms": int(run.processing_ms or 0),
            "gpu_ms": 0,
            "quality": {"before": source_measurement, "after": after},
        }
        meta = dict(source.meta_json or {})
        meta.update({
            "lineage": {
                "source_asset_id": source.id,
                "run_id": run.id,
                "provider": provider_key,
                "kind": RUN_KIND,
                "derived_at": _iso_now(),
            },
            "intel": {"stages": derivation["stages"], "params": derivation["params"]},
        })
        asset = MediaAsset(
            workspace_id=ws.id,
            type=str(source.type or "audio"),
            origin="generated",
            provider=provider_key or "ffmpeg",
            storage_key=storage_key,
            mime_type=_mime_for(derived),
            duration_seconds=after.get("duration_s") or None,
            sample_rate=after.get("sample_rate") or None,
            channels=after.get("channels") or None,
            audio_codec="pcm_s16le" if derived.suffix.lower() == ".wav" else "aac",
            file_size=size,
            checksum=_sha256_file(derived),
            meta_json=meta,
            parent_asset_id=source.id,
            derivation_json=derivation,
        )
        self.db.add(asset)
        self.db.flush()
        return str(asset.id)

    def _manifest(
        self,
        run: MediaIntelRun,
        provider: Any,
        health: Any,
        license_info: dict,
        stages: list[dict],
        analysis: dict,
        comparison: dict,
        claims: list[dict],
        cost: dict,
        processing_ms: int,
        after: dict | None,
    ) -> dict:
        """The processing manifest stored on the run and the derived asset."""
        return {
            "pipeline": "audio_enhancement",
            "provider": str(getattr(provider, "key", "") or ""),
            "provider_health": health.to_dict() if hasattr(health, "to_dict") else {},
            "license": license_info,
            "model_version": str(run.model_version or ""),
            "stages": stages,
            "analysis": analysis,
            "before_after": comparison,
            # ``engine.intel.qc._flatten_metrics`` documents that a QC pass reads
            # the run's own measurements from TOP-LEVEL ``source``/``output``
            # blocks (it never re-measures). These two aliases are that
            # contract; ``before_after`` stays the canonical, richer form.
            "source": dict(comparison.get("before") or {}),
            "output": dict(comparison.get("after") or {}),
            "claims": claims,
            "claims_policy": (
                "measured metrics only; no perceptual quality claim is made "
                "because no perceptual measurement is performed"
            ),
            "cost": cost,
            "processing_ms": processing_ms,
            "gpu_ms": 0,
            "latency_ms_per_audio_second": _latency(processing_ms, after),
            "derived": bool(after),
            "stages_applied": [s["stage"] for s in stages if s["status"] == "applied"],
            "stages_unavailable": [
                {"stage": s["stage"], "reason": s["reason"]}
                for s in stages if s["status"] == "unavailable"
            ],
            "stages_failed": [
                {"stage": s["stage"], "reason": s["reason"]}
                for s in stages if s["status"] == "failed"
            ],
            "stages_skipped": [
                {"stage": s["stage"], "reason": s["reason"]}
                for s in stages if s["status"] == "skipped"
            ],
        }

    def _result(
        self,
        dto: dict,
        db: Session,
        *,
        stages: list[dict] | None = None,
        analysis: dict | None = None,
        comparison: dict | None = None,
        claims: list[dict] | None = None,
        manifest: dict | None = None,
        warnings: list[str] | None = None,
        events: list[dict] | None = None,
        cache_hit: bool = False,
        job: dict | None = None,
    ) -> dict:
        """The payload the route returns. Commits are the ROUTE's job."""
        return {
            "run": dto,
            "stages": stages or [],
            "analysis": analysis or {},
            "before_after": comparison or {},
            "claims": claims or [],
            "manifest": manifest or {},
            "warnings": warnings or [],
            "events": events or [],
            "cache_hit": bool(cache_hit),
            "job": job or {},
        }

    def _events(
        self,
        emitter: EmitFn | None,
        ws: Any,
        run: MediaIntelRun,
        *,
        partial: bool,
        reason: str,
    ) -> list[dict]:
        """The ONE event kind this lane reports, returned for the route to emit.

        ``record_event``/``track_cost`` open their own session, so emitting from
        here would deadlock the transaction. The run lifecycle events
        (``MEDIA_INTEL_RUN_*``) are emitted by ``services.media_intel_runs``
        itself, which commits first; this list is what the lane ADDs.
        """
        stage_rows = [s for s in (run.metrics_json or {}).get("stages", [])
                     if isinstance(s, dict)]
        partial_run = partial or any(s.get("status") == "failed" for s in stage_rows)
        kind = EVENT_ENHANCE_PARTIAL if partial_run else EVENT_ENHANCE_COMPLETED
        event = {
            "kind": kind,
            "workspace_id": str(ws.id),
            "message": (
                f"audio enhancement {'partially applied' if partial_run else 'completed'}"
                + (f": {reason[:120]}" if reason else "")
            ),
            "level": "warning" if partial_run else "info",
            "source": "media_intel",
            "data": {
                "run_id": str(run.id),
                "asset_id": str(run.asset_id or ""),
                "output_asset_id": run.output_asset_id,
                "provider": str(run.provider_key or ""),
                "stages": [s.get("stage") for s in stage_rows
                           if s.get("status") == "applied"],
                "stages_unavailable": [s.get("stage") for s in stage_rows
                                       if s.get("status") == "unavailable"],
                "reason": str(reason or "")[:200],
            },
        }
        if emitter is not None:
            try:
                emitter(event)
            except Exception as exc:  # noqa: BLE001 - telemetry never breaks a run
                logger.warning("injected enhance emit failed: %s", type(exc).__name__)
        return [event]

    def _unavailable(
        self, ws: Any, asset: MediaAsset, params: dict, reason: str, reasons: dict
    ) -> dict:
        """No provider could serve: still a durable run, in the terminal state."""
        run_dto = runs_service.create_run(
            self.db, ws, kind=RUN_KIND, asset=asset, provider_key="unavailable",
            model_version="", params=params, requested_by=None,
        )
        run = self.db.get(MediaIntelRun, run_dto["id"])
        if run is not None and not run_dto.get("cache_hit"):
            detail = "; ".join(f"{key}: {why}" for key, why in reasons.items())
            runs_service.unavailable_run(
                self.db, run, f"{reason} ({detail})" if detail else str(reason)
            )
        return self._result(runs_service.run_dto(run) if run is not None else run_dto,
                            self.db, events=[])


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _license_dict(provider: Any) -> dict:
    try:
        info = provider.license_info()
    except Exception as exc:  # noqa: BLE001 - never fail a manifest
        return {"commercial_use": "UNVERIFIED", "error": type(exc).__name__}
    return {
        "code_license": info.code_license,
        "model_license": info.model_license,
        "model_gated": bool(info.model_gated),
        "commercial_use": info.commercial_use,
        "audited_on": info.audited_on,
        "notes": info.notes,
    }


def _cost_dict(provider: Any, processing_ms: int) -> dict:
    try:
        spec = provider.resource_requirements()
        cost = provider.cost(spec)
    except Exception as exc:  # noqa: BLE001 - cost is never a correctness issue
        return {"cost_micros": 0, "error": type(exc).__name__}
    return {
        "gpu_ms": int(cost.get("gpu_ms") or 0),
        "cpu_ms": int(cost.get("cpu_ms") or 0),
        "cost_micros": int(cost.get("cost_micros") or 0),
        "billed": bool(cost.get("billed", False)),
        "measured_processing_ms": int(processing_ms),
    }


def _latency(processing_ms: int, after: dict | None) -> float | None:
    duration = (after or {}).get("duration_s")
    if not duration:
        return None
    return round(processing_ms / float(duration), 2)


def _true_peak_verdict(
    stages: Sequence[dict], after: dict | None, params: dict
) -> dict:
    """Did the limiter actually keep the true peak under the requested ceiling?

    The contracts list ``limiting`` as ``alimiter (+ loudnorm true-peak
    limit)``. A second ``loudnorm`` pass would ALSO re-normalise loudness --
    which is the ``loudness_normalization`` stage's job and would undo the
    target when both are enabled -- so the true-peak ceiling is enforced as a
    MEASURED acceptance bound instead: ``alimiter`` sets it, ``ebur128`` measures
    whether it held, and the verdict is reported either way. ``measured`` is
    False whenever there is no measurement, which is never the same as "passed".
    """
    applied = [s for s in stages if s["stage"] == "limiting" and s["status"] == "applied"]
    ceiling = params.get("true_peak_limit_db")
    verdict: dict[str, Any] = {
        "requested": bool(applied),
        "ceiling_dbfs": _round(ceiling) if isinstance(ceiling, (int, float)) else None,
        "measured": False,
        "measured_true_peak_dbfs": None,
        "exceeded": False,
        "basis": "alimiter ceiling verified against the measured ebur128 true peak",
    }
    measured = (after or {}).get("true_peak_dbfs")
    if measured is not None:
        verdict["measured"] = True
        verdict["measured_true_peak_dbfs"] = measured
    if verdict["measured"] and isinstance(ceiling, (int, float)) and applied:
        verdict["exceeded"] = float(measured) > float(ceiling)
        verdict["headroom_db"] = round(float(ceiling) - float(measured), 3)
    return verdict


def _mime_for(path: Path) -> str:
    return {
        ".wav": "audio/wav",
        ".mp4": "video/mp4",
        ".m4a": "audio/mp4",
        ".mp3": "audio/mpeg",
        ".flac": "audio/flac",
        ".ogg": "audio/ogg",
    }.get(path.suffix.lower(), "application/octet-stream")


def _iso_now() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def enhance(
    db: Session,
    ws: Any,
    asset: MediaAsset,
    *,
    emit: EmitFn | None = None,
    **kwargs: Any,
) -> dict:
    """Module-level convenience wrapper around :class:`AudioEnhancementPipeline`.

    ``emit`` is the only engine-level option; everything else is a run option
    (``stages``, ``params``, ``force``, ``requested_by``, ``enqueue``).
    """
    return AudioEnhancementPipeline(db, emit=emit).run(ws, asset, **kwargs)


__all__ = [
    "ANALYSIS_STAGES",
    "CAPABILITY",
    "CONFIDENCE_HEURISTIC",
    "DERIVED_PREFIX",
    "DURATION_TOLERANCE_S",
    "EVENTS",
    "EVENT_ENHANCE_COMPLETED",
    "EVENT_ENHANCE_PARTIAL",
    "JOB_KIND",
    "RUN_KIND",
    "STAGES",
    "STAGE_STATUSES",
    "AudioEnhancementError",
    "AudioEnhancementPipeline",
    "asset_path",
    "before_after",
    "count_clipped_samples",
    "derived_filename",
    "detect_breath_click",
    "enhance",
    "measure_audio",
    "normalise_stages",
    "read_pcm16",
]

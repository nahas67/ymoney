"""Media-intelligence QC (Work 12 Lane H) -- contracts §13.

``AudioIntelligenceQC`` / ``VisualIntelligenceQC`` turn a run (and, for visuals,
its reframe plan) into one verdict out of
``PASS | PASS_WITH_WARNINGS | REVIEW_REQUIRED | FAIL`` plus the per-check
evidence behind it.

Three rules shape the whole module:

1. **Measure separately, judge separately.** Every check is a PURE function of
   already-measured inputs, so a verdict can be reproduced and unit-tested with
   no subprocess. The ffmpeg passes live in the measurement helpers at the top
   (:func:`frame_luma`, :func:`audio_measurements`) and are only executed when
   the run did not already record the measurement -- a run's ``metrics_json``
   (contracts §6 before/after metrics) is authoritative and is never re-derived.
   Every value a check did not measure is ``None`` -> the check is ``UNKNOWN``,
   never a plausible default.
2. **A verdict is a function of severities, not of vibes.** Each check declares
   a severity for its own violation:

   * ``HARD``   -- a measured hard violation  => ``FAIL``
   * ``REVIEW`` -- heuristic-only evidence, an ``UNRESOLVED`` dependency, or a
     policy override                             => ``REVIEW_REQUIRED``
   * ``WARN``   -- a minor issue (or an absent measurement) => ``PASS_WITH_WARNINGS``

   ``UNKNOWN`` is a warning by default: no evidence is not a pass. The only
   unknown that is NOT cosmetic is a missing input the check exists to police
   (a ``FAIL``-class check that could not run at all stays visible as a warning
   with a machine reason, never as a clean ``PASS``).
3. **Applying a FAIL needs an attributable human override.**
   :func:`assert_qc_allows_apply` raises :class:`QCApplyBlocked` (a
   ``ValueError`` subclass, so a route guard maps it to 409) unless the caller
   both passes ``override=True`` AND a recorded, attributable override exists
   for the current verdict. Overrides are APPEND-ONLY: a new
   ``intel_qc_results`` row copies the overridden verdict's checks and appends
   an ``qc_override`` evidence entry naming WHO overrode WHAT and WHY. The
   measured verdict is never rewritten by an override -- the audit trail keeps
   both facts.

Thresholds are NAMED module constants with a rationale in their docstring, so
no number is buried in the logic and a later lane can retune one value in one
place. Everything here is stdlib + sqlalchemy + the shared
``engine/intel/ffmpeg_util`` helpers: no new runtime dependency (contracts §0).
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.intel import ffmpeg_util as fu
from app.models.media_intel import (
    QC_KINDS,
    QC_VERDICTS,
    ActiveSpeakerMap,
    AudioTimeMap,
    DiarizationSegment,
    EditProposal,
    FaceTrack,
    FaceTrackSample,
    IntelQCResult,
    MaskAsset,
    MediaIntelRun,
    MediaIntelWord,
    ReframeKeyframe,
    ReframePlan,
)

logger = logging.getLogger("ymoney.intel")

# ---------------------------------------------------------------------------
# vocabularies
# ---------------------------------------------------------------------------

#: per-check status. ``UNKNOWN`` = the measurement the check needs is absent
#: (never a guessed pass); ``VIOLATION`` = the measured value broke the rule.
CHECK_STATUSES: tuple[str, ...] = ("OK", "WARN", "VIOLATION", "UNKNOWN")
#: what a violation of that check MEANS (see the module docstring).
CHECK_SEVERITIES: tuple[str, ...] = ("HARD", "REVIEW", "WARN")
SEVERITY_HARD = "HARD"
SEVERITY_REVIEW = "REVIEW"
SEVERITY_WARN = "WARN"

#: run kinds whose output is judged by the VISUAL checks; everything else is
#: audio (a reframe plan is visual even though it is not a rendered file).
VISUAL_RUN_KINDS: frozenset[str] = frozenset(
    {"face_tracking", "segmentation", "active_speaker", "reframe", "background"}
)

#: keyframe coordinate convention. ``x``/``y`` are the crop CENTRE in
#: normalised (0..1) source-frame coordinates and ``scale`` is a zoom factor
#: (``2.0`` == half the frame width). ``rect_json`` wins when it carries a
#: complete ``{x, y, w, h}`` in normalised units; its own ``anchor`` field may
#: say ``top_left`` instead. See :func:`crop_rect_at`.
KEYFRAME_ANCHOR = "center"
ANCHOR_VALUES: tuple[str, ...] = ("center", "top_left")

#: activity-feed kinds this lane emits. ``record_event`` opens its own session,
#: so the events are RETURNED in the payload and the ROUTE emits them after
#: ``db.commit()`` (contracts §3 emission-ordering rule, mirroring
#: ``engine/collab/reviews.py``). Report both to the orchestrator for the
#: ``services/webhooks.py::WEBHOOK_EVENTS`` allowlist.
EVENT_QC_COMPLETED = "MEDIA_INTEL_QC_COMPLETED"
EVENT_QC_OVERRIDDEN = "MEDIA_INTEL_QC_OVERRIDDEN"

#: the check-name an override row appends to its ``checks_json``.
OVERRIDE_CHECK_NAME = "qc_override"
#: keyframe sample rate assumption when a face track's own ``sample_count``
#: contradicts its span (contracts §8 default sample fps).
DEFAULT_SAMPLE_FPS = 2.0


class QCApplyBlocked(ValueError):
    """A FAIL (or unreviewed) verdict blocks an apply until overridden.

    Carries the machine-readable facts so a route can attach them to a 409 body
    without re-querying: ``verdict``, ``run_id``, ``result_id``, ``failures``
    (the failing check names) and ``needs_override``.
    """

    def __init__(
        self,
        message: str,
        *,
        verdict: str = "",
        run_id: str = "",
        result_id: str = "",
        kind: str = "",
        failures: Sequence[str] = (),
        needs_override: bool = True,
    ) -> None:
        super().__init__(message)
        self.verdict = str(verdict or "")
        self.run_id = str(run_id or "")
        self.result_id = str(result_id or "")
        self.kind = str(kind or "")
        self.failures = [str(f) for f in failures]
        self.needs_override = bool(needs_override)

    def as_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "run_id": self.run_id,
            "result_id": self.result_id,
            "kind": self.kind,
            "failures": list(self.failures),
            "needs_override": self.needs_override,
            "detail": _short(self),
        }


# ---------------------------------------------------------------------------
# thresholds (contracts §13) -- named, documented, one place each
# ---------------------------------------------------------------------------

# --- audio -----------------------------------------------------------------

#: Relative duration drift between the source and the derived output.
#: Rationale: container/encoder rounding moves a duration by a few tens of
#: milliseconds, and a cut timeline legitimately shortens the file -- 5 % of a
#: 10-minute source is 30 s of legitimate edit. Above that the media and the
#: edit map disagree far enough for captions to visibly desync.
DURATION_DRIFT_WARN_RATIO = 0.05
#: A fifth of the programme gone is not a rounding artefact: the output is a
#: different programme, so this is a hard violation.
DURATION_DRIFT_FAIL_RATIO = 0.20

#: A measured sample OR true peak at/above full scale IS clipping. The check
#: is ``>= 0.0`` on purpose: ffmpeg 8.1.1's ``astats`` reports 0.000265 dBFS for
#: a genuinely clipped 16-bit tone (rounding up through the float conversion),
#: so a strict ``> 0`` would miss the case it exists to catch.
CLIP_HARD_PEAK_DBFS = 0.0
#: Within 1 dB of full scale, any downstream re-encode will clip (lossy codecs
#: overshoot); warn before the damage happens.
CLIP_WARN_PEAK_DBFS = -1.0
#: A non-zero full-scale sample count is a hard violation on its own (ffmpeg
#: 8.1.1 does not print it, so this is driven by the run's recorded metrics).
CLIPPED_SAMPLE_VIOLATION = 0

#: ``ebur128`` reports its own digital-silence floor: any integrated loudness at
#: or below -70 LUFS is indistinguishable from a silent stream. Used by
#: ``missing_audio`` as the fast, conclusive path.
MISSING_AUDIO_LUFS_FLOOR = -70.0
#: Fraction of the file that must be below the speech floor before the output
#: counts as silent/empty. 0.98 tolerates a short head/tail pad and nothing
#: else: a real programme with 2 % of "silence" is still a programme.
MISSING_AUDIO_SILENCE_RATIO = 0.98
#: 90 % silent is not "missing audio" (there is a signal) but it is worth a
#: warning -- a QC pass must not hide a near-empty render behind a clean PASS.
MISSING_AUDIO_SILENCE_WARN_RATIO = 0.90
#: ``silencedetect`` minimum run for the missing-audio probe. 0.25 s (rather
#: than the helper's 0.8 s default) so a SHORT render of pure silence is still
#: detected instead of reporting no silence at all.
MISSING_AUDIO_SILENCE_MIN_S = 0.25
#: The speech floor shared with ``ffmpeg_util.detect_silence``'s default
#: (-50 dB), so "speech" means the same thing in QC and in the VAD adapter.
SPEECH_FLOOR_DBFS = -50.0

#: Share of speech activity an edit may remove before QC calls it excessive
#: (contracts §7 ``max_removal_ratio``). Over a third removed is no longer a
#: tidy-up, it is a re-edit of someone else's words. A lane may pass a stricter
#: ``max_removal_ratio`` from its policy; this is the ceiling QC enforces.
MAX_REMOVAL_RATIO = 0.35

#: Word-rate drift between the output and the source transcript. Natural
#: delivery is 120-180 wpm; a 25 % change is audible as missing or repeated
#: content, 60 % means the transcript no longer describes the audio.
TRANSCRIPT_RATE_DRIFT_WARN = 0.25
TRANSCRIPT_RATE_DRIFT_FAIL = 0.60
#: Share of the source words still present. Below 80 % the transcript drifted;
#: below 50 % half the words are gone and the mismatch is a hard violation.
TRANSCRIPT_WORD_COUNT_WARN = 0.80
TRANSCRIPT_WORD_COUNT_FAIL = 0.50

# --- visual ----------------------------------------------------------------

#: The primary subject must be inside the crop for at least this share of the
#: sampled frames. Brief dips are normal (a turn, a gesture, motion blur);
#: losing the subject for a fifth of the shot is not.
SUBJECT_COVERAGE_MIN = 0.80
#: Below 60 % the shot has effectively lost its subject: a hard violation.
SUBJECT_COVERAGE_HARD = 0.60

#: Crop movement clamp, in normalised frame widths per second (contracts §11
#: "max movement/sec clamp + smoothing; violations surface in QC"). 0.25 takes
#: 4 s to cross the frame -- a deliberate reframe, not a whip pan.
CROP_MOVEMENT_CLAMP_PER_SEC = 0.25
#: Movement under the clamp but above a tenth of a frame per second reads as
#: twitchy on a phone-sized 9:16 crop: a warning.
CROP_MOVEMENT_WARN_PER_SEC = 0.10
#: Jitter = mean |second difference| of the crop centre per second, i.e. how
#: fast the movement direction reverses. A smoothed plan measures ~0; 0.05/s^2
#: is a visible wobble, 0.15/s^2 is an unsmoothed keyframe ramp.
CROP_JITTER_WARN_PER_S2 = 0.05
CROP_JITTER_FAIL_PER_S2 = 0.15

#: A face track may vanish for this long (occlusion, a head turn) before the
#: subject counts as lost. 1.0 s at the 2 fps sample rate is two missed
#: samples -- anything longer is a real gap, not a sampled dropout.
FACE_GAP_WARN_S = 0.5
FACE_GAP_FAIL_S = 1.0
#: A track's own sample coverage vs its span. 90 % tolerates a couple of missed
#: detections at the track's edges; below that the "continuous" track is not.
FACE_COVERAGE_MIN = 0.90

#: Minimum mask area as a share of the frame. A talking head in a 9:16 crop is
#: typically 5-40 % of the frame, so 1 % is a degenerate mask (a speck), not a
#: subject mask.
MASK_MIN_AREA_RATIO = 0.01
#: A mask thinner/shorter than this cannot be composited without artefacts.
MASK_MIN_DIMENSION = 8
#: The only two real mask containers (contracts §9). Anything else is a format
#: failure, never a silently-accepted file.
MASK_FORMATS: tuple[str, ...] = ("PNG", "RLE_JSON")

#: Black-frame predicate, evaluated per sampled frame. Both parts must hold:
#: the MEAN must be below 4/255 (measured: ``black_mp4`` = 0.0, a real lit
#: frame with a 48x48 white box on 320x180 = 10.2) AND the BRIGHTEST pixel must
#: stay below 32/255, so a dark-but-present frame is never called black.
BLACK_FRAME_MEAN_LUMA = 4.0
BLACK_FRAME_MAX_LUMA = 32.0
#: Share of sampled frames that must be black. 1 % is a suspicious dip (a fade);
#: 5 % of a clip being black is a broken render.
BLACK_FRAME_RATIO_WARN = 0.01
BLACK_FRAME_RATIO_FAIL = 0.05

#: sampled frames per second for the black-frame probe (bounded work: a full
#: decode of a long master would be a request-time hazard, not a QC check).
LUMA_SAMPLE_FPS = 4.0
#: hard ceiling on sampled frames, matching the fixtures' per-frame ceiling
LUMA_MAX_FRAMES = 64


# ---------------------------------------------------------------------------
# the check value object
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QCCheck:
    """One check's name, measurement, threshold, verdict and human reason.

    ``measured`` is what was actually measured (``None`` when the measurement
    was unavailable -- never a placeholder), ``threshold`` the limit it was
    compared against and ``comparator`` the rule in words, so a UI can render
    "clipped_samples 35680 > 0" without re-deriving the comparison.
    """

    name: str
    status: str
    severity: str = SEVERITY_WARN
    measured: float | int | None = None
    threshold: float | int | None = None
    comparator: str = ""
    reason: str = ""
    method: str = ""
    evidence: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "OK"

    @property
    def violated(self) -> bool:
        return self.status == "VIOLATION"

    def as_dict(self) -> dict:
        return {
            "name": str(self.name),
            "ok": self.ok,
            "verdict": str(self.status),
            "severity": str(self.severity),
            "measured": self.measured,
            "threshold": self.threshold,
            "comparator": str(self.comparator),
            "reason": str(self.reason),
            "method": str(self.method),
            "evidence": dict(self.evidence or {}),
        }


def _short(exc: BaseException, limit: int = 180) -> str:
    """One-line, truncated message for a deliberate domain error."""
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:limit]


def _ok(name: str, reason: str, *, measured=None, threshold=None, comparator="",
        method: str = "", evidence: dict | None = None,
        severity: str = SEVERITY_WARN) -> QCCheck:
    return QCCheck(name=name, status="OK", severity=severity, measured=measured,
                   threshold=threshold, comparator=comparator, reason=reason,
                   method=method, evidence=dict(evidence or {}))


def _violation(name: str, reason: str, *, severity: str, measured=None, threshold=None,
               comparator="", method: str = "", evidence: dict | None = None) -> QCCheck:
    return QCCheck(name=name, status="VIOLATION", severity=severity, measured=measured,
                   threshold=threshold, comparator=comparator, reason=reason,
                   method=method, evidence=dict(evidence or {}))


def _warn(name: str, reason: str, *, measured=None, threshold=None, comparator="",
          method: str = "", evidence: dict | None = None) -> QCCheck:
    return QCCheck(name=name, status="WARN", severity=SEVERITY_WARN, measured=measured,
                   threshold=threshold, comparator=comparator, reason=reason,
                   method=method, evidence=dict(evidence or {}))


def _unknown(name: str, reason: str, *, measured=None, threshold=None, comparator="",
             method: str = "", evidence: dict | None = None,
             severity: str = SEVERITY_WARN) -> QCCheck:
    return QCCheck(name=name, status="UNKNOWN", severity=severity, measured=measured,
                   threshold=threshold, comparator=comparator, reason=reason,
                   method=method, evidence=dict(evidence or {}))


def _num(value: Any) -> float | None:
    """Coerce to a finite float, or None (never a plausible default)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out or out in (float("inf"), float("-inf")):
        return None
    return out


def _round(value: float | None, digits: int = 4) -> float | None:
    return None if value is None else round(float(value), digits)


# ---------------------------------------------------------------------------
# measurement helpers (real ffmpeg, through the shared util only)
# ---------------------------------------------------------------------------


def asset_media_path(db: Session, workspace_id: str, asset_id: str | None) -> str | None:
    """Filesystem path of a workspace asset, or None when unresolvable.

    Fails closed. The storage key goes through
    ``services.storage.managed_path`` (which refuses absolute keys outside the
    workspace, ``..`` escapes, ``mock:`` references and any foreign
    directory), and a foreign or missing asset is ``None`` -- never another
    workspace's file.

    Both key conventions the repo actually writes are tried, EACH through
    ``managed_path`` (so the security gate stays the single one): the
    CWD-relative ``data/videos/<ws>/<file>`` that ``services.storage`` and
    ``providers/motion.py`` write, and the workspace-relative ``<file>`` that
    ``providers/longform_assets.py`` writes.
    """
    if not asset_id or not workspace_id:
        return None
    from app.models import MediaAsset
    from app.services import storage as storage_service

    row = db.get(MediaAsset, str(asset_id))
    if row is None or str(row.workspace_id) != str(workspace_id):
        return None
    key = str(row.storage_key or "")
    candidates = [key, str(Path(storage_service.STORAGE_ROOT) / str(workspace_id) / key)]
    for candidate in candidates:
        resolved = storage_service.managed_path(str(workspace_id), candidate)
        if resolved is not None and resolved.exists() and resolved.is_file():
            return str(resolved)
    return None


def _flatten_metrics(metrics: dict | None) -> dict:
    """Flatten a run manifest's metrics into ``source_*`` / ``output_*`` keys.

    Lane C writes before/after measurements under contracts §6; a QC pass must
    read them rather than re-measure. Both shapes are accepted: the nested
    ``{"source": {...}, "output": {...}}`` form and the flat
    ``{"output_peak_dbfs": ...}`` form. The flat key wins when both exist.
    """
    flat: dict[str, Any] = {}
    raw = dict(metrics or {})
    for side in ("source", "input", "output", "result"):
        block = raw.get(side)
        if isinstance(block, dict):
            prefix = "source" if side in ("source", "input") else "output"
            for key, value in block.items():
                flat.setdefault(f"{prefix}_{key}", value)
    for key, value in raw.items():
        if isinstance(value, dict):
            continue
        flat[str(key)] = value
    return flat


def _recorded(metrics: dict, *keys: str) -> Any:
    """First non-None recorded value for ``keys`` (the run already measured it)."""
    for key in keys:
        value = metrics.get(key)
        if value is not None:
            return value
    return None


def audio_measurements(
    path: str | Path | None, *, need: Sequence[str] | None = None
) -> dict:
    """Measure one audio file with the SHARED helpers (contracts §6 metrics).

    ``ffmpeg_util`` does the ffmpeg invocation -- this function only decides
    what to ask for: the stream layout from ``probe``, the sample peak + sample
    count from ``measure_peaks`` (astats), EBU R128 from ``measure_loudness``
    (ebur128) and the silent spans from ``detect_silence`` (silencedetect). Every
    field the invocation did not produce is ``None`` -- never estimated.

    ``need`` limits the passes to the measurements a caller is actually missing
    (``probe`` | ``peaks`` | ``loudness`` | ``silence``). ``None`` (the default)
    means "measure everything"; an EMPTY sequence means "measure nothing" -- a
    run that already recorded its before/after metrics must never pay for a pass
    whose result would be discarded.
    """
    out: dict[str, Any] = {
        "duration_s": None,
        "has_audio_stream": None,
        "streams": 0,
        "peak_dbfs": None,
        "true_peak_dbfs": None,
        "integrated_lufs": None,
        "samples": None,
        "clipped_samples": None,
        "silence_s": 0.0,
        "silence_ratio": None,
        "passes": [],
    }
    wanted = ({str(name) for name in need} if need is not None
              else {"probe", "peaks", "loudness", "silence"})
    if not path or not wanted:
        return out
    if "probe" in wanted:
        out["passes"].append("probe")
        data = fu.probe(path)
        streams = [s for s in (data.get("streams") or []) if isinstance(s, dict)]
        out["has_audio_stream"] = bool([s for s in streams if s.get("codec_type") == "audio"])
        out["streams"] = len(streams)
        out["duration_s"] = fu.duration_seconds(path)
    if "peaks" in wanted:
        out["passes"].append("peaks")
        peaks = fu.measure_peaks(path)
        out["peak_dbfs"] = peaks.get("peak_dbfs")
        out["samples"] = peaks.get("samples")
        out["clipped_samples"] = peaks.get("clipped_samples")
    if "loudness" in wanted:
        out["passes"].append("loudness")
        loudness = fu.measure_loudness(path)
        out["true_peak_dbfs"] = loudness.get("true_peak_dbfs")
        out["integrated_lufs"] = loudness.get("integrated_lufs")
    if "silence" in wanted:
        lufs = _num(out["integrated_lufs"])
        if lufs is None or lufs > MISSING_AUDIO_LUFS_FLOOR:
            out["passes"].append("silence")
            spans = fu.detect_silence(
                path, noise_db=SPEECH_FLOOR_DBFS, min_duration=MISSING_AUDIO_SILENCE_MIN_S
            )
            silent = sum(max(0.0, float(s.get("duration_s") or 0.0)) for s in spans)
            out["silence_s"] = round(silent, 4)
            if out["duration_s"]:
                out["silence_ratio"] = round(silent / float(out["duration_s"]), 4)
        elif out["duration_s"]:
            out["silence_ratio"] = 1.0
    return out


def frame_luma(
    path: str | Path | None,
    *,
    fps: float = LUMA_SAMPLE_FPS,
    max_frames: int = LUMA_MAX_FRAMES,
) -> list[dict]:
    """Per-sampled-frame luma statistics -- the REAL black-frame measurement.

    ``ffmpeg_util.run_filter`` decodes the source to a raw 8-bit gray stream in
    a NEW temp file::

        ffmpeg -y -i <src> -vf fps=<fps> -pix_fmt gray -f rawvideo <tmp>/frames.raw

    (``-pix_fmt gray -f rawvideo` is the same raw-gray contract
    ``ffmpeg_util.frame_difference_energy`` decodes; the bytes are then reduced
    with ``sum``/``max``, which is the only frame reading the stdlib can do
    without an image library -- hence no new dependency, contracts §0.)

    Returns ``[{"index", "t_s", "mean_luma", "max_luma"}, ...]`` or ``[]`` when
    the file is unreadable, has no video stream, or ffmpeg is unavailable. The
    source media is only ever read.
    """
    if not path:
        return []
    data = fu.probe(path)
    stream = next(
        (s for s in (data.get("streams") or [])
         if isinstance(s, dict) and s.get("codec_type") == "video"),
        None,
    )
    if not stream:
        return []
    width, height = stream.get("width"), stream.get("height")
    try:
        frame_bytes = int(width or 0) * int(height or 0)
    except (TypeError, ValueError):
        return []
    if frame_bytes <= 0:
        return []
    try:
        rate = float(fps)
    except (TypeError, ValueError):
        return []
    if rate <= 0:
        return []
    with tempfile.TemporaryDirectory(prefix="ymoney-qc-luma-") as tmp:
        raw_path = Path(tmp) / "frames.raw"
        result = fu.run_filter(
            path, raw_path, f"fps={rate:g}",
            extra_args=["-pix_fmt", "gray", "-f", "rawvideo"], video=True,
        )
        if not result.get("ok") or not raw_path.exists():
            logger.warning("frame_luma could not decode %s", path)
            return []
        try:
            data_bytes = raw_path.read_bytes()
        except OSError as exc:  # pragma: no cover - unreadable temp file
            logger.warning("frame_luma read failed: %s", type(exc).__name__)
            return []
    count = min(len(data_bytes) // frame_bytes, max(1, int(max_frames)))
    frames: list[dict] = []
    for index in range(count):
        frame = data_bytes[index * frame_bytes:(index + 1) * frame_bytes]
        frames.append({
            "index": index,
            "t_s": round(index / rate, 4),
            "mean_luma": round(sum(frame) / frame_bytes, 3),
            "max_luma": max(frame),
        })
    return frames


def _mask_file_checksum(path: str | Path) -> str:
    """sha256 of a mask FILE, or "" when it cannot be read."""
    try:
        digest = hashlib.sha256()
        with open(path, "rb") as handle:
            for chunk in iter(lambda: handle.read(65536), b""):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError as exc:
        logger.warning("mask checksum failed for %s: %s", path, type(exc).__name__)
        return ""


# ---------------------------------------------------------------------------
# audio checks (pure: measured inputs in, one QCCheck out)
# ---------------------------------------------------------------------------


def check_duration_drift(
    source_s: float | None,
    output_s: float | None,
    *,
    warn_ratio: float = DURATION_DRIFT_WARN_RATIO,
    fail_ratio: float = DURATION_DRIFT_FAIL_RATIO,
) -> QCCheck:
    """Relative drift between the source duration and the derived output.

    A derived file is EXPECTED to be shorter (that is what an edit is), so the
    comparison is relative and one-sided in spirit: the ratios are symmetric
    (|out - src| / src) because a LONGER output is just as wrong as a much
    shorter one. Without both durations there is nothing to compare and the
    check is ``UNKNOWN`` -- never a clean PASS on an unmeasured edit.
    """
    src, out = _num(source_s), _num(output_s)
    if src is None or out is None or src <= 0:
        return _unknown(
            "duration_drift",
            "source or output duration unknown -- drift was not measured",
            threshold=fail_ratio,
            comparator=f"|out - src| / src > {fail_ratio}",
            method="ffprobe",
            evidence={"source_duration_s": src, "output_duration_s": out},
        )
    drift = abs(out - src) / src
    measured = _round(drift)
    evidence = {
        "source_duration_s": _round(src, 3),
        "output_duration_s": _round(out, 3),
        "delta_s": _round(out - src, 3),
    }
    if drift > fail_ratio:
        return _violation(
            "duration_drift",
            f"output is {drift * 100:.1f}% longer/shorter than the source "
            f"({out:.2f}s vs {src:.2f}s)",
            severity=SEVERITY_HARD, measured=measured, threshold=fail_ratio,
            comparator=f"|out - src| / src > {fail_ratio}",
            method="ffprobe", evidence=evidence,
        )
    if drift > warn_ratio:
        return _warn(
            "duration_drift",
            f"output drifted {drift * 100:.1f}% from the source "
            f"({out:.2f}s vs {src:.2f}s) -- captions may need re-timing",
            measured=measured, threshold=warn_ratio,
            comparator=f"|out - src| / src > {warn_ratio}",
            method="ffprobe", evidence=evidence,
        )
    return _ok(
        "duration_drift", f"output duration within {drift * 100:.1f}% of the source",
        measured=measured, threshold=warn_ratio,
        comparator=f"|out - src| / src > {warn_ratio}",
        method="ffprobe", evidence=evidence,
    )


def check_clipping(
    *,
    peak_dbfs: float | None = None,
    true_peak_dbfs: float | None = None,
    clipped_samples: int | None = None,
    warn_peak_dbfs: float = CLIP_WARN_PEAK_DBFS,
    hard_peak_dbfs: float = CLIP_HARD_PEAK_DBFS,
) -> QCCheck:
    """Clipping = a peak at/over full scale, or a non-zero full-scale count.

    Both the sample peak (``astats``) and the inter-sample true peak
    (``ebur128``) count: lossy delivery clips on the true peak even when the
    samples do not reach 0 dBFS. ffmpeg 8.1.1 does not print a clipped-sample
    count, so ``clipped_samples`` is ``None`` unless a lane computed it (the
    measured 35 680 full-scale samples of a clipped 2 s tone then drives the
    violation through the run's metrics).
    """
    sample_peak = _num(peak_dbfs)
    true_peak = _num(true_peak_dbfs)
    worst = max((p for p in (sample_peak, true_peak) if p is not None), default=None)
    try:
        clipped = int(clipped_samples) if clipped_samples is not None else None
    except (TypeError, ValueError):
        clipped = None
    evidence = {
        "peak_dbfs": sample_peak,
        "true_peak_dbfs": true_peak,
        "clipped_samples": clipped,
    }
    comparator = f"peak >= {hard_peak_dbfs} dBFS or clipped_samples > {CLIPPED_SAMPLE_VIOLATION}"
    if worst is None and clipped is None:
        return _unknown(
            "clipping", "no peak measurement available -- clipping was not measured",
            threshold=hard_peak_dbfs, comparator=comparator,
            method="astats+ebur128", evidence=evidence,
        )
    if (worst is not None and worst >= hard_peak_dbfs) or (
        clipped is not None and clipped > CLIPPED_SAMPLE_VIOLATION
    ):
        parts = []
        if worst is not None and worst >= hard_peak_dbfs:
            parts.append(f"peak {worst:.3f} dBFS (>= {hard_peak_dbfs})")
        if clipped:
            parts.append(f"{clipped} full-scale sample(s)")
        return _violation(
            "clipping", "clipping: " + " and ".join(parts),
            severity=SEVERITY_HARD, measured=_round(worst if worst is not None else clipped, 4),
            threshold=hard_peak_dbfs, comparator=comparator,
            method="astats+ebur128", evidence=evidence,
        )
    if worst is not None and worst > warn_peak_dbfs:
        return _warn(
            "clipping",
            f"peak {worst:.2f} dBFS is within {abs(warn_peak_dbfs):.0f} dB of full "
            "scale -- a re-encode will clip",
            measured=_round(worst), threshold=warn_peak_dbfs,
            comparator=f"peak > {warn_peak_dbfs} dBFS",
            method="astats+ebur128", evidence=evidence,
        )
    return _ok(
        "clipping", f"peak {_round(worst) if worst is not None else 'n/a'} dBFS, "
        "no full-scale samples",
        measured=_round(worst), threshold=hard_peak_dbfs,
        comparator=comparator, method="astats+ebur128", evidence=evidence,
    )


def check_missing_audio(
    *,
    has_audio_stream: bool | None = None,
    peak_dbfs: float | None = None,
    integrated_lufs: float | None = None,
    samples: int | None = None,
    silence_ratio: float | None = None,
    duration_s: float | None = None,
    hard_ratio: float = MISSING_AUDIO_SILENCE_RATIO,
    warn_ratio: float = MISSING_AUDIO_SILENCE_WARN_RATIO,
) -> QCCheck:
    """No audio stream, an empty stream, or a stream that is digital silence.

    Two independent signals, either of which is conclusive:
    ``integrated_lufs <= -70`` (the EBU R128 measurement floor, so the content
    is indistinguishable from silence) or silence covering the whole file per
    ``silencedetect`` at the shared -50 dB speech floor. A near-empty file
    (mostly silent) is a WARNING, not a hard failure: there IS a signal.
    """
    lufs = _num(integrated_lufs)
    ratio = _num(silence_ratio)
    duration = _num(duration_s)
    try:
        sample_count = int(samples) if samples is not None else None
    except (TypeError, ValueError):
        sample_count = None
    evidence = {
        "has_audio_stream": has_audio_stream,
        "integrated_lufs": lufs,
        "peak_dbfs": _num(peak_dbfs),
        "samples": sample_count,
        "silence_ratio": ratio,
        "duration_s": duration,
    }
    comparator = (f"integrated_lufs <= {MISSING_AUDIO_LUFS_FLOOR} or "
                  f"silence_ratio > {hard_ratio}")
    if has_audio_stream is False:
        return _violation(
            "missing_audio", "output has no audio stream at all",
            severity=SEVERITY_HARD, measured=0, threshold=1,
            comparator="audio stream count >= 1",
            method="ffprobe", evidence=evidence,
        )
    if sample_count is not None and sample_count == 0:
        return _violation(
            "missing_audio", "audio stream is empty (0 samples)",
            severity=SEVERITY_HARD, measured=0, threshold=1,
            comparator="samples > 0", method="astats", evidence=evidence,
        )
    if lufs is not None and lufs <= MISSING_AUDIO_LUFS_FLOOR:
        return _violation(
            "missing_audio",
            f"integrated loudness {lufs:.1f} LUFS is at the EBU R128 digital-silence floor",
            severity=SEVERITY_HARD, measured=_round(lufs, 2),
            threshold=MISSING_AUDIO_LUFS_FLOOR,
            comparator=f"integrated_lufs <= {MISSING_AUDIO_LUFS_FLOOR}",
            method="ebur128", evidence=evidence,
        )
    if ratio is not None and ratio > hard_ratio:
        return _violation(
            "missing_audio",
            f"{ratio * 100:.1f}% of the output is below the -50 dB speech floor -- "
            "the audio is effectively silent",
            severity=SEVERITY_HARD, measured=ratio, threshold=hard_ratio,
            comparator=f"silence_ratio > {hard_ratio}",
            method="silencedetect", evidence=evidence,
        )
    if ratio is not None and ratio > warn_ratio:
        return _warn(
            "missing_audio",
            f"{ratio * 100:.1f}% of the output is silent -- check the edit",
            measured=ratio, threshold=warn_ratio,
            comparator=f"silence_ratio > {warn_ratio}",
            method="silencedetect", evidence=evidence,
        )
    if has_audio_stream is None and lufs is None and ratio is None:
        return _unknown(
            "missing_audio", "no audio stream/level measurement available",
            threshold=hard_ratio, comparator=comparator,
            method="ffprobe+ebur128+silencedetect", evidence=evidence,
        )
    return _ok(
        "missing_audio", "audio stream present and above the silence floor",
        measured=ratio if ratio is not None else lufs, threshold=hard_ratio,
        comparator=comparator, method="ffprobe+ebur128+silencedetect", evidence=evidence,
    )


def check_excessive_removed_speech(
    removed_s: float | None,
    speech_activity_s: float | None,
    *,
    source_duration_s: float | None = None,
    max_removal_ratio: float | None = None,
) -> QCCheck:
    """Removed duration as a ratio of SPEECH activity (contracts §7).

    The denominator is speech activity, not total duration: cutting 40 % of a
    10-minute file that is 20 % speech removes 200 % of the speech, while
    cutting the same 40 % out of a podcast with 90 % speech is routine. When
    speech activity is genuinely unknown the check still runs, but on the
    whole-duration denominator and therefore downgrades to ``REVIEW``
    severity -- heuristic-only evidence never claims a hard failure.

    Over the limit the check reports ``evidence["force_keep"] = True``: the
    policy MUST be forced to keep (contracts §7) and the verdict is FAIL.
    """
    limit = float(max_removal_ratio) if max_removal_ratio is not None else MAX_REMOVAL_RATIO
    removed = max(0.0, _num(removed_s) or 0.0)
    speech = _num(speech_activity_s)
    total = _num(source_duration_s)
    denominator = speech if (speech and speech > 0) else total
    comparator = f"removed_s / speech_activity_s > {limit}"
    if removed <= 0:
        return _ok(
            "excessive_removed_speech", "nothing was removed from the source audio",
            measured=0.0, threshold=limit, comparator=comparator,
            method="edit_proposals+audio_time_maps",
            evidence={"removed_s": 0.0, "speech_activity_s": speech, "denominator": "none"},
        )
    if not denominator or denominator <= 0:
        return _unknown(
            "excessive_removed_speech",
            f"{removed:.2f}s removed but no speech-activity or duration baseline to "
            "compare against -- the removal ratio was not measured",
            measured=_round(removed, 3), threshold=limit, comparator=comparator,
            method="edit_proposals+audio_time_maps",
            evidence={"removed_s": _round(removed, 3), "speech_activity_s": speech,
                      "source_duration_s": total},
        )
    ratio = removed / denominator
    evidence = {
        "removed_s": _round(removed, 3),
        "speech_activity_s": _round(speech, 3) if speech else None,
        "source_duration_s": _round(total, 3) if total else None,
        "denominator": "speech_activity" if (speech and speech > 0) else "source_duration",
        "max_removal_ratio": limit,
    }
    if ratio > limit:
        evidence["force_keep"] = True
        known = bool(speech and speech > 0)
        return _violation(
            "excessive_removed_speech",
            f"{ratio * 100:.1f}% of the "
            f"{'speech activity' if known else 'source duration'} was removed "
            f"({removed:.2f}s of {denominator:.2f}s) over the {limit * 100:.0f}% limit "
            "-- the policy must keep these ranges",
            severity=SEVERITY_HARD if known else SEVERITY_REVIEW,
            measured=_round(ratio), threshold=limit,
            comparator=comparator if known else f"removed_s / source_duration_s > {limit}",
            method="edit_proposals+audio_time_maps", evidence=evidence,
        )
    return _ok(
        "excessive_removed_speech",
        f"{ratio * 100:.1f}% of the "
        f"{'speech activity' if speech and speech > 0 else 'source duration'} removed "
        f"({removed:.2f}s) within the {limit * 100:.0f}% limit",
        measured=_round(ratio), threshold=limit, comparator=comparator,
        method="edit_proposals+audio_time_maps", evidence=evidence,
    )


def check_transcript_mismatch(
    *,
    source_words: int | None = None,
    output_words: int | None = None,
    source_duration_s: float | None = None,
    output_duration_s: float | None = None,
    rate_drift_warn: float = TRANSCRIPT_RATE_DRIFT_WARN,
    rate_drift_fail: float = TRANSCRIPT_RATE_DRIFT_FAIL,
    count_warn: float = TRANSCRIPT_WORD_COUNT_WARN,
    count_fail: float = TRANSCRIPT_WORD_COUNT_FAIL,
) -> QCCheck:
    """Word-count / word-rate drift of the output against the SOURCE transcript.

    Both signals are reported: the retained share of the source words, and the
    change in speaking rate. Either can break the check. A word rate can only
    be computed with both durations, so a run with no duration baseline still
    gets the word-count half rather than nothing.
    """
    try:
        src_words = int(source_words) if source_words is not None else None
        out_words = int(output_words) if output_words is not None else None
    except (TypeError, ValueError):
        src_words = out_words = None
    src_dur, out_dur = _num(source_duration_s), _num(output_duration_s)
    evidence: dict[str, Any] = {
        "source_words": src_words, "output_words": out_words,
        "source_duration_s": src_dur, "output_duration_s": out_dur,
    }
    comparator = (f"word_count_ratio < {count_fail} or |word_rate_drift| > {rate_drift_fail}")
    if not src_words or src_words <= 0 or out_words is None:
        return _unknown(
            "transcript_mismatch",
            "no comparable source transcript -- word drift was not measured",
            threshold=count_fail, comparator=comparator,
            method="media_intel_words", evidence=evidence,
        )
    count_ratio = max(0.0, out_words / src_words)
    evidence["word_count_ratio"] = _round(count_ratio)
    source_rate = out_rate = None
    if src_dur and out_dur and src_dur > 0 and out_dur > 0:
        source_rate = src_words / src_dur
        out_rate = out_words / out_dur
        evidence["source_word_rate"] = _round(source_rate, 3)
        evidence["output_word_rate"] = _round(out_rate, 3)
    rate_drift = abs(out_rate - source_rate) / source_rate if source_rate and out_rate else None
    evidence["word_rate_drift"] = _round(rate_drift)
    if count_ratio < count_fail:
        return _violation(
            "transcript_mismatch",
            f"only {count_ratio * 100:.0f}% of the source words remain "
            f"({out_words} of {src_words})",
            severity=SEVERITY_HARD, measured=_round(count_ratio), threshold=count_fail,
            comparator=f"word_count_ratio < {count_fail}",
            method="media_intel_words", evidence=evidence,
        )
    if rate_drift is not None and rate_drift > rate_drift_fail:
        return _violation(
            "transcript_mismatch",
            f"speaking rate drifted {rate_drift * 100:.0f}% "
            f"({_round(source_rate, 2)} -> {_round(out_rate, 2)} words/s)",
            severity=SEVERITY_HARD, measured=_round(rate_drift), threshold=rate_drift_fail,
            comparator=f"|word_rate_drift| > {rate_drift_fail}",
            method="media_intel_words", evidence=evidence,
        )
    if count_ratio < count_warn:
        return _warn(
            "transcript_mismatch",
            f"{count_ratio * 100:.0f}% of the source words remain "
            f"({out_words} of {src_words}) -- a filler/silence pass was aggressive",
            measured=_round(count_ratio), threshold=count_warn,
            comparator=f"word_count_ratio < {count_warn}",
            method="media_intel_words", evidence=evidence,
        )
    if rate_drift is not None and rate_drift > rate_drift_warn:
        return _warn(
            "transcript_mismatch",
            f"speaking rate drifted {rate_drift * 100:.0f}% against the source transcript",
            measured=_round(rate_drift), threshold=rate_drift_warn,
            comparator=f"|word_rate_drift| > {rate_drift_warn}",
            method="media_intel_words", evidence=evidence,
        )
    return _ok(
        "transcript_mismatch",
        f"{out_words}/{src_words} words retained (rate drift "
        f"{_round(rate_drift) if rate_drift is not None else 'n/a'})",
        measured=_round(count_ratio), threshold=count_warn,
        comparator=comparator, method="media_intel_words", evidence=evidence,
    )


# ---------------------------------------------------------------------------
# visual checks
# ---------------------------------------------------------------------------


def _aspect_ratio(aspect: str) -> float:
    """``"9:16"`` -> 9/16. Unparseable input falls back to 16/9 (documented)."""
    try:
        width, _, height = str(aspect or "").partition(":")
        if float(height) > 0:
            return float(width) / float(height)
    except (TypeError, ValueError):
        pass
    return 16.0 / 9.0


def crop_rect_at(
    keyframe: Any,
    *,
    target_aspect: str = "9:16",
    source_aspect: str = "16:9",
    anchor: str = KEYFRAME_ANCHOR,
) -> dict | None:
    """Normalised ``{x0, y0, x1, y1}`` crop rect in effect at one keyframe.

    ``rect_json`` wins when it carries a complete normalised ``{x, y, w, h}``
    (its own ``anchor`` field may override the module default). Otherwise the
    rect is derived from the ``x``/``y``/``scale`` columns: ``x``/``y`` are the
    crop CENTRE in normalised source coordinates and ``scale`` is a zoom factor,
    so ``w = 1/scale`` and
    ``h = w * (target_w/target_h) * (src_w/src_h)`` -- which is what keeps a
    9:16 crop from a 16:9 source square in PIXELS (``h == w == 0.5625`` for a
    full-height 16:9 -> 9:16 crop) instead of stretching it.

    Returns ``None`` when the keyframe carries no usable geometry.
    """
    rect = getattr(keyframe, "rect_json", None)
    if isinstance(rect, dict) and all(
        _num(rect.get(k)) is not None for k in ("x", "y", "w", "h")
    ):
        x, y = float(rect["x"]), float(rect["y"])
        w, h = abs(float(rect["w"])), abs(float(rect["h"]))
        if w <= 0 or h <= 0:
            return None
        point = str(rect.get("anchor") or anchor or KEYFRAME_ANCHOR)
        if point == "top_left":
            return {"x0": x, "y0": y, "x1": x + w, "y1": y + h}
        return {"x0": x - w / 2, "y0": y - h / 2, "x1": x + w / 2, "y1": y + h / 2}
    cx, cy = _num(getattr(keyframe, "x", None)), _num(getattr(keyframe, "y", None))
    scale = _num(getattr(keyframe, "scale", None)) or 1.0
    if cx is None or cy is None or scale <= 0:
        return None
    width = min(1.0, 1.0 / scale)
    # Normalised geometry: w is a fraction of source WIDTH, h a fraction of
    # source HEIGHT, so h = w * (src_w/src_h) / (target_w/target_h). The old
    # chain used (target_w/target_h) and collapsed h to w -- a square in
    # normalised space, which is only right for a 1:1 target. When the derived
    # height would exceed the frame we fit INSIDE (shrinking the width too) so
    # the rect keeps the target aspect: a 16:9 -> 9:16 crop at scale 2 is
    # 0.3164 x 1.0 (101x180 px), not 0.5 x 1.0 (160x180 px = 8:9).
    height = width * _aspect_ratio(source_aspect) / _aspect_ratio(target_aspect)
    if height > 1.0:
        # The crop would be taller than the frame: clamp to full height and
        # leave `width = 1/scale` authoritative, so scale=1.0 still means the
        # whole frame. Lane G writes a COMPLETE rect_json (the preferred
        # branch) with exact per-aspect geometry, so this fallback only has to
        # be sane, not aspect-exact, when the zoom is wide.
        height = 1.0
    point = str(anchor or KEYFRAME_ANCHOR)
    if point == "top_left":
        return {"x0": cx, "y0": cy, "x1": cx + width, "y1": cy + height}
    return {
        "x0": cx - width / 2, "y0": cy - height / 2,
        "x1": cx + width / 2, "y1": cy + height / 2,
    }


def crop_motion(
    keyframes: Sequence[Any],
    *,
    target_aspect: str = "9:16",
    source_aspect: str = "16:9",
    anchor: str = KEYFRAME_ANCHOR,
) -> dict:
    """Per-second crop movement + jitter over a keyframe plan (contracts §11).

    ``keyframes`` may arrive in any order (the caller sorts). For each
    consecutive pair the crop CENTRE is compared, so ``speeds`` are normalised
    frame-widths per second. ``jitter`` is the crop's ACCELERATION -- the
    absolute second difference of the centre, ``|c[i+1] - 2c[i] + c[i-1]| / h^2``
    with ``h`` the mean interval -- because a constant-MAGNITUDE oscillation
    (back and forth at the same speed) has zero speed change yet is exactly the
    wobble a viewer sees, and a smooth ramp has zero acceleration. A speed-only
    metric cannot tell those two apart. Duplicate timestamps are skipped (a
    division by zero is not a movement).
    """
    rects: list[tuple[float, float]] = []
    for keyframe in sorted(keyframes or [], key=lambda k: _num(getattr(k, "t_s", 0)) or 0.0):
        t_s = _num(getattr(keyframe, "t_s", None))
        rect = crop_rect_at(keyframe, target_aspect=target_aspect,
                            source_aspect=source_aspect, anchor=anchor)
        if t_s is None or not rect:
            continue
        rects.append((t_s, (rect["x0"] + rect["x1"]) / 2.0))
    speeds: list[dict] = []
    for (t0, c0), (t1, c1) in zip(rects, rects[1:], strict=False):
        span = t1 - t0
        if span <= 0:
            continue
        speeds.append({
            "from_s": round(t0, 4), "to_s": round(t1, 4),
            "speed_per_s": round(abs(c1 - c0) / span, 6),
        })
    jitters: list[float] = []
    for (t_prev, c_prev), (t_mid, c_mid), (t_next, c_next) in zip(
        rects, rects[1:], rects[2:], strict=False
    ):
        step = (t_next - t_prev) / 2.0
        if step <= 0:
            continue
        jitters.append(abs(c_next - 2.0 * c_mid + c_prev) / (step * step))
    values = [s["speed_per_s"] for s in speeds]
    return {
        "keyframes": len(rects),
        "speeds": speeds,
        "max_speed_per_s": _round(max(values), 6) if values else None,
        "mean_speed_per_s": _round(sum(values) / len(values), 6) if values else None,
        "max_jitter_per_s2": _round(max(jitters), 6) if jitters else None,
        "mean_jitter_per_s2": _round(sum(jitters) / len(jitters), 6) if jitters else None,
    }


def check_excessive_crop_movement(
    motion: dict | None, *, clamp_per_s: float = CROP_MOVEMENT_CLAMP_PER_SEC,
    warn_per_s: float = CROP_MOVEMENT_WARN_PER_SEC,
) -> QCCheck:
    """Crop speed vs the plan's own per-second clamp (contracts §11).

    Exceeding the clamp is a hard violation because it means the plan violates
    the contract it was built to; staying under it but moving fast is a
    warning (a phone-sized 9:16 crop magnifies any movement).
    """
    max_speed = _num((motion or {}).get("max_speed_per_s"))
    comparator = f"max_speed_per_s > {clamp_per_s}"
    evidence = dict(motion or {})
    evidence["clamp_per_s"] = clamp_per_s
    if max_speed is None:
        return _unknown(
            "excessive_crop_movement",
            "no keyframe movement measured (plan has no usable keyframes)",
            threshold=clamp_per_s, comparator=comparator, method="reframe_keyframes",
            evidence=evidence,
        )
    if max_speed > clamp_per_s:
        worst = max((motion or {}).get("speeds") or [{}], key=lambda s: s["speed_per_s"])
        return _violation(
            "excessive_crop_movement",
            f"crop moves {max_speed:.3f} frame-widths/s at {worst.get('from_s')}s, over "
            f"the {clamp_per_s}/s clamp",
            severity=SEVERITY_HARD, measured=max_speed, threshold=clamp_per_s,
            comparator=comparator, method="reframe_keyframes", evidence=evidence,
        )
    if max_speed > warn_per_s:
        return _warn(
            "excessive_crop_movement",
            f"crop moves up to {max_speed:.3f} frame-widths/s -- fast but within the clamp",
            measured=max_speed, threshold=warn_per_s,
            comparator=f"max_speed_per_s > {warn_per_s}",
            method="reframe_keyframes", evidence=evidence,
        )
    return _ok(
        "excessive_crop_movement", f"max crop movement {max_speed:.3f}/s within the clamp",
        measured=max_speed, threshold=clamp_per_s, comparator=comparator,
        method="reframe_keyframes", evidence=evidence,
    )


def check_unstable_crop(
    motion: dict | None, *, warn_per_s2: float = CROP_JITTER_WARN_PER_S2,
    fail_per_s2: float = CROP_JITTER_FAIL_PER_S2,
) -> QCCheck:
    """Jitter: the crop's acceleration, i.e. how fast the movement REVERSES.

    A smoothed plan accelerates at ~0; a plan whose keyframes were not smoothed
    measures high even when the average speed is low, which is exactly the
    "unstable crop" a viewer notices as a wobble. A slow but reversing crop
    (constant speed, changing direction) is the case a speed-only metric
    cannot see and this one does.
    """
    max_jitter = _num((motion or {}).get("max_jitter_per_s2"))
    mean_jitter = _num((motion or {}).get("mean_jitter_per_s2"))
    comparator = f"mean_jitter_per_s2 > {fail_per_s2}"
    evidence = dict(motion or {})
    evidence["warn_per_s2"] = warn_per_s2
    if max_jitter is None:
        return _unknown(
            "unstable_crop",
            "not enough keyframes to measure crop jitter (need 3+ rects)",
            threshold=fail_per_s2, comparator=comparator, method="reframe_keyframes",
            evidence=evidence,
        )
    measured = mean_jitter if mean_jitter is not None else max_jitter
    if (mean_jitter or 0.0) > fail_per_s2:
        return _violation(
            "unstable_crop",
            f"crop accelerates at {_round(mean_jitter)}/s^2 (max {max_jitter}/s^2) -- "
            "the plan reverses direction and was not smoothed",
            severity=SEVERITY_HARD, measured=measured, threshold=fail_per_s2,
            comparator=comparator, method="reframe_keyframes", evidence=evidence,
        )
    if (mean_jitter or 0.0) > warn_per_s2:
        return _warn(
            "unstable_crop",
            f"crop jitter {_round(mean_jitter)}/s^2 is visible on a 9:16 crop",
            measured=measured, threshold=warn_per_s2,
            comparator=f"mean_jitter_per_s2 > {warn_per_s2}",
            method="reframe_keyframes", evidence=evidence,
        )
    return _ok(
        "unstable_crop", f"crop jitter {_round(mean_jitter) or 0.0}/s^2 within tolerance",
        measured=measured, threshold=fail_per_s2, comparator=comparator,
        method="reframe_keyframes", evidence=evidence,
    )


def subject_coverage(
    keyframes: Sequence[Any],
    samples: Sequence[Any],
    *,
    target_aspect: str = "9:16",
    source_aspect: str = "16:9",
    anchor: str = KEYFRAME_ANCHOR,
    source_width: float | None = None,
    source_height: float | None = None,
) -> dict:
    """Share of sampled times where a face box is inside the crop in effect.

    A sample is "covered" when its box intersects the crop rect that the LAST
    keyframe at or before its ``t_s`` defines (a keyframe holds until the next
    one). Returns
    ``{"covered", "samples", "coverage", "uncovered_t_s"}``; ``coverage`` is
    ``None`` when there is no subject evidence at all, which is what keeps
    ``missing_subject`` an ``UNKNOWN`` rather than a fabricated zero.

    UNITS: ``crop_rect_at`` returns a rect NORMALISED to the source frame
    (0..1 per axis). ``face_track_samples`` store boxes in SOURCE PIXELS (lane
    E never normalises), so pass ``source_width``/``source_height`` and the
    samples are converted before the intersection. Samples that already look
    normalised (nothing past 1.0) are compared as-is; pixel-sized samples
    without a frame size are skipped so coverage stays ``None`` (UNKNOWN)
    instead of a fabricated 0.0 that would read as a HARD violation.
    """
    ordered = sorted(keyframes or [], key=lambda k: _num(getattr(k, "t_s", 0)) or 0.0)
    rects: list[tuple[float, dict]] = []
    for keyframe in ordered:
        t_s = _num(getattr(keyframe, "t_s", None))
        rect = crop_rect_at(keyframe, target_aspect=target_aspect,
                            source_aspect=source_aspect, anchor=anchor)
        if t_s is not None and rect:
            rects.append((t_s, rect))
    uncovered: list[float] = []
    covered = 0
    count = 0
    for sample in samples or []:
        t_s = _num(getattr(sample, "t_s", None))
        if t_s is None:
            continue
        active = [rect for start, rect in rects if start <= t_s]
        rect = active[-1] if active else (rects[0][1] if rects else None)
        if not rect:
            continue
        x, y = _num(getattr(sample, "x", None)), _num(getattr(sample, "y", None))
        w, h = _num(getattr(sample, "w", None)), _num(getattr(sample, "h", None))
        if None in (x, y, w, h):
            continue
        # Sample boxes are SOURCE PIXELS (lane E never normalises) while the
        # rect is NORMALISED. Determine the sample unit instead of assuming it:
        # anything extending past 1.0 cannot be normalised, so the frame size
        # is required and the sample is divided by it. If such a sample arrives
        # without a frame size we skip it -- coverage then stays None
        # (UNKNOWN) rather than reporting a fabricated 0.0 that would read as a
        # HARD missing-subject violation.
        pixel_units = any(
            (v or 0.0) > 1.0 for v in (x, y, (x or 0.0) + (w or 0.0), (y or 0.0) + (h or 0.0))
        )
        if pixel_units:
            if not (source_width and source_height and source_width > 0 and source_height > 0):
                continue
            x, y = x / source_width, y / source_height
            w, h = w / source_width, h / source_height
        count += 1
        if x < rect["x1"] and x + (w or 0.0) > rect["x0"] \
                and y < rect["y1"] and y + (h or 0.0) > rect["y0"]:
            covered += 1
        else:
            uncovered.append(round(t_s, 3))
    return {
        "covered": covered,
        "samples": count,
        "coverage": _round(covered / count) if count else None,
        "uncovered_t_s": uncovered[:20],
    }


def check_missing_subject(
    coverage: dict | None, *, min_coverage: float = SUBJECT_COVERAGE_MIN,
    hard_coverage: float = SUBJECT_COVERAGE_HARD,
) -> QCCheck:
    """Face/subject coverage of the crop over the sampled frames (contracts §13).

    Without any subject evidence (no face tracks -- usually a provider that is
    honestly UNAVAILABLE) this is ``UNKNOWN``, never a zero: QC must not claim
    "the subject is missing" from the absence of a detector.
    """
    ratio = _num((coverage or {}).get("coverage"))
    evidence = dict(coverage or {})
    evidence["min_coverage"] = min_coverage
    comparator = f"coverage < {min_coverage}"
    if ratio is None:
        return _unknown(
            "missing_subject",
            "no face/subject evidence for this plan -- coverage was not measured",
            threshold=min_coverage, comparator=comparator,
            method="face_track_samples+reframe_keyframes", evidence=evidence,
        )
    if ratio < hard_coverage:
        return _violation(
            "missing_subject",
            f"the subject is inside the crop for only {ratio * 100:.0f}% of the "
            "sampled frames",
            severity=SEVERITY_HARD, measured=ratio, threshold=hard_coverage,
            comparator=f"coverage < {hard_coverage}",
            method="face_track_samples+reframe_keyframes", evidence=evidence,
        )
    if ratio < min_coverage:
        return _warn(
            "missing_subject",
            f"the subject is inside the crop for {ratio * 100:.0f}% of the sampled "
            f"frames (below the {min_coverage * 100:.0f}% target)",
            measured=ratio, threshold=min_coverage, comparator=comparator,
            method="face_track_samples+reframe_keyframes", evidence=evidence,
        )
    return _ok(
        "missing_subject", f"subject covered in {ratio * 100:.0f}% of sampled frames",
        measured=ratio, threshold=min_coverage, comparator=comparator,
        method="face_track_samples+reframe_keyframes", evidence=evidence,
    )


def track_reports(
    tracks: Sequence[FaceTrack],
    samples_by_track: dict[str, list[FaceTrackSample]],
    *,
    sample_fps: float = DEFAULT_SAMPLE_FPS,
) -> list[dict]:
    """Per-track coverage/gap report used by :func:`check_face_lost`.

    ``max_gap_s`` is the largest interval between consecutive samples (0.0 for
    a single-sample track), ``coverage`` is the sample count over what the
    track's own span implies at ``sample_fps`` -- ``floor(span * fps) + 1``
    samples, i.e. one per nominal interval including both ends, so a perfectly
    sampled track measures 1.0. A track flagged ``truncated`` (contracts §8
    sample cap) is reported with that flag so its lower coverage is never read
    as a detector failure.
    """
    reports: list[dict] = []
    for track in tracks or []:
        label = str(getattr(track, "track_id", "") or "")
        times = sorted(
            t for t in (_num(getattr(s, "t_s", None)) for s in samples_by_track.get(label, []))
            if t is not None
        )
        start = _num(getattr(track, "start_s", None)) or (times[0] if times else 0.0)
        end = _num(getattr(track, "end_s", None)) or (times[-1] if times else start)
        span = max(0.0, end - start)
        gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
        expected = max(1.0, float(int(span * max(0.001, sample_fps))) + 1.0)
        reports.append({
            "track_id": label,
            "start_s": _round(start, 3),
            "end_s": _round(end, 3),
            "span_s": _round(span, 3),
            "samples": len(times),
            "max_gap_s": _round(max(gaps), 3) if gaps else 0.0,
            "coverage": _round(len(times) / expected),
            "truncated": bool(getattr(track, "truncated", False)),
            "reentry_count": int(getattr(track, "reentry_count", 0) or 0),
        })
    return reports


def check_face_lost(
    reports: Sequence[dict] | None, *, gap_warn_s: float = FACE_GAP_WARN_S,
    gap_fail_s: float = FACE_GAP_FAIL_S, coverage_min: float = FACE_COVERAGE_MIN,
) -> QCCheck:
    """A track with a coverage gap beyond tolerance means the face was LOST.

    Both signals are per-track: the longest hole between samples (an occlusion
    or a failed association) and the track's own sample coverage vs its span. A
    track the provider itself flagged ``truncated`` is EXEMPT from both
    verdicts (its holes are the sample cap, not a lost face) and only warns --
    the evidence is honestly partial, never silently declared complete.
    """
    rows = list(reports or [])
    evidence = {"tracks": rows, "tracks_total": len(rows)}
    comparator = f"max_gap_s > {gap_fail_s} or coverage < {coverage_min}"
    if not rows:
        return _unknown(
            "face_lost", "no face tracks to evaluate -- face loss was not measured",
            threshold=gap_fail_s, comparator=comparator,
            method="face_tracks+face_track_samples", evidence=evidence,
        )
    lost = [r for r in rows
            if not r.get("truncated") and (r.get("max_gap_s") or 0.0) > gap_fail_s]
    thin = [r for r in rows
            if not r.get("truncated")
            and _num(r.get("coverage")) is not None
            and float(r["coverage"]) < coverage_min]
    if lost:
        worst = max(lost, key=lambda r: r.get("max_gap_s") or 0.0)
        return _violation(
            "face_lost",
            f"track {worst.get('track_id') or '?'} has a "
            f"{worst.get('max_gap_s')}s gap with no detection",
            severity=SEVERITY_HARD, measured=worst.get("max_gap_s"), threshold=gap_fail_s,
            comparator=f"max_gap_s > {gap_fail_s}",
            method="face_tracks+face_track_samples", evidence=evidence,
        )
    if thin:
        worst = min(thin, key=lambda r: r.get("coverage") or 0.0)
        return _violation(
            "face_lost",
            f"track {worst.get('track_id') or '?'} covers only "
            f"{float(worst.get('coverage') or 0) * 100:.0f}% of its own span",
            severity=SEVERITY_HARD, measured=worst.get("coverage"), threshold=coverage_min,
            comparator=f"coverage < {coverage_min}",
            method="face_tracks+face_track_samples", evidence=evidence,
        )
    gapped = [r for r in rows if (r.get("max_gap_s") or 0.0) > gap_warn_s]
    partial = [r for r in rows if r.get("truncated")]
    if partial:
        return _warn(
            "face_lost",
            f"{len(partial)} track(s) hit the provider sample cap -- coverage is "
            "partial by the provider's own admission, not a lost face",
            measured=max((r.get("max_gap_s") or 0.0) for r in partial),
            threshold=gap_fail_s, comparator="truncated track: not verifiable",
            method="face_tracks+face_track_samples", evidence=evidence,
        )
    if gapped:
        worst = max(gapped, key=lambda r: r.get("max_gap_s") or 0.0)
        return _warn(
            "face_lost",
            f"track {worst.get('track_id') or '?'} has a {worst.get('max_gap_s')}s "
            f"detection gap (tolerance {gap_fail_s}s)",
            measured=worst.get("max_gap_s"), threshold=gap_warn_s,
            comparator=f"max_gap_s > {gap_warn_s}",
            method="face_tracks+face_track_samples", evidence=evidence,
        )
    return _ok(
        "face_lost", f"all {len(rows)} track(s) continuous within tolerance",
        measured=max((r.get("max_gap_s") or 0.0) for r in rows), threshold=gap_fail_s,
        comparator=comparator, method="face_tracks+face_track_samples", evidence=evidence,
    )


def check_mask_failure(
    masks: Sequence[dict] | None, *, min_area_ratio: float = MASK_MIN_AREA_RATIO,
    min_dimension: int = MASK_MIN_DIMENSION,
) -> QCCheck:
    """Missing / empty / tiny / corrupt mask asset (contracts §9).

    ``masks`` are plain dicts so this stays a pure function: ``mask_asset_id``
    (missing file ref), ``format`` (must be a real mask container),
    ``width``/``height``/``area_ratio`` (a speck is a failure), and the
    optional ``checksum_mismatch`` flag the caller sets after comparing the
    recorded checksum against the file on disk.
    """
    rows = list(masks or [])
    evidence = {"masks": rows, "masks_total": len(rows)}
    comparator = f"area_ratio < {min_area_ratio} or min(w,h) < {min_dimension}"
    if not rows:
        return _unknown(
            "mask_failure", "no mask assets recorded -- mask integrity was not measured",
            threshold=min_area_ratio, comparator=comparator,
            method="mask_assets", evidence=evidence,
        )
    problems: list[str] = []
    for row in rows:
        label = str(row.get("kind") or row.get("mask_asset_id") or "mask")
        if not row.get("mask_asset_id"):
            problems.append(f"{label}: no mask file reference")
            continue
        fmt = str(row.get("format") or "").upper()
        if fmt not in MASK_FORMATS:
            problems.append(f"{label}: unsupported mask format {fmt or 'missing'!r}")
        width, height = _num(row.get("width")), _num(row.get("height"))
        if width is not None and height is not None and (
            width < min_dimension or height < min_dimension
        ):
            problems.append(f"{label}: {int(width)}x{int(height)} px is below "
                            f"{min_dimension}px")
        area = _num(row.get("area_ratio"))
        if area is not None and area < min_area_ratio:
            problems.append(f"{label}: covers {area * 100:.2f}% of the frame")
        if row.get("missing_file"):
            problems.append(f"{label}: mask file missing on disk")
        if row.get("checksum_mismatch"):
            problems.append(f"{label}: checksum does not match the mask file")
    if problems:
        return _violation(
            "mask_failure", "mask failure: " + "; ".join(problems[:4]),
            severity=SEVERITY_HARD, measured=len(problems), threshold=0,
            comparator="mask reference present, readable, >= 1% area, PNG/RLE_JSON",
            method="mask_assets", evidence=evidence,
        )
    return _ok(
        "mask_failure", f"{len(rows)} mask asset(s) present, sized and checksummed",
        measured=len(rows), threshold=min_area_ratio, comparator=comparator,
        method="mask_assets", evidence=evidence,
    )


def is_black_frame(frame: dict, *, mean_threshold: float = BLACK_FRAME_MEAN_LUMA,
                  max_threshold: float = BLACK_FRAME_MAX_LUMA) -> bool:
    """The black-frame predicate: dark AND with nothing bright in it."""
    mean = _num(frame.get("mean_luma") if isinstance(frame, dict) else frame)
    peak = _num(frame.get("max_luma") if isinstance(frame, dict) else None)
    if mean is None:
        return False
    if mean >= mean_threshold:
        return False
    return True if peak is None else peak < max_threshold


def check_black_frames(
    frames: Sequence[dict] | None, *, warn_ratio: float = BLACK_FRAME_RATIO_WARN,
    fail_ratio: float = BLACK_FRAME_RATIO_FAIL,
) -> QCCheck:
    """Share of sampled frames that are black (mean luma, contracts §13).

    A dip is a warning (a fade, a letterbox); a fifth-of-a-clip share is a hard
    violation. Real detection only -- the numbers come from
    :func:`frame_luma`, and an empty sample set is ``UNKNOWN`` (nothing was
    decoded, which is not the same as "no black frames").
    """
    rows = list(frames or [])
    evidence = {
        "frames_sampled": len(rows),
        "black_t_s": [f.get("t_s") for f in rows if is_black_frame(f)][:20],
        "mean_luma_min": min((_num(f.get("mean_luma")) for f in rows
                              if _num(f.get("mean_luma")) is not None), default=None),
    }
    comparator = f"black_frame_ratio > {fail_ratio}"
    if not rows:
        return _unknown(
            "black_frames", "no video frames decoded -- black frames were not measured",
            threshold=fail_ratio, comparator=comparator, method="ffmpeg/rawvideo",
            evidence=evidence,
        )
    black = [f for f in rows if is_black_frame(f)]
    ratio = len(black) / len(rows)
    evidence["black_frame_ratio"] = _round(ratio)
    if ratio > fail_ratio:
        return _violation(
            "black_frames",
            f"{len(black)}/{len(rows)} sampled frames are black ({ratio * 100:.0f}%)",
            severity=SEVERITY_HARD, measured=_round(ratio), threshold=fail_ratio,
            comparator=comparator, method="ffmpeg/rawvideo", evidence=evidence,
        )
    if ratio > warn_ratio:
        return _warn(
            "black_frames",
            f"{len(black)}/{len(rows)} sampled frames are black ({ratio * 100:.0f}%)",
            measured=_round(ratio), threshold=warn_ratio,
            comparator=f"black_frame_ratio > {warn_ratio}",
            method="ffmpeg/rawvideo", evidence=evidence,
        )
    return _ok(
        "black_frames", f"no black frames in {len(rows)} sampled frames",
        measured=_round(ratio), threshold=fail_ratio, comparator=comparator,
        method="ffmpeg/rawvideo", evidence=evidence,
    )


def check_unresolved_dependency(
    rows: Sequence[dict] | None, *, kind: str = "active_speaker_map",
) -> QCCheck:
    """An ``UNRESOLVED`` dependency (e.g. the active-speaker map) is REVIEW.

    A plan that follows the active speaker can only be as trustworthy as that
    mapping, and contracts §10 requires an unresolved mapping to say so rather
    than guess. So an unresolved row is not a failure -- the honest state is a
    human look, hence ``REVIEW`` severity (=> ``REVIEW_REQUIRED``).
    """
    items = list(rows or [])
    unresolved = [r for r in items if str(r.get("status") or "") == "UNRESOLVED"]
    reasons = sorted({str(r.get("reason") or "unspecified") for r in unresolved})
    evidence = {
        "dependency": kind, "rows": len(items), "unresolved": len(unresolved),
        "reasons": reasons[:10],
    }
    comparator = "unresolved dependency rows == 0"
    if not items:
        return _unknown(
            "unresolved_dependency",
            f"no {kind} rows -- the dependency state was not measured",
            threshold=0, comparator=comparator, method=kind, evidence=evidence,
            severity=SEVERITY_REVIEW,
        )
    if unresolved:
        return _violation(
            "unresolved_dependency",
            f"{len(unresolved)}/{len(items)} {kind} row(s) unresolved "
            f"({', '.join(reasons[:3]) or 'unspecified'}) -- a human must confirm",
            severity=SEVERITY_REVIEW, measured=len(unresolved), threshold=0,
            comparator=comparator, method=kind, evidence=evidence,
        )
    return _ok(
        "unresolved_dependency", f"all {len(items)} {kind} row(s) resolved",
        measured=0, threshold=0, comparator=comparator, method=kind, evidence=evidence,
    )


# ---------------------------------------------------------------------------
# aggregation (contracts §13)
# ---------------------------------------------------------------------------


def aggregate_verdict(
    checks: Sequence[QCCheck], *, override: dict | None = None
) -> dict:
    """Turn per-check severities into one verdict + the reasons behind it.

    1. any ``HARD`` violation            -> ``FAIL``
    2. else any ``REVIEW`` violation    -> ``REVIEW_REQUIRED``
       (heuristic-only evidence, an ``UNRESOLVED`` dependency, or a recorded
       override -- an override never rewrites the measurement)
    3. else any ``WARN``/``UNKNOWN``    -> ``PASS_WITH_WARNINGS``
    4. else                             -> ``PASS``
    """
    hard: list[str] = []
    review: list[str] = []
    warnings: list[str] = []
    unknown: list[str] = []
    for check in checks or []:
        if check.status == "VIOLATION":
            bucket = hard if check.severity == SEVERITY_HARD else (
                review if check.severity == SEVERITY_REVIEW else warnings
            )
            bucket.append(str(check.name))
        elif check.status == "UNKNOWN":
            unknown.append(str(check.name))
        elif check.status == "WARN":
            warnings.append(str(check.name))
    if override:
        review = [*review, OVERRIDE_CHECK_NAME]
    if hard:
        verdict = "FAIL"
    elif review:
        verdict = "REVIEW_REQUIRED"
    elif warnings or unknown:
        verdict = "PASS_WITH_WARNINGS"
    else:
        verdict = "PASS"
    return {
        "verdict": verdict,
        "failures": hard,
        "review_reasons": review,
        "warnings": warnings,
        "unknown": unknown,
        "overridden": bool(override),
    }


def qc_dto(row: IntelQCResult) -> dict:
    """Public shape of one QC result (checks carry their own evidence)."""
    checks = list(row.checks_json or [])
    overrides = overrides_of(row)
    override = overrides[-1] if overrides else None
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "run_id": row.run_id,
        "kind": str(row.kind or ""),
        "verdict": str(row.verdict or "PASS"),
        "checks": checks,
        "failures": [c["name"] for c in checks
                     if c.get("verdict") == "VIOLATION" and c.get("severity") == SEVERITY_HARD],
        "review_reasons": [c["name"] for c in checks
                           if c.get("verdict") == "VIOLATION"
                           and c.get("severity") == SEVERITY_REVIEW],
        "warnings": [c["name"] for c in checks if c.get("verdict") in ("WARN", "UNKNOWN")],
        "overrides": overrides,
        "override": override,
        "apply_requires_override": str(row.verdict or "") == "FAIL" and not override,
        "created_at": row.created_at.isoformat() + "Z" if row.created_at else None,
    }


def overrides_of(row: IntelQCResult) -> list[dict]:
    """Every recorded override carried by a QC result, oldest first."""
    out = []
    for check in list(row.checks_json or []):
        if isinstance(check, dict) and check.get("name") == OVERRIDE_CHECK_NAME:
            evidence = dict(check.get("evidence") or {})
            out.append({
                "by": str(evidence.get("by") or ""),
                "reason": str(evidence.get("reason") or ""),
                "at": str(evidence.get("at") or ""),
                "overridden_verdict": str(evidence.get("overridden_verdict") or ""),
                "result_id": str(evidence.get("result_id") or ""),
                "checks": list(evidence.get("checks") or []),
            })
    return out


# ---------------------------------------------------------------------------
# persistence + evidence collection
# ---------------------------------------------------------------------------


def get_result(db: Session, workspace_id: str, run_id: str,
               kind: str | None = None) -> IntelQCResult | None:
    """Latest QC result for a run in THIS workspace (foreign/missing -> None)."""
    if not run_id or not workspace_id:
        return None
    query = select(IntelQCResult).where(
        IntelQCResult.workspace_id == str(workspace_id),
        IntelQCResult.run_id == str(run_id),
    )
    if kind:
        query = query.where(IntelQCResult.kind == str(kind))
    return db.scalars(
        query.order_by(IntelQCResult.created_at.desc(), IntelQCResult.id.desc()).limit(1)
    ).first()


def list_results(db: Session, workspace_id: str, run_id: str) -> list[IntelQCResult]:
    """Every QC result for a run, newest first (append-only history)."""
    if not run_id or not workspace_id:
        return []
    return list(db.scalars(
        select(IntelQCResult)
        .where(
            IntelQCResult.workspace_id == str(workspace_id),
            IntelQCResult.run_id == str(run_id),
        )
        .order_by(IntelQCResult.created_at.desc(), IntelQCResult.id.desc())
    ).all())


def _persist(
    db: Session,
    workspace_id: str,
    run_id: str,
    kind: str,
    checks: Sequence[QCCheck] | Sequence[dict],
    summary: dict,
    *,
    extra: dict | None = None,
) -> IntelQCResult:
    """Append one QC row. Never edits an earlier verdict.

    ``checks`` accepts :class:`QCCheck` objects or already-serialised dicts (an
    override row copies the checks it overrides, so the newest result for a run
    is always self-describing).
    """
    rows = [c.as_dict() if isinstance(c, QCCheck) else dict(c) for c in checks]
    if extra:
        rows.append(dict(extra))
    kind = str(kind or "").strip().lower()
    if kind not in QC_KINDS:
        raise ValueError(f"unknown qc kind {kind!r}")
    verdict = str(summary.get("verdict") or "PASS")
    if verdict not in QC_VERDICTS:
        raise ValueError(f"unknown qc verdict {verdict!r}")
    row = IntelQCResult(
        workspace_id=str(workspace_id),
        run_id=str(run_id),
        kind=kind,
        verdict=verdict,
        checks_json=rows,
    )
    db.add(row)
    db.flush()
    return row


def _event(kind: str, message: str, *, level: str, payload: dict) -> dict:
    """One activity-feed event, SURFACED not emitted (contracts §3).

    ``record_event`` opens its own session, so the route emits this AFTER
    ``db.commit()``. Mirrors ``engine/collab/reviews.py``'s ordering rule.
    """
    return {
        "kind": kind, "message": message, "level": level,
        "source": "media_intel", "data": dict(payload or {}),
    }


def _qc_event(workspace_id: str, run_id: str, kind: str, summary: dict,
              result_id: str) -> dict:
    verdict = str(summary.get("verdict") or "PASS")
    level = "error" if verdict == "FAIL" else (
        "warning" if verdict in ("REVIEW_REQUIRED", "PASS_WITH_WARNINGS") else "info"
    )
    return _event(
        EVENT_QC_COMPLETED,
        f"media-intel {kind} QC {verdict}",
        level=level,
        payload={
            "run_id": str(run_id), "kind": str(kind), "verdict": verdict,
            "result_id": str(result_id),
            "failures": list(summary.get("failures") or []),
            "review_reasons": list(summary.get("review_reasons") or []),
            "warnings": list(summary.get("warnings") or []),
            "workspace_id": str(workspace_id),
        },
    )


# --- audio evidence ---------------------------------------------------------


def _speech_activity_s(db: Session, run: MediaIntelRun) -> float | None:
    """Total SPEECH_ACTIVITY seconds for a run (``kind='SPEECH_ACTIVITY'`` rows).

    These are ffmpeg VAD measurements, not speakers (contracts §4). Returns
    ``None`` when the run has no VAD evidence -- the caller then falls back to
    the whole duration and downgrades the check to REVIEW severity.
    """
    rows = db.execute(
        select(DiarizationSegment.start_s, DiarizationSegment.end_s)
        .where(
            DiarizationSegment.run_id == str(run.id),
            DiarizationSegment.kind == "SPEECH_ACTIVITY",
        )
    ).all()
    if not rows:
        return None
    return round(sum(max(0.0, float(b) - float(a)) for a, b in rows), 4)


def _removed_s(db: Session, workspace_id: str, run: MediaIntelRun) -> tuple[float, str]:
    """Seconds removed by the edit, and the evidence source that said so.

    Preference order (the most authoritative statement first):
    1. an ``audio_time_maps`` row for the asset -- the actual source->edited
       mapping, so the removed time is derived, not assumed;
    2. ``edit_proposals`` whose decision is ``remove``/``shorten`` -- merged
       first so overlapping proposals are not double counted.

    Returns ``(0.0, "none")`` when nothing was decided.
    """
    maps = db.scalars(
        select(AudioTimeMap)
        .where(AudioTimeMap.workspace_id == str(workspace_id),
               AudioTimeMap.asset_id == str(run.asset_id))
        .order_by(AudioTimeMap.created_at.desc())
    ).first()
    if maps is not None:
        segments = list(maps.segments_json or [])
        if segments:
            kept = sum(
                max(0.0, float(s.get("src_end", 0)) - float(s.get("src_start", 0)))
                for s in segments if isinstance(s, dict)
            )
            covered = sum(
                max(0.0, float(s.get("out_end", 0)) - float(s.get("out_start", 0)))
                for s in segments if isinstance(s, dict)
            )
            if kept > 0 and covered > 0:
                return round(max(0.0, kept - covered), 4), "audio_time_maps"
    rows = db.execute(
        select(EditProposal.start_s, EditProposal.end_s)
        .where(
            EditProposal.workspace_id == str(workspace_id),
            EditProposal.run_id == str(run.id),
            EditProposal.decision.in_(("remove", "shorten")),
        )
    ).all()
    spans = sorted((float(a), float(b)) for a, b in rows if float(b) > float(a))
    merged: list[list[float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return round(sum(end - start for start, end in merged), 4), "edit_proposals"


def _word_counts(
    db: Session, run: MediaIntelRun, metrics: dict
) -> tuple[int | None, int | None]:
    """``(source_words, output_words)`` -- recorded metrics first, rows second."""
    source = _recorded(metrics, "source_words", "source_word_count", "words_source")
    output = _recorded(metrics, "output_words", "output_word_count", "words_output")
    try:
        source_words = int(source) if source is not None else None
    except (TypeError, ValueError):
        source_words = None
    try:
        output_words = int(output) if output is not None else None
    except (TypeError, ValueError):
        output_words = None
    if output_words is None:
        counted = int(db.query(MediaIntelWord)
                      .filter(MediaIntelWord.run_id == str(run.id)).count() or 0)
        output_words = counted or None
    if source_words is None:
        prior = db.scalars(
            select(MediaIntelRun)
            .where(
                MediaIntelRun.workspace_id == str(run.workspace_id),
                MediaIntelRun.asset_id == str(run.asset_id),
                MediaIntelRun.id != str(run.id),
                MediaIntelRun.status == "COMPLETED",
            )
            .order_by(MediaIntelRun.created_at.desc())
        ).first()
        if prior is not None:
            count = int(db.query(MediaIntelWord)
                        .filter(MediaIntelWord.run_id == str(prior.id)).count() or 0)
            source_words = count or None
    return source_words, output_words


def run_audio_qc(
    db: Session,
    ws: Any,
    run: MediaIntelRun,
    *,
    source_path: str | Path | None = None,
    output_path: str | Path | None = None,
    metrics: dict | None = None,
    max_removal_ratio: float | None = None,
    persist: bool = True,
) -> dict:
    """``AudioIntelligenceQC`` -- the audio verdict for one run (contracts §13).

    Measurement order: the run's own ``metrics_json`` (contracts §6 before/after
    block) first, a real ffmpeg pass second, and only for what is still missing.
    Checks, in contract order: ``duration_drift``, ``clipping``, ``missing_audio``,
    ``excessive_removed_speech``, ``transcript_mismatch``.

    Returns the QC DTO plus ``events`` (the caller emits them after
    ``db.commit()``). With ``persist=False`` nothing is written -- the verdict is
    still computed and returned.
    """
    workspace_id = str(getattr(ws, "id", ws) or run.workspace_id)
    flat = _flatten_metrics(metrics if metrics is not None else run.metrics_json)
    src_path = source_path if source_path is not None else (
        asset_media_path(db, workspace_id, run.asset_id))
    out_path = output_path if output_path is not None else (
        asset_media_path(db, workspace_id, run.output_asset_id))

    # 1) what the run already recorded -- authoritative, never re-measured
    source_duration = _num(_recorded(flat, "source_duration_s", "input_duration_s",
                                     "source_duration_seconds"))
    output_duration = _num(_recorded(flat, "output_duration_s", "result_duration_s",
                                     "output_duration_seconds", "duration_seconds"))
    peak = _num(_recorded(flat, "output_peak_dbfs", "peak_dbfs"))
    true_peak = _num(_recorded(flat, "output_true_peak_dbfs", "true_peak_dbfs"))
    clipped = _recorded(flat, "output_clipped_samples", "clipped_samples")
    has_stream = _recorded(flat, "output_has_audio_stream")
    lufs = _num(_recorded(flat, "output_integrated_lufs", "integrated_lufs"))
    silence_ratio = _num(_recorded(flat, "output_silence_ratio", "silence_ratio"))
    samples = _recorded(flat, "output_samples", "samples")

    # 2) only the passes that are still missing, on the real files
    need: set[str] = set()
    if has_stream is None or output_duration is None:
        need.add("probe")
    if peak is None or samples is None or clipped is None:
        need.add("peaks")
    if true_peak is None or lufs is None:
        need.add("loudness")
    if silence_ratio is None:
        # the silence pass is only worth running when the loudness is not
        # already conclusive (recorded or measured at the R128 silence floor)
        if lufs is None or lufs > MISSING_AUDIO_LUFS_FLOOR:
            need.add("silence")
        elif output_duration:
            silence_ratio = 1.0
    output_metrics = audio_measurements(out_path, need=sorted(need)) if out_path else {}
    if source_duration is None and src_path:
        source_duration = fu.duration_seconds(src_path)
    if output_duration is None:
        output_duration = output_metrics.get("duration_s")
    if peak is None:
        peak = _num(output_metrics.get("peak_dbfs"))
    if true_peak is None:
        true_peak = _num(output_metrics.get("true_peak_dbfs"))
    if has_stream is None:
        has_stream = output_metrics.get("has_audio_stream")
    if lufs is None:
        lufs = _num(output_metrics.get("integrated_lufs"))
    if silence_ratio is None:
        silence_ratio = _num(output_metrics.get("silence_ratio"))
    if samples is None:
        samples = output_metrics.get("samples")

    removed_s, removed_source = _removed_s(db, workspace_id, run)
    recorded_removed = _num(_recorded(flat, "removed_speech_s", "removed_s"))
    if recorded_removed is not None:
        removed_s, removed_source = recorded_removed, "run.metrics_json"
    speech_s = _num(_recorded(flat, "speech_activity_s"))
    if speech_s is None:
        speech_s = _speech_activity_s(db, run)
    source_words, output_words = _word_counts(db, run, flat)

    checks = [
        check_duration_drift(source_duration, output_duration),
        check_clipping(peak_dbfs=peak, true_peak_dbfs=true_peak, clipped_samples=clipped),
        check_missing_audio(
            has_audio_stream=has_stream, peak_dbfs=peak, integrated_lufs=lufs,
            samples=samples, silence_ratio=silence_ratio, duration_s=output_duration,
        ),
        check_excessive_removed_speech(
            removed_s, speech_s, source_duration_s=source_duration,
            max_removal_ratio=max_removal_ratio,
        ),
        check_transcript_mismatch(
            source_words=source_words, output_words=output_words,
            source_duration_s=source_duration, output_duration_s=output_duration,
        ),
    ]
    summary = aggregate_verdict(checks)
    summary["measured"] = {
        "source_path": str(src_path or ""),
        "output_path": str(out_path or ""),
        "measured_passes": list(output_metrics.get("passes") or []),
        "removed_s": removed_s,
        "removed_source": removed_source,
        "speech_activity_s": speech_s,
        "source_words": source_words,
        "output_words": output_words,
    }
    payload = {
        "workspace_id": workspace_id,
        "run_id": str(run.id),
        "kind": "audio",
        "checks": [c.as_dict() for c in checks],
        "failures": summary["failures"],
        "review_reasons": summary["review_reasons"],
        "warnings": summary["warnings"],
        "unknown": summary["unknown"],
        "verdict": summary["verdict"],
        "measured": summary["measured"],
        "persisted": False,
        "events": [_qc_event(workspace_id, run.id, "audio", summary, "")],
    }
    if persist:
        row = _persist(db, workspace_id, run.id, "audio", checks, summary)
        payload["id"] = row.id
        payload["persisted"] = True
        payload["created_at"] = row.created_at.isoformat() + "Z" if row.created_at else None
        payload["events"] = [_qc_event(workspace_id, run.id, "audio", summary, row.id)]
    return payload


# --- visual evidence --------------------------------------------------------


def _plan_keyframes(db: Session, workspace_id: str, run: MediaIntelRun,
                    plan: ReframePlan | None = None) -> tuple[list[ReframeKeyframe], dict]:
    """Keyframes of the run's plan (or the caller's plan) + the plan context."""
    if plan is None:
        plan = db.scalar(
            select(ReframePlan)
            .where(ReframePlan.workspace_id == str(workspace_id),
                   ReframePlan.run_id == str(run.id))
            .order_by(ReframePlan.created_at.desc())
        )
    if plan is None:
        return [], {}
    rows = db.scalars(
        select(ReframeKeyframe)
        .where(ReframeKeyframe.plan_id == str(plan.id),
               ReframeKeyframe.workspace_id == str(workspace_id))
        .order_by(ReframeKeyframe.t_s.asc())
    ).all()
    meta = dict(plan.meta_json or {})
    context = {
        "plan_id": str(plan.id),
        "layout": str(plan.layout or ""),
        "aspect": str(plan.aspect or "9:16"),
        "strategy": str(plan.strategy or ""),
        "source_width": _num(meta.get("source_width")),
        "source_height": _num(meta.get("source_height")),
    }
    return list(rows), context


def _face_evidence(
    db: Session, workspace_id: str, run: MediaIntelRun
) -> tuple[list[FaceTrack], dict[str, list[FaceTrackSample]]]:
    tracks = db.scalars(
        select(FaceTrack)
        .where(FaceTrack.workspace_id == str(workspace_id), FaceTrack.run_id == str(run.id))
        .order_by(FaceTrack.start_s.asc())
    ).all()
    by_track: dict[str, list[FaceTrackSample]] = {}
    if tracks:
        ids = [str(t.id) for t in tracks]
        for sample in db.scalars(
            select(FaceTrackSample)
            .where(FaceTrackSample.run_id == str(run.id),
                   FaceTrackSample.track_id.in_(ids))
            .order_by(FaceTrackSample.t_s.asc())
        ).all():
            by_track.setdefault(str(sample.track_label or ""), []).append(sample)
    return list(tracks), by_track


def _mask_evidence(
    db: Session, workspace_id: str, run: MediaIntelRun
) -> list[dict]:
    """Mask rows + the file facts a mask check must not guess (contracts §9)."""
    rows = db.scalars(
        select(MaskAsset)
        .where(MaskAsset.workspace_id == str(workspace_id), MaskAsset.run_id == str(run.id))
        .order_by(MaskAsset.created_at.asc())
    ).all()
    out: list[dict] = []
    for row in rows:
        path = asset_media_path(db, workspace_id, row.mask_asset_id)
        entry = {
            "kind": str(row.kind or ""),
            "mask_asset_id": str(row.mask_asset_id or ""),
            "format": str(row.format or ""),
            "width": row.width,
            "height": row.height,
            "area_ratio": _round(_num(row.area_ratio), 6),
            "checksum": str(row.checksum or ""),
            "path": path or "",
        }
        if not row.mask_asset_id or not path:
            entry["missing_file"] = True
        elif str(row.checksum or ""):
            entry["checksum_mismatch"] = _mask_file_checksum(path) != str(row.checksum)
        out.append(entry)
    return out


def _speaker_map_rows(db: Session, workspace_id: str, run: MediaIntelRun) -> list[dict]:
    rows = db.scalars(
        select(ActiveSpeakerMap)
        .where(ActiveSpeakerMap.workspace_id == str(workspace_id),
               ActiveSpeakerMap.run_id == str(run.id))
        .order_by(ActiveSpeakerMap.start_s.asc())
    ).all()
    return [
        {"status": str(r.status or ""), "reason": str(r.reason or ""),
         "start_s": float(r.start_s or 0.0), "end_s": float(r.end_s or 0.0),
         "speaker_id": r.speaker_id, "face_track_id": r.face_track_id}
        for r in rows
    ]


def run_visual_qc(
    db: Session,
    ws: Any,
    run: MediaIntelRun | ReframePlan,
    *,
    plan: ReframePlan | None = None,
    luma: Sequence[dict] | None = None,
    video_path: str | Path | None = None,
    source_aspect: str = "16:9",
    anchor: str = KEYFRAME_ANCHOR,
    sample_fps: float = DEFAULT_SAMPLE_FPS,
    persist: bool = True,
) -> dict:
    """``VisualIntelligenceQC`` -- the visual verdict for a run or a plan.

    Accepts either a ``MediaIntelRun`` (its plan and evidence are looked up) or
    a ``ReframePlan`` directly (lanes C/D/G hand the plan they are about to
    apply). Checks, in contract order: ``missing_subject``, ``unstable_crop``,
    ``face_lost``, ``mask_failure``, ``excessive_crop_movement``,
    ``black_frames`` -- plus ``unresolved_dependency`` when the plan leans on
    an active-speaker map that is not resolved.

    ``luma`` lets a caller inject already-measured per-frame luma (a preview
    render measured elsewhere); without it the frames are decoded here through
    ``ffmpeg_util`` and the black-frame check is a REAL measurement.
    """
    is_plan = isinstance(run, ReframePlan)
    if not is_plan and run is None:
        raise ValueError("run_visual_qc needs a MediaIntelRun or a ReframePlan")
    if is_plan:
        plan = run  # type: ignore[assignment]
        run = db.scalar(
            select(MediaIntelRun).where(MediaIntelRun.id == str(plan.run_id))
        ) if plan.run_id else None
    workspace_id = str(getattr(ws, "id", ws) or (run.workspace_id if run else plan.workspace_id))
    if is_plan and run is None:
        # a plan with no run row: judge the plan geometry only, honestly
        # (no face/mask/speaker evidence exists, so those checks stay UNKNOWN)
        keyframes = db.scalars(
            select(ReframeKeyframe)
            .where(ReframeKeyframe.plan_id == str(plan.id),
                   ReframeKeyframe.workspace_id == str(workspace_id))
            .order_by(ReframeKeyframe.t_s.asc())
        ).all()
        motion = crop_motion(keyframes, target_aspect=plan.aspect,
                             source_aspect=source_aspect, anchor=anchor)
        checks = [
            check_missing_subject(None),
            check_unstable_crop(motion),
            check_face_lost(None),
            check_mask_failure(None),
            check_excessive_crop_movement(motion),
            check_black_frames(luma),
        ]
        summary = aggregate_verdict(checks)
        return _visual_payload(
            db, workspace_id, str(plan.id), checks, summary, persist=False,
            measured={"plan": {"plan_id": str(plan.id), "aspect": str(plan.aspect or "")},
                      "motion": motion, "run_id": None},
            plan_id=str(plan.id),
        )
    keyframes, context = _plan_keyframes(db, workspace_id, run, plan)
    aspect = str(context.get("aspect") or (plan.aspect if plan is not None else "9:16"))
    tracks, samples_by_track = _face_evidence(db, workspace_id, run)
    samples = [s for rows in samples_by_track.values() for s in rows]
    motion = crop_motion(keyframes, target_aspect=aspect,
                         source_aspect=source_aspect, anchor=anchor)
    coverage = subject_coverage(keyframes, samples, target_aspect=aspect,
                                source_aspect=source_aspect, anchor=anchor,
                                source_width=_num(context.get("source_width")),
                                source_height=_num(context.get("source_height")))
    reports = track_reports(tracks, samples_by_track, sample_fps=sample_fps)
    masks = _mask_evidence(db, workspace_id, run)
    speaker_rows = _speaker_map_rows(db, workspace_id, run)
    path = video_path
    if path is None:
        path = asset_media_path(db, workspace_id, run.output_asset_id) or (
            asset_media_path(db, workspace_id, run.asset_id))
    frames = list(luma) if luma is not None else frame_luma(path)

    checks = [
        check_missing_subject(coverage),
        check_unstable_crop(motion),
        check_face_lost(reports),
        check_mask_failure(masks),
        check_excessive_crop_movement(motion),
        check_black_frames(frames),
        check_unresolved_dependency(speaker_rows),
    ]
    summary = aggregate_verdict(checks)
    measured = {
        "plan": context,
        "motion": motion,
        "subject": coverage,
        "face_tracks": reports,
        "masks": len(masks),
        "video_path": str(path or ""),
    }
    return _visual_payload(
        db, workspace_id, str(run.id), checks, summary, measured=measured,
        persist=persist, plan_id=str(plan.id) if plan is not None else "",
    )


def _visual_payload(
    db: Session,
    workspace_id: str,
    run_id: str,
    checks: Sequence[QCCheck],
    summary: dict,
    *,
    measured: dict,
    persist: bool,
    plan_id: str = "",
) -> dict:
    payload = {
        "workspace_id": workspace_id,
        "run_id": run_id,
        "kind": "visual",
        "checks": [c.as_dict() for c in checks],
        "failures": summary["failures"],
        "review_reasons": summary["review_reasons"],
        "warnings": summary["warnings"],
        "unknown": summary["unknown"],
        "verdict": summary["verdict"],
        "measured": measured,
        "plan_id": plan_id,
        "persisted": False,
        "events": [_qc_event(workspace_id, run_id, "visual", summary, "")],
    }
    if persist and run_id:
        row = _persist(db, workspace_id, run_id, "visual", checks, summary)
        payload["id"] = row.id
        payload["persisted"] = True
        payload["created_at"] = row.created_at.isoformat() + "Z" if row.created_at else None
        payload["events"] = [_qc_event(workspace_id, run_id, "visual", summary, row.id)]
    return payload


# ---------------------------------------------------------------------------
# override + apply gate (contracts §13)
# ---------------------------------------------------------------------------


def record_override(
    db: Session,
    ws: Any,
    result: IntelQCResult,
    *,
    by_user: str,
    reason: str,
    checks: Sequence[str] | None = None,
) -> dict:
    """Record an attributable override of one QC verdict (append-only).

    Requires a non-empty ``reason`` and a ``by_user``: an override with no
    author or no reason is not an override, it is an unrecorded bypass, so
    :class:`ValueError` is raised instead. The new row COPIES the overridden
    verdict's checks and appends a ``qc_override`` entry naming who overrode
    what (the failing check names) and why. The measured verdict is preserved.
    """
    workspace_id = str(getattr(ws, "id", ws) or result.workspace_id)
    if result.workspace_id != workspace_id:
        raise QCApplyBlocked(
            f"QC result {result.id} does not belong to workspace {workspace_id}",
            verdict=str(result.verdict or ""), run_id=str(result.run_id or ""),
            kind=str(result.kind or ""), needs_override=False,
        )
    if not str(by_user or "").strip():
        raise ValueError("a QC override must name the user who made it")
    text = " ".join(str(reason or "").split())
    if not text:
        raise ValueError("a QC override must state why it was made")
    from app.models.base import utcnow

    failing = [
        c.get("name") for c in (result.checks_json or [])
        if isinstance(c, dict) and c.get("verdict") == "VIOLATION"
    ]
    extra = {
        "name": OVERRIDE_CHECK_NAME,
        "ok": True,
        "verdict": "OK",
        "severity": SEVERITY_REVIEW,
        "measured": None,
        "threshold": None,
        "comparator": "attributable human override",
        "reason": f"override by {str(by_user)[:64]}: {text[:200]}",
        "method": "api",
        "evidence": {
            "by": str(by_user),
            "reason": text[:500],
            "at": utcnow().isoformat() + "Z",
            "overridden_verdict": str(result.verdict or ""),
            "result_id": str(result.id),
            "checks": [str(c) for c in (checks or failing)],
        },
    }
    summary = aggregate_verdict([], override={"by": by_user})
    summary["verdict"] = str(result.verdict or "PASS")
    row = _persist(db, workspace_id, result.run_id, str(result.kind or "audio"),
                   [dict(c) for c in (result.checks_json or []) if isinstance(c, dict)],
                   summary, extra=extra)
    payload = qc_dto(row)
    payload["events"] = [
        _event(
            EVENT_QC_OVERRIDDEN,
            f"media-intel {result.kind} QC override on {str(result.run_id)[:8]}",
            level="warning",
            payload={
                "run_id": str(result.run_id), "kind": str(result.kind),
                "result_id": str(result.id), "override_result_id": str(row.id),
                "overridden_verdict": str(result.verdict or ""),
                "by": str(by_user), "reason": text[:200],
                "checks": [str(c) for c in (checks or failing)],
                "workspace_id": workspace_id,
            },
        )
    ]
    return payload


def _resolve_target(db: Session, workspace_id: str, target: Any) -> IntelQCResult | None:
    """Accept a result row, a run id, a plan id or a dict -> the latest result."""
    if target is None:
        return None
    if isinstance(target, IntelQCResult):
        return target if target.workspace_id == str(workspace_id) else None
    if isinstance(target, MediaIntelRun):
        return get_result(db, workspace_id, str(target.id))
    if isinstance(target, ReframePlan):
        if target.workspace_id != str(workspace_id):
            return None
        run = db.scalar(
            select(MediaIntelRun).where(MediaIntelRun.id == str(target.run_id))
        ) if target.run_id else None
        return get_result(db, workspace_id, str(run.id)) if run is not None else None
    if isinstance(target, dict):
        for key in ("result_id", "id"):
            if target.get(key):
                row = db.get(IntelQCResult, str(target[key]))
                if row is not None and row.workspace_id == str(workspace_id):
                    return row
        for key in ("run_id", "plan_id"):
            if target.get(key):
                found = get_result(db, workspace_id, str(target[key]))
                if found is not None:
                    return found
        return None
    return get_result(db, workspace_id, str(target))


def assert_qc_allows_apply(
    db: Session,
    ws: Any,
    target: Any,
    *,
    override: bool = False,
) -> dict:
    """The apply gate: may this plan/result be applied? (contracts §13)

    ``PASS`` and ``PASS_WITH_WARNINGS`` apply freely; ``REVIEW_REQUIRED``
    applies (a human has already been told); ``FAIL`` requires BOTH
    ``override=True`` (the caller proved it holds the override capability) AND
    a recorded, attributable override. Anything else raises
    :class:`QCApplyBlocked` with the failing check names.

    Returns the allow-decision dict on success so the caller can log which
    verdict it acted on.
    """
    workspace_id = str(getattr(ws, "id", ws) or "")
    result = _resolve_target(db, workspace_id, target)
    if result is None:
        raise QCApplyBlocked(
            "no QC verdict for this target -- run QC before applying",
            needs_override=False,
        )
    verdict = str(result.verdict or "PASS")
    checks = list(result.checks_json or [])
    failures = [
        str(c.get("name")) for c in checks
        if isinstance(c, dict) and c.get("verdict") == "VIOLATION"
        and c.get("severity") == SEVERITY_HARD
    ]
    recorded = overrides_of(result)
    override_entry = recorded[-1] if recorded else None
    decision = {
        "allowed": True,
        "verdict": verdict,
        "result_id": str(result.id),
        "run_id": str(result.run_id or ""),
        "kind": str(result.kind or ""),
        "failures": failures,
        "override_used": bool(override_entry),
        "override": override_entry,
    }
    if verdict != "FAIL":
        return decision
    if not override:
        raise QCApplyBlocked(
            f"QC verdict FAIL ({', '.join(failures[:3]) or 'hard violation'}); "
            "an explicit override is required to apply this plan",
            verdict=verdict, run_id=str(result.run_id or ""), result_id=str(result.id),
            kind=str(result.kind or ""), failures=failures, needs_override=True,
        )
    if not override_entry:
        raise QCApplyBlocked(
            f"QC verdict FAIL with no recorded override for result {result.id}; "
            "record one (POST /qc/{run_id}/override) before applying",
            verdict=verdict, run_id=str(result.run_id or ""), result_id=str(result.id),
            kind=str(result.kind or ""), failures=failures, needs_override=True,
        )
    return decision


__all__ = [
    "BLACK_FRAME_MAX_LUMA",
    "BLACK_FRAME_MEAN_LUMA",
    "BLACK_FRAME_RATIO_FAIL",
    "BLACK_FRAME_RATIO_WARN",
    "CHECK_SEVERITIES",
    "CHECK_STATUSES",
    "CROP_JITTER_FAIL_PER_S2",
    "CROP_JITTER_WARN_PER_S2",
    "CROP_MOVEMENT_CLAMP_PER_SEC",
    "CLIP_HARD_PEAK_DBFS",
    "CLIP_WARN_PEAK_DBFS",
    "DEFAULT_SAMPLE_FPS",
    "DURATION_DRIFT_FAIL_RATIO",
    "DURATION_DRIFT_WARN_RATIO",
    "EVENT_QC_COMPLETED",
    "EVENT_QC_OVERRIDDEN",
    "FACE_COVERAGE_MIN",
    "FACE_GAP_FAIL_S",
    "FACE_GAP_WARN_S",
    "KEYFRAME_ANCHOR",
    "MASK_MIN_AREA_RATIO",
    "MASK_MIN_DIMENSION",
    "MAX_REMOVAL_RATIO",
    "MISSING_AUDIO_LUFS_FLOOR",
    "MISSING_AUDIO_SILENCE_RATIO",
    "OVERRIDE_CHECK_NAME",
    "QCApplyBlocked",
    "QCCheck",
    "SEVERITY_HARD",
    "SEVERITY_REVIEW",
    "SEVERITY_WARN",
    "SUBJECT_COVERAGE_HARD",
    "SUBJECT_COVERAGE_MIN",
    "TRANSCRIPT_RATE_DRIFT_FAIL",
    "TRANSCRIPT_WORD_COUNT_FAIL",
    "VISUAL_RUN_KINDS",
    "aggregate_verdict",
    "assert_qc_allows_apply",
    "asset_media_path",
    "audio_measurements",
    "check_black_frames",
    "check_clipping",
    "check_duration_drift",
    "check_excessive_crop_movement",
    "check_excessive_removed_speech",
    "check_face_lost",
    "check_mask_failure",
    "check_missing_audio",
    "check_missing_subject",
    "check_transcript_mismatch",
    "check_unresolved_dependency",
    "check_unstable_crop",
    "crop_motion",
    "crop_rect_at",
    "frame_luma",
    "get_result",
    "is_black_frame",
    "list_results",
    "overrides_of",
    "qc_dto",
    "record_override",
    "run_audio_qc",
    "run_visual_qc",
    "subject_coverage",
    "track_reports",
]

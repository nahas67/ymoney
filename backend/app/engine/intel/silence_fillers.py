"""Silence, dead air, filler words and false starts -- proposals, never edits.

Contracts §7 (non-destructive) + §13 (the removal-ratio QC flag) + the Work 02
integration rule ("cuts become split_item+delete_item+move_item; never a second
audio-edit timeline"). This module is the ONLY place that turns a media
measurement into a timeline mutation, and it obeys three hard rules:

* **Derived only.** Nothing here writes, trims or re-encodes the source media.
  A detection pass only READS the asset through
  :mod:`app.engine.intel.ffmpeg_util`; the bytes on disk are never opened for
  writing. A cut is a set of Work 02 operations the editor may later submit.
* **Proposals, not mutations.** Every finding becomes an ``edit_proposals`` row
  with ``status=PROPOSED`` and ``decision=NULL``. ``policy.auto_apply`` defaults
  to **OFF**: nothing becomes applicable until an operator calls
  :func:`decide_proposal`.
* **One mapping function.** :meth:`TimeMap.map_time` / :meth:`TimeMap.map_range`
  is the single source↔edited translation. Audio clips, caption clips, text
  overlays, scenes and the UI all go through it, so a cut cannot desynchronise
  one consumer and not another.

Where the per-proposal evidence lives (and why)
-----------------------------------------------
``edit_proposals`` (contracts §2) has no evidence column, and ``ops_json`` is
documented as "the canonical Work 02 operations the apply step would submit".
Rather than overloading ``ops_json`` with a dict, the evidence is written to
``media_intel_runs.metrics_json["evidence"][<proposal_id>]`` and re-joined by
:func:`proposal_dto`. The cut range is NEVER trusted from there: it is recomputed
by :func:`cut_range_for`, a pure function of
``(kind, start_s, end_s, policy)``, so a stale row cannot inject a cut.

Reason vocabulary (stable codes, contracts §7 "reason code")
===========================================================
=============================  ============================================
``DEAD_AIR``                   a measured silent span, interior removable
``DEAD_AIR_SHORTEN``           a long silent span, bounded air is kept
``SILENCE_KEPT``               a measured silent span the policy protects
``FILLER_WORD``                lexicon hit (see ``FILLER_LEXICON``)
``STUTTER``                    an immediately repeated / extended token
``REPEATED_PHRASE``            the same phrase restarted inside a short window
``MAX_REMOVAL_RATIO``          forced to KEEP: the plan removed too much
                               (``evidence.force_keep``, QC verdict FAIL)
=============================  ============================================

The *reason* says WHAT was detected; the *kind* (``REMOVE_RANGE`` /
``SHORTEN_RANGE`` / ``KEEP``) says what to do about it. ``MAX_REMOVAL_RATIO``
overrides the detection reason because the policy, not the detector, decided.

Silence boundary rule (the ffmpeg caveat, measured on ffmpeg 8.1.1)
------------------------------------------------------------------
``ffmpeg_util.detect_silence`` documents that a silent MEDIA START may carry no
matching ``silence_start``, so ``start_s == 0`` has to be treated as a boundary
rather than as an ordinary interior range. Measured on this build (asserted in
``backend/tests/test_silence_fillers.py``):

* leading / fully silent media DOES emit ``silence_start: 0``;
* an interior gap emits ``silence_start: 3`` / ``silence_end: 5.000021``;
* trailing silence emits both lines normally.

So a range is a media boundary when ``start_s <= 1e-6`` (head) or
``end_s >= duration - 1e-3`` (tail), and a head range that the detector did NOT
report is never invented: when the first reported range starts at ``s > 0`` the
head ``[0, s)`` is treated as containing audio and is never a removal candidate.
Padding (``keep_padding_s``) is applied only on the side that faces SPEECH, so a
leading dead-air cut still keeps its lead-in, and the fixture gap ``[3, 5)`` with
``keep_padding_s=0.15`` yields the cut ``(3.15, 4.85)``.

Why a cut is split/delete/move and not a rewrite
------------------------------------------------
``engine.timeline_ops`` has no "cut range" operation, so a cut on an audio clip
is expressed as ``split_item`` at the range edges, ``delete_item`` of the middle
piece and ``move_item`` to close the gap. Every generated op type is asserted to
be inside :data:`app.engine.timeline_ops.OP_TYPES`, and the batch is submitted
through the existing ``POST /timelines/{id}/operations`` path (see
``api/v1/media_intel_edits.py``) so the Work 02 ``base_version`` optimistic
concurrency gate still applies: a stale base version is a 409, never a parallel
save path.

The removal-ratio signal belongs to Lane H (contracts §13)
-----------------------------------------------------------
The over-ratio decision has exactly ONE implementation: this lane calls
:func:`app.engine.intel.qc.check_excessive_removed_speech` and persists the
``QCCheck`` it returns, so ``checks_json`` carries H's own shape -- ``verdict
VIOLATION`` + ``severity HARD`` + ``evidence["force_keep"] = True`` -- with
``verdict=FAIL`` on the row. The API route then consults
``assert_qc_allows_apply`` before it touches a timeline, so a FAIL blocks the
apply until a recorded, attributable override exists.

Two deliberate consequences:

* **This lane's ``max_removal_ratio`` default (0.25) is STRICTER than QC's
  (0.35).** The policy limit is passed EXPLICITLY into H's check, so the two can
  never disagree about a given plan: this lane's gate fires first (it refuses to
  even propose a cut), QC's is the recorded, auditable verdict.
* **If ``app.engine.intel.qc`` cannot be imported, the safety behaviour does not
  change.** Forcing KEEP, the ``force_keep`` evidence and the FAIL verdict in the
  returned payload are all computed here; only the persisted QC ROW is skipped
  (with a logged warning). A plan can never be applied destructively just
  because the QC module is missing.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.engine.intel import ffmpeg_util
from app.engine.timeline_ops import OP_TYPES
from app.models import (
    AudioTimeMap,
    ContentTimeline,
    DiarizationSegment,
    EditProposal,
    IntelQCResult,
    MediaAsset,
    MediaIntelRun,
    MediaIntelWord,
)
from app.models.base import utcnow
from app.models.media_intel import (
    PROPOSAL_DECISIONS,
    PROPOSAL_KINDS,
    PROPOSAL_STATUSES,
)

logger = logging.getLogger("ymoney.intel")

try:  # Lane H owns the removal-ratio SIGNAL (contracts §13); see the docstring
    from app.engine.intel import qc as intel_qc
except Exception as _qc_import_error:  # pragma: no cover - sibling lane absent
    intel_qc = None  # type: ignore[assignment]

# ---------------------------------------------------------------------------
# event kinds -- the ROUTE emits these AFTER db.commit(); the orchestrator
# whitelists them in services/webhooks.py::WEBHOOK_EVENTS (this lane must not
# edit that file). record_event opens its own session, so an engine must never
# call it mid-transaction (contracts §3).
# ---------------------------------------------------------------------------
EVENT_SILENCE_PROPOSED = "MEDIA_INTEL_SILENCE_PROPOSED"
EVENT_FILLERS_PROPOSED = "MEDIA_INTEL_FILLERS_PROPOSED"
EVENT_PROPOSAL_DECIDED = "MEDIA_INTEL_PROPOSAL_DECIDED"
EVENT_EDITS_APPLIED = "MEDIA_INTEL_EDITS_APPLIED"

#: provider identity of the two detection passes (contracts §2 manifest).
SILENCE_PROVIDER = "ffmpeg_silencedetect"
SILENCE_MODEL = "silencedetect"

#: the stable ``reason`` vocabulary (see the module docstring).
REASONS: tuple[str, ...] = (
    "DEAD_AIR",
    "DEAD_AIR_SHORTEN",
    "SILENCE_KEPT",
    "FILLER_WORD",
    "STUTTER",
    "REPEATED_PHRASE",
    "MAX_REMOVAL_RATIO",
)

#: a measured silence shorter than this is not dead air worth naming.
MIN_DEAD_AIR_S = 0.2
#: a cut shorter than this is not worth an operation (protects the batch from
#: hundreds of sub-frame trims).
MIN_CUT_S = 0.05
#: float tolerance for "these two times are the same instant".
EPS = 1e-6
#: seconds of precision persisted in a time map.
MAP_PRECISION = 6
#: the Work 02 batch ceiling (``apply_operations`` refuses more).
MAX_BATCH_OPERATIONS = 200

#: filler lexicon, versioned. Bumping FILLER_LEXICON_VERSION changes the run
#: cache key on purpose: a different lexicon is different work.
FILLER_LEXICON_VERSION = "1.0.0"
#: core lexicon -- disyllabic hesitations and fixed multi-word fillers.
#: Unambiguous in edited speech, so they are safe to propose.
FILLER_LEXICON: tuple[str, ...] = (
    "uh", "um", "er", "erm", "erh", "ah", "ahh", "eh",
    "hmm", "hmh", "mhm", "mm", "mmm", "uhh", "umm",
    "you know", "i mean", "sort of", "kind of", "you see",
)
#: context-dependent words: real content far more often than filler. OFF unless
#: ``policy.include_weak_fillers`` is set, and reduced in confidence when on.
WEAK_FILLER_LEXICON: tuple[str, ...] = (
    "basically", "literally", "actually", "anyway", "right", "okay", "like", "so", "just",
)
#: provider identity of the filler pass.
FILLER_PROVIDER = "transcript_lexicon"
FILLER_MODEL = FILLER_LEXICON_VERSION

#: confidence per evidence class. Word-level evidence is timed; cue-level
#: evidence is not (a caption cue is the smallest timed unit available), hence
#: the lower numbers. Stated, not implied.
CONFIDENCE: dict[str, dict[str, float]] = {
    "word": {"lexicon": 0.75, "stutter": 0.7, "repeat": 0.6},
    "cue": {"lexicon": 0.55, "stutter": 0.5, "repeat": 0.45},
}
#: multiplier applied to a weak-lexicon hit.
WEAK_CONFIDENCE_FACTOR = 0.7

#: audio tracks a cut may touch (Work 02 track kinds).
AUDIO_TRACKS: tuple[str, ...] = ("voice", "music", "sfx")
#: overlay tracks that must follow the audio so the render stays in sync.
SYNCED_OVERLAY_TRACKS: tuple[str, ...] = ("caption", "text")

_NON_WORD = re.compile(r"[^a-z0-9']+")


def _norm(token: str) -> str:
    """Lowercase and strip punctuation: ``"Uh,"`` / ``"uh..."`` -> ``"uh"``."""
    return _NON_WORD.sub("", str(token or "").lower())


def _r(value: float) -> float:
    return round(float(value), MAP_PRECISION)


# ---------------------------------------------------------------------------
# policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EditPolicy:
    """Every knob of contracts §7, with the honest defaults.

    ``auto_apply`` is **False**: a detection pass only proposes. ``filler_policy``
    is a *recommendation* -- it is written to ``decision`` only when
    ``auto_apply`` is set; otherwise the operator decides.
    """

    #: a silent span shorter than this is not dead air (the detector's ``d=``).
    min_silence_s: float = 0.8
    #: ``silencedetect=noise=<n>dB`` -- the level that counts as silence.
    noise_db: float = -50.0
    #: air kept on the side of a cut that faces speech, so no word is clipped.
    keep_padding_s: float = 0.15
    #: a silent span longer than this is SHORTENed, keeping
    #: ``max_silence_kept_s`` of air. ``0.0`` disables (always REMOVE).
    long_silence_s: float = 0.0
    max_silence_kept_s: float = 0.5
    #: keep | remove | shorten -- what to propose for a filler hit.
    filler_policy: str = "remove"
    #: SHORTEN keeps this much of the range start and cuts the remainder.
    shorten_to_s: float = 0.4
    #: include the context-dependent lexicon (off by default).
    include_weak_fillers: bool = False
    #: a phrase repeated inside this window is a false start.
    repeat_window_s: float = 3.0
    #: n-gram sizes tried for a false start, longest first.
    repeat_phrase_words: tuple[int, ...] = (4, 3, 2)
    #: a repetition within this many tokens counts as an adjacent stutter.
    stutter_window_words: int = 2
    #: total cuts may not exceed this share of the media duration. Exceeding it
    #: forces KEEP on the WHOLE plan and records Lane H's
    #: ``excessive_removed_speech`` FAIL verdict with ``evidence.force_keep``.
    max_removal_ratio: float = 0.25
    #: OFF by default -- nothing becomes applicable without a decision.
    auto_apply: bool = False
    audio_tracks: tuple[str, ...] = AUDIO_TRACKS
    overlay_tracks: tuple[str, ...] = SYNCED_OVERLAY_TRACKS

    # -- serialisation ----------------------------------------------------

    def to_dict(self) -> dict:
        """JSON-safe policy (the cache-key input and the API echo)."""
        return {k: (list(v) if isinstance(v, tuple) else v) for k, v in asdict(self).items()}

    @property
    def policy_id(self) -> str:
        """Stable 32-hex id: same policy => same cache key and time-map key."""
        canonical = json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]

    # -- validation -------------------------------------------------------

    def validate(self) -> EditPolicy:
        """Raise ``ValueError`` on an out-of-range policy (routes answer 422)."""
        if self.min_silence_s <= 0:
            raise ValueError("min_silence_s must be > 0")
        if not -120.0 < self.noise_db <= 0.0:
            raise ValueError("noise_db must be in (-120, 0] dB")
        if self.keep_padding_s < 0:
            raise ValueError("keep_padding_s must be >= 0")
        if self.long_silence_s < 0:
            raise ValueError("long_silence_s must be >= 0")
        if self.max_silence_kept_s <= 0:
            raise ValueError("max_silence_kept_s must be > 0")
        if self.filler_policy not in PROPOSAL_DECISIONS:
            raise ValueError(f"filler_policy must be one of {list(PROPOSAL_DECISIONS)}")
        if self.shorten_to_s <= 0:
            raise ValueError("shorten_to_s must be > 0")
        if self.repeat_window_s <= 0:
            raise ValueError("repeat_window_s must be > 0")
        if self.stutter_window_words < 1:
            raise ValueError("stutter_window_words must be >= 1")
        if not 0.0 < self.max_removal_ratio <= 1.0:
            raise ValueError("max_removal_ratio must be in (0, 1]")
        if any(int(size) < 1 for size in self.repeat_phrase_words):
            raise ValueError("repeat_phrase_words entries must be >= 1")
        return self

    #: numeric fields coerced from JSON, and their identity defaults.
    _NUMERIC = (
        "min_silence_s", "noise_db", "keep_padding_s", "long_silence_s",
        "max_silence_kept_s", "shorten_to_s", "repeat_window_s",
        "stutter_window_s", "max_removal_ratio",
    )
    _TUPLES = ("repeat_phrase_words", "audio_tracks", "overlay_tracks")
    _BOOLS = ("include_weak_fillers", "auto_apply")

    @classmethod
    def from_dict(cls, data: dict | None) -> EditPolicy:
        """Build from an API body. Unknown keys and bad values are 422-worthy."""
        payload = dict(data or {})
        unknown = sorted(set(payload) - set(cls.__dataclass_fields__))
        if unknown:
            raise ValueError(f"unknown policy key(s): {', '.join(unknown)}")
        kwargs: dict[str, Any] = {}
        for key, value in payload.items():
            if key in cls._TUPLES:
                if not isinstance(value, (list, tuple)):
                    raise ValueError(f"policy.{key} must be a list")
                kwargs[key] = tuple(str(v) for v in value)
            elif key in cls._BOOLS:
                kwargs[key] = bool(value)
            elif key == "filler_policy":
                kwargs[key] = str(value or "").strip().lower()
            elif key in cls._NUMERIC:
                try:
                    kwargs[key] = float(value)
                except (TypeError, ValueError):
                    raise ValueError(f"policy.{key} must be a number") from None
        return cls(**kwargs).validate()


DEFAULT_POLICY = EditPolicy()


# ---------------------------------------------------------------------------
# time map
# ---------------------------------------------------------------------------


class TimeMap:
    """Ordered ``{src_start, src_end, out_start, out_end}`` segments.

    The kept regions tile the OUTPUT timeline; the gaps between them are the
    cuts. :meth:`map_time` is monotone non-decreasing, which is what stops
    re-timed caption clips from overlapping, and it clamps a time that falls
    INSIDE a removed gap onto the cut point (that clamp is lossy by
    construction: a removed instant has no edited coordinate).
    """

    __slots__ = ("segments", "source_duration_s")

    def __init__(self, segments: Sequence[dict], *, source_duration_s: float = 0.0) -> None:
        self.segments: list[dict] = [
            {
                "src_start": _r(seg["src_start"]),
                "src_end": _r(seg["src_end"]),
                "out_start": _r(seg["out_start"]),
                "out_end": _r(seg["out_end"]),
            }
            for seg in segments
        ]
        self.source_duration_s = _r(source_duration_s)

    # -- construction -----------------------------------------------------

    @classmethod
    def from_cut_ranges(
        cls, cuts: Iterable[tuple[float, float]], source_duration_s: float
    ) -> TimeMap:
        """Build the map from the source-time ranges that will be REMOVED."""
        total = _r(source_duration_s)
        merged: list[list[float]] = []
        for raw in cuts or []:
            a, b = _r(raw[0]), _r(raw[1])
            a = max(0.0, a)
            b = min(total, b) if total > 0 else b
            if b - a <= MIN_CUT_S:
                continue  # too small to be worth an operation
            if merged and a <= merged[-1][1] + MIN_CUT_S:
                merged[-1][1] = max(merged[-1][1], b)
            else:
                merged.append([a, b])
        merged.sort()
        segments: list[dict] = []
        cursor, out = 0.0, 0.0
        for a, b in [*merged, [total, total]]:
            if a > cursor + MIN_CUT_S:
                segments.append({
                    "src_start": _r(cursor), "src_end": _r(a),
                    "out_start": _r(out), "out_end": _r(out + (a - cursor)),
                })
                out += a - cursor
            cursor = max(cursor, b)
        return cls(segments, source_duration_s=total)

    @classmethod
    def from_persisted(cls, row: AudioTimeMap | None) -> TimeMap | None:
        """Rebuild from a stored ``audio_time_maps`` row (``None`` passes through)."""
        if row is None:
            return None
        segments = list(row.segments_json or [])
        duration = max((_r(seg.get("src_end", 0.0)) for seg in segments), default=0.0)
        return cls(segments, source_duration_s=duration)

    # -- properties -------------------------------------------------------

    @property
    def removals(self) -> list[tuple[float, float]]:
        """The cut ranges in source time: the complement of the kept segments.

        Includes a LEADING gap (``[0, first.src_start)``) and a TRAILING one
        (``[last.src_end, duration)``), which is what a cut at t=0 or a cut that
        runs to the end of the media produces. A silent head is normally padded
        so the first kept segment starts at 0 and no leading gap appears -- but a
        caller may legitimately cut from the very first instant, and the map must
        describe that honestly.
        """
        gaps: list[tuple[float, float]] = []
        cursor = 0.0
        for seg in self.segments:
            if seg["src_start"] - cursor > EPS:
                gaps.append((_r(cursor), seg["src_start"]))
            cursor = max(cursor, seg["src_end"])
        if self.source_duration_s - cursor > EPS:
            gaps.append((_r(cursor), _r(self.source_duration_s)))
        return gaps

    def removed_before(self, t: float) -> float:
        """Total removed duration STRICTLY BEFORE source instant ``t``.

        This is the shift a clip that starts at ``t`` has already absorbed, and
        it is NOT ``t - map_time(t)``: a cut's own end point is still at its
        unshifted position while that cut is being applied, so the two split
        points of one cut are symmetric.
        """
        point = _r(t)
        return _r(sum(b - a for a, b in self.removals if b <= point + EPS))

    def cut_shifts(self) -> list[tuple[float, float, float]]:
        """The merged cuts as ``(start, end, removed_before)``, ascending.

        ``removed_before`` is the shift already applied when this cut's split
        points have to be placed on a document where earlier cuts were removed
        -- this is what lets two cuts in ONE clip both land on the right
        content (see :func:`_audio_ops_for_clip`).
        """
        out: list[tuple[float, float, float]] = []
        accumulated = 0.0
        for start, end in self.removals:
            out.append((start, end, _r(accumulated)))
            accumulated += end - start
        return out

    @property
    def output_duration_s(self) -> float:
        return _r(self.segments[-1]["out_end"]) if self.segments else 0.0

    @property
    def removed_duration_s(self) -> float:
        return _r(self.source_duration_s - self.output_duration_s)

    @property
    def removal_ratio(self) -> float:
        if self.source_duration_s <= 0:
            return 0.0
        return _r(self.removed_duration_s / self.source_duration_s)

    # -- the one mapping function ----------------------------------------

    def map_time(self, t: float) -> float:
        """Source time -> edited time.

        Inside a kept segment the offset is preserved exactly, so a round trip
        through :meth:`inverse_time` is lossless. Inside a removed gap the
        result is the cut point -- clamped forward onto the next kept
        segment's start, never extrapolated or held back.
        """
        point = _r(t)
        if not self.segments:
            return 0.0
        for seg in self.segments:
            if seg["src_start"] - EPS <= point <= seg["src_end"] + EPS:
                inside = min(max(point, seg["src_start"]), seg["src_end"])
                return _r(seg["out_start"] + (inside - seg["src_start"]))
            if point < seg["src_start"]:
                return seg["out_start"]  # inside a removed gap
        return self.output_duration_s

    def map_range(self, a: float, b: float) -> dict:
        """Source range -> ``{"start", "end", "duration"}`` in edited time."""
        start, end = _r(a), _r(b)
        new_start = self.map_time(start)
        new_end = max(new_start, self.map_time(end))
        return {"start": new_start, "end": new_end, "duration": _r(new_end - new_start)}

    def inverse_time(self, out_t: float) -> float:
        """Edited time -> source time (the honest inverse; exact on kept spans)."""
        point = _r(out_t)
        if not self.segments:
            return 0.0
        for seg in self.segments:
            if seg["out_start"] - EPS <= point <= seg["out_end"] + EPS:
                inside = min(max(point, seg["out_start"]), seg["out_end"])
                return _r(seg["src_start"] + (inside - seg["out_start"]))
        if point >= self.segments[-1]["out_end"] - EPS:
            return self.source_duration_s
        return 0.0

    def to_dict(self, *, policy_id: str = "") -> dict:
        return {
            "policy_id": policy_id,
            "segments": [dict(seg) for seg in self.segments],
            "removals": [{"start_s": a, "end_s": b} for a, b in self.removals],
            "source_duration_s": self.source_duration_s,
            "output_duration_s": self.output_duration_s,
            "removed_duration_s": self.removed_duration_s,
            "removal_ratio": self.removal_ratio,
        }


# ---------------------------------------------------------------------------
# policy -> cut range (pure; recomputed at apply time, never trusted from JSON)
# ---------------------------------------------------------------------------


def cut_range_for(
    kind: str, start_s: float, end_s: float, policy: EditPolicy = DEFAULT_POLICY
) -> tuple[float, float]:
    """The source-time range a proposal actually CUTS. Pure and total.

    * ``REMOVE_RANGE``  -> the whole proposed range;
    * ``SHORTEN_RANGE`` -> everything after the first ``shorten_to_s``: the head
      is kept so the listener keeps the natural onset of the phrase;
    * ``KEEP``          -> an empty cut (nothing is ever cut for a KEEP).
    """
    start, end = _r(start_s), _r(end_s)
    if kind == "REMOVE_RANGE":
        return start, end
    if kind == "SHORTEN_RANGE":
        keep = min(policy.shorten_to_s, max(0.0, end - start))
        return _r(start + keep), end
    return start, start


# ---------------------------------------------------------------------------
# silence detection (real ffmpeg measurement)
# ---------------------------------------------------------------------------


def detect_dead_air(path: str, policy: EditPolicy = DEFAULT_POLICY) -> dict:
    """Measure silent spans with the real ``silencedetect`` filter.

    ``ffmpeg_util.detect_silence`` runs
    ``ffmpeg -i <path> -af silencedetect=noise=<n>dB:d=<s> -f null -``; the result
    is classified here (see the module docstring for the measured boundary rule).
    Padding is applied only on the side that faces speech, so the cut is
    ``(start + pad_left, end - pad_right)`` with ``pad = 0`` on a media boundary.

    Returns ``{"measured", "reason", "source_duration_s", "duration_s", "ranges"}``
    where each range is ``{"start_s", "end_s", "duration_s", "cut_start_s",
    "cut_end_s", "removable_s", "at_media_start", "at_media_end", "kind",
    "reason"}``. ``measured=False`` with a machine ``reason`` when ffmpeg or the
    file is unusable -- honest emptiness, never a fabricated span.
    """
    target = str(path or "")
    empty: dict[str, Any] = {
        "measured": False, "reason": "",
        "source_duration_s": 0.0, "duration_s": 0.0, "ranges": [],
    }
    if not target:
        return {**empty, "reason": "no media path supplied"}
    if not ffmpeg_util.ffmpeg_available():
        return {**empty, "reason": "ffmpeg not on PATH"}
    duration = ffmpeg_util.duration_seconds(target)
    if not duration or duration <= 0:
        return {**empty, "reason": "media duration could not be probed"}
    total = _r(duration)
    raw = ffmpeg_util.detect_silence(
        target, noise_db=policy.noise_db, min_duration=policy.min_silence_s
    )
    if not raw:
        return {
            "measured": True,
            "reason": "no silent span reached the detector threshold",
            "source_duration_s": total, "duration_s": total, "ranges": [],
        }

    ranges: list[dict] = []
    for span in raw:
        start = max(0.0, _r(span["start_s"]))
        end = min(total, _r(span["end_s"]))
        at_start = start <= EPS
        at_end = end >= total - 1e-3
        cut_start = _r(start + (0.0 if at_start else _r(policy.keep_padding_s)))
        cut_end = _r(end - (0.0 if at_end else _r(policy.keep_padding_s)))
        removable = _r(max(0.0, cut_end - cut_start))
        if policy.long_silence_s > 0 and (end - start) > policy.long_silence_s:
            kind, reason = "SHORTEN_RANGE", "DEAD_AIR_SHORTEN"
            cut_end = min(cut_end, _r(cut_start + policy.max_silence_kept_s))
            removable = _r(max(0.0, cut_end - cut_start))
        elif removable < MIN_DEAD_AIR_S:
            kind, reason = "KEEP", "SILENCE_KEPT"
        else:
            kind, reason = "REMOVE_RANGE", "DEAD_AIR"
        ranges.append({
            "start_s": start, "end_s": end, "duration_s": _r(end - start),
            "cut_start_s": cut_start, "cut_end_s": cut_end, "removable_s": removable,
            "at_media_start": at_start, "at_media_end": at_end,
            "kind": kind, "reason": reason,
        })
    ranges.sort(key=lambda item: item["start_s"])
    return {
        "measured": True, "reason": "",
        "source_duration_s": total, "duration_s": total, "ranges": ranges,
    }


# ---------------------------------------------------------------------------
# filler / stutter / false-start detection
# ---------------------------------------------------------------------------


def _tokens(units: Sequence[dict]) -> list[dict]:
    """Timed tokens from units.

    A WORD unit is already one token. A CUE unit carries a whole caption string,
    so it is split on whitespace -- every word inherits the CUE's timing, which is
    why a cue-level finding can only ever claim the cue's range (a caption cue is
    the smallest timed unit available; claiming a sub-word instant would be
    fabricated precision).
    """
    out: list[dict] = []
    index = 0
    for unit in units or []:
        raw = str(unit.get("text") or unit.get("word") or "")
        start = float(unit.get("start_s", 0.0))
        end = float(unit.get("end_s", 0.0))
        parts = raw.split() if str(unit.get("unit") or "") == "cue" else [raw]
        for part in parts:
            norm = _norm(part)
            if not norm:
                continue
            out.append({"i": index, "norm": norm, "text": part,
                        "start_s": start, "end_s": end})
            index += 1
    return out


def _grams(tokens: Sequence[dict], size: int) -> list[list[dict]]:
    return [list(tokens[i:i + size]) for i in range(len(tokens) - size + 1)]


def _is_stutter(first: str, second: str) -> bool:
    """``the the`` / ``th-the`` / ``wa-was`` -> True. Real words rarely repeat."""
    if first == second:
        return True
    short, long = sorted((first, second), key=len)
    if len(short) < 3 or len(long) - len(short) > 2:
        return False
    return long.startswith(short)


def find_fillers(
    units: Sequence[dict], policy: EditPolicy = DEFAULT_POLICY
) -> list[dict]:
    """Filler / stutter / false-start findings over timed text units.

    ``units`` is ``[{"text", "start_s", "end_s"}, ...]`` -- word rows from
    ``media_intel_words`` (unit ``word``) or caption cues (unit ``cue``). Each
    finding is ``{"unit", "reason", "start_s", "end_s", "confidence", "match",
    "token_count", "evidence"}``.

    Rules, in order (the earlier rule claims the tokens, so a later rule never
    double-reports them):

    1. multi-word lexicon phrases, then single tokens;
    2. the same n-gram restarted within ``repeat_window_s`` -- the SECOND
       occurrence is reported, because the first is the real take and the retry
       is the false start. Running BEFORE the stutter rule matters: a restarted
       phrase is a bigger, more useful finding than two isolated repetitions
       inside it;
    3. an immediately repeated / extended token (``STUTTER``) -- word-timed only,
       because a caption cue has no word granularity to repeat.

    Nothing here decides what to DO: :func:`proposal_for_finding` maps findings to
    ``REMOVE_RANGE`` / ``SHORTEN_RANGE`` / ``KEEP``.
    """
    unit_kind = "cue" if any(u.get("unit") == "cue" for u in units or []) else "word"
    tokens = _tokens(units)
    if not tokens:
        return []
    weights = CONFIDENCE[unit_kind]
    lexicon = list(FILLER_LEXICON) + (
        list(WEAK_FILLER_LEXICON) if policy.include_weak_fillers else []
    )
    weak = set(WEAK_FILLER_LEXICON) - set(FILLER_LEXICON)
    by_phrase: dict[tuple[str, ...], list[str]] = {}
    for entry in lexicon:
        by_phrase.setdefault(tuple(_norm(w) for w in entry.split()), []).append(entry)

    findings: list[dict] = []
    consumed: set[int] = set()

    def _add(reason: str, hits: Sequence[dict], confidence: float, detail: dict) -> None:
        consumed.update(h["i"] for h in hits)
        findings.append({
            "unit": unit_kind,
            "reason": reason,
            "start_s": _r(hits[0]["start_s"]),
            "end_s": _r(max(h["end_s"] for h in hits)),
            "confidence": _r(min(1.0, max(0.0, confidence))),
            "match": " ".join(h["text"] for h in hits),
            "token_count": len(hits),
            "evidence": {
                "unit": unit_kind,
                "rule": reason,
                "matched": [h["norm"] for h in hits],
                "tokens": [
                    {"i": h["i"], "text": h["text"],
                     "start_s": _r(h["start_s"]), "end_s": _r(h["end_s"])}
                    for h in hits
                ],
                **detail,
            },
        })

    # 1) lexicon (longest phrase first so "you know" beats a bare "you")
    for size in sorted({len(phrase) for phrase in by_phrase}, reverse=True):
        for gram in _grams(tokens, size):
            if any(t["i"] in consumed for t in gram):
                continue
            key = tuple(t["norm"] for t in gram)
            if key not in by_phrase:
                continue
            is_weak = any(word in weak for word in key)
            confidence = weights["lexicon"] * (WEAK_CONFIDENCE_FACTOR if is_weak else 1.0)
            _add("FILLER_WORD", gram, confidence, {
                "lexicon_version": FILLER_LEXICON_VERSION,
                "lexicon_entry": by_phrase[key][0],
                "weak_lexicon": bool(is_weak),
            })

    # 2) false start -- the same phrase restarted inside a short window
    for size in sorted(set(policy.repeat_phrase_words), reverse=True):
        seen: dict[tuple[str, ...], list[dict]] = {}
        for gram in _grams(tokens, size):
            if any(t["i"] in consumed for t in gram):
                continue
            key = tuple(t["norm"] for t in gram)
            if all(word in FILLER_LEXICON for word in key):
                continue  # a pure filler repeat is already reported as a lexicon hit
            first = seen.get(key)
            if first is None:
                seen[key] = gram
                continue
            # the hesitation is the gap between the two PHRASES, not between the
            # first tokens: a speaker restarts after a pause, not mid-word
            gap = _r(gram[0]["start_s"] - first[-1]["end_s"])
            if gap < 0 or gap > policy.repeat_window_s:
                seen[key] = gram
                continue
            _add("REPEATED_PHRASE", gram, weights["repeat"], {
                "pattern": "restarted_phrase",
                "phrase_words": size,
                "first_start_s": _r(first[0]["start_s"]),
                "first_end_s": _r(first[-1]["end_s"]),
                "gap_s": gap,
                "window_s": policy.repeat_window_s,
            })

    # 3) stutter -- an adjacent repetition, only in a word-timed stream
    if unit_kind == "word":
        window = max(2, int(policy.stutter_window_words) + 1)
        for i in range(len(tokens) - 1):
            first, second = tokens[i], tokens[i + 1]
            if first["i"] in consumed or second["i"] in consumed:
                continue
            if second["i"] - first["i"] > window:
                continue
            if not _is_stutter(first["norm"], second["norm"]):
                continue
            _add("STUTTER", [first, second], weights["stutter"],
                 {"pattern": "adjacent_repetition"})

    findings.sort(key=lambda f: (f["start_s"], f["end_s"]))
    return findings


def proposal_for_finding(finding: dict, policy: EditPolicy = DEFAULT_POLICY) -> dict:
    """Map one finding to ``{kind, start_s, end_s, reason, confidence, evidence}``.

    ``filler_policy`` decides the kind:

    * ``keep``    -> ``KEEP`` (the finding is still reported with its evidence);
    * ``remove``  -> ``REMOVE_RANGE``;
    * ``shorten`` -> ``SHORTEN_RANGE`` when the range is longer than
      ``shorten_to_s``; a range that cannot give anything up is recorded as
      ``KEEP`` with ``evidence.rule = "shorten_noop"`` instead of proposing an
      operation that removes nothing.
    """
    start, end = _r(finding["start_s"]), _r(finding["end_s"])
    evidence = dict(finding.get("evidence") or {})
    if policy.filler_policy == "keep":
        kind = "KEEP"
        evidence["policy"] = "keep"
    elif policy.filler_policy == "shorten":
        if (end - start) > policy.shorten_to_s + EPS:
            kind = "SHORTEN_RANGE"
            evidence["policy"] = "shorten"
            evidence["kept_head_s"] = _r(min(policy.shorten_to_s, end - start))
        else:
            kind = "KEEP"
            evidence["policy"] = "shorten"
            evidence["rule"] = "shorten_noop"
            evidence["detail"] = (
                f"range is {end - start:.3f}s but shorten_to_s is "
                f"{policy.shorten_to_s:.3f}s; shortening cannot remove anything"
            )
    else:
        kind = "REMOVE_RANGE"
        evidence["policy"] = "remove"
    cut_start, cut_end = cut_range_for(kind, start, end, policy)
    evidence["cut_start_s"] = cut_start
    evidence["cut_end_s"] = cut_end
    evidence["cut_duration_s"] = _r(max(0.0, cut_end - cut_start))
    return {
        "kind": kind,
        "start_s": start,
        "end_s": end,
        "reason": str(finding["reason"]),
        "confidence": float(finding.get("confidence") or 0.0),
        "evidence": evidence,
    }


# ---------------------------------------------------------------------------
# word-source resolution (word rows preferred, timed cue source otherwise)
# ---------------------------------------------------------------------------


def resolve_units(
    db: Session,
    workspace_id: str,
    asset_id: str,
    *,
    timeline_id: str | None = None,
    cues: Sequence[dict] | None = None,
) -> dict:
    """Pick the timed text source for filler analysis, and SAY which one.

    Preference order (documented, deterministic):

    1. ``media_intel_words`` of the newest run that has words for the asset --
       the only source with real word timings (unit ``word``);
    2. caption / text overlay clips of ``timeline_id`` (unit ``cue``);
    3. explicit ``cues`` from the request (unit ``cue``);
    4. nothing: ``available=False`` with a machine reason. A bare transcript
       STRING is refused, because inventing word timings from character
       offsets would be fabricated evidence (contracts §0).
    """
    if asset_id:
        rows = list(db.scalars(
            select(MediaIntelWord)
            .where(MediaIntelWord.workspace_id == str(workspace_id),
                   MediaIntelWord.asset_id == str(asset_id))
            .order_by(MediaIntelWord.run_id, MediaIntelWord.idx)
        ).all())
        if rows:
            newest = rows[-1].run_id
            words = [
                {"text": str(row.word or ""), "start_s": float(row.start_s or 0.0),
                 "end_s": float(row.end_s or 0.0), "unit": "word"}
                for row in rows if row.run_id == newest and _norm(row.word or "")
            ]
            if words:
                return {"source": "media_intel_words", "available": True, "reason": "",
                        "unit": "word", "units": words, "run_id": newest}
    if timeline_id:
        timeline = db.get(ContentTimeline, str(timeline_id))
        if timeline is not None and timeline.workspace_id == str(workspace_id):
            doc = dict(timeline.tracks_json or {})
            units = cue_units_from_doc(doc)
            if units:
                return {"source": "caption_cues", "available": True, "reason": "",
                        "unit": "cue", "units": units, "run_id": ""}
    if cues:
        units = [
            {"text": str(cue.get("text") or ""), "start_s": float(cue.get("start_s", 0.0)),
             "end_s": float(cue.get("end_s", 0.0)), "unit": "cue"}
            for cue in cues if str(cue.get("text") or "").strip()
        ]
        if units:
            return {"source": "request_cues", "available": True, "reason": "",
                    "unit": "cue", "units": units, "run_id": ""}
    return {
        "source": "none", "available": False, "unit": "word", "units": [], "run_id": "",
        "reason": (
            "no media_intel_words rows for this asset and no timed cue source; "
            "filler ranges need word or cue timings, and a bare transcript is "
            "refused because its word timings would have to be invented"
        ),
    }


def cue_units_from_doc(doc: dict) -> list[dict]:
    """Caption / text overlay clips as timed cue units (sorted by start)."""
    out: list[dict] = []
    for track in (doc or {}).get("tracks", []) or []:
        if str(track.get("kind") or "") not in SYNCED_OVERLAY_TRACKS:
            continue
        for clip in track.get("clips", []) or []:
            text = str(clip.get("name") or (clip.get("text") or {}).get("content") or "")
            if not text.strip():
                continue
            start = float(clip.get("start", 0.0))
            out.append({"text": text, "start_s": start,
                        "end_s": start + float(clip.get("duration", 0.0)), "unit": "cue"})
    out.sort(key=lambda unit: unit["start_s"])
    return out


# ---------------------------------------------------------------------------
# proposals
# ---------------------------------------------------------------------------


def proposal_dto(row: EditProposal, evidence: dict | None = None) -> dict:
    """The API shape of one proposal (evidence merged in when available)."""
    start = float(row.start_s or 0.0)
    end = float(row.end_s or 0.0)
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "project_id": row.project_id,
        "asset_id": row.asset_id,
        "run_id": row.run_id,
        "kind": str(row.kind or "KEEP"),
        "start_s": start,
        "end_s": end,
        "duration_s": _r(end - start),
        "reason": str(row.reason or ""),
        "confidence": (float(row.confidence) if row.confidence is not None else None),
        "status": str(row.status or "PROPOSED"),
        "decision": row.decision,
        "operations": list(row.ops_json or []),
        "evidence": dict(evidence or {}),
        "decided_by": row.decided_by,
        "decided_at": (row.decided_at.isoformat() + "Z") if row.decided_at else None,
        "created_at": (row.created_at.isoformat() + "Z") if row.created_at else None,
    }


def _run_evidence(db: Session, run_id: str | None) -> dict:
    if not run_id:
        return {}
    run = db.get(MediaIntelRun, str(run_id))
    if run is None:
        return {}
    return dict((run.metrics_json or {}).get("evidence") or {})


def list_proposals(
    db: Session,
    workspace_id: str,
    *,
    asset_id: str | None = None,
    run_id: str | None = None,
    status: str | None = None,
    kind: str | None = None,
    limit: int = 200,
) -> list[dict]:
    """Workspace-scoped proposal listing (a foreign id is simply absent)."""
    query = select(EditProposal).where(EditProposal.workspace_id == str(workspace_id))
    if asset_id:
        query = query.where(EditProposal.asset_id == str(asset_id))
    if run_id:
        query = query.where(EditProposal.run_id == str(run_id))
    if status:
        query = query.where(EditProposal.status == str(status).upper())
    if kind:
        query = query.where(EditProposal.kind == str(kind).upper())
    rows = list(db.scalars(
        query.order_by(EditProposal.created_at, EditProposal.start_s)
        .limit(max(1, min(int(limit), 500)))
    ).all())
    by_run: dict[str, dict] = {}
    out: list[dict] = []
    for row in rows:
        if row.run_id not in by_run:
            by_run[row.run_id] = _run_evidence(db, row.run_id)
        out.append(proposal_dto(row, by_run[row.run_id].get(row.id)))
    return out


def get_proposal(db: Session, workspace_id: str, proposal_id: str) -> EditProposal | None:
    """Workspace-scoped fetch; foreign/missing is ``None`` (-> the route is 404)."""
    row = db.get(EditProposal, str(proposal_id or ""))
    if row is None or row.workspace_id != str(workspace_id):
        return None
    return row


def proposals_for_run(db: Session, run_id: str) -> list[EditProposal]:
    """Every proposal of one run, in media order (test/diagnostic helper)."""
    return list(db.scalars(
        select(EditProposal).where(EditProposal.run_id == str(run_id))
        .order_by(EditProposal.start_s)
    ).all())


def _decision_for(kind: str) -> str:
    if kind == "REMOVE_RANGE":
        return "remove"
    if kind == "SHORTEN_RANGE":
        return "shorten"
    return "keep"


def _insert_proposals(
    db: Session,
    *,
    workspace_id: str,
    project_id: str | None,
    asset_id: str,
    run_id: str,
    entries: Sequence[dict],
    policy: EditPolicy,
) -> list[EditProposal]:
    """Write ``PROPOSED`` rows -- or ``DECIDED`` when ``auto_apply`` is on.

    ``ops_json`` is left empty: the operations depend on the timeline document,
    which is fetched at APPLY time. A preview is attached afterwards when the
    caller supplied a ``timeline_id``.
    """
    rows: list[EditProposal] = []
    for entry in entries:
        if entry["kind"] not in PROPOSAL_KINDS:
            raise ValueError(f"unknown proposal kind {entry['kind']!r}")
        status = "DECIDED" if policy.auto_apply else "PROPOSED"
        if status not in PROPOSAL_STATUSES:
            raise ValueError(f"unknown proposal status {status!r}")
        row = EditProposal(
            workspace_id=str(workspace_id),
            project_id=str(project_id) if project_id else None,
            asset_id=str(asset_id),
            run_id=str(run_id),
            kind=entry["kind"],
            start_s=float(entry["start_s"]),
            end_s=float(entry["end_s"]),
            reason=str(entry["reason"]),
            confidence=(float(entry["confidence"])
                        if entry.get("confidence") is not None else None),
            status=status,
            decision=_decision_for(entry["kind"]) if policy.auto_apply else None,
            ops_json=[],
        )
        db.add(row)
        rows.append(row)
    db.flush()
    return rows


def _counts_by_reason(rows: Sequence[EditProposal]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        counts[str(row.reason or "")] = counts.get(str(row.reason or ""), 0) + 1
    return dict(sorted(counts.items()))


def _counts_summary(counts: dict[str, int]) -> str:
    return ", ".join(f"{key}={value}" for key, value in counts.items()) or "none"


def _qc_flag(ratio_info: dict, policy: EditPolicy) -> dict:
    """The QC surface this lane REPORTS: Lane H's removal-ratio verdict.

    ``verdict`` mirrors :func:`app.engine.intel.qc.check_excessive_removed_speech`
    -- ``FAIL`` with ``force_keep`` once the plan is over the limit -- so the API
    answers exactly what QC answers instead of a second opinion.
    """
    return {
        "kind": "audio",
        "check": "excessive_removed_speech",
        "verdict": "FAIL" if ratio_info["triggered"] else "PASS",
        "force_keep": bool(ratio_info["triggered"]),
        "measured": ratio_info["ratio"],
        "threshold": policy.max_removal_ratio,
        "source": "app.engine.intel.qc" if intel_qc is not None else "local",
    }


def _persist_qc_flag(
    db: Session,
    *,
    workspace_id: str,
    run_id: str,
    policy: EditPolicy,
    ratio_info: dict,
) -> dict:
    """Append Lane H's ``excessive_removed_speech`` verdict for this run.

    The row is produced by :func:`app.engine.intel.qc.check_excessive_removed_speech`
    and aggregated by :func:`~app.engine.intel.qc.aggregate_verdict`, so the
    persisted ``checks_json`` is the same shape H's own QC route writes and the
    same one :func:`~app.engine.intel.qc.assert_qc_allows_apply` reads
    (``verdict=VIOLATION`` + ``severity=HARD`` + ``evidence["force_keep"]``).

    ``policy.max_removal_ratio`` is passed EXPLICITLY: this lane's ceiling
    (0.25 by default) is stricter than QC's built-in 0.35, and the row must
    record the ceiling the plan was actually judged against. No speech-activity
    evidence is claimed here, so H's check runs on the whole-duration
    denominator and its severity stays honest (REVIEW, not HARD, when the
    denominator is unmeasured).

    Returns the stored check dict, or ``{}`` when the QC module is unavailable --
    the caller still gets ``ratio_info`` and the forced KEEP either way.
    """
    if intel_qc is None:  # pragma: no cover - sibling lane absent
        logger.warning(
            "app.engine.intel.qc unavailable: no QC row recorded for run %s "
            "(the force_keep policy still applies)", run_id,
        )
        return {}
    check = intel_qc.check_excessive_removed_speech(
        ratio_info["removed_s"],
        ratio_info.get("speech_activity_s"),
        source_duration_s=ratio_info["source_duration_s"],
        max_removal_ratio=policy.max_removal_ratio,
    )
    summary = intel_qc.aggregate_verdict([check])
    row = IntelQCResult(
        workspace_id=str(workspace_id), run_id=str(run_id), kind="audio",
        verdict=str(summary["verdict"]), checks_json=[check.as_dict()],
    )
    db.add(row)
    db.flush()
    ratio_info["qc_result_id"] = row.id
    ratio_info["qc_verdict"] = str(summary["verdict"])
    return dict(check.as_dict())


def _stamp_metrics(run: MediaIntelRun, metrics: dict) -> None:
    """Write the plan's manifest (incl. per-proposal evidence) onto the run.

    The evidence has to survive here rather than in the returned payload: the
    ``edit_proposals`` table has no evidence column, so
    ``metrics_json["evidence"][<proposal_id>]`` IS the stored evidence and
    :func:`proposal_dto` re-joins it on every read. Writing it from the engine
    (instead of relying on the route) means a non-HTTP caller gets it too; the
    caller's session owns the flush.
    """
    merged = dict(run.metrics_json or {})
    merged.update(metrics)
    run.metrics_json = merged


def speech_activity_seconds(db: Session, workspace_id: str, asset_id: str) -> float | None:
    """Total measured SPEECH seconds for an asset, or ``None`` when unmeasured.

    Lane B's ``diarization_segments`` rows with ``kind='SPEECH_ACTIVITY'`` are
    the only honest denominator available here (they are VAD measurements, NOT
    speakers -- contracts §4). Overlapping segments are merged first so a
    segment emitted by two runs is not double counted.

    This value is what upgrades Lane H's ``excessive_removed_speech`` check from
    ``REVIEW`` to a hard ``FAIL``: with no speech-activity evidence the check
    cannot claim a hard failure, and this lane never invents the baseline.
    """
    rows = db.execute(
        select(DiarizationSegment.start_s, DiarizationSegment.end_s).where(
            DiarizationSegment.workspace_id == str(workspace_id),
            DiarizationSegment.asset_id == str(asset_id),
            DiarizationSegment.kind == "SPEECH_ACTIVITY",
        )
    ).all()
    spans = sorted((float(a), float(b)) for a, b in rows if float(b) > float(a))
    if not spans:
        return None
    merged: list[list[float]] = []
    for start, end in spans:
        if merged and start <= merged[-1][1] + EPS:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return _r(sum(end - start for start, end in merged))


def _enforce_removal_ratio(
    db: Session,
    entries: list[dict],
    policy: EditPolicy,
    duration_s: float,
    *,
    workspace_id: str,
    run_id: str,
    asset_id: str | None = None,
) -> tuple[list[dict], dict]:
    """Force the whole plan to KEEP when it removes too much (contracts §7).

    Silently deleting more speech than the policy allows is the one failure mode
    this feature must not have, so the plan is NOT quietly trimmed: every entry
    becomes ``KEEP`` with reason ``MAX_REMOVAL_RATIO``, its evidence gains
    ``force_keep``, and Lane H's ``excessive_removed_speech`` verdict is recorded
    on the run (contracts §13). :func:`_persist_qc_flag` writes H's own check
    dict, so ``assert_qc_allows_apply`` sees a hard violation for this run.
    """
    cuts = [
        cut_range_for(entry["kind"], entry["start_s"], entry["end_s"], policy)
        for entry in entries if entry["kind"] != "KEEP"
    ]
    total = _r(duration_s)
    removed = _r(sum(max(0.0, b - a) for a, b in cuts))
    ratio = _r(removed / total) if total > 0 else 0.0
    # the ONLY measured denominator we have: Lane B's VAD segments, when present
    speech = speech_activity_seconds(db, workspace_id, asset_id) if asset_id else None
    info = {
        "triggered": False, "ratio": ratio, "removed_s": removed,
        "threshold": policy.max_removal_ratio, "source_duration_s": total,
        "speech_activity_s": speech,
        "denominator": "speech_activity" if (speech and speech > 0) else "source_duration",
    }
    if total <= 0 or ratio <= policy.max_removal_ratio + EPS:
        return entries, info
    info["triggered"] = True
    check = _persist_qc_flag(db, workspace_id=workspace_id, run_id=run_id,
                             policy=policy, ratio_info=info)
    forced: list[dict] = []
    for entry in entries:
        evidence = dict(entry.get("evidence") or {})
        # the same two keys Lane H's check uses, so any consumer of either side
        # reads the identical signal
        evidence["force_keep"] = True
        evidence["max_removal_ratio"] = dict(info, forced=True)
        if check:
            evidence["qc_check"] = check
        forced.append({
            "kind": "KEEP", "start_s": entry["start_s"], "end_s": entry["end_s"],
            "reason": "MAX_REMOVAL_RATIO", "confidence": entry.get("confidence"),
            "evidence": evidence,
        })
    return forced, info


def _attach_preview_ops(
    db: Session, rows: Sequence[EditProposal], policy: EditPolicy, timeline_id: str
) -> None:
    """Best-effort ``ops_json`` preview against the CURRENT timeline document.

    Purely informational: ``apply`` always recomputes against the document the
    editor is actually on, so a stale preview can never be submitted.
    """
    if not rows:
        return
    timeline = db.get(ContentTimeline, str(timeline_id))
    if timeline is None or timeline.workspace_id != rows[0].workspace_id:
        return
    duration = _media_span(dict(timeline.tracks_json or {}), policy)
    if duration <= 0:
        return
    cuts = [
        cut_range_for(str(row.kind or "KEEP"), float(row.start_s or 0.0),
                      float(row.end_s or 0.0), policy)
        for row in rows if str(row.kind or "KEEP") != "KEEP"
    ]
    try:
        ops = build_cut_operations(
            dict(timeline.tracks_json or {}),
            TimeMap.from_cut_ranges(cuts, duration),
            policy,
        )
    except ValueError:
        return  # preview only: too many operations, or an unusable document
    for row in rows:
        row.ops_json = [dict(op) for op in ops]


# ---------------------------------------------------------------------------
# detection entry points (DB-writing, but never event-emitting)
# ---------------------------------------------------------------------------


def detect_silence(
    db: Session,
    ws: Any,
    asset: MediaAsset,
    run: MediaIntelRun,
    path: str,
    policy: EditPolicy = DEFAULT_POLICY,
    *,
    project_id: str | None = None,
    timeline_id: str | None = None,
) -> dict:
    """Measure dead air and PROPOSE cuts. Never touches the media bytes.

    Returns the detection measurement, the proposals, per-reason counts, the
    planned removal ratio, the QC flag, the run metrics (with the per-proposal
    evidence) and the ``events`` the ROUTE must emit after ``db.commit()``.
    """
    policy.validate()
    detection = detect_dead_air(path, policy)
    entries: list[dict] = []
    for span in detection["ranges"]:
        # A removable proposal's range IS the padded cut, so ``apply`` -- which
        # recomputes the cut from (kind, start, end, policy) alone -- reproduces
        # exactly this range without having to trust the evidence. A KEEP row
        # points at the MEASURED span instead: nothing is cut, so the padding
        # question does not arise, and the operator wants to see the silence.
        start = span["cut_start_s"] if span["kind"] != "KEEP" else span["start_s"]
        end = span["cut_end_s"] if span["kind"] != "KEEP" else span["end_s"]
        entries.append({
            "kind": span["kind"], "start_s": start, "end_s": end,
            "reason": span["reason"], "confidence": 1.0,
            "evidence": {
                "detector": SILENCE_PROVIDER,
                "filter": (f"silencedetect=noise={policy.noise_db:g}dB"
                           f":d={policy.min_silence_s:g}"),
                "measured_span": {"start_s": span["start_s"], "end_s": span["end_s"],
                                  "duration_s": span["duration_s"]},
                "at_media_start": span["at_media_start"],
                "at_media_end": span["at_media_end"],
                "keep_padding_s": policy.keep_padding_s,
                "cut_start_s": span["cut_start_s"], "cut_end_s": span["cut_end_s"],
                "removable_s": span["removable_s"],
            },
        })
    duration = float(detection["source_duration_s"] or 0.0)
    if duration <= 0:
        duration = float(getattr(asset, "duration_seconds", None) or 0.0)
    workspace_id = str(getattr(ws, "id", ws))
    entries, ratio_info = _enforce_removal_ratio(
        db, entries, policy, duration, workspace_id=workspace_id, run_id=run.id,
        asset_id=asset.id,
    )
    rows = _insert_proposals(
        db, workspace_id=workspace_id, project_id=project_id, asset_id=asset.id,
        run_id=run.id, entries=entries, policy=policy,
    )
    evidence = {row.id: entry["evidence"] for row, entry in zip(rows, entries, strict=True)}
    if timeline_id:
        _attach_preview_ops(db, rows, policy, timeline_id)
    counts = _counts_by_reason(rows)
    metrics = {
        "detector": SILENCE_PROVIDER, "measured": detection["measured"],
        "reason": detection["reason"], "source_duration_s": duration,
        "policy": policy.to_dict(), "removal_ratio": ratio_info,
        "counts_by_reason": counts, "evidence": evidence,
    }
    _stamp_metrics(run, metrics)
    return {
        "detection": detection, "policy": policy.to_dict(), "policy_id": policy.policy_id,
        "auto_apply": bool(policy.auto_apply), "applied": False,
        "proposals": [proposal_dto(row, evidence.get(row.id)) for row in rows],
        "counts_by_reason": counts, "removal_ratio": ratio_info,
        "qc": _qc_flag(ratio_info, policy),
        "events": [{
            "kind": EVENT_SILENCE_PROPOSED,
            "message": (f"silence detection proposed {len(rows)} edit(s) "
                        f"({_counts_summary(counts)})"),
            "level": "warning" if ratio_info["triggered"] else "info",
            "data": {"run_id": run.id, "asset_id": asset.id, "proposals": len(rows),
                     "auto_apply": bool(policy.auto_apply), "removal_ratio": ratio_info},
        }],
        "metrics": metrics,
    }


def detect_fillers(
    db: Session,
    ws: Any,
    asset: MediaAsset,
    run: MediaIntelRun,
    units: Sequence[dict],
    source: dict,
    policy: EditPolicy = DEFAULT_POLICY,
    *,
    project_id: str | None = None,
    timeline_id: str | None = None,
) -> dict:
    """Find fillers / stutters / false starts and PROPOSE edits. Explainable only.

    ``units`` and ``source`` come from :func:`resolve_units`, which reports which
    text source was used; that string is echoed in the payload so a caller can
    tell word-level evidence from cue-level evidence.
    """
    policy.validate()
    findings = find_fillers(units, policy)
    entries = [proposal_for_finding(finding, policy) for finding in findings]
    for entry, finding in zip(entries, findings, strict=True):
        entry["evidence"]["source"] = str(source.get("source") or "none")
        entry["evidence"]["match"] = finding["match"]
    duration = max((_r(unit["end_s"]) for unit in units or []), default=0.0)
    if not duration:
        duration = float(getattr(asset, "duration_seconds", None) or 0.0)
    workspace_id = str(getattr(ws, "id", ws))
    entries, ratio_info = _enforce_removal_ratio(
        db, entries, policy, duration, workspace_id=workspace_id, run_id=run.id,
        asset_id=asset.id,
    )
    rows = _insert_proposals(
        db, workspace_id=workspace_id, project_id=project_id, asset_id=asset.id,
        run_id=run.id, entries=entries, policy=policy,
    )
    evidence = {row.id: entry["evidence"] for row, entry in zip(rows, entries, strict=True)}
    if timeline_id:
        _attach_preview_ops(db, rows, policy, timeline_id)
    counts = _counts_by_reason(rows)
    metrics = {
        "detector": FILLER_PROVIDER, "lexicon_version": FILLER_LEXICON_VERSION,
        "source": str(source.get("source") or "none"),
        "unit": str(source.get("unit") or "word"),
        "source_reason": str(source.get("reason") or ""),
        "source_duration_s": duration, "policy": policy.to_dict(),
        "removal_ratio": ratio_info, "counts_by_reason": counts, "evidence": evidence,
    }
    _stamp_metrics(run, metrics)
    return {
        "findings": findings, "policy": policy.to_dict(), "policy_id": policy.policy_id,
        "auto_apply": bool(policy.auto_apply), "applied": False,
        "text_source": {
            "source": str(source.get("source") or "none"),
            "available": bool(source.get("available")),
            "unit": str(source.get("unit") or "word"),
            "reason": str(source.get("reason") or ""),
            "units": len(units or []),
            "lexicon_version": FILLER_LEXICON_VERSION,
        },
        "proposals": [proposal_dto(row, evidence.get(row.id)) for row in rows],
        "counts_by_reason": counts, "removal_ratio": ratio_info,
        "qc": _qc_flag(ratio_info, policy),
        "events": [{
            "kind": EVENT_FILLERS_PROPOSED,
            "message": (f"filler detection proposed {len(rows)} edit(s) "
                        f"({_counts_summary(counts)})"),
            "level": "warning" if ratio_info["triggered"] else "info",
            "data": {"run_id": run.id, "asset_id": asset.id, "proposals": len(rows),
                     "text_source": str(source.get("source") or "none"),
                     "auto_apply": bool(policy.auto_apply),
                     "removal_ratio": ratio_info},
        }],
        "metrics": metrics,
    }


# ---------------------------------------------------------------------------
# decision
# ---------------------------------------------------------------------------


def decide_proposal(
    db: Session,
    workspace_id: str,
    proposal_id: str,
    decision: str,
    *,
    user_id: str | None = None,
) -> EditProposal:
    """Record the human decision on one proposal.

    ``PROPOSED -> DECIDED``; ``APPLIED`` is terminal -- re-deciding an applied
    proposal is refused (the route answers 409) because the timeline it reached
    no longer matches the proposal.
    """
    choice = str(decision or "").strip().lower()
    if choice not in PROPOSAL_DECISIONS:
        raise ValueError(f"decision must be one of {list(PROPOSAL_DECISIONS)}")
    row = get_proposal(db, workspace_id, proposal_id)
    if row is None:
        raise KeyError("proposal not found")
    if row.status == "APPLIED":
        raise ValueError("proposal is already APPLIED and cannot be re-decided")
    row.decision = choice
    row.status = "DECIDED"
    row.decided_by = str(user_id) if user_id else None
    row.decided_at = utcnow()
    db.flush()
    return row


# ---------------------------------------------------------------------------
# time map persistence
# ---------------------------------------------------------------------------


def build_time_map(
    db: Session,
    workspace_id: str,
    asset_id: str,
    policy: EditPolicy,
    cuts: Iterable[tuple[float, float]],
    source_duration_s: float,
    *,
    created_by: str | None = None,
) -> AudioTimeMap:
    """Persist the ordered segments in ``audio_time_maps`` (contracts §7).

    Idempotent per ``(workspace, asset, policy_id)``: applying the same policy
    again REPLACES the segments instead of piling up maps that disagree.
    """
    time_map = TimeMap.from_cut_ranges(cuts, source_duration_s)
    segments = [dict(seg) for seg in time_map.segments]
    row = db.scalar(select(AudioTimeMap).where(
        AudioTimeMap.workspace_id == str(workspace_id),
        AudioTimeMap.asset_id == str(asset_id),
        AudioTimeMap.policy_id == policy.policy_id,
    ))
    if row is None:
        row = AudioTimeMap(
            workspace_id=str(workspace_id), asset_id=str(asset_id),
            policy_id=policy.policy_id, segments_json=segments,
            created_by=str(created_by) if created_by else None,
        )
        db.add(row)
    else:
        row.segments_json = segments
        if created_by:
            row.created_by = str(created_by)
    db.flush()
    return row


def load_time_map(
    db: Session, workspace_id: str, asset_id: str, policy_id: str | None = None
) -> AudioTimeMap | None:
    """Newest stored map for an asset, optionally for one policy id."""
    query = select(AudioTimeMap).where(
        AudioTimeMap.workspace_id == str(workspace_id),
        AudioTimeMap.asset_id == str(asset_id),
    )
    if policy_id:
        query = query.where(AudioTimeMap.policy_id == str(policy_id))
    return db.scalars(query.order_by(AudioTimeMap.created_at.desc()).limit(1)).first()


# ---------------------------------------------------------------------------
# canonical Work 02 operations
# ---------------------------------------------------------------------------


def _track_of(doc: dict, kind: str) -> dict | None:
    for track in (doc or {}).get("tracks", []) or []:
        if str(track.get("kind") or "") == kind:
            return track
    return None


def _overlaps(
    start: float, end: float, cuts: Sequence[tuple[float, float, float]]
) -> list[tuple[float, float, float]]:
    """The ``(start, end, removed_before)`` cuts this clip actually touches."""
    return [
        (max(start, a), min(end, b), prior)
        for a, b, prior in cuts
        if min(end, b) - max(start, a) > EPS
    ]


def _media_span(doc: dict, policy: EditPolicy = DEFAULT_POLICY) -> float:
    """End of the audio material a cut acts on (the time-map domain).

    The time map's domain is the SOURCE asset, so the domain end is the end of
    the audio clips -- not the document duration, which may also contain a
    longer video. Falls back to ``duration_seconds`` when no audio clip exists.
    """
    span = 0.0
    for kind in policy.audio_tracks:
        track = _track_of(doc, kind)
        for clip in (track or {}).get("clips", []) or []:
            span = max(span, float(clip.get("start", 0.0)) + float(clip.get("duration", 0.0)))
    if span <= 0:
        span = float((doc or {}).get("duration_seconds") or 0.0)
    return _r(span)


def _audio_ops_for_clip(
    kind: str, clip: dict, time_map: TimeMap,
    cuts: Sequence[tuple[float, float, float]],
) -> list[dict]:
    """One audio clip, every cut inside it, in ascending source order.

    The delicate part is WHERE a split lands. ``split_item`` cuts the clip's
    CONTENT at a timeline position, so a split point must be expressed on the
    document as it exists at that moment: the source instant minus everything
    already removed before it. That is ``cut.removed_before`` -- deliberately
    NOT ``t - map_time(t)``, because a cut's own end point has not been removed
    yet while that cut is being applied, and that is exactly what makes both of
    a cut's split points symmetric.

    With that, each cut is a local transaction on the live piece:

    * head + tail -> ``split`` at both edges, ``delete`` the middle, ``move``
      the tail onto the head's end (that is the gap closing);
    * head only  -> ``split`` once, ``delete`` the tail piece, keep the head;
    * tail only  -> ``split`` once, ``delete`` the head piece, ``move`` the
      survivor back to where the head was;
    * neither    -> ``delete`` the whole piece.

    A clip that overlaps no cut is simply moved earlier by whatever was removed
    before it.
    """
    clip_id = str(clip.get("id") or "")
    src_start = float(clip.get("start", 0.0))
    src_end = src_start + float(clip.get("duration", 0.0))
    hits = _overlaps(src_start, src_end, cuts)
    if not hits:
        target = _r(src_start - time_map.removed_before(src_start))
        if abs(target - src_start) > EPS:
            return [{"type": "move_item", "track": kind, "clip_id": clip_id,
                     "start": target}]
        return []

    ops: list[dict] = []
    live_id, live_src_start = clip_id, src_start
    for cut_start, cut_end, prior in hits:
        at0 = _r(cut_start - prior)
        at1 = _r(cut_end - prior)
        cur_start = _r(live_src_start - prior)
        cur_end = _r(src_end - prior)
        if at1 <= at0 + EPS or at1 <= cur_start + EPS:
            continue  # fully absorbed by an earlier cut in the same clip
        head, tail = _r(at0 - cur_start), _r(cur_end - at1)
        if head <= EPS and tail <= EPS:
            ops.append({"type": "delete_item", "track": kind, "clip_id": live_id})
            return ops
        if head <= EPS:
            # the cut starts at the piece head: split, drop the head, slide back
            ops.append({"type": "split_item", "track": kind, "clip_id": live_id, "at": at1})
            ops.append({"type": "delete_item", "track": kind, "clip_id": live_id})
            live_id = f"{live_id}__b"
            ops.append({"type": "move_item", "track": kind, "clip_id": live_id,
                        "start": cur_start})
        elif tail <= EPS:
            # the cut runs to the piece end: split, drop the tail, keep the head
            ops.append({"type": "split_item", "track": kind, "clip_id": live_id, "at": at0})
            ops.append({"type": "delete_item", "track": kind, "clip_id": f"{live_id}__b"})
        else:
            # interior cut: split both edges, drop the middle, close the gap
            ops.append({"type": "split_item", "track": kind, "clip_id": live_id, "at": at0})
            ops.append({"type": "split_item", "track": kind, "clip_id": f"{live_id}__b",
                        "at": at1})
            ops.append({"type": "delete_item", "track": kind, "clip_id": f"{live_id}__b"})
            ops.append({"type": "move_item", "track": kind, "clip_id": f"{live_id}__b__b",
                        "start": at0})
            live_id = f"{live_id}__b__b"
        live_src_start = cut_end
    return ops


def build_cut_operations(
    doc: dict, time_map: TimeMap, policy: EditPolicy = DEFAULT_POLICY
) -> list[dict]:
    """Canonical Work 02 operations implementing ``time_map`` on ``doc``.

    * every audio clip that overlaps a cut becomes ``split_item`` at the edges +
      ``delete_item`` of the middle piece (+ ``move_item`` to close the gap);
    * every other audio clip after a cut is ``move_item``\\ d earlier;
    * caption / text overlay clips are re-timed with ``update_caption`` (or
      ``move_item`` when only the start moved) and deleted when a cut consumed
      them entirely.

    Every emitted ``type`` is checked against
    :data:`app.engine.timeline_ops.OP_TYPES` and a batch above the Work 02
    200-operation ceiling is REFUSED rather than silently truncated.
    """
    cuts = time_map.cut_shifts()
    if not cuts:
        return []
    ops: list[dict] = []
    for kind in policy.audio_tracks:
        track = _track_of(doc, kind)
        if track is None:
            continue
        for clip in sorted(track.get("clips", []) or [],
                           key=lambda c: float(c.get("start", 0.0))):
            ops.extend(_audio_ops_for_clip(kind, clip, time_map, cuts))

    for kind in policy.overlay_tracks:
        track = _track_of(doc, kind)
        if track is None:
            continue
        for clip in sorted(track.get("clips", []) or [],
                           key=lambda c: float(c.get("start", 0.0))):
            clip_id = str(clip.get("id") or "")
            start = float(clip.get("start", 0.0))
            duration = float(clip.get("duration", 0.0))
            new_start = time_map.map_time(start)
            new_end = time_map.map_time(start + duration)
            if new_end - new_start <= EPS:
                ops.append({"type": "delete_item", "track": kind, "clip_id": clip_id})
            elif abs((new_end - new_start) - duration) > EPS:
                ops.append({"type": "update_caption", "track": kind, "clip_id": clip_id,
                            "start": new_start, "duration": _r(new_end - new_start)})
            elif abs(new_start - start) > EPS:
                ops.append({"type": "move_item", "track": kind, "clip_id": clip_id,
                            "start": new_start})

    for op in ops:
        if op.get("type") not in OP_TYPES:
            raise ValueError(f"refusing to emit op type {op.get('type')!r}")
    if len(ops) > MAX_BATCH_OPERATIONS:
        raise ValueError(
            f"plan needs {len(ops)} operations; the Work 02 batch ceiling is "
            f"{MAX_BATCH_OPERATIONS}"
        )
    return ops


# ---------------------------------------------------------------------------
# apply planning (the SAVE happens in the timelines router, never here)
# ---------------------------------------------------------------------------


def plan_apply(
    db: Session,
    workspace_id: str,
    *,
    proposal_ids: Sequence[str] | None = None,
    asset_id: str | None = None,
) -> dict:
    """Resolve the APPLICABLE proposals of a workspace into one batch.

    Only ``status=DECIDED`` with ``decision in (remove, shorten)`` is
    applicable: a ``PROPOSED`` row carries no human decision yet, so applying it
    is refused rather than assumed. Raises ``ValueError`` / ``KeyError`` that the
    route maps to 409 / 404.
    """
    if proposal_ids:
        rows = []
        for pid in proposal_ids:
            row = get_proposal(db, workspace_id, pid)
            if row is None:
                raise KeyError("proposal not found")
            rows.append(row)
    elif asset_id:
        rows = list(db.scalars(
            select(EditProposal)
            .where(EditProposal.workspace_id == str(workspace_id),
                   EditProposal.asset_id == str(asset_id))
            .order_by(EditProposal.start_s)
        ).all())
    else:
        raise ValueError("proposal_ids or asset_id is required")
    if not rows:
        raise ValueError("no proposals in this workspace match the request")
    assets = {row.asset_id for row in rows}
    if len(assets) > 1:
        raise ValueError("proposals from different assets cannot share one batch")
    applicable = [row for row in rows
                  if row.status == "DECIDED" and row.decision in ("remove", "shorten")]
    if not applicable:
        raise ValueError(
            "no decided proposal to apply: a PROPOSED row has no operator decision "
            "yet (POST /proposals/{id}/decide first)"
        )
    return {
        "rows": applicable,
        "asset_id": next(iter(assets)),
        "skipped": [row.id for row in rows if row not in applicable],
    }


def apply_plan(
    db: Session,
    rows: Sequence[EditProposal],
    doc: dict,
    *,
    policy: EditPolicy = DEFAULT_POLICY,
) -> dict:
    """Build the operation batch + the time map for the DECIDED rows.

    Pure of the SAVE: the caller submits ``operations`` through
    ``POST /timelines/{id}/operations`` (Work 02 optimistic concurrency) and only
    then marks the proposals ``APPLIED``.
    """
    policy.validate()
    media_span = _media_span(doc, policy)
    cuts = [
        cut_range_for(
            "REMOVE_RANGE" if row.decision == "remove" else "SHORTEN_RANGE",
            float(row.start_s or 0.0), float(row.end_s or 0.0), policy,
        )
        for row in rows
    ]
    time_map = TimeMap.from_cut_ranges(cuts, media_span)
    operations = build_cut_operations(doc, time_map, policy)
    if not operations:
        raise ValueError(
            "the decided proposals produce no timeline operation on this document"
        )
    return {
        "operations": operations,
        "time_map": time_map,
        "policy": policy,
        "policy_id": policy.policy_id,
        "media_span_s": media_span,
        "proposal_ids": [row.id for row in rows],
    }


def mark_applied(db: Session, proposal_ids: Sequence[str]) -> None:
    """Terminal state of the applied proposals (the save already happened)."""
    for pid in proposal_ids:
        row = db.get(EditProposal, str(pid))
        if row is not None:
            row.status = "APPLIED"
            row.decided_at = row.decided_at or utcnow()
    db.flush()


def map_scenes(
    db: Session, workspace_id: str, timeline_id: str, time_map: TimeMap
) -> int:
    """Re-time every Scene of a timeline through the SAME map (contracts §7).

    A scene a cut consumed entirely collapses to a zero-length range instead of
    being deleted: the scene is narrative structure, and clip-derived bounds
    stay owned by the canonical ``scene_sync.resync_scene_ranges`` that the
    timelines route runs. Returns the number of scenes whose range moved.
    """
    from app.models.assets import Scene

    scenes = db.scalars(
        select(Scene)
        .where(Scene.workspace_id == str(workspace_id),
               Scene.timeline_id == str(timeline_id))
        .order_by(Scene.index)
    ).all()
    changed = 0
    for scene in scenes:
        start = float(scene.start_seconds or 0.0)
        end = float(scene.end_seconds or 0.0)
        new_start = time_map.map_time(start)
        new_end = max(new_start, time_map.map_time(end))
        if (new_start, new_end) != (start, end):
            scene.start_seconds, scene.end_seconds = new_start, new_end
            changed += 1
    db.flush()
    return changed


__all__ = [
    "AUDIO_TRACKS",
    "CONFIDENCE",
    "DEFAULT_POLICY",
    "EPS",
    "EVENT_EDITS_APPLIED",
    "EVENT_FILLERS_PROPOSED",
    "EVENT_PROPOSAL_DECIDED",
    "EVENT_SILENCE_PROPOSED",
    "EditPolicy",
    "FILLER_LEXICON",
    "FILLER_LEXICON_VERSION",
    "MAX_BATCH_OPERATIONS",
    "MIN_CUT_S",
    "MIN_DEAD_AIR_S",
    "REASONS",
    "SYNCED_OVERLAY_TRACKS",
    "TimeMap",
    "WEAK_FILLER_LEXICON",
    "apply_plan",
    "build_cut_operations",
    "build_time_map",
    "cue_units_from_doc",
    "cut_range_for",
    "decide_proposal",
    "detect_dead_air",
    "detect_fillers",
    "detect_silence",
    "find_fillers",
    "get_proposal",
    "list_proposals",
    "load_time_map",
    "map_scenes",
    "mark_applied",
    "plan_apply",
    "proposal_dto",
    "proposal_for_finding",
    "proposals_for_run",
    "resolve_units",
]

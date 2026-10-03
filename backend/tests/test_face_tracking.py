"""Face tracking gates (Work 12 Lane E) -- contracts §8 + the §16 test matrix.

Rows owned here: **face tracking**, **multi-face tracking** (2 and 3+
participants), exits/re-entry, overlapping detections and honesty.

The CI venv installs NO ML package (contracts §0/§1.3), so the production path
is honestly UNAVAILABLE here and these tests prove two different things:

1. **Honesty** (fast): ``health()`` is unavailable with a reason, there is no
   OpenCV-Haar fallback, the license verdict is the audited
   ``REVIEW_REQUIRED``, track ids are anonymous, a foreign workspace reads 404,
   the sample cap sets ``truncated``, and an uncertain crossing starts a NEW
   track instead of merging identities.
2. **The real pipeline over real pixels** (``@pytest.mark.slow``):
   :func:`app.engine.intel.face_tracking.analyze` samples REAL PNG frames with
   ffmpeg, hands them to a deterministic detector double and tracks the result.
   The double is a pure-stdlib connected-blob detector over the decoded PNG
   bytes -- it is NOT a face detector and proves nothing about face
   recognition. What it proves is that the tracking code consumes genuine
   detections derived from genuine pixels. It is validated against the shared
   fixture's measured ground truth (``bright_box_per_frame``: the 48x48 box at
   y=66 whose x follows ``(320-48) * t / 2`` over t = 0 .. 1.8).
"""

from __future__ import annotations

import json
import re
import struct
import subprocess
import zlib
from pathlib import Path

import pytest

from app.engine.intel import face_tracking as ft
from app.engine.intel import registry as intel_registry
from app.engine.intel.base import (
    COMMERCIAL_REVIEW_REQUIRED,
    IntelRequest,
    LicenseInfo,
    MediaIntelProvider,
    ProviderHealth,
    ProviderResult,
    ProviderUnavailable,
    ResourceSpec,
    safe_health,
)
from app.engine.intel.impl import mediapipe_faces as mp_faces

# ---------------------------------------------------------------------------
# detector doubles (test-only; production runs the MediaPipe detector)
# ---------------------------------------------------------------------------


def _decode_png_rgb(path: Path) -> tuple[int, int, bytes]:
    """Decode an 8-bit truecolour PNG with stdlib only (zlib + unfiltering).

    A real decode of the real bytes ffmpeg wrote -- no PIL, no numpy. Raises
    ValueError for anything but 8-bit RGB/RGBA non-interlaced, so a fixture
    change can never silently degrade into "zero pixels, no faces".
    """
    data = Path(path).read_bytes()
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError(f"{path}: not a PNG")
    offset, idat, header = 8, b"", None
    while offset < len(data):
        length = struct.unpack(">I", data[offset:offset + 4])[0]
        kind = data[offset + 4:offset + 8]
        body = data[offset + 8:offset + 8 + length]
        if kind == b"IHDR":
            header = struct.unpack(">IIBBBBB", body)
        elif kind == b"IDAT":
            idat += body
        offset += 12 + length
    if header is None:
        raise ValueError(f"{path}: no IHDR chunk")
    width, height, depth, colour, _comp, _filt, interlace = header
    if depth != 8 or colour not in (2, 6) or interlace != 0:
        raise ValueError(f"{path}: unsupported PNG layout {header}")
    channels = 3 if colour == 2 else 4
    stride = width * channels
    raw = zlib.decompress(idat)
    if len(raw) != height * (stride + 1):
        raise ValueError(f"{path}: unexpected raw size {len(raw)}")
    out = bytearray(height * stride)
    previous = bytearray(stride)
    pos = 0
    for y in range(height):
        method = raw[pos]
        pos += 1
        line = bytearray(raw[pos:pos + stride])
        pos += stride
        if method == 1:
            for i in range(channels, stride):
                line[i] = (line[i] + line[i - channels]) & 0xFF
        elif method == 2:
            for i in range(stride):
                line[i] = (line[i] + previous[i]) & 0xFF
        elif method == 3:
            for i in range(stride):
                left = line[i - channels] if i >= channels else 0
                line[i] = (line[i] + ((left + previous[i]) >> 1)) & 0xFF
        elif method == 4:
            for i in range(stride):
                a = line[i - channels] if i >= channels else 0
                b = previous[i]
                c = previous[i - channels] if i >= channels else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                line[i] = (line[i] + pred) & 0xFF
        out[y * stride:(y + 1) * stride] = line
        previous = line
    return width, height, bytes(out)


def _blobs(red: bytes, width: int, height: int, threshold: int, min_area: int):
    """Connected bright blobs of one frame -> ``[{x, y, w, h, luma}, ...]``.

    Row runs + union-find over the thresholded RED channel (the fixtures are
    neutral white on black, so one channel is the luminance). ``luma`` is the
    blob's MEASURED mean red value -- the only honest confidence signal this
    double can produce.
    """
    lut = bytes(255 if value >= threshold else 0 for value in range(256))
    mask = red.translate(lut)
    parent: dict[int, int] = {}

    def find(node: int) -> int:
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    nodes: list[list[int]] = []  # [x0, x1, y0, y1, area, red_sum]
    previous: list[tuple[int, int, int]] = []
    for y in range(height):
        row = mask[y * width:(y + 1) * width]
        source = red[y * width:(y + 1) * width]
        runs: list[tuple[int, int]] = []
        pos = row.find(255)
        while pos != -1:
            end = row.find(0, pos)
            if end == -1:
                end = width
            runs.append((pos, end - 1))
            pos = row.find(255, end)
        current: list[tuple[int, int, int]] = []
        for start, stop in runs:
            node = len(nodes)
            nodes.append([start, stop, y, y, stop - start + 1,
                          sum(source[start:stop + 1])])
            parent[node] = node
            current.append((start, stop, node))
            for prev_start, prev_stop, prev_node in previous:
                if start <= prev_stop and prev_start <= stop:
                    union(node, prev_node)
        previous = current

    groups: dict[int, list[int]] = {}
    for node, (x0, x1, y0, y1, area, red_sum) in enumerate(nodes):
        box = groups.setdefault(find(node), [x0, y0, x1, y1, 0, 0])
        box[0] = min(box[0], x0)
        box[1] = min(box[1], y0)
        box[2] = max(box[2], x1)
        box[3] = max(box[3], y1)
        box[4] += area
        box[5] += red_sum
    out = []
    for x0, y0, x1, y1, area, red_sum in groups.values():
        if area < min_area:
            continue
        out.append({
            "x": x0, "y": y0, "w": x1 - x0 + 1, "h": y1 - y0 + 1,
            "luma": red_sum // area,
        })
    return sorted(out, key=lambda item: (item["x"], item["y"]))


class BrightBoxDetector:
    """Deterministic bright-blob detector over REAL decoded PNG pixels.

    NOT a face detector: it finds thresholded blobs, which is exactly what the
    synthetic fixtures draw. It exists so the tracking code can be exercised
    over genuine pixels without an ML package, and it is validated against the
    shared fixture's measured ground truth.
    """

    version = "bright-box-double"
    name = "bright_box"

    def __init__(self, threshold: int = 200, min_area: int = 200) -> None:
        self.threshold = int(threshold)
        self.min_area = int(min_area)
        self.calls = 0
        self.boxes_seen = 0

    def detect(self, frame_path: str) -> list[dict]:
        self.calls += 1
        width, height, rgb = _decode_png_rgb(Path(frame_path))
        boxes = _blobs(rgb[0::3], width, height, self.threshold, self.min_area)
        self.boxes_seen += len(boxes)
        return [
            {
                "x": box["x"],
                "y": box["y"],
                "w": box["w"],
                "h": box["h"],
                "confidence": round(box["luma"] / 255.0, 4),
                # no landmarks: the double cannot produce them, so the pipeline
                # must persist NULL instead of inventing six keypoints
                "landmarks": None,
            }
            for box in boxes
        ]


class ScriptedDetector:
    """Detector double fed a fixed per-frame detection list (no pixels at all).

    Used for the pure-Python association tests, where the subject under test is
    the POLICY (ambiguous crossing, re-entry, cap), not the pixels.
    """

    version = "scripted-double"
    name = "scripted"

    def __init__(self, boxes: dict[int, list[dict]]) -> None:
        self._boxes = boxes
        self.calls = 0

    def detect(self, frame_path: str) -> list[dict]:
        index = self.calls
        self.calls += 1
        return list(self._boxes.get(index, []))


# ---------------------------------------------------------------------------
# local media fixtures (real ffmpeg)
#
# ``drawbox`` t-expressions silently draw nothing in this ffmpeg build, so every
# box is a lavfi colour source composited with ``overlay=...:eval=frame``.
# ---------------------------------------------------------------------------

_BITEXACT = ["-fflags", "+bitexact", "-flags", "+bitexact"]


def _build_overlay_mp4(
    path: Path,
    graph: str,
    sources: list[str],
    *,
    seconds: float,
    fps: int,
    width: int = 320,
    height: int = 180,
) -> Path:
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error", *_BITEXACT,
        "-f", "lavfi", "-i",
        f"color=c=black:s={width}x{height}:r={fps}:d={seconds:g}",
    ]
    for source in sources:
        cmd += ["-f", "lavfi", "-i", source]
    cmd += [
        "-filter_complex", graph, "-map", "[v]",
        "-frames:v", str(max(1, int(round(seconds * fps)))),
        "-c:v", "libx264", "-preset", "medium", "-crf", "12",
        "-pix_fmt", "yuv420p", "-an", "-f", "mp4", str(path),
    ]
    done = subprocess.run(cmd, capture_output=True, timeout=300, check=False)  # noqa: S603
    if done.returncode != 0 or not path.exists():
        raise AssertionError(f"fixture build failed: {done.stderr.decode()[-400:]}")
    return path


def _white_box(seconds: float, fps: int, side: int = 40) -> str:
    return f"color=c=white:s={side}x{side}:r={fps}:d={seconds:g}"


def multi_face_mp4(path: Path, *, seconds: float = 2.0, fps: int = 10) -> Path:
    """Three fixed bright boxes: interviewer + guest + a third participant."""
    sources = [_white_box(seconds, fps) for _ in range(3)]
    graph = (
        "[0:v][1:v]overlay=x=20:y=20:eval=frame:shortest=1[a];"
        "[a][2:v]overlay=x=200:y=20:eval=frame:shortest=1[b];"
        "[b][3:v]overlay=x=110:y=110:eval=frame:shortest=0[v]"
    )
    return _build_overlay_mp4(path, graph, sources, seconds=seconds, fps=fps)


def crossing_faces_mp4(path: Path, *, seconds: float = 2.0, fps: int = 10) -> Path:
    """Two faces that pass each other in the SAME row band.

    While they overlap the blob detector can only report ONE box, which matches
    both tracks about equally well -- the honest "which face is this?" case the
    tracker must refuse to guess.
    """
    sources = [_white_box(seconds, fps) for _ in range(2)]
    graph = (
        "[0:v][1:v]overlay=x='40+110*t':y=70:eval=frame:shortest=1[a];"
        "[a][2:v]overlay=x='240-110*t':y=70:eval=frame:shortest=0[v]"
    )
    return _build_overlay_mp4(path, graph, sources, seconds=seconds, fps=fps)


def exit_reentry_mp4(path: Path, *, seconds: float = 3.0, fps: int = 10) -> Path:
    """One face that leaves the frame for ~1 s and returns to the same spot."""
    sources = [_white_box(seconds, fps)]
    graph = (
        "[0:v][1:v]overlay=x='if(between(t,1.0,2.0),-100,60)':y=70:"
        "eval=frame:shortest=0[v]"
    )
    return _build_overlay_mp4(path, graph, sources, seconds=seconds, fps=fps)


# ---------------------------------------------------------------------------
# fast: detection normalisation + association policy (pure python)
# ---------------------------------------------------------------------------


def test_normalize_detection_clamps_to_frame_and_drops_degenerate_boxes():
    det = ft.normalize_detection({"x": -5, "y": 10, "w": 40, "h": 30}, 320, 180)
    assert det is not None
    assert (det.x, det.y, det.w, det.h) == (0.0, 10.0, 40.0, 30.0)

    tall = ft.normalize_detection({"x": 0, "y": 0, "w": 40, "h": 400}, 320, 180)
    assert (tall.h, tall.y) == (180.0, 0.0)
    assert ft.normalize_detection({"x": 0, "y": 0, "w": 1, "h": 40}, 320, 180) is None
    assert ft.normalize_detection({"x": "nan", "y": 0, "w": 40, "h": 40}, 320, 180) is None
    assert ft.normalize_detection({"x": 400, "y": 300, "w": 40, "h": 40}, 320, 180) is None
    assert ft.normalize_detection(None, 320, 180) is None
    assert ft.normalize_detection("not a box", 320, 180) is None


def test_normalize_detection_keeps_honest_confidence_and_landmarks():
    plain = ft.normalize_detection({"x": 1, "y": 2, "w": 30, "h": 30}, 320, 180)
    assert plain.confidence is None and plain.landmarks is None

    rich = ft.normalize_detection(
        {"x": 1, "y": 2, "w": 30, "h": 30, "confidence": 1.4,
         "landmarks": [{"key": "nose_tip", "x": 15.0, "y": 15.0}]},
        320, 180,
    )
    assert rich.confidence == 1.0
    assert rich.landmarks and rich.landmarks[0]["key"] == "nose_tip"
    assert rich.has_landmarks is True


def test_track_params_clamps_every_requested_value():
    params = ft.TrackParams.from_params(
        {"min_iou": -1, "max_samples_per_track": 10**9, "max_gap_frames": "nope"}
    )
    assert params.min_iou == 0.0
    assert params.max_samples_per_track == ft.MAX_MAX_SAMPLES
    assert params.max_gap_frames == ft.DEFAULT_MAX_GAP_FRAMES
    default = ft.TrackParams.from_params({})
    assert default.max_samples_per_track == ft.DEFAULT_MAX_SAMPLES
    assert default.min_iou == ft.DEFAULT_MIN_IOU


def test_single_face_keeps_one_stable_anonymous_id():
    """The point of §8: one person, one stable FT_00, no id churn."""
    frames = [
        (i * 0.25, [ft.Detection(x=100 + i * 4, y=60, w=48, h=48, confidence=0.9)])
        for i in range(8)
    ]
    tracks, warnings = ft.track_detections(frames)
    assert [t.track_id for t in tracks] == ["FT_00"]
    assert tracks[0].sample_count == 8
    assert tracks[0].reentry_count == 0
    assert tracks[0].truncated is False
    assert tracks[0].unresolved_crossing is False
    assert tracks[0].confidence_max == 0.9
    assert warnings == []


def test_multi_face_two_participants_get_two_stable_tracks():
    frames = [
        (
            i * 0.25,
            [
                ft.Detection(x=20 + i * 2, y=30, w=48, h=48, confidence=0.9),
                ft.Detection(x=240 - i * 2, y=30, w=48, h=48, confidence=0.85),
            ],
        )
        for i in range(6)
    ]
    tracks, _ = ft.track_detections(frames)
    assert [t.track_id for t in tracks] == ["FT_00", "FT_01"]
    assert all(t.sample_count == 6 for t in tracks)
    assert all(t.reentry_count == 0 and not t.unresolved_crossing for t in tracks)


def test_multi_face_three_participants_get_three_tracks():
    frames = [
        (
            i * 0.25,
            [
                ft.Detection(x=20, y=20, w=40, h=40),
                ft.Detection(x=140, y=20, w=40, h=40),
                ft.Detection(x=110, y=120, w=40, h=40),
            ],
        )
        for i in range(5)
    ]
    tracks, _ = ft.track_detections(frames)
    assert [t.track_id for t in tracks] == ["FT_00", "FT_01", "FT_02"]
    assert all(t.sample_count == 5 for t in tracks)


def test_overlapping_detections_are_both_retained():
    """Two faces that overlap in the frame stay two tracks (interviewer + guest)."""
    frames = [
        (
            i * 0.25,
            [
                ft.Detection(x=60 + i, y=40, w=60, h=60),
                ft.Detection(x=100 + i, y=40, w=60, h=60),
            ],
        )
        for i in range(6)
    ]
    tracks, warnings = ft.track_detections(frames)
    assert len(tracks) == 2, [t.to_dict(include_samples=False) for t in tracks]
    assert all(t.sample_count == 6 for t in tracks)
    assert all(t.unresolved_crossing is False for t in tracks)
    assert warnings == []


def test_exit_and_reentry_keeps_the_id_and_counts_the_reentry():
    frames: list[tuple[float, list[ft.Detection]]] = [
        (i * 0.25, [ft.Detection(x=60 + i, y=40, w=40, h=40)]) for i in range(3)
    ]
    # the face leaves the frame for six sampled frames (1.5 s) and comes back
    frames += [(k * 0.25, []) for k in range(3, 9)]
    frames += [(9 * 0.25 + k * 0.25, [ft.Detection(x=66 + k, y=40, w=40, h=40)])
               for k in range(1, 4)]
    tracks, warnings = ft.track_detections(frames)
    assert len(tracks) == 1, "a re-entry must not invent a second identity"
    assert tracks[0].track_id == "FT_00"
    assert tracks[0].reentry_count == 1
    assert tracks[0].sample_count == 6
    # gap_count counts the frames the track was OPEN and unmatched (6 frames are
    # empty, but the track is closed after 4 of them and stops counting)
    assert tracks[0].gap_count == 4
    assert any("re-entered" in w for w in warnings)


def test_reentry_beyond_the_tolerance_window_starts_a_new_track():
    """Outside the window an exited face is NOT assumed to be the same one."""
    params = ft.TrackParams(reentry_window_s=0.5)
    frames: list[tuple[float, list[ft.Detection]]] = [
        (0.0, [ft.Detection(x=60, y=40, w=40, h=40)]),
        (0.25, [ft.Detection(x=62, y=40, w=40, h=40)]),
    ]
    frames += [(k * 0.25, []) for k in range(2, 10)]
    frames.append((2.5, [ft.Detection(x=62, y=40, w=40, h=40)]))
    tracks, _ = ft.track_detections(frames, params)
    assert [t.track_id for t in tracks] == ["FT_00", "FT_01"]
    assert tracks[1].reentry_count == 0


def test_uncertain_crossing_starts_a_new_track_and_never_merges():
    """Two mutually ambiguous detections => NEW flagged tracks, not a merge."""
    frames = [
        (0.0, [ft.Detection(x=100, y=40, w=48, h=48),
               ft.Detection(x=120, y=40, w=48, h=48)]),
        (0.25, [ft.Detection(x=102, y=40, w=48, h=48),
                ft.Detection(x=122, y=40, w=48, h=48)]),
        # both detections now sit in the shared region: IoU with either track is equal
        (0.5, [ft.Detection(x=110, y=40, w=48, h=48),
               ft.Detection(x=112, y=40, w=48, h=48)]),
        (0.75, [ft.Detection(x=130, y=40, w=48, h=48),
                ft.Detection(x=140, y=40, w=48, h=48)]),
    ]
    tracks, warnings = ft.track_detections(frames)
    ids = [t.track_id for t in tracks]
    assert ids[:2] == ["FT_00", "FT_01"]
    assert len(ids) >= 3, ids
    flagged = [t.track_id for t in tracks if t.unresolved_crossing]
    assert flagged, "the ambiguous crossing must be flagged"
    assert any("ambiguous crossing" in w for w in warnings)
    for track in tracks:
        xs = [s.x for s in track.samples]
        assert not (min(xs) <= 100 and max(xs) >= 140), track.to_dict(include_samples=False)


def test_sample_cap_truncates_honestly():
    params = ft.TrackParams(max_samples_per_track=3)
    frames = [(i * 0.25, [ft.Detection(x=20 + i, y=20, w=40, h=40)]) for i in range(6)]
    tracks, _ = ft.track_detections(frames, params)
    assert tracks[0].sample_count == 3
    assert tracks[0].truncated is True
    assert tracks[0].end_s == 0.5, "the cap stops samples, it does not backfill"


def test_iou_and_tracker_state_are_pure():
    a = ft.Detection(x=0, y=0, w=10, h=10)
    assert ft.iou(a, ft.Detection(x=10, y=0, w=10, h=10)) == 0.0
    assert ft.iou(a, a) == 1.0
    assert 0.0 < ft.iou(a, ft.Detection(x=5, y=0, w=10, h=10)) < 1.0
    tracker = ft.FaceTracker()
    tracker.update(0.0, [ft.Detection(x=1, y=1, w=40, h=40)])
    assert tracker.frame_count == 1 and tracker.detection_count == 1
    assert ft.anonymous_track_label(0) == "FT_00"
    assert ft.anonymous_track_label(11) == "FT_11"
    assert ft.anonymous_track_label(100) == "FT_100"


# ---------------------------------------------------------------------------
# honesty: anonymous labels only, no detector => nothing
# ---------------------------------------------------------------------------

FORBIDDEN_KEYS = frozenset({
    "name", "names", "first_name", "last_name", "label", "labels", "alias",
    "gender", "sex", "age", "race", "ethnicity", "identity", "identities",
    "person", "person_id", "biometric", "biometrics", "demographic",
    "embedding", "descriptor", "recognized", "recognised", "actor",
})


def _all_keys(payload) -> set[str]:
    if isinstance(payload, dict):
        keys = set(payload)
        for value in payload.values():
            keys |= _all_keys(value)
        return keys
    if isinstance(payload, (list, tuple)):
        keys = set()
        for value in payload:
            keys |= _all_keys(value)
        return keys
    return set()


def test_track_dtos_are_anonymous_and_carry_no_identity_vocabulary():
    tracks, _ = ft.track_detections(
        [
            (0.0, [ft.Detection(x=10, y=10, w=40, h=40, confidence=0.9,
                                landmarks=[{"key": "nose_tip", "x": 30.0, "y": 30.0}])]),
            (0.25, [ft.Detection(x=12, y=10, w=40, h=40)]),
        ]
    )
    payload = {"tracks": [t.to_dict() for t in tracks]}
    assert not (_all_keys(payload) & FORBIDDEN_KEYS)
    dumped = json.dumps(payload).lower()
    for word in ("gender", "ethnic", "biometric", "identity", "recognis",
                 "recogniz", "person_id"):
        assert word not in dumped, word
    for item in payload["tracks"]:
        assert re.fullmatch(r"FT_\d{2,3}", item["track_id"]), item["track_id"]


def test_landmarks_are_absent_when_the_detector_supplies_none():
    tracks, _ = ft.track_detections(
        [(0.0, [ft.Detection(x=10, y=10, w=40, h=40, confidence=0.7)])]
    )
    sample = tracks[0].to_dict()["samples"][0]
    assert sample["landmarks"] is None
    assert tracks[0].has_landmarks is False
    assert sample["confidence"] == 0.7


def test_analyze_without_a_detector_reports_nothing_rather_than_a_fake_face():
    result = ft.analyze("definitely-not-a-video.mp4", detector=None)
    assert result.tracks == []
    assert result.detections == 0
    assert result.warnings, "an unusable input must say why"


# ---------------------------------------------------------------------------
# honesty: the provider in an ML-free environment
# ---------------------------------------------------------------------------


def test_provider_health_is_unavailable_here_with_a_reason():
    intel_registry.clear_cache()
    provider = mp_faces.PROVIDER()
    health = safe_health(provider)
    assert isinstance(health, ProviderHealth)
    assert health.available is False
    assert "mediapipe not installed" in health.reason
    assert health.detail["remediation"]
    assert health.detail["haar_fallback"] is False
    caps = provider.capabilities()
    assert caps["available"] is False
    assert caps["identity_recognition"] is False


def test_provider_health_is_unavailable_without_a_model_bundle(monkeypatch):
    """Even WITH the package installed the capability stays dark without weights."""
    import sys
    import types

    monkeypatch.setitem(sys.modules, "mediapipe", types.SimpleNamespace(__version__="0.0-fake"))
    provider = mp_faces.PROVIDER()
    health = safe_health(provider)
    assert health.available is False
    assert "no face landmarker model bundle is configured" in health.reason
    assert health.detail["model_license"] == "UNVERIFIED"
    assert health.detail["commercial_use"] == COMMERCIAL_REVIEW_REQUIRED


def test_provider_health_never_raises_for_a_broken_probe(monkeypatch):
    import sys
    import types

    monkeypatch.setitem(sys.modules, "mediapipe", types.SimpleNamespace(__version__="0.0-fake"))
    provider = mp_faces.PROVIDER()

    def boom():
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(provider, "_resolved_model", boom)
    health = safe_health(provider)
    assert health.available is False
    assert "health probe failed" in health.reason
    assert health.detail["error_type"] == "RuntimeError"


def test_provider_run_refuses_without_the_backend():
    provider = mp_faces.PROVIDER()
    request = IntelRequest(workspace_id="w", asset_id="a", storage_path="x.mp4")
    with pytest.raises(ProviderUnavailable):
        provider.run(request, progress=lambda _p: None,
                     should_cancel=lambda: False, deadline=None)


def test_no_opencv_haar_fallback_is_ever_offered():
    caps = mp_faces.PROVIDER().capabilities()
    assert caps["haar_fallback"] is False
    assert "Haar" in caps["haar_note"]
    assert "rather than Apache-2.0" in caps["haar_note"]
    assert "never a fabricated face box" in caps["haar_note"]


def test_license_is_the_audited_review_required_verdict():
    info = mp_faces.PROVIDER().license_info()
    assert info.code_license == "Apache-2.0"
    assert info.code_license_url.endswith("/mediapipe/master/LICENSE")
    assert info.model_license == "UNVERIFIED"
    assert info.commercial_use == COMMERCIAL_REVIEW_REQUIRED
    assert info.audited_on == "2026-09-29"
    assert info.notes == mp_faces.LICENSE_REASON
    assert ".task" in info.notes and "REVIEW_REQUIRED" in info.notes


def test_registry_reports_face_tracking_unavailable_with_the_reason():
    intel_registry.clear_cache()
    provider, reasons = intel_registry.resolve("face_tracking")
    assert provider is None
    assert "mediapipe_faces" in reasons
    assert reasons["mediapipe_faces"].strip()


def test_injected_detector_makes_the_provider_healthy_without_any_ml_package():
    detector = ScriptedDetector({0: [{"x": 1, "y": 1, "w": 40, "h": 40}]})
    provider = mp_faces.PROVIDER(detector)
    health = provider.health()
    assert health.available is True and health.reason == ""
    assert health.detail["backend"] == "injected"
    assert provider._resolve_detector() is detector
    # the license verdict does not change just because a double is injected
    assert provider.license_info().commercial_use == COMMERCIAL_REVIEW_REQUIRED


@pytest.mark.slow
def test_injected_detector_drives_the_identical_tracking_code(tmp_path):
    """Same provider.run -> same analyze -> same tracker; only detect() differs."""
    from tests.media_intel_fixtures import test_pattern_mp4

    video = test_pattern_mp4(tmp_path / "pattern.mp4")
    detector = BrightBoxDetector()
    provider = mp_faces.PROVIDER(detector)
    request = IntelRequest(
        workspace_id="w", asset_id="a", storage_path=str(video),
        params={"sample_fps": 10, "max_samples_per_track": 2000},
    )
    progress: list[float] = []
    result = provider.run(
        request, progress=progress.append, should_cancel=lambda: False, deadline=None,
    )
    assert result.ok is True
    assert detector.calls == result.metrics["frames"]
    tracks = result.artifacts["face_tracks"]["payload"]["tracks"]
    assert [t["track_id"] for t in tracks] == ["FT_00"]
    assert tracks[0]["sample_count"] == result.metrics["frames"]
    assert progress[0] == 0.0 and progress[-1] == pytest.approx(0.95, abs=0.01)


# ---------------------------------------------------------------------------
# persistence + workspace scoping
# ---------------------------------------------------------------------------


def _sample_track(*, label="FT_00", samples=2, reentry=0, truncated=False, crossing=False):
    track = ft.FaceTrack(track_id=label, reentry_count=reentry, truncated=truncated,
                         unresolved_crossing=crossing)
    for index in range(samples):
        track.add_sample(
            index * 0.5,
            ft.Detection(x=10 + index, y=20, w=40, h=40, confidence=0.5 + index / 100),
            max_samples=ft.DEFAULT_MAX_SAMPLES,
        )
    return track


def _asset_row(db, workspace_id: str, checksum: str):
    from app.models import MediaAsset

    asset = MediaAsset(workspace_id=workspace_id, type="video",
                       storage_key="clip.mp4", checksum=checksum)
    db.add(asset)
    db.flush()
    return asset


def _face_run(db, workspace_id: str, asset):
    from app.services import media_intel_runs as runs

    created = runs.create_run(db, workspace_id, kind="face_tracking", asset=asset,
                              provider_key="mediapipe_faces", params={"sample_fps": 2})
    return runs.get_run(db, workspace_id, created["id"])


def test_persist_tracks_writes_rows_with_denormalised_label(db_session, workspace_with_user):
    from sqlalchemy import select

    from app.models.media_intel import FaceTrack as FaceTrackRow
    from app.models.media_intel import FaceTrackSample

    ws_id = workspace_with_user["workspace"]
    asset = _asset_row(db_session, ws_id, "sum-face")
    run = _face_run(db_session, ws_id, asset)
    db_session.commit()

    summary = ft.persist_tracks(
        db_session, run_id=run.id, workspace_id=ws_id, asset_id=asset.id,
        tracks=[_sample_track(label="FT_00", samples=3, reentry=1),
                _sample_track(label="FT_01", samples=2, truncated=True)],
    )
    db_session.commit()
    assert summary == {"tracks": 2, "samples": 5, "unresolved_crossings": [],
                      "truncated_tracks": ["FT_01"], "reentry_total": 1}

    rows = db_session.scalars(
        select(FaceTrackRow).where(FaceTrackRow.run_id == run.id)
    ).all()
    assert len(rows) == 2
    samples = db_session.scalars(
        select(FaceTrackSample).where(FaceTrackSample.run_id == run.id)
        .order_by(FaceTrackSample.t_s.asc())
    ).all()
    assert len(samples) == 5
    assert {s.track_label for s in samples} == {"FT_00", "FT_01"}
    assert all(s.landmarks_json is None for s in samples)
    assert all(s.track_id in {r.id for r in rows} for s in samples)
    assert rows[0].confidence_max is not None
    assert rows[0].reentry_count == 1
    assert rows[1].truncated is True


def test_persist_tracks_reports_unresolved_crossings(db_session, workspace_with_user):
    ws_id = workspace_with_user["workspace"]
    asset = _asset_row(db_session, ws_id, "sum-cross")
    run = _face_run(db_session, ws_id, asset)
    db_session.commit()

    summary = ft.persist_tracks(
        db_session, run_id=run.id, workspace_id=ws_id, asset_id=asset.id,
        tracks=[_sample_track(samples=1, crossing=True)],
    )
    db_session.commit()
    assert summary["unresolved_crossings"] == ["FT_00"]


def test_track_row_dto_shape_is_anonymous():
    class _Row:
        id = "row-1"
        run_id = "run-1"
        track_id = "FT_03"
        start_s = 0.0
        end_s = 1.5
        sample_count = 2
        confidence_max = 0.91
        truncated = False
        reentry_count = 1

    dto = ft.track_row_dto(_Row(), [])
    assert not (_all_keys(dto) & FORBIDDEN_KEYS)
    assert dto["track_id"] == "FT_03" and dto["reentry_count"] == 1
    assert dto["samples"] == []


# ---------------------------------------------------------------------------
# API surface (contracts §14)
# ---------------------------------------------------------------------------


def _client(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api.v1.media_intel_faces import media_intel_faces_router
    from app.main import create_app

    monkeypatch.chdir(tmp_path)
    app = create_app()
    prefix = "/api/v1/workspaces/{workspace_id}/media-intel/face-tracks"
    if not any(path.endswith(prefix) for path in app.openapi()["paths"]):
        # the orchestrator registers the router at integration time; mount it
        # explicitly so the route contract is exercised either way
        app.include_router(media_intel_faces_router, prefix="/api/v1")
    return TestClient(app, raise_server_exceptions=False)


def _register(client, tag: str = "e"):
    import uuid

    response = client.post("/api/v1/auth/register", json={
        "email": f"face{tag}{uuid.uuid4().hex[:8]}@test.local",
        "password": "supersecret123",
    })
    assert response.status_code == 200, response.text
    data = response.json()
    return data["workspace"]["id"], {"Authorization": f"Bearer {data['access_token']}"}


def _store_asset(workspace_id: str, name: str, payload: bytes = b"not-media") -> str:
    from app.db import session_scope
    from app.models import MediaAsset
    from app.services.storage import STORAGE_ROOT

    root = STORAGE_ROOT / workspace_id
    root.mkdir(parents=True, exist_ok=True)
    (root / name).write_bytes(payload)
    with session_scope() as session:
        asset = MediaAsset(workspace_id=workspace_id, type="video", storage_key=name,
                           checksum=f"sum-{name}")
        session.add(asset)
        session.flush()
        return asset.id


def _stub_provider(tracks_payload: dict, *, raises: Exception | None = None):
    """A provider whose run() answers from a literal payload (no ffmpeg, no ML)."""

    class StubFaceProvider(MediaIntelProvider):
        key = "mediapipe_faces"
        kind = "face_tracking"

        def __init__(self) -> None:
            self.calls = 0

        def health(self):
            return ProviderHealth(available=True, reason="", version="stub-1",
                                  mode="local", detail={"backend": "stub"})

        def capabilities(self):
            return {"available": True, "reason": "", "backend": "stub"}

        def resource_requirements(self):
            return ResourceSpec(gpu=False)

        def license_info(self):
            return LicenseInfo(
                code_license="Apache-2.0", code_license_url="https://example.invalid",
                model_license="UNVERIFIED", commercial_use="PERMITTED",
                audited_on="2026-09-29",
            )

        def run(self, request, *, progress, should_cancel, deadline):
            self.calls += 1
            if raises is not None:
                raise raises
            progress(0.5)
            return ProviderResult(
                ok=True, artifacts={"face_tracks": {"payload": tracks_payload}},
                metrics={"tracks": len(tracks_payload.get("tracks") or []),
                         "samples": 2, "elapsed_ms": 5},
                warnings=["stubbed detector: not a real face detector"],
            )

        def cost(self, spec):
            return {"gpu_ms": 0, "cpu_ms": 0, "cost_micros": 0, "billed": False}

    return StubFaceProvider()


TRACK_PAYLOAD = {
    "tracks": [
        {
            "track_id": "FT_00",
            "start_s": 0.0,
            "end_s": 0.5,
            "sample_count": 2,
            "confidence_max": 0.9,
            "truncated": False,
            "reentry_count": 0,
            "unresolved_crossing": False,
            "gap_count": 0,
            "landmarks": False,
            "samples": [
                {"t_s": 0.0, "x": 10.0, "y": 20.0, "w": 40.0, "h": 40.0,
                 "confidence": 0.9, "landmarks": None},
                {"t_s": 0.5, "x": 12.0, "y": 20.0, "w": 40.0, "h": 40.0,
                 "confidence": 0.88, "landmarks": None},
            ],
        }
    ],
    "metrics": {"tracks": 1},
    "warnings": [],
}


def test_post_face_tracks_is_unavailable_in_this_environment(tmp_path, monkeypatch):
    """No ML package installed => a terminal UNAVAILABLE run, never a fake success."""
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _store_asset(ws_id, "nope.mp4")
    intel_registry.clear_cache()

    response = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/face-tracks",
                           headers=headers, json={"asset_id": asset_id})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["status"] == "UNAVAILABLE"
    assert body["run"]["error_code"] == "PROVIDER_UNAVAILABLE"
    assert "mediapipe" in body["run"]["metrics"]["reason"]
    assert body["items"] == []


def test_face_track_routes_persist_tracks_and_are_workspace_scoped(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _store_asset(ws_id, "clip.mp4")
    provider = _stub_provider(TRACK_PAYLOAD)
    monkeypatch.setattr(intel_registry, "_instances", {"mediapipe_faces": provider})

    response = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/face-tracks",
                           headers=headers, json={"asset_id": asset_id, "sample_fps": 4})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["status"] == "COMPLETED"
    assert provider.calls == 1
    assert len(body["items"]) == 1
    track = body["items"][0]
    assert track["track_id"] == "FT_00"
    assert track["sample_count"] == 2
    assert len(track["samples"]) == 2
    assert track["samples"][0]["landmarks"] is None
    assert not (_all_keys(body) & FORBIDDEN_KEYS)
    assert any("not a real face detector" in w for w in body["run"]["warnings"])
    run_id = body["run"]["id"]

    response = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/face-tracks/{run_id}",
                          headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["items"][0]["track_id"] == "FT_00"

    response = client.get(f"/api/v1/workspaces/{ws_id}/media-intel/face-tracks",
                          headers=headers)
    assert response.status_code == 200, response.text
    assert [item["id"] for item in response.json()["items"]] == [run_id]

    # an unknown id is 404 too (never a 200 with an empty body)
    import uuid

    missing = client.get(
        f"/api/v1/workspaces/{ws_id}/media-intel/face-tracks/{uuid.uuid4()}",
        headers=headers,
    )
    assert missing.status_code == 404, missing.text

    # a foreign workspace sees nothing at all (404, never 403)
    other_ws, other_headers = _register(client, tag="f")
    response = client.get(
        f"/api/v1/workspaces/{other_ws}/media-intel/face-tracks/{run_id}",
        headers=other_headers,
    )
    assert response.status_code == 404, response.text
    response = client.get(f"/api/v1/workspaces/{other_ws}/media-intel/face-tracks",
                          headers=other_headers)
    assert response.status_code == 200, response.text
    assert response.json()["items"] == []

    # the activity-feed event is emitted for the written tracks
    from sqlalchemy import select

    from app.db import session_scope
    from app.models import EventLog

    with session_scope() as session:
        events = session.scalars(
            select(EventLog).where(EventLog.workspace_id == ws_id)
        ).all()
    kinds = [event.kind for event in events]
    assert "MEDIA_INTEL_FACE_TRACKS" in kinds
    assert "MEDIA_INTEL_RUN_COMPLETED" in kinds
    face_event = next(e for e in events if e.kind == "MEDIA_INTEL_FACE_TRACKS")
    assert face_event.data_json["tracks"] == 1
    assert face_event.data_json["samples"] == 2


def test_post_face_tracks_validates_the_request_and_scoping(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    base = f"/api/v1/workspaces/{ws_id}/media-intel/face-tracks"

    unknown = client.post(base, headers=headers, json={"asset_id": "does-not-exist"})
    assert unknown.status_code == 404

    asset_id = _store_asset(ws_id, "clip.mp4")
    other_ws, other_headers = _register(client, tag="g")
    foreign = client.post(f"/api/v1/workspaces/{other_ws}/media-intel/face-tracks",
                          headers=other_headers, json={"asset_id": asset_id})
    assert foreign.status_code == 404, foreign.text

    assert client.post(base, headers=headers, json={}).status_code == 422
    assert client.post(base, headers=headers,
                       json={"asset_id": asset_id, "sample_fps": 999}).status_code == 422
    assert client.post(base, headers=headers,
                       json={"asset_id": asset_id,
                             "max_samples_per_track": 0}).status_code == 422
    assert client.post(base, json={"asset_id": asset_id}).status_code in (401, 403)


def test_post_face_tracks_maps_provider_unavailable_to_the_unavailable_state(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _store_asset(ws_id, "clip.mp4")
    monkeypatch.setattr(intel_registry, "_instances", {
        "mediapipe_faces": _stub_provider({}, raises=ProviderUnavailable("gone")),
    })
    response = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/face-tracks",
                           headers=headers, json={"asset_id": asset_id})
    assert response.status_code == 200, response.text
    assert response.json()["run"]["status"] == "UNAVAILABLE"
    assert "gone" in response.json()["run"]["metrics"]["reason"]


def test_post_face_tracks_honours_the_run_cache(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    asset_id = _store_asset(ws_id, "clip.mp4")
    provider = _stub_provider(TRACK_PAYLOAD)
    monkeypatch.setattr(intel_registry, "_instances", {"mediapipe_faces": provider})
    url = f"/api/v1/workspaces/{ws_id}/media-intel/face-tracks"

    first = client.post(url, headers=headers, json={"asset_id": asset_id}).json()
    assert provider.calls == 1
    second = client.post(url, headers=headers, json={"asset_id": asset_id}).json()
    assert second["cache_hit"] is True
    assert second["run"]["id"] == first["run"]["id"]
    assert provider.calls == 1, "a cache hit must not recompute"
    forced = client.post(url, headers=headers,
                         json={"asset_id": asset_id, "force": True}).json()
    assert forced["cache_hit"] is False
    assert provider.calls == 2


# ---------------------------------------------------------------------------
# slow: the real pipeline over real decoded frames
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_media(tmp_path_factory):
    """Real ffmpeg fixtures, built once for this module."""
    import shutil

    assert shutil.which("ffmpeg"), "these slow tests need ffmpeg on PATH"
    root = tmp_path_factory.mktemp("laneE-media")
    from tests.media_intel_fixtures import test_pattern_mp4

    return {
        "root": root,
        "pattern": test_pattern_mp4(root / "pattern.mp4"),
        "multi": multi_face_mp4(root / "multi.mp4"),
        "crossing": crossing_faces_mp4(root / "crossing.mp4"),
        "exit": exit_reentry_mp4(root / "exit.mp4"),
    }


@pytest.mark.slow
def test_detector_double_matches_the_shared_fixture_ground_truth(real_media):
    """The double is validated against the fixture's MEASURED ground truth.

    The very same source frames ``bright_box_per_frame`` measured (the 48x48 box
    at y=66 whose x follows ``(320-48) * t / 2``) are fed to the double, so any
    drift shows up as a pixel error instead of a plausible box.
    """
    from tests.media_intel_fixtures import bright_box_per_frame, video_frame_luma

    video = real_media["pattern"]
    truth = bright_box_per_frame(video, fps=4)
    frames = video_frame_luma(video, fps=4, out_dir=real_media["root"] / "truth")
    assert len(frames) == len(truth) == 8
    detector = BrightBoxDetector()
    recovered = []
    for frame, expected in zip(frames, truth, strict=True):
        boxes = detector.detect(frame["path"])
        assert len(boxes) == 1, (frame, boxes)
        box = boxes[0]
        recovered.append(box["x"])
        assert box["w"] == expected["w"] == 48
        assert box["h"] == expected["h"] == 48
        assert box["y"] == expected["y"] == 66
        assert abs(box["x"] - expected["x"]) <= 2, (frame["t_s"], box, expected)
        assert box["landmarks"] is None
        assert box["confidence"] == pytest.approx(1.0, abs=0.05)
    # the box really moved (otherwise the check above would be vacuous)
    assert recovered == sorted(recovered)
    assert recovered[-1] - recovered[0] > 200


@pytest.mark.slow
def test_real_pipeline_keeps_one_stable_track_over_the_moving_box(real_media):
    """Real decode -> detections -> ONE stable anonymous FT_00, boxes truthful."""
    detector = BrightBoxDetector()
    result = ft.analyze(real_media["pattern"], detector=detector, fps=10)
    assert detector.calls == 20 == result.frames
    assert result.detections == 20
    assert len(result.tracks) == 1, [t.to_dict(include_samples=False)
                                     for t in result.tracks]
    track = result.tracks[0]
    assert track.track_id == "FT_00"
    assert track.sample_count == 20
    assert track.reentry_count == 0 and track.truncated is False
    for sample in track.samples:
        expected_x = (320 - 48) * sample.t_s / 2.0
        assert abs(sample.x - expected_x) <= 2, sample.to_dict()
        assert (sample.y, sample.w, sample.h) == (66, 48.0, 48.0)
        assert sample.confidence is not None
        assert sample.landmarks is None
    assert result.warnings == []


@pytest.mark.slow
def test_sampling_cadence_never_links_an_unreliable_step(real_media):
    """MEASURED consequence of the conservative policy (see analyze's docstring).

    The same moving box sampled at 4 fps moves ~34 px per sample, which drops
    below ``min_iou`` for a 48 px box: the tracker starts a NEW anonymous track
    per step instead of inventing one long track through unreliable links.
    Sampling at the media rate (10 fps) keeps ONE stable id.
    """
    sparse = ft.analyze(real_media["pattern"], detector=BrightBoxDetector(), fps=4)
    assert sparse.frames == 8
    assert sparse.track_count == 8, "fragmented, never falsely merged"
    assert sparse.tracks[0].unresolved_crossing is False
    dense = ft.analyze(real_media["pattern"], detector=BrightBoxDetector(), fps=10)
    assert [t.track_id for t in dense.tracks] == ["FT_00"]


@pytest.mark.slow
def test_real_pipeline_tracks_three_participants(real_media):
    result = ft.analyze(real_media["multi"], detector=BrightBoxDetector(), fps=4)
    assert len(result.tracks) == 3, [t.to_dict(include_samples=False) for t in result.tracks]
    assert [t.track_id for t in result.tracks] == ["FT_00", "FT_01", "FT_02"]
    for track in result.tracks:
        assert track.sample_count == result.frames
        assert track.reentry_count == 0
        assert track.unresolved_crossing is False


@pytest.mark.slow
def test_real_pipeline_counts_an_exit_and_reentry(real_media):
    result = ft.analyze(real_media["exit"], detector=BrightBoxDetector(), fps=4)
    assert result.frames == 12
    assert len(result.tracks) == 1, [t.to_dict(include_samples=False) for t in result.tracks]
    track = result.tracks[0]
    assert track.track_id == "FT_00"
    assert track.reentry_count == 1
    assert track.gap_count >= 3, "the missing frames are a real, counted gap"
    assert track.sample_count == result.frames - track.gap_count
    assert any("re-entered" in warning for warning in result.warnings)


@pytest.mark.slow
def test_real_pipeline_refuses_to_merge_an_ambiguous_crossing(real_media):
    result = ft.analyze(real_media["crossing"], detector=BrightBoxDetector(), fps=4)
    assert result.frames == 8
    assert result.unresolved_crossings, [t.to_dict(include_samples=False)
                                         for t in result.tracks]
    assert result.track_count >= 3, "a crossing must not collapse two people into one id"
    assert any("ambiguous crossing" in warning for warning in result.warnings)
    for track in result.tracks:
        xs = [sample.x for sample in track.samples]
        assert not (min(xs) <= 45 and max(xs) >= 235), track.to_dict(include_samples=False)


@pytest.mark.slow
def test_real_pipeline_truncates_at_the_sample_cap(real_media):
    result = ft.analyze(
        real_media["pattern"], detector=BrightBoxDetector(), fps=10,
        params=ft.TrackParams(max_samples_per_track=5),
    )
    assert result.tracks[0].sample_count == 5
    assert result.tracks[0].truncated is True
    assert result.truncated_tracks == ["FT_00"]
    assert result.metrics()["truncated_tracks"] == ["FT_00"]


@pytest.mark.slow
def test_real_pipeline_persists_through_the_route(real_media, tmp_path, monkeypatch):
    """End to end: HTTP -> provider -> real frames -> rows -> GET."""
    from sqlalchemy import select

    from app.db import session_scope
    from app.models.media_intel import FaceTrack as FaceTrackRow
    from app.models.media_intel import FaceTrackSample
    from app.services.storage import STORAGE_ROOT

    client = _client(tmp_path, monkeypatch)
    ws_id, headers = _register(client)
    payload = real_media["pattern"].read_bytes()
    asset_id = _store_asset(ws_id, "pattern.mp4", payload)
    monkeypatch.setattr(intel_registry, "_instances", {
        "mediapipe_faces": mp_faces.PROVIDER(BrightBoxDetector()),
    })

    response = client.post(f"/api/v1/workspaces/{ws_id}/media-intel/face-tracks",
                           headers=headers, json={"asset_id": asset_id, "sample_fps": 10})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["run"]["status"] == "COMPLETED", body
    assert [item["track_id"] for item in body["items"]] == ["FT_00"]
    assert body["items"][0]["sample_count"] == 20
    assert not (_all_keys(body) & FORBIDDEN_KEYS)

    with session_scope() as session:
        rows = session.scalars(
            select(FaceTrackRow).where(FaceTrackRow.workspace_id == ws_id)
        ).all()
        samples = session.scalars(
            select(FaceTrackSample).where(FaceTrackSample.workspace_id == ws_id)
        ).all()
    assert len(rows) == 1 and len(samples) == 20
    assert rows[0].asset_id == asset_id
    assert all(sample.track_label == "FT_00" for sample in samples)
    assert all(sample.landmarks_json is None for sample in samples)
    # derived-only: the source asset bytes are untouched
    assert (STORAGE_ROOT / ws_id / "pattern.mp4").read_bytes() == payload

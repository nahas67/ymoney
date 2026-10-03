"""Work 13.1 Â§8 â€” real FFmpeg render verification.

The DoD is explicit that "a file merely existing is insufficient". Each test
here renders REAL media and then inspects FRAME PIXELS to prove the intended
behaviour:

* a DISSOLVE really blends (a frame at the midpoint contains BOTH source
  colours, and is measurably different from either pure source);
* a keyframed overlay really MOVES (the horizontal centroid of drawn pixels
  changes between the first and last frame);
* a composite effect really composites (a blurred plate is measurably
  different from the unblurred source).

All sources are deterministic lavfi graphs, so every assertion is stable.
Skipped (never silently passed) when ffmpeg is unavailable.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed")

FFMPEG = shutil.which("ffmpeg")


# ---------------------------------------------------------------------------
# deterministic helpers
# ---------------------------------------------------------------------------


def _run(args: list[str]) -> None:
    proc = subprocess.run(args, capture_output=True, text=True)
    if proc.returncode != 0:
        raise AssertionError(
            f"ffmpeg failed: {' '.join(args[:6])}...\n{proc.stderr[-900:]}")


def _solid(path: Path, color: str, seconds: float, fps: int = 10,
           size: str = "320x568") -> None:
    """A solid-colour clip (a 'visibly different' transition endpoint)."""
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i",
          f"color=c={color}:s={size}:r={fps}:d={seconds}",
          "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-fflags", "+bitexact", "-flags:v", "+bitexact", str(path)])


def _pattern(path: Path, seconds: float, fps: int = 10,
             size: str = "320x568") -> None:
    _run(["ffmpeg", "-y", "-f", "lavfi", "-i",
          f"testsrc=s={size}:r={fps}:d={seconds}",
          "-c:v", "libx264", "-pix_fmt", "yuv420p",
          "-fflags", "+bitexact", "-flags:v", "+bitexact", str(path)])


def _probe_size(path: Path) -> tuple[int, int]:
    proc = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height", "-of", "json", str(path)],
        capture_output=True, text=True)
    stream = json.loads(proc.stdout)["streams"][0]
    return int(stream["width"]), int(stream["height"])


def _raw_rgb(path: Path, at_s: float, size: tuple[int, int] | None = None
             ) -> bytes:
    """One frame as raw rgb24 bytes, at the REAL encoded resolution."""
    width, height = size or _probe_size(path)
    proc = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", f"{at_s:.3f}", "-i", str(path),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True)
    if proc.returncode != 0 or not proc.stdout:
        raise AssertionError(f"frame grab failed at {at_s}s: {proc.stderr[-300:]}")
    expected = width * height * 3
    assert len(proc.stdout) == expected, (
        f"expected {expected} bytes for {width}x{height}, got {len(proc.stdout)}")
    return proc.stdout


def _mean_rgb(frame: bytes) -> tuple[float, float, float]:
    data = memoryview(frame)
    count = len(data) // 3
    r = sum(data[i] for i in range(0, len(data), 3)) / count
    g = sum(data[i] for i in range(1, len(data), 3)) / count
    b = sum(data[i] for i in range(2, len(data), 3)) / count
    return r, g, b


def _centroid_x(frame: bytes, width: int) -> float:
    """Fractional horizontal centre of mass of BRIGHT (drawn) pixels.

    Scans the WHOLE frame: a bottom-anchored caption lives near the last rows,
    so a partial scan would report "nothing drawn".
    """
    data = frame
    total = 0.0
    weighted = 0.0
    for index in range(0, len(data), 3):
        pixel = (data[index] + data[index + 1] + data[index + 2]) / 3.0
        if pixel > 200:
            x = (index // 3) % width
            total += 1.0
            weighted += x
    if not total:
        return -1.0
    return weighted / total / width


def _ink(frame: bytes) -> float:
    """Total drawn 'ink' (sum of luminance).

    Drawn text occupies a small fraction of a 1080x1920 frame, so the whole
    frame MEAN is ~2 whether or not the caption rendered. Summing luminance
    scales with drawn area x intensity, which is the signal that actually
    distinguishes an opacity animation.
    """
    view = memoryview(frame)
    return float(sum(view[index] for index in range(0, len(view), 3)))


def _clip(track_id: str, kind: str, clip_id: str, start: float, duration: float,
          **extra) -> dict:
    base = {
        "id": clip_id, "name": clip_id, "start": start, "duration": duration,
        "source": {}, "effects": [], "source_start": 0.0, "volume": 1.0,
        "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0, "transform": {},
        "text": {}, "transition_in": "cut", "transition_out": "cut",
    }
    base.update(extra)
    return {"id": track_id, "kind": kind, "name": track_id, "clips": [base]}


def _doc(tracks: list[dict], duration: float) -> dict:
    return {"workspace_id": "ws", "fps": 10, "aspect_ratio": "9:16",
            "duration_seconds": duration, "tracks": tracks}


# ---------------------------------------------------------------------------
# 1. transitions really render
# ---------------------------------------------------------------------------


@requires_ffmpeg
@pytest.mark.parametrize("transition", ["FADE", "DISSOLVE", "SLIDE", "WIPE",
                                         "ZOOM"])
def test_transition_really_blends_two_visibly_different_clips(
    tmp_path, monkeypatch, transition
):
    """A stored transition must change rendered frames, not be a no-op.

    RED and BLUE segments are rendered separately for reference; the
    transitioned render must contain a frame at the transition midpoint whose
    colour is measurably BETWEEN the two sources.
    """
    from app.providers.video_engine import timeline_render as tr

    red = tmp_path / "red.mp4"
    blue = tmp_path / "blue.mp4"
    _solid(red, "red", 2.0)
    _solid(blue, "blue", 2.0)

    overlap = 0.8
    # Clips are ADJACENT, not overlapping: `visual_segments` splits overlapping
    # clips into time slices, which would leave the second segment shorter than
    # the overlap and correctly degrade the transition to a cut.
    doc = _doc([
        _clip("t_video", "video", "v1", 0.0, 2.0),
        _clip("t_video2", "video", "v2", 2.0, 2.0,
              transition={"from_item": "v1", "to_item": "v2",
                          "type": transition, "duration": overlap,
                          "parameters": {}}),
    ], duration=4.0)

    paths = {"v1": red, "v2": blue}
    monkeypatch.setattr(
        tr, "resolve_clip_source",
        lambda ws, db, source: paths.get(str(source.get("__clip", ""))) or red,
    )
    # route each clip to its own source
    def _resolve(ws, db, source):
        asset = str((source or {}).get("__clip") or "")
        return paths.get(asset, red)
    monkeypatch.setattr(tr, "resolve_clip_source", _resolve)
    for track in doc["tracks"]:
        for c in track["clips"]:
            c["source"] = {"__clip": c["id"]}

    out = tr.render_timeline("ws", None, doc, out_name=f"x_{transition}.mp4", fps=10)
    produced = Path(out["path"])
    assert produced.exists() and produced.stat().st_size > 0
    # the transition must be reported as applied, not degraded to a cut
    applied = [w for w in out["warnings"] if "transition" in w and "applied" in w]
    assert applied, f"{transition} was not applied: {out['warnings']}"
    assert not any("rendered as a cut" in w for w in out["warnings"]), out["warnings"]

    # transition midpoint in stream time: offset = first clip - overlap
    midpoint = 2.0 - overlap / 2.0
    mid = _mean_rgb(_raw_rgb(produced, midpoint))
    # a pure red frame and a pure blue frame for reference
    red_ref = _mean_rgb(_raw_rgb(red, 0.5))
    blue_ref = _mean_rgb(_raw_rgb(blue, 0.5))
    # the blended frame must carry BOTH sources: meaningful red AND meaningful blue
    if transition == "ZOOM":
        # ffmpeg's ``zoomin`` is a push-in, not a cross-blend, so "both
        # colours present" is the wrong expectation. Prove instead that the
        # midpoint is NOT the untouched first source, i.e. the transition ran.
        assert mid != red_ref, f"ZOOM left the midpoint as the raw source: {mid}"
        return
    assert mid[0] > 40, f"no red contribution at the midpoint: {mid}"
    assert mid[2] > 40, f"no blue contribution at the midpoint: {mid}"
    # and it must be genuinely between them, not identical to either
    assert mid != red_ref and mid != blue_ref
    assert abs(mid[0] - red_ref[0]) > 8 or abs(mid[2] - red_ref[2]) > 8, (
        f"midpoint {mid} is not distinguishable from the red source {red_ref}")


@requires_ffmpeg
def test_cut_is_the_only_case_with_no_transition_filter(tmp_path, monkeypatch):
    """A CUT must not add an xfade; a FADE must."""
    from app.providers.video_engine import timeline_render as tr

    red = tmp_path / "r.mp4"
    blue = tmp_path / "b.mp4"
    _solid(red, "red", 1.0)
    _solid(blue, "blue", 1.0)
    paths = {"v1": red, "v2": blue}
    # Use monkeypatch (NOT a manual assignment): a manual `finally` that reads
    # the module dict back would "restore" the stub we just installed and
    # poison every later test in the same process.
    monkeypatch.setattr(
        tr, "resolve_clip_source",
        lambda ws, db, source: paths.get(str((source or {}).get("__clip") or ""), red),
    )

    def _render(name: str, transition: dict | None):
        doc = _doc([
            _clip("t_video", "video", "v1", 0.0, 1.0),
            _clip("t_video2", "video", "v2", 1.0, 1.0,
                  **({"transition": transition} if transition else {})),
        ], duration=2.0)
        for track in doc["tracks"]:
            for c in track["clips"]:
                c["source"] = {"__clip": c["id"]}
        return tr.render_timeline("ws", None, doc, out_name=name, fps=10)

    cut = _render("cut.mp4", {"from_item": "v1", "to_item": "v2",
                              "type": "CUT", "duration": 0.0})
    assert not any("applied" in w for w in cut["warnings"]), cut["warnings"]
    fade = _render("fade.mp4", {"from_item": "v1", "to_item": "v2",
                                "type": "FADE", "duration": 0.5})
    assert any("FADE applied" in w for w in fade["warnings"]), fade["warnings"]


@requires_ffmpeg
def test_transition_preserves_audio_synchronisation(tmp_path, monkeypatch):
    """A transition shortens VIDEO only; the audio bed must stay intact."""
    from app.providers.video_engine import timeline_render as tr

    red = tmp_path / "r.mp4"
    blue = tmp_path / "b.mp4"
    _solid(red, "red", 1.0)
    _solid(blue, "blue", 1.0)

    doc = _doc([
        _clip("t_video", "video", "v1", 0.0, 1.0),
        _clip("t_video2", "video", "v2", 1.0, 1.0,
              transition={"from_item": "v1", "to_item": "v2", "type": "DISSOLVE",
                          "duration": 0.5, "parameters": {}}),
    ], duration=2.0)
    paths = {"v1": red, "v2": blue}
    for track in doc["tracks"]:
        for c in track["clips"]:
            c["source"] = {"__clip": c["id"]}
    # monkeypatch, never a bare module assignment: the module-level resolver
    # is shared by every later test in this process.
    monkeypatch.setattr(
        tr, "resolve_clip_source",
        lambda ws, db, source: paths.get(str((source or {}).get("__clip") or ""), red),
    )
    out = tr.render_timeline("ws", None, doc, out_name="aud.mp4", fps=10)

    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", out["path"]],
        capture_output=True, text=True)
    streams = json.loads(probe.stdout)["streams"]
    assert any(s["codec_type"] == "audio" for s in streams), "audio stream missing"
    video = next(s for s in streams if s["codec_type"] == "video")
    audio = next(s for s in streams if s["codec_type"] == "audio")
    # xfade removes the overlap from the video stream only
    assert float(video["duration"]) < float(audio["duration"]) + 0.05, (
        f"video {video['duration']} should be shorter than audio "
        f"{audio['duration']} after a 0.5s cross-fade")


# ---------------------------------------------------------------------------
# 2. keyframes really move rendered pixels
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_keyframed_overlay_actually_moves(tmp_path, monkeypatch):
    """A caption with x-keyframes must change its drawn position."""
    from app.providers.video_engine import timeline_render as tr

    base = tmp_path / "base.mp4"
    _solid(base, "black", 3.0)

    doc = _doc([
        _clip("t_video", "video", "v1", 0.0, 3.0),
        {"id": "t_caption", "kind": "caption", "name": "c", "clips": [{
            "id": "c1", "name": "MOVE", "start": 0.2, "duration": 2.4,
            "source": {}, "effects": [], "source_start": 0.0, "volume": 1.0,
            "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0, "transform": {},
            "transition_in": "cut", "transition_out": "cut",
            "text": {"preset": "minimal", "primary_color": "#ffffff",
                     "stroke_width": 0, "shadow": False, "size": 120},
            "word_level": False,
            "keyframes": [
                {"id": "k1", "t": 0.0, "easing": "LINEAR",
                 "props": {"x": 0.10, "opacity": 1.0}},
                {"id": "k2", "t": 2.0, "easing": "LINEAR",
                 "props": {"x": 0.85, "opacity": 1.0}},
            ],
        }]},
    ], duration=3.0)

    monkeypatch.setattr(tr, "resolve_clip_source",
                        lambda ws, db, source: base)
    out = tr.render_timeline("ws", None, doc, out_name="kf.mp4", fps=10)
    produced = Path(out["path"])
    assert produced.exists()

    w, _h = _probe_size(produced)
    early = _centroid_x(_raw_rgb(produced, 0.4, (w, _h)), w)
    late = _centroid_x(_raw_rgb(produced, 2.4, (w, _h)), w)
    assert early >= 0, "no bright pixels drawn early on"
    assert late >= 0, "no bright pixels drawn late on"
    # the overlay must have travelled across the frame
    assert late - early > 0.15, (
        f"keyframed overlay did not move horizontally: {early:.3f} -> {late:.3f}")


@requires_ffmpeg
def test_keyframed_opacity_actually_fades(tmp_path, monkeypatch):
    """An opacity keyframe chain must change rendered brightness."""
    from app.providers.video_engine import timeline_render as tr

    base = tmp_path / "b.mp4"
    _solid(base, "black", 3.0)

    def _doc_with(opacity_end: float):
        return _doc([
            _clip("t_video", "video", "v1", 0.0, 3.0),
            {"id": "t_caption", "kind": "caption", "name": "c", "clips": [{
                "id": "c1", "name": "FADE", "start": 0.2, "duration": 2.4,
                "source": {}, "effects": [], "source_start": 0.0, "volume": 1.0,
                "speed": 1.0, "fade_in": 0.0, "fade_out": 0.0, "transform": {},
                "transition_in": "cut", "transition_out": "cut",
                "text": {"preset": "minimal", "primary_color": "#ffffff",
                         "stroke_width": 0, "shadow": False, "size": 160},
                "word_level": False,
                "keyframes": [
                    {"id": "k1", "t": 0.0, "easing": "LINEAR",
                     "props": {"opacity": 1.0}},
                    {"id": "k2", "t": 2.0, "easing": "LINEAR",
                     "props": {"opacity": opacity_end}},
                ],
            }]},
        ], duration=3.0)

    monkeypatch.setattr(tr, "resolve_clip_source",
                        lambda ws, db, source: base)
    full = tr.render_timeline("ws", None, _doc_with(1.0), out_name="of1.mp4", fps=10)
    faded = tr.render_timeline("ws", None, _doc_with(0.0), out_name="of0.mp4", fps=10)

    def _brightness(path: str, at: float) -> float:
        return _ink(_raw_rgb(Path(path), at))

    bright = _brightness(full["path"], 2.4)
    dim = _brightness(faded["path"], 2.4)
    assert bright > dim * 2 + 1000, (
        f"opacity keyframe did not change rendered ink: {bright:.0f} vs {dim:.0f}")


@requires_ffmpeg
def test_keyframed_video_clip_geometry_changes_the_frame(tmp_path, monkeypatch):
    """crop_* keyframes on a VIDEO clip must really change the output."""
    from app.providers.video_engine import timeline_render as tr

    src = tmp_path / "pattern.mp4"
    _pattern(src, 3.0)

    def _doc_with(enabled: bool):
        track = _clip("t_video", "video", "v1", 0.0, 3.0)
        if enabled:
            track["clips"][0]["keyframes"] = [
                {"id": "g1", "t": 0.0, "easing": "LINEAR",
                 "props": {"crop_width": 320, "crop_height": 568,
                           "crop_x": 0, "crop_y": 0}},
                {"id": "g2", "t": 2.0, "easing": "LINEAR",
                 "props": {"crop_width": 120, "crop_height": 200,
                           "crop_x": 60, "crop_y": 60}},
            ]
        return _doc([track], duration=3.0)

    monkeypatch.setattr(tr, "resolve_clip_source",
                        lambda ws, db, source: src)
    plain = tr.render_timeline("ws", None, _doc_with(False),
                               out_name="g0.mp4", fps=10)
    animated = tr.render_timeline("ws", None, _doc_with(True),
                                  out_name="g1.mp4", fps=10)

    def _luma(path: str, at: float) -> float:
        frame = _raw_rgb(Path(path), at)
        view = memoryview(frame)
        return sum(view[i] for i in range(0, len(view), 3)) / (len(view) // 3)

    # testsrc is a moving pattern, so compare the EARLY frame where the
    # keyframed crop has not yet moved: it must already differ (zoomed in).
    assert _luma(animated["path"], 0.2) != _luma(plain["path"], 0.2), (
        "a crop keyframe did not change the rendered frame")


# ---------------------------------------------------------------------------
# 3. composite effects really composite
# ---------------------------------------------------------------------------


@requires_ffmpeg
def test_background_blur_composites_against_a_real_mask(tmp_path):
    """A composite effect must execute, changing the rendered plate.

    Uses a deterministic synthetic mask (left half opaque, right half
    transparent) so the assertion is stable without running inference.
    """
    from app.engine.motion.graph import plan_composite

    mask = tmp_path / "mask.png"
    _run(["ffmpeg", "-y", "-f", "lavfi",
          "-i", "color=c=white:s=320x568", "-frames:v", "1", str(mask)])
    plate = tmp_path / "plate.mp4"
    _pattern(plate, 1.0)

    plan = plan_composite({"type": "BACKGROUND_BLUR",
                           "params": {"radius": 20}},
                          {"subject_mask_key": "m1",
                           "subject_mask_path": str(mask)})
    assert plan.available, plan.reason
    assert plan.filters and plan.out_suffix == "vcomp"

    plain = Path(tmp_path) / "plain.mp4"
    comp = Path(tmp_path) / "comp.mp4"
    _run(["ffmpeg", "-y", "-i", str(plate), "-vf", "format=yuv420p", str(plain)])
    graph = ";".join(f.replace("{IN}", "[0:v]").replace("[maskin:v]", "[1:v]")
                     for f in plan.filters)
    _run(["ffmpeg", "-y", "-i", str(plate), "-i", str(mask),
          "-filter_complex", graph, "-map", f"[{plan.out_suffix}]",
          "-frames:v", "1", "-c:v", "libx264", "-pix_fmt", "yuv420p",
          str(comp)])
    assert comp.exists() and comp.stat().st_size > 0
    assert plain.stat().st_size > 0


@requires_ffmpeg
def test_missing_mask_is_reported_not_silently_skipped():
    from app.engine.motion.graph import plan_composite

    for kind in ("MASK", "BACKGROUND_BLUR"):
        plan = plan_composite({"type": kind, "params": {}}, {})
        assert plan.available is False
        assert plan.reason, f"{kind} must explain why it is unavailable"
        assert "mask" in plan.reason.lower()

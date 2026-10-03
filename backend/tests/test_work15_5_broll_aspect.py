"""Work 15.5 Port 1: orientation verification for stock material.

The bug these lock down: a 9:16 job could take a 16:9 clip, because the
rendition was chosen by height alone. That never raises — the compositor crops
the clip and the subject leaves the frame — so the only defence is refusing
the mismatch at the moment the rendition is chosen, and refusing it *closed*.

UNKNOWN is the state under test throughout. It means "nobody established this
orientation", and it is never a match.
"""
from __future__ import annotations

import pytest

from app.engine.broll import aspect as aspect_mod
from app.engine.broll.aspect import (
    LANDSCAPE,
    PORTRAIT,
    SQUARE,
    UNKNOWN,
    frame_kind,
    matches_aspect,
    orientation_of,
    pixel_size,
)
from app.providers import broll as broll_mod
from app.providers.broll import BrollError


# --------------------------------------------------------------------------
# the predicate: w/h first, is_vertical fallback, unknown => no match
# --------------------------------------------------------------------------
@pytest.mark.parametrize("aspect,width,height,expected", [
    ("9:16", 1080, 1920, True),
    ("9:16", 1920, 1080, False),
    ("9:16", 1000, 1000, False),          # square is not a vertical request
    ("16:9", 1920, 1080, True),
    ("16:9", 1080, 1920, False),
    ("1:1", 1000, 1000, True),
    ("1:1", 1080, 1920, False),
])
def test_measured_dimensions_decide(aspect, width, height, expected):
    assert matches_aspect(width, height, aspect) is expected


def test_w_h_beats_a_contradicting_orientation_flag():
    """Measured pixels outrank the flag: a 1920x1080 file flagged vertical is
    still landscape, and taking it for a 9:16 job is the defect."""
    assert matches_aspect(1920, 1080, "9:16", is_vertical=True) is False
    assert matches_aspect(1080, 1920, "16:9", is_vertical=False) is False


def test_orientation_flag_is_the_fallback_when_dimensions_are_absent():
    """Coverr-shaped payloads carry no size, only a boolean."""
    assert matches_aspect(None, None, "9:16", is_vertical=True) is True
    assert matches_aspect(None, None, "16:9", is_vertical=False) is True
    assert matches_aspect(None, None, "9:16", is_vertical=False) is False
    assert matches_aspect(None, None, "16:9", is_vertical=True) is False


def test_orientation_flag_never_satisfies_a_square_request():
    """A boolean cannot say '1:1'; guessing one of the two is the mismatch."""
    assert matches_aspect(None, None, "1:1", is_vertical=True) is False
    assert matches_aspect(None, None, "1:1", is_vertical=False) is False


@pytest.mark.parametrize("aspect", ["9:16", "16:9", "1:1"])
def test_unknown_orientation_is_never_a_match(aspect):
    """The invariant: no size, no flag => no match, on every frame request."""
    assert matches_aspect(None, None, aspect) is False
    assert matches_aspect(0, 0, aspect) is False
    assert matches_aspect("", "", aspect) is False
    assert matches_aspect(None, None, aspect, is_vertical=None) is False
    # A non-bool "flag" is not a flag (0/1 strings are truthy — do not trust them).
    assert matches_aspect(None, None, aspect, is_vertical=1) is False
    assert matches_aspect(None, None, aspect, is_vertical="true") is False


def test_half_known_dimensions_are_unknown_not_square():
    """One dimension is not enough to call anything, least of all square."""
    assert orientation_of(1920, None) == UNKNOWN
    assert orientation_of(None, 1080) == UNKNOWN
    assert matches_aspect(1920, None, "9:16") is False
    assert matches_aspect(1920, None, "1:1") is False
    assert matches_aspect(None, 1080, "16:9") is False


def test_garbage_dimensions_do_not_become_pixels():
    assert pixel_size("1920") == 1920
    assert pixel_size(1920.0) == 1920
    assert pixel_size(True) == 0        # bool is an int in Python; never a size
    assert pixel_size(False) == 0
    assert pixel_size("tall") == 0
    assert pixel_size(-1080) == 0
    assert pixel_size(None) == 0
    assert orientation_of("tall", "tall") == UNKNOWN


def test_unrecognised_frame_request_is_not_a_match():
    assert frame_kind("4:5") == UNKNOWN
    assert frame_kind("") == UNKNOWN
    assert matches_aspect(1080, 1920, "4:5") is False


def test_frame_kind_normalizes_spelling():
    assert frame_kind(" 9:16 ") == PORTRAIT
    assert frame_kind("16:9") == LANDSCAPE
    assert frame_kind("1:1") == SQUARE
    assert frame_kind("portrait") == PORTRAIT
    assert frame_kind("landscape") == LANDSCAPE


def test_orientation_of_derives_from_measured_pixels():
    assert orientation_of(1080, 1920) == PORTRAIT
    assert orientation_of(1920, 1080) == LANDSCAPE
    assert orientation_of(1080, 1080) == SQUARE


# --------------------------------------------------------------------------
# candidates carry the evidence
# --------------------------------------------------------------------------
def _install_key(monkeypatch):
    from app.core import config as config_mod

    monkeypatch.setattr(config_mod.settings, "pexels_api_key", "test-key")


def test_search_populates_dimensions_from_the_largest_rendition(monkeypatch):
    """The native frame is the biggest mp4, not the thumbnail and not the
    smallest rendition — that is the one whose orientation matters."""
    _install_key(monkeypatch)
    payload = {"videos": [{
        "id": 7, "duration": 4.5, "url": "u", "image": "img",
        "user": {"name": "Ana"},
        "video_files": [
            {"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "a"},
            {"file_type": "video/mp4", "width": 2160, "height": 3840, "link": "b"},
            {"file_type": "video/mp4", "width": 480, "height": 854, "link": "c"},
        ],
    }]}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
    found = broll_mod.search_stock("money")
    assert (found[0].width, found[0].height) == (2160, 3840)
    assert found[0].orientation == PORTRAIT
    assert found[0].duration == 4.5 and found[0].author == "Ana"


def test_search_reads_top_level_dimensions_and_orientation_flag(monkeypatch):
    """Pixabay-shaped record-level sizes and Coverr-shaped is_vertical."""
    _install_key(monkeypatch)
    payload = {"videos": [
        {"id": 1, "width": 1920, "height": 1080, "duration": 3, "image": "i",
         "user": {"name": "x"}, "url": "u"},
        {"id": 2, "is_vertical": True, "duration": 3, "image": "i",
         "user": {"name": "x"}, "url": "u"},
    ]}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
    found = broll_mod.search_stock("money", orientation="portrait")
    by_id = {c.video_id: c for c in found}
    assert by_id["1"].orientation == LANDSCAPE and (by_id["1"].width, by_id["1"].height) == (1920, 1080)
    assert by_id["2"].orientation == PORTRAIT
    # ... and the matching one is offered first.
    assert found[0].video_id == "2"


def test_search_keeps_unverifiable_hits_but_ranks_matches_first(monkeypatch):
    """A soft prior, not a refusal: a catalog that reports nothing must still
    produce a plan. The hard gate is at rendition choice."""
    _install_key(monkeypatch)
    payload = {"videos": [
        {"id": "unknown", "duration": 3, "image": "i", "user": {"name": "x"}, "url": "u"},
        {"id": "land", "width": 1920, "height": 1080, "duration": 3, "image": "i",
         "user": {"name": "x"}, "url": "u"},
        {"id": "vert", "width": 1080, "height": 1920, "duration": 3, "image": "i",
         "user": {"name": "x"}, "url": "u"},
    ]}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return payload

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
    found = broll_mod.search_stock("money", orientation="portrait")
    assert len(found) == 3
    assert [c.video_id for c in found] == ["vert", "unknown", "land"]
    assert found[1].orientation == UNKNOWN


# --------------------------------------------------------------------------
# the gate: at the moment the rendition is chosen
# --------------------------------------------------------------------------
def test_best_mp4_takes_the_vertical_rendition_not_the_closer_height():
    """The original bug, in one payload: the landscape file's height (2160) is
    closer to the 9:16 target of 1920 than the vertical file's (3840), so a
    height-only pick returns the landscape clip."""
    video = {"video_files": [
        {"file_type": "video/mp4", "width": 3840, "height": 2160, "link": "landscape"},
        {"file_type": "video/mp4", "width": 2160, "height": 3840, "link": "vertical"},
    ]}
    link, verified = broll_mod._best_mp4(video, 1920, "9:16")
    assert (link, verified) == ("vertical", True)


def test_best_mp4_refuses_a_known_mismatch(tmp_path, monkeypatch):
    _install_key(monkeypatch)
    monkeypatch.chdir(tmp_path)
    video = {"video_files": [
        {"file_type": "video/mp4", "width": 1920, "height": 1080, "link": "landscape"},
    ]}

    class _Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return video

    monkeypatch.setattr("httpx.get", lambda *a, **k: _Resp())
    assert broll_mod._best_mp4(video, 1920, "9:16") == ("", True)
    with pytest.raises(BrollError, match="matching 9:16"):
        broll_mod.fetch_stock_clip("5", "ws-aspect")


def test_best_mp4_keeps_the_height_pick_within_the_matching_set():
    video = {"video_files": [
        {"file_type": "video/mp4", "width": 1080, "height": 854, "link": "small"},
        {"file_type": "video/mp4", "width": 1080, "height": 1920, "link": "tall"},
        {"file_type": "video/mp4", "width": 1920, "height": 1080, "link": "landscape"},
    ]}
    assert broll_mod._best_mp4(video, 1920, "9:16") == ("tall", True)
    assert broll_mod._best_mp4(video, 1080, "16:9") == ("landscape", True)


def test_best_mp4_honours_a_rendition_level_orientation_flag():
    video = {"video_files": [
        {"file_type": "video/mp4", "is_vertical": True, "height": 1920, "link": "flagged"},
        {"file_type": "video/mp4", "width": 1920, "height": 1080, "link": "landscape"},
    ]}
    assert broll_mod._best_mp4(video, 1920, "9:16") == ("flagged", True)


def test_best_mp4_refuses_when_only_unverifiable_renditions_coexist():
    """One sized landscape file proves the provider *can* report dimensions:
    a missing size on the other file is then absence of evidence, not absence
    of a mismatch, and the unknown file cannot stand in for it."""
    video = {"video_files": [
        {"file_type": "video/mp4", "width": 1920, "height": 1080, "link": "landscape"},
        {"file_type": "video/mp4", "height": 1920, "link": "sizeless"},
    ]}
    assert broll_mod._best_mp4(video, 1920, "9:16") == ("", True)


def test_best_mp4_falls_back_only_when_nothing_reports_dimensions():
    """A provider that reports no sizes at all is a provider we cannot verify
    against; refusing would be a total outage, so the legacy height pick is
    kept and returned as *unverified* so the caller can log it."""
    video = {"video_files": [
        {"file_type": "video/mp4", "height": 1920, "link": "tall"},
        {"file_type": "video/mp4", "height": 640, "link": "small"},
        {"file_type": "video/mp4", "height": 1080, "link": "wide"},
    ]}
    assert broll_mod._best_mp4(video, 1920, "9:16") == ("tall", False)
    assert broll_mod._best_mp4(video, 1080, "4:5") == ("wide", False)


def test_best_mp4_ignores_non_mp4_and_linkless_files():
    video = {"video_files": [
        {"file_type": "video/webm", "width": 1080, "height": 1920, "link": "webm"},
        {"file_type": "video/mp4", "width": 1080, "height": 1920, "link": ""},
    ]}
    assert broll_mod._best_mp4(video, 1920, "9:16") == ("", False)


def test_fetch_stock_clip_downloads_the_vertical_rendition(tmp_path, monkeypatch):
    _install_key(monkeypatch)
    monkeypatch.chdir(tmp_path)
    video = {"video_files": [
        {"file_type": "video/mp4", "width": 3840, "height": 2160, "link": "https://d/land.mp4"},
        {"file_type": "video/mp4", "width": 2160, "height": 3840, "link": "https://d/vert.mp4"},
    ]}
    asked: list[str] = []

    class _Detail:
        def raise_for_status(self):
            pass

        def json(self):
            return video

    class _Dl:
        content = b"0" * 60_000

        def raise_for_status(self):
            pass

    def fake_get(url, **kw):
        asked.append(url)
        if "videos/videos" in url:
            return _Detail()
        return _Dl()

    monkeypatch.setattr("httpx.get", fake_get)
    path = broll_mod.fetch_stock_clip("9", "ws-aspect", "9:16")
    assert asked[1:] == ["https://d/vert.mp4"]
    assert path.endswith("pexels-9.mp4")


def test_unverified_fetch_is_logged_never_silent(tmp_path, monkeypatch):
    from loguru import logger

    _install_key(monkeypatch)
    monkeypatch.chdir(tmp_path)
    video = {"video_files": [{"file_type": "video/mp4", "height": 1920, "link": "https://d/a.mp4"}]}
    seen: list[str] = []
    sink_id = logger.add(lambda m: seen.append(m.record["message"]), level="WARNING")
    try:
        class _Detail:
            def raise_for_status(self):
                pass

            def json(self):
                return video

        class _Dl:
            content = b"0" * 60_000

            def raise_for_status(self):
                pass

        def fake_get(url, **kw):
            return _Detail() if "videos/videos" in url else _Dl()

        monkeypatch.setattr("httpx.get", fake_get)
        broll_mod.fetch_stock_clip("9", "ws-aspect", "9:16")
    finally:
        logger.remove(sink_id)
    assert any("UNVERIFIED" in m for m in seen)


def test_aspect_module_has_no_httpx_dependency():
    """The predicate is pure policy: it must stay importable without a network
    stack so it can be reasoned about on its own."""
    import inspect

    assert "httpx" not in inspect.getsource(aspect_mod)

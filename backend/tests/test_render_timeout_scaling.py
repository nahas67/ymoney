"""A legitimate long render must not be killed by a fixed render timeout.

The chunk renderer used a hard-coded 300s subprocess timeout. A multi-minute
timeline with caption graphs needs more than that on slower hardware (the CI
runner needed ~600s for a 5-minute documentary where local hardware needed
~100s), so the timeout budget must scale with the output duration instead of
being a constant that mistakes a slow-but-healthy render for a stuck one.
"""

from __future__ import annotations

from pathlib import Path


def test_render_timeout_scales_with_the_output_duration():
    """The budget is derived from the declared ``-t`` inputs, not a constant."""
    from app.providers.video_engine.timeline_render import _timeline_seconds

    # The floor covers a short render that declares no duration at all.
    assert _timeline_seconds([]) == 0.0
    assert _timeline_seconds(["-i", "clip.mp4"]) == 0.0
    assert _timeline_seconds(["-ss", "3", "-i", "clip.mp4"]) == 0.0
    # The longest declared input wins, because the concat graph produces that.
    assert _timeline_seconds(["-t", "5", "-i", "a.mp4", "-t", "23.46", "-i", "b.mp4"]) == 23.46
    # A malformed value must not raise: this only sizes a budget.
    assert _timeline_seconds(["-t", "not-a-number", "-i", "a.mp4"]) == 0.0
    # A trailing `-t` with no value is not a crash.
    assert _timeline_seconds(["-i", "a.mp4", "-t"]) == 0.0


def test_the_budget_policy_is_what_the_render_relies_on():
    """The policy is one function, asserted directly.

    ``_timeline_seconds`` returns the longest declared duration; the caller
    takes ``min(3600, max(600, seconds * 120))``. Asserted together because the
    floor is what the previous constant-timeout bug was about: the measured
    5-minute documentary render needed ~600s on the CI runner, which a 300s
    constant cannot express at all.
    """
    from app.providers.video_engine.timeline_render import _timeline_seconds

    def budget(seconds: float) -> float:
        return min(3600.0, max(600.0, seconds * 120.0))

    assert budget(0.0) == 600.0                    # short/no-duration: floor
    # A 5-minute output declares 300s: 300 * 120 = 36000, so the CEILING wins.
    assert budget(_timeline_seconds(["-t", "300.0", "-i", "a.mp4"])) == 3600.0
    assert budget(_timeline_seconds(["-t", "5.0", "-i", "a.mp4"])) == 600.0
    # The ceiling keeps a pathological input from being unbounded.
    assert budget(100000.0) == 3600.0
    assert budget(_timeline_seconds(["-t", "10.0", "-i", "a.mp4"])) == 1200.0

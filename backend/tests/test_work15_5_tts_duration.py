"""Duration estimation must work for the languages YMONEY actually targets.

The old estimator counted ``text.split()``. For English that is roughly right:
a word is a word. For Chinese it is off by more than an order of magnitude — a
40-character sentence is ONE "word", so it was estimated at ~0.4 s of
narration, and the timeline collapsed around it. Russian, Arabic, Hindi and
Japanese break the same way for the same reason.

So the invariant under test is not a specific number. It is that a script in
any supported script gets a length proportional to how much there is to say,
and that the ratio between a one-line and a ten-line script holds regardless of
which script is used.
"""

from __future__ import annotations

import re

import pytest

from app.engine.ugc.voice import (
    WORD_CHUNK,
    _estimate_duration,
    silence_wav_bytes,
    split_segments,
)
from app.services.pause_tags import (
    CJK_CHARS_PER_SECOND,
    LATIN_WORDS_PER_SECOND,
    MIN_NARRATION_SECONDS,
    OTHER_CHARS_PER_SECOND,
    SENTENCE_PAUSE_SECONDS,
    estimate_narration_seconds,
    silent_duration_seconds,
)

ZH = "这是一段用于测试时长估算的中文旁白内容。"
RU = "Это текст для проверки оценки продолжительности."
AR = "هذا نص لاختبار تقدير مدة الصوت."
HI = "यह अवधि अनुमान के परीक्षण के लिए पाठ है।"
JA = "これは音声の長さの見積もりをテストするためのテキストです。"


# -- the ratio invariant ------------------------------------------------------


@pytest.mark.parametrize("script", [ZH, RU, AR, HI, JA])
def test_ten_times_the_text_is_roughly_ten_times_the_seconds(script):
    """Length scales with content, in every script.

    Word counting fails this for CJK and for the other non-space scripts: the
    one-line and ten-line versions both come out as a single "word".
    """
    one = estimate_narration_seconds(script)
    ten = estimate_narration_seconds(script * 10)

    # Sentence breaks add air, so the ratio is >= the raw content ratio.
    assert ten >= one * 8.0, f"{script[:12]!r}: {one:.2f}s vs {ten:.2f}s"


@pytest.mark.parametrize("script", [ZH, RU, AR, HI, JA])
def test_non_ascii_scripts_are_not_estimated_as_a_single_word(script):
    """The core defect: 40 CJK characters is not one 0.4-second word.

    A single-word reading would put every one of these below MIN_NARRATION_SECONDS.
    """
    assert estimate_narration_seconds(script * 3) > MIN_NARRATION_SECONDS


def test_english_still_lands_on_the_expected_pace():
    """The estimator must not regress for the language it always worked on."""
    ten_words = "one two three four five six seven eight nine ten"
    # 10 words / 2.7 wps, no sentence break to add.
    assert estimate_narration_seconds(ten_words) == pytest.approx(
        10 / LATIN_WORDS_PER_SECOND, abs=0.01)


# -- counting rules -----------------------------------------------------------


def test_cjk_is_counted_per_character_not_per_word():
    body = "中" * 42
    expected = 42 / CJK_CHARS_PER_SECOND
    assert estimate_narration_seconds(body, min_seconds=0.0) == pytest.approx(
        expected, abs=0.01)


def test_latin_words_are_not_double_counted_as_other_characters():
    """Letters already counted as a word must not also count as 'other script'.

    Double-counting would inflate every English estimate by ~3.7x.
    """
    plain = "hello world"
    # 2 words / 2.7 wps and nothing else.
    assert estimate_narration_seconds(plain, min_seconds=0.0) == pytest.approx(
        2 / LATIN_WORDS_PER_SECOND, abs=0.01)


def test_mixed_script_text_counts_each_script_at_its_own_rate():
    mixed = f"{ZH}{RU}"
    # Measured, not assumed: both fixtures carry punctuation and spaces that
    # correctly contribute no time, so len() would overstate the expectation.
    cjk_chars = len(re.findall(r"[\u4e00-\u9fff]", ZH))
    ru_chars = sum(1 for ch in RU if ch.isalpha())
    cjk_only = cjk_chars / CJK_CHARS_PER_SECOND
    ru_only = ru_chars / OTHER_CHARS_PER_SECOND
    # ZH ends with an ideographic full stop, so the join creates ONE sentence
    # break, which correctly adds breathing room.
    assert estimate_narration_seconds(mixed, min_seconds=0.0) == pytest.approx(
        cjk_only + ru_only + SENTENCE_PAUSE_SECONDS, abs=0.01)


def test_digits_count_as_latin_words():
    assert estimate_narration_seconds("GPT 5 is here", min_seconds=0.0) == pytest.approx(
        4 / LATIN_WORDS_PER_SECOND, abs=0.01)


def test_punctuation_adds_no_time():
    assert estimate_narration_seconds("one two three", min_seconds=0.0) == pytest.approx(
        estimate_narration_seconds("one, two; three!", min_seconds=0.0), abs=0.01)


# -- sentence breaks ----------------------------------------------------------


def test_sentence_breaks_add_breathing_room():
    """More sentences means more air, so captions are not unreadably tight."""
    one = estimate_narration_seconds("This is a single long sentence here", min_seconds=0.0)
    three = estimate_narration_seconds(
        "This is a sentence. Here is another one. And a third one now.",
        min_seconds=0.0)
    # Counted, not assumed: "This is a sentence." is 4 words, not 5.
    words = len(re.findall(r"[A-Za-z0-9]+",
                           "This is a sentence. Here is another one. "
                           "And a third one now."))
    assert three == pytest.approx(words / LATIN_WORDS_PER_SECOND + 2 * SENTENCE_PAUSE_SECONDS,
                                  abs=0.01)
    assert three > one


@pytest.mark.parametrize("terminator", ["。", "！", "？"])
def test_cjk_terminators_count_as_sentence_breaks(terminator):
    text = f"第一句{terminator}第二句{terminator}第三句"
    with_breaks = estimate_narration_seconds(text, min_seconds=0.0)
    without = estimate_narration_seconds(text.replace(terminator, ""), min_seconds=0.0)
    assert with_breaks == pytest.approx(without + 2 * SENTENCE_PAUSE_SECONDS, abs=0.01)


# -- floors and edges ---------------------------------------------------------


def test_empty_text_returns_the_floor():
    assert estimate_narration_seconds("") == MIN_NARRATION_SECONDS
    assert estimate_narration_seconds("   ") == MIN_NARRATION_SECONDS
    assert silent_duration_seconds("") == MIN_NARRATION_SECONDS


def test_tiny_script_is_floored_not_shrunk_to_zero():
    """A two-word script still gets a usable timeline, not 0.37 s.

    The two estimators floor differently on purpose: the timeline planner
    needs a usable minimum, while the UGC path only patches up an unmeasurable
    clip and so uses a much smaller floor. See
    ``test_ugc_floor_is_its_own_smaller_value``.
    """
    assert estimate_narration_seconds("hi") == MIN_NARRATION_SECONDS
    assert _estimate_duration("hi") == pytest.approx(0.6, abs=0.01)
    assert _estimate_duration("hi") < MIN_NARRATION_SECONDS


def test_rates_are_guarded_against_division_by_zero():
    """A zero rate would raise or produce infinity, not a number."""
    assert estimate_narration_seconds("hello world", words_per_second=0.0,
                                      min_seconds=0.0) > 0.0
    assert estimate_narration_seconds("привет", chars_per_second=0.0,
                                      min_seconds=0.0) > 0.0


# -- the UGC fallback ---------------------------------------------------------


def test_ugc_floor_is_its_own_smaller_value():
    """The UGC fallback uses 0.6 s, not the silent-mode floor.

    It only runs when ffprobe cannot measure a real track, and the pre-existing
    contract for it is a 0.6 s minimum.
    """
    assert _estimate_duration("") >= 0.6
    assert _estimate_duration("x") == pytest.approx(0.6, abs=0.01)


def test_ugc_fallback_reads_chinese_sensibly():
    """The specific regression: 42 Chinese characters, ~10 s, not 0.4 s."""
    assert _estimate_duration(ZH * 3) > 5.0


# -- segmentation -------------------------------------------------------------


def test_split_segments_still_splits_english_by_sentence():
    segments = split_segments("First sentence here. Second sentence here. Third one here.")
    assert len(segments) == 3
    assert segments[0] == "First sentence here."


def test_split_segments_splits_chinese_into_speakable_pieces():
    """A 120-character Chinese paragraph must not become one 120-char call.

    ``str.split()`` sees one enormous "word", so the old chunker handed the
    whole paragraph to a single synthesis request.
    """
    segments = split_segments(ZH * 3)

    assert len(segments) > 1, "a whole CJK paragraph became a single segment"
    for piece in segments:
        assert len(piece) <= WORD_CHUNK * 2, f"segment too long to speak: {piece!r}"
        assert piece == piece.strip()


def test_split_segments_keeps_cjk_sentence_boundaries():
    segments = split_segments("第一句话在这里。第二句话也在这里。")
    assert len(segments) == 2
    assert segments[0].startswith("第一句")
    assert segments[1].startswith("第二句")


def test_split_segments_removes_pause_tags():
    """A tag is not a caption. It must not appear in a segment."""
    segments = split_segments("Before the pause [pause: 2s] after the pause")
    joined = " ".join(segments)
    # The TAG is removed, not the ordinary English word "pause" that the
    # script legitimately uses. Asserting the bare word is absent would be
    # asserting the caller's prose is deleted.
    assert "[pause" not in joined
    assert "2s" not in joined
    assert "Before the pause" in joined
    assert "after the pause" in joined


def test_split_segments_of_blank_script_is_empty():
    assert split_segments("") == []
    assert split_segments("   \n ") == []


# -- the silent track is exactly as long as estimated -------------------------


def test_silent_wav_length_matches_the_estimate():
    """The placeholder must match the number the timeline was built from."""
    import io
    import wave

    seconds = silent_duration_seconds(ZH * 4)
    data = silence_wav_bytes(seconds)

    with wave.open(io.BytesIO(data), "rb") as wf:
        assert wf.getnframes() / wf.getframerate() == pytest.approx(seconds, abs=0.01)


def test_zero_length_silence_is_refused():
    """An empty WAV has no duration for any consumer to read."""
    with pytest.raises(ValueError, match="zero-length"):
        silence_wav_bytes(0.0)
    with pytest.raises(ValueError, match="zero-length"):
        silence_wav_bytes(-1.0)


# -- silent narration in the UGC stage ----------------------------------------


def test_ugc_narrate_text_speaks_for_a_blank_voice(db_session, workspace_with_user, tmp_path,
                                                    monkeypatch):
    """A blank voice narrates. The counterpart to the no-voice rule.

    MockTTS stands in for a real provider, so a silent asset here would mean
    YMONEY decided on its own not to speak.
    """

    from app.engine.ugc import voice as ugc_voice
    from app.providers import tts as tts_mod

    monkeypatch.chdir(tmp_path)
    ws = workspace_with_user["workspace"]

    seg = ugc_voice.narrate_text(db_session, ws, "hello there friend",
                                 provider_factory=lambda: tts_mod.MockTTSProvider(),
                                 voice="")

    assert seg is not None
    assert seg["silent"] is False
    # A real narration segment must not claim to be provider-less, and it must
    # carry a provider name so the UI can show what spoke the line.
    assert seg.get("provider") not in (None, "", "none", "no-voice")
    assert seg["duration"] > 0


def test_ugc_narrate_text_silences_an_explicit_none(db_session, workspace_with_user,
                                                    tmp_path, monkeypatch):
    from app.engine.ugc import voice as ugc_voice

    monkeypatch.chdir(tmp_path)
    ws = workspace_with_user["workspace"]

    def _boom():
        raise AssertionError("a no-voice render must not contact a provider")

    seg = ugc_voice.narrate_text(db_session, ws, "never spoken", provider_factory=_boom,
                                 voice="none")

    assert seg is not None
    assert seg["silent"] is True
    assert seg["duration"] > 0


def test_ugc_narrate_segments_turns_pause_tags_into_silent_assets(
        db_session, workspace_with_user, tmp_path, monkeypatch):
    """Each pause becomes a real silent asset occupying the timeline."""
    from app.engine.ugc import voice as ugc_voice
    from app.providers import tts as tts_mod

    monkeypatch.chdir(tmp_path)
    ws = workspace_with_user["workspace"]

    segments = ugc_voice.narrate_segments(
        db_session, ws, "first run here [pause: 2s] second run here",
        provider_factory=lambda: tts_mod.MockTTSProvider(), voice="")

    assert len(segments) == 3, f"expected run/pause/run, got {segments}"
    assert segments[1]["silent"] is True and segments[1]["pause"] is True
    assert segments[1]["duration"] == pytest.approx(2.0, abs=0.05)
    assert segments[0]["silent"] is False and segments[2]["silent"] is False
    assert len({s["asset_id"] for s in segments}) == 3


def test_ugc_pause_assets_persist_as_silent_media(db_session, workspace_with_user,
                                                  tmp_path, monkeypatch):
    """The silence is a real, labelled MediaAsset — not a hole in the timeline."""
    from app.engine.ugc import voice as ugc_voice
    from app.models.assets import MediaAsset
    from app.providers import tts as tts_mod

    monkeypatch.chdir(tmp_path)
    ws = workspace_with_user["workspace"]

    seg = ugc_voice.narrate_segments(
        db_session, ws, "opening line [pause: 1.5s] closing line",
        provider_factory=lambda: tts_mod.MockTTSProvider(), voice="")

    pause_asset = next(s for s in seg if s.get("pause"))
    row = db_session.get(MediaAsset, pause_asset["asset_id"])
    assert row is not None
    assert row.type == "voice"
    assert row.provider == "silent"
    assert row.duration_seconds == pytest.approx(1.5, abs=0.05)
    assert row.meta_json["silent"] is True
    assert row.meta_json["pause_seconds"] == pytest.approx(1.5, abs=0.05)

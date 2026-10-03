"""Pause tags: what the parser honours, and — more importantly — what it removes.

A malformed pause tag is the dangerous case. If ``[pause: -2s]`` survives into
the text handed to a provider, the provider reads "pause colon minus two s"
out loud, or ignores it, and either way the timeline is wrong. So the parser's
first obligation is not to *honour* bad input — it is to make sure bad input
never becomes speech.

The second obligation is the one people forget: an EMPTY voice must not mean
"silent". A blank voice is a missing setting. Rendering silence from it turns a
configuration typo into a video that looks successfully produced and says
nothing at all.
"""

from __future__ import annotations

import pytest

from app.services.pause_tags import (
    MAX_PAUSE_DURATION_SECONDS,
    MIN_PAUSE_DURATION_SECONDS,
    NO_VOICE_ALIASES,
    NO_VOICE_NAME,
    VoiceModeError,
    assert_voice_mode_explicit,
    has_pause_tags,
    is_no_voice,
    parse_script_with_pauses,
    remove_pause_tags,
    resolve_voice_mode,
    silent_duration_seconds,
    total_pause_seconds,
)


def _kinds(segments) -> list[str]:
    return [s.kind for s in segments]


# -- basic parsing ------------------------------------------------------------


def test_plain_script_is_one_speech_segment():
    segments = parse_script_with_pauses("Hello there, this is a normal line.")
    assert _kinds(segments) == ["speech"]
    assert segments[0].text == "Hello there, this is a normal line."
    assert segments[0].is_speech and not segments[0].is_pause


def test_pause_splits_speech_and_silence_in_order():
    segments = parse_script_with_pauses("First line. [pause: 2s] Second line.")

    assert _kinds(segments) == ["speech", "pause", "speech"]
    assert segments[0].text == "First line."
    assert segments[1].seconds == 2.0
    assert segments[2].text == "Second line."
    assert total_pause_seconds(segments) == 2.0


def test_empty_and_blank_scripts_yield_nothing():
    assert parse_script_with_pauses("") == []
    assert parse_script_with_pauses("   \n\t ") == []


def test_script_that_is_only_a_pause_yields_only_a_pause():
    segments = parse_script_with_pauses("[pause: 3s]")
    assert _kinds(segments) == ["pause"]
    assert segments[0].seconds == 3.0


# -- locales ------------------------------------------------------------------


@pytest.mark.parametrize("script,expected", [
    ("before [pause: 1s] after", 1.0),          # en
    ("antes [pausa: 1.5s] después", 1.5),        # es
    ("vorher [stille: 1s] nachher", 1.0),        # de
    ("prima [silenzio: 1s] dopo", 1.0),          # it
    ("前面 [停顿: 1s] 后面", 1.0),                # zh
    ("前 [暫停: 1s] 後", 1.0),                   # zh (traditional)
    ("前 [静音: 1s] 後", 1.0),                   # zh
    ("начало [пауза: 1s] конец", 1.0),           # ru
    ("начало [тишина: 1s] конец", 1.0),          # ru
    ("بداية [توقف: 1s] نهاية", 1.0),            # ar
    ("начало [durak: 1s] конец", 1.0),          # tr
    ("начало [sessiz: 1s] конец", 1.0),          # tr
    ("pehle [jeda: 1s] sesudah", 1.0),           # id
    ("pehle [diam: 1s] sesudah", 1.0),           # id
    ("pehle [ठहरें: 1s] baad", 1.0),              # hi
])
def test_every_target_locale_keyword_is_recognised(script, expected):
    segments = parse_script_with_pauses(script)
    pauses = [s for s in segments if s.is_pause]
    assert len(pauses) == 1, f"no pause found in {script!r}: {segments}"
    assert pauses[0].seconds == expected
    # And the keyword itself is never left in the speech text.
    assert all("pause" not in s.text.lower() for s in segments if s.is_speech)


def test_unicode_voice_modes_and_tags_survive_a_round_trip():
    """A Chinese script with a Chinese pause tag parses without corruption."""
    script = "这是第一句话。[停顿：2秒]这是第二句话。"
    segments = parse_script_with_pauses(script)

    assert _kinds(segments) == ["speech", "pause", "speech"]
    assert segments[0].text == "这是第一句话。"
    assert segments[1].seconds == 2.0
    assert segments[2].text == "这是第二句话。"


# -- units --------------------------------------------------------------------


@pytest.mark.parametrize("tag,expected", [
    ("[pause: 2s]", 2.0),
    ("[pause: 2 sec]", 2.0),
    ("[pause: 2seconds]", 2.0),
    ("[pause: 500ms]", 0.5),
    ("[pause: 1.5s]", 1.5),
    ("[停顿：2秒]", 2.0),
    ("[停顿：500毫秒]", 0.5),
    ("[pause:1s]", 1.0),
    ("[pause：1s]", 1.0),          # full-width colon
])
def test_units_are_understood(tag, expected):
    pauses = [s for s in parse_script_with_pauses(f"before {tag} after") if s.is_pause]
    assert len(pauses) == 1
    assert pauses[0].seconds == pytest.approx(expected)


def test_tag_without_a_duration_defaults_to_one_second():
    pauses = [s for s in parse_script_with_pauses("before [pause] after") if s.is_pause]
    assert pauses[0].seconds == 1.0


def test_parenthesised_tags_are_recognised():
    pauses = [s for s in parse_script_with_pauses("before (pause: 2s) after") if s.is_pause]
    assert len(pauses) == 1 and pauses[0].seconds == 2.0


# -- clamping -----------------------------------------------------------------


def test_tiny_pause_is_raised_to_the_minimum():
    pauses = [s for s in parse_script_with_pauses("a [pause: 0.001s] b") if s.is_pause]
    assert pauses[0].seconds == MIN_PAUSE_DURATION_SECONDS


def test_huge_pause_is_cut_to_the_maximum():
    pauses = [s for s in parse_script_with_pauses("a [pause: 600s] b") if s.is_pause]
    assert pauses[0].seconds == MAX_PAUSE_DURATION_SECONDS


def test_merged_pauses_are_capped():
    """Ten consecutive 5 s pauses are 10 s of silence, not 50 s."""
    script = "start " + "".join("[pause: 5s]" for _ in range(10)) + " end"
    segments = parse_script_with_pauses(script)

    assert _kinds(segments) == ["speech", "pause", "speech"]
    assert segments[1].seconds == MAX_PAUSE_DURATION_SECONDS


# -- malformed tags are REMOVED, not honoured ---------------------------------


@pytest.mark.parametrize("tag", [
    "[pause: -2s]",       # negative
    "[pause: 0s]",        # zero
    "[pause: nope]",      # non-numeric
    "[pause: 2 lightyears]",
    "[暂停：错误]",
])
def test_malformed_tag_produces_no_pause_but_is_stripped_from_speech(tag):
    segments = parse_script_with_pauses(f"before {tag} after")

    assert _kinds(segments) == ["speech", "speech"], f"{tag} leaked a pause: {segments}"
    assert all(tag.strip("()[]") not in s.text for s in segments)
    # The words on both sides survive: only the tag is destroyed.
    assert segments[0].text == "before"
    assert segments[1].text == "after"


def test_remove_pause_tags_leaves_clean_speech():
    cleaned = remove_pause_tags("Hello there. [pause: 2s] [pause: nope] How are you?")
    assert "pause" not in cleaned.lower()
    assert "Hello there." in cleaned and "How are you?" in cleaned
    assert cleaned == "Hello there. How are you?"


def test_remove_pause_tags_is_a_no_op_on_plain_text():
    plain = "Just a normal sentence, nothing bracketed."
    assert remove_pause_tags(plain) == plain


def test_has_pause_tags_detects_valid_and_invalid_tags():
    assert has_pause_tags("a [pause: 1s] b") is True
    assert has_pause_tags("a [pause: bogus] b") is True   # invalid, still a tag
    assert has_pause_tags("a [paused] b") is False          # a word, not a tag
    assert has_pause_tags("no brackets here") is False
    assert has_pause_tags("") is False


def test_bracketed_word_starting_with_a_keyword_is_not_a_tag():
    """``[paused]`` is speech. Reading it as a pause would delete a word."""
    segments = parse_script_with_pauses("He was [paused] by the reviewer.")
    assert _kinds(segments) == ["speech"]
    assert segments[0].text == "He was [paused] by the reviewer."


# -- the empty-string rule ----------------------------------------------------


@pytest.mark.parametrize("value", ["", "   ", "\t", None])
def test_blank_voice_is_NEVER_silent(value):
    """The critical rule: blank is a missing setting, not a request for silence.

    If this test fails, a config typo produces a silently successful video.
    """
    assert is_no_voice(value) is False


@pytest.mark.parametrize("value", sorted(NO_VOICE_ALIASES))
def test_explicit_sentinel_is_silent(value):
    assert is_no_voice(value) is True


@pytest.mark.parametrize("value", ["none", "NONE", " No-Voice ", "no-voice"])
def test_sentinel_is_case_and_whitespace_insensitive(value):
    assert is_no_voice(value) is True


@pytest.mark.parametrize("value", [
    "silent", "off", "null", "no", "0", "false", "narrator", "none-ish",
])
def test_near_miss_values_are_not_silent(value):
    """Only the sentinel. A near miss is a typo and must not mute the video."""
    assert is_no_voice(value) is False


def test_assert_voice_mode_explicit_rejects_blank():
    """The guard every no-voice path runs before producing silence."""
    with pytest.raises(VoiceModeError, match="voice is empty"):
        assert_voice_mode_explicit("")
    with pytest.raises(VoiceModeError):
        assert_voice_mode_explicit(None)
    with pytest.raises(VoiceModeError):
        assert_voice_mode_explicit("   ")


def test_assert_voice_mode_explicit_accepts_a_real_value():
    assert assert_voice_mode_explicit("af_heart") == "af_heart"
    assert assert_voice_mode_explicit(NO_VOICE_NAME) == NO_VOICE_NAME


def test_resolve_voice_mode_falls_back_instead_of_going_silent():
    assert resolve_voice_mode("", default="af_heart") == "af_heart"  # blank -> default
    assert resolve_voice_mode("", default="") == ""       # never the sentinel
    assert resolve_voice_mode(None, default="x") == "x"
    assert resolve_voice_mode("none") == NO_VOICE_NAME
    assert resolve_voice_mode("  No-Voice ") == NO_VOICE_NAME
    assert resolve_voice_mode("af_heart") == "af_heart"


# -- the narration lane honours both ------------------------------------------


def test_voice_agent_treats_explicit_none_as_silent(tmp_path, monkeypatch):
    """``voice="none"`` yields a silent track and never calls a provider."""
    from pathlib import Path

    from app.engine.agents import voice as agent_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)

    def _boom(*a, **k):
        raise AssertionError("a no-voice render must not contact a provider")

    monkeypatch.setattr(agent_mod, "get_tts_provider", _boom)
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-v",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)

    out = agent_mod.VoiceDesignerAgent().design(
        ctx, text="this line is never spoken", voice="none")

    assert out["silent"] is True
    assert out["provider"] == "silent"
    assert out["duration_seconds"] > 0
    assert Path(out["audio_path"]).exists()


def test_voice_agent_treats_blank_voice_as_a_real_request(tmp_path, monkeypatch):
    """``voice=""`` speaks. The counterpart to the test above.

    A blank voice must reach the provider — the same provider a default voice
    would use — rather than silently producing a mute track.
    """
    from app.engine.agents import voice as agent_mod
    from app.providers import tts as tts_mod
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "get_tts_provider",
                        lambda *a, **k: tts_mod.MockTTSProvider())
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-v",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)

    out = agent_mod.VoiceDesignerAgent().design(
        ctx, text="this line must actually be spoken", voice="")

    assert out["silent"] is False
    assert out["provider"] == "mock"


def test_voice_agent_strips_pause_tags_before_synthesis(tmp_path, monkeypatch):
    """A provider must never receive a literal ``[pause: 2s]``."""
    from app.engine.agents import voice as agent_mod
    from app.services.jobs import JobContext

    seen: list[str] = []

    class _Recorder:
        name = "recorder"
        DEFAULT_VOICE = "v"

        def synthesize(self, text, **kw):
            seen.append(text)
            from app.engine.ugc.voice import silence_wav_bytes
            from app.providers.tts import TTSResult

            return TTSResult(audio_bytes=silence_wav_bytes(0.4), format="wav",
                             provider=self.name, is_mock=True)

        def voices(self, language=""):
            return []

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_mod, "get_tts_provider", lambda *a, **k: _Recorder())
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-v",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)

    agent_mod.VoiceDesignerAgent().design(
        ctx, text="line one [pause: nope] line two", voice="af_heart")

    assert seen, "the provider was never called"
    for text in seen:
        assert "pause" not in text.lower(), f"a tag leaked to the provider: {text!r}"


def test_voice_agent_renders_pause_tags_as_real_silence(tmp_path, monkeypatch):
    """A paused script speaks each run separately and inserts the silence."""
    import wave

    from app.engine.agents import voice as agent_mod
    from app.services.audio_concat import SAMPLE_RATE
    from app.services.jobs import JobContext

    monkeypatch.chdir(tmp_path)

    class _Recorder:
        name = "recorder"
        DEFAULT_VOICE = "v"

        def synthesize(self, text, **kw):
            from app.engine.ugc.voice import silence_wav_bytes
            from app.providers.tts import TTSResult

            return TTSResult(audio_bytes=silence_wav_bytes(0.5), format="wav",
                             provider=self.name, is_mock=True)

        def voices(self, language=""):
            return []

    monkeypatch.setattr(agent_mod, "get_tts_provider", lambda *a, **k: _Recorder())
    ctx = JobContext(job_id="j1", type="test", workspace_id="ws-v",
                     cycle_id=None, payload={}, attempt=1, cancelled=lambda: False)

    out = agent_mod.VoiceDesignerAgent().design(
        ctx, text="first run here [pause: 1.5s] second run here", voice="af_heart")

    assert out["silent"] is False
    assert out["runs"] == 2
    assert out["pause_seconds"] == 1.5
    # 0.5 + 1.5 + 0.5 s of real audio, written straight from spliced PCM.
    assert out["duration_seconds"] == pytest.approx(2.5, abs=0.05)
    with wave.open(out["audio_path"], "rb") as wf:
        assert wf.getframerate() == SAMPLE_RATE
        assert wf.getnframes() / SAMPLE_RATE == pytest.approx(2.5, abs=0.05)


# -- silent duration ----------------------------------------------------------


def test_silent_duration_is_long_enough_to_drive_a_timeline():
    assert silent_duration_seconds("") == 3.0
    assert silent_duration_seconds("hi") >= 3.0
    long_zh = "这是一段很长的中文旁白，用来测试静音轨道的长度估算是否合理。" * 4
    assert silent_duration_seconds(long_zh) > 8.0

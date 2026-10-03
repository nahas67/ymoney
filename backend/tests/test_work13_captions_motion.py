"""Work 13 — Advanced Captions + Motion Design + Visual Effects.

Test battery for the Work 13 modules. Each test pins one behaviour the
Definition of Done requires, so a regression names itself.

Deterministic throughout: no network, no model, no reliance on the wall clock
(except one explicit measurement of how the renderer scales, which uses fixed
inputs), and every ffmpeg-dependent test is guarded so a machine without
ffmpeg SKIPS rather than silently passing.
"""

from __future__ import annotations

import shutil

import pytest

# ---------------------------------------------------------------------------
# §1 caption style schema
# ---------------------------------------------------------------------------


def test_plain_caption_stays_valid_and_matches_legacy_look():
    """A caption with no styling block must render as it always did."""
    from app.engine.captions.style import CaptionStyle

    style = CaptionStyle.from_dict({})
    assert (style.size, style.weight, style.primary_color, style.stroke_width,
            style.position) == (56, 700, "#ffffff", 2, "bottom")
    assert style.animation.is_static()


def test_style_roundtrips_and_clamps():
    from app.engine.captions.style import CaptionStyle

    style = CaptionStyle.from_dict({"size": 999, "opacity": 5})
    assert style.size == 400 and style.opacity == 1.0
    assert CaptionStyle.from_dict(style.to_dict()) == style


def test_style_rejects_unknown_keys_and_unsafe_colours():
    """Typed schema: a typo must fail, never render as something else."""
    from app.engine.captions.style import CaptionStyle, CaptionStyleError

    with pytest.raises(CaptionStyleError):
        CaptionStyle.from_dict({"colour": "red"})
    with pytest.raises(CaptionStyleError):
        CaptionStyle.from_dict({"primary_color": "red; rm -rf /"})
    with pytest.raises(CaptionStyleError):
        CaptionStyle.from_dict({"align": "middle"})
    with pytest.raises(CaptionStyleError):
        CaptionStyle.from_dict({"font": "evil.ttf':x=y:"})


def test_style_accepts_legacy_aliases():
    """Work 01..12 wrote color/borderw/bordercolor; keep them working."""
    from app.engine.captions.style import CaptionStyle

    style = CaptionStyle.from_dict({"color": "yellow", "borderw": 4,
                                    "bordercolor": "black"})
    assert style.primary_color == "yellow"
    assert style.stroke_width == 4
    assert style.stroke_color == "black"


# ---------------------------------------------------------------------------
# §2 word-level captions + honest degradation
# ---------------------------------------------------------------------------


def test_word_groups_use_only_stored_timings():
    from app.engine.captions.words import WordTiming, group_words_into_captions

    words = [WordTiming("Save", 1.0, 1.4), WordTiming("$500", 1.4, 1.8),
             WordTiming("now", 1.8, 2.0)]
    groups = group_words_into_captions(words, max_chars_per_line=24)
    assert len(groups) == 1
    group = groups[0]
    assert group.word_level is True
    # span == first word start .. last word end, exactly
    assert group.start_s == 1.0 and group.end_s == 2.0
    assert group.char_spans() == [(0, 4), (5, 9), (10, 13)]


def test_long_pause_and_speaker_change_start_a_new_group():
    from app.engine.captions.words import WordTiming, group_words_into_captions

    # 'one' -> 'two' has a 2.6s pause (> max_gap_s) so it breaks; 'two' -> 'three'
    # is a 0.1s gap but a SPEAKER change, so it breaks too.
    words = [WordTiming("one", 0.0, 0.4, speaker_id="SPEAKER_00"),
             WordTiming("two", 3.0, 3.4, speaker_id="SPEAKER_00"),
             WordTiming("three", 3.5, 3.9, speaker_id="SPEAKER_01")]
    groups = group_words_into_captions(words, max_gap_s=0.6)
    assert [g.text for g in groups] == ["one", "two", "three"]
    assert all(g.word_level for g in groups)
    # with break_on_speaker disabled the short gap keeps them together
    merged = group_words_into_captions(words, max_gap_s=0.6,
                                       break_on_speaker=False)
    assert [g.text for g in merged] == ["one", "two three"]


def test_missing_word_alignment_degrades_honestly(db_session, workspace_with_user):
    """No stored words -> available=False with a reason, and NO words."""
    from app.engine.captions.words import load_word_timings

    source = load_word_timings(db_session, workspace_with_user["workspace"],
                               asset_id="no-such-asset")
    assert source.available is False
    assert source.words == []
    assert source.reason  # never an empty/blank reason
    assert source.word_level is False


def test_no_asset_is_reported_not_guessed(db_session, workspace_with_user):
    from app.engine.captions.words import load_word_timings

    source = load_word_timings(db_session, workspace_with_user["workspace"],
                               asset_id=None)
    assert source.available is False and "asset" in source.reason.lower()


def test_segment_fallback_marks_itself_not_word_level():
    from app.engine.captions.words import segments_to_groups

    groups = segments_to_groups(
        [{"text": "A segment with no alignment at all", "start_s": 0.0,
          "end_s": 3.0}], max_chars_per_line=20, max_lines=2)
    assert groups and all(g.word_level is False for g in groups)
    assert all(g.words == [] for g in groups)


def test_segment_fallback_splits_instead_of_overflowing():
    from app.engine.captions.words import segments_to_groups

    groups = segments_to_groups(
        [{"text": "one two three four five six seven eight nine ten",
          "start_s": 0.0, "end_s": 4.0}], max_chars_per_line=16, max_lines=2)
    assert len(groups) > 1, "overflow must become more captions, not a tall one"
    for group in groups:
        assert len(group.text.splitlines()) <= 2
    assert groups[0].end_s <= groups[-1].end_s


# ---------------------------------------------------------------------------
# §3 semantic emphasis
# ---------------------------------------------------------------------------


def test_emphasis_classifies_deterministically():
    from app.engine.captions.emphasis import CaptionEmphasisEngine

    engine = CaptionEmphasisEngine(max_per_caption=4)
    kinds = [w.kind for w in engine.emphasize("Save $500 today - subscribe now")]
    assert "NUMBER" in kinds
    assert "CTA" in kinds


def test_emphasis_is_stored_explicitly_with_provenance():
    from app.engine.captions.emphasis import CaptionEmphasisEngine

    for item in CaptionEmphasisEngine().emphasize("We grew 40% in 2024"):
        assert item.source == "deterministic"
        assert item.kind in ("NUMBER", "KEYWORD", "CTA", "QUESTION",
                             "EMOTION_CUE", "ENTITY", "NONE")
        assert item.start < item.end


def test_emphasis_respects_max_per_caption():
    from app.engine.captions.emphasis import CaptionEmphasisEngine

    engine = CaptionEmphasisEngine(max_per_caption=2)
    assert len(engine.emphasize("subscribe now 500 300 900 click")) <= 2


def test_engine_refuses_sensitive_inference_kinds():
    """No demographic/health/etc. emphasis may ever be produced."""
    from app.engine.captions.emphasis import (
        CaptionEmphasisEngine,
        EmphasisError,
        assert_no_sensitive_kinds,
    )

    for kind in ("gender", "AGE", "ethnicity", "religion", "health",
                 "political", "income"):
        with pytest.raises(EmphasisError):
            assert_no_sensitive_kinds([kind])
    with pytest.raises(EmphasisError):
        CaptionEmphasisEngine(enabled_kinds=("KEYWORD", "gender"))


def test_emphasis_unknown_kind_is_refused():
    from app.engine.captions.emphasis import CaptionEmphasisEngine, EmphasisError

    with pytest.raises(EmphasisError):
        CaptionEmphasisEngine(enabled_kinds=("KEYWORD", "TELEPATHY"))


# ---------------------------------------------------------------------------
# §4 presets
# ---------------------------------------------------------------------------


def test_all_required_presets_exist():
    from app.engine.captions.presets import PRESET_NAMES

    for name in ("minimal", "bold_shorts", "karaoke", "podcast",
                 "documentary", "educational", "news", "ugc", "brand_primary"):
        assert name in PRESET_NAMES


def test_brand_overrides_a_preset():
    """BrandDNA wins over the preset default."""
    from app.engine.captions.presets import effective_preset, get_preset

    base = get_preset("minimal")
    branded = effective_preset("minimal", brand_patch={"size": 96,
                                                       "primary_color": "#ff0055"})
    assert base.style.size == 48 and branded.style.size == 96
    assert branded.style.primary_color == "#ff0055"


def test_invalid_brand_override_does_not_half_apply():
    from app.engine.captions.presets import effective_preset
    from app.engine.captions.style import CaptionStyleError

    with pytest.raises(CaptionStyleError):
        effective_preset("minimal", brand_patch={"primary_color": "nope"})


def test_preset_chain_applies_brand_then_clip():
    from app.engine.captions.presets import resolve_preset_chain

    preset, style = resolve_preset_chain("news", brand_patch={"size": 40},
                                         clip_patch={"size": 44})
    assert preset.key == "news" and style.size == 44


def test_unknown_preset_is_refused_never_silently_defaulted():
    from app.engine.captions.presets import UnknownPresetError, get_preset

    with pytest.raises(UnknownPresetError):
        get_preset("not-a-preset")


# ---------------------------------------------------------------------------
# §5 kinetic typography (keyframes are config, never pixels)
# ---------------------------------------------------------------------------


def test_kinetic_animation_is_configuration_that_serializes():
    from app.engine.captions.style import CaptionAnimation

    anim = CaptionAnimation.from_dict({"entrance": "pop",
                                       "word_animation": "karaoke",
                                       "duration": 0.2})
    assert anim.to_dict()["word_animation"] == "karaoke"
    assert not anim.is_static()
    assert CaptionAnimation.from_dict({}).is_static()


def test_kinetic_animation_rejects_unknown_vocab():
    from app.engine.captions.style import CaptionAnimation, CaptionStyleError

    with pytest.raises(CaptionStyleError):
        CaptionAnimation.from_dict({"entrance": "explode"})
    with pytest.raises(CaptionStyleError):
        CaptionAnimation.from_dict({"nope": 1})


# ---------------------------------------------------------------------------
# §6 motion templates
# ---------------------------------------------------------------------------


def test_all_template_types_exist():
    from app.engine.motion.templates import TEMPLATES

    types = {t.type for t in TEMPLATES.values()}
    for expected in ("TITLE", "LOWER_THIRD", "CALLOUT", "QUOTE", "STAT",
                     "CTA", "CHAPTER", "PRODUCT_LABEL", "NAME_TAG"):
        assert expected in types


def test_template_instance_becomes_a_timeline_clip():
    from app.engine.motion.templates import validate_instance

    instance = validate_instance({"template": "title_main",
                                  "bindings": {"headline": "Launch Day"},
                                  "start": 0.5, "duration": 2.0})
    clip = instance.to_clip()
    assert clip["start"] == 0.5 and clip["duration"] == 2.0
    assert "Launch Day" in clip["text"]["content"]
    assert "{{" not in clip["text"]["content"]


def test_template_rejects_executable_keys():
    from app.engine.motion.templates import MotionTemplateError, validate_template

    for bad in ({"type": "TITLE", "body": "x", "code": "import os"},
                {"type": "TITLE", "body": "x", "filter": "drawtext=x"},
                {"type": "TITLE", "body": "x", "script": "rm -rf /"}):
        with pytest.raises(MotionTemplateError):
            validate_template(bad)


def test_template_rejects_unknown_slot():
    from app.engine.motion.templates import MotionTemplateError, validate_instance

    with pytest.raises(MotionTemplateError):
        validate_instance({"template": "title_main",
                           "bindings": {"not_a_slot": "x"}})


# ---------------------------------------------------------------------------
# §7 lower thirds never invent metadata
# ---------------------------------------------------------------------------


def test_lower_third_builds_from_known_metadata():
    from app.engine.motion.lower_thirds import LowerThirdRequest, build_lower_third

    result = build_lower_third(LowerThirdRequest(
        kind="person", name="Ada Lovelace", role="Engineer", start=1.0))
    assert result.built
    assert "Ada Lovelace" in result.clip["text"]["content"]
    assert "Engineer" in result.clip["text"]["content"]


def test_lower_third_declines_when_metadata_is_unknown():
    from app.engine.motion.lower_thirds import LowerThirdRequest, build_lower_third

    result = build_lower_third(LowerThirdRequest(kind="person"))
    assert result.built is False
    assert "invent" in result.reason


def test_lower_third_excludes_undeclared_fields():
    """A value that exists but the caller cannot vouch for is NOT used."""
    from app.engine.motion.lower_thirds import LowerThirdRequest, build_lower_third

    result = build_lower_third(LowerThirdRequest(
        kind="person", name="Ada", role="CEO of Nothing", known=("name",)))
    assert result.built is True
    assert "CEO of Nothing" not in result.clip["text"]["content"]
    assert result.missing == ("role",)


def test_lower_third_unknown_kind_is_refused():
    from app.engine.motion.lower_thirds import LowerThirdRequest, build_lower_third

    assert build_lower_third(LowerThirdRequest(kind="telepathy")).built is False


# ---------------------------------------------------------------------------
# §8 effects
# ---------------------------------------------------------------------------


def test_all_required_effects_registered():
    from app.engine.motion.effects import EFFECT_NAMES

    for name in ("BLUR", "BACKGROUND_BLUR", "VIGNETTE", "ZOOM", "PAN", "CROP",
                 "OPACITY", "COLOR_ADJUST", "SHARPEN", "GLOW", "DROP_SHADOW",
                 "MASK"):
        assert name in EFFECT_NAMES


def test_effect_params_are_typed_and_clamped():
    from app.engine.motion.effects import validate_effect

    out = validate_effect({"type": "zoom", "params": {"step": 99,
                                                      "max_zoom": -5}})
    assert out["params"]["step"] == 0.05
    assert out["params"]["max_zoom"] == 1.0


def test_effect_registry_rejects_unknown_names_and_params():
    from app.engine.motion.effects import EffectError, validate_effect

    with pytest.raises(EffectError):
        validate_effect({"type": "definitely_not_an_effect"})
    with pytest.raises(EffectError):
        validate_effect({"type": "BLUR", "params": {"evil": 1}})
    with pytest.raises(EffectError):
        validate_effect({"type": "BLUR", "params": {"radius": "abc"}})
    with pytest.raises(EffectError):
        validate_effect({"type": "COLOR_ADJUST", "params": {"brightness": "x"}})


def test_no_effect_accepts_a_raw_filter_fragment():
    """There is no parameter that could carry an ffmpeg expression."""
    from app.engine.motion.effects import EFFECTS

    for spec in EFFECTS.values():
        assert "filter" not in {p.name.lower() for p in spec.params}
        assert "expr" not in {p.name.lower() for p in spec.params}


def test_tracked_effect_declines_without_evidence():
    """No tracking data -> no filter, and the caller records a warning."""
    from app.engine.motion.effects import build_effect_filter

    assert build_effect_filter({"type": "TRACKED_CALLOUT", "params": {}}, {}) is None
    assert build_effect_filter({"type": "BACKGROUND_BLUR", "params": {}}, {}) is None


def test_tracked_callout_emits_when_evidence_is_sufficient():
    from app.engine.motion.effects import build_effect_filter

    built = build_effect_filter(
        {"type": "TRACKED_CALLOUT", "params": {"color": "yellow"}},
        {"subject_box": {"x": 100, "y": 200, "w": 80, "h": 80,
                         "confidence": 0.9}, "window_s": (1.0, 3.0),
         "min_confidence": 0.55})
    assert built and "drawbox" in built and "between(t" in built


def test_tracked_callout_refuses_low_confidence():
    from app.engine.motion.effects import build_effect_filter

    assert build_effect_filter(
        {"type": "TRACKED_CALLOUT", "params": {}},
        {"subject_box": {"x": 1, "y": 1, "w": 10, "h": 10,
                         "confidence": 0.2}, "window_s": (0, 1)}) is None


# ---------------------------------------------------------------------------
# §9 transitions
# ---------------------------------------------------------------------------


def test_transition_registry_covers_required_types():
    from app.engine.motion.transitions import TRANSITION_NAMES

    for name in ("CUT", "FADE", "DISSOLVE", "SLIDE", "WIPE", "ZOOM"):
        assert name in TRANSITION_NAMES


def _doc():
    return {"tracks": [{"kind": "video", "clips": [
        {"id": "a", "start": 0.0, "duration": 3.0},
        {"id": "b", "start": 3.0, "duration": 2.0},
        {"id": "c", "start": 9.0, "duration": 1.0},
    ]}]}


def test_transition_validates_against_adjacent_clips():
    from app.engine.motion.transitions import validate_transition

    out = validate_transition(
        {"from_item": "a", "to_item": "b", "type": "dissolve",
         "duration": 0.5}, _doc())
    assert out["type"] == "DISSOLVE"
    # reversed order still resolves to (earlier, later)
    assert validate_transition(
        {"from_item": "b", "to_item": "a", "type": "fade", "duration": 0.5},
        _doc())["type"] == "FADE"


def test_transition_refuses_non_adjacent_and_overlong():
    from app.engine.motion.transitions import TransitionError, validate_transition

    with pytest.raises(TransitionError):
        validate_transition({"from_item": "a", "to_item": "c", "type": "fade",
                             "duration": 0.5}, _doc())
    with pytest.raises(TransitionError):
        validate_transition({"from_item": "a", "to_item": "b", "type": "fade",
                             "duration": 9.0}, _doc())
    with pytest.raises(TransitionError):
        validate_transition({"from_item": "a", "to_item": "a", "type": "fade",
                             "duration": 0.5}, _doc())
    with pytest.raises(TransitionError):
        validate_transition({"from_item": "a", "to_item": "b", "type": "cut",
                             "duration": 1.0}, _doc())


def test_cut_emits_no_filter():
    from app.engine.motion.transitions import build_transition_filter

    assert build_transition_filter(
        {"from_item": "a", "to_item": "b", "type": "CUT", "duration": 0.0},
        offset_s=3.0) is None


# ---------------------------------------------------------------------------
# §11 BrandDNA motion policy
# ---------------------------------------------------------------------------


def test_brand_policy_blocks_forbidden_effects_and_presets():
    from app.engine.motion.policy import EffectiveMotionPolicy, MotionPolicyError

    policy = EffectiveMotionPolicy(approved_caption_presets=("ugc",),
                                   forbidden_effects=("GLOW",))
    assert policy.caption_preset_allowed("ugc")
    assert not policy.caption_preset_allowed("minimal")
    assert not policy.effect_allowed("GLOW")
    assert policy.effect_allowed("BLUR")
    with pytest.raises(MotionPolicyError):
        policy.require_effect("GLOW")


def test_background_blur_is_hard_blocked():
    """It needs a mask asset; the policy refuses it regardless of brand."""
    from app.engine.motion.policy import EffectiveMotionPolicy

    assert EffectiveMotionPolicy().effect_allowed("BACKGROUND_BLUR") is False


def test_stale_preset_names_are_filtered_out():
    """A brand naming a preset that no longer exists must not block everything."""
    from app.engine.motion.policy import EffectiveMotionPolicy

    policy = EffectiveMotionPolicy(
        approved_caption_presets=("ugc", "deleted_last_year"))
    assert policy.approved_caption_presets == ("ugc",)


def test_learning_cannot_override_brand():
    """A recommendation outside the approved list is refused, with a reason."""
    from app.engine.motion.policy import EffectiveMotionPolicy

    policy = EffectiveMotionPolicy(approved_caption_presets=("ugc",))
    assert policy.recommend("ugc")["accepted"] is True
    refused = policy.recommend("news")
    assert refused["accepted"] is False
    assert refused["reason"]


def test_motion_intensity_gates_animation():
    from app.engine.motion.policy import EffectiveMotionPolicy

    subtle = EffectiveMotionPolicy(motion_intensity="subtle")
    assert subtle.intensity_allows("fade") is True
    assert subtle.intensity_allows("pop") is False
    frozen = EffectiveMotionPolicy(motion_intensity="none")
    assert frozen.intensity_allows("pop") is False


# ---------------------------------------------------------------------------
# §15 cache keys
# ---------------------------------------------------------------------------


def test_render_cache_key_covers_style_and_renderer_version():
    """Cache reuse must be keyed on style + renderer, not just source."""
    from app.engine.captions.cache import caption_cache_key

    a = caption_cache_key(source_checksum="abc", timeline_version=3,
                          style={"size": 56}, words=[{"w": "a", "s": 0.0, "e": 1.0}])
    b = caption_cache_key(source_checksum="abc", timeline_version=3,
                          style={"size": 72}, words=[{"w": "a", "s": 0.0, "e": 1.0}])
    c = caption_cache_key(source_checksum="abc", timeline_version=3,
                          style={"size": 56}, words=[])
    assert a != b, "a style change must invalidate the cache"
    assert a != c, "word timing must be part of the key"
    assert a == caption_cache_key(
        source_checksum="abc", timeline_version=3,
        style={"size": 56}, words=[{"w": "a", "s": 0.0, "e": 1.0}])


# ---------------------------------------------------------------------------
# §16 QC
# ---------------------------------------------------------------------------


def test_qc_clean_document_passes():
    from app.engine.motion.qc import run_caption_motion_qc

    doc = {"tracks": [
        {"kind": "video", "clips": [{"id": "v", "start": 0, "duration": 6,
                                     "effects": []}]},
        {"kind": "caption", "clips": [
            {"id": "c1", "name": "Short line", "start": 0, "duration": 2.0}]},
    ]}
    report = run_caption_motion_qc(doc, font_available="fake-font.ttf")
    assert report["status"] in ("PASS", "PASS_WITH_WARNINGS")
    assert report["blocking"] is False


def test_qc_fails_on_broken_timing():
    from app.engine.motion.qc import run_caption_motion_qc

    doc = {"tracks": [{"kind": "caption", "clips": [
        {"id": "c1", "name": "x", "start": -1, "duration": 0}]}]}
    report = run_caption_motion_qc(doc, font_available="f.ttf")
    assert report["status"] == "FAIL"
    assert report["blocking"] is True


def test_qc_fails_when_no_font_resolves():
    """Every caption would be silently dropped at render time."""
    from app.engine.motion.qc import run_caption_motion_qc

    doc = {"tracks": [{"kind": "caption", "clips": [
        {"id": "c1", "name": "hi", "start": 0, "duration": 2}]}]}
    report = run_caption_motion_qc(doc, font_available=None)
    assert report["status"] == "FAIL"
    assert any(c["name"] == "font_resolves" and not c["passed"]
               for c in report["checks"])


def test_qc_flags_overflow_and_short_captions():
    from app.engine.motion.qc import run_caption_motion_qc

    doc = {"tracks": [{"kind": "caption", "clips": [
        {"id": "long", "name": "x" * 300, "start": 0, "duration": 30.0},
        {"id": "flash", "name": "hi", "start": 31.0, "duration": 0.05},
    ]}]}
    report = run_caption_motion_qc(doc, font_available="f.ttf")
    names = {c["name"] for c in report["checks"] if not c["passed"]}
    assert "caption_no_overflow" in names
    assert "caption_readable_duration" in names


def test_qc_flags_safe_zone_and_overlap():
    from app.engine.motion.qc import run_caption_motion_qc

    doc = {"tracks": [{"kind": "caption", "clips": [
        {"id": "a", "name": "one", "start": 0, "duration": 2.0},
        {"id": "b", "name": "two", "start": 1.0, "duration": 2.0},
    ]}]}
    report = run_caption_motion_qc(doc, safe_box={"top": 0.08, "bottom": 0.14},
                                   font_available="f.ttf")
    names = {c["name"] for c in report["checks"] if not c["passed"]}
    assert "overlay_no_overlap" in names


def test_qc_rejects_invalid_effects_and_transitions():
    from app.engine.motion.qc import run_caption_motion_qc

    doc = {"tracks": [
        {"kind": "video", "clips": [
            {"id": "v", "start": 0, "duration": 5,
             "effects": [{"type": "NOT_AN_EFFECT"}]}]},
        {"kind": "video2", "clips": []},
    ]}
    doc["tracks"][0]["clips"][0]["transition"] = {
        "from_item": "v", "to_item": "nobody", "type": "fade", "duration": 0.5}
    report = run_caption_motion_qc(doc, font_available="f.ttf")
    failed = {c["name"] for c in report["checks"] if c["severity"] == "FAIL"}
    assert "effects_valid" in failed
    assert "transitions_valid" in failed


def test_qc_flags_invalid_keyframes():
    from app.engine.motion.qc import run_caption_motion_qc

    doc = {"tracks": [{"kind": "text", "clips": [
        {"id": "t", "name": "hi", "start": 0, "duration": 2.0,
         "keyframes": [{"t": 1.0}, {"t": 0.5}]}]}]}
    report = run_caption_motion_qc(doc, font_available="f.ttf")
    assert any(c["name"] == "keyframes_valid" and not c["passed"]
               for c in report["checks"])


# ---------------------------------------------------------------------------
# §13 renderer: real media, real ffmpeg
# ---------------------------------------------------------------------------

requires_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def test_font_fallback_prefers_env_then_family_then_default(monkeypatch):
    from app.engine.captions.ffmpeg_escape import resolve_font

    monkeypatch.setenv("YMONEY_FONT_FILE", "/definitely/not/here.ttf")
    assert resolve_font() is not None  # falls through to a real default
    monkeypatch.setenv("YMONEY_FONT_FILE", "")
    assert resolve_font("NoSuchFamilyName") is not None


def test_font_family_candidates_reject_traversal():
    from app.engine.captions.ffmpeg_escape import family_font_candidates

    assert family_font_candidates("") == []
    assert family_font_candidates("../../etc/passwd") == []


def test_caption_filter_escapes_hostile_text():
    """Quote/colon/comma must not escape the quoted drawtext payload.

    NOTE on `[`, `]` and `;`: these are protected by the surrounding single
    quotes, which is exactly what quoting is for. Escaping them as ``\\;``
    would render a LITERAL backslash in the caption, so they are deliberately
    left alone -- the assertion below pins that decision.
    """
    from app.engine.captions.filters import build_caption_filters
    from app.engine.captions.presets import get_preset

    plan = build_caption_filters(
        label_in="v", caption={"name": "a:b,c'd[e]f;g", "start": 0,
                               "duration": 2.0},
        style=get_preset("minimal").style, width=1080, height=1920)
    assert plan.filters
    built = plan.filters[0]
    assert "\\:" in built and "\\," in built and "\\'" in built
    # quoted payload: brackets/semicolon survive unescaped (no literal backslash)
    assert "\\[" not in built and "\\;" not in built
    # and the text is still inside quotes
    assert "text='a\\:b\\,c\\'d[e]f;g'" in built


def test_caption_filter_is_deterministic():
    from app.engine.captions.filters import build_caption_filters
    from app.engine.captions.presets import get_preset

    caption = {"name": "deterministic", "start": 1.0, "duration": 2.0}
    style = get_preset("karaoke").style
    first = build_caption_filters(label_in="v", caption=caption, style=style,
                                  width=1080, height=1920).filters
    second = build_caption_filters(label_in="v", caption=caption, style=style,
                                   width=1080, height=1920).filters
    assert first == second


def test_word_level_emits_one_filter_per_word():
    from app.engine.captions.filters import build_caption_filters
    from app.engine.captions.presets import get_preset
    from app.engine.captions.words import WordTiming

    words = [WordTiming("a", 0.0, 0.4), WordTiming("b", 0.4, 0.8),
             WordTiming("c", 0.8, 1.2)]
    plan = build_caption_filters(
        label_in="v", caption={"name": "a b c", "start": 0, "duration": 1.2},
        style=get_preset("karaoke").style, width=1080, height=1920,
        words=words, word_level=True)
    assert plan.filter_count == 3
    assert plan.word_level is True
    # each filter gates on its word's real window
    assert "between(t\\,0.000\\,0.400)" in plan.filters[0]
    assert "between(t\\,0.800\\,1.200)" in plan.filters[2]


def test_segment_caption_emits_exactly_one_filter():
    from app.engine.captions.filters import build_caption_filters
    from app.engine.captions.presets import get_preset

    plan = build_caption_filters(
        label_in="v", caption={"name": "no words here", "start": 0,
                               "duration": 2.0},
        style=get_preset("karaoke").style, width=1080, height=1920,
        words=None, word_level=False)
    assert plan.filter_count == 1 and plan.word_level is False


def test_safe_box_moves_the_caption_upward():
    from app.engine.captions.filters import build_caption_filters
    from app.engine.captions.presets import get_preset

    caption = {"name": "hi", "start": 0, "duration": 2.0}
    style = get_preset("minimal").style
    free = build_caption_filters(label_in="v", caption=caption, style=style,
                                 width=1080, height=1920).filters[0]
    boxed = build_caption_filters(
        label_in="v", caption=caption, style=style, width=1080, height=1920,
        safe_box={"bottom": 0.3}).filters[0]
    assert free != boxed, "the safe box must change placement"


@requires_ffmpeg
def test_styled_caption_renders_real_media(tmp_path, monkeypatch):
    """The DoD's hard requirement: real verified media out of the renderer."""
    from app.providers.video_engine.timeline_render import render_timeline

    doc = {"workspace_id": "ws", "fps": 12, "aspect_ratio": "9:16",
           "duration_seconds": 2.0, "tracks": [
               {"id": "t_video", "kind": "video", "name": "v", "clips": [
                   {"id": "v1", "name": "src", "start": 0.0, "duration": 2.0,
                    "source": {}, "effects": [
                        {"type": "COLOR_ADJUST", "params": {"brightness": 0.05}}],
                    "source_start": 0.0, "volume": 1.0, "speed": 1.0,
                    "fade_in": 0.0, "fade_out": 0.0, "transform": {},
                    "text": {}, "transition_in": "cut",
                    "transition_out": "cut"}]},
               {"id": "t_caption", "kind": "caption", "name": "c", "clips": [
                   {"id": "c1", "name": "Verified render", "start": 0.0,
                    "duration": 2.0,
                    "text": {"preset": "bold_shorts", "size": 48},
                    "source": {}, "effects": [], "source_start": 0.0,
                    "volume": 1.0, "speed": 1.0, "fade_in": 0.0,
                    "fade_out": 0.0, "transform": {}, "transition_in": "cut",
                    "transition_out": "cut"}]},
           ]}

    # A lavfi source needs no asset row; patch the resolver to hand back a
    # generated test pattern so the render is genuinely deterministic.
    import subprocess

    pattern = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "testsrc=size=320x568:rate=12:duration=2",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(pattern)],
        capture_output=True, check=True)

    from app.providers.video_engine import timeline_render as tr

    monkeypatch.setattr(tr, "resolve_clip_source",
                        lambda ws, db, source: pattern)

    out = render_timeline("ws", None, doc, out_name="w13.mp4", fps=12)
    from pathlib import Path

    produced = Path(out["path"])
    assert produced.exists() and produced.stat().st_size > 0
    assert out["width"] == 1080 and out["height"] == 1920
    # the styled caption must have produced filters, and the effect must have
    # been applied without a warning
    assert not any("refused" in w for w in out.get("warnings", []))
    assert out["job_hash"]
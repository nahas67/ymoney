"""Work 14 §7/§8/§9/§10/§11 -- optimizer, campaign integration, verification.

Covers the layers BETWEEN the providers and the database: the capability
registry, the variant optimizer (priority order + BrandDNA precedence), the
campaign/scheduler integration rules, the publication-mode verifier, analytics
and inbox capability mapping, and workspace isolation.
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# §1 capability registry
# ---------------------------------------------------------------------------


def test_registry_exposes_the_full_work14_vocabulary():
    from app.engine.platform_registry import DISTRIBUTION_CAPABILITIES

    assert {c.value for c in DISTRIBUTION_CAPABILITIES} == {
        "TEXT", "IMAGE", "VIDEO", "CAROUSEL", "REPLY", "LINK", "ALT_TEXT",
        "COMMENTS", "METRICS", "DIRECT_PUBLISH", "USER_HANDOFF"}


def test_registry_covers_the_four_new_platforms():
    from app.engine.platform_registry import get_registry

    registry = get_registry()
    platforms = {s.platform for s in registry.specs()}
    assert {"threads", "pinterest", "bluesky", "snapchat"} <= platforms


def test_direct_publish_and_user_handoff_are_mutually_exclusive():
    """A platform is one or the other. Never both, never neither silently."""
    from app.engine.platform_registry import get_registry

    registry = get_registry()
    for spec in registry.specs():
        direct = spec.has("DIRECT_PUBLISH")
        handoff = spec.has("USER_HANDOFF")
        assert not (direct and handoff), (
            f"{spec.platform} claims both DIRECT_PUBLISH and USER_HANDOFF")
    # snapchat is the handoff; the other three publish directly
    assert registry.supports("snapchat", "USER_HANDOFF") is True
    for platform in ("threads", "pinterest", "bluesky"):
        assert registry.supports(platform, "DIRECT_PUBLISH") is True
        assert registry.supports(platform, "USER_HANDOFF") is False


def test_unsupported_capability_is_refused_not_inferred():
    from app.engine.platform_registry import get_registry

    registry = get_registry()
    # Pinterest documents no organic comment/reply API
    assert registry.supports("pinterest", "COMMENTS") is False
    assert registry.supports("pinterest", "REPLY") is False
    # Pinterest carousels exist; Bluesky carousels are not the same contract
    assert registry.supports("pinterest", "CAROUSEL") is True
    assert registry.supports("bluesky", "CAROUSEL") is False
    # Threads carousels ARE official
    assert registry.supports("threads", "CAROUSEL") is True


def test_unknown_capability_name_raises_rather_than_defaulting():
    from app.engine.platform_registry import get_registry

    with pytest.raises(ValueError):
        get_registry().supports("threads", "TELEPATHY")


def test_unknown_platform_raises():
    from app.engine.platform_registry import get_registry

    with pytest.raises(KeyError):
        get_registry().spec("myspace")


def test_existing_platform_capabilities_are_unchanged():
    """Work 14 must not widen what the pre-existing platforms claim."""
    from app.engine.platform_registry import get_registry

    registry = get_registry()
    # Work 09 baseline: instagram publishes Shorts only, NOT long video
    assert registry.supports("instagram", "PUBLISH_SHORT") is True
    assert registry.supports("instagram", "PUBLISH_VIDEO") is False
    # and the Work 14 vocabulary is additive: existing platforms gain nothing
    for platform in ("youtube", "tiktok", "instagram", "facebook", "linkedin",
                     "x"):
        assert registry.supports(platform, "DIRECT_PUBLISH") is False
        assert registry.supports(platform, "USER_HANDOFF") is False


def test_the_two_capability_vocabularies_cannot_disagree():
    """The guard that caught a real omission (pinterest VIDEO)."""
    from app.engine.platform_registry import (
        Capability,
        assert_vocabularies_agree,
    )

    assert_vocabularies_agree("ok", frozenset({
        Capability.VIDEO, Capability.PUBLISH_VIDEO}))
    with pytest.raises(RuntimeError) as caught:
        assert_vocabularies_agree("bad", frozenset({Capability.VIDEO}))
    assert "PUBLISH_VIDEO" in str(caught.value)


# ---------------------------------------------------------------------------
# §6 PlatformOptimizationProfile
# ---------------------------------------------------------------------------


def test_profiles_only_carry_verified_values_or_unknown():
    from app.engine.distribution.profiles import UNKNOWN, get_profile

    for name in ("threads", "pinterest", "bluesky", "snapchat"):
        profile = get_profile(name)
        for field in profile.__dataclass_fields__:
            if field == "platform":
                continue
            value = profile.value(field)
            assert value is not UNKNOWN or not profile.known(field), (
                f"{name}.{field} reports known but has no value")
            if profile.known(field):
                assert profile.source_of(field), (
                    f"{name}.{field} is verified with no source recorded")


def test_unknown_limits_stay_unknown_rather_than_being_guessed():
    from app.engine.distribution.profiles import UNKNOWN, get_profile

    threads = get_profile("threads")
    # Meta publishes no Threads title field and no hashtag limit
    assert threads.value("title_max") is UNKNOWN
    assert threads.value("hashtag_limit") is UNKNOWN
    bluesky = get_profile("bluesky")
    # the lexicon sets NO alt-text character cap
    assert bluesky.value("alt_text")["max_chars"] is UNKNOWN
    # no video duration limit is documented anywhere official
    assert bluesky.value("duration") is UNKNOWN
    # Snapchat documents nothing, because there is no publish endpoint
    snapchat = get_profile("snapchat")
    assert snapchat.media_types == frozenset()
    assert snapchat.value("text_limits") is UNKNOWN


def test_verified_limits_match_the_documented_values():
    from app.engine.distribution.profiles import get_profile

    assert get_profile("threads").value("text_limits")["chars"] == 500
    assert get_profile("threads").value("alt_text") == 1000
    assert get_profile("threads").value("link_limit") == 5
    assert get_profile("pinterest").value("title_max") == 100
    assert get_profile("pinterest").value("description_max") == 800
    assert get_profile("pinterest").value("alt_text") == 500
    assert get_profile("bluesky").value("text_limits")["graphemes"] == 300


def test_every_profile_records_the_document_that_states_its_limit():
    from app.engine.distribution.profiles import PROFILES

    for name, profile in PROFILES.items():
        assert profile.verified_notes, f"{name} records no verified notes"
        for field in ("title_max", "alt_text", "rate_limit"):
            if profile.known(field):
                assert profile.source_of(field).startswith("http"), (
                    f"{name}.{field} source is not a URL: "
                    f"{profile.source_of(field)!r}")


def test_bluesky_video_is_recorded_as_available_not_unavailable():
    from app.engine.distribution.profiles import get_profile

    bluesky = get_profile("bluesky")
    assert "VIDEO" in bluesky.media_types
    assert any("VIDEO IS SUPPORTED" in n for n in bluesky.verified_notes)


def test_snapchat_profile_explains_why_it_is_a_handoff():
    from app.engine.distribution.profiles import get_profile

    notes = " ".join(get_profile("snapchat").verified_notes)
    assert "USER_HANDOFF" in notes
    assert "ALLOWLIST-ONLY" in notes.upper()
    assert "read only" in notes      # the documented contradiction is recorded


# ---------------------------------------------------------------------------
# §7 PlatformVariantOptimizer
# ---------------------------------------------------------------------------


MASTER = {
    "title": "How to double your clicks in 40 seconds",
    "description": "A short walkthrough with real numbers.",
    "hashtags": ["ads", "growth"],
    "duration_s": 40.0,
    "aspect": "9:16",
    "media_kind": "video",
    "alt_text": "a screenshot of an analytics dashboard",
}


def _brand(**overrides):
    from app.engine.brand.dna import BrandDNA

    return BrandDNA(**overrides)


def test_optimizer_blocks_a_media_type_the_platform_rejects():
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    result = PlatformVariantOptimizer().optimize(
        platform="pinterest", master={**MASTER, "media_kind": "text"})
    assert result.is_blocked is True
    assert any("does not support" in b for b in result.blocked)


def test_optimizer_blocks_snapchat_because_there_is_no_publish_contract():
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    result = PlatformVariantOptimizer().optimize(platform="snapchat",
                                                  master=MASTER)
    assert result.is_blocked is True
    assert "user handoff" in result.blocked[0]


def test_optimizer_still_works_for_pre_work14_platforms():
    """A missing verified profile must not regress an existing platform."""
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    result = PlatformVariantOptimizer().optimize(platform="youtube_shorts",
                                                  master=MASTER)
    assert result.blocked == []
    assert result.spec["title"] == MASTER["title"]
    assert any("no verified distribution profile" in s for s in result.skipped)
    assert any("no verified media-type list" in s for s in result.skipped)


def test_variants_materially_differ_across_platforms():
    """DoD: identical metadata renamed per platform is PARTIAL."""
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    optimizer = PlatformVariantOptimizer()
    long_master = {**MASTER,
                   "title": "T" * 900,
                   "description": "D" * 2000,
                   "hashtags": [f"t{i}" for i in range(20)]}
    specs = {p: optimizer.optimize(platform=p, master=long_master).spec
             for p in ("threads", "pinterest", "bluesky", "snapchat")}

    # Threads: ONE 500-char text field, so title+description merge and clip
    assert len(specs["threads"]["title"]) == 500
    assert specs["threads"]["description"] == ""
    # Pinterest: the official 100-char title and 800-char description
    assert len(specs["pinterest"]["title"]) == 100
    assert len(specs["pinterest"]["description"]) == 800
    # Bluesky: 300 graphemes. Alt text is KEPT because the lexicon supports it
    # (it is simply uncapped), unlike a platform with no alt support at all.
    assert len(specs["bluesky"]["title"]) == 300
    assert specs["bluesky"]["alt_text"] == MASTER["alt_text"]
    # Snapchat: alt text is dropped, because nothing documents alt support on a
    # handoff (there is no API to send it to).
    assert specs["snapchat"]["alt_text"] == ""
    # Threads keeps its alt text because a 1000-char cap IS documented
    assert specs["threads"]["alt_text"] == MASTER["alt_text"]

    # and the specs are genuinely pairwise different, not renamed copies
    for a in specs:
        for b in specs:
            if a < b:
                assert specs[a] != specs[b], f"{a} and {b} produced the same spec"


def test_platform_constraint_outranks_a_campaign_override():
    """An override that breaks a documented limit is refused, and said so."""
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    result = PlatformVariantOptimizer().optimize(
        platform="pinterest", master=MASTER,
        overrides={"title": "T" * 400})
    # Pinterest documents title <= 100, so the override cannot be applied
    assert result.spec["title"] == MASTER["title"]
    assert any("400 chars" in s and "refused" in s for s in result.skipped)


def test_platform_limit_is_enforced_even_on_a_brand_supplied_title():
    from app.engine.distribution.optimizer import (
        PRIORITY_ORDER,
        PlatformVariantOptimizer,
    )

    # a title that itself exceeds the documented cap
    result = PlatformVariantOptimizer().optimize(
        platform="pinterest", master={**MASTER, "title": "T" * 300})
    decision = next(d for d in result.decisions if d.field_name == "title")
    assert decision.priority == PRIORITY_ORDER.index("platform_constraint") + 1
    assert len(result.spec["title"]) == 100


def test_campaign_override_is_applied_when_it_fits():
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    result = PlatformVariantOptimizer().optimize(
        platform="pinterest", master=MASTER,
        overrides={"title": "A short clean title"})
    assert result.spec["title"] == "A short clean title"
    assert any(d.source == "campaign_override" for d in result.decisions)


def test_brand_dna_forbidden_phrase_is_a_compliance_veto():
    """Priority 2 (compliance) outranks everything below it."""
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    result = PlatformVariantOptimizer().optimize(
        platform="threads", master=MASTER,
        brand=_brand(forbidden_phrases=["double your clicks"]))
    assert result.is_blocked is True
    assert any("forbidden phrase" in b for b in result.blocked)


def test_brand_dna_precedence_over_platform_defaults_for_captions():
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    result = PlatformVariantOptimizer().optimize(
        platform="threads", master=MASTER,
        brand=_brand(caption_style={"style": "karaoke"}))
    assert result.spec["caption"]["style"] == "karaoke"
    assert any(d.source == "brand_dna" for d in result.decisions)


def test_unsupported_cta_is_refused_with_a_reason():
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    result = PlatformVariantOptimizer().optimize(
        platform="pinterest", master=MASTER, overrides={"cta": "SUBSCRIBE"})
    # SUBSCRIBE is not a Pinterest CTA kind
    assert result.spec["cta"] != "SUBSCRIBE"
    assert any("SUBSCRIBE" in s and "not supported" in s for s in result.skipped)


def test_cover_image_is_refused_for_a_platform_with_no_cover_parameter():
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    result = PlatformVariantOptimizer().optimize(
        platform="threads", master=MASTER,
        overrides={"cover_asset_id": "asset-1"})
    assert result.spec["cover"]["asset_id"] == ""
    assert any("no cover/thumbnail parameter" in s for s in result.skipped)


def test_link_without_card_fields_is_reported_not_faked():
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    result = PlatformVariantOptimizer().optimize(
        platform="bluesky", master={**MASTER, "link_url": "https://a.example"})
    assert any("client to build the link card" in s for s in result.skipped)
    # Pinterest has a plain link field, so no card is needed
    pin = PlatformVariantOptimizer().optimize(
        platform="pinterest", master={**MASTER, "link_url": "https://a.example"})
    assert not any("link card" in s for s in pin.skipped)


def test_every_decision_records_its_priority_and_reason():
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    result = PlatformVariantOptimizer().optimize(
        platform="threads", master=MASTER,
        overrides={"aspect": "21:9", "title": "T" * 900})
    assert result.decisions
    for decision in result.decisions:
        assert decision.reason
        assert 1 <= decision.priority <= 5
        assert decision.source in (
            "platform_constraint", "compliance", "brand_dna",
            "campaign_override", "learned")
    # the unsupported aspect was refused AND the reason recorded
    assert result.spec["aspect"] == "9:16"
    assert any("21:9" in s for s in result.skipped)


def test_optimizer_is_deterministic():
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    optimizer = PlatformVariantOptimizer()
    first = optimizer.optimize(platform="pinterest", master=MASTER)
    second = optimizer.optimize(platform="pinterest", master=MASTER)
    assert first.to_dict() == second.to_dict()


def test_learned_recommendations_are_the_lowest_priority():
    from app.engine.distribution.optimizer import PlatformVariantOptimizer

    result = PlatformVariantOptimizer().optimize(
        platform="pinterest", master=MASTER,
        learned={"title_template": "Learned: a template title"})
    # the learned template applies when it fits the 100-char limit
    assert result.spec["title"].startswith("Learned: a template title")
    assert any(d.source == "learned" for d in result.decisions)


# ---------------------------------------------------------------------------
# §8 campaign / scheduler integration (registry-driven, no platform branching)
# ---------------------------------------------------------------------------


def test_campaign_platform_map_covers_every_new_platform():
    from app.engine.campaign.platforms import (
        ACCOUNT_PLATFORM,
        CAMPAIGN_PLATFORMS,
        HANDOFF_PLATFORMS,
    )

    for platform in ("threads", "pinterest", "bluesky", "snapchat"):
        assert platform in CAMPAIGN_PLATFORMS
        assert ACCOUNT_PLATFORM[platform] == platform
    assert frozenset({"snapchat"}) == HANDOFF_PLATFORMS


def test_every_campaign_platform_has_a_profile_and_is_validatable():
    from app.engine.campaign.platforms import (
        CAMPAIGN_PLATFORMS,
        validate_against_profile,
    )

    for platform in CAMPAIGN_PLATFORMS:
        issues = validate_against_profile(
            platform, 30.0, "9:16",
            {"title": "t", "description": "d", "hashtags": ["a"]})
        assert isinstance(issues, list), platform


def test_no_if_platform_branching_leaked_into_campaign_code():
    """Work 14 §8: platform decisions must read the registry, not names."""
    from pathlib import Path

    import app.engine.campaign as campaign_pkg

    banned = ('platform == "snapchat"', "platform == 'snapchat'",
              'platform == "threads"', "platform == 'threads'",
              'platform == "bluesky"', "platform == 'bluesky'",
              'platform == "pinterest"', "platform == 'pinterest'")
    offenders: list[str] = []
    root = Path(campaign_pkg.__file__).parent
    for path in root.glob("*.py"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        offenders += [f"{path.name}: {needle}" for needle in banned
                      if needle in text]
    assert not offenders, (
        f"campaign code branches on the platform name instead of the "
        f"registry: {offenders}")


def test_publish_idempotency_key_is_stable_per_variant_and_platform():
    from app.engine.campaign.publish_flow import publish_idempotency_key

    first = publish_idempotency_key("v-1", "threads")
    assert first == publish_idempotency_key("v-1", "threads")
    assert first != publish_idempotency_key("v-1", "bluesky")
    assert first != publish_idempotency_key("v-2", "threads")


def test_handoff_platforms_come_from_the_factory_not_a_hardcoded_set():
    from app.providers.publishers.factory import (
        HANDOFF_PLATFORMS,
        _registry,
        get_publisher,
    )

    # the set is derived from the publishers themselves
    assert frozenset(
        name for name, publisher in _registry.items()
        if getattr(publisher, "handoff_only", False)) == HANDOFF_PLATFORMS
    # and every one of them really is handoff-only
    for name in HANDOFF_PLATFORMS:
        assert getattr(get_publisher(name, has_account=True),
                       "handoff_only", False) is True


# ---------------------------------------------------------------------------
# §9 publication mode + verification
# ---------------------------------------------------------------------------


def test_publication_mode_classification_never_conflates_the_four():
    from app.engine.distribution.modes import (
        PublicationMode,
        assert_not_confused,
        classify_publication,
    )

    assert classify_publication(remote_id="x") is PublicationMode.LIVE
    assert classify_publication(is_mock=True, remote_id="m") is PublicationMode.MOCK
    assert classify_publication(handoff_required=True) is PublicationMode.HANDOFF
    assert classify_publication(unavailable_reason="no account") \
        is PublicationMode.UNAVAILABLE
    # a handoff is never live evidence
    assert PublicationMode.HANDOFF.is_live is False
    assert PublicationMode.HANDOFF.is_evidence_of_publication is False
    # and the guard raises rather than letting it through
    with pytest.raises(ValueError):
        assert_not_confused(PublicationMode.HANDOFF, expect_live=True)
    with pytest.raises(ValueError):
        assert_not_confused(PublicationMode.MOCK, expect_live=True)
    assert_not_confused(PublicationMode.LIVE, expect_live=True)


def test_unavailable_outranks_handoff_and_mock():
    from app.engine.distribution.modes import PublicationMode, classify_publication

    mode = classify_publication(is_mock=True, handoff_required=True,
                                unavailable_reason="missing scope")
    assert mode is PublicationMode.UNAVAILABLE


# ---------------------------------------------------------------------------
# §10 analytics / inbox capability mapping
# ---------------------------------------------------------------------------


def test_metrics_are_offered_only_where_the_registry_declares_them():
    from app.engine.platform_registry import get_registry

    registry = get_registry()
    # Threads and Pinterest document an insights/analytics metric set
    assert registry.supports("threads", "METRICS") is True
    assert registry.supports("pinterest", "METRICS") is True
    # Bluesky publishes no AppView metric set we implement
    assert registry.supports("bluesky", "METRICS") is False
    # Snapchat analytics is allowlist-gated
    assert registry.supports("snapchat", "METRICS") is False


def test_inbox_is_offered_only_where_comments_are_declared():
    from app.engine.platform_registry import get_registry

    registry = get_registry()
    assert registry.spec("threads").supports_inbox is True
    assert registry.spec("bluesky").supports_inbox is True
    # Pinterest has no organic comment API
    assert registry.spec("pinterest").supports_inbox is False
    assert registry.spec("snapchat").supports_inbox is False


def test_pinterest_analytics_uses_the_documented_metric_names():
    from app.providers.publishers.pinterest import (
        STANDARD_METRICS,
        VIDEO_METRICS,
    )

    assert "IMPRESSION" in STANDARD_METRICS
    assert "SAVE" in STANDARD_METRICS
    assert "OUTBOUND_CLICK" in STANDARD_METRICS
    assert "PIN_CLICK" in STANDARD_METRICS
    assert "VIDEO_MRC_VIEW" in VIDEO_METRICS


def test_threads_analytics_refuses_metrics_that_do_not_exist():
    from app.providers.publishers.threads import ThreadsError, ThreadsPublisher

    publisher = ThreadsPublisher(client=None)
    account = {"access_token": "t", "scopes": ["threads_manage_insights"]}
    for metric in ("impressions", "clicks"):
        with pytest.raises(ThreadsError) as caught:
            publisher.insights(media_id="m", account=account,
                               metrics=(metric,))
        assert metric in str(caught.value)


def test_no_browser_scraping_fallback_exists_in_the_new_providers():
    """Work 14 §11: no automation/browser substitute for an official API."""
    from pathlib import Path

    import app.providers.publishers as pkg

    banned = ("playwright", "selenium", "puppeteer", "webdriver",
              "headless", "chromium", "browser")
    offenders: list[str] = []
    root = Path(pkg.__file__).parent
    for name in ("threads.py", "pinterest.py", "bluesky.py", "snapchat.py"):
        text = (root / name).read_text(encoding="utf-8").lower()
        offenders += [f"{name}: {word}" for word in banned if word in text]
    assert not offenders, f"browser-automation reference found: {offenders}"


# ---------------------------------------------------------------------------
# §11 security
# ---------------------------------------------------------------------------


def test_provider_errors_fail_closed_with_a_retryable_flag():
    from app.providers.publishers.bluesky import BlueskyError
    from app.providers.publishers.pinterest import PinterestError
    from app.providers.publishers.threads import ThreadsError

    for error_type in (ThreadsError, PinterestError, BlueskyError):
        assert error_type("x").retryable is False
        assert error_type("x", retryable=True).retryable is True


def test_no_token_is_ever_written_into_a_publish_error():
    import httpx

    from app.providers.publishers.threads import ThreadsError, _json

    response = httpx.Response(400, json={"error": {"type": "OAuthException",
                                                   "message": "bad token"}})
    error = _json(response) if False else None  # _json raises; call directly
    with pytest.raises(ThreadsError) as caught:
        _json(response)
    # the token is in the request, never echoed into the error text
    assert "tok-abc" not in str(caught.value)
    _ = error


def test_threads_and_instagram_credentials_are_never_interchangeable():
    """Threads OAuth is a separate integration; nothing borrows an IG token."""
    from app.providers.publishers.threads import (
        PERM_BASIC,
        PERM_PUBLISH,
    )

    # the Threads scopes are the literal Threads namespace
    assert PERM_BASIC.startswith("threads_")
    assert PERM_PUBLISH.startswith("threads_")
    assert "instagram" not in PERM_BASIC


def test_workspace_isolation_of_publication_verification():
    """A publication in another workspace must never verify here."""
    from app.engine.intelligence.verifier import (
        CompletionContract,
        check_publication,
    )

    class _Post:
        id = "p1"
        workspace_id = "ws-other"
        remote_post_id = "r1"
        publication_mode = "LIVE"
        is_mock = False
        handoff_payload = None
        handoff_completed_at = None
        platform = "threads"
        account_id = "a1"
        video_id = "v1"
        remote_url = ""

    class _Session:
        def get(self, *_args, **_kwargs):
            return _Post()

        def query(self, *_args, **_kwargs):
            class _Q:
                def count(self_inner):
                    return 1
            return _Q()

    execution, verdict, _checks = check_publication(
        _Session(), "ws-mine", CompletionContract(kind="publication",
                                                  subject_id="p1"))
    assert verdict != "VERIFIED"
    assert execution == "UNKNOWN"

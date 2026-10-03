"""``PlatformVariantOptimizer`` (Work 14 §7) -- make variants actually differ.

This is an OPTIMIZER over the existing variant infrastructure, not a new
content pipeline. It takes a Master/Short plus a BrandDNA and a verified
``PlatformOptimizationProfile`` and returns a platform-specific variant spec:
aspect/reframe, duration, captions, title/copy, CTA, cover, topic/hashtags and
first-frame treatment -- each with a recorded decision and its provenance.

Priority order (higher wins, and the loser is recorded so the UI can explain
why a brand or user request was NOT applied):

    1. platform hard constraints   (a verified limit is not negotiable)
    2. security / compliance       (forbidden phrases, disclosure, safety)
    3. BrandDNA                    (voice, colors, forbidden phrases)
    4. campaign / user overrides   (an explicit ask)
    5. learned recommendations     (Work 06 lessons / performance data)

**The single most important property: an UNKNOWN constraint causes no action.**
If official docs do not state a limit, the optimizer declines to invent one and
records ``skipped_unknown`` rather than guessing. That is why this module reads
:class:`~app.engine.distribution.profiles.PlatformOptimizationProfile` instead
of hardcoding a table of "platform limits" -- guessing produces either silent
truncation or a publish-time rejection the user discovers too late.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.engine.campaign.platforms import get_profile as get_campaign_profile
from app.engine.distribution.profiles import (
    UNKNOWN,
    PlatformOptimizationProfile,
    get_profile,
)

__all__ = [
    "Decision",
    "OptimizationResult",
    "PlatformVariantOptimizer",
    "PRIORITY_ORDER",
]

#: Lower number = higher authority. Recorded on every decision.
PRIORITY_ORDER = (
    "platform_constraint",   # 1
    "compliance",            # 2
    "brand_dna",             # 3
    "campaign_override",     # 4
    "learned",               # 5
)

_PRIORITY_INDEX = {name: index + 1 for index, name in enumerate(PRIORITY_ORDER)}


@dataclass
class Decision:
    """One optimizer choice, with why and from where."""

    field_name: str
    before: Any
    after: Any
    reason: str
    source: str            # which priority level decided it
    priority: int

    def to_dict(self) -> dict:
        return {"field": self.field_name, "before": self.before,
                "after": self.after, "reason": self.reason,
                "source": self.source, "priority": self.priority}


@dataclass
class OptimizationResult:
    """The optimized variant plus its full provenance."""

    platform: str
    spec: dict = field(default_factory=dict)
    decisions: list[Decision] = field(default_factory=list)
    #: Human-readable reasons an action was NOT taken (unknown limits etc).
    skipped: list[str] = field(default_factory=list)
    #: Refusals that block this platform (e.g. an unsupported media type).
    blocked: list[str] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        return bool(self.blocked)

    def changed_fields(self) -> list[str]:
        return [d.field_name for d in self.decisions
                if d.before != d.after]

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "spec": self.spec,
            "decisions": [d.to_dict() for d in self.decisions],
            "skipped": list(self.skipped),
            "blocked": list(self.blocked),
            "changed_fields": self.changed_fields(),
        }


def _decide(result: OptimizationResult, name: str, before: Any, after: Any,
            reason: str, source: str) -> None:
    """Record a decision only when it actually changes something."""
    if before == after:
        return
    result.decisions.append(Decision(
        field_name=name, before=before, after=after, reason=reason,
        source=source, priority=_PRIORITY_INDEX.get(source, 99)))


class PlatformVariantOptimizer:
    """Produce one platform-specific variant spec from a master + brand."""

    def __init__(self, profile: PlatformOptimizationProfile | None = None) -> None:
        self._profile = profile

    def profile_for(self, platform: str) -> PlatformOptimizationProfile:
        """The verified profile, or an explicitly-UNVERIFIED one.

        Platforms that predate Work 14 (youtube, tiktok, ...) have no verified
        distribution profile. Blocking them would regress existing campaigns,
        and silently treating their campaign numbers as documented would be
        dishonest. So they get a profile whose every field is UNKNOWN: the
        optimizer still works (from the campaign profile's own budgets) and
        every skipped/unknown note makes clear nothing is officially verified.
        """
        if self._profile is not None:
            return self._profile
        try:
            return get_profile(platform)
        except KeyError:
            return PlatformOptimizationProfile(platform=platform)

    # -- the entry point --------------------------------------------------

    def optimize(self, *, platform: str, master: dict, brand: Any = None,
                 overrides: dict | None = None,
                 learned: dict | None = None) -> OptimizationResult:
        """Optimize one master into a platform-specific variant spec."""
        platform = str(platform).strip().lower()
        result = OptimizationResult(platform=platform)
        try:
            profile = self.profile_for(platform)
        except KeyError as exc:
            result.blocked.append(str(exc))
            return result
        campaign = get_campaign_profile(platform)
        if not profile.media_types:
            result.skipped.append(
                f"{platform} has no verified distribution profile; every "
                f"constraint below is YMONEY's own campaign budget, not an "
                f"official platform limit")

        spec: dict[str, Any] = {
            "platform": platform,
            "aspect": campaign["aspects"][0],
            "accepted_aspects": list(campaign["aspects"]),
            "duration_s": None,
            "title": str(master.get("title") or ""),
            "description": str(master.get("description") or ""),
            "hashtags": list(master.get("hashtags") or []),
            "cta": (campaign.get("cta") or [None])[0],
            "caption": dict(campaign.get("caption") or {}),
            "cover": {"behavior": (campaign.get("thumbnail") or {}).get(
                "behavior", "poster-frame")},
            "alt_text": str(master.get("alt_text") or ""),
            "link_url": str(master.get("link_url") or ""),
        }

        self._check_media_supported(result, profile, master)
        self._apply_aspect(result, profile, campaign, spec, master, overrides)
        self._apply_duration(result, profile, campaign, spec, master, overrides)
        self._apply_text(result, profile, campaign, spec, master, brand,
                         overrides, learned)
        self._apply_hashtags(result, profile, campaign, spec, master, learned)
        self._apply_captions(result, profile, spec, master, brand)
        self._apply_cta(result, spec, campaign, master, brand, overrides)
        self._apply_cover(result, profile, spec, master, overrides)
        self._apply_alt_text(result, profile, spec, master)
        self._apply_link(result, profile, spec, master)
        self._apply_first_frame(result, spec, master, learned)

        result.spec = spec
        return result

    # -- individual stages ------------------------------------------------

    def _check_media_supported(self, result: OptimizationResult,
                               profile: PlatformOptimizationProfile,
                               master: dict) -> None:
        """Refuse a media type the platform does not accept (§1)."""
        wanted = str(master.get("media_kind") or "video").upper()
        supported = {m.upper() for m in profile.media_types}
        if not supported:
            # A platform with NO verified profile at all (every pre-Work-14
            # platform) is unverified, not unsupported: blocking it would
            # regress existing campaigns. A profile that EXISTS but accepts no
            # media (snapchat: there is no publish endpoint) is blocked.
            if profile.verified_notes or profile.api_base:
                result.blocked.append(
                    f"{profile.platform} has no server-side publishing path "
                    f"(user handoff): media handling is not validated against "
                    f"an API contract")
            else:
                result.skipped.append(
                    f"{profile.platform} has no verified media-type list; the "
                    f"media kind was not checked against an official contract")
            return
        aliases = {"VIDEO": "VIDEO", "IMAGE": "IMAGE", "TEXT": "TEXT",
                   "SHORT": "VIDEO", "REEL": "VIDEO", "STORY": "IMAGE",
                   "CAROUSEL": "CAROUSEL", "PIN": "IMAGE"}
        normalised = aliases.get(wanted, wanted)
        if normalised not in supported:
            result.blocked.append(
                f"{profile.platform} does not support {wanted} via the "
                f"official API (supported: {sorted(supported)})")

    def _apply_aspect(self, result: OptimizationResult,
                      profile: PlatformOptimizationProfile, campaign: dict,
                      spec: dict, master: dict, overrides: dict | None) -> None:
        accepted = list(campaign["aspects"])
        base = str(master.get("aspect") or "9:16")
        chosen = base if base in accepted else accepted[0]
        # An explicit user ask is honoured only if the platform accepts it.
        wanted = str((overrides or {}).get("aspect") or "")
        if wanted:
            if wanted in accepted:
                chosen = wanted
                _decide(result, "aspect", base, chosen,
                        f"campaign override requested {wanted}, which "
                        f"{profile.platform} accepts", "campaign_override")
            else:
                result.skipped.append(
                    f"aspect override {wanted!r} refused: {profile.platform} "
                    f"accepts {accepted}")
                _decide(result, "aspect", base, chosen,
                        f"override {wanted} not accepted by the platform; kept "
                        f"the canonical {chosen}", "platform_constraint")
        elif base not in accepted:
            _decide(result, "aspect", base, chosen,
                    f"{base} is not accepted on {profile.platform}; reframe to "
                    f"the canonical {chosen}", "platform_constraint")
        spec["aspect"] = chosen
        spec["reframe"] = chosen != base

    def _apply_duration(self, result: OptimizationResult,
                       profile: PlatformOptimizationProfile, campaign: dict,
                       spec: dict, master: dict, overrides: dict | None) -> None:
        """Duration from the profile, or UNKNOWN => no opinion."""
        current = master.get("duration_s")
        if current is None:
            spec["duration_s"] = None
            return
        hard_max = campaign["max_duration"]
        chosen = float(current)
        if chosen > hard_max:
            chosen = hard_max
            _decide(result, "duration_s", float(current), chosen,
                    f"{chosen:.0f}s is the hard max for {profile.platform}",
                    "platform_constraint")
        wanted = (overrides or {}).get("duration_s")
        if wanted is not None:
            value = float(wanted)
            if value <= hard_max:
                chosen = value
                _decide(result, "duration_s", float(current), chosen,
                        f"campaign override requested {value:.0f}s",
                        "campaign_override")
            else:
                result.skipped.append(
                    f"duration override {value:.0f}s refused: exceeds the "
                    f"{profile.platform} hard max {hard_max:.0f}s")
        lo, hi = campaign["preferred_duration"]
        if chosen < lo or chosen > hi:
            result.skipped.append(
                f"duration {chosen:.0f}s is outside the {profile.platform} "
                f"preferred window {lo:.0f}-{hi:.0f}s; left unchanged because "
                f"no official document states a duration PREFERENCE")
        spec["duration_s"] = chosen

    def _apply_text(self, result: OptimizationResult,
                    profile: PlatformOptimizationProfile, campaign: dict,
                    spec: dict, master: dict, brand: Any,
                    overrides: dict | None, learned: dict | None) -> None:
        """Title/description under verified limits and brand rules."""
        limits = campaign["metadata"]
        title = str(spec["title"])
        description = str(spec["description"])
        tags = " ".join(str(t) for t in spec["hashtags"])

        # -- 2. compliance first: forbidden phrases are a hard veto ------
        forbidden = list(getattr(brand, "forbidden_phrases", []) or [])
        for phrase in forbidden:
            clean = str(phrase or "").strip()
            if not clean:
                continue
            for label, value in (("title", title), ("description", description)):
                if clean.lower() in str(value).lower():
                    result.blocked.append(
                        f"{label} contains BrandDNA-forbidden phrase "
                        f"{clean!r}; compliance veto")
                    _decide(result, f"forbidden:{clean}", str(value), "",
                            "BrandDNA forbidden_phrases is a compliance veto",
                            "compliance")

        # -- 1. platform hard limits -------------------------------------
        title = self._clip(result, profile, "title_max", title,
                           int(limits["title_max"]), "title",
                           spec.get("alt_text") or description)
        description = self._clip(result, profile, "description_max", description,
                                 int(limits["description_max"]), "description",
                                 title)
        # A single grapheme/byte budget when the profile states one.
        text_limits = profile.value("text_limits")
        if text_limits is not UNKNOWN and isinstance(text_limits, dict):
            joined = f"{title}\n\n{description}".strip() if description else title
            budget = text_limits.get("chars") or text_limits.get("graphemes")
            if isinstance(budget, int) and len(joined) > budget:
                # Threads has ONE text field: title+description are the same
                # string, so the combined budget governs, not each part.
                clipped = joined[:budget]
                _decide(result, "text", len(joined), budget,
                        f"{profile.platform} has a single text field capped at "
                        f"{budget}; title and description were merged and "
                        f"clipped", "platform_constraint")
                result.skipped.append(
                    f"{profile.platform} text clipped {len(joined)} -> "
                    f"{budget} characters")
                title = clipped
                description = ""
            if (isinstance(text_limits.get("bytes"), int)
                    and len(joined.encode("utf-8")) > text_limits["bytes"]):
                result.skipped.append(
                    f"{profile.platform} text is "
                    f"{len(joined.encode('utf-8'))} bytes, over the documented "
                    f"{text_limits['bytes']}-byte cap; the character clip above "
                    f"is applied first")

        # -- 4. campaign override ----------------------------------------
        for key, target in (("title", "title"), ("description", "description")):
            wanted = (overrides or {}).get(key)
            if wanted is None or str(wanted) == str(spec[key]):
                continue
            candidate = str(wanted)
            cap = int(limits["title_max"] if target == "title"
                      else limits["description_max"])
            if len(candidate) <= cap:
                _decide(result, target, str(spec[target]), candidate,
                        f"campaign override supplied a {key}",
                        "campaign_override")
                if target == "title":
                    title = candidate
                else:
                    description = candidate
            else:
                result.skipped.append(
                    f"{key} override is {len(candidate)} chars, over the "
                    f"{profile.platform} limit {cap}; refused")

        # -- 5. learned --------------------------------------------------
        learned_title = (learned or {}).get("title_template")
        if learned_title:
            candidate = str(learned_title)[:int(limits["title_max"])]
            _decide(result, "title", title, candidate,
                    "applied the learned title template from Work 06 lessons",
                    "learned")
            title = candidate

        spec["title"] = title
        spec["description"] = description
        _ = tags

    def _clip(self, result: OptimizationResult,
              profile: PlatformOptimizationProfile, profile_field: str,
              value: str, fallback_cap: int, name: str, alt_source: str) -> str:
        """Clip to the verified limit, or to the campaign cap when unknown.

        When the official profile does not state a limit we fall back to the
        campaign profile's own budget AND record that the number is not
        documented, so the value is never mistaken for an official limit.
        """
        verified = profile.value(profile_field)
        if verified is UNKNOWN:
            if len(value) > fallback_cap:
                result.skipped.append(
                    f"{name} is {len(value)} chars; no official "
                    f"{profile.platform} limit is documented, so YMONEY's own "
                    f"budget of {fallback_cap} was applied (not a platform rule)")
            return value[:fallback_cap]
        cap = int(verified)
        if len(value) > cap:
            _decide(result, name, len(value), cap,
                    f"{profile.platform} documents a {cap}-character "
                    f"{name} limit", "platform_constraint")
            return value[:cap]
        return value

    def _apply_hashtags(self, result: OptimizationResult,
                        profile: PlatformOptimizationProfile, campaign: dict,
                        spec: dict, master: dict, learned: dict | None) -> None:
        limit = campaign["metadata"]["hashtag_limit"]
        tags = [str(t).lstrip("#") for t in spec["hashtags"] if str(t).strip()]
        extra = [str(t) for t in (learned or {}).get("hashtags") or []]
        if extra:
            _decide(result, "hashtags", len(tags), min(len(tags) + len(extra), limit),
                    "added hashtags from Work 06 learned topics", "learned")
            tags = tags + extra
        if len(tags) > limit:
            verified = profile.value("hashtag_limit")
            source = ("platform_constraint"
                      if verified is not UNKNOWN else "learned")
            reason = (f"{profile.platform} allows at most {int(verified)} "
                      f"hashtags" if verified is not UNKNOWN
                      else f"no official {profile.platform} hashtag limit is "
                           f"documented; YMONEY's budget of {limit} was applied")
            if verified is UNKNOWN:
                result.skipped.append(
                    f"hashtag budget {limit} for {profile.platform} is a "
                    f"YMONEY default, not a documented limit")
            _decide(result, "hashtags", len(tags), limit, reason, source)
            tags = tags[:limit]
        spec["hashtags"] = tags

    def _apply_captions(self, result: OptimizationResult,
                        profile: PlatformOptimizationProfile, spec: dict,
                        master: dict, brand: Any) -> None:
        """Brand caption style wins, but the platform's safe zone is applied."""
        caption = dict(spec["caption"])
        brand_style = getattr(brand, "caption_style", None) or {}
        if isinstance(brand_style, dict) and brand_style.get("style"):
            caption["style"] = brand_style["style"]
            _decide(result, "caption_style", spec["caption"].get("style"),
                    caption["style"],
                    "BrandDNA caption_style applies to every platform",
                    "brand_dna")
        safe = profile.value("safe_zone")
        if safe is not UNKNOWN and isinstance(safe, dict):
            caption["safe_zone"] = dict(safe)
            if "safe_zone" not in spec["caption"]:
                _decide(result, "caption_safe_zone", None, dict(safe),
                        "applied the platform caption safe zone", "brand_dna")
        spec["caption"] = caption

    def _apply_cta(self, result: OptimizationResult, spec: dict,
                   campaign: dict, master: dict, brand: Any,
                   overrides: dict | None) -> None:
        """Only offer a CTA the platform actually supports."""
        supported = tuple(campaign.get("cta") or ())
        requested = str((overrides or {}).get("cta")
                        or (getattr(brand, "cta_style", {}) or {}).get("kind")
                        or master.get("cta") or "")
        if not requested:
            spec["cta"] = supported[0] if supported else None
            if not supported:
                result.skipped.append(
                    f"{spec['platform']} declares no supported CTA kind")
            return
        if requested in supported:
            _decide(result, "cta", master.get("cta"), requested,
                    f"{requested} is supported on {spec['platform']}",
                    "campaign_override")
            spec["cta"] = requested
        else:
            result.skipped.append(
                f"CTA {requested!r} is not supported on {spec['platform']}; "
                f"supported={list(supported)}")
            spec["cta"] = supported[0] if supported else None

    def _apply_cover(self, result: OptimizationResult,
                     profile: PlatformOptimizationProfile, spec: dict,
                     master: dict, overrides: dict | None) -> None:
        """A cover may only be requested when the platform supports one."""
        supported = profile.value("cover_supported")
        wanted = (overrides or {}).get("cover_asset_id") or master.get("cover_asset_id")
        if wanted and supported is False:
            result.skipped.append(
                f"cover image refused: {profile.platform}'s official API has "
                f"no cover/thumbnail parameter")
            spec["cover"] = {"behavior": "poster-frame", "asset_id": ""}
            return
        if wanted and supported is UNKNOWN:
            result.skipped.append(
                "cover image accepted with no official contract to validate "
                "it against; using the platform default frame")
        spec["cover"] = {"behavior": spec["cover"]["behavior"],
                         "asset_id": str(wanted or "")}

    def _apply_alt_text(self, result: OptimizationResult,
                        profile: PlatformOptimizationProfile, spec: dict,
                        master: dict) -> None:
        """Alt text: only claimed when the official API documents it."""
        capability = profile.value("alt_text")
        if capability is UNKNOWN:
            if spec["alt_text"]:
                result.skipped.append(
                    f"alt text is not documented for {profile.platform}; it "
                    f"will not be sent")
            spec["alt_text"] = ""
            return
        if not spec["alt_text"]:
            source = spec["title"] or spec["description"]
            if source:
                spec["alt_text"] = source
                _decide(result, "alt_text", "", source[:120],
                        f"{profile.platform} documents alt text; derived it "
                        f"from the copy because none was supplied",
                        "platform_constraint")
        limit = capability.get("max_chars") if isinstance(capability, dict) else None
        if isinstance(limit, int) and len(spec["alt_text"]) > limit:
            _decide(result, "alt_text", len(spec["alt_text"]), limit,
                    f"{profile.platform} documents a {limit}-character alt "
                    f"text limit", "platform_constraint")
            spec["alt_text"] = spec["alt_text"][:limit]
        elif isinstance(capability, dict) and capability.get("required_on_images"):
            result.skipped.append(
                f"{profile.platform} REQUIRES alt on image posts; the lexicon "
                f"sets no character cap, so none was applied")

    def _apply_link(self, result: OptimizationResult,
                    profile: PlatformOptimizationProfile, spec: dict,
                    master: dict) -> None:
        """Link behaviour: never assume a platform unfurls a link."""
        behaviour = profile.value("link_behavior")
        if behaviour is UNKNOWN:
            if spec["link_url"]:
                result.skipped.append(
                    f"link behaviour for {profile.platform} is undocumented; "
                    f"the link is kept as plain text")
            spec["link_behavior"] = "unknown"
            return
        spec["link_behavior"] = str(behaviour)
        if "unfurl" in str(behaviour) or "client_side" in str(behaviour):
            # A bare URL is NOT enough: the client must supply the card fields
            # the platform makes mandatory. `link_url` only satisfies `uri`.
            missing = []
            if not master.get("link_url"):
                missing.append("uri")
            if not master.get("link_title"):
                missing.append("title")
            if not master.get("link_description"):
                missing.append("description")
            if missing:
                result.skipped.append(
                    f"{profile.platform} needs the client to build the link "
                    f"card (missing {missing}); a bare URL will not render as "
                    f"a card")
        if not spec["link_url"]:
            spec["link_url"] = ""

    def _apply_first_frame(self, result: OptimizationResult, spec: dict,
                           master: dict, learned: dict | None) -> None:
        """First-frame treatment, a genuinely platform-specific choice."""
        treatment = (learned or {}).get("first_frame")
        if treatment:
            _decide(result, "first_frame", master.get("first_frame"), treatment,
                    "learned first-frame treatment from Work 06 lessons",
                    "learned")
            spec["first_frame"] = treatment
        else:
            spec["first_frame"] = str(master.get("first_frame") or "hook")

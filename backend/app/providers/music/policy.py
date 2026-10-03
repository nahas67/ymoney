"""Music policy: generated soundtracks are OPT-IN, and the brand owns the taste.

``generate_music`` (Work 15.5) had zero callers. This module supplies the two
things it needed before it could be called from a pipeline, and both are gates
rather than features:

1. **Consent.** A paid, generated asset is not a free side effect. Generation
   only runs when a workspace has explicitly opted in via
   ``settings_json["music"]["generate"] is True`` -- the same opt-in shape
   ``engine/performance/learning.py::learning_assist_enabled`` uses for
   ``settings_json["learning_assist"]`` (a boolean, default False, read from the
   workspace's own settings). **Unset is not "yes"**: an unset policy returns a
   decision whose ``generate`` is False and whose ``reason`` says
   ``"policy_not_enabled"``, so the caller records the refusal instead of
   quietly spending money.

2. **Brand authority.** ``BrandDNA.music_prefs`` (``engine/brand/dna.py:157``)
   is the only source of taste. The keys honoured here are exactly the ones the
   typed document already carries as free-form ``dict[str, Any]`` -- they are
   read, never invented:

       mood, genre, brief, preferred_genres, forbidden_genres,
       instrumental (vocal policy), energy, intensity, enabled

   ``forbidden_genres`` is a HARD rule: :func:`enforce_forbidden_genres` raises
   before any billable call, and it is applied *after* a recommendation layer so
   a learned/recommended style can never smuggle a forbidden genre past it. A
   preference that is unset contributes nothing -- this module never picks a
   genre, mood or energy on the brand's behalf.

The policy decision is a plain frozen dataclass so the API can render it and the
pipeline can record it verbatim as lineage: "why there is no soundtrack" is as
important a fact as "there is one".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.providers.music.base import (
    DISABLED_MUSIC_PREFERENCES,
    MusicRequest,
)

__all__ = [
    "MUSIC_PREF_KEYS",
    "MUSIC_SETTINGS_KEY",
    "MusicPolicy",
    "MusicPolicyRefused",
    "brand_music_prefs",
    "enforce_forbidden_genres",
    "music_policy",
    "music_settings",
    "recommend_style",
]

#: The workspace settings key holding the opt-in, mirroring
#: ``settings_json["learning_assist"]`` (a boolean, default False).
MUSIC_SETTINGS_KEY = "music"

#: The exact ``BrandDNA.music_prefs`` keys this policy reads. Every key is an
#: operator-set value inside the typed document's free-form ``music_prefs`` dict;
#: nothing here adds a field to BrandDNA.
MUSIC_PREF_KEYS: tuple[str, ...] = (
    "mood",
    "genre",
    "brief",
    "preferred_genres",
    "forbidden_genres",
    "instrumental",
    "energy",
    "intensity",
    "enabled",
)

#: ``music_prefs["enabled"] is False`` means "no soundtrack", the same intent as a
#: ``brand_templates.music_preference`` label in DISABLED_MUSIC_PREFERENCES.
DISABLED_PREF_KEY = "enabled"


class MusicPolicyRefused(RuntimeError):
    """A hard music rule refused generation.

    Raised BEFORE any billable call, so a refusal can never cost money. The
    pipeline catches it and degrades to a soundtrack-less render.
    """


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on")
    return bool(value)


def _genre_list(value: Any) -> list[str]:
    """A genre list, lowercased, de-duplicated, order preserved.

    A bare string is accepted as a one-element list so an operator does not have
    to know the shape; anything unrecognised yields ``[]`` rather than a guess.
    """
    if value is None:
        return []
    if isinstance(value, str):
        raw = [value]
    elif isinstance(value, (list, tuple, set)):
        raw = list(value)
    else:
        return []
    out: list[str] = []
    for item in raw:
        text = str(item or "").strip().lower()
        if text and text not in out:
            out.append(text)
    return out


def _text(value: Any) -> str:
    return str(value or "").strip()


def music_settings(workspace_or_settings: Any) -> dict:
    """``settings_json["music"]`` for a Workspace row, a settings dict or None.

    Never raises and never invents a default policy: an absent key yields ``{}``
    so the caller sees "unset", not "off by a value I chose".
    """
    try:
        if workspace_or_settings is None:
            return {}
        if isinstance(workspace_or_settings, dict):
            raw = workspace_or_settings.get(MUSIC_SETTINGS_KEY)
        else:
            raw = getattr(workspace_or_settings, "settings_json", None) or {}
            raw = raw.get(MUSIC_SETTINGS_KEY) if isinstance(raw, dict) else None
    except Exception:  # noqa: BLE001 — a broken settings blob must not break music
        return {}
    return dict(raw) if isinstance(raw, dict) else {}


def brand_music_prefs(dna: Any) -> dict:
    """The ``music_prefs`` mapping from a BrandDNA, a dna_json dict or ``None``.

    Only :data:`MUSIC_PREF_KEYS` survive, so an unrelated or malformed entry in
    the document cannot leak into a provider prompt. A missing document is
    ``{}`` -- "no preference stated", never a default genre.
    """
    raw: Any = None
    if dna is None:
        raw = None
    elif isinstance(dna, dict):
        raw = dna.get("music_prefs")
        if raw is None and "music_prefs" not in dna:
            raw = None
    else:
        raw = getattr(dna, "music_prefs", None)
    if not isinstance(raw, dict):
        return {}
    return {key: raw[key] for key in MUSIC_PREF_KEYS if key in raw}


@dataclass(frozen=True)
class MusicPolicy:
    """One resolved decision: may we generate, and from which preferences?

    ``generate`` is the ONLY field that authorises a billable call. ``reason`` is
    the recorded explanation and is always populated when ``generate`` is False.
    """

    workspace_id: str = ""
    generate: bool = False
    reason: str = "policy_not_enabled"
    provider_key: str = "elevenlabs_music"
    prefs: dict = field(default_factory=dict)
    #: True when the brand explicitly refused music (``enabled: False`` or a
    #: disabled preference label). A refusal outranks the workspace opt-in.
    brand_disabled: bool = False
    #: The forbidden-genre list, kept on the decision so a caller can render it
    #: and a test can assert the rule travelled with the decision.
    forbidden_genres: tuple[str, ...] = ()

    @property
    def mood(self) -> str:
        return _text(self.prefs.get("mood"))

    @property
    def genre(self) -> str:
        return _text(self.prefs.get("genre"))

    @property
    def brief(self) -> str:
        return _text(self.prefs.get("brief"))

    @property
    def preferred_genres(self) -> list[str]:
        return _genre_list(self.prefs.get("preferred_genres"))

    @property
    def energy(self) -> str:
        return _text(self.prefs.get("energy"))

    @property
    def intensity(self) -> str:
        return _text(self.prefs.get("intensity"))

    @property
    def instrumental(self) -> bool | None:
        """Vocal policy as stated, or ``None`` when the brand did not state one.

        ``None`` is meaningful: the prompt's own "instrumental only" line stays
        in force, and the pipeline reports "unstated" rather than claiming a
        vocal preference nobody gave.
        """
        value = self.prefs.get("instrumental")
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip().lower()
            if text in ("instrumental", "no vocals", "no_vocals"):
                return True
            if text in ("vocal", "vocals", "with vocals"):
                return False
            return None
        return bool(value)

    def to_dict(self) -> dict:
        return {
            "workspace_id": self.workspace_id,
            "generate": self.generate,
            "reason": self.reason,
            "provider_key": self.provider_key,
            "brand_disabled": self.brand_disabled,
            "forbidden_genres": list(self.forbidden_genres),
            "prefs": dict(self.prefs),
            "instrumental": self.instrumental,
            "note": ("generate=False with a reason means the soundtrack was "
                     "declined on purpose, not that it failed"),
        }

    def apply_to(self, request: MusicRequest) -> MusicRequest:
        """Stamp this policy's preferences onto a MusicRequest.

        Only *stated* preferences are written. An unset mood stays unset so the
        provider -- not this module -- decides, and ``brief``/genre reach the
        prompt through ``MusicRequest.style()`` exactly as Work 15.5 wired them.
        """
        prefs = dict(request.brand_music_prefs or {})
        prefs.update(self.prefs)
        request.brand_music_prefs = prefs
        if self.mood and not request.mood:
            request.mood = self.mood
        if self.genre and not request.genre:
            request.genre = self.genre
        if self.energy or self.intensity:
            prefs["energy"] = self.energy
            prefs["intensity"] = self.intensity
        return request


def music_policy(workspace_or_settings: Any, dna: Any = None, *,
                 provider_key: str = "elevenlabs_music",
                 workspace_id: str = "") -> MusicPolicy:
    """Resolve whether generated music may run, and under which preferences.

    Precedence, strongest first:

    1. **Brand refusal** -- ``music_prefs["enabled"] is False`` or a
       ``brand_templates.music_preference`` label in
       ``DISABLED_MUSIC_PREFERENCES``. A brand that says no is never overridden
       by a workspace opt-in.
    2. **Workspace opt-in** -- ``settings_json["music"]["generate"] is True``.
       Anything else (missing, False, a non-boolean) means no generation and the
       reason explains which case it was.

    Reads only ``settings_json`` and the ``music_prefs`` a caller supplies; it
    never resolves the brand document itself, so the caller keeps control of
    which brand/DNA layer applies.
    """
    settings = music_settings(workspace_or_settings)
    prefs = brand_music_prefs(dna)
    forbidden = _genre_list(prefs.get("forbidden_genres"))
    base = {"workspace_id": str(workspace_id or ""),
            "provider_key": str(provider_key or "elevenlabs_music"),
            "prefs": prefs,
            "forbidden_genres": tuple(forbidden)}

    if DISABLED_PREF_KEY in prefs and not _truthy(prefs.get(DISABLED_PREF_KEY)):
        return MusicPolicy(**base, generate=False, reason="brand_disabled_music",
                           brand_disabled=True)
    label = _text(settings.get("preference")).lower()
    if label in DISABLED_MUSIC_PREFERENCES:
        return MusicPolicy(**base, generate=False, reason="brand_disabled_music",
                           brand_disabled=True)
    if "generate" not in settings:
        return MusicPolicy(**base, generate=False, reason="policy_not_enabled")
    if not _truthy(settings.get("generate")):
        return MusicPolicy(**base, generate=False, reason="policy_not_enabled")
    return MusicPolicy(**base, generate=True, reason="policy_enabled")


def enforce_forbidden_genres(genres: Any, policy: MusicPolicy) -> list[str]:
    """Refuse generation when a genre the brand forbade was requested.

    ``genres`` is the requested genre or list (from the brand, a brief, or a
    recommendation layer). Raises :class:`MusicPolicyRefused` naming the exact
    offending genre so the refusal is recorded verbatim. Returns the cleaned
    genre list when nothing is forbidden.
    """
    requested = _genre_list(genres)
    forbidden = set(policy.forbidden_genres)
    if not forbidden:
        return requested
    hits = [genre for genre in requested if genre in forbidden]
    if hits:
        raise MusicPolicyRefused(
            f"brand forbids {hits!r}; a recommendation or brief cannot "
            f"override a BrandDNA hard rule")
    return requested


def recommend_style(candidate: dict, policy: MusicPolicy) -> dict:
    """Filter a *recommendation* through the brand's hard rules.

    The learning/recommendation layer may propose a mood/genre/brief; this is the
    only place its output meets the brand. A candidate naming a forbidden genre
    is dropped (the keys return empty) and the refusal is reported, rather than
    the value being quietly rewritten -- a silent repair would hide the conflict
    from the operator who has to resolve it.

    Brand-stated preferences are never overwritten by a recommendation.
    """
    out = dict(candidate or {})
    try:
        enforce_forbidden_genres(out.get("genre"), policy)
    except MusicPolicyRefused:
        return {"mood": "", "genre": "", "brief": _text(out.get("brief")),
                "rejected": True,
                "reason": "recommendation named a forbidden genre"}
    merged = {"mood": policy.mood or _text(out.get("mood")),
              "genre": policy.genre or _text(out.get("genre")),
              "brief": policy.brief or _text(out.get("brief")),
              "rejected": False,
              "reason": ""}
    return merged


def duration_within_tolerance(actual: float, expected: float, *,
                              tolerance: float = 0.25) -> bool:
    """True when a generated track is close enough to the timeline length.

    A soundtrack is mixed under narration for the whole video, so a track that
    is wildly shorter leaves silence and one that is wildly longer is trimmed by
    the render. The provider's own coverage floor lives in
    ``elevenlabs_music.MIN_COVERAGE_RATIO``; this is the timeline-side check that
    a bed actually fits the video it was cut for.
    """
    try:
        actual = float(actual)
        expected = float(expected)
    except (TypeError, ValueError):
        return False
    if actual <= 0 or expected <= 0:
        return False
    return abs(actual - expected) <= max(0.0, float(tolerance)) * expected
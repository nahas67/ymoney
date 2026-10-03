"""AI-music provider interface: one billable soundtrack per video.

This is the ONLY seam YMONEY has for generating a soundtrack. It deliberately
does NOT create a second audio or render system:

* the output is a canonical ``MediaAsset`` (``models/assets.py``) of type
  ``audio``, so every existing consumer -- the timeline renderer, QC, the
  editor -- already understands it;
* the placement is a clip on the EXISTING ``music`` track kind
  (``models/timeline.py`` TRACK_KINDS, mixed by
  ``providers/video_engine/timeline_render.py`` together with ``voice``/``sfx``),
  not a bespoke render step;
* the license/allowlist gate is the existing ``_bgm_for`` allowlist
  (``providers/video_engine/ffmpeg_avatar.py``).

Inputs are the editorial context (video asset, duration, mood, genre, campaign)
plus the brand's music preference. ``brand_templates.music_preference`` is a
label that today reaches no provider; :func:`build_music_prompt` is where it
becomes a real generation input.

Safety behaviour is inherited from the donor and is not optional:

* analysis uses a PROXY (audio-stripped, 1280 long-edge H.264, byte-capped,
  deleted in ``finally``), never the HD master;
* the downloaded audio is streamed with a byte cap, ``fsync``-ed, and then
  FULLY decoded by ffmpeg before ``os.replace`` publishes it -- a truncated or
  undecodable stream never reaches the final path;
* the provider is BILLABLE, so submits go through
  ``services/paid_jobs.py``: a lost submit response is ``SUBMISSION_UNKNOWN``
  and is never auto-retried;
* music is a nice-to-have: :func:`music_or_none` degrades to ``None`` with a
  warning so the video still renders.

Ported from MoneyPrinterTurbo 1.3.7 (MIT, Copyright (c) 2024 Harry): the donor's
``elevenlabs_music.py`` / ``sonilo.py`` supplied the proxy+validate+atomic-
publish shape and the "BGM failure must not fail the video" rule. The
``PaidSubmission*`` contract, the canonical ``MediaAsset`` and the ``music``
track placement are YMONEY's.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from app.services.paid_jobs import (
    PaidArtifactUndownloadable,
    PaidJobRejected,
    PaidSubmissionUnconfirmed,
    SubmissionState,
)

logger = logging.getLogger("ymoney.music")

#: The timeline track kind the soundtrack is placed on. Reused verbatim -- this
#: is what keeps the music inside the single existing render path.
MUSIC_TRACK_KIND = "music"

#: Brand music_preference labels that mean "no soundtrack at all". A brand that
#: says "none" must not receive generated music just because a provider is
#: configured.
DISABLED_MUSIC_PREFERENCES: frozenset[str] = frozenset({"none", "off", "no", "mute", "silent"})

#: The label -> (mood, genre) vocabulary the built-in brand templates use. This
#: is the mapping that gives `brand_templates.music_preference` teeth.
PREFERENCE_STYLE: dict[str, dict[str, str]] = {
    "low_ambient": {"mood": "calm", "genre": "ambient minimal",
                    "brief": "sparse ambient bed, very low energy, no melody in the way of the voice"},
    "upbeat": {"mood": "energetic", "genre": "upbeat electronic",
               "brief": "driving upbeat groove with a clear percussive pulse"},
    "trend_audio": {"mood": "energetic", "genre": "social trend audio",
                    "brief": "current short-form social audio feel, hook-forward"},
    "cinematic": {"mood": "reflective", "genre": "cinematic underscore",
                  "brief": "cinematic underscore building gently, no sudden hits"},
    "urgent_bed": {"mood": "urgent", "genre": "tension bed",
                   "brief": "tight tense bed that lifts but never masks speech"},
    "lofi": {"mood": "relaxed", "genre": "lofi chill",
             "brief": "warm lo-fi chill texture, soft and unobtrusive"},
}


class MusicProviderError(RuntimeError):
    """Provider-independent music failure. Never carries a credential."""


class MusicUnavailable(MusicProviderError):
    """No provider is configured, or its prerequisites are missing."""


class MusicNotConfigured(MusicUnavailable):
    """A provider was selected but has no usable credential/dependency."""


class MusicPlanRequired(MusicProviderError):
    """The brand or campaign explicitly asks for no soundtrack."""


@dataclass
class MusicRequest:
    """Editorial context for one soundtrack.

    ``duration_seconds`` is the video length the music must fit; ``video_asset_id``
    is the parent the derived audio points back to (derived-only lineage).
    """

    workspace_id: str
    duration_seconds: float
    #: Absolute path to the rendered video the music must fit. The provider
    #: reads only a PROXY of it; the master stays local.
    video_path: str = ""
    video_asset_id: str = ""
    video_title: str = ""
    script_excerpt: str = ""
    mood: str = ""
    genre: str = ""
    campaign_id: str = ""
    #: ``brand_templates.music_preference`` and/or ``BrandDNA.music_prefs``.
    brand_music_preference: str = ""
    brand_music_prefs: dict = field(default_factory=dict)
    brand_tone: str = ""
    keywords: list[str] = field(default_factory=list)

    def style(self) -> dict[str, str]:
        """Resolve the effective (mood, genre, brief) for this request.

        Precedence is explicit -- an explicit mood/genre on the request wins,
        then the brand preference label, then nothing (the provider decides).
        """
        label = str(self.brand_music_preference or "").strip().lower()
        mapped = PREFERENCE_STYLE.get(label, {})
        prefs = self.brand_music_prefs if isinstance(self.brand_music_prefs, dict) else {}
        mood = (self.mood or prefs.get("mood") or mapped.get("mood") or "").strip().lower()
        genre = (self.genre or prefs.get("genre") or mapped.get("genre") or "").strip().lower()
        brief = str(prefs.get("brief") or mapped.get("brief") or "").strip()
        return {"mood": mood, "genre": genre, "brief": brief,
                "preference": label}

    def disabled(self) -> bool:
        """True when the brand/campaign asked for NO soundtrack."""
        return str(self.brand_music_preference or "").strip().lower() in DISABLED_MUSIC_PREFERENCES


@dataclass
class MusicResult:
    """A verified soundtrack plus its honest provenance.

    ``state`` is the ``SubmissionState`` the paid pipeline reached. A result is
    only produced for ``SUCCEEDED``; anything else is a warning, not a track.
    """

    provider: str
    asset_id: str = ""
    storage_key: str = ""
    path: str = ""
    duration_seconds: float = 0.0
    prompt: str = ""
    state: SubmissionState = SubmissionState.PREPARED
    remote_id: str = ""
    file_size: int = 0
    audio_codec: str = ""
    sample_rate: int = 0
    channels: int = 0
    checksum: str = ""
    warnings: list[str] = field(default_factory=list)
    provenance: dict = field(default_factory=dict)

    @property
    def usable(self) -> bool:
        return bool(self.path) and self.state == SubmissionState.SUCCEEDED


def music_prompt(request: MusicRequest) -> str:
    """The provider-facing prompt, assembled from real editorial inputs."""
    style = request.style()
    parts = [
        f"Background music for a {request.duration_seconds:.0f}-second "
        f"{'vertical short' if request.duration_seconds <= 60 else 'video'}."
    ]
    if request.video_title:
        parts.append(f'Topic: "{request.video_title[:160]}".')
    if style["mood"]:
        parts.append(f"Mood: {style['mood']}.")
    if style["genre"]:
        parts.append(f"Genre: {style['genre']}.")
    if style["brief"]:
        parts.append(style["brief"])
    if request.brand_tone:
        parts.append(f"Brand tone: {request.brand_tone}.")
    if request.keywords:
        parts.append("Keywords: " + ", ".join(str(k)[:40] for k in request.keywords[:6]) + ".")
    parts.append("Instrumental only: no vocals, no lyrics, leave space for narration.")
    return " ".join(parts)


#: Backwards/forwards-friendly alias -- the brief calls this the prompt builder.
build_music_prompt = music_prompt


def music_timeline_clip(asset_id: str, duration_seconds: float, *,
                        volume: float = 0.18,
                        clip_id: str = "music_0",
                        name: str = "AI music bed") -> dict:
    """A clip for the EXISTING ``music`` track kind.

    The render engine already delays + mixes ``voice``/``music``/``sfx`` clips
    over silence (``timeline_render.py``), so a soundtrack needs no new render
    code: it is a clip on the ``music`` track whose ``source.asset_id`` points at
    the generated ``MediaAsset``.
    """
    return {
        "id": clip_id,
        "name": name[:80],
        "start": 0.0,
        "duration": max(0.0, float(duration_seconds or 0.0)),
        "source": {"asset_id": asset_id},
        # Music sits UNDER narration: a bed at full volume is the single most
        # common reason a rendered video sounds bad.
        "volume": max(0.0, min(float(volume), 1.0)),
    }


class MusicIntelligenceProvider(ABC):
    """One AI music backend.

    Implementations must: analyse a PROXY (never the HD master), publish the
    artifact only after a full ffmpeg decode, and classify every submit outcome
    through ``services/paid_jobs.py``.
    """

    key: str = "base"
    is_mock: bool = False

    @abstractmethod
    def available(self) -> bool:
        """True when credentials + binaries are present and usable."""

    @abstractmethod
    def generate(self, request: MusicRequest) -> MusicResult:
        """Produce one verified soundtrack, or raise a ``Music*``/``Paid*`` error."""

    def health(self) -> dict:
        return {"provider": self.key, "available": False, "is_mock": self.is_mock}

    def estimate_cost(self, request: MusicRequest) -> float:
        """Honest per-generation estimate in USD; flagged as an estimate upstream."""
        return 0.0


def music_or_none(provider: MusicIntelligenceProvider | None,
                  request: MusicRequest) -> MusicResult | None:
    """Graceful degradation: music is optional, the video is not.

    Mirrors the donor's ``task.py`` behaviour -- a soundtrack failure appends a
    warning and rendering continues without a bed. Only an explicitly
    *requested* no-music brand (``MusicPlanRequired``) is not a warning, and a
    ``PaidSubmissionUnconfirmed`` is surfaced loudly because it may have been
    billed and must not be silently retried.
    """
    if provider is None:
        logger.info("music: no provider configured; rendering without a soundtrack")
        return None
    if request.disabled():
        logger.info("music: brand preference disables music; skipping generation")
        return None
    try:
        result = provider.generate(request)
    except MusicPlanRequired as exc:
        logger.info("music: plan declines a soundtrack (%s)", exc)
        return None
    except PaidSubmissionUnconfirmed as exc:
        # Loud, because the money may already be spent and a human may need to
        # reconcile it -- but still NOT a render failure.
        logger.error("music: paid submission UNCONFIRMED (%s); "
                     "do not resubmit automatically", exc)
        return None
    except PaidArtifactUndownloadable as exc:
        logger.warning("music: paid artifact could not be downloaded (%s); "
                       "rendering without a soundtrack", exc)
        return None
    except (PaidJobRejected, MusicProviderError) as exc:
        logger.warning("music: generation failed (%s); "
                       "rendering without a soundtrack", exc)
        return None
    except Exception as exc:  # noqa: BLE001 - a soundtrack must never fail a render
        logger.warning("music: unexpected failure (%s: %s); rendering without a soundtrack",
                       type(exc).__name__, exc)
        return None
    if not result.usable:
        logger.warning("music: provider returned no usable track (%s)", provider.key)
        return None
    return result


__all__ = [
    "DISABLED_MUSIC_PREFERENCES",
    "MUSIC_TRACK_KIND",
    "PREFERENCE_STYLE",
    "MusicIntelligenceProvider",
    "MusicNotConfigured",
    "MusicPlanRequired",
    "MusicProviderError",
    "MusicRequest",
    "MusicResult",
    "MusicUnavailable",
    "build_music_prompt",
    "music_or_none",
    "music_prompt",
    "music_timeline_clip",
]
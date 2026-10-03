"""Canonical provider-maturity registry (Work 15.6 §4).

One table, one vocabulary, zero side effects. This module answers exactly one
question honestly: **how far has each provider actually been taken?** It exists
because the failure mode it prevents is not hypothetical — an adapter that
merely *imports* gets talked about as if it were production-ready, and nobody
notices until a render fails or a vendor's terms turn out to forbid the use.

The design rule everything else follows from:

    **A status is recorded by a human after evidence. It is never derived from
    the module importing.**

So this is *data + functions*, not an autodetector. There is deliberately no
``importlib`` anywhere in this file, no ``hasattr`` probe of a provider class,
and no "looks configured ⇒ healthy" shortcut. A provider is recorded
``IMPLEMENTED`` because someone implemented it, ``CONTRACT_TESTED`` because a
named test exercises it offline, and ``LIVE_VERIFIED`` only when a real call
was made against the real service on a real credential. Right now that last
one has happened for *nothing* in this repository, and the table says so.

Two vocabularies, deliberately kept apart:

* :data:`STATES` — the eight maturity states below. Used for the
  ``implementation_status`` / ``contract_status`` / ``live_status`` /
  ``commercial_status`` axes. Nothing outside this set may be stored.
* The licence ledger's own classification (``BLOCKED_LICENSE``,
  ``ARCHITECTURE_REJECTED``, ``NOT_NEEDED``, …) lives in
  ``docs/oss/MONEYPRINTERTURBO_INTEGRATION.md``. "We chose not to use this" is
  an architecture decision and is *not* a provider state — see
  :func:`production_blockers`, which reports commercial limits without
  pretending they are the same thing as "the adapter is missing".

The four status axes are independent on purpose. Fish Audio's free tier is
``BLOCKED_COMMERCIAL_TERMS`` *and* ``UNAVAILABLE`` in YMONEY; collapsing those
into one verdict is how a workspace ends up paying for something that was never
built.

Laziness is load-bearing and is asserted by the test suite: importing this
module must not open a database session, read config, or touch the network. The
only module-scope imports are from the standard library, matching the pattern
``app.engine.intelligence.llm_registry`` established. Credential *resolution*
happens in :func:`resolve_credential_status`, at call time, and returns a state
word — never a value, a length, or a digest.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import UTC, datetime

# ---------------------------------------------------------------------------
# vocabulary
# ---------------------------------------------------------------------------

#: The closed set of maturity states. A state that is not here cannot be
#: stored, and that is the point: "PRODUCTION_READY" is not available as a
#: label, because it is a *conclusion* (:func:`production_ready`) that depends
#: on four axes agreeing, not a fact about one provider.
IMPLEMENTED = "IMPLEMENTED"
CONTRACT_TESTED = "CONTRACT_TESTED"
LIVE_VERIFIED = "LIVE_VERIFIED"
UNVERIFIED = "UNVERIFIED"
UNAVAILABLE = "UNAVAILABLE"
BLOCKED_LICENSE = "BLOCKED_LICENSE"
BLOCKED_COMMERCIAL_TERMS = "BLOCKED_COMMERCIAL_TERMS"
EXTERNAL_LIMITATION = "EXTERNAL_LIMITATION"

STATES: frozenset[str] = frozenset({
    IMPLEMENTED,
    CONTRACT_TESTED,
    LIVE_VERIFIED,
    UNVERIFIED,
    UNAVAILABLE,
    BLOCKED_LICENSE,
    BLOCKED_COMMERCIAL_TERMS,
    EXTERNAL_LIMITATION,
})

#: Which of the eight each axis may hold. ``commercial_status`` cannot be
#: ``IMPLEMENTED``: "we wrote the adapter" says nothing about a vendor's terms.
#: ``live_status`` cannot be ``BLOCKED_LICENSE``: those are different
#: questions, and forcing them into one column is the overloading this module
#: exists to stop.
IMPLEMENTATION_STATES: frozenset[str] = STATES
CONTRACT_STATES: frozenset[str] = frozenset(
    {CONTRACT_TESTED, UNVERIFIED, UNAVAILABLE}
)
LIVE_STATES: frozenset[str] = frozenset({LIVE_VERIFIED, UNVERIFIED, UNAVAILABLE})
COMMERCIAL_STATES: frozenset[str] = frozenset({
    UNVERIFIED,
    UNAVAILABLE,
    BLOCKED_LICENSE,
    BLOCKED_COMMERCIAL_TERMS,
    EXTERNAL_LIMITATION,
})

#: Capability axes. ``avatar`` is listed alongside the five media/AI
#: capabilities because Wav2Lip is the loudest licence case in the repo and it
#: would be dishonest to file it under "video".
CAPABILITIES: tuple[str, ...] = ("llm", "tts", "music", "video", "image", "avatar")

#: ``credential_status`` on a *record* is a requirement, not an observation.
#: Whether a credential is actually present is per-workspace and resolved at
#: call time by :func:`resolve_credential_status`.
CREDENTIAL_NOT_REQUIRED = "NOT_REQUIRED"
CREDENTIAL_REQUIRED = "REQUIRED"
CREDENTIAL_STATES: frozenset[str] = frozenset({CREDENTIAL_NOT_REQUIRED, CREDENTIAL_REQUIRED})

#: Resolved credential states (per workspace, at call time).
CREDENTIAL_CONFIGURED = "CONFIGURED"
CREDENTIAL_NOT_CONFIGURED = "NOT_CONFIGURED"
CREDENTIAL_UNRESOLVED = "UNRESOLVED"
RESOLVED_CREDENTIAL_STATES: frozenset[str] = frozenset({
    CREDENTIAL_NOT_REQUIRED,
    CREDENTIAL_CONFIGURED,
    CREDENTIAL_NOT_CONFIGURED,
    CREDENTIAL_UNRESOLVED,
})

#: ``health`` is asserted by nobody in this table. It is ``UNKNOWN`` until an
#: explicit probe reports otherwise, because a registry that guessed health
#: would be indistinguishable from one that measured it.
HEALTH_UNKNOWN = "UNKNOWN"
HEALTH_OK = "OK"
HEALTH_DEGRADED = "DEGRADED"
HEALTH_DOWN = "DOWN"
HEALTH_STATES: frozenset[str] = frozenset(
    {HEALTH_UNKNOWN, HEALTH_OK, HEALTH_DEGRADED, HEALTH_DOWN}
)

#: When this table was last read against the tree. Deliberately separate from
#: ``last_verified_at``, which is per-provider and means "a real call was
#: made". Auditing the table is not verifying a provider.
REGISTRY_REVIEWED_AT = "2026-10-01"


# ---------------------------------------------------------------------------
# the record
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ProviderMaturity:
    """One provider on one axis. Data only — no behaviour, no probing.

    ``last_verified_at`` is empty for every entry in this repository, and that
    is the honest value: no provider here has been exercised against its real
    service with a real credential. :data:`REGISTRY_REVIEWED_AT` records the
    audit of *this table*, which is a different claim.
    """

    provider: str
    capability: str
    implementation_status: str
    contract_status: str
    live_status: str
    commercial_status: str
    credential_status: str
    health: str = HEALTH_UNKNOWN
    last_verified_at: str = ""
    #: Credential key *names* in ``services.provider_settings.REGISTRY`` to look
    #: up. Names, never values.
    credential_keys: tuple[str, ...] = ()
    #: A simulation that must never be mistaken for a real backend. Mock TTS,
    #: the mock video engine, and the mock image provider all set this.
    simulation_only: bool = False
    #: The evidence and the gaps. "No reason recorded" is itself a defect, so
    #: the table requires a note on every row.
    notes: str = ""
    #: Where the implementation and its contract tests actually live.
    evidence: tuple[str, ...] = ()
    #: Known surfaces this record does NOT cover. An empty tuple means the
    #: author claimed full coverage, which is its own kind of claim.
    gaps: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.provider or self.provider != self.provider.strip():
            raise ValueError(f"provider must be a trimmed non-empty id: {self.provider!r}")
        if self.capability not in CAPABILITIES:
            raise ValueError(f"unknown capability {self.capability!r}")
        for field_name, allowed in (
            ("implementation_status", IMPLEMENTATION_STATES),
            ("contract_status", CONTRACT_STATES),
            ("live_status", LIVE_STATES),
            ("commercial_status", COMMERCIAL_STATES),
            ("credential_status", CREDENTIAL_STATES),
            ("health", HEALTH_STATES),
        ):
            value = getattr(self, field_name)
            if value not in allowed:
                raise ValueError(f"{self.provider}: {field_name}={value!r} not in {sorted(allowed)}")
        if self.implementation_status != IMPLEMENTED and self.evidence:
            raise ValueError(
                f"{self.provider}: only an implemented provider may cite evidence"
            )
        if not self.notes.strip():
            raise ValueError(f"{self.provider}: a maturity record must carry a reason")

    def to_dict(self) -> dict:
        """A JSON-ready view. Contains no secret, length, or digest."""
        return {
            "provider": self.provider,
            "capability": self.capability,
            "implementation_status": self.implementation_status,
            "contract_status": self.contract_status,
            "live_status": self.live_status,
            "commercial_status": self.commercial_status,
            "credential_status": self.credential_status,
            "health": self.health,
            "last_verified_at": self.last_verified_at,
            "credential_keys": list(self.credential_keys),
            "simulation_only": self.simulation_only,
            "notes": self.notes,
            "evidence": list(self.evidence),
            "gaps": list(self.gaps),
            "production_ready": production_ready(self),
        }


def validated_records(records: tuple[ProviderMaturity, ...]) -> tuple[ProviderMaturity, ...]:
    """Reject a table with duplicate ``(provider, capability)`` rows.

    A duplicate is not a cosmetic problem: two rows for one provider is how a
    registry ends up answering differently depending on which one the lookup
    happened to hit.
    """
    seen = {(r.provider, r.capability) for r in records}
    if len(seen) != len(records):
        raise RuntimeError("duplicate (provider, capability) in maturity registry")
    return records


_validated = validated_records

_EVIDENCE_FILE_SUFFIXES = (".py", ".md", ".toml")


def evidence_paths(record: ProviderMaturity) -> tuple[str, ...]:
    """Repository-relative file paths this record cites as evidence.

    A ``path:line`` reference is split on the colon; a bare note is not a path
    and is skipped. Reading the filesystem here would couple the registry to a
    working directory, so the existence check lives in :func:`broken_evidence`
    and is driven by the test suite instead.
    """
    out: list[str] = []
    for ref in record.evidence:
        path = ref.split(":", 1)[0]
        if path.endswith(_EVIDENCE_FILE_SUFFIXES) and path.startswith("backend/"):
            out.append(path)
    return tuple(out)


def broken_evidence(repo_root, records: tuple[ProviderMaturity, ...] | None = None
                    ) -> list[str]:
    """Evidence paths that do not exist. Empty means the table is honest.

    A row citing a file nobody wrote is the same defect as the ledger line that
    claimed ``services/path_safety.py`` was copied when it never was: it reads
    as an attestation and is not one.
    """
    from pathlib import Path

    root = Path(repo_root)
    rows = _all_records() if records is None else records
    broken: list[str] = []
    for record in rows:
        for rel in evidence_paths(record):
            if not (root / rel).is_file():
                broken.append(f"{record.provider}: {rel}")
    return broken


# ---------------------------------------------------------------------------
# the table
# ---------------------------------------------------------------------------

# Every LLM vendor shares ONE adapter (``app.providers.llm.complete``, an
# OpenAI-compatible chat-completions client), so the implementation and contract
# states are genuinely identical across the row set. What differs is the
# commercial note, and that is the part worth recording.
_LLM_CREDENTIALS = ("llm.api_key", "llm.base_url", "llm.model")

_LLM_NOTES: dict[str, str] = {
    "openai": "Direct vendor. No restriction identified in this audit; YMONEY has not verified current terms.",
    "anthropic": "OpenAI-compatible endpoint only. Native wire format differs.",
    "gemini": "OpenAI-compatible endpoint; response_format support unverified.",
    "deepseek": "Reasoning models return a <think> scratchpad, stripped at providers/llm.py.",
    "moonshot": "Two published regional endpoints; neither is a default.",
    "groq": "Free tier carries rate limits and no SLA.",
    "mistral": "No restriction identified in this audit.",
    "xai": "No restriction identified in this audit.",
    "openrouter": "Aggregator: one key, many underlying vendors. Their terms apply transitively and were not audited here.",
    "together": "No restriction identified in this audit.",
    "fireworks": "No restriction identified in this audit.",
    "siliconflow": "LLM vendor only. Its TTS product is a separate, unimplemented provider (see tts_qualification).",
    "dashscope": "Chinese and international endpoints differ.",
    "azure_openai": "Deployment-scoped URL plus api_version; no universal endpoint.",
    "ollama": "Self-hosted runtime. The MODEL's licence is the operator's problem, not the runtime's.",
    "lm_studio": "Self-hosted runtime. The MODEL's licence is the operator's problem, not the runtime's.",
    "openai_compatible": "Self-hosted gateway. YMONEY cannot know whose models sit behind it.",
}

_LLM_EXTERNAL = frozenset({"groq"})
_LLM_REVIEWED = frozenset({"openai", "moonshot"})


def _llm_records() -> tuple[ProviderMaturity, ...]:
    """Build the LLM rows from the Work 15.5 vendor ids.

    The ids are literals here rather than imported from
    ``app.engine.intelligence.llm_registry`` on purpose: a module-scope
    ``app.`` import would break the laziness this file guarantees. The test
    suite asserts the two lists still agree, so drift fails loudly.
    """
    return tuple(
        ProviderMaturity(
            provider=provider,
            capability="llm",
            implementation_status=IMPLEMENTED,
            contract_status=CONTRACT_TESTED,
            live_status=UNVERIFIED,
            commercial_status=EXTERNAL_LIMITATION
            if provider in _LLM_EXTERNAL
            else UNVERIFIED,
            credential_status=CREDENTIAL_REQUIRED,
            credential_keys=_LLM_CREDENTIALS,
            notes=(
                f"{_LLM_NOTES[provider]} Live status is UNVERIFIED because no "
                "LLM vendor call in this repository is made against a real key."
            ),
            evidence=(
                "backend/app/providers/llm.py:47",
                "backend/app/engine/intelligence/llm_registry.py:73",
                "backend/tests/test_work15_5_llm_registry.py:40",
            ),
            gaps=("no live call has been recorded for this vendor",),
        )
        for provider in sorted(_LLM_NOTES)
    )





#: TTS rows live in ``app.providers.tts_qualification`` because the TTS
#: qualification needs a contract probe and the donor's per-provider detail,
#: and putting that here would make this file a provider implementation. The
#: dependency points tts_qualification -> maturity (for the record type), so
#: this module cannot import it at module scope without creating a cycle.
#: :func:`list_maturity` therefore assembles the full table on first call, and
#: the laziness test asserts that merely importing this module pulls in neither
#: the credential resolver nor the TTS layer.
_TTS_CAPABILITY = "tts"
_ALL_RECORDS: tuple[ProviderMaturity, ...] | None = None


def _all_records() -> tuple[ProviderMaturity, ...]:
    global _ALL_RECORDS
    if _ALL_RECORDS is None:
        from app.providers.tts_qualification import tts_records

        _ALL_RECORDS = _validated((*PROVIDER_MATURITY, *tts_records()))
    return _ALL_RECORDS


_STATIC_RECORDS: tuple[ProviderMaturity, ...] = (
    # -- music -------------------------------------------------------------
    ProviderMaturity(
        provider="elevenlabs_music",
        capability="music",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNVERIFIED,
        commercial_status=UNVERIFIED,
        credential_status=CREDENTIAL_REQUIRED,
        credential_keys=("tts.elevenlabs_api_key",),
        notes=("Billable, so it goes through services/paid_jobs.py: an "
               "unconfirmed submit is never auto-retried. Requires an "
               "ElevenLabs paid plan; a subscription preflight runs first. "
               "Live status UNVERIFIED: the live test needs a real key."),
        evidence=(
            "backend/app/providers/music/elevenlabs_music.py:125",
            "backend/app/providers/music/base.py:223",
            "backend/tests/test_work15_5_music.py:1",
            "backend/tests/test_work15_6_music.py:1",
        ),
        gaps=("no live generation recorded (needs YMONEY_ELEVENLABS_API_KEY)",),
    ),
    # -- video -------------------------------------------------------------
    ProviderMaturity(
        provider="moneyprinterturbo",
        capability="video",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNVERIFIED,
        commercial_status=UNVERIFIED,
        credential_status=CREDENTIAL_REQUIRED,
        credential_keys=("engine.base_url", "engine.timeout_seconds"),
        notes=("HTTP adapter over the donor engine. The adapter is MIT; the "
               "donor's bundled songs/fonts/images are NOT reused and are "
               "BLOCKED_LICENSE in the provenance ledger. Live status "
               "UNVERIFIED: needs a running donor instance."),
        evidence=(
            "backend/app/providers/video_engine/mpt.py:52",
            "backend/app/providers/video_engine/factory.py:62",
            "backend/tests/test_mpt_adapter.py:1",
        ),
        gaps=("no render has been recorded against a live donor engine",),
    ),
    ProviderMaturity(
        provider="ffmpeg_avatar",
        capability="video",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNVERIFIED,
        commercial_status=UNVERIFIED,
        credential_status=CREDENTIAL_NOT_REQUIRED,
        notes=("Local ffmpeg render. Needs ffmpeg on PATH; no credential. "
               "Live status UNVERIFIED in the sense that no render is recorded "
               "in the maturity ledger, although engine-level render tests exist."),
        evidence=(
            "backend/app/providers/video_engine/ffmpeg_avatar.py:327",
            "backend/tests/test_ffmpeg_engine_v2.py:1",
        ),
        gaps=("render requires ffmpeg, which CI may not provide",),
    ),
    ProviderMaturity(
        provider="mock_video_engine",
        capability="video",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNAVAILABLE,
        commercial_status=UNAVAILABLE,
        credential_status=CREDENTIAL_NOT_REQUIRED,
        simulation_only=True,
        notes=("SIMULATION. Refused in production unless "
               "allow_mock_in_production is explicitly set. It can never be "
               "production-ready: it fabricates a video file."),
        evidence=(
            "backend/app/providers/video_engine/mock.py:31",
            "backend/app/providers/video_engine/factory.py:66",
        ),
    ),
    # -- image -------------------------------------------------------------
    ProviderMaturity(
        provider="pollinations",
        capability="image",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNVERIFIED,
        commercial_status=EXTERNAL_LIMITATION,
        credential_status=CREDENTIAL_NOT_REQUIRED,
        notes=("Keyless public endpoint with no SLA and an unpublished "
               "availability guarantee. That is an external limitation, not a "
               "licence problem. Live status UNVERIFIED."),
        evidence=(
            "backend/app/providers/images.py:50",
            "backend/tests/test_ai_covers.py:1",
        ),
        gaps=("no live generation recorded",),
    ),
    ProviderMaturity(
        provider="openai_compat_image",
        capability="image",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNVERIFIED,
        commercial_status=UNVERIFIED,
        credential_status=CREDENTIAL_REQUIRED,
        credential_keys=("image.openai_api_key", "image.openai_base_url", "image.openai_model"),
        notes=("Any OpenAI-compatible /images endpoint. YMONEY cannot know "
               "whose models sit behind a self-hosted gateway."),
        evidence=("backend/app/providers/images.py:123", "backend/tests/test_covers.py:1"),
        gaps=("no live generation recorded",),
    ),
    ProviderMaturity(
        provider="xkiro_image",
        capability="image",
        implementation_status=IMPLEMENTED,
        contract_status=UNVERIFIED,
        live_status=UNVERIFIED,
        commercial_status=UNVERIFIED,
        credential_status=CREDENTIAL_REQUIRED,
        credential_keys=("image.openai_api_key", "image.openai_base_url", "image.openai_model"),
        notes=("Same wire format as openai_compat_image but with no dedicated "
               "contract test, so it is honestly UNVERIFIED rather than "
               "inheriting a sibling's status."),
        evidence=("backend/app/providers/images.py:190",),
        gaps=("no contract test exists for this adapter",),
    ),
    ProviderMaturity(
        provider="pexels_image",
        capability="image",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNVERIFIED,
        commercial_status=EXTERNAL_LIMITATION,
        credential_status=CREDENTIAL_REQUIRED,
        credential_keys=("pexels.api_key",),
        notes=("Pexels API licence requires attribution and forbids resale of "
               "assets standalone; YMONEY uses them inside rendered video. "
               "That review was not re-done in this audit, so the finding is "
               "recorded, not cleared."),
        evidence=(
            "backend/app/providers/images_pexels.py:1",
            "backend/tests/test_pexels_images.py:1",
        ),
        gaps=("no live fetch recorded",),
    ),
    ProviderMaturity(
        provider="mock_image",
        capability="image",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNAVAILABLE,
        commercial_status=UNAVAILABLE,
        credential_status=CREDENTIAL_NOT_REQUIRED,
        simulation_only=True,
        notes="SIMULATION. Deterministic gradient PNGs. Never production-ready.",
        evidence=("backend/app/providers/images.py:349",),
    ),
    # -- avatar ------------------------------------------------------------
    ProviderMaturity(
        provider="avatar_server",
        capability="avatar",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNVERIFIED,
        commercial_status=UNVERIFIED,
        credential_status=CREDENTIAL_REQUIRED,
        credential_keys=("avatar.base_url",),
        notes="HTTP multipart renderer (image + audio -> mp4). Needs a live server.",
        evidence=("backend/app/providers/avatar.py:229", "backend/tests/test_avatar.py:1"),
        gaps=("no live render recorded",),
    ),
    ProviderMaturity(
        provider="sadtalker",
        capability="avatar",
        implementation_status=IMPLEMENTED,
        contract_status=UNVERIFIED,
        live_status=UNVERIFIED,
        commercial_status=UNVERIFIED,
        credential_status=CREDENTIAL_REQUIRED,
        credential_keys=("avatar.sadtalker_dir",),
        notes=("Runs a local SadTalker checkout. The ADAPTER is code and is "
               "MIT-clean; the CHECKPOINT weights carry their own terms which "
               "this audit did not verify, hence UNVERIFIED rather than cleared."),
        evidence=("backend/app/providers/avatar.py:275",),
        gaps=(
            "no contract test for this lane",
            "checkpoint weight licence not audited",
        ),
    ),
    ProviderMaturity(
        provider="wav2lip",
        capability="avatar",
        implementation_status=IMPLEMENTED,
        contract_status=UNVERIFIED,
        live_status=UNVERIFIED,
        commercial_status=BLOCKED_LICENSE,
        credential_status=CREDENTIAL_REQUIRED,
        credential_keys=("avatar.wavlip_dir",),
        notes=("BLOCKED_LICENSE: the Wav2Lip weights are released for "
               "non-commercial research use only. The provider refuses to run "
               "when settings.commercial_mode is set, and it must stay that "
               "way. The MIT licence of the surrounding code does not "
               "relicense the weights."),
        evidence=(
            "backend/app/providers/avatar.py:75",
            "backend/app/services/provider_settings.py:80",
        ),
        gaps=("no contract test for this lane",),
    ),
)

#: The part of the table this module owns outright: LLM, music, video, image,
#: avatar. The TTS rows are added by :func:`_all_records`.
PROVIDER_MATURITY: tuple[ProviderMaturity, ...] = _validated(
    (*_llm_records(), *_STATIC_RECORDS)
)


def _by_key() -> dict[tuple[str, str], ProviderMaturity]:
    return {(r.provider, r.capability): r for r in _all_records()}

#: The TTS candidates from the donor (MiniMax, Fish Audio, VoxCPM, SiliconFlow,
#: Gemini TTS, Azure Speech v2). Present as records so an operator asking
#: "why can't I pick Fish Audio?" gets an answer rather than silence.
UNIMPLEMENTED_DONOR_TTS: tuple[str, ...] = (
    "minimax",
    "fish_audio",
    "voxcpm",
    "siliconflow_tts",
    "gemini_tts",
    "azure_speech_v2",
)


# ---------------------------------------------------------------------------
# read API — pure data access
# ---------------------------------------------------------------------------


def list_maturity(capability: str = "") -> tuple[ProviderMaturity, ...]:
    """Every record, optionally filtered to one capability. No resolution."""
    rows = _all_records()
    if not capability:
        return rows
    return tuple(r for r in rows if r.capability == capability.strip().lower())


def get_maturity(provider: str, capability: str = "") -> ProviderMaturity | None:
    """Look a provider up. Unknown ids return ``None``, never raise."""
    key = (provider or "").strip().lower()
    table = _by_key()
    if capability:
        return table.get((key, capability.strip().lower()))
    for (name, _cap), record in table.items():
        if name == key:
            return record
    return None


def capabilities() -> tuple[str, ...]:
    return CAPABILITIES


def unimplemented_donor_tts() -> tuple[dict, ...]:
    """The donor TTS candidates with the reason each one is not here.

    This exists to stop the conversation "the matrix said MERGE, so it is
    merged". It did not say that; it said it could be done, and nobody did it.
    """
    from app.providers.tts_qualification import get_qualification

    out: list[dict] = []
    for name in UNIMPLEMENTED_DONOR_TTS:
        qual = get_qualification(name)
        if qual is None:
            continue
        record = get_maturity(qual.provider, "tts")
        out.append({
            "provider": qual.provider,
            "label": qual.label,
            "implementation_status": record.implementation_status if record else UNAVAILABLE,
            "reason": qual.notes,
            "donor_reference": qual.donor_reference,
            "would_require": list(qual.would_require),
        })
    return tuple(out)


def resolve_credential_status(
    record: ProviderMaturity, workspace_id: str | None = None
) -> str:
    """Resolve whether this workspace has the credential, as a STATE WORD.

    Returns ``NOT_REQUIRED`` when the provider needs none, ``CONFIGURED`` /
    ``NOT_CONFIGURED`` when the resolver answers, and ``UNRESOLVED`` when the
    resolver itself fails.

    It returns a word, never a value. Not the key, not its length, not a
    prefix, not a digest — the matrix's own ``asgi.py:32`` rule, applied to
    this module. A registry that leaked a fingerprint would be worse than one
    that leaked nothing, because the fingerprint is what makes brute-forcing
    cheap.
    """
    if record.credential_status == CREDENTIAL_NOT_REQUIRED:
        return CREDENTIAL_NOT_REQUIRED
    if not record.credential_keys:
        # A requirement with no key to look up is a defect in the record, not
        # a reason to claim the credential is fine.
        return CREDENTIAL_UNRESOLVED
    from app.services.provider_settings import REGISTRY, get_credential

    any_configured = False
    try:
        for key in record.credential_keys:
            if key not in REGISTRY:
                return CREDENTIAL_UNRESOLVED
            value, _source = get_credential(key, workspace_id)
            if value:
                any_configured = True
    except Exception:
        # Resolution failing must not read as "configured". Reporting the
        # failure is the whole point of the UNRESOLVED state.
        return CREDENTIAL_UNRESOLVED
    return CREDENTIAL_CONFIGURED if any_configured else CREDENTIAL_NOT_CONFIGURED


def probe_health(record: ProviderMaturity) -> str:
    """Report health, or ``UNKNOWN``.

    Only the TTS layer exposes a ``health()`` probe, and even there a probe
    that fails or raises reports ``UNKNOWN`` rather than guessing. Nothing here
    infers health from configuration.
    """
    if record.capability != "tts" or record.implementation_status != IMPLEMENTED:
        return HEALTH_UNKNOWN
    try:
        from app.providers.tts import TTSError, get_tts_provider

        provider = get_tts_provider(record.provider)
    except TTSError:
        return HEALTH_DOWN
    except Exception:
        return HEALTH_UNKNOWN
    try:
        return HEALTH_OK if provider.health() else HEALTH_DOWN
    except Exception:
        return HEALTH_UNKNOWN


def production_blockers(record: ProviderMaturity) -> list[str]:
    """Every reason this provider is not production-ready, in plain words.

    ``production_ready`` is deliberately not a stored field: it is a conclusion
    over four axes, and storing it would let the axes drift apart from the
    conclusion nobody rechecked.
    """
    blockers: list[str] = []
    if record.simulation_only:
        blockers.append("simulation_only: this provider fabricates output")
    if record.implementation_status == UNAVAILABLE:
        blockers.append("implementation_status=UNAVAILABLE: there is no adapter")
    elif record.implementation_status != IMPLEMENTED:
        blockers.append(
            f"implementation_status={record.implementation_status}: not implemented")
    if record.contract_status != CONTRACT_TESTED:
        blockers.append(f"contract_status={record.contract_status}: no offline contract test")
    if record.live_status != LIVE_VERIFIED:
        blockers.append(f"live_status={record.live_status}: never exercised against the real service")
    if record.commercial_status in (BLOCKED_LICENSE, BLOCKED_COMMERCIAL_TERMS):
        blockers.append(f"commercial_status={record.commercial_status}: use is restricted")
    elif record.commercial_status == EXTERNAL_LIMITATION:
        blockers.append(
            f"commercial_status={record.commercial_status}: external limit (no SLA / ToS restriction)")
    if record.gaps:
        blockers.append(f"uncovered surfaces: {'; '.join(record.gaps)}")
    return blockers


def production_ready(record: ProviderMaturity) -> bool:
    """True only when no axis objects. Right now: nothing at all."""
    return not production_blockers(record)


def status_summary(records: tuple[ProviderMaturity, ...] | None = None) -> dict:
    """Counts per state per axis, plus what is actually usable in production.

    The ``production_ready`` list is expected to be empty for this repository.
    A summary that reported a long list of ready providers would itself be the
    bug this module exists to catch.
    """
    rows = _all_records() if records is None else records
    summary: dict = {
        "registry_reviewed_at": REGISTRY_REVIEWED_AT,
        "states": sorted(STATES),
        "capabilities": list(CAPABILITIES),
        "total": len(rows),
        "by_capability": {},
        "implementation_status": {},
        "contract_status": {},
        "live_status": {},
        "commercial_status": {},
        "production_ready": [],
        "not_production_ready": {},
    }
    for record in rows:
        for axis in ("implementation_status", "contract_status",
                     "live_status", "commercial_status"):
            summary[axis][getattr(record, axis)] = (
                summary[axis].get(getattr(record, axis), 0) + 1)
        summary["by_capability"][record.capability] = (
            summary["by_capability"].get(record.capability, 0) + 1)
        if production_ready(record):
            summary["production_ready"].append(
                {"provider": record.provider, "capability": record.capability})
        else:
            summary["not_production_ready"][record.provider] = production_blockers(record)
    return summary


def with_resolved_credentials(
    records: tuple[ProviderMaturity, ...], workspace_id: str | None = None
) -> list[dict]:
    """Records as dicts, each with its resolved credential state attached.

    The returned dicts gain one key, ``resolved_credential_status``. The stored
    ``credential_status`` keeps its requirement meaning, so a reader can always
    tell "needs a key" apart from "has a key".
    """
    out: list[dict] = []
    for record in records:
        payload = record.to_dict()
        payload["resolved_credential_status"] = resolve_credential_status(
            record, workspace_id)
        out.append(payload)
    return out


def utc_now_iso() -> str:
    """Current UTC timestamp. Kept here so no caller invents its own format."""
    return datetime.now(UTC).isoformat(timespec="seconds")


def stamp_verification(record: ProviderMaturity, when: str = "") -> ProviderMaturity:
    """Return a copy with ``last_verified_at`` set. The record stays frozen.

    Deliberately a copy: mutating the shared table in place would let one
    request's claim become every reader's fact.
    """
    return replace(record, last_verified_at=when or utc_now_iso())


__all__ = [
    "BLOCKED_COMMERCIAL_TERMS",
    "BLOCKED_LICENSE",
    "CAPABILITIES",
    "COMMERCIAL_STATES",
    "CONTRACT_STATES",
    "CONTRACT_TESTED",
    "CREDENTIAL_CONFIGURED",
    "CREDENTIAL_NOT_CONFIGURED",
    "CREDENTIAL_NOT_REQUIRED",
    "CREDENTIAL_REQUIRED",
    "CREDENTIAL_STATES",
    "CREDENTIAL_UNRESOLVED",
    "EXTERNAL_LIMITATION",
    "HEALTH_DEGRADED",
    "HEALTH_DOWN",
    "HEALTH_OK",
    "HEALTH_STATES",
    "HEALTH_UNKNOWN",
    "IMPLEMENTATION_STATES",
    "IMPLEMENTED",
    "LIVE_STATES",
    "LIVE_VERIFIED",
    "PROVIDER_MATURITY",
    "REGISTRY_REVIEWED_AT",
    "RESOLVED_CREDENTIAL_STATES",
    "STATES",
    "UNAVAILABLE",
    "UNIMPLEMENTED_DONOR_TTS",
    "UNVERIFIED",
    "ProviderMaturity",
    "capabilities",
    "broken_evidence",
    "evidence_paths",
    "get_maturity",
    "list_maturity",
    "production_blockers",
    "production_ready",
    "probe_health",
    "resolve_credential_status",
    "stamp_verification",
    "status_summary",
    "unimplemented_donor_tts",
    "utc_now_iso",
    "validated_records",
    "with_resolved_credentials",
]
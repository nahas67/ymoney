"""TTS adapter qualification (Work 15.6 §3).

This module answers a narrow, uncomfortable question: **which of the donor's
TTS adapters actually exist in YMONEY, and how far has each been taken?**

It answers it from the tree, not from a plan. ``docs/MPT_TECHNOLOGY_MATRIX.md``
classified MiniMax, Fish Audio, VoxCPM, SiliconFlow TTS, Gemini TTS and Azure
Speech v2 as ``MERGE — all fit BaseTTSProvider unchanged``. That was a
statement about *feasibility at inspection time*. It is routinely read
afterwards as *they were merged*. They were not: ``rg`` over ``backend/app``
finds no module, class, factory branch, or credential key for any of the six.
So they are recorded here as ``UNAVAILABLE`` with the reason, rather than being
implemented speculatively six times over on the strength of a one-line note.

What *is* implemented is qualified against :class:`BaseTTSProvider` by
:func:`contract_probe`, an offline check with no network: every provider must
return a real :class:`TTSResult`, raise :class:`TTSError` on empty input rather
than returning empty audio, label itself honestly through ``provider``/``name``,
and only the mock provider may claim ``is_mock``. That is the difference between
"the module imports" and "the adapter honours its contract".

The harvested behaviours are reused, not reinvented, and each is named so a
reader can check the claim:

* pause handling — ``app/services/pause_tags.py`` (``[pause 1.5]``), applied
  upstream of the provider by the narration path;
* PCM decode then a single encode — ``app/services/audio_concat.py``;
* real duration measurement — ``providers.tts.wav_duration_seconds`` for wav,
  ffprobe elsewhere;
* no-voice mode — ``pause_tags.NO_VOICE_NAME`` / ``assert_voice_mode_explicit``,
  so ``""`` never reads as silent narration;
* custom-audio path — ``app/engine/ugc/voice.py`` (``narrate_text`` /
  ``narrate_segments``), which bypasses the provider entirely.

Statuses reuse ``app.providers.maturity``'s closed vocabulary and are not
re-declared here. A state invented locally would defeat the point of having one
canonical registry.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.providers.maturity import (
    BLOCKED_COMMERCIAL_TERMS,
    BLOCKED_LICENSE,
    CONTRACT_TESTED,
    CREDENTIAL_NOT_REQUIRED,
    CREDENTIAL_REQUIRED,
    EXTERNAL_LIMITATION,
    IMPLEMENTED,
    LIVE_VERIFIED,
    UNAVAILABLE,
    UNVERIFIED,
    ProviderMaturity,
    validated_records,
)

#: The TTS capability this module owns rows for.
CAPABILITY = "tts"


@dataclass(frozen=True)
class TTSQualification:
    """One TTS adapter's qualification record.

    ``donor_reference`` is where the donor implemented it, when it implemented
    it at all. ``would_require`` is the honest price of merging it: naming that
    price is what stops "MERGE" from being read as "done".
    """

    provider: str
    label: str
    implementation_status: str
    contract_status: str
    live_status: str
    commercial_status: str
    notes: str
    credential_keys: tuple[str, ...] = ()
    simulation_only: bool = False
    #: ``class path in app.providers.tts`` when one exists, else "".
    adapter: str = ""
    donor_reference: str = ""
    evidence: tuple[str, ...] = ()
    gaps: tuple[str, ...] = ()
    #: What a future merge would actually have to do. Empty when N/A.
    would_require: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# the six donor candidates the matrix called MERGE
# ---------------------------------------------------------------------------
#
# Not one of these exists in ``backend/app``. ``backend/app/providers/tts.py``
# has exactly six provider classes — edge, kokoro, mock, chatterbox, qwen3,
# elevenlabs — and ``get_tts_provider`` raises ``TTSError`` for anything else.
# No ``ApiCredential`` key exists for any of the six either, so even a
# hand-patched branch would have nowhere to read a key from.

DONOR_CANDIDATES: tuple[TTSQualification, ...] = (
    TTSQualification(
        provider="minimax",
        label="MiniMax T2A v2",
        implementation_status=UNAVAILABLE,
        contract_status=UNAVAILABLE,
        live_status=UNAVAILABLE,
        commercial_status=UNVERIFIED,
        donor_reference="app/services/voice.py:1957 (minimax_tts)",
        notes=("NOT IMPLEMENTED. The donor's minimax_tts is a module-level "
               "function returning Union[SubMaker, None] that writes hex "
               "audio to a file path; it has no voices() and no health(). "
               "YMONEY has no class, no factory branch, and no credential key "
               "for it. Its vendor terms were not audited here."),
        credential_keys=(),
        would_require=(
            "a new BaseTTSProvider subclass with synthesize() + voices()",
            "a tts.minimax_api_key entry in provider_settings.REGISTRY",
            "a branch in get_tts_provider",
            "region inference (a China key must not reach the global host)",
        ),
    ),
    TTSQualification(
        provider="fish_audio",
        label="Fish Audio",
        implementation_status=UNAVAILABLE,
        contract_status=UNAVAILABLE,
        live_status=UNAVAILABLE,
        commercial_status=BLOCKED_COMMERCIAL_TERMS,
        donor_reference="app/services/voice.py:2297 (fish_audio_tts)",
        notes=("NOT IMPLEMENTED, and the free tier is BLOCKED_COMMERCIAL_TERMS: "
               "developer terms with a Fair Use Policy and no SLA "
               "(config.example.toml:580-584). Two independent reasons not to "
               "add it. The donor's function returns Union[SubMaker, None] and "
               "maps voice_volume to dB; no YMONEY adapter, class, or "
               "credential key exists."),
        credential_keys=(),
        would_require=(
            "a new BaseTTSProvider subclass",
            "a paid-plan agreement, or the terms review clears the free tier",
            "a tts.fish_api_key entry in provider_settings.REGISTRY",
        ),
    ),
    TTSQualification(
        provider="voxcpm",
        label="ModelBest VoxCPM",
        implementation_status=UNAVAILABLE,
        contract_status=UNAVAILABLE,
        live_status=UNAVAILABLE,
        commercial_status=UNVERIFIED,
        donor_reference="app/services/voice.py:2458 (voxcpm_tts)",
        notes=("NOT IMPLEMENTED. The donor streams base64 WAV chunks over SSE "
               "and decodes them through pydub, which is not a YMONEY "
               "dependency (pyproject.toml declares httpx and edge-tts only). "
               "No class, factory branch, or credential key exists."),
        credential_keys=(),
        would_require=(
            "an SSE + base64 decode path, or a server that returns mp3 directly",
            "removing the pydub dependency the donor relies on",
            "a tts.voxcpm_api_key entry in provider_settings.REGISTRY",
        ),
    ),
    TTSQualification(
        provider="siliconflow_tts",
        label="SiliconFlow TTS",
        implementation_status=UNAVAILABLE,
        contract_status=UNAVAILABLE,
        live_status=UNAVAILABLE,
        commercial_status=UNVERIFIED,
        donor_reference="app/services/voice.py:1345 (siliconflow_tts)",
        notes=("NOT IMPLEMENTED as TTS. SiliconFlow IS present in YMONEY as an "
               "LLM vendor (llm_registry.py:149) — that is a different "
               "capability and its maturity row must not be read as covering "
               "the TTS product. No TTS class, factory branch, or credential "
               "key exists."),
        credential_keys=(),
        would_require=(
            "a new BaseTTSProvider subclass (the LLM row proves nothing here)",
            "a tts.siliconflow_api_key entry in provider_settings.REGISTRY",
        ),
    ),
    TTSQualification(
        provider="gemini_tts",
        label="Google Gemini TTS",
        implementation_status=UNAVAILABLE,
        contract_status=UNAVAILABLE,
        live_status=UNAVAILABLE,
        commercial_status=UNVERIFIED,
        donor_reference="app/services/voice.py:1566 (gemini_tts)",
        notes=("NOT IMPLEMENTED. The donor imports google.genai and pydub; "
               "neither is installed in backend/.venv and neither is declared "
               "in pyproject.toml. Gemini appears in YMONEY only as an "
               "OpenAI-compatible LLM vendor. No TTS class, factory branch, or "
               "credential key exists."),
        credential_keys=(),
        would_require=(
            "the google-genai SDK, or an httpx reimplementation of the "
            "generateContent speech path",
            "a tts.gemini_api_key entry in provider_settings.REGISTRY",
        ),
    ),
    TTSQualification(
        provider="azure_speech_v2",
        label="Azure Speech v2",
        implementation_status=UNAVAILABLE,
        contract_status=UNAVAILABLE,
        live_status=UNAVAILABLE,
        commercial_status=UNVERIFIED,
        donor_reference="app/services/voice.py:1515 (speechsdk branch)",
        notes=("NOT IMPLEMENTED. The donor uses azure-cognitiveservices-speech "
               "(pinned ==1.41.1 in its requirements.txt), which is not "
               "installed and is deliberately not vendored. The work order's "
               "matrix classifies the MoviePy/SpeechSDK/pydub SDK row "
               "BLOCKED_LICENSE in favour of httpx + ffprobe + ffmpeg. No "
               "class, factory branch, or credential key exists. Note this is "
               "Azure *Speech*, which is distinct from the azure_openai LLM "
               "row and needs its own key plus region."),
        credential_keys=(),
        would_require=(
            "azure-cognitiveservices-speech, or a direct REST implementation "
            "of the Speech SSML endpoint",
            "a tts.azure_speech_key + tts.azure_speech_region pair in "
            "provider_settings.REGISTRY",
        ),
    ),
)


# ---------------------------------------------------------------------------
# what actually exists, and how far it has been taken
# ---------------------------------------------------------------------------

#: Named ``IMPLEMENTED_ADAPTERS``, not ``IMPLEMENTED``: the maturity state
#: ``IMPLEMENTED`` is imported from ``app.providers.maturity`` and is a string.
#: Shadowing it with this table silently broke every ``== IMPLEMENTED``
#: comparison below (the state check compared a string to a tuple and always
#: took the ``UNAVAILABLE`` branch). The test that caught it is
#: ``test_qualification_labels_are_derived_from_state_fields_not_prose``.
IMPLEMENTED_ADAPTERS: tuple[TTSQualification, ...] = (
    TTSQualification(
        provider="edge",
        label="Microsoft Edge neural voices",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNVERIFIED,
        commercial_status=UNVERIFIED,
        adapter="EdgeTTSProvider",
        notes=("Implemented against the public edge-tts protocol; no key, no "
               "local model, the default when nothing is configured. "
               "Contract-tested offline with edge_tts stubbed at the transport "
               "boundary. Live status is UNVERIFIED: this repository records no "
               "successful call against Microsoft's endpoint, and edge-tts has "
               "no published SLA, which is why it is not claimed as more than "
               "it is."),
        credential_keys=(),
        evidence=(
            "backend/app/providers/tts.py:79",
            "backend/app/providers/tts.py:117",
            "backend/tests/test_work15_6_providers.py",
        ),
        gaps=(
            "no live call recorded",
            "the donor's stream-deadline guard (daemon thread + queue) is not "
            "ported, so a hung communicate.stream() has no deadline here",
        ),
    ),
    TTSQualification(
        provider="kokoro",
        label="Kokoro-82M (self-hosted OpenAI-compatible server)",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNVERIFIED,
        commercial_status=UNVERIFIED,
        adapter="KokoroTTSProvider",
        credential_keys=("tts.kokoro_base_url", "tts.kokoro_api_key"),
        notes=("Implemented against any OpenAI-compatible /audio/speech server "
               "hosting Kokoro. CONFIG_GATED: it cannot synthesise without a "
               "server URL, and the provider says so by raising TTSError "
               "rather than falling back. weights licence is stated as "
               "Apache-2 in the adapter docstring; that was not re-verified "
               "against the artefact here."),
        evidence=(
            "backend/app/providers/tts.py:158",
            "backend/app/providers/tts.py:189",
            "backend/tests/test_work15_6_providers.py",
        ),
        gaps=(
            "no live call recorded (needs a local Kokoro server)",
            "the weight licence is asserted in a docstring, not audited",
        ),
    ),
    TTSQualification(
        provider="chatterbox",
        label="Chatterbox-Turbo (native package or compat server)",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNVERIFIED,
        commercial_status=UNVERIFIED,
        adapter="ChatterboxTTSProvider",
        credential_keys=("tts.chatterbox_base_url",),
        notes=("Implemented on two backends: the native chatterbox-tts package "
               "and an OpenAI-compatible server. Clone references require the "
               "native package and are refused on the server path rather than "
               "silently ignored. CONFIG_GATED on both the package and the "
               "server URL; neither is installed here, so the native path is "
               "UNVERIFIED by construction."),
        evidence=(
            "backend/app/providers/tts.py:324",
            "backend/app/providers/tts.py:350",
            "backend/tests/test_work15_6_providers.py",
            "backend/tests/test_voices_e2.py:47",
        ),
        gaps=(
            "the native synthesis path needs chatterbox-tts + torch, neither "
            "installed, so only the server path is exercised",
            "no live call recorded",
        ),
    ),
    TTSQualification(
        provider="qwen3",
        label="Qwen3-TTS (vLLM-Omni OpenAI-compatible server)",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNVERIFIED,
        commercial_status=UNVERIFIED,
        adapter="QwenTTSProvider",
        credential_keys=("tts.qwen_base_url", "tts.qwen_instruct", "tts.qwen_api_key"),
        notes=("Implemented as a server-only adapter with an optional "
               "natural-language delivery direction. CONFIG_GATED: no base URL "
               "means TTSError, not a silent default voice."),
        evidence=(
            "backend/app/providers/tts.py:442",
            "backend/app/providers/tts.py:462",
            "backend/tests/test_work15_6_providers.py",
            "backend/tests/test_voices_e2.py:70",
        ),
        gaps=("no live call recorded (needs a vLLM-Omni server)",),
    ),
    TTSQualification(
        provider="elevenlabs",
        label="ElevenLabs cloud TTS",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNVERIFIED,
        commercial_status=UNVERIFIED,
        adapter="ElevenLabsTTSProvider",
        credential_keys=("tts.elevenlabs_api_key",),
        notes=("Implemented against api.elevenlabs.io. Credential-gated: the "
               "constructor refuses to build without a key, which is why this "
               "provider is never a silent fallback. Billing is per character "
               "and the estimate is recorded as an estimate. Commercial terms "
               "were not audited here."),
        evidence=(
            "backend/app/providers/tts.py:515",
            "backend/app/providers/tts.py:552",
            "backend/tests/test_work15_6_providers.py",
            "backend/tests/test_voices_elevenlabs.py:63",
        ),
        gaps=("no live call recorded (needs a real ElevenLabs key)",),
    ),
    TTSQualification(
        provider="mock",
        label="Deterministic silence (simulation)",
        implementation_status=IMPLEMENTED,
        contract_status=CONTRACT_TESTED,
        live_status=UNAVAILABLE,
        commercial_status=UNAVAILABLE,
        adapter="MockTTSProvider",
        simulation_only=True,
        notes=("SIMULATION. Produces labelled silence sized to the text. It is "
               "the only provider permitted to set TTSResult.is_mock, and it "
               "can never be production-ready. Testing it offline is free, so "
               "it is genuinely contract-tested."),
        evidence=(
            "backend/app/providers/tts.py:257",
            "backend/tests/test_work15_6_providers.py",
            "backend/tests/test_phase_a.py:22",
        ),
        gaps=(
            "the output is silence, so it exercises the contract and nothing "
            "about a vendor",
            "excluded from the contract probe set — probing the simulation "
            "would prove the probe, not an adapter",
        ),
    ),
)

TTS_QUALIFICATIONS: tuple[TTSQualification, ...] = (
    IMPLEMENTED_ADAPTERS + DONOR_CANDIDATES)

_BY_PROVIDER: dict[str, TTSQualification] = {q.provider: q for q in TTS_QUALIFICATIONS}


def get_qualification(provider: str) -> TTSQualification | None:
    """Look one up by id. Unknown ids return ``None``, never raise."""
    return _BY_PROVIDER.get((provider or "").strip().lower())


def list_qualifications() -> tuple[TTSQualification, ...]:
    return TTS_QUALIFICATIONS


#: The TTS *qualification* labels the work order asks for, and what each maps
#: onto. These are not maturity states — three of the six have no state of their
#: own — so they are derived from the state fields rather than typed into a
#: second, competing vocabulary:
#:
#: ``IMPLEMENTED``            -> implementation_status == IMPLEMENTED
#: ``CONTRACT_TESTED``         -> contract_status == CONTRACT_TESTED
#: ``LIVE_VERIFIED``           -> live_status == LIVE_VERIFIED
#: ``CONFIG_GATED``            -> the adapter cannot run without configuration
#:                               (a credential key it must read first)
#: ``COMMERCIAL_LIMITATION``   -> commercial_status is any of the three
#:                               restriction states
#: ``UNAVAILABLE``             -> implementation_status == UNAVAILABLE
CONFIG_GATED = "CONFIG_GATED"
COMMERCIAL_LIMITATION = "COMMERCIAL_LIMITATION"
SIMULATION = "SIMULATION"

_QUALIFICATION_LABELS = (
    "IMPLEMENTED", "CONTRACT_TESTED", "LIVE_VERIFIED", "CONFIG_GATED",
    "COMMERCIAL_LIMITATION", "UNAVAILABLE", SIMULATION,
)

_RESTRICTED_COMMERCIAL = frozenset(
    {BLOCKED_COMMERCIAL_TERMS, BLOCKED_LICENSE, EXTERNAL_LIMITATION})


def qualification_labels(qual: TTSQualification) -> tuple[str, ...]:
    """Map one adapter onto the work order's qualification labels.

    Derived from the state fields, never from prose: searching a note for the
    word "IMPLEMENTED" would match "NOT IMPLEMENTED", which is exactly the kind
    of accidental agreement that lets a false claim through.
    """
    labels: list[str] = []
    if qual.implementation_status == IMPLEMENTED:
        labels.append("IMPLEMENTED")
    else:
        labels.append("UNAVAILABLE")
    if qual.contract_status == CONTRACT_TESTED:
        labels.append("CONTRACT_TESTED")
    if qual.live_status == LIVE_VERIFIED:
        labels.append("LIVE_VERIFIED")
    if qual.credential_keys:
        labels.append(CONFIG_GATED)
    if qual.commercial_status in _RESTRICTED_COMMERCIAL:
        labels.append(COMMERCIAL_LIMITATION)
    if qual.simulation_only:
        labels.append(SIMULATION)
    return tuple(label for label in _QUALIFICATION_LABELS if label in labels)


def donor_candidates() -> tuple[dict, ...]:
    """The six donor TTS adapters the matrix called MERGE, with their verdicts.

    Returned as dicts so the API can serve them without importing this module's
    dataclasses. ``merged`` is the field the matrix's wording invites people to
    assume, stated explicitly so nobody has to.
    """
    out: list[dict] = []
    for qual in DONOR_CANDIDATES:
        out.append({
            "provider": qual.provider,
            "label": qual.label,
            "merged": False,
            "qualification_labels": list(qualification_labels(qual)),
            "implementation_status": qual.implementation_status,
            "commercial_status": qual.commercial_status,
            "donor_reference": qual.donor_reference,
            "reason": qual.notes,
            "would_require": list(qual.would_require),
        })
    return tuple(out)


def tts_records() -> tuple[ProviderMaturity, ...]:
    """The TTS rows for the canonical maturity registry."""
    return validated_records(tuple(
        ProviderMaturity(
            provider=qual.provider,
            capability=CAPABILITY,
            implementation_status=qual.implementation_status,
            contract_status=qual.contract_status,
            live_status=qual.live_status,
            commercial_status=qual.commercial_status,
            credential_status=(CREDENTIAL_NOT_REQUIRED if not qual.credential_keys
                               else CREDENTIAL_REQUIRED),
            credential_keys=qual.credential_keys,
            simulation_only=qual.simulation_only,
            notes=qual.notes,
            evidence=qual.evidence,
            gaps=qual.gaps,
        )
        for qual in TTS_QUALIFICATIONS
    ))


# ---------------------------------------------------------------------------
# the contract probe — what "contract tested" actually means here
# ---------------------------------------------------------------------------


class ContractProbe:
    """The result of probing one adapter offline.

    ``failures`` empty means the adapter honoured the interface. It does **not**
    mean the vendor works, and ``live_status`` stays ``UNVERIFIED`` regardless:
    that distinction is the reason this type exists.
    """

    __slots__ = ("provider", "failures", "checked")

    def __init__(self, provider: str) -> None:
        self.provider = provider
        self.failures: list[str] = []
        self.checked: list[str] = []

    def record(self, name: str, ok: bool, detail: str = "") -> None:
        self.checked.append(name)
        if not ok:
            self.failures.append(f"{name}: {detail}" if detail else name)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict:
        return {"provider": self.provider, "ok": self.ok,
                "checked": list(self.checked), "failures": list(self.failures)}


class _Resp:
    """A minimal httpx response double. No sockets, no real client."""

    def __init__(self, status: int = 200, content: bytes = b"audio-bytes",
                 payload: dict | None = None, text: str = "") -> None:
        self.status_code = status
        self.content = content
        self.text = text
        self._payload = payload

    def json(self):
        if self._payload is None:
            raise ValueError("no json body")
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


#: Audio bytes every transport double returns. ElevenLabs refuses anything under
#: 512 bytes as "suspiciously small" (providers/tts.py:581), so a probe using a
#: short stub would fail on a real adapter's own sanity check rather than on the
#: contract it is meant to check.
#: Audio bytes every transport double returns. ElevenLabs refuses anything under
#: 512 bytes as "suspiciously small" (providers/tts.py:581), so a probe using a
#: short stub would fail on a real adapter's own sanity check rather than on the
#: contract it is meant to check.
PROBE_AUDIO_BYTES = b"probe-audio-payload" * 40

#: A synthetic workspace for qualification probes. The transport is stubbed, so
#: nothing here reaches a real tenant; the scope exists because a billable
#: adapter correctly refuses an ownerless call (Work 15.9 §1).
PROBE_WORKSPACE = "__probe__"


def _install_transport_doubles(monkeypatch, *, status: int = 200,
                               content: bytes = PROBE_AUDIO_BYTES) -> dict:
    """Replace the HTTP boundary with doubles and watch the raw socket layer.

    The point is that a probe cannot accidentally reach a vendor: ``httpx`` is
    stubbed, and any connection to a non-loopback address is recorded and then
    refused, so a probe that tries to bill somebody fails loudly instead.

    Loopback is deliberately allowed. The adapters call ``asyncio.run`` (via
    ``_run_coro``), and on Windows the Proactor event loop builds its self-pipe
    from a real loopback socketpair — so banning every socket would fail the
    probe for a reason that has nothing to do with the vendor.
    """
    seen: dict = {"remote_sockets": []}

    def _post(url, **kw):
        seen["post_url"] = url
        seen["post_json"] = kw.get("json", {})
        return _Resp(status=status, content=content, text="probe")

    def _get(url, **kw):
        seen["get_url"] = url
        # Both id spellings on purpose: the OpenAI-compatible adapters read
        # ``id`` while ElevenLabs reads ``voice_id`` (providers/tts.py:593).
        # A double with only one would fail a real adapter on the probe's own
        # fixture shape rather than on the contract under test.
        return _Resp(status=status, content=content,
                     payload={"voices": [{"id": "probe-voice",
                                          "voice_id": "probe-voice",
                                          "gender": "Female",
                                          "labels": {"gender": "female",
                                                     "accent": "en-us"},
                                          "locale": "en-us"}]})

    monkeypatch.setattr("httpx.post", _post)
    monkeypatch.setattr("httpx.get", _get)

    import socket as _socket

    def _is_loopback(address) -> bool:
        host = address[0] if isinstance(address, tuple) else address
        return str(host) in ("127.0.0.1", "::1", "localhost")

    def _guard(self, address, *args, **kwargs):  # pragma: no cover - failure path
        if _is_loopback(address):
            return _original_connect(self, address, *args, **kwargs)
        seen["remote_sockets"].append(address)
        raise AssertionError(
            f"the contract probe opened a socket to {address!r}")

    _original_connect = _socket.socket.connect
    monkeypatch.setattr(_socket.socket, "connect", _guard)
    return seen


#: Synthetic configuration the probe supplies for CONFIG_GATED adapters.
#:
#: ``kokoro``, ``chatterbox``, ``qwen3`` and ``elevenlabs`` all refuse to build
#: without their URL or key — which is correct behaviour, and also means a probe
#: that does not supply one could only ever assert "refused". Supplying a
#: synthetic value is what makes the probe reach the code under test, and the
#: probe says so in its result (``config.synthetic``) so nobody reads a passing
#: probe as evidence that the provider is configured or reachable.
PROBE_CONFIG: dict[str, dict[str, str]] = {
    "kokoro": {"_kokoro_base_url": "http://kokoro.probe.invalid"},
    "chatterbox": {"_chatterbox_base_url": "http://chatterbox.probe.invalid"},
    "qwen3": {"_qwen_base_url": "http://qwen.probe.invalid",
              "_qwen_instruct": "speak evenly"},
    # ElevenLabs reads its key through ``_cred`` in the constructor, so the
    # probe substitutes that resolver rather than a credential row.
    "elevenlabs": {"_cred": "probe-key-not-a-credential"},
}

def contract_probe(provider: str, monkeypatch, *, configure: bool = True,
                   workspace_id: str = PROBE_WORKSPACE) -> ContractProbe:
    """Exercise one adapter against :class:`BaseTTSProvider`, offline.

    Requires ``monkeypatch`` because the transport must be replaced, not
    trusted. Pass ``configure=False`` to assert the *ungated* behaviour — that
    is how a CONFIG_GATED provider's refusal is proved.

    Work 15.9: the probe runs inside a synthetic workspace scope. A probe is a
    qualification exercise with no tenant, and a billable adapter now refuses
    an ownerless call before the request leaves — which is correct, and would
    otherwise make every adapter look broken. The scope is a fixed synthetic id:
    nothing here touches a real workspace, and the transport is stubbed anyway.

    Checks, in order:

    1. the factory returns a ``BaseTTSProvider`` instance,
    2. ``synthesize`` returns a ``TTSResult`` with audio bytes, the right
       format, and a ``provider`` that matches the adapter,
    3. ``is_mock`` is True for exactly one provider (the simulation),
    4. empty input raises ``TTSError`` instead of returning empty audio,
    5. ``voices()`` returns a list of dicts carrying an ``id``,
    6. ``wav_duration_seconds`` agrees with the RIFF header of a wav result.
    """
    from app.providers import tts as tts_module
    from app.providers.tts import (
        BaseTTSProvider,
        TTSError,
        TTSResult,
        get_tts_provider,
        wav_duration_seconds,
    )
    from app.services.provider_settings import workspace_scope

    probe = ContractProbe(provider)
    scope = workspace_scope(workspace_id) if workspace_id else None
    if scope is not None:
        scope.__enter__()
    try:
        return _contract_probe_body(provider, monkeypatch, configure, probe,
                                    tts_module, BaseTTSProvider, TTSError,
                                    TTSResult, get_tts_provider,
                                    wav_duration_seconds)
    finally:
        if scope is not None:
            scope.__exit__(None, None, None)


def _contract_probe_body(provider, monkeypatch, configure, probe, tts_module,
                         BaseTTSProvider, TTSError, TTSResult,
                         get_tts_provider, wav_duration_seconds) -> ContractProbe:
    key = (provider or "").strip().lower()
    if key == "edge":
        _install_edge_double(monkeypatch)
    else:
        _install_transport_doubles(monkeypatch)
    if configure:
        # One shape for both kinds of stub: the zero-argument helpers
        # (``_kokoro_base_url()``) and ``_cred(key, env_attr)`` both work with a
        # ``*args`` lambda that ignores what it is handed.
        for attr, value in PROBE_CONFIG.get(key, {}).items():
            monkeypatch.setattr(tts_module, attr,
                                lambda _v=value, *a, **k: _v)
        if PROBE_CONFIG.get(key):
            probe.record("config.synthetic", True)

    try:
        built = get_tts_provider(key)
    except TTSError as exc:
        probe.record("factory.builds", False, f"factory refused: {exc}")
        return probe
    probe.record("factory.builds", isinstance(built, BaseTTSProvider),
                 f"not a BaseTTSProvider: {type(built).__name__}")

    try:
        result = built.synthesize("a short honest sentence for the probe")
    except Exception as exc:  # noqa: BLE001 - a probe reports, it does not raise
        probe.record("synthesize.returns", False, f"{type(exc).__name__}: {exc}")
        return probe
    probe.record("synthesize.returns", isinstance(result, TTSResult),
                 f"returned {type(result).__name__}")
    probe.record("synthesize.non_empty_audio", bool(getattr(result, "audio_bytes", b"")))
    probe.record("synthesize.format", getattr(result, "format", "") in ("mp3", "wav"),
                 f"format={getattr(result, 'format', '')!r}")
    probe.record("synthesize.provider_label",
                 getattr(result, "provider", "") == getattr(built, "name", ""),
                 f"{getattr(result, 'provider', '')!r} != {getattr(built, 'name', '')!r}")
    probe.record("synthesize.mock_flag",
                 bool(getattr(result, "is_mock", False)) == (key == "mock"),
                 "is_mock is only true for the simulation")

    try:
        built.synthesize("   ")
    except TTSError:
        probe.record("synthesize.empty_text_raises", True)
    except Exception as exc:  # noqa: BLE001
        probe.record("synthesize.empty_text_raises", False,
                     f"raised {type(exc).__name__}, not TTSError")
    else:
        probe.record("synthesize.empty_text_raises", False,
                     "empty text returned audio")

    try:
        voices = built.voices()
        well_formed = (isinstance(voices, list) and voices
                       and all(isinstance(v, dict) and v.get("id") for v in voices))
        probe.record("voices.well_formed", well_formed, f"voices={voices!r}")
    except Exception as exc:  # noqa: BLE001
        probe.record("voices.well_formed", False, f"{type(exc).__name__}: {exc}")

    if getattr(result, "format", "") == "wav":
        measured = wav_duration_seconds(result.audio_bytes,
                                        sample_rate=getattr(result, "sample_rate", None) or 16000)
        probe.record("duration.measured_from_header", measured > 0,
                     f"duration={measured}")
    return probe


def _install_edge_double(monkeypatch) -> None:
    """Stub ``edge_tts`` so the edge adapter's async stream can be exercised.

    The adapter imports ``edge_tts`` inside ``_run``, so a stubbed module in
    ``sys.modules`` is both necessary and sufficient — and it keeps the test
    offline without touching the adapter's source.
    """
    import sys
    import types

    class _Communicate:
        def __init__(self, text, voice, rate=None, volume=None):
            self.text = text
            self.voice = voice
            self.rate = rate
            self.volume = volume

        async def stream(self):
            if not self.text.strip():
                return
            yield {"type": "audio", "data": b"edge-mp3-bytes"}

    async def _list_voices():
        return [{"ShortName": "en-US-ProbeNeural", "Gender": "Female",
                 "Locale": "en-US"}]

    module = types.ModuleType("edge_tts")
    module.Communicate = _Communicate
    module.list_voices = _list_voices
    monkeypatch.setitem(sys.modules, "edge_tts", module)
    _install_transport_doubles(monkeypatch)


#: The providers a contract probe may exercise. ``mock`` is excluded because it
#: is the simulation: probing it proves the probe, not a real adapter.
PROBEABLE: tuple[str, ...] = tuple(
    q.provider for q in IMPLEMENTED_ADAPTERS if q.provider != "mock"
)


def probed_providers() -> tuple[str, ...]:
    return PROBEABLE


__all__ = [
    "CAPABILITY",
    "COMMERCIAL_LIMITATION",
    "COMMERCIAL_STATES_IN_USE",
    "CONFIG_GATED",
    "ContractProbe",
    "DONOR_CANDIDATES",
    "IMPLEMENTED_ADAPTERS",
    "PROBEABLE",
    "SIMULATION",
    "TTS_QUALIFICATIONS",
    "TTSQualification",
    "contract_probe",
    "donor_candidates",
    "get_qualification",
    "list_qualifications",
    "probed_providers",
    "qualification_labels",
    "tts_records",
]

#: Re-exported so a reader of this module can see which commercial states the
#: donor candidates use without opening the registry. The values come from the
#: single canonical vocabulary; nothing here declares its own.
COMMERCIAL_STATES_IN_USE: tuple[str, ...] = tuple(sorted({
    q.commercial_status for q in TTS_QUALIFICATIONS
} - {UNVERIFIED}))
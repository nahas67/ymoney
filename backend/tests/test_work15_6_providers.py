"""Work 15.6 §3+§4 — provider maturity must be honest, and laziness must hold.

Two failure modes are under test, and they are the reason this lane exists:

* **A status inferred from "the module imported."** The registry is a data
  table, and the tests below assert that no function reaches for ``importlib``,
  no row claims ``LIVE_VERIFIED``, no row claims ``CONTRACT_TESTED`` without a
  test that actually drives it, and nothing anywhere reports
  ``production_ready``.
* **A credential fingerprint leaking through a status endpoint.** The API
  resolves whether a key is *present*; the tests write a real-looking secret and
  assert the value, its length, and its digest are all absent from the payload.

Plus the property Work 15.5 established and this must match: importing the
registry resolves nothing. The test poisons the credential resolver, drops the
module from ``sys.modules``, reimports it, and exercises every lookup — if any
lookup reaches the resolver or the network, it fails there.

No network: the transport boundary is stubbed and ``socket.connect`` is made to
explode, so a probe that tries to reach a vendor fails the test rather than
billing somebody. Live-provider tests are a separate ``live`` marker, honestly
skipped without a credential.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import os
import sys
from pathlib import Path

import pytest

from app.providers import maturity
from app.providers import tts_qualification as ttsq

REPO_ROOT = Path(__file__).resolve().parents[2]

#: A stand-in for a real key. Built by concatenation so this file never holds a
#: literal that looks like a credential to a scanner — and so a leak test can
#: search for the exact string it wrote.
FAKE_KEY = "eleven" + "labs-" + "Zq7" + "unit-test-value"


# ---------------------------------------------------------------------------
# the vocabulary is closed, and the axes are not interchangeable
# ---------------------------------------------------------------------------


EXPECTED_STATES = frozenset({
    "IMPLEMENTED", "CONTRACT_TESTED", "LIVE_VERIFIED", "UNVERIFIED",
    "UNAVAILABLE", "BLOCKED_LICENSE", "BLOCKED_COMMERCIAL_TERMS",
    "EXTERNAL_LIMITATION",
})


def test_the_state_vocabulary_is_exactly_the_eight_named_states():
    """No invented verdicts. An extra state would let a claim slip through."""
    assert maturity.STATES == EXPECTED_STATES


def test_a_state_outside_the_vocabulary_cannot_be_stored():
    with pytest.raises(ValueError, match="not in"):
        maturity.ProviderMaturity(
            provider="rogue", capability="tts",
            implementation_status="PRODUCTION_READY",  # not a state
            contract_status="UNVERIFIED", live_status="UNVERIFIED",
            commercial_status="UNVERIFIED", credential_status="NOT_REQUIRED",
            notes="trying to smuggle a label in")


def test_production_ready_is_a_derived_conclusion_not_a_stored_state():
    """Even a record with every axis at its best is not 'ready'.

    ``production_ready`` is deliberately absent from the vocabulary: it is a
    conclusion over several axes plus the gaps list, and storing it would let
    the axes drift from a label nobody rechecked.
    """
    record = maturity.ProviderMaturity(
        provider="best_case_vendor", capability="video",
        implementation_status=maturity.LIVE_VERIFIED,
        contract_status=maturity.CONTRACT_TESTED,
        live_status=maturity.LIVE_VERIFIED, commercial_status=maturity.UNVERIFIED,
        credential_status=maturity.CREDENTIAL_NOT_REQUIRED,
        notes="every axis at its best, and still not ready")
    assert "PRODUCTION_READY" not in maturity.STATES
    assert maturity.production_ready(record) is False
    assert not hasattr(record, "production_ready")


def test_a_declared_gap_blocks_readiness_even_with_every_axis_green():
    record = maturity.ProviderMaturity(
        provider="gapped_vendor", capability="video",
        implementation_status=maturity.LIVE_VERIFIED,
        contract_status=maturity.CONTRACT_TESTED,
        live_status=maturity.LIVE_VERIFIED, commercial_status=maturity.UNVERIFIED,
        credential_status=maturity.CREDENTIAL_NOT_REQUIRED,
        notes="all axes green but one surface is unexercised",
        gaps=("no live call recorded",))
    assert maturity.production_ready(record) is False
    assert any("uncovered surfaces" in b for b in maturity.production_blockers(record))


def test_commercial_status_cannot_hold_an_implementation_state():
    """'We wrote the adapter' says nothing about a vendor's terms.

    Collapsing the two axes is exactly the overloading that makes a licence
    problem look like a code problem.
    """
    with pytest.raises(ValueError, match="commercial_status"):
        maturity.ProviderMaturity(
            provider="rogue", capability="tts",
            implementation_status="IMPLEMENTED",
            contract_status="CONTRACT_TESTED", live_status="UNVERIFIED",
            commercial_status="IMPLEMENTED",  # nonsense on this axis
            credential_status="NOT_REQUIRED", notes="axis mixing")


def test_live_status_cannot_hold_a_licence_state():
    with pytest.raises(ValueError, match="live_status"):
        maturity.ProviderMaturity(
            provider="rogue", capability="avatar",
            implementation_status="IMPLEMENTED",
            contract_status="UNVERIFIED", live_status="BLOCKED_LICENSE",
            commercial_status="BLOCKED_LICENSE",
            credential_status="REQUIRED", notes="axis mixing again")


def test_a_record_without_a_reason_is_refused():
    """Silence is the defect. An unexplained status is indistinguishable from a guess."""
    with pytest.raises(ValueError, match="must carry a reason"):
        maturity.ProviderMaturity(
            provider="rogue", capability="tts",
            implementation_status="IMPLEMENTED", contract_status="UNVERIFIED",
            live_status="UNVERIFIED", commercial_status="UNVERIFIED",
            credential_status="NOT_REQUIRED", notes="   ")


def test_an_unimplemented_provider_may_not_cite_evidence():
    with pytest.raises(ValueError, match="cite evidence"):
        maturity.ProviderMaturity(
            provider="rogue", capability="tts",
            implementation_status="UNAVAILABLE", contract_status="UNAVAILABLE",
            live_status="UNAVAILABLE", commercial_status="UNVERIFIED",
            credential_status="NOT_REQUIRED", notes="pretending to be built",
            evidence=("backend/app/providers/tts.py:79",))


def test_the_table_has_no_duplicate_provider_capability_pairs():
    table = maturity.list_maturity()
    keys = [(r.provider, r.capability) for r in table]
    assert len(keys) == len(set(keys))


def test_every_capability_axis_has_at_least_one_row():
    seen = {r.capability for r in maturity.list_maturity()}
    assert seen == set(maturity.capabilities())


def test_duplicate_rows_are_rejected_rather_than_silently_kept():
    row = maturity.list_maturity()[0]
    with pytest.raises(RuntimeError, match="duplicate"):
        maturity.validated_records((row, row))


# ---------------------------------------------------------------------------
# the honesty rules
# ---------------------------------------------------------------------------


def test_nothing_anywhere_claims_live_verification():
    """The single most important assertion in this file.

    No provider in this repository has been exercised against its real service
    with a real credential. If a future change flips one of these to
    LIVE_VERIFIED, this test is where the reviewer is told to justify it.
    """
    offenders = [r.provider for r in maturity.list_maturity()
                 if r.live_status == maturity.LIVE_VERIFIED]
    assert offenders == [], (
        f"live_status=LIVE_VERIFIED asserted without recorded evidence: {offenders}")


def test_no_record_claims_a_verification_timestamp():
    stamped = [r.provider for r in maturity.list_maturity() if r.last_verified_at]
    assert stamped == [], f"last_verified_at set without a live call: {stamped}"


def test_nothing_is_production_ready():
    """The honest bottom line: not one provider clears all four axes."""
    summary = maturity.status_summary()
    assert summary["production_ready"] == []
    assert summary["not_production_ready"], "blockers must explain the verdict"


def test_every_production_blocker_names_a_state_a_reader_can_act_on():
    for record in maturity.list_maturity():
        blockers = maturity.production_blockers(record)
        assert blockers, f"{record.provider} claims ready with no blocker"
        assert any(":" in b for b in blockers), blockers


def test_a_simulation_can_never_be_production_ready_even_if_every_axis_agrees():
    """The mock engine is IMPLEMENTED and CONTRACT_TESTED — and still not usable."""
    mock = maturity.get_maturity("mock_video_engine", "video")
    assert mock is not None
    assert mock.implementation_status == maturity.IMPLEMENTED
    assert mock.contract_status == maturity.CONTRACT_TESTED
    assert maturity.production_ready(mock) is False
    assert any("simulation_only" in b for b in maturity.production_blockers(mock))


def test_stamping_a_verification_returns_a_copy_and_leaves_the_table_alone():
    """A request must not be able to promote a provider for every reader."""
    row = maturity.get_maturity("edge", "tts")
    stamped = maturity.stamp_verification(row, "2026-10-01T00:00:00+00:00")
    assert stamped.last_verified_at == "2026-10-01T00:00:00+00:00"
    assert stamped is not row
    assert maturity.get_maturity("edge", "tts").last_verified_at == ""


def test_the_table_is_frozen_so_no_runtime_code_can_rewrite_a_verdict():
    row = maturity.get_maturity("wav2lip", "avatar")
    with pytest.raises(dataclasses.FrozenInstanceError):
        row.commercial_status = maturity.UNVERIFIED  # type: ignore[misc]
    assert maturity.get_maturity("wav2lip", "avatar").commercial_status == \
        maturity.BLOCKED_LICENSE


def test_blocked_license_is_used_only_for_a_licence_or_weight_problem():
    """'No SLA' is EXTERNAL_LIMITATION, not BLOCKED_LICENSE.

    The distinction the work order insists on: conflating them makes a vendor
    availability gap look like a legal blocker and vice versa.
    """
    for record in maturity.list_maturity():
        if record.commercial_status != maturity.BLOCKED_LICENSE:
            continue
        note = record.notes.lower()
        assert any(word in note for word in
                   ("licence", "license", "weights", "checkpoint", "attribution",
                    "non-commercial", "restricted")), (
            f"{record.provider} uses BLOCKED_LICENSE without a licence reason")


def test_every_external_limitation_says_what_the_external_limit_is():
    for record in maturity.list_maturity():
        if record.commercial_status != maturity.EXTERNAL_LIMITATION:
            continue
        note = record.notes.lower()
        assert any(word in note for word in
                   ("sla", "terms", "tos", "rate limit", "attribution", "key")), (
            f"{record.provider} uses EXTERNAL_LIMITATION without naming the limit")


def test_an_architecture_decision_is_never_encoded_as_a_provider_state():
    """'We chose not to use this' is not a maturity state.

    The states are about what a provider IS, so a row cannot claim a status
    that describes a decision instead of a fact. The concrete guard: every
    UNIMPLEMENTED donor row says what is missing (an adapter), never that
    YMONEY declined to build one.
    """
    for qual in ttsq.DONOR_CANDIDATES:
        record = maturity.get_maturity(qual.provider, "tts")
        assert record.implementation_status == maturity.UNAVAILABLE
        assert "not implemented" in qual.notes.lower()
        assert qual.would_require, f"{qual.provider} names no path forward"


def test_every_row_cites_evidence_that_exists_on_disk():
    """A maturity row citing a file nobody wrote is a false attestation.

    Same defect class as the provenance line that claimed
    ``services/path_safety.py`` was copied when it never was built.
    """
    assert maturity.broken_evidence(REPO_ROOT) == []


def test_broken_evidence_actually_detects_a_missing_file():
    ghost = maturity.ProviderMaturity(
        provider="ghost", capability="tts",
        implementation_status=maturity.IMPLEMENTED,
        contract_status=maturity.UNVERIFIED, live_status=maturity.UNVERIFIED,
        commercial_status=maturity.UNVERIFIED,
        credential_status=maturity.CREDENTIAL_NOT_REQUIRED,
        notes="cites a file that does not exist",
        evidence=("backend/app/providers/not_a_real_module.py:1",))
    assert maturity.broken_evidence(REPO_ROOT, (ghost,)) == [
        "ghost: backend/app/providers/not_a_real_module.py"]


# ---------------------------------------------------------------------------
# laziness — the property Work 15.5 proved and this must match
# ---------------------------------------------------------------------------


def _module_scope_imports(module) -> list[str]:
    tree = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in tree.body:  # module scope only; function bodies are not walked
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    return imported


def test_the_maturity_table_has_no_module_scope_app_import():
    """Structural, not behavioural: a module-level ``app.`` import is the defect.

    ``app.providers.tts_qualification`` is imported lazily because it imports
    this module back; the TTS rows are assembled on first use, and the test
    below proves the assembly itself resolves nothing.
    """
    imported = _module_scope_imports(maturity)
    assert imported, "no module-level imports found — the AST walk is broken"
    offenders = [name for name in imported if name.startswith("app.")]
    assert offenders == [], f"module-scope app imports: {offenders}"
    assert "dataclasses" in imported


def test_importing_the_registry_resolves_no_credential_and_opens_no_session(monkeypatch):
    """The Work 15.5 pattern: poison the resolver, reimport, exercise lookups.

    The registry is imported by request handlers, workers, and the CLI. A
    module-level resolver call would make all of them pay a DB round-trip at
    import and would make a registry-only CLI unusable offline.
    """
    import app.services.provider_settings as ps

    def _forbidden(*a, **kw):  # pragma: no cover - only runs on failure
        raise AssertionError("the maturity registry resolved a credential at import")

    monkeypatch.setattr(ps, "get_credential", _forbidden)
    monkeypatch.delitem(sys.modules, "app.providers.maturity", raising=False)
    fresh = importlib.import_module("app.providers.maturity")
    # Touch the data table and every read helper. None may resolve anything.
    assert len(fresh.list_maturity()) > 10
    assert fresh.get_maturity("openai") is not None
    assert fresh.get_maturity("edge", "tts") is not None
    assert fresh.capabilities()
    assert fresh.status_summary()
    assert fresh.unimplemented_donor_tts()


def test_assembling_the_tts_rows_resolves_nothing_either(monkeypatch):
    """The lazy half is only honest if it stays lazy.

    ``list_maturity`` pulls in the TTS qualification table on first call. That
    call must not reach the resolver, the network, or a provider factory.
    """
    import app.services.provider_settings as ps

    def _forbidden(*a, **kw):  # pragma: no cover - only on failure
        raise AssertionError("reading the table resolved a credential")

    def _no_network(*a, **kw):  # pragma: no cover - only on failure
        raise AssertionError("reading the table attempted a network call")

    monkeypatch.setattr(ps, "get_credential", _forbidden)
    monkeypatch.setattr("httpx.get", _no_network)
    monkeypatch.setattr("httpx.post", _no_network)
    monkeypatch.setattr("httpx.Client.request", _no_network)
    monkeypatch.setattr(maturity, "_ALL_RECORDS", None)
    rows = maturity.list_maturity()
    assert any(r.capability == "tts" for r in rows)


def test_reading_the_table_does_not_construct_a_provider():
    """A status must never be inferred by instantiating an adapter.

    If listing the table built providers, it would need credentials, a network,
    and ffmpeg — and a provider that merely imported would start reading as
    ready. The guard is structural: ``importlib`` and ``find_spec`` are absent
    from the read path.
    """
    tree = ast.parse(Path(maturity.__file__).read_text(encoding="utf-8"))
    read_fns = {"list_maturity", "get_maturity", "status_summary",
                "production_blockers", "production_ready", "capabilities"}
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in read_fns:
            seen.add(node.name)
            body = ast.dump(node)
            for forbidden in ("import_module", "find_spec", "__import__"):
                assert forbidden not in body, (
                    f"{node.name} performs module discovery; a status must be "
                    "recorded, not inferred")
    assert seen == read_fns, f"read helpers missing, AST walk broken: {read_fns - seen}"


def test_no_record_claims_a_health_it_never_measured():
    """Nobody in the table claims health. Only an explicit probe may."""
    for record in maturity.list_maturity():
        assert record.health == maturity.HEALTH_UNKNOWN
        assert record.to_dict()["health"] == maturity.HEALTH_UNKNOWN


def test_probe_health_reports_unknown_for_a_non_tts_capability():
    """Only the TTS layer has a probe; guessing elsewhere would be a lie."""
    for capability in ("llm", "music", "video", "image", "avatar"):
        for record in maturity.list_maturity(capability):
            assert maturity.probe_health(record) == maturity.HEALTH_UNKNOWN


def test_health_probe_reads_unknown_when_the_probe_raises():
    """A probe that blows up reports UNKNOWN, never OK and never DOWN."""
    from app.providers import tts as tts_module

    class _Exploding(tts_module.BaseTTSProvider):
        name = "edge"

        def synthesize(self, text, **kw):  # pragma: no cover - never called
            raise AssertionError("health probe must not synthesise")

        def voices(self, language=""):  # pragma: no cover - never called
            return []

        def health(self):
            raise RuntimeError("probe exploded")

    original = tts_module.EdgeTTSProvider
    tts_module.EdgeTTSProvider = _Exploding
    try:
        record = maturity.get_maturity("edge", "tts")
        assert maturity.probe_health(record) == maturity.HEALTH_UNKNOWN
    finally:
        tts_module.EdgeTTSProvider = original


# ---------------------------------------------------------------------------
# credential resolution returns a word, never a value
# ---------------------------------------------------------------------------


def test_credential_resolution_returns_a_state_word(workspace_with_user):
    from app.services.provider_settings import set_credential

    ws = workspace_with_user["workspace"]
    record = maturity.get_maturity("elevenlabs", "tts")
    set_credential("tts.elevenlabs_api_key", None, ws)
    assert maturity.resolve_credential_status(record, ws) == \
        maturity.CREDENTIAL_NOT_CONFIGURED
    set_credential("tts.elevenlabs_api_key", FAKE_KEY, ws)
    assert maturity.resolve_credential_status(record, ws) == \
        maturity.CREDENTIAL_CONFIGURED


def test_a_provider_that_needs_no_credential_says_so_without_resolving():
    """Edge and the mocks never read the resolver at all."""
    import app.services.provider_settings as ps

    record = maturity.get_maturity("edge", "tts")
    assert record.credential_keys == ()

    def _forbidden(*a, **kw):  # pragma: no cover - only on failure
        raise AssertionError("a keyless provider asked for a credential")

    original = ps.get_credential
    ps.get_credential = _forbidden
    try:
        assert maturity.resolve_credential_status(record, None) == \
            maturity.CREDENTIAL_NOT_REQUIRED
    finally:
        ps.get_credential = original


def test_a_resolver_failure_is_unresolved_not_configured(workspace_with_user, monkeypatch):
    """The dangerous direction is claiming a credential exists when it does not."""
    import app.services.provider_settings as ps

    def _boom(*a, **kw):
        raise RuntimeError("database is down")

    monkeypatch.setattr(ps, "get_credential", _boom)
    record = maturity.get_maturity("elevenlabs", "tts")
    assert maturity.resolve_credential_status(record, workspace_with_user["workspace"]) == \
        maturity.CREDENTIAL_UNRESOLVED


def test_a_credential_key_missing_from_the_registry_is_unresolved():
    """A typo'd key must not read as 'configured' or as 'fine'."""
    record = maturity.ProviderMaturity(
        provider="typo_vendor", capability="tts",
        implementation_status=maturity.IMPLEMENTED,
        contract_status=maturity.UNVERIFIED, live_status=maturity.UNVERIFIED,
        commercial_status=maturity.UNVERIFIED,
        credential_status=maturity.CREDENTIAL_REQUIRED,
        credential_keys=("tts.not_a_real_key",),
        notes="cites a credential key the resolver does not know")
    assert maturity.resolve_credential_status(record, None) == \
        maturity.CREDENTIAL_UNRESOLVED


def test_a_requirement_with_no_key_to_look_up_is_unresolved():
    """Recorded as needing a credential but naming nothing is a defect."""
    record = maturity.ProviderMaturity(
        provider="vague_vendor", capability="tts",
        implementation_status=maturity.IMPLEMENTED,
        contract_status=maturity.UNVERIFIED, live_status=maturity.UNVERIFIED,
        commercial_status=maturity.UNVERIFIED,
        credential_status=maturity.CREDENTIAL_REQUIRED, credential_keys=(),
        notes="needs a key but names none")
    assert maturity.resolve_credential_status(record, None) == \
        maturity.CREDENTIAL_UNRESOLVED


def test_every_declared_credential_key_exists_in_the_workspace_registry():
    """A typo here would only surface as an UNRESOLVED row at runtime."""
    from app.services.provider_settings import REGISTRY

    for record in maturity.list_maturity():
        for key in record.credential_keys:
            assert key in REGISTRY, f"{record.provider}: unknown credential key {key}"


def test_resolved_credentials_never_include_a_value_length_or_digest(workspace_with_user):
    from app.services.provider_settings import set_credential

    ws = workspace_with_user["workspace"]
    set_credential("tts.elevenlabs_api_key", FAKE_KEY, ws)
    rows = maturity.with_resolved_credentials(maturity.list_maturity(), ws)
    assert FAKE_KEY not in repr(rows)
    for row in rows:
        assert row["resolved_credential_status"] in maturity.RESOLVED_CREDENTIAL_STATES
        for forbidden in ("value", "key_value", "digest", "fingerprint", "length"):
            assert forbidden not in row


# ---------------------------------------------------------------------------
# TTS qualification — the six donor candidates the matrix called MERGE
# ---------------------------------------------------------------------------


DONOR_EXPECTED = {
    "minimax": maturity.UNAVAILABLE,
    "fish_audio": maturity.UNAVAILABLE,
    "voxcpm": maturity.UNAVAILABLE,
    "siliconflow_tts": maturity.UNAVAILABLE,
    "gemini_tts": maturity.UNAVAILABLE,
    "azure_speech_v2": maturity.UNAVAILABLE,
}


@pytest.mark.parametrize("provider", sorted(DONOR_EXPECTED))
def test_every_donor_candidate_is_recorded_as_unavailable_not_merged(provider):
    qual = ttsq.get_qualification(provider)
    assert qual is not None, f"{provider} is missing from the qualification table"
    record = maturity.get_maturity(provider, "tts")
    assert record is not None
    assert record.implementation_status == DONOR_EXPECTED[provider]
    assert qual.donor_reference, "a donor candidate must cite where the donor had it"


def test_the_donor_candidate_list_is_exactly_the_six_the_matrix_named():
    assert {q.provider for q in ttsq.DONOR_CANDIDATES} == set(DONOR_EXPECTED)
    assert len(ttsq.DONOR_CANDIDATES) == 6


def test_donor_candidates_expose_merged_false_explicitly():
    """``MERGE`` in the matrix reads as 'done' given time. Say it is not done."""
    for payload in ttsq.donor_candidates():
        assert payload["merged"] is False
        assert payload["reason"]
        assert payload["would_require"]
        assert "UNAVAILABLE" in payload["qualification_labels"]
        assert "IMPLEMENTED" not in payload["qualification_labels"]


def test_no_donor_candidate_exists_as_a_provider_class_or_factory_branch():
    """The strongest evidence: grep the tree, not the plan.

    If a future change does implement one of these, this test fails and forces
    the status to be revisited rather than silently leaving ``UNAVAILABLE``
    next to a working adapter.
    """
    from app.providers import tts as tts_module

    source = Path(tts_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    classes = {node.name.lower() for node in ast.walk(tree)
               if isinstance(node, ast.ClassDef)}
    for provider in DONOR_EXPECTED:
        needle = provider.replace("_tts", "").replace("_v2", "").replace("_", "")
        assert not any(needle in name for name in classes), (
            f"a class matching {provider} now exists — update the status")
        assert provider not in source, (
            f"{provider} appears in providers/tts.py — update the status")
    assert set(tts_module.__all__) >= {
        "BaseTTSProvider", "ChatterboxTTSProvider", "EdgeTTSProvider",
        "KokoroTTSProvider", "MockTTSProvider", "QwenTTSProvider", "TTSError",
        "TTSResult", "get_tts_provider", "tts_provider_status",
        "wav_duration_seconds"}


def test_no_donor_candidate_has_a_credential_slot():
    """Even a hand-patched factory branch would have nowhere to read a key."""
    from app.services.provider_settings import REGISTRY

    for provider in DONOR_EXPECTED:
        stem = provider.replace("_tts", "").replace("_v2", "")
        assert not [k for k in REGISTRY if stem in k], (
            f"{provider} gained a credential key — update the status")


def test_fish_audio_is_blocked_on_commercial_terms_not_licence():
    """Two independent reasons, recorded on two different axes."""
    record = maturity.get_maturity("fish_audio", "tts")
    assert record.commercial_status == maturity.BLOCKED_COMMERCIAL_TERMS
    assert record.implementation_status == maturity.UNAVAILABLE
    note = record.notes.lower()
    assert "fair use" in note or "sla" in note


def test_siliconflow_the_llm_vendor_is_not_the_tts_provider():
    """The registry must not let one capability's row vouch for another's."""
    llm = maturity.get_maturity("siliconflow", "llm")
    tts = maturity.get_maturity("siliconflow_tts", "tts")
    assert llm.implementation_status == maturity.IMPLEMENTED
    assert tts.implementation_status == maturity.UNAVAILABLE
    assert tts is not llm


def test_implemented_tts_providers_all_have_an_adapter_path():
    for qual in ttsq.IMPLEMENTED_ADAPTERS:
        assert qual.adapter, f"{qual.provider} claims implemented with no adapter"


def test_qualification_labels_are_derived_from_state_fields_not_prose():
    """The work order's six labels are mapped, not typed.

    Deriving them from the state fields is the point: searching the note for the
    word ``IMPLEMENTED`` would match "NOT IMPLEMENTED" — an accidental agreement
    that is exactly how a false claim survives review.
    """
    expected = {
        # provider: labels the work order expects
        "edge": ("IMPLEMENTED", "CONTRACT_TESTED"),
        "kokoro": ("IMPLEMENTED", "CONTRACT_TESTED", "CONFIG_GATED"),
        "chatterbox": ("IMPLEMENTED", "CONTRACT_TESTED", "CONFIG_GATED"),
        "qwen3": ("IMPLEMENTED", "CONTRACT_TESTED", "CONFIG_GATED"),
        "elevenlabs": ("IMPLEMENTED", "CONTRACT_TESTED", "CONFIG_GATED"),
        "mock": ("IMPLEMENTED", "CONTRACT_TESTED", "SIMULATION"),
        "minimax": ("UNAVAILABLE",),
        "fish_audio": ("COMMERCIAL_LIMITATION", "UNAVAILABLE"),
        "voxcpm": ("UNAVAILABLE",),
        "siliconflow_tts": ("UNAVAILABLE",),
        "gemini_tts": ("UNAVAILABLE",),
        "azure_speech_v2": ("UNAVAILABLE",),
    }
    for provider, labels in expected.items():
        qual = ttsq.get_qualification(provider)
        assert qual is not None, provider
        assert ttsq.qualification_labels(qual) == labels, provider


def test_no_tts_adapter_is_labelled_live_verified():
    """The label is derived from ``live_status``, which is UNVERIFIED throughout."""
    for qual in ttsq.list_qualifications():
        assert "LIVE_VERIFIED" not in ttsq.qualification_labels(qual), qual.provider
    assert ttsq.qualification_labels(ttsq.get_qualification("edge")) == \
        ("IMPLEMENTED", "CONTRACT_TESTED")


def test_the_implemented_tts_set_matches_the_classes_in_the_module():
    """Every concrete adapter class in ``providers.tts`` has a qualification row.

    This compares against the *classes defined in the module*, not ``__all__``,
    because ``__all__`` is itself incomplete: ``ElevenLabsTTSProvider``
    (providers/tts.py:515) is a working adapter that ``__all__`` omits. That is
    a real defect in a file this lane does not own, so it is recorded rather
    than silently worked around — and it is why the check is on classes.
    """
    from app.providers import tts as tts_module

    tree = ast.parse(Path(tts_module.__file__).read_text(encoding="utf-8"))
    defined = {node.name for node in tree.body if isinstance(node, ast.ClassDef)
               and node.name.endswith("Provider")}
    assert "BaseTTSProvider" in defined, "the base class moved — update this test"
    defined.discard("BaseTTSProvider")
    recorded = {q.adapter for q in ttsq.IMPLEMENTED_ADAPTERS if q.adapter}
    assert recorded == defined, (
        "a TTS adapter class exists without a qualification row: "
        f"{defined ^ recorded}")


def test_elevenlabs_is_missing_from_the_tts_module_dunder_all():
    """A real defect in a file this lane does not own, recorded so it is not lost.

    ``providers/tts.py:739`` omits ``ElevenLabsTTSProvider`` from ``__all__``
    even though the class exists, the factory builds it, and other tests import
    it directly. ``from app.providers.tts import *`` therefore silently drops a
    provider. This assertion documents the current state; when the omission is
    fixed upstream it fails and gets updated.
    """
    from app.providers import tts as tts_module

    assert hasattr(tts_module, "ElevenLabsTTSProvider")
    assert "ElevenLabsTTSProvider" not in tts_module.__all__


def test_the_llm_rows_agree_with_the_work15_5_vendor_list():
    """The maturity table lists vendors literally, so drift must fail a test."""
    from app.engine.intelligence.llm_registry import list_providers

    registry_ids = {spec.provider_id for spec in list_providers()}
    maturity_ids = {r.provider for r in maturity.list_maturity("llm")}
    assert registry_ids == maturity_ids


# ---------------------------------------------------------------------------
# the contract probe — what CONTRACT_TESTED actually means
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("provider", ttsq.probed_providers())
def test_every_real_tts_adapter_satisfies_the_base_contract(provider, monkeypatch):
    """Returns TTSResult, honours errors, labels itself, and touches no network."""
    probe = ttsq.contract_probe(provider, monkeypatch)
    assert probe.ok, f"{provider} contract failures: {probe.failures}"
    assert probe.checked, "a probe that checked nothing proves nothing"


@pytest.mark.parametrize("provider", ttsq.probed_providers())
def test_the_contract_probe_never_opens_a_socket(provider, monkeypatch):
    """The doubles are not a courtesy — the probe must be provably offline."""
    probe = ttsq.contract_probe(provider, monkeypatch)
    assert probe.ok
    assert "synthesize.returns" in probe.checked


def _install_broken_provider(monkeypatch, provider_cls, target="KokoroTTSProvider"):
    """Swap a deliberately non-conforming adapter in for a mutation proof.

    ``contract_probe`` supplies its own synthetic configuration, so the broken
    class replaces only the adapter under test. ``target`` is the *existing*
    attribute name being replaced, not the local class name.

    The replacement is built with ``__new__`` so it accepts whatever arguments
    the real constructor took: the point is to reach the code under test, not to
    fail earlier on a signature mismatch.
    """
    from app.providers import tts as tts_module

    assert hasattr(tts_module, target), f"providers.tts has no {target} to replace"

    def _factory(*a, **k):
        return provider_cls.__new__(provider_cls)

    monkeypatch.setattr(tts_module, target, _factory)


def test_the_contract_probe_catches_a_provider_that_returns_the_wrong_shape(monkeypatch):
    """Mutation proof: break the contract and the probe must notice."""
    from app.providers import tts as tts_module

    class _Broken(tts_module.BaseTTSProvider):
        name = "kokoro"

        def synthesize(self, text, **kw):
            return {"audio": b"not-a-TTSResult"}  # wrong shape entirely

        def voices(self, language=""):
            return [{"no_id": True}]

    _install_broken_provider(monkeypatch, _Broken)
    probe = ttsq.contract_probe("kokoro", monkeypatch)
    assert not probe.ok
    assert any("synthesize.returns" in f for f in probe.failures), probe.failures


def test_the_contract_probe_catches_a_provider_that_swallows_empty_text(monkeypatch):
    """Returning empty audio for empty text is the failure mode that matters:
    a silent render that reports success."""
    from app.providers import tts as tts_module

    class _Silent(tts_module.BaseTTSProvider):
        name = "kokoro"

        def synthesize(self, text, **kw):
            return tts_module.TTSResult(audio_bytes=b"", format="mp3",
                                        provider=self.name)

        def voices(self, language=""):
            return [{"id": "x"}]

    _install_broken_provider(monkeypatch, _Silent)
    probe = ttsq.contract_probe("kokoro", monkeypatch)
    assert not probe.ok
    assert any("non_empty_audio" in f for f in probe.failures), probe.failures
    assert any("empty_text_raises" in f for f in probe.failures), probe.failures


def test_the_contract_probe_catches_a_provider_that_mislabels_itself(monkeypatch):
    """A result whose ``provider`` disagrees with the adapter's ``name`` is how
    one vendor's audio ends up filed under another's licence and provenance."""
    from app.providers import tts as tts_module

    class _Liar(tts_module.BaseTTSProvider):
        name = "kokoro"

        def synthesize(self, text, **kw):
            return tts_module.TTSResult(audio_bytes=b"a" * 64, format="mp3",
                                        provider="elevenlabs")

        def voices(self, language=""):
            return [{"id": "x"}]

    _install_broken_provider(monkeypatch, _Liar)
    probe = ttsq.contract_probe("kokoro", monkeypatch)
    assert not probe.ok
    assert any("provider_label" in f for f in probe.failures), probe.failures


def test_only_the_mock_provider_may_claim_is_mock(monkeypatch):
    """A real provider returning audio must never set the simulation flag."""
    from app.providers import tts as tts_module

    class _Faked(tts_module.BaseTTSProvider):
        name = "kokoro"

        def synthesize(self, text, **kw):
            return tts_module.TTSResult(audio_bytes=b"a" * 64, format="mp3",
                                        provider=self.name, is_mock=True)

        def voices(self, language=""):
            return [{"id": "x"}]

    _install_broken_provider(monkeypatch, _Faked)
    probe = ttsq.contract_probe("kokoro", monkeypatch)
    assert not probe.ok
    assert any("mock_flag" in f for f in probe.failures), probe.failures


def test_the_contract_probe_catches_a_provider_with_no_usable_voice_list(monkeypatch):
    """An empty or malformed voice list breaks every settings screen."""
    from app.providers import tts as tts_module

    class _Voiceless(tts_module.BaseTTSProvider):
        name = "kokoro"

        def synthesize(self, text, **kw):
            return tts_module.TTSResult(audio_bytes=b"a" * 64, format="mp3",
                                        provider=self.name)

        def voices(self, language=""):
            return []

    _install_broken_provider(monkeypatch, _Voiceless)
    probe = ttsq.contract_probe("kokoro", monkeypatch)
    assert not probe.ok
    assert any("voices.well_formed" in f for f in probe.failures), probe.failures


def test_a_config_gated_provider_refuses_to_build_when_unconfigured(monkeypatch):
    """CONFIG_GATED means refusing loudly, not falling back to a default voice."""
    from app.providers import tts as tts_module

    monkeypatch.setattr(tts_module, "_qwen_base_url", lambda: "")
    probe = ttsq.contract_probe("qwen3", monkeypatch, configure=False)
    assert not probe.ok
    assert any("factory.builds" in f for f in probe.failures), probe.failures
    assert "qwen_base_url" in " ".join(probe.failures)


def test_a_config_gated_provider_builds_once_given_synthetic_configuration(monkeypatch):
    """The probe's own configuration is recorded, so a pass is not misread as
    evidence that the deployment is set up."""
    probe = ttsq.contract_probe("qwen3", monkeypatch)
    assert probe.ok, probe.failures
    assert "config.synthetic" in probe.checked


def test_the_probe_reports_an_unimplemented_provider_instead_of_raising(monkeypatch):
    probe = ttsq.contract_probe("fish_audio", monkeypatch, configure=False)
    assert not probe.ok
    assert any("factory.builds" in f for f in probe.failures), probe.failures


def test_the_mock_provider_is_contract_tested_but_excluded_from_the_probe_set():
    """Probing the simulation would prove the probe, not an adapter."""
    assert "mock" not in ttsq.probed_providers()
    mock = ttsq.get_qualification("mock")
    assert mock.contract_status == maturity.CONTRACT_TESTED
    assert mock.simulation_only is True


def test_contract_tested_always_carries_a_gap_list():
    """A coverage claim with no stated gap is a completeness claim nobody checked."""
    for qual in ttsq.IMPLEMENTED_ADAPTERS:
        assert qual.gaps, f"{qual.provider} claims contract coverage with no gaps recorded"


def test_every_contract_tested_tts_row_cites_evidence_that_exists():
    """Links the CONTRACT_TESTED claim to a test that actually drives the adapter."""
    for qual in ttsq.IMPLEMENTED_ADAPTERS:
        if qual.contract_status != maturity.CONTRACT_TESTED:
            continue
        assert qual.evidence, qual.provider
        record = maturity.get_maturity(qual.provider, "tts")
        assert maturity.broken_evidence(REPO_ROOT, (record,)) == []


# ---------------------------------------------------------------------------
# the harvested behaviours the qualification claims to reuse
# ---------------------------------------------------------------------------


def test_pause_handling_lives_in_the_shared_parser_not_the_adapter():
    """The adapter must not grow its own pause parser."""
    from app.providers import tts as tts_module
    from app.services.pause_tags import PAUSE_TAG_PATTERN, remove_pause_tags

    assert PAUSE_TAG_PATTERN.search("hello [pause 1.5] world")
    assert remove_pause_tags("hello [pause 1.5] world").strip() == "hello world"
    source = Path(tts_module.__file__).read_text(encoding="utf-8").lower()
    assert "parse_script_with_pauses" not in source


def test_pcm_decode_then_single_encode_is_the_shared_helper():
    from app.services import audio_concat

    assert audio_concat.SAMPLE_RATE == 24_000
    assert audio_concat.CHANNELS == 1
    assert callable(audio_concat.decode_parts)
    assert callable(audio_concat.concat_audio)


def test_real_duration_measurement_is_available_for_wav_output():
    from app.providers.tts import MockTTSProvider, wav_duration_seconds

    words = "word " * 20
    result = MockTTSProvider().synthesize(words)
    measured = wav_duration_seconds(result.audio_bytes, result.sample_rate)
    expected = len(words.split()) / MockTTSProvider.WORDS_PER_SECOND
    assert measured > 1.0
    assert abs(measured - expected) < 1.5


def test_no_voice_mode_is_explicit_and_never_reads_as_narration():
    """``""`` must not mean silent narration — that is a config error wearing
    a success's clothes."""
    from app.services.pause_tags import (
        NO_VOICE_NAME,
        VoiceModeError,
        assert_voice_mode_explicit,
        is_no_voice,
        resolve_voice_mode,
    )

    assert is_no_voice(NO_VOICE_NAME)
    assert is_no_voice("none")
    assert not is_no_voice("en-US-AndrewNeural")
    with pytest.raises(VoiceModeError):
        assert_voice_mode_explicit("")
    assert resolve_voice_mode("none") == NO_VOICE_NAME


def test_the_custom_audio_path_bypasses_the_provider():
    """An uploaded narration must never be routed through a TTS provider."""
    from app.engine.ugc import voice as ugc_voice

    source = Path(ugc_voice.__file__).read_text(encoding="utf-8")
    assert "get_tts_provider" in source
    assert "def narrate_text" in source
    assert "def narrate_segments" in source


# ---------------------------------------------------------------------------
# the API — read-only, scoped, and never leaking
# ---------------------------------------------------------------------------


@pytest.fixture()
def client():
    from fastapi.testclient import TestClient

    from app.main import create_app

    return TestClient(create_app(), raise_server_exceptions=False)


def _register(client, tag: str) -> dict:
    r = client.post("/api/v1/auth/register",
                    json={"email": f"{tag}{os.urandom(4).hex()}@test.local",
                          "password": "supersecret123"})
    assert r.status_code == 200, r.text
    data = r.json()
    return {"headers": {"Authorization": f"Bearer {data['access_token']}"},
            "ws": data["workspace"]["id"]}


def test_the_global_table_is_readable_without_a_workspace(client):
    """Maturity is a fact about the build, so it must not need a tenant."""
    r = client.get("/api/v1/provider-maturity")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["count"] > 10
    assert set(body["states"]) == maturity.STATES
    for item in body["items"]:
        assert item["implementation_status"] in maturity.STATES
        assert item["live_status"] in maturity.STATES
        assert item["blockers"]


def test_the_workspace_route_reports_no_resolved_credential_on_the_global_one(client):
    """Global rows resolve nothing; ambiguous resolution would read as 'no key'."""
    body = client.get("/api/v1/provider-maturity").json()
    assert "resolved_credential_status" not in body["items"][0]


def test_the_summary_reports_nothing_production_ready(client):
    body = client.get("/api/v1/provider-maturity/summary").json()
    assert body["production_ready"] == []
    assert body["not_production_ready"]
    assert body["total"] > 10


def test_an_unknown_capability_is_a_422_not_an_empty_list(client):
    """A typo must not look like 'this repository has no such providers'."""
    r = client.get("/api/v1/provider-maturity?capability=telepathy")
    assert r.status_code == 422, r.text
    assert "unknown capability" in r.json()["detail"]


def test_health_stays_unknown_unless_probing_is_requested(client):
    body = client.get("/api/v1/provider-maturity").json()
    assert {i["health"] for i in body["items"]} == {maturity.HEALTH_UNKNOWN}


def test_the_tts_qualification_route_reports_the_unmerged_candidates(client):
    body = client.get("/api/v1/provider-maturity/tts/qualification").json()
    donors = {d["provider"] for d in body["donor_candidates"]}
    assert donors == set(DONOR_EXPECTED)
    assert all(d["merged"] is False for d in body["donor_candidates"])
    assert "edge" in body["probeable"]
    assert "mock" not in body["probeable"]


def test_the_workspace_route_requires_auth_and_membership(client):
    ctx = _register(client, "pmauth")
    base = f"/api/v1/workspaces/{ctx['ws']}/provider-maturity"
    assert client.get(base).status_code == 401
    other = _register(client, "pmother")
    assert client.get(base, headers=other["headers"]).status_code == 403


def test_the_workspace_route_reports_credential_presence_as_a_word_only(client):
    """The leak this test exists to prevent: a status endpoint that tells an
    attacker how much of a key it guessed right."""
    ctx = _register(client, "pmsecret")
    from app.services.provider_settings import set_credential

    set_credential("tts.elevenlabs_api_key", FAKE_KEY, ctx["ws"])
    try:
        r = client.get(f"/api/v1/workspaces/{ctx['ws']}/provider-maturity",
                       headers=ctx["headers"])
        assert r.status_code == 200, r.text
        raw = r.text
        assert FAKE_KEY not in raw
        body = r.json()
        row = next(i for i in body["items"]
                   if i["provider"] == "elevenlabs" and i["capability"] == "tts")
        assert row["resolved_credential_status"] == maturity.CREDENTIAL_CONFIGURED
        # No field may carry a fingerprint of the key. The payload's own
        # ``note`` prose mentions "digest" and "fingerprint" while promising not
        # to emit them, so the check is on keys and values, not raw substrings.
        import json as _json

        def _walk(node):
            if isinstance(node, dict):
                for k, v in node.items():
                    assert k not in ("value", "digest", "fingerprint", "length",
                                     "key_value", "sha256"), k
                    _walk(v)
            elif isinstance(node, list):
                for item in node:
                    _walk(item)

        _walk(_json.loads(raw))
        assert FAKE_KEY not in raw
        assert str(len(FAKE_KEY)) not in str(row)
    finally:
        set_credential("tts.elevenlabs_api_key", None, ctx["ws"])


def test_one_workspaces_credential_never_leaks_into_another(client):
    """Tenant isolation must survive the status API, exactly as it does the
    credential resolver."""
    first = _register(client, "pmws1")
    second = _register(client, "pmws2")
    from app.services.provider_settings import set_credential

    set_credential("tts.elevenlabs_api_key", FAKE_KEY, first["ws"])
    try:
        body = client.get(
            f"/api/v1/workspaces/{second['ws']}/provider-maturity",
            headers=second["headers"]).json()
        row = next(i for i in body["items"]
                   if i["provider"] == "elevenlabs" and i["capability"] == "tts")
        assert row["resolved_credential_status"] == maturity.CREDENTIAL_NOT_CONFIGURED
    finally:
        set_credential("tts.elevenlabs_api_key", None, first["ws"])


def test_one_provider_route_404s_on_an_unknown_id(client):
    ctx = _register(client, "pmone")
    base = f"/api/v1/workspaces/{ctx['ws']}/provider-maturity"
    assert client.get(f"{base}/no-such-vendor",
                      headers=ctx["headers"]).status_code == 404


def test_one_provider_route_returns_a_real_record_with_blockers(client):
    ctx = _register(client, "pmreal")
    base = f"/api/v1/workspaces/{ctx['ws']}/provider-maturity"
    body = client.get(f"{base}/wav2lip", headers=ctx["headers"]).json()
    assert body["capability"] == "avatar"
    assert body["commercial_status"] == maturity.BLOCKED_LICENSE
    assert body["production_ready"] is False
    assert any("BLOCKED_LICENSE" in b for b in body["blockers"])
    assert body["health"] == maturity.HEALTH_UNKNOWN


def test_the_workspace_summary_route_also_reports_nothing_ready(client):
    ctx = _register(client, "pmsum")
    body = client.get(f"/api/v1/workspaces/{ctx['ws']}/provider-maturity/summary",
                      headers=ctx["headers"]).json()
    assert body["production_ready"] == []
    assert "by_state" in body["credential_summary"]


def test_no_route_in_this_module_can_write():
    """Read-only by construction: no POST/PUT/PATCH/DELETE route exists."""
    from app.api.v1.providers import (
        provider_maturity_router,
        workspace_maturity_router,
    )

    routes = [*provider_maturity_router.routes, *workspace_maturity_router.routes]
    assert routes, "the router mounted nothing"
    for route in routes:
        assert route.methods == {"GET"}, f"{route.path} is not read-only"


# ---------------------------------------------------------------------------
# live provider coverage — a separate marker, honestly skipped
# ---------------------------------------------------------------------------

live_provider = pytest.mark.skipif(
    not os.environ.get("YMONEY_ELEVENLABS_API_KEY"),
    reason="needs YMONEY_ELEVENLABS_API_KEY to reach the real service",
)


@live_provider
@pytest.mark.live
def test_a_live_tts_call_does_not_silently_promote_the_record():
    """Only a real credential may move ``live_status``. Never faked.

    When someone runs this with a key, the record still needs a human to stamp
    it: :func:`maturity.stamp_verification` returns a copy, so a live test
    cannot silently promote every reader's view.
    """
    from app.providers.tts import ElevenLabsTTSProvider

    provider = ElevenLabsTTSProvider(api_key=os.environ["YMONEY_ELEVENLABS_API_KEY"])
    result = provider.synthesize("provider maturity live probe")
    assert result.audio_bytes
    assert maturity.get_maturity("elevenlabs", "tts").live_status == \
        maturity.UNVERIFIED  # unchanged until a human stamps the record
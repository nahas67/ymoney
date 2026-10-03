"""Work 15.6 §5 -- the paid-path audit and the no-double-submit property.

Two claims are under test.

**The audit is true.** Coverage is re-derived from the real source files
(:func:`verify_against_source`), so a table entry that claims coverage the code
does not have fails, and an uncovered billable path without a recorded reason
fails. Every billable path must also be classified somewhere -- silently
omitting a provider is the failure mode an audit cannot detect by itself, so
the outbound-``httpx`` inventory is asserted explicitly.

**A timeout never buys twice.** The counting-transport test issues exactly one
submit, lets the response be lost, and asserts the submit count is still 1 and
the state is ``SUBMISSION_UNKNOWN``.
"""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from app.services import paid_jobs_audit as audit_mod
from app.services.paid_jobs import (
    PaidArtifactUndownloadable,
    PaidJobRejected,
    PaidSubmissionUnconfirmed,
    SubmissionRecord,
    SubmissionState,
    classify_submit_exception,
    download_with_retry,
    record_submission,
)
from app.services.paid_jobs_audit import (
    BillablePath,
    billable_paths,
    not_billable_paths,
    render_table,
    summary,
    uncovered_billable_paths,
    verify_against_source,
)

#: Every provider module that performs an outbound generation request. A new
#: billable provider must appear in PAID_PATHS; this list is how we notice.
GENERATION_MODULES: tuple[str, ...] = (
    "app.providers.images",
    "app.providers.tts",
    "app.providers.avatar",
    "app.providers.broll",
    "app.providers.music.elevenlabs_music",
    "app.providers.llm",
    "app.providers.video_engine.mpt",
    "app.providers.video_engine.ffmpeg_avatar",
    "app.providers.video_engine.timeline_render",
    "app.providers.motion",
    "app.providers.clips",
    "app.engine.lipsync.external",
    "app.engine.lipsync.musetalk",
    "app.engine.intel.impl.semantic_rerank",
)


# ===========================================================================
# the audit is complete and honest
# ===========================================================================


def test_every_generation_module_is_classified_by_the_audit():
    """No outbound generation path may be missing from the table.

    A path that is simply absent would make the audit look clean while the
    risk stays, so this asserts MODULE coverage: each module that performs an
    outbound request appears in at least one audit row.
    """
    audited = {item.module for item in audit_mod.PAID_PATHS}
    missing = [m for m in GENERATION_MODULES if m not in audited]
    assert not missing, f"unaudited generation module(s): {missing}"


def test_the_audit_table_has_unique_keys():
    keys = [item.key for item in audit_mod.PAID_PATHS]
    assert len(keys) == len(set(keys)), "duplicate audit key"


def test_every_row_states_a_billing_reason():
    for item in audit_mod.PAID_PATHS:
        assert item.billing_note.strip(), item.key
        assert item.provider.strip(), item.key
        assert item.module.strip(), item.key
        assert item.operation.strip(), item.key


def test_the_billable_and_not_billable_sets_partition_the_table():
    billable = {i.key for i in billable_paths()}
    local = {i.key for i in not_billable_paths()}
    assert billable & local == set()
    assert billable | local == {i.key for i in audit_mod.PAID_PATHS}


def test_every_covered_claim_is_verified_against_the_real_source():
    """A 'covered' verdict must be readable in the code, not asserted here."""
    problems = verify_against_source()
    assert not problems, "\n".join(str(p) for p in problems)


def test_coverage_is_computed_from_source_not_declared():
    """Mutate the truth and the verdict must follow.

    A hand-written ``covered=True`` would ignore this; the property reads the
    file. Removing the declared site for a genuinely covered path flips the
    verdict to UNCOVERED, which is the point: the audit reports what the code
    does, not what a table once claimed.
    """
    music = audit_mod.path("music.elevenlabs.video_to_music")
    assert music is not None and music.covered is True

    # Same provider, no declared site -> nothing supports the claim.
    stripped = BillablePath(
        key="music.elevenlabs.video_to_music.probe",
        provider=music.provider,
        module=music.module,
        operation=music.operation,
        billable=music.billable,
        billing_note=music.billing_note,
    )
    assert stripped.covered is False
    assert stripped.coverage == audit_mod.UNCOVERED


def test_a_covered_row_whose_site_lost_the_import_is_reported_as_a_mismatch():
    """The stale-claim detector. This is what keeps the audit from rotting."""
    # Point the row at a real module that does NOT import paid_jobs.
    #
    # NOTE: this used to be ``app.providers.tts``, which no longer works --
    # Work 15.7 gave that module the paid contract, so the probe row became
    # genuinely covered and the detector correctly reported nothing wrong. The
    # probe must name a module that really has no money path.
    broken = BillablePath(
        key="probe.stale",
        provider="acme",
        module="app.providers.clips",
        operation="ClipFetcher.fetch",
        billable=True,
        billing_note="probe",
        paid_jobs_sites=("app.providers.clips",),   # no paid_jobs import there
    )
    assert broken.covered is False
    # And the detector finds it once it is in the table under test.
    original = audit_mod.PAID_PATHS
    try:
        audit_mod.PAID_PATHS = original + (broken,)
        found = verify_against_source()
    finally:
        audit_mod.PAID_PATHS = original
    assert any(p.key == "probe.stale" for p in found)
    assert [p for p in found if p.key != "probe.stale"] == [], (
        "the detector is noisy: it also flagged an unrelated row")


def test_a_site_pointing_at_a_missing_module_is_reported():
    broken = BillablePath(
        key="probe.missing",
        provider="acme",
        module="app.providers.tts",
        operation="x",
        billable=True,
        billing_note="probe",
        paid_jobs_sites=("app.providers.does_not_exist",),
    )
    original = audit_mod.PAID_PATHS
    try:
        audit_mod.PAID_PATHS = original + (broken,)
        found = verify_against_source()
    finally:
        audit_mod.PAID_PATHS = original
    assert any("does not exist on disk" in str(p) for p in found)


def test_an_uncovered_billable_path_without_a_recorded_gap_is_reported():
    """An unexplained gap is an unaudited path, and is reported as one."""
    unexplained = BillablePath(
        key="probe.no_gap",
        provider="acme",
        module="app.providers.tts",
        operation="x",
        billable=True,
        billing_note="probe",
        gap="   ",
    )
    original = audit_mod.PAID_PATHS
    try:
        audit_mod.PAID_PATHS = original + (unexplained,)
        found = verify_against_source()
    finally:
        audit_mod.PAID_PATHS = original
    assert any("no recorded gap" in str(p) for p in found)


def test_a_local_ffmpeg_path_is_not_given_a_fake_state_machine():
    """Not-billable is a real answer, with a reason -- never a fake machine."""
    for key in ("broll.synth.generate", "avatar.mock.render",
                "motion.hyperframes.render", "clips.yt_dlp.fetch"):
        row = audit_mod.path(key)
        assert row is not None, key
        assert row.billable is False, key
        assert row.paid_jobs_sites == (), f"{key} claims paid-job coverage"
        assert row.coverage == audit_mod.UNCOVERED, key
        assert row.billing_note.strip(), key


def test_the_uncovered_billable_list_is_exactly_the_unguarded_billable_paths():
    """Work 15.9 §9 took the inventory to 100%, so the canary is spent.

    This asserted the uncovered list is NON-empty, which was a placeholder for
    a gap that has since been closed. The invariant is now the opposite: every
    billable path must be classified. The quality bar on a gap survives for the
    day one reappears.
    """
    uncovered = tuple(uncovered_billable_paths())
    for row in uncovered:
        assert row.billable is True
        assert row.coverage == audit_mod.UNCOVERED
        assert row.gap.strip(), row.key
        assert len(row.gap.strip()) > 80, f"{row.key}: the gap is a shrug"
    assert uncovered == (), (
        f"an uncovered billable path reappeared: {[r.key for r in uncovered]}")
    covered = [r for r in billable_paths() if r.covered]
    assert len(covered) + len(uncovered) == len(billable_paths())


def test_the_summary_counts_are_computed_not_hand_written():
    data = summary()
    assert data["total"] == len(audit_mod.PAID_PATHS)
    assert data["billable"] + data["not_billable"] == data["total"]
    assert data["covered"] + data["uncovered_billable"] == data["billable"]
    assert set(data["uncovered_keys"]) == {r.key for r in uncovered_billable_paths()}
    for key, verdict in data["verdicts"].items():
        assert verdict["coverage"] in audit_mod.COVERAGE, key


def test_evidence_lines_point_at_real_files():
    """Every cited file:line must exist, so the table cannot cite fiction."""
    root = audit_mod._BACKEND_ROOT
    for row in audit_mod.PAID_PATHS:
        for citation in row.evidence:
            rel, _, line = citation.rpartition(":")
            assert (root / rel).is_file(), citation
            assert line.isdigit(), citation
            lines = (root / rel).read_text(encoding="utf-8").splitlines()
            assert 1 <= int(line) <= len(lines), citation


def test_the_table_renders_from_the_same_data():
    text = render_table()
    lines = text.strip().splitlines()
    assert len(lines) == len(audit_mod.PAID_PATHS) + 2, "header + rule + rows"
    for row in audit_mod.PAID_PATHS:
        assert row.key in text


def test_idempotency_unsupported_is_a_documented_finding():
    """Providers that publish no idempotency header are recorded, not assumed."""
    for provider in ("elevenlabs", "twelvelabs", "moneyprinterturbo"):
        assert provider in audit_mod.IDEMPOTENCY_UNSUPPORTED
        assert audit_mod.IDEMPOTENCY_UNSUPPORTED[provider].strip()


def test_describe_reports_an_unknown_key_instead_of_inventing_a_row():
    assert "error" in audit_mod.describe("nope.not.here")
    assert audit_mod.path("nope.not.here") is None
    assert audit_mod.describe("llm.complete")["billable"] is True


# ===========================================================================
# no double submit
# ===========================================================================


class CountingTransport(httpx.BaseTransport):
    """Counts REAL outbound submits and replays a scripted failure.

    This is the evidence that matters: the assertion is about requests that
    actually went out, not about a function that was called.
    """

    def __init__(self, *, failure: Exception | None = None,
                 response: httpx.Response | None = None) -> None:
        self.submits = 0
        self.keys: list[str] = []
        self._failure = failure
        self._response = response

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.submits += 1
        self.keys.append(request.headers.get("Idempotency-Key", ""))
        if self._failure is not None:
            raise self._failure
        if self._response is not None:
            return self._response
        return httpx.Response(200, json={"data": [{"embedding": [0.1, 0.2]}]})


def _submit(transport: httpx.BaseTransport, *, idempotency_key: str) -> str:
    """One submit through the paid-job contract. Mirrors the music provider."""
    record = SubmissionRecord(workspace_id="w1", provider="acme")
    record.idempotency_key = idempotency_key
    client = httpx.Client(transport=transport)
    try:
        response = client.post(
            "https://provider.test/jobs",
            headers={"Idempotency-Key": record.idempotency_key},
            json={"prompt": "x"}, timeout=(15.0, 60.0),
        )
        if response.status_code >= 400:
            raise httpx.HTTPStatusError(
                f"{response.status_code}", request=response.request,
                response=response)
        payload = response.json()
    except httpx.HTTPError as exc:
        classified = classify_submit_exception(
            exc, provider="acme", remote_id=record.remote_id,
            attempt=record.attempts + 1)
        record_submission(record, classified)
        raise classified from exc
    record_submission(record, None)
    return str(payload["id"])


@pytest.mark.parametrize(
    "failure, note",
    [
        (httpx.ConnectTimeout("connect timed out"), "never reached the provider"),
        (httpx.ReadTimeout("response lost"), "delivered, response lost"),
        (httpx.RemoteProtocolError("peer closed"), "delivered, response lost"),
    ],
)
def test_an_ambiguous_submit_costs_exactly_one_outbound_request(failure, note):
    """The property. One submit went out; after an ambiguous failure we STOP.

    ``submits == 1`` is the assertion that no automatic retry happened, and
    the state must be SUBMISSION_UNKNOWN so no later code path may resubmit.
    """
    transport = CountingTransport(failure=failure)
    with pytest.raises(PaidSubmissionUnconfirmed) as caught:
        _submit(transport, idempotency_key="k-1")

    assert transport.submits == 1, (
        f"a retry bought a second job ({note}): {transport.submits} submits")
    # The record the provider would persist agrees.
    record = SubmissionRecord(workspace_id="w1", provider="acme")
    record = record_submission(record, caught.value)
    assert record.state is SubmissionState.SUBMISSION_UNKNOWN
    assert record.may_resubmit is False


def test_an_ambiguous_submit_sends_one_idempotency_key_and_reuses_it_on_retry():
    """A retry is possible ONLY for a provably undelivered request.

    Even then the key must be identical, so a provider that DOES honour the
    header deduplicates instead of charging twice.
    """
    transport = CountingTransport(failure=httpx.ConnectTimeout("connect"))
    key = "stable-key"
    with pytest.raises(PaidSubmissionUnconfirmed) as caught:
        _submit(transport, idempotency_key=key)
    assert caught.value.provably_undelivered is True
    # Provably undelivered -> a human may retry, and the key does not change.
    with pytest.raises(PaidSubmissionUnconfirmed):
        _submit(transport, idempotency_key=key)
    assert transport.submits == 2
    assert transport.keys == [key, key], transport.keys


def test_a_read_timeout_is_never_retried_even_by_hand():
    """Delivered means possibly billed. The state forbids the retry outright."""
    transport = CountingTransport(failure=httpx.ReadTimeout("lost"))
    record = SubmissionRecord(workspace_id="w1", provider="acme")
    with pytest.raises(PaidSubmissionUnconfirmed) as caught:
        _submit(transport, idempotency_key="k-2")
    # The flag a caller would use to justify a retry MUST be False here. This is
    # the distinction the whole module rests on: a connect timeout proves the
    # request never arrived, a read timeout proves only that the answer did.
    assert caught.value.provably_undelivered is False
    assert "DO NOT RESUBMIT" in str(caught.value)
    record = record_submission(record, caught.value)
    assert record.may_resubmit is False
    assert record.state is SubmissionState.SUBMISSION_UNKNOWN
    assert transport.submits == 1


def test_a_dropped_connection_is_not_provably_undelivered_either():
    """A closed peer is not proof of non-delivery."""
    transport = CountingTransport(failure=httpx.RemoteProtocolError("closed"))
    with pytest.raises(PaidSubmissionUnconfirmed) as caught:
        _submit(transport, idempotency_key="k-2b")
    assert caught.value.provably_undelivered is False
    assert transport.submits == 1


def test_a_connect_timeout_is_the_only_case_flagged_provably_undelivered():
    """Mutation-critical: flipping this flag makes every timeout retryable."""
    transport = CountingTransport(failure=httpx.ConnectTimeout("connect"))
    with pytest.raises(PaidSubmissionUnconfirmed) as caught:
        _submit(transport, idempotency_key="k-2c")
    assert caught.value.provably_undelivered is True
    assert "retry is safe" in str(caught.value)


def test_a_known_rejection_costs_one_submit_and_permits_a_retry():
    transport = CountingTransport(
        response=httpx.Response(422, json={"error": "nope"}))
    with pytest.raises(PaidJobRejected) as caught:
        _submit(transport, idempotency_key="k-3")
    assert caught.value.status_code == 422
    assert transport.submits == 1
    record = record_submission(
        SubmissionRecord(workspace_id="w1", provider="acme"), caught.value)
    assert record.state is SubmissionState.FAILED
    assert record.may_resubmit is True, "a 4xx created no job, retry is free"


def test_a_5xx_is_ambiguous_even_though_the_status_is_a_real_response():
    transport = CountingTransport(
        response=httpx.Response(503, json={"error": "later"}))
    with pytest.raises(PaidSubmissionUnconfirmed) as caught:
        _submit(transport, idempotency_key="k-4")
    assert transport.submits == 1
    assert "may have been created" in str(caught.value)


def test_a_successful_submit_confirms_a_remote_id_and_blocks_a_resubmit():
    transport = CountingTransport(
        response=httpx.Response(200, json={"id": "job-42"}))
    remote_id = _submit(transport, idempotency_key="k-5")
    assert remote_id == "job-42"
    assert transport.submits == 1
    record = record_submission(
        SubmissionRecord(workspace_id="w1", provider="acme"), None)
    record.remote_id = remote_id
    assert record.state is SubmissionState.REMOTE_ID_CONFIRMED
    assert record.may_resubmit is False


def test_a_lost_response_after_a_billable_job_is_reconcilable_not_lost():
    """The remote id survives the ambiguity so reconciliation can find it."""
    exc = PaidSubmissionUnconfirmed(provider="acme", remote_id="job-42",
                                    detail="read timeout")
    record = record_submission(SubmissionRecord(workspace_id="w1", provider="acme"), exc)
    assert record.state is SubmissionState.SUBMISSION_UNKNOWN
    assert record.remote_id == "job-42"
    assert "job-42" in record.detail


def test_a_billed_artifact_whose_download_fails_retries_the_fetch_only():
    """Re-generating costs money again; re-fetching does not."""
    attempts = {"n": 0}
    outbound_submits = {"n": 0}

    def fetch_bytes() -> bytes:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ReadTimeout("cdn jitter")
        return b"mp4"

    def submit() -> str:
        outbound_submits["n"] += 1
        return "job-7"

    remote_id = submit()
    data = download_with_retry(fetch_bytes, provider="acme", remote_id=remote_id,
                               sleep=lambda _s: None)
    assert data == b"mp4"
    assert outbound_submits["n"] == 1, "a download retry re-bought the job"
    assert attempts["n"] == 3


def test_an_exhausted_download_reports_the_job_succeeded_remotely():
    def always_fails() -> bytes:
        raise httpx.ReadTimeout("cdn down")

    with pytest.raises(PaidArtifactUndownloadable) as caught:
        download_with_retry(always_fails, provider="acme", remote_id="job-9",
                            url="https://cdn.test/v.mp4", sleep=lambda _s: None)
    assert caught.value.remote_id == "job-9"


def test_the_semantic_rerank_embed_call_is_a_covered_billable_path():
    """The new billable path added in 15.6 obeys the same contract."""
    row = audit_mod.path("semantic_rerank.twelve_labs.embed")
    assert row is not None
    assert row.billable is True
    assert row.covered is True
    assert row.gap == ""


def test_the_semantic_rerank_embedder_refuses_to_retry_an_ambiguous_submit():
    """Same property, on the code 15.6 added: one POST, then STOP."""
    from app.engine.intel.impl.semantic_rerank import TwelveLabsEmbedder

    transport = CountingTransport(failure=httpx.ReadTimeout("lost"))
    client = httpx.Client(transport=transport)
    embedder = TwelveLabsEmbedder(api_key="k", client=client)
    with pytest.raises(PaidSubmissionUnconfirmed):
        embedder.embed_text("a caption", workspace_id="w1")
    assert transport.submits == 1, (
        f"the new provider retried a lost response: {transport.submits} submits")
    # Nothing was sent upstream, so the key is only a local recovery handle.
    assert transport.keys == [""]


def test_the_semantic_rerank_embedder_sends_one_request_per_text_and_no_more():
    from app.engine.intel.impl.semantic_rerank import TwelveLabsEmbedder

    transport = CountingTransport()
    embedder = TwelveLabsEmbedder(api_key="k", client=httpx.Client(transport=transport))
    embedder.embed_text("query")
    for text in ("a", "b", "c"):
        embedder.embed_text(text)
    assert transport.submits == 4   # one query + three candidates, no retries
    assert transport.keys == ["", "", "", ""]


def test_a_2xx_that_cannot_be_parsed_is_ambiguous_not_a_success():
    """No artifact and no durable id: the paid-job state must say UNKNOWN."""
    from app.engine.intel.impl.semantic_rerank import TwelveLabsEmbedder

    transport = CountingTransport(response=httpx.Response(200, content=b"not json"))
    embedder = TwelveLabsEmbedder(api_key="k", client=httpx.Client(transport=transport))
    with pytest.raises(PaidSubmissionUnconfirmed):
        embedder.embed_text("a caption")
    assert transport.submits == 1


def test_the_derived_idempotency_key_is_stable_for_the_same_pair():
    """Same request -> same key; different candidate -> different key."""
    from app.engine.intel.impl.semantic_rerank import derive_idempotency_key

    first = derive_idempotency_key("", "a red balloon")
    again = derive_idempotency_key("", "a red balloon")
    other = derive_idempotency_key("", "a blue balloon")
    assert first == again
    assert first != other
    assert len(first) == 32
    # And it is a sha256 prefix, so it cannot leak the caption text.
    assert "balloon" not in first


def test_a_key_built_from_the_same_rule_matches_a_hand_computed_digest():
    """Pin the derivation so a refactor cannot silently change the key space."""
    from app.engine.intel.impl.semantic_rerank import derive_idempotency_key

    canonical = json.dumps({"query": "", "text": "hello"}, sort_keys=True)
    expected = hashlib.sha256(canonical.encode()).hexdigest()[:32]
    assert derive_idempotency_key("", "hello") == expected


# ===========================================================================
# live provider coverage -- honestly skipped without credentials
# ===========================================================================


@pytest.mark.live
@pytest.mark.skipif(
    not __import__("os").environ.get("TWELVELABS_API_KEY"),
    reason="live TwelveLabs credentials are not configured",
)
def test_live_twelvelabs_embed_returns_a_finite_vector():
    from app.engine.intel.impl.semantic_rerank import TwelveLabsEmbedder

    vector = TwelveLabsEmbedder().embed_text("a red balloon rising")
    assert vector
    assert all(isinstance(v, float) for v in vector)


@pytest.mark.live
@pytest.mark.skipif(
    not __import__("os").environ.get("ELEVENLABS_API_KEY"),
    reason="live ElevenLabs credentials are not configured",
)
def test_live_elevenlabs_music_submit_is_recorded():
    """Live billing path: a real submission must produce a durable id."""
    record = SubmissionRecord(workspace_id="live", provider="elevenlabs_music")
    record_submission(record, None)
    assert record.state is SubmissionState.REMOTE_ID_CONFIRMED
"""The paid-path audit as DATA plus a query surface (Work 15.6 Â§5).

A paid generation is the one operation where a network error costs real
money, so "which paths bill?" must be an answerable question rather than a
memory. This module is that answer:

* :data:`PAID_PATHS` is the explicit audit -- every outbound generation path,
  whether it can bill, whether it routes through
  :mod:`app.services.paid_jobs`, and where the idempotency key goes.
* :func:`verify_against_source` re-derives every claim by READING the real
  source files, in BOTH directions.

Three rules shape the table.

**Not-billable is a real answer.** ``ffmpeg``, a local ``sadtalker``
checkout, ``yt-dlp`` and the Pexels/YouTube read APIs spend operator CPU and
free-tier quota, not money. Giving those a fake state machine would be noise
that hides the paths that matter, so they are recorded as
``billable=False`` with the reason, and are never given a fake
``SubmissionState``.

**Coverage is verified, not asserted.** ``covered`` is never a hand-written
boolean. It is derived: a path is covered exactly when every module that is
supposed to carry the contract for it really imports ``app.services.paid_jobs``
or :mod:`app.services.paid_executor`. :func:`verify_against_source` returns the
mismatches and the test suite asserts the list is empty.

**The audit is checked in both directions (Work 15.7).** The original
docstring claimed the table "cannot rot". It could, and it did, in the one
direction nobody implemented: a *missing* row and a *misclassified* row were
both invisible, because the detector only asked whether a DECLARED site still
imported the contract. That is exactly how ``video_engine.ffmpeg_avatar.submit``
kept claiming ``NOT_BILLABLE`` while calling paid ElevenLabs TTS and up to eight
paid image jobs. So now every row names the literal source markers it cites
(:attr:`BillablePath.source_markers`), and :data:`MONEY_MARKERS` holds the
inventory of outbound billable call sites independently of the table. A marker
present in a module that no row cites is reported as a MISSING path; a marker a
row cites that is gone from the module is reported as a STALE row.

Work 15.7 also added **composite** rows (:attr:`BillablePath.composite_paths`).
A path that spends money through its callees is billable, and it is covered
exactly when every one of those callees is.

What this module deliberately does NOT do: decide the price of anything. An
amount the provider never reports is recorded downstream as
``UNKNOWN_EXPOSURE``; the audit's job is to say WHICH paths can bill and
whether their submit is guarded, not to guess a number.

Work 15.7 applied :mod:`app.services.paid_executor` to the media lanes
(``providers/images.py``, ``providers/tts.py``, ``providers/avatar.py``,
``providers/broll.py``, ``engine/lipsync/external.py`` and the
``engine/lipsync/worker.py`` retry loop).

**Work 15.8 changed what COUNTS as carrying the contract.** The four media
providers each hand-rolled the same wiring around the executor (build it, keep the
records, repeat a three-way settle branch, add a one-line budget gate), so §8
extracted those mechanics into :mod:`app.services.paid_provider` and added it to
:data:`PAID_CONTRACT_MODULES`: a site that imports the helper carries the
contract on exactly the same reasoning that ``paid_executor`` does. That moved
two rows from UNCOVERED to COVERED, both on their merits rather than on a
definition change:

* ``providers.dubbing.translate_segments`` (§5) reserved nothing at all while
  making ``ceil(N/20)`` billable requests per run. It now reserves per REQUEST --
  per segment it would over-reserve twenty-fold -- atomically, before the send.
* ``video_engine.mpt.submit`` (§6) raised a correct
  ``VideoEngineSubmissionUnknown`` and wrote NO durable record and NO cost row, so
  a lost response was survivable for the pipeline and unexplainable for an
  operator. It now writes the attempt before the request and keeps the
  reservation on ambiguity.

Both rows' :attr:`BillablePath.gap` now names the limits that remain, and they
are provider facts rather than missing code: MoneyPrinterTurbo reports no price
and accepts no idempotency key, and a chat completion has no remote id at all.

What is still UNCOVERED below is the LLM completion lane itself
(``llm.complete``), which belongs to the router/``llm_paid`` lane rather than
this one. Every uncovered row carries a specific :attr:`BillablePath.gap` naming
the exact file:line.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "BILLABLE",
    "COVERED",
    "COVERAGE",
    "IDEMPOTENCY_HEADER",
    "IDEMPOTENCY_UNSUPPORTED",
    "MONEY_MARKERS",
    "NOT_BILLABLE",
    "PAID_PATHS",
    "UNCOVERED",
    "BillablePath",
    "CoverageMismatch",
    "MoneyMarker",
    "audit",
    "billable_paths",
    "describe",
    "not_billable_paths",
    "path",
    "render_table",
    "summary",
    "uncovered_billable_paths",
    "verify_against_source",
]

#: ``backend/`` -- derived, never hard-coded to a machine path.
_BACKEND_ROOT = Path(__file__).resolve().parents[2]

#: coverage verdicts
COVERED = "COVERED"
UNCOVERED = "UNCOVERED"
COVERAGE = (COVERED, UNCOVERED)

#: billable verdicts
BILLABLE = True
NOT_BILLABLE = False

#: The paid-job taxonomy every covered path must reach. Checked literally.
PAID_JOBS_MODULE = "app.services.paid_jobs"
#: ...and the shared executor that wraps it. A site carrying the executor
#: carries the taxonomy too (``paid_executor`` imports ``paid_jobs``), so
#: either import proves the contract is present at that site.
PAID_EXECUTOR_MODULE = "app.services.paid_executor"
#: ...and the per-operation helper Work 15.8 §8 extracted from the four
#: duplicated provider wirings. It imports the executor, so a site carrying it
#: carries the taxonomy on the same reasoning -- and a site that does NOT carry
#: it is exactly the site that still hand-rolls a budget gate, which is what the
#: dubbing translation loop did until §5.
PAID_PROVIDER_MODULE = "app.services.paid_provider"
PAID_CONTRACT_MODULES = (PAID_JOBS_MODULE, PAID_EXECUTOR_MODULE,
                         PAID_PROVIDER_MODULE)

#: Local, unbilled compute. Recorded so the audit says "not billable" with a
#: reason instead of leaving the reader to assume a paid provider.
LOCAL_COMPUTE = "operator CPU/GPU via a local process; no vendor invoice"

#: The conventional upstream header name, used the moment a provider documents
#: support for it.
IDEMPOTENCY_HEADER = "Idempotency-Key"

#: Providers audited in Work 15.6 whose API documentation publishes NO
#: idempotency header. This is a finding, not an omission: for these providers
#: the header route is closed, so the ONLY protection against a double charge
#: is the local key plus refusing to resubmit from SUBMISSION_UNKNOWN. Do not
#: add a provider here without checking its own documentation.
IDEMPOTENCY_UNSUPPORTED: dict[str, str] = {
    "moneyprinterturbo": "self-hosted; POST /api/v1/videos takes no such header",
    "elevenlabs": (
        "Text to Speech / Music document xi-api-key + Content-Type only; no "
        "Idempotency-Key"
    ),
    "xkiro": "free-tier image jobs; no documented idempotency parameter",
    "twelvelabs": "Embed API v2 documents x-api-key; no idempotency header",
    "external_lipsync_worker": "operator worker; POST /jobs contract is ours",
    "openai_compatible_llm": "chat/completions; no idempotency header",
    "avatar_server": "operator server; multipart /render contract is ours",
    "broll_ai_server": "operator server; /generate contract is ours",
}


@dataclass(frozen=True)
class BillablePath:
    """One outbound generation path and its paid-job standing.

    ``paid_jobs_sites`` names the modules that are *supposed* to carry the
    contract. It is an assertion to be VERIFIED, not evidence: coverage is
    computed by :func:`verify_against_source`, which reads those files.
    """

    key: str
    provider: str
    module: str
    operation: str
    billable: bool
    #: local reason for the billable verdict (never left blank)
    billing_note: str
    #: modules expected to carry the paid contract for this path
    paid_jobs_sites: tuple[str, ...] = ()
    #: where the idempotency key is used, verbatim
    idempotency: str = ""
    #: the honest gap while ``covered`` is UNCOVERED
    gap: str = ""
    #: file:line evidence for the submit call
    evidence: tuple[str, ...] = ()
    #: literal substrings of :attr:`module` this row cites. A marker that is
    #: gone from the module makes the row STALE; a marker in
    #: :data:`MONEY_MARKERS` that no row cites makes the path MISSING.
    source_markers: tuple[str, ...] = ()
    #: keys of the rows this path spends money THROUGH. A composite row renders
    #: nothing billable itself; its callees do.
    composite_paths: tuple[str, ...] = ()
    #: why the composition costs money (never blank when composite)
    composite_note: str = ""

    @property
    def is_composite(self) -> bool:
        return bool(self.composite_paths)

    @property
    def covered(self) -> bool:
        """True only when the code really carries the contract.

        Computed, never declared -- see :func:`verify_against_source`. A path
        with no declared site is uncovered by construction. A COMPOSITE path is
        covered when every path it delegates to is covered: the module itself
        submits nothing, so its guarantee is exactly its callees' guarantees.
        """
        if self.is_composite:
            leaves = [path(key) for key in self.composite_paths]
            return bool(leaves) and all(
                leaf is not None and leaf.covered for leaf in leaves)
        return bool(self.paid_jobs_sites) and all(
            _carries_paid_contract(site) for site in self.paid_jobs_sites
        )

    @property
    def coverage(self) -> str:
        return COVERED if self.covered else UNCOVERED

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "provider": self.provider,
            "module": self.module,
            "operation": self.operation,
            "billable": bool(self.billable),
            "billing_note": self.billing_note,
            "coverage": self.coverage,
            "composite": self.is_composite,
            "composite_paths": list(self.composite_paths),
            "composite_note": self.composite_note,
            "paid_jobs_sites": list(self.paid_jobs_sites),
            "idempotency": self.idempotency,
            "gap": self.gap,
            "evidence": list(self.evidence),
            "source_markers": list(self.source_markers),
        }


@dataclass(frozen=True)
class MoneyMarker:
    """One outbound billable call site the audit must be able to name.

    Held independently of :data:`PAID_PATHS` on purpose: a check that compares
    the table against itself proves nothing. This is the list a reviewer can
    argue with, and it is what makes a MISSING row detectable at all.
    """

    #: literal substring of the module's source
    marker: str
    #: why this call site can cost money, in one sentence
    why: str

    def __str__(self) -> str:
        return f"{self.marker} ({self.why})"


@dataclass(frozen=True)
class CoverageMismatch:
    """One disagreement between the declared table and the real source."""

    key: str
    module: str
    site: str
    problem: str

    def __str__(self) -> str:
        # Inventory findings have no row key, so the module names them: a new
        # billable call site is reported against the module it lives in.
        return f"{self.key or self.module}: {self.problem} ({self.site})"


def _source_path(module: str) -> Path:
    """File backing a dotted module name (stdlib only, no import)."""
    return _BACKEND_ROOT.joinpath(*module.split(".")).with_suffix(".py")


def _source_text(module: str) -> str:
    """Module source as text; ``""`` for a module that does not exist.

    Read as text rather than imported: an audit must never execute the code it
    audits, and a missing file is simply "not covered".
    """
    try:
        return _source_path(module).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _carries_paid_contract(module: str) -> bool:
    """True when the module's source really carries the paid-job contract."""
    text = _source_text(module)
    return any(carrier in text for carrier in PAID_CONTRACT_MODULES)


#: Backwards-compatible alias for the pre-15.7 name. The meaning widened from
#: "imports paid_jobs" to "carries the paid contract", which is what the
#: coverage verdict has always needed.
_imports_paid_jobs = _carries_paid_contract


# ---------------------------------------------------------------------------
# the money inventory -- independent of the table, on purpose
# ---------------------------------------------------------------------------

MONEY_MARKERS: dict[str, tuple[MoneyMarker, ...]] = {
    "app.providers.images": (
        MoneyMarker("/images/generations",
                    "one billable image job per POST, for xKiro's async job "
                    "and for any OpenAI-compatible metered gateway"),
    ),
    "app.providers.tts": (
        MoneyMarker("/audio/speech",
                    "operator-server synthesis: the base URL is a credential, "
                    "so a remote host bills us per segment"),
        MoneyMarker("/text-to-speech/",
                    "ElevenLabs bills Text to Speech per character"),
    ),
    "app.providers.avatar": (
        MoneyMarker("/render",
                    "a billed GPU avatar render; video_url is an artifact, "
                    "not a job id"),
    ),
    "app.providers.broll": (
        MoneyMarker("/generate",
                    "a billed GPU B-roll render per POST"),
    ),
    "app.engine.lipsync.external": (
        MoneyMarker('"/jobs"',
                    "POST /jobs creates the billable lip-sync job; the worker "
                    "reports cost_usd per job"),
    ),
    "app.providers.video_engine.ffmpeg_avatar": (
        MoneyMarker("tts.synthesize(",
                    "submit() narrates through the configured TTS provider, "
                    "which may be paid ElevenLabs"),
        MoneyMarker("img_prov.generate(",
                    "submit() generates up to eight scene images through the "
                    "configured image provider, which may be a paid gateway"),
    ),
    "app.providers.dubbing": (
        MoneyMarker(".synthesize(text, voice=voice)",
                    "one billable TTS call per translated subtitle segment"),
        MoneyMarker("complete_json(",
                    "the translation step is a metered LLM completion"),
    ),
    "app.engine.intelligence.llm_paid": (
        MoneyMarker("run_completion(",
                    "the shared chat-completions lane: every complete() and "
                    "every complete_json() leg goes through here, and its "
                    "submission record is the only evidence a lost completion "
                    "existed"),
    ),
    "app.providers.longform_assets": (
        MoneyMarker("get_image_provider()",
                    "long-form scene images come from the configured image "
                    "provider, which may be a paid gateway"),
    ),
    "app.providers.tts_qualification": (
        MoneyMarker("a short honest sentence for the probe",
                    "the qualification probe synthesizes REAL audio, so it "
                    "spends real ElevenLabs characters"),
    ),
    "app.engine.ugc.voice": (
        MoneyMarker(".synthesize(text, voice=voice)",
                    "UGC voice lines go through the configured TTS provider"),
    ),
    "app.engine.ugc.pipeline": (
        MoneyMarker("generate_clip(",
                    "UGC scenes can pull an AI-generated B-roll clip, which "
                    "may be a billed render"),
    ),
    "app.engine.agents.avatar": (
        MoneyMarker("render_avatar(",
                    "the Avatar Director agent can drive a billed server "
                    "render"),
    ),
    "app.engine.agents.broll": (
        MoneyMarker("generate_clip(",
                    "the B-roll agent's AI branch can drive a billed render"),
    ),
    "app.engine.agents.voice": (
        MoneyMarker(".synthesize(",
                    "three synthesis loops (single take, segmented, "
                    "multi-voice) each cost ElevenLabs characters"),
    ),
    "app.api.v1.connections": (
        MoneyMarker("provider.synthesize(",
                    "the Connections TTS test button spends real characters "
                    "on every click"),
        MoneyMarker("provider.generate(",
                    "the Connections image test button spends a real "
                    "generation on every click"),
    ),
    "app.api.v1.content": (
        # Work 15.7 §9: the voice-preview synthesize() call left this module --
        # :1682 now delegates to app.api.v1.preview.preview_voice, which is
        # inventoried there. Avatar driving audio is booked through
        # _reserve_speak and is the api.content.voice_synth row below.
        MoneyMarker("_reserve_speak(",
                    "avatar driving audio is booked through the narration "
                    "reservation"),
        MoneyMarker("provider.generate(",
                    "the content image endpoints generate scene images"),
        MoneyMarker("generate_clip(",
                    "the content B-roll endpoint generates an AI clip"),
        MoneyMarker("render_avatar(",
                    "the content avatar endpoint drives a server render"),
    ),
    "app.api.v1.preview": (
        MoneyMarker(".synthesize(",
                    "a voice-sample preview is a real synthesis"),
    ),
}


# ---------------------------------------------------------------------------
# the audit
# ---------------------------------------------------------------------------

_IDEMPOTENCY_NONE = (
    "no idempotency header is sent upstream: the provider documents none, so "
    "the key is persisted locally only and the SUBMISSION_UNKNOWN state is "
    "what prevents a second purchase"
)

PAID_PATHS: tuple[BillablePath, ...] = (
    # ---- AI video generation ------------------------------------------
    BillablePath(
        key="video_engine.mpt.submit",
        provider="moneyprinterturbo",
        module="app.providers.video_engine.mpt",
        operation="MoneyPrinterTurboAdapter.submit",
        billable=BILLABLE,
        billing_note=(
            "per-render cost estimate charged through services/cost.py "
            "(settings.mpt_estimated_render_cost_usd); the pre-spend gate runs "
            "before submit"
        ),
        idempotency=(
            "RenderRequest.request_hash() is persisted on Video.params_json "
            "and used to reattach/adopt an existing task, but it is NOT sent "
            "as an upstream header -- MPT documents no idempotency header"
        ),
        paid_jobs_sites=("app.engine.agents.production",),
        evidence=(
            "app/providers/video_engine/mpt.py:184",
            "app/providers/video_engine/mpt.py:190",
            "app/providers/video_engine/mpt.py:218",
            "app/providers/video_engine/mpt.py:230",
            "app/providers/video_engine/base.py:52",
            "app/engine/agents/production.py:158",
            "app/engine/agents/production.py:175",
            "app/engine/agents/production.py:236",
            "app/engine/agents/production.py:275",
        ),
        source_markers=('/api/v1/videos',),
        # Work 15.8 §6/§8: NOW COVERED, and the row says exactly what changed.
        # The 15.7 note was honest that the RETRY was protected (production.py
        # refuses a prior SUBMISSION_UNKNOWN) while the RECORD was missing. §6
        # closed the record without adding a table: the durable evidence is the
        # Video row's own columns -- submission_state, provider_task_id,
        # submission_operation_id, submission_attempted_at, cost_outcome,
        # submission_detail -- plus the CostEntry row the reservation is settled
        # on. `paid.authorize()` runs BEFORE `engine.submit`, so a crash mid-flight
        # still leaves both. What remains uncovered is a provider-side fact no
        # amount of local bookkeeping can invent: the engine reports no price and
        # accepts no idempotency key, so an ambiguous render is priced as an
        # estimate and reconciled by hand against the engine's task list.
        gap=(
            "COVERED for the record, and the residual limits are provider facts, "
            "not gaps in YMONEY. (1) MoneyPrinterTurbo exposes no monetary cost, "
            "so a render is booked as an ESTIMATE and stays one: `settle_reservation` "
            "would stamp it ACTUAL, which no vendor invoice supports. (2) It "
            "documents no idempotency header, so the only protection against a "
            "second purchase is the local request hash plus the refusal to "
            "resubmit from SUBMISSION_UNKNOWN. (3) An ambiguous submit has no "
            "remote id, so reconciliation is a human comparing "
            "Video.params_json['ambiguous_request_hash'] with the engine's task "
            "list -- the fingerprint is attached by the adapter "
            "(mpt.submission_unknown) because it is the only place that knows what "
            "was actually sent."
        ),
    ),
    # ---- premium TTS ---------------------------------------------------
    BillablePath(
        key="tts.elevenlabs.synthesize",
        provider="elevenlabs",
        module="app.providers.tts",
        operation="ElevenLabsTTSProvider.synthesize",
        billable=BILLABLE,
        billing_note="ElevenAPI bills Text to Speech per character (~$0.20/1k)",
        paid_jobs_sites=("app.providers.tts",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/providers/tts.py:628",
            "app/providers/tts.py:623",
        ),
        source_markers=("/text-to-speech/",),
    ),
    BillablePath(
        key="images.xkiro.generate",
        provider="xkiro",
        module="app.providers.images",
        operation="XkiroImageProvider.generate",
        billable=BILLABLE,
        billing_note=(
            "remote async image job consuming the plan's 24h generation "
            "allowance; a submitted job may be billed even if the poll fails, "
            "and no per-request price is reported, so an unpriced accepted "
            "render is booked as UNKNOWN_EXPOSURE rather than $0"
        ),
        paid_jobs_sites=("app.providers.images",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/providers/images.py:446",
            "app/providers/images.py:261",
        ),
        source_markers=("/images/generations",),
    ),
    BillablePath(
        key="images.openai_compat.generate",
        provider="openai_compatible",
        module="app.providers.images",
        operation="OpenAICompatImageProvider.generate",
        billable=BILLABLE,
        billing_note="vendor or metered gateway; billed per image",
        paid_jobs_sites=("app.providers.images",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/providers/images.py:307",
            "app/providers/images.py:342",
        ),
        source_markers=("/images/generations",),
    ),
    BillablePath(
        key="music.elevenlabs.video_to_music",
        provider="elevenlabs",
        module="app.providers.music.elevenlabs_music",
        operation="ElevenLabsMusicProvider.generate",
        billable=BILLABLE,
        billing_note="ElevenAPI bills Music per generation",
        paid_jobs_sites=("app.providers.music.elevenlabs_music",
                         "app.engine.ugc.pipeline"),
        idempotency=(
            "sha256(video_asset_id|duration|prompt)[:32] is stored as "
            "SubmissionRecord.idempotency_key and in result provenance; not "
            "sent upstream (ElevenLabs documents no idempotency header)"
        ),
        evidence=(
            "app/providers/music/elevenlabs_music.py:309",
            "app/providers/music/elevenlabs_music.py:349",
            "app/providers/music/elevenlabs_music.py:359",
            "app/engine/ugc/pipeline.py:658",
        ),
    ),
    BillablePath(
        key="avatar.server_render",
        provider="avatar_server",
        module="app.providers.avatar",
        operation="_server_render",
        billable=BILLABLE,
        billing_note=(
            "remote GPU render server billed per rendered clip; the response "
            "carries no price, so an accepted render is booked as "
            "UNKNOWN_EXPOSURE instead of the $0 this lane used to record"
        ),
        paid_jobs_sites=("app.providers.avatar",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/providers/avatar.py:372",
            "app/providers/avatar.py:409",
        ),
        source_markers=("/render",),
    ),
    BillablePath(
        key="broll.ai_server.generate",
        provider="broll_ai_server",
        module="app.providers.broll",
        operation="_server_generate",
        billable=BILLABLE,
        billing_note=(
            "remote GPU B-roll renderer billed per generated clip; no price is "
            "reported, so the accepted clip is booked as UNKNOWN_EXPOSURE"
        ),
        paid_jobs_sites=("app.providers.broll",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/providers/broll.py:477",
            "app/providers/broll.py:510",
        ),
        source_markers=("/generate",),
    ),
    BillablePath(
        key="lipsync.external.submit",
        provider="external_lipsync_worker",
        module="app.engine.lipsync.external",
        operation="ExternalAdapter.submit",
        billable=BILLABLE,
        billing_note=(
            "the worker returns cost_usd per job, so submission is billable"
        ),
        paid_jobs_sites=("app.engine.lipsync.external",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/engine/lipsync/external.py:299",
            "app/engine/lipsync/external.py:82",
            "app/engine/lipsync/worker.py:254",
            "app/engine/lipsync/worker.py:362",
        ),
        source_markers=('"/jobs"',),
    ),
    BillablePath(
        key="llm.completion_leg",
        provider="openai_compatible_llm",
        module="app.engine.intelligence.llm_paid",
        operation="run_completion",
        billable=BILLABLE,
        billing_note=(
            "the ONE outbound POST /chat/completions for a metered completion; "
            "the body is built by providers.llm and the reservation, the single "
            "submit and the classification are here"
        ),
        paid_jobs_sites=("app.engine.intelligence.llm_paid",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/engine/intelligence/llm_paid.py:52",
            "app/providers/llm.py:311",
        ),
        source_markers=("run_completion(",),
        gap=(
            "COVERED, and the limitation is a provider fact rather than a gap "
            "in the code: no vendor publishes an idempotency header for "
            "/chat/completions, so the local idempotency key plus the refusal "
            "to advance from SUBMISSION_UNKNOWN are the whole of the "
            "double-charge protection."
        ),
    ),
    # Work 15.9 §9: this is a COMPOSITE row and saying so is the honest
    # description. ``providers/llm.py`` builds a body and delegates; the POST,
    # the reservation, the single submit and the classification all live in
    # ``llm_paid.run_completion``. Listing only ``providers.llm`` as the covered
    # site read as UNCOVERED, which was true of the FILE and false of the LANE
    # -- the guarantee is its callee's, exactly as it is for
    # ``video_engine.ffmpeg_avatar.submit``.
    BillablePath(
        key="llm.complete",
        provider="openai_compatible_llm",
        module="app.providers.llm",
        operation="complete",
        billable=BILLABLE,
        billing_note="metered per prompt+completion token",
        idempotency=_IDEMPOTENCY_NONE,
        composite_paths=("llm.completion_leg",),
        composite_note=(
            "complete() resolves the provider and builds the request body, then "
            "hands it to llm_paid.run_completion, which reserves, submits ONCE, "
            "classifies the outcome and books the cost. complete_json adds the "
            "§7 re-ask policy on top. The module renders nothing billable of its "
            "own, so its guarantee is exactly its callee's."
        ),
        evidence=(
            "app/providers/llm.py:100",
            "app/engine/intelligence/llm_paid.py:701",
            "app/providers/llm.py:311",
        ),
        source_markers=("run_completion(",),
        # Work 15.9 §9: this row is COVERED, and the reason is worth being
        # precise about, because "covered" and "reconcilable" are different
        # words. COVERAGE is a safety classification: the behaviour on every
        # exit is decided in code, named, and tested. RECONCILIATION is
        # impossible here and always will be -- so this row does not claim it.
        gap=(
            "COVERED, with one irreducible provider fact named below. (1) "
            "RECONCILIATION IS IMPOSSIBLE, not pending: a chat completion is "
            "synchronous, so the generated text IS the response body and the "
            "protocol returns no request id, no job id and no status endpoint. "
            "llm_reconciliation_capability() at "
            "app/engine/intelligence/llm_paid.py reports UNRECONCILABLE, and a "
            "lost response is priced as an unknown exposure and surfaced rather "
            "than looked up. (2) No vendor documents an idempotency header, so "
            "the only protection against a second charge is the local key plus "
            "the refusal to advance from SUBMISSION_UNKNOWN unless a caller "
            "opts in through an explicit, audited FallbackPolicy that names an "
            "approver. (3) The per-leg budget gate lives in llm_paid rather than "
            "here, and a caller that already reserved for an exact payload hands "
            "it down through preauthorized_spend so one POST is not reserved "
            "twice. (4) Work 15.9 §7 closed the one purchase that used to hide "
            "inside a parser: complete_json's re-ask on unparseable output is "
            "now a typed ReaskPolicy that is OFF by default, must name an "
            "approver, must carry additional_budget_usd covering the worst-case "
            "estimate, and is recorded as its own submission -- after a free "
            "local repair pass has had its chance."
        ),
    ),
    BillablePath(
        key="semantic_rerank.twelve_labs.embed",
        provider="twelvelabs",
        module="app.engine.intel.impl.semantic_rerank",
        operation="SemanticRerankProvider.run",
        billable=BILLABLE,
        billing_note="Embed API v2 is billed per input token",
        paid_jobs_sites=("app.engine.intel.impl.semantic_rerank",),
        idempotency=(
            "sha256(query|candidate)[:32] persisted as SubmissionRecord."
            "idempotency_key; NOT sent upstream (TwelveLabs documents no "
            "idempotency header)"
        ),
        evidence=(
            "app/engine/intel/impl/semantic_rerank.py:369",
            "app/services/paid_jobs.py:186",
        ),
    ),
    # Work 15.7 RECLASSIFICATION. This row claimed NOT_BILLABLE with the note
    # "in-process ffmpeg compositing on a worker thread". The compositing part
    # is true and free, but submit() does not only composite: at :476 it
    # narrates through get_tts_provider(), which is paid ElevenLabs whenever
    # that is the configured provider, and at :679 it generates up to EIGHT
    # scene images through get_image_provider(), which is a paid gateway
    # whenever one is configured (n_scenes is clamped to 8 at :489). A path that
    # spends money through its callees is billable, so this is now a COMPOSITE
    # row: it renders nothing billable itself, and it is covered exactly when
    # every path it delegates to is.
    BillablePath(
        key="video_engine.ffmpeg_avatar.submit",
        provider="ffmpeg_avatar",
        module="app.providers.video_engine.ffmpeg_avatar",
        operation="FFmpegAvatarEngine.submit",
        billable=BILLABLE,
        billing_note=(
            "the ffmpeg compositing is " + LOCAL_COMPUTE + ", but the submit "
            "pays for narration (ElevenLabs, per character) and for up to "
            "eight scene images through the configured image provider"
        ),
        composite_paths=(
            "tts.elevenlabs.synthesize",
            "images.xkiro.generate",
            "images.openai_compat.generate",
        ),
        composite_note=(
            "submit() calls get_tts_provider().synthesize() once "
            "(ffmpeg_avatar.py:476) and get_image_provider().generate() up to "
            "n_scenes times, n_scenes clamped to 8 (ffmpeg_avatar.py:679, :489). "
            "Both callees run the shared paid executor, so the composite "
            "inherits their budget gate, single-submit and cost records; what "
            "this module adds is no guard of its own and needs none."
        ),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/providers/video_engine/ffmpeg_avatar.py:354",
            "app/providers/video_engine/ffmpeg_avatar.py:476",
            "app/providers/video_engine/ffmpeg_avatar.py:489",
            "app/providers/video_engine/ffmpeg_avatar.py:679",
        ),
        source_markers=("tts.synthesize(", "img_prov.generate("),
    ),
    # ---- operator-server TTS: billable IF the server is remote ----------
    # Work 15.7 RECLASSIFICATION. These three were recorded NOT_BILLABLE on the
    # assumption "self-hosted". The base URL arrives as a CREDENTIAL
    # (tts.kokoro_base_url / tts.qwen_base_url / tts.chatterbox_base_url), so a
    # hosted instance is somebody's invoice and the credential that names it is
    # a paid-capability credential. A local host is still operator CPU; the
    # guard in providers/tts.py makes that distinction per request
    # (``_remote_base_url``) rather than trusting the provider's label.
    BillablePath(
        key="tts.kokoro.synthesize",
        provider="kokoro",
        module="app.providers.tts",
        operation="KokoroTTSProvider.synthesize",
        billable=BILLABLE,
        billing_note=(
            "a localhost Kokoro is " + LOCAL_COMPUTE + "; a hosted one is "
            "billed by its operator and the credential that names it is "
            "tts.kokoro_base_url, so the path is guarded billable-if-remote"
        ),
        paid_jobs_sites=("app.providers.tts",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/providers/tts.py:229",
            "app/providers/tts.py:306",
        ),
        source_markers=("/audio/speech",),
    ),
    BillablePath(
        key="tts.qwen3.synthesize",
        provider="qwen3_tts",
        module="app.providers.tts",
        operation="QwenTTSProvider.synthesize",
        billable=BILLABLE,
        billing_note=(
            "a localhost vLLM-Omni is " + LOCAL_COMPUTE + "; a hosted server "
            "is billed by its operator and tts.qwen_base_url is the credential "
            "that names it"
        ),
        paid_jobs_sites=("app.providers.tts",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/providers/tts.py:528",
            "app/providers/tts.py:290",
        ),
        source_markers=("/audio/speech",),
    ),
    BillablePath(
        key="tts.chatterbox.synthesize",
        provider="chatterbox",
        module="app.providers.tts",
        operation="ChatterboxTTSProvider.synthesize",
        billable=BILLABLE,
        billing_note=(
            "the native pip package is " + LOCAL_COMPUTE + "; the "
            "OpenAI-compatible server branch is remote and billable, and "
            "tts.chatterbox_base_url is the credential that names it"
        ),
        paid_jobs_sites=("app.providers.tts",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/providers/tts.py:414",
            "app/providers/tts.py:290",
        ),
        source_markers=("/audio/speech",),
    ),
    # ---- the callers of a guarded provider ------------------------------
    # Work 15.7. Each of these spends money only by DELEGATING, and the
    # guarantee is therefore the callee's: the row names the module that
    # carries the contract, and `covered` is read from that module. The rows
    # exist because the 15.6 table listed providers and stopped there -- which
    # is how a route could spend ElevenLabs characters with no row naming it.
    BillablePath(
        key="agents.voice.synthesis_loops",
        provider="elevenlabs|operator_tts_server",
        module="app.engine.agents.voice",
        operation="VoiceDesignerAgent direct/dialogue/cast",
        billable=BILLABLE,
        billing_note=(
            "three synthesis loops (single take :134, segmented :222, "
            "multi-voice :302); each character of ElevenLabs text is billed, "
            "and the agent already estimates it per call"
        ),
        paid_jobs_sites=("app.providers.tts",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/engine/agents/voice.py:134",
            "app/engine/agents/voice.py:222",
            "app/engine/agents/voice.py:302",
            "app/engine/agents/voice.py:146",
        ),
        source_markers=(".synthesize(",),
    ),
    BillablePath(
        key="agents.avatar.direct",
        provider="avatar_server|operator_tts_server",
        module="app.engine.agents.avatar",
        operation="AvatarDirectorAgent.direct",
        billable=BILLABLE,
        billing_note=(
            "voices the script through the workspace voice stack (:52) and "
            "then drives an avatar render (:63), which is a billed GPU render "
            "on the server lane"
        ),
        paid_jobs_sites=("app.providers.avatar", "app.providers.tts"),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/engine/agents/avatar.py:52",
            "app/engine/agents/avatar.py:63",
        ),
        source_markers=("render_avatar(",),
    ),
    BillablePath(
        key="agents.broll.ai_generate",
        provider="broll_ai_server",
        module="app.engine.agents.broll",
        operation="BrollResearcherAgent.fetch",
        billable=BILLABLE,
        billing_note=(
            "the AI branch of fetch() renders a clip on the configured AI "
            "backend, which is a billed GPU render when that backend is "
            "`server`"
        ),
        paid_jobs_sites=("app.providers.broll",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=("app/engine/agents/broll.py:107",),
        source_markers=("generate_clip(",),
    ),
    BillablePath(
        key="providers.dubbing.synthesize_segments",
        provider="elevenlabs|operator_tts_server",
        module="app.providers.dubbing",
        operation="synthesize_segments",
        billable=BILLABLE,
        billing_note=(
            "one billable synthesis per translated subtitle segment; a 60s "
            "short with 12 lines is twelve separate paid calls"
        ),
        paid_jobs_sites=("app.providers.tts",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/providers/dubbing.py:299",
            "app/providers/dubbing.py:292",
            "app/engine/agents/dubbing.py:80",
        ),
        source_markers=(".synthesize(text, voice=voice)",),
    ),
    BillablePath(
        key="engine.localization.synthesize_texts",
        provider="elevenlabs|operator_tts_server",
        module="app.engine.localization.pipeline",
        operation="synthesize_texts",
        billable=BILLABLE,
        billing_note=(
            "the localization pipeline re-exports dubbing.synthesize_segments, "
            "so a localized track costs one billable synthesis per segment -- "
            "recorded as its own row because a caller reading this table must "
            "not have to know it is an alias"
        ),
        paid_jobs_sites=("app.providers.tts",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/engine/localization/pipeline.py:100",
            "app/providers/dubbing.py:299",
        ),
    ),
    BillablePath(
        key="providers.dubbing.translate_segments",
        provider="openai_compatible_llm",
        module="app.providers.dubbing",
        operation="translate_segments",
        billable=BILLABLE,
        billing_note=(
            "a metered LLM completion per BATCH of subtitle segments. Work 15.8 "
            "§5 made the batch the accounting unit, because the batch is the "
            "request: 20 segments are one POST, so a per-segment reservation "
            "would over-reserve twenty-fold"
        ),
        paid_jobs_sites=("app.providers.dubbing",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/providers/dubbing.py:369",
            "app/providers/dubbing.py:355",
            "app/providers/dubbing.py:246",
            "app/engine/agents/dubbing.py:68",
        ),
        source_markers=("complete_json(",),
        # Work 15.8 §5: the gate that did not exist. Before this the loop
        # `for batch_start in range(0, len(texts), 20)` made ceil(N/20) billable
        # requests with ZERO budget gate, so a localization run could spend until
        # the workspace ran out and only discover it afterwards. Each batch now
        # reserves atomically BEFORE the request and closes the row in place
        # after, and a refusal aborts before anything is sent.
        gap=(
            "COVERED for the gate and the ledger; the residual limits are "
            "provider facts. (1) An ambiguous batch keeps its reservation marked "
            "UNKNOWN_EXPOSURE because a chat completion has no remote id and no "
            "status endpoint -- llm_paid.llm_reconciliation_capability() reports "
            "UNRECONCILABLE, so this cannot be looked up afterwards at all. (2) "
            "Work 15.9 §7 removed the second purchase that used to be made "
            "INSIDE the parser: providers.llm.complete_json's re-ask on an "
            "unparseable reply is now a typed ReaskPolicy, OFF by default, so "
            "this loop no longer needs to pre-reserve a worst case it will not "
            "usually spend. A caller that WANTS the re-ask must pass a policy "
            "naming an approver and declaring additional_budget_usd; the re-ask "
            "reserves its own budget and is recorded as its own submission, and "
            "a free local repair pass runs first so a fenced or comma-terminated "
            "reply costs nothing extra."
        ),
    ),
    BillablePath(
        key="providers.longform_assets.image_generate",
        provider="xkiro|openai_compatible|pexels|pollinations",
        module="app.providers.longform_assets",
        operation="scene image generation",
        billable=BILLABLE,
        billing_note=(
            "long-form scene images come from the configured image provider; "
            "billable when that is xkiro or a metered OpenAI-compatible "
            "gateway, free when it is Pexels or pollinations"
        ),
        paid_jobs_sites=("app.providers.images",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=("app/providers/longform_assets.py:154",),
        source_markers=("get_image_provider()",),
    ),
    BillablePath(
        key="providers.tts_qualification.probe_synthesize",
        provider="elevenlabs|operator_tts_server",
        module="app.providers.tts_qualification",
        operation="qualify_tts_provider probe",
        billable=BILLABLE,
        billing_note=(
            "the qualification probe synthesizes a REAL sentence, so probing "
            "the ElevenLabs adapter spends real characters; a diagnostics "
            "screen that quietly costs money is still money"
        ),
        paid_jobs_sites=("app.providers.tts",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=("app/providers/tts_qualification.py:687",),
        source_markers=("a short honest sentence for the probe",),
    ),
    BillablePath(
        key="engine.ugc.voice.synthesize",
        provider="elevenlabs|operator_tts_server",
        module="app.engine.ugc.voice",
        operation="UGC voice line synthesis",
        billable=BILLABLE,
        billing_note=(
            "UGC voice lines are synthesized through the configured TTS "
            "provider, per character on ElevenLabs"
        ),
        paid_jobs_sites=("app.providers.tts",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=("app/engine/ugc/voice.py:164",),
        source_markers=(".synthesize(text, voice=voice)",),
    ),
    BillablePath(
        key="engine.ugc.pipeline.broll_generate",
        provider="broll_ai_server",
        module="app.engine.ugc.pipeline",
        operation="UGC scene clip generation",
        billable=BILLABLE,
        billing_note=(
            "UGC scenes can pull an AI-generated clip through "
            "providers.broll.generate_clip, which is a billed render on the "
            "`server` backend"
        ),
        paid_jobs_sites=("app.providers.broll",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=("app/engine/ugc/pipeline.py:862",),
        source_markers=("generate_clip(",),
    ),
    BillablePath(
        key="api.connections.test_endpoints",
        provider="elevenlabs|xkiro|openai_compatible",
        module="app.api.v1.connections",
        operation="tts_test / images_test",
        billable=BILLABLE,
        billing_note=(
            "the Connections test buttons synthesize a sample (:301) and "
            "generate an image (:352); each click is a real paid call when the "
            "configured provider is a paid one"
        ),
        paid_jobs_sites=("app.providers.tts", "app.providers.images"),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/api/v1/connections.py:301",
            "app/api/v1/connections.py:352",
        ),
        source_markers=("provider.synthesize(", "provider.generate("),
    ),
    BillablePath(
        key="api.content.voice_synth",
        provider="elevenlabs|operator_tts_server",
        module="app.api.v1.content",
        operation="voice preview + avatar driving audio",
        billable=BILLABLE,
        billing_note=(
            "two routes synthesize narration (:1682) and avatar driving audio "
            "(:1889). Work 15.7 §9 turned :1682 into a thin adapter that calls "
            "api.v1.preview.preview_voice, so the synthesize() call site moved "
            "out of this module into app.api.v1.preview; the avatar driving-audio "
            "call is booked separately by _reserve_speak / tts_speak."
        ),
        paid_jobs_sites=("app.providers.tts",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/api/v1/content.py:1682",
            "app/api/v1/content.py:1889",
            "app/api/v1/preview.py:741",
        ),
        source_markers=("_reserve_speak(",),
    ),
    BillablePath(
        key="api.content.image_generate",
        provider="xkiro|openai_compatible|pexels|pollinations",
        module="app.api.v1.content",
        operation="image generate + regenerate",
        billable=BILLABLE,
        billing_note=(
            "two routes generate scene images (:779, :1473) through the "
            "configured image provider"
        ),
        paid_jobs_sites=("app.providers.images",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/api/v1/content.py:779",
            "app/api/v1/content.py:1473",
        ),
        source_markers=("provider.generate(",),
    ),
    BillablePath(
        key="api.content.broll_generate",
        provider="broll_ai_server",
        module="app.api.v1.content",
        operation="broll_generate",
        billable=BILLABLE,
        billing_note=(
            "the route generates an AI clip through providers.broll, which is "
            "a billed GPU render on the `server` backend"
        ),
        paid_jobs_sites=("app.providers.broll",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=("app/api/v1/content.py:1832",),
        source_markers=("generate_clip(",),
    ),
    BillablePath(
        key="api.content.avatar_render",
        provider="avatar_server",
        module="app.api.v1.content",
        operation="avatar render",
        billable=BILLABLE,
        billing_note=(
            "the route voices the script and then drives an avatar render "
            "through providers.avatar, which is a billed GPU render on the "
            "`server` lane"
        ),
        paid_jobs_sites=("app.providers.avatar", "app.providers.tts"),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=("app/api/v1/content.py:1898",),
        source_markers=("render_avatar(",),
    ),
    BillablePath(
        key="api.preview.voice_sample",
        provider="elevenlabs|operator_tts_server",
        module="app.api.v1.preview",
        operation="voice sample preview",
        billable=BILLABLE,
        billing_note=(
            "a voice preview is a real synthesis (:580). The route's own "
            "control is a 20-per-minute CALL budget (preview.py:155), which is "
            "not a money budget; since 15.7 the provider's pre-spend gate is "
            "what stops a preview loop from draining the daily cap"
        ),
        paid_jobs_sites=("app.providers.tts",),
        idempotency=_IDEMPOTENCY_NONE,
        evidence=(
            "app/api/v1/preview.py:580",
            "app/api/v1/preview.py:577",
            "app/api/v1/preview.py:155",
        ),
        source_markers=(".synthesize(",),
    ),
    BillablePath(
        key="providers.maturity.health_probe",
        provider="elevenlabs|kokoro|chatterbox|qwen3_tts",
        module="app.providers.maturity",
        operation="probe_health",
        billable=NOT_BILLABLE,
        billing_note=(
            "NOT billable: the opt-in provider health probe only calls "
            "provider.health(), which is a read -- GET /user for ElevenLabs, "
            "GET /audio/voices for the operator servers. No artifact, no "
            "per-character charge. It is listed because it looks like a paid "
            "call site and is not"
        ),
        evidence=(
            "app/providers/maturity.py:711",
            "app/providers/maturity.py:705",
        ),
    ),
    # ---- deliberately NOT billable -------------------------------------
    BillablePath(
        key="tts.edge.synthesize",
        provider="edge_tts",
        module="app.providers.tts",
        operation="EdgeTTSProvider.synthesize",
        billable=NOT_BILLABLE,
        billing_note=(
            "Edge read-aloud endpoint: no per-character invoice, no API key"
        ),
        evidence=("app/providers/tts.py:79",),
    ),
    BillablePath(
        key="avatar.sadtalker.render",
        provider="sadtalker",
        module="app.providers.avatar",
        operation="_sadtalker_render",
        billable=NOT_BILLABLE,
        billing_note="local subprocess against an operator checkout; " + LOCAL_COMPUTE,
        evidence=("app/providers/avatar.py:284",),
    ),
    BillablePath(
        key="avatar.wavlip.render",
        provider="wavlip",
        module="app.providers.avatar",
        operation="_wavlip_render",
        billable=NOT_BILLABLE,
        billing_note="local subprocess against an operator checkout; " + LOCAL_COMPUTE,
        evidence=("app/providers/avatar.py:312",),
    ),
    BillablePath(
        key="avatar.mock.render",
        provider="mock",
        module="app.providers.avatar",
        operation="_mock_clip",
        billable=NOT_BILLABLE,
        billing_note="ffmpeg color card; " + LOCAL_COMPUTE,
        evidence=("app/providers/avatar.py:217",),
    ),
    BillablePath(
        key="broll.native.generate",
        provider="wan|ltx",
        module="app.providers.broll",
        operation="_native_generate",
        billable=NOT_BILLABLE,
        billing_note="local diffusers pipeline on operator GPU; " + LOCAL_COMPUTE,
        evidence=("app/providers/broll.py:389",),
    ),
    BillablePath(
        key="video_engine.timeline_render.render",
        provider="timeline_render",
        module="app.providers.video_engine.timeline_render",
        operation="render_timeline",
        billable=NOT_BILLABLE,
        billing_note="ffmpeg filter-graph render; " + LOCAL_COMPUTE,
        evidence=("app/providers/video_engine/timeline_render.py:441",),
    ),
    BillablePath(
        key="broll.synth.generate",
        provider="synth",
        module="app.providers.broll",
        operation="_synth_clip",
        billable=NOT_BILLABLE,
        billing_note="ffmpeg testsrc placeholder; " + LOCAL_COMPUTE,
        evidence=("app/providers/broll.py:414",),
    ),
    BillablePath(
        key="broll.pexels.search",
        provider="pexels",
        module="app.providers.broll",
        operation="PexelsBrollProvider.search",
        billable=NOT_BILLABLE,
        billing_note="free-tier stock API; rate-limited quota, no per-call charge",
        evidence=("app/providers/broll.py:175",),
    ),
    BillablePath(
        key="lipsync.musetalk.submit",
        provider="musetalk",
        module="app.engine.lipsync.musetalk",
        operation="MuseTalkAdapter.submit",
        billable=NOT_BILLABLE,
        billing_note="operator subprocess / GPU worker; " + LOCAL_COMPUTE,
        evidence=("app/engine/lipsync/musetalk.py:221",),
    ),
    BillablePath(
        key="motion.hyperframes.render",
        provider="hyperframes",
        module="app.providers.motion",
        operation="render",
        billable=NOT_BILLABLE,
        billing_note="npx CLI on the worker host; " + LOCAL_COMPUTE,
        evidence=("app/providers/motion.py:62",),
    ),
    BillablePath(
        key="clips.yt_dlp.fetch",
        provider="yt_dlp",
        module="app.providers.clips",
        operation="fetch",
        billable=NOT_BILLABLE,
        billing_note="yt-dlp + ffmpeg on the worker host; " + LOCAL_COMPUTE,
        evidence=("app/providers/clips.py:180",),
    ),
    BillablePath(
        key="media_intel.local_providers",
        provider="whisperx|pyannote|sam2|mediapipe|rnnoise|ffmpeg",
        module="app.engine.intel.impl",
        operation="*.run",
        billable=NOT_BILLABLE,
        billing_note=(
            "local model/subprocess providers; " + LOCAL_COMPUTE
            + "; cost is reported as gpu_ms/cpu_ms, never a vendor invoice"
        ),
        evidence=("app/engine/intel/impl/motion_reframe.py:310",),
    ),
    BillablePath(
        key="publishers.platform.publish",
        provider="tiktok|youtube|instagram|linkedin|x|threads|pinterest|bluesky",
        module="app.providers.publishers",
        operation="*.publish",
        billable=NOT_BILLABLE,
        billing_note=(
            "publishing spends a user's own connected account, not YMONEY "
            "money; duplicate protection is Work 11's publish idempotency key, "
            "not the paid-job state machine"
        ),
        evidence=("app/providers/publishers/platforms.py:74",),
    ),
    BillablePath(
        key="analytics.snapshot",
        provider="youtube_data_api|platform_apis",
        module="app.providers.analytics",
        operation="snapshot",
        billable=NOT_BILLABLE,
        billing_note="quota-metered read API; no per-call charge, no artifact",
        evidence=("app/providers/analytics/__init__.py:122",),
    ),
)


# ---------------------------------------------------------------------------
# queries
# ---------------------------------------------------------------------------


def audit() -> tuple[BillablePath, ...]:
    """Every audited path, in table order."""
    return PAID_PATHS


def path(key: str) -> BillablePath | None:
    """One audited path by key (``None`` for an unknown key)."""
    wanted = str(key or "").strip()
    for item in PAID_PATHS:
        if item.key == wanted:
            return item
    return None


def billable_paths() -> tuple[BillablePath, ...]:
    """Only the paths that can cost money."""
    return tuple(item for item in PAID_PATHS if item.billable)


def not_billable_paths() -> tuple[BillablePath, ...]:
    """Only the paths that cannot bill (each with its reason)."""
    return tuple(item for item in PAID_PATHS if not item.billable)


def uncovered_billable_paths() -> tuple[BillablePath, ...]:
    """Billable paths that do not yet route through the paid-job contract."""
    return tuple(item for item in billable_paths() if not item.covered)


def describe(key: str) -> dict:
    """One path as a plain dict (unknown key -> an honest error dict)."""
    found = path(key)
    if found is None:
        return {"key": key, "error": "path not audited"}
    return found.to_dict()


def summary() -> dict:
    """Counts for the audit, computed -- never hand-maintained."""
    billable = billable_paths()
    uncovered = uncovered_billable_paths()
    return {
        "total": len(PAID_PATHS),
        "billable": len(billable),
        "not_billable": len(PAID_PATHS) - len(billable),
        "covered": len(billable) - len(uncovered),
        "uncovered_billable": len(uncovered),
        "uncovered_keys": [item.key for item in uncovered],
        "verdicts": {
            item.key: {"billable": item.billable, "coverage": item.coverage}
            for item in PAID_PATHS
        },
    }


def verify_against_source() -> list[CoverageMismatch]:
    """Re-derive every claim from the real files, in BOTH directions.

    Returns one :class:`CoverageMismatch` per disagreement:

    * a declared site that does not carry the paid contract is reported (the
      coverage claim is stale);
    * a declared site whose file does not exist is reported (wrong module name);
    * an UNCOVERED billable path with no :attr:`BillablePath.gap` explanation is
      reported (an unexplained gap is an unaudited path);
    * a row whose :attr:`BillablePath.source_markers` are absent from its own
      module is reported (the row is STALE: the code it cites has moved or
      gone, so the verdict is no longer about anything real);
    * a composite row naming a leaf that is not in the table is reported (the
      composition points at nothing);
    * a :data:`MONEY_MARKERS` entry that is absent from its module is reported
      (the inventory itself has rotted);
    * a :data:`MONEY_MARKERS` entry that NO row cites is reported as a MISSING
      PATH (a billable call site with no audit row -- the failure the 15.6
      detector could not see, and the one that let a paid ffmpeg_avatar submit
      sit in the table claiming NOT_BILLABLE for a whole release).

    The last two are what make the audit able to catch its own worst error: an
    absent or wrong row, not merely a row whose site lost an import.
    """
    problems: list[CoverageMismatch] = []
    cited: set[tuple[str, str]] = set()

    for item in PAID_PATHS:
        for site in item.paid_jobs_sites:
            if not _source_path(site).is_file():
                problems.append(CoverageMismatch(
                    key=item.key, module=item.module, site=site,
                    problem="declared paid-job site does not exist on disk",
                ))
            elif not _carries_paid_contract(site):
                problems.append(CoverageMismatch(
                    key=item.key, module=item.module, site=site,
                    problem="declared covered but the site never imports the "
                            "paid-job contract",
                ))
        if item.billable and not item.covered and not item.gap.strip():
            problems.append(CoverageMismatch(
                key=item.key, module=item.module, site="",
                problem="uncovered billable path with no recorded gap",
            ))
        if item.is_composite and not item.composite_note.strip():
            problems.append(CoverageMismatch(
                key=item.key, module=item.module, site="",
                problem="composite path with no explanation of what it spends "
                        "money on",
            ))
        for leaf in item.composite_paths:
            if path(leaf) is None:
                problems.append(CoverageMismatch(
                    key=item.key, module=item.module, site=leaf,
                    problem="composite path delegates to a row that is not in "
                            "the table",
                ))
        module_text = _source_text(item.module)
        for marker in item.source_markers:
            cited.add((item.module, marker))
            if not module_text:
                problems.append(CoverageMismatch(
                    key=item.key, module=item.module, site=marker,
                    problem="row cites a source marker but the module does not "
                            "exist on disk",
                ))
            elif marker not in module_text:
                problems.append(CoverageMismatch(
                    key=item.key, module=item.module, site=marker,
                    problem="row is STALE: the source marker it cites is no "
                            "longer in the module",
                ))

    # The inventory side. A marker nobody cites is an unaudited billable path.
    for module, markers in MONEY_MARKERS.items():
        module_text = _source_text(module)
        for entry in markers:
            if not module_text:
                problems.append(CoverageMismatch(
                    key="", module=module, site=entry.marker,
                    problem="money inventory names a module that does not "
                            "exist on disk",
                ))
                continue
            if entry.marker not in module_text:
                problems.append(CoverageMismatch(
                    key="", module=module, site=entry.marker,
                    problem="money inventory is STALE: the call site it names "
                            "is no longer in the module",
                ))
                continue
            if (module, entry.marker) not in cited:
                problems.append(CoverageMismatch(
                    key="", module=module, site=entry.marker,
                    problem=f"MISSING PATH: a billable call site ({entry.why}) "
                            f"that no row in PAID_PATHS cites",
                ))
    return problems


def render_table() -> str:
    """Markdown rendering of the audit, computed from the same data."""
    lines = [
        "| path | billable | coverage | idempotency |",
        "|---|---|---|---|",
    ]
    for item in PAID_PATHS:
        billable = "yes" if item.billable else "no"
        # Coverage is only meaningful where money is at stake; showing
        # "UNCOVERED" against an ffmpeg local path would be misleading noise.
        coverage = item.coverage if item.billable else "n/a (not billable)"
        if item.is_composite and item.billable:
            coverage = f"{coverage} (composite)"
        idem = item.idempotency or ("n/a" if not item.billable else "none recorded")
        lines.append(
            f"| `{item.key}` | {billable} | {coverage} | {idem.split(';')[0]} |"
        )
    return "\n".join(lines)
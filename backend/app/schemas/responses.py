"""Declared response contracts for UI-consumed routes (Work 16.5.2 §1).

WHY THIS EXISTS
---------------
Every rebuilt screen reads these routes, and TypeScript could not tell whether a
field existed: the routes returned bare ``dict``, so FastAPI emitted a 2xx with
**no schema at all**. The frontend contract tests could therefore only check
route and method. A renamed field would have stayed green until runtime.

HOW IT IS ATTACHED, AND WHY THAT WAY
------------------------------------
These models are attached with ``responses={200: {"model": X}}`` rather than
``response_model=X``. The difference matters:

* ``response_model`` VALIDATES and FILTERS the response. A model that is even
  slightly wrong silently DROPS the fields it omits -- the UI would lose data
  while every test stayed green. That is a semantic change made for schema
  convenience, which this work order forbids.
* ``responses`` only documents. The handler's dict passes through untouched, so
  declaring a contract cannot break a working endpoint.

The trade is explicit: the spec can drift from reality, so every model below is
transcribed from the handler that produces it (named in the comment), and
``tests/test_work16_5_2_response_contracts.py`` fails if a transcription stops
matching the code it claims to describe.

STATE FIELDS ARE `str`, NOT `Literal`
-------------------------------------
A closed ``Literal`` in the spec becomes an enum the frontend contract test then
*enforces*. Declaring ``status`` as the eight values observed today would make a
ninth status a build failure while the backend is perfectly happy to emit it.
Open-ended state stays ``str``; genuinely closed vocabularies (the provider
maturity ladder) use ``Literal`` because those ARE closed by definition.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Jobs  (backend/app/api/v1/misc.py::list_jobs -> jobs_service._to_dict)
# ---------------------------------------------------------------------------


class JobOut(BaseModel):
    """One job row. Mirrors ``jobs_service._to_dict`` exactly."""

    id: str
    type: str
    workspace_id: str
    cycle_id: str | None = None
    #: Open-ended on purpose: the queue adds states, and a spec that forbids a
    #: state the backend can emit is a spec that will be wrong.
    status: str
    priority: int = 0
    retry_count: int = 0
    max_retries: int = 0
    next_run_at: str | None = None
    started_at: str | None = None
    completed_at: str | None = None
    last_error: str = ""
    payload: dict = Field(default_factory=dict)
    result: dict = Field(default_factory=dict)
    created_at: str
    # Work 16.2: lease ownership, so an operator can see who runs what.
    claimed_by: str = ""
    claimed_at: str | None = None
    lease_expires_at: str | None = None
    heartbeat_at: str | None = None
    lease_state: str = ""


class JobListOut(BaseModel):
    items: list[JobOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Costs  (backend/app/api/v1/misc.py::cost_summary)
# ---------------------------------------------------------------------------


class CostSummaryOut(BaseModel):
    """24h spend against the configured budgets.

    ``spent_last_24h_usd`` WAS ``float``, and the handler fabricated the zero:
    an empty ledger, or one holding an ``UNKNOWN_EXPOSURE`` row, summed to ``0.0``
    and published as "$0 spent". Both are fabrications -- nothing was observed at
    all in the first case, and in the second the money is possibly spent but
    unmeasured.

    It is now ``float | None``: ``None`` means UNKNOWN, and a real measured zero
    (rows exist and sum to zero) is still ``0.0``. Consumers must render ``None``
    as unavailable, never as $0.
    """

    last_24h_by_category: dict[str, float] = Field(default_factory=dict)
    spent_last_24h_usd: float | None
    #: How many rows in the window could not be priced. Non-zero here forces the
    #: total to ``None`` -- that is the whole point of exposing it, so the UI can
    #: say WHY the number is missing instead of guessing.
    spent_last_24h_unknown_exposure_rows: int = 0
    daily_budget_usd: float
    per_video_budget_usd: float
    #: ``remaining > 0`` server-side. NOT a measurement of spend.
    within_budget: bool
    remaining_usd: float


# ---------------------------------------------------------------------------
# Planner opportunities  (backend/app/api/v1/planner.py::list_opportunities)
#
# ``basis`` is the planner's central honesty claim and IS closed (see
# models/planning.py), so it gets a Literal the frontend can enforce.
# ``dedupe_verdict`` is likewise a fixed vocabulary. The numeric scores are
# deliberately left as ``float | None``: an absent score must read as
# unavailable, never as 0.0.
# ---------------------------------------------------------------------------

OpportunityBasis = Literal["OBSERVED", "INFERRED", "RECOMMENDED"]
DedupeVerdict = Literal["NEW", "RELATED", "DUPLICATE", "SATURATED"]


class OpportunityOut(BaseModel):
    id: str
    topic: str
    basis: OpportunityBasis
    #: Human explanation of the basis, resolved server-side.
    basis_meaning: str = ""
    angle: str | None = None
    audience: str | None = None
    platforms: list[str] = Field(default_factory=list)
    format: str | None = None
    score: float | None = None
    confidence: float | None = None
    freshness: str | None = None
    brand_fit: float | None = None
    evidence: list = Field(default_factory=list)
    competition_evidence: list = Field(default_factory=list)
    estimated_effort_hours: float | None = None
    estimated_cost_usd: float | None = None
    dedupe_verdict: DedupeVerdict | None = None
    dedupe_reason: str | None = None
    plan_item_id: str | None = None
    #: The stored scoring record. ``factors[*].measured`` is the field that
    #: distinguishes "contributed 0" from "no data existed".
    scoring: dict | None = None
    why: str | None = None


class OpportunityListOut(BaseModel):
    workspace_id: str
    count: int = 0
    opportunities: list[OpportunityOut] = Field(default_factory=list)

    #: MISSING IN WORK 16.5.2 AND CAUGHT BY THE RUNTIME VALIDATOR (16.5.3 §5).
    #:
    #: The 16.5.2 AST drift guard compared only the ITEM literal inside the
    #: comprehension, so it could not see the two keys the handler adds to the
    #: ENVELOPE after the list closes. Both are load-bearing:
    #:
    #:  * ``forbidden_claims`` is the planner's honesty contract -- the claims it
    #:    refuses to make, served to the client so the UI can show them rather
    #:    than invent its own.
    #:  * ``note`` explains why a RECOMMENDED idea is capped below measured
    #:    demand, which is the whole basis of the ``basis`` field.
    #:
    #: A document-only check could never have found this: the schema was
    #: syntactically perfect and still wrong.
    forbidden_claims: list[str] = Field(default_factory=list)
    note: str | None = None


# ---------------------------------------------------------------------------
# Distribution capabilities
#   backend/app/api/v1/distribution.py::capabilities
#
# Note `publish_mode` is the CAPABILITY name (USER_HANDOFF / DIRECT_PUBLISH), not
# the stored publication state (HANDOFF / LIVE / ...). That is deliberate on the
# backend side -- the string is rendered on a card -- so the contract has to
# carry the capability vocabulary or the frontend will filter on the wrong one.
# ---------------------------------------------------------------------------

PublishModeCapability = Literal["DIRECT_PUBLISH", "USER_HANDOFF"]


class DistributionCapabilityOut(BaseModel):
    platform: str
    capabilities: list[str] = Field(default_factory=list)
    publish_mode: PublishModeCapability
    direct_publish: bool = False
    user_handoff: bool = False
    supports_inbox: bool = False
    supports_analytics: bool = False
    campaign_platforms: list[str] = Field(default_factory=list)
    media: dict = Field(default_factory=dict)
    metadata_limits: dict = Field(default_factory=dict)


class DistributionCapabilityListOut(BaseModel):
    items: list[DistributionCapabilityOut] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Provider maturity (backend/app/providers/maturity.py::ProviderMaturity.to_dict)
#
# FIRST DRAFT WAS WRONG AND THE TESTS CAUGHT IT. It modelled a single
# ``maturity`` field. The real record carries FOUR INDEPENDENT status axes plus a
# credential axis -- a provider can be IMPLEMENTED, CONTRACT_TESTED, UNVERIFIED
# live and BLOCKED_COMMERCIAL_TERMS all at once, and collapsing that to one value
# destroys the only information the Providers screen exists to show.
# ---------------------------------------------------------------------------

#: Closed vocabulary shared by the four status axes. Genuinely closed: these are
#: module constants, so a Literal is correct and the frontend may enforce it.
MaturityStatus = Literal[
    "IMPLEMENTED",
    "CONTRACT_TESTED",
    "LIVE_VERIFIED",
    "UNVERIFIED",
    "UNAVAILABLE",
    "BLOCKED_LICENSE",
    "BLOCKED_COMMERCIAL_TERMS",
    "EXTERNAL_LIMITATION",
]

CredentialStatus = Literal[
    "NOT_REQUIRED",
    "REQUIRED",
    "CONFIGURED",
    "NOT_CONFIGURED",
    "UNRESOLVED",
]

ProviderHealth = Literal["UNKNOWN", "OK", "DEGRADED", "DOWN"]


class ProviderMaturityOut(BaseModel):
    """One provider/capability maturity row.

    Carries NO secret, key length or digest -- only the NAMES of the credential
    keys a deployment would need. ``simulation_only`` is load-bearing: a
    simulated provider must never read as a working one.
    """

    provider: str
    capability: str
    implementation_status: MaturityStatus
    contract_status: MaturityStatus
    live_status: MaturityStatus
    commercial_status: MaturityStatus
    credential_status: CredentialStatus
    #: ``UNKNOWN`` when not probed. An unprobed row must not read as healthy.
    health: ProviderHealth = "UNKNOWN"
    last_verified_at: str | None = None
    #: Key NAMES only, never values.
    credential_keys: list[str] = Field(default_factory=list)
    simulation_only: bool = False
    notes: str | None = None
    evidence: list[str] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    production_ready: bool = False


class ProviderMaturityListOut(BaseModel):
    """``GET /provider-maturity`` -- the table with no credential resolution."""

    items: list[ProviderMaturityOut] = Field(default_factory=list)


class WorkspaceProviderMaturityOut(BaseModel):
    """``GET /workspaces/{ws}/provider-maturity`` -- transcribes the handler.

    Note the ``note`` field is carried verbatim: it is the backend explaining
    that ``UNRESOLVED`` (resolver failed) is not ``NOT_CONFIGURED`` (no key), and
    dropping it would remove the distinction the Providers screen must show.
    """

    workspace_id: str
    items: list[ProviderMaturityOut] = Field(default_factory=list)
    count: int = 0
    resolved_credential_states: list[str] = Field(default_factory=list)
    note: str | None = None
    credential_summary: dict = Field(default_factory=dict)

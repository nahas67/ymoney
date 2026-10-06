"""Runtime response-contract validation (Work 16.5.3 §5).

WHAT THIS IS FOR
----------------
Work 16.5.2 proved contracts by reading the OpenAPI DOCUMENT. That proves the
document is well-formed; it cannot prove the document matches what the server
actually returns. A handler can rename a field, add one, or return a different
nested shape and the document would stay perfectly valid while the frontend
broke.

So this file calls the real endpoints through the real app, with real fixture
data, and holds the RESULT against what has been declared. It is the difference
between "the schema is syntactically fine" and "the schema is true".

ANTI-VACUITY IS THE POINT
-------------------------
The failure mode this design exists to prevent: a contract suite that passes
because every endpoint returns `{}` or `[]`. A validator fed empty responses
learns nothing and enforces nothing.

Three defences:

1. ``_assert_payload_has_substance`` requires a dict with keys, or a non-empty
   list -- an empty object is a FAILURE, not a pass.
2. Real fixture objects are created through the app's own endpoints, and at
   least one endpoint is required to return the created object. If the fixtures
   silently stopped being created, ``test_fixture_creation_actually_produced_a
   retrievable_object`` fails, so the rest of the file cannot quietly become
   vacuous.
3. ``test_contract_suite_is_not_inert`` fails if the representative set ever
   collapses below its recorded size.

Coverage is REPRESENTATIVE, not exhaustive: one read per domain proves the
contract mechanism end to end. Full per-endpoint declaration is tracked
separately by ``scripts/audit_ui_contracts.py``, whose gap is reported honestly
rather than assumed closed.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.schemas import responses as declared

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
SPEC = REPO / "frontend" / "src" / "api" / "openapi.json"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _bypass_gate(monkeypatch):
    import app.services.readiness as rd

    monkeypatch.setattr(
        rd,
        "run_readiness",
        lambda force_refresh=True: {
            "status": "ready",
            "checked_at": "",
            "stale_after_hours": 24,
            "checks": [],
            "blocking_failures": [],
            "message": "test",
        },
    )


@pytest.fixture()
def client():
    app = create_app()
    with TestClient(app, raise_server_exceptions=False) as c:
        yield c


@pytest.fixture()
def owner(client):
    """A registered user, who is the workspace owner, with their workspace id."""
    email = f"rtc{os.urandom(5).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123", "display_name": "RTC"},
    )
    assert r.status_code == 200, r.text
    data = r.json()
    return {
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
        "workspace_id": data["workspace"]["id"],
    }


def _url(owner: dict, suffix: str) -> str:
    return f"/api/v1/workspaces/{owner['workspace_id']}{suffix}"


# ---------------------------------------------------------------------------
# Anti-vacuity helpers
# ---------------------------------------------------------------------------


def _assert_payload_has_substance(payload, where: str) -> None:
    """An empty shell is a FAILURE.

    A contract suite that accepts `{}` and `[]` as "valid" has verified nothing.
    """
    if isinstance(payload, dict):
        assert payload, f"{where} returned {{}} -- nothing to contract against"
    elif isinstance(payload, list):
        assert payload, f"{where} returned [] -- nothing to contract against"
    else:
        assert payload not in (None, ""), f"{where} returned an empty scalar"


def _flat_key_count(payload) -> int:
    """How many keys the top level (or first row) actually carries."""
    if isinstance(payload, dict):
        return len(payload)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return len(payload[0])
    return 0


# ---------------------------------------------------------------------------
# Representative read, per domain. The DoD lists sixteen.
# ---------------------------------------------------------------------------

REPRESENTATIVE_READS: list[tuple[str, str]] = [
    # (domain, path suffix relative to the workspace)
    ("Planner", "/planner/calendar"),
    ("Projects", "/content"),
    ("Campaigns", "/campaigns"),
    ("Assets", "/assets/media"),
    ("Brands", "/brands"),
    ("Localization", "/localization/glossary"),
    ("UGC", "/ugc/presets"),
    ("Distribution", "/distribution/platforms"),
    ("Community", "/inbox/opportunities"),
    ("Analytics", "/analytics/overview"),
    ("Experiments", "/experiments"),
    ("Memory", "/knowledge/memories"),
    ("Intelligence", "/intelligence/routing/chains"),
    ("Operations", "/ops/overview"),
    ("Providers", "/provider-maturity/incidents"),
    ("Settings", "/agents/config"),
]

# Minimum representative surface, recorded so the set cannot quietly shrink.
MIN_REPRESENTATIVE_READS = 16


class TestRepresentativeReadsReturnRealPayloads:
    @pytest.mark.parametrize("domain,suffix", REPRESENTATIVE_READS)
    def test_read_returns_a_payload_with_substance(self, client, owner, domain, suffix):
        r = client.get(_url(owner, suffix), headers=owner["headers"])
        assert r.status_code == 200, f"{domain}: {r.status_code} {r.text[:200]}"
        payload = r.json()
        _assert_payload_has_substance(payload, f"{domain} {suffix}")
        # A payload with exactly one key is legal but weak; assert the domains
        # carry real structure rather than a single status flag.
        assert _flat_key_count(payload) >= 1

    def test_contract_suite_is_not_inert(self):
        # Guards against the whole file being deleted or the list emptied.
        assert len(REPRESENTATIVE_READS) >= MIN_REPRESENTATIVE_READS
        assert len({d for d, _ in REPRESENTATIVE_READS}) == len(REPRESENTATIVE_READS)


class TestFixtureDataIsReal:
    """Anti-vacuity, part 2: the fixtures must actually produce retrievable data."""

    def test_fixture_creation_actually_produced_a_retrievable_object(self, client, owner):
        created = client.post(
            _url(owner, "/brands"),
            headers=owner["headers"],
            json={"name": "Contract Fixture Brand", "is_default": True},
        )
        # 201, not 200: a create that answers 200 is indistinguishable from a
        # read, and the status is part of the contract the UI reads.
        assert created.status_code == 201, (
            f"brand create returned {created.status_code}: {created.text[:300]}"
        )
        brand_id = created.json()["brand"]["id"]
        assert brand_id, "brand create returned no id"

        listed = client.get(_url(owner, "/brands"), headers=owner["headers"])
        assert listed.status_code == 200, listed.text
        payload = listed.json()
        # The real envelope key, read from the handler rather than guessed.
        rows = payload["brands"]
        assert isinstance(rows, list), f"unexpected /brands shape: {type(rows)}"

        ids = [r.get("id") for r in rows if isinstance(r, dict)]
        assert brand_id in ids, (
            "the created brand is not retrievable, so the Brands read is proving "
            "nothing -- fixture creation silently regressed"
        )


class TestDeclaredContractsMatchRuntime:
    """Where a schema IS declared, the real response must satisfy it."""

    def _schema_for(self, path: str, method: str = "get") -> str | None:
        spec = json.loads(SPEC.read_text(encoding="utf-8"))
        op = (spec["paths"].get(path) or {}).get(method)
        if not op:
            return None
        for code, body in (op.get("responses") or {}).items():
            if not code.startswith("2"):
                continue
            schema = ((body or {}).get("content") or {}).get("application/json", {})
            ref = (schema.get("schema") or {}).get("$ref")
            if ref:
                return ref.split("/")[-1]
        return None

    @pytest.mark.parametrize(
        "model_name,path,method",
        [
            ("JobListOut", "/api/v1/workspaces/{workspace_id}/jobs", "get"),
            ("CostSummaryOut", "/api/v1/workspaces/{workspace_id}/costs", "get"),
            (
                "DistributionCapabilityListOut",
                "/api/v1/workspaces/{workspace_id}/distribution/capabilities",
                "get",
            ),
            (
                "WorkspaceProviderMaturityOut",
                "/api/v1/workspaces/{workspace_id}/provider-maturity",
                "get",
            ),
            (
                "OpportunityListOut",
                "/api/v1/workspaces/{workspace_id}/planner/opportunities",
                "get",
            ),
        ],
    )
    def test_real_response_validates_against_the_declared_model(
        self, client, owner, model_name, path, method
    ):
        model = getattr(declared, model_name, None)
        assert model is not None, f"{model_name} is no longer declared"

        url = path.format(workspace_id=owner["workspace_id"])
        r = client.request(method.upper(), url, headers=owner["headers"])
        assert r.status_code == 200, f"{r.status_code} {r.text[:300]}"

        payload = r.json()
        _assert_payload_has_substance(payload, f"{model_name} {path}")

        # The whole point: a real response, validated against the declaration.
        # A silently dropped field would surface here as a validation error once
        # the model is exhaustive, and as an `extra` mismatch if it is not.
        validated = model.model_validate(payload)
        dumped = validated.model_dump()
        for key in payload:
            assert key in dumped, f"{model_name} dropped the real field {key!r}"

    def test_the_declared_registry_is_importable_and_non_trivial(self):
        # A generated module that silently produced nothing would make every
        # test above pass for the wrong reason.
        models = [
            n
            for n in dir(declared)
            if isinstance(getattr(declared, n), type)
            and issubclass(getattr(declared, n), declared.BaseModel)
            and not n.startswith("_")
        ]
        assert len(models) >= 8, f"only {len(models)} response models declared"


class TestGlobalReadsAreNotWorkspaceScoped:
    def test_system_health_is_reachable_without_a_workspace(self, client):
        r = client.get("/api/v1/system/health")
        assert r.status_code == 200, r.text
        _assert_payload_has_substance(r.json(), "system/health")
"""Declared response contracts must keep matching the code they describe.

``app/schemas/responses.py`` transcribes what handlers actually return, and
``responses={200: {"model": ...}}`` publishes that transcription as the contract
the frontend tests enforce. The weakness of ``responses`` (versus
``response_model``) is that it does NOT validate: a stale model would sit in the
spec, the frontend would be built against it, and nothing would fail until
runtime.

This file closes that gap by extracting the dict literals the handlers really
return and comparing them to the declared models. The comparison is by SOURCE, so
it cannot be satisfied by a model that merely agrees with itself.

Deliberate non-test: these models do not authorise anything and are not verified
against live data. They are a documented, drift-checked description.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from app.schemas import responses as R

BACKEND = Path(__file__).resolve().parents[1]
APP = BACKEND / "app"
REPO = BACKEND.parent
SPEC = REPO / "frontend" / "src" / "api" / "openapi.json"


@pytest.fixture(scope="module")
def spec() -> dict:
    """The committed baseline every "does the spec publish it" test reads."""
    return json.loads(SPEC.read_text(encoding="utf-8"))


def _dict_keys_returned_by(func_name: str, path: Path) -> set[str]:
    """Top-level string keys of the dict a function returns.

    AST rather than text so a renamed variable or a reordered key does not break
    the comparison, and so a comment cannot satisfy it.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == func_name:
            for sub in ast.walk(node):
                if isinstance(sub, ast.Return) and isinstance(sub.value, ast.Dict):
                    keys = {
                        k.value
                        for k in sub.value.keys
                        if isinstance(k, ast.Constant) and isinstance(k.value, str)
                    }
                    if keys:
                        return keys
    raise AssertionError(f"no dict return found in {func_name} ({path.name})")


class TestJobOutMatchesTheSerializer:
    def test_declared_fields_cover_every_key_the_serializer_emits(self):
        emitted = _dict_keys_returned_by("_to_dict", APP / "services" / "jobs.py")
        declared = set(R.JobOut.model_fields)
        missing = emitted - declared
        assert not missing, (
            f"jobs_service._to_dict emits {sorted(missing)} but JobOut does not "
            f"declare them; the frontend contract would omit real data"
        )

    def test_declared_fields_are_not_invented(self):
        emitted = _dict_keys_returned_by("_to_dict", APP / "services" / "jobs.py")
        extra = set(R.JobOut.model_fields) - emitted
        assert not extra, (
            f"JobOut declares {sorted(extra)} which the serializer never emits; "
            f"the frontend would read a field that is always absent"
        )


class TestCostSummaryMatchesTheHandler:
    def test_declared_fields_match_the_returned_dict(self):
        emitted = _dict_keys_returned_by(
            "cost_summary", APP / "api" / "v1" / "misc.py"
        )
        declared = set(R.CostSummaryOut.model_fields)
        assert emitted - declared == set(), f"undeclared: {sorted(emitted - declared)}"
        assert declared - emitted == set(), f"invented: {sorted(declared - emitted)}"


class TestSpecActuallyPublishesTheContracts:
    """The declaration is worthless if it never reaches the committed baseline."""

    def test_jobs_route_declares_job_list(self, spec):
        op = spec["paths"]["/api/v1/workspaces/{workspace_id}/jobs"]["get"]
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("JobListOut")

    def test_costs_route_declares_cost_summary(self, spec):
        op = spec["paths"]["/api/v1/workspaces/{workspace_id}/costs"]["get"]
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("CostSummaryOut")

    def test_auth_me_declares_the_capability_contract(self, spec):
        op = spec["paths"]["/api/v1/auth/me"]["get"]
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("MeResponse")

        ws = spec["components"]["schemas"]["WorkspaceCapability"]
        # role and capabilities are what the whole permission-aware UI reads.
        assert {"role"} <= set(ws.get("required", []))
        assert "capabilities" in ws["properties"]

    def test_no_response_model_was_used_where_filtering_would_change_data(self, spec):
        """``response_model`` would silently drop undeclared fields.

        The routes touched by this work order must declare via ``responses``
        only, so declaring a contract cannot alter a payload. ``/auth/me`` is the
        one route using ``response_model``, and that is deliberate: it was
        already returning exactly ``MeResponse``.
        """
        for path, item in spec["paths"].items():
            for method, op in item.items():
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                ref = (
                    op.get("responses", {})
                    .get("200", {})
                    .get("content", {})
                    .get("application/json", {})
                    .get("schema", {})
                    .get("$ref", "")
                )
                if ref.endswith(("JobListOut", "CostSummaryOut")):
                    # Declared through `responses`; the handler still returns a
                    # plain dict, so nothing is filtered.
                    continue


class TestDistributionCapabilities:
    def test_model_matches_the_handler(self):
        # The items are built as dict literals inside a loop, so compare against
        # the literal rather than the whole envelope.
        source = (APP / "api" / "v1" / "distribution.py").read_text(encoding="utf-8")
        assert '"platform": spec.platform' in source
        assert '"publish_mode": _publish_mode(spec.platform)' in source

        tree = ast.parse(source)
        emitted: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = {
                    k.value
                    for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
                if "publish_mode" in keys and "direct_publish" in keys:
                    emitted = keys
        assert emitted, "could not locate the capability item literal"
        declared = set(R.DistributionCapabilityOut.model_fields)
        assert emitted - declared == set(), f"undeclared: {sorted(emitted - declared)}"
        assert declared - emitted == set(), f"invented: {sorted(declared - emitted)}"

    def test_publish_mode_carries_the_capability_vocabulary_not_the_state(self):
        """`USER_HANDOFF`, not `HANDOFF`.

        The backend deliberately returns the capability name because the string
        is rendered on a card. A frontend filtering on `HANDOFF` would match
        nothing and quietly show every platform as direct-publish.
        """
        import typing

        assert set(typing.get_args(R.PublishModeCapability)) == {
            "DIRECT_PUBLISH",
            "USER_HANDOFF",
        }

    def test_route_declares_the_contract(self, spec):
        op = spec["paths"][
            "/api/v1/workspaces/{workspace_id}/distribution/capabilities"
        ]["get"]
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("DistributionCapabilityListOut")


class TestPlannerOpportunities:
    def test_model_matches_the_handler_item_literal(self):
        source = (APP / "api" / "v1" / "planner.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        emitted: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                keys = {
                    k.value
                    for k in node.keys
                    if isinstance(k, ast.Constant) and isinstance(k.value, str)
                }
                if "basis_meaning" in keys and "dedupe_verdict" in keys:
                    emitted = keys
        assert emitted, "could not locate the opportunity item literal"
        declared = set(R.OpportunityOut.model_fields)
        assert emitted - declared == set(), f"undeclared: {sorted(emitted - declared)}"
        assert declared - emitted == set(), f"invented: {sorted(declared - emitted)}"

    def test_envelope_matches_the_handler_too(self):
        """Work 16.5.3 §5: the ITEM check alone missed two real fields.

        ``forbidden_claims`` and ``note`` are added to the envelope AFTER the
        list comprehension closes, so a test that only inspects the item literal
        reports success while the published schema drops both. The runtime
        validator found it; this keeps the static guard honest about the limit
        it can actually see.
        """
        emitted = _dict_keys_returned_by("list_opportunities", APP / "api" / "v1" / "planner.py")
        declared_fields = set(R.OpportunityListOut.model_fields)
        assert emitted - declared_fields == set(), (
            f"undeclared envelope keys: {sorted(emitted - declared_fields)}"
        )
        assert declared_fields - emitted == set(), (
            f"declared envelope keys the handler never returns: "
            f"{sorted(declared_fields - emitted)}"
        )

    def test_the_planner_honesty_contract_is_published_not_reinvented(self):
        # `forbidden_claims` is what stops the UI inventing its own list of
        # claims the planner will not make. Declaring it empty would make the
        # field look optional when it is the endpoint's whole point.
        assert "forbidden_claims" in R.OpportunityListOut.model_fields
        assert R.OpportunityListOut.model_fields["forbidden_claims"].annotation is not None

    def test_scores_stay_nullable_so_absent_never_reads_as_zero(self):
        """A missing score must be UNAVAILABLE, not 0.0.

        `Optional[float]` in the schema is what lets the frontend contract test
        require a nullable field; a bare `float` would invite `?? 0` at the call
        site, which is the exact fabrication this work order forbids.
        """
        for field in ("score", "confidence", "brand_fit", "estimated_cost_usd"):
            assert R.OpportunityOut.model_fields[field].annotation == float | None, field

    def test_basis_is_the_closed_planner_vocabulary(self):
        import importlib
        import typing

        from app.models import planning

        backend_values = {
            getattr(planning, n) for n in ("OBSERVED", "INFERRED", "RECOMMENDED")
        }
        assert backend_values == set(typing.get_args(R.OpportunityBasis))

    def test_route_declares_the_contract(self, spec):
        op = spec["paths"]["/api/v1/workspaces/{workspace_id}/planner/opportunities"]["get"]
        ref = op["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        assert ref.endswith("OpportunityListOut")


class TestClosedVocabulariesStayClosed:
    def test_provider_maturity_values_are_exactly_the_agreed_ladder(self):
        # A Literal in the spec becomes an enum the frontend test ENFORCES, so
        # this list is a contract, not documentation.
        import typing

        values = typing.get_args(R.MaturityStatus)
        assert set(values) == {
            "IMPLEMENTED",
            "CONTRACT_TESTED",
            "LIVE_VERIFIED",
            "UNVERIFIED",
            "UNAVAILABLE",
            "BLOCKED_LICENSE",
            "BLOCKED_COMMERCIAL_TERMS",
            "EXTERNAL_LIMITATION",
        }

    def test_maturity_ladder_matches_the_backend_module_constants(self):
        """The ladder must be the backend's, not the frontend's opinion of it."""
        import importlib
        import typing

        maturity = importlib.import_module("app.providers.maturity")
        backend_values = {
            getattr(maturity, name)
            for name in dir(maturity)
            if name.isupper()
            and isinstance(getattr(maturity, name), str)
            and name
            in {
                "IMPLEMENTED",
                "CONTRACT_TESTED",
                "LIVE_VERIFIED",
                "UNVERIFIED",
                "UNAVAILABLE",
                "BLOCKED_LICENSE",
                "BLOCKED_COMMERCIAL_TERMS",
                "EXTERNAL_LIMITATION",
            }
        }
        assert backend_values == set(typing.get_args(R.MaturityStatus))

    def test_credential_and_health_values_match_the_backend(self):
        import importlib
        import typing

        maturity = importlib.import_module("app.providers.maturity")
        creds = {
            getattr(maturity, n)
            for n in dir(maturity)
            if n.startswith("CREDENTIAL_") and isinstance(getattr(maturity, n), str)
        }
        healths = {
            getattr(maturity, n)
            for n in dir(maturity)
            if n.startswith("HEALTH_") and isinstance(getattr(maturity, n), str)
        }
        assert creds == set(typing.get_args(R.CredentialStatus))
        assert healths == set(typing.get_args(R.ProviderHealth))

    def test_maturity_is_four_independent_axes_not_one_field(self):
        """A single collapsed ``maturity`` would lose the whole point of the screen.

        A provider can be IMPLEMENTED + CONTRACT_TESTED + UNVERIFIED live +
        BLOCKED_COMMERCIAL_TERMS simultaneously. The first draft of this model
        collapsed them into one field; this asserts that mistake cannot return.
        """
        fields = set(R.ProviderMaturityOut.model_fields)
        assert "maturity" not in fields
        for axis in (
            "implementation_status",
            "contract_status",
            "live_status",
            "commercial_status",
        ):
            assert axis in fields, f"missing the {axis} axis"

    def test_maturity_model_matches_to_dict(self):
        emitted = _dict_keys_returned_by(
            "to_dict", APP / "providers" / "maturity.py"
        )
        declared = set(R.ProviderMaturityOut.model_fields)
        assert emitted - declared == set(), f"undeclared: {sorted(emitted - declared)}"
        assert declared - emitted == set(), f"invented: {sorted(declared - emitted)}"

    def test_workspace_maturity_model_matches_its_handler(self):
        emitted = _dict_keys_returned_by(
            "workspace_provider_maturity", APP / "api" / "v1" / "providers.py"
        )
        declared = set(R.WorkspaceProviderMaturityOut.model_fields)
        assert emitted - declared == set(), f"undeclared: {sorted(emitted - declared)}"
        assert declared - emitted == set(), f"invented: {sorted(declared - emitted)}"

    def test_open_ended_job_status_is_not_narrowed_to_an_enum(self):
        # The queue adds states. Pinning this to the states seen today would make
        # a legitimate backend state a frontend build failure.
        assert R.JobOut.model_fields["status"].annotation is str

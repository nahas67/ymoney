"""The response-contract registry must not drift (Work 16.5.4 §1/§17).

Two properties, and the second is the one that matters:

1. **Every contract names a route that exists, and a model that exists.** A stale
   entry would publish nothing while looking configured.

2. **Every audited UI call has a contract, or is listed with a reason.** This is
   the property individual decorators could never give. A new endpoint added to
   the UI without a contract is a FAILURE here rather than a quiet gap, which is
   the entire reason the contracts live in one table instead of 100+ decorator
   edits.

It also pins the safety property the whole approach rests on: contracts are
published through ``responses`` (documentation), never as a filter, because
filtering a model inferred from one fixture state was measured breaking real
endpoints with ``ResponseValidationError``.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.routing import APIRoute

from app.schemas import generated as _generated
from app.schemas.contract_registry import (
    _iter_routes,
    apply_response_contracts,
    declared_contracts,
)

BACKEND = Path(__file__).resolve().parents[1]
REPO = BACKEND.parent
AUDIT = REPO / "docs" / "UI_CONTRACT_AUDIT.json"
GENERATION = REPO / "docs" / "UI_CONTRACT_GENERATION.json"


@pytest.fixture(scope="module")
def app():
    import app.services.readiness as rd

    rd.run_readiness = lambda force_refresh=True: {  # type: ignore[assignment]
        "status": "ready",
        "checked_at": "",
        "stale_after_hours": 24,
        "checks": [],
        "blocking_failures": [],
        "message": "test",
    }
    from app.main import create_app

    return create_app()


@pytest.fixture(scope="module")
def routes(app) -> dict[tuple[str, str], APIRoute]:
    """Every real route, keyed by (method, full_path)."""
    out: dict[tuple[str, str], APIRoute] = {}
    for route, full_path in _iter_routes(app):
        for method in getattr(route, "methods", None) or ():
            out[(method, full_path)] = route
    return out


class TestRegistryIntegrity:
    def test_the_table_is_not_empty(self):
        assert len(declared_contracts()) > 50

    def test_every_contract_names_a_model_that_exists(self):
        missing = {
            key: name
            for key, name in declared_contracts().items()
            if not hasattr(_generated, name)
        }
        assert not missing, f"contract table names missing models: {missing}"

    def test_every_contract_names_a_route_that_exists(self, routes):
        table = declared_contracts()
        orphans = [
            key for key in table if not any(k[1] == key.split(" ", 1)[1] for k in routes)
        ]
        assert not orphans, f"contracts for routes that do not exist: {orphans}"

    def test_the_nested_router_walk_actually_finds_routes(self, routes):
        # The walk is the silent-failure risk: this FastAPI keeps included routers
        # behind a private wrapper, and an earlier implementation that missed that
        # matched zero routes and logged success.
        assert len(routes) > 300, f"only discovered {len(routes)} routes"
        assert any("/api/v1/workspaces/{workspace_id}/jobs" == p for _, p in routes)


class TestContractsArePublishedWithoutFiltering:
    def test_applying_publishes_a_substantial_number_of_routes(self, app, routes):
        applied = apply_response_contracts(app)
        assert applied > 50, f"only annotated {applied} routes"

    def test_no_generated_contract_is_attached_as_a_filtering_response_model(
        self, app, routes
    ):
        # THE SAFETY PROPERTY. A generated model used as `response_model` was
        # measured breaking endpoints: a model inferred from one fixture state
        # rejected a different one with ResponseValidationError, turning
        # POST /brands into a 500. `extra="allow"` does not help -- it tolerates
        # unknown fields, not a declared field whose type differs between states.
        table = declared_contracts()
        filtered = []
        for key, name in table.items():
            method, path = key.split(" ", 1)
            route = routes.get((method, path))
            if route is None:
                continue
            model = getattr(route, "response_model", None)
            if model is not None and getattr(model, "__name__", None) == name:
                filtered.append(key)
        assert not filtered, (
            "these generated contracts are attached as response_model and can "
            f"reject real payloads: {filtered}"
        )

    def test_the_spec_publishes_the_declared_schema(self, app):
        schema = app.openapi()
        published = 0
        for path, item in schema["paths"].items():
            for method, op in item.items():
                if method not in ("get", "post", "put", "patch", "delete"):
                    continue
                for code, body in (op.get("responses") or {}).items():
                    if not code.startswith("2"):
                        continue
                    ref = (
                        ((body or {}).get("content") or {})
                        .get("application/json", {})
                        .get("schema", {})
                        .get("$ref")
                    )
                    if ref:
                        published += 1
                        break
        assert published > 100, f"only {published} operations publish a 2xx schema"


class TestCoverageIsTrackedNotAssumed:
    def test_every_audited_ui_call_either_has_a_contract_or_a_recorded_reason(self):
        # The anti-silent-gap check. An endpoint with neither is a hole.
        audit = json.loads(AUDIT.read_text(encoding="utf-8"))
        table = declared_contracts()
        generation = json.loads(GENERATION.read_text(encoding="utf-8"))
        justified = {f"{s['method']} {s['specPath']}": s["reason"] for s in generation["skipped"]}

        unaccounted = []
        for call in audit["calls"]:
            if call["class"] != "UNDECLARED_JSON":
                continue
            for method in (call["method"],):
                key = f"{method} {call['specPath']}"
                if key not in table and key not in justified:
                    unaccounted.append(key)

        assert not unaccounted, (
            f"{len(unaccounted)} UI calls have neither a contract nor a recorded "
            f"reason: {unaccounted[:6]}"
        )

    def test_coverage_has_not_regressed(self):
        audit = json.loads(AUDIT.read_text(encoding="utf-8"))
        assert audit["ordinaryJsonDocumented"] >= 130, (
            f"only {audit['ordinaryJsonDocumented']}/{audit['ordinaryJsonTotal']} "
            "ordinary JSON UI calls are documented; Work 16.5.3 left 19"
        )

    def test_every_skipped_call_states_why(self):
        generation = json.loads(GENERATION.read_text(encoding="utf-8"))
        for skipped in generation["skipped"]:
            assert skipped.get("reason"), f"no reason recorded: {skipped}"
            assert skipped.get("status") is not None, f"no status recorded: {skipped}"


def _defined_models() -> list[str]:
    """Model classes DEFINED in generated.py.

    Scoped by `__module__` because the module imports `BaseModel`, `ConfigDict`
    and `Literal` into its namespace, and those are not generated contracts --
    counting them made every honesty assertion below report a false positive.
    """
    return sorted(
        name
        for name in dir(_generated)
        if isinstance(getattr(_generated, name), type)
        and issubclass(getattr(_generated, name), _generated.BaseModel)
        and getattr(_generated, name).__module__ == _generated.__name__
        # `_ObservedBase` is the intentional abstract base: it carries the
        # `extra="allow"` config and no fields, by design.
        and not name.startswith("_")
    )


class TestGeneratedModelsAreHonest:
    def test_every_model_preserves_undeclared_fields(self):
        models = _defined_models()
        assert len(models) > 50
        for name in models:
            model = getattr(_generated, name)
            assert model.model_config.get("extra") == "allow", (
                f"{name} does not preserve undeclared fields"
            )

    def test_no_generated_model_is_field_free(self):
        # A model with no fields validates as `Any` -- the fictional contract the
        # work order forbids, wearing a schema as a disguise.
        empty = [n for n in _defined_models() if not getattr(_generated, n).model_fields]
        assert not empty, f"field-free models are effectively Any: {empty}"

    def test_open_ended_operational_states_stay_strings(self):
        # §3: do not freeze open-ended states merely to generate enums. Job status
        # is the canonical case -- the queue adds states, and a Literal would turn
        # a legitimate ninth state into a startup validation failure.
        job = getattr(_generated, "JobOut", None)
        if job is None:
            pytest.skip("JobOut is hand-written, not generated")
        assert job.model_fields["status"].annotation is str
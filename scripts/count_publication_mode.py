"""How each operation publishes its contract: `response_model` vs `responses`.

Work 16.5.4 §2 requires the two counts reported separately, so they are measured
rather than estimated.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import app.services.readiness as rd  # noqa: E402

rd.run_readiness = lambda force_refresh=True: {  # type: ignore[assignment]
    "status": "ready", "checked_at": "", "stale_after_hours": 24,
    "checks": [], "blocking_failures": [], "message": "probe",
}

from fastapi.routing import APIRoute  # noqa: E402

from app.main import create_app  # noqa: E402
from app.schemas.contract_registry import _iter_routes  # noqa: E402

app = create_app()

via_model = 0
via_responses = 0
via_both = 0
total_with_contract = 0

for route, _path in _iter_routes(app):
    if not isinstance(route, APIRoute):
        continue

    model = getattr(route, "response_model", None)
    has_model = model is not None and model is not type(None)

    declared = (getattr(route, "responses", {}) or {}).get("200") or {}
    content = declared.get("content") or {}
    json_schema = (content.get("application/json") or {}).get("schema") or {}
    has_responses = bool(json_schema.get("$ref") or json_schema.get("type"))

    if not (has_model or has_responses):
        continue
    total_with_contract += 1

    if has_model and has_responses:
        via_both += 1
    elif has_responses:
        via_responses += 1
    else:
        via_model += 1

print("=" * 68)
print("§2  HOW EACH CONTRACT IS PUBLISHED")
print("=" * 68)
print(f"  operations with a declared contract   {total_with_contract}")
print(f"    via response_model only             {via_model}")
print(f"    via responses only                  {via_responses}")
print(f"    both                                {via_both}")
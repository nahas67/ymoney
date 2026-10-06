"""How many operations now declare a typed 2xx body, after the registry applies."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import app.services.readiness as rd  # noqa: E402

rd.run_readiness = lambda force_refresh=True: {  # type: ignore[assignment]
    "status": "ready",
    "checked_at": "",
    "stale_after_hours": 24,
    "checks": [],
    "blocking_failures": [],
    "message": "probe",
}

from app.main import create_app  # noqa: E402
from app.schemas.contract_registry import declared_contracts  # noqa: E402

app = create_app()
schema = app.openapi()
declared = declared_contracts()

total_ops = 0
with_ref = 0
for path, item in schema["paths"].items():
    for method, op in item.items():
        if method not in ("get", "post", "put", "patch", "delete"):
            continue
        total_ops += 1
        for code, body in (op.get("responses") or {}).items():
            if not code.startswith("2"):
                continue
            content = (body or {}).get("content") or {}
            ref = (content.get("application/json") or {}).get("schema") or {}
            if ref.get("$ref") or ref.get("type"):
                with_ref += 1
                break

print("=" * 70)
print(f"registry entries          {len(declared)}")
print(f"operations in the spec    {total_ops}")
print(f"operations with 2xx body  {with_ref}")
print(f"components.schemas        {len(schema['components']['schemas'])}")
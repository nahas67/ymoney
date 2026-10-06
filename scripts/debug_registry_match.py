"""Why did `apply_response_contracts` match zero routes?

Prints the actual `APIRoute.path` values next to the registry keys, so the
mismatch is visible rather than guessed at.
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
from app.schemas import generated  # noqa: E402
from app.schemas.contract_registry import declared_contracts  # noqa: E402

app = create_app()
table = declared_contracts()

print(f"registry entries: {len(table)}")
print()
print("first 5 registry keys:")
for k in list(table)[:5]:
    print(f"  {k}  -> {table[k]}  (model exists: {hasattr(generated, table[k])})")
print()

route_paths = sorted({r.path for r in app.routes if isinstance(r, APIRoute)})
print(f"APIRoute paths: {len(route_paths)}")
for p in route_paths[:5]:
    print(f"  {p}")

print()
print("exact key/path intersection:", len(set(table) & {f"{m} {p}" for p in route_paths for m in ("GET", "POST", "PUT", "PATCH", "DELETE")}))

# Compare one path ignoring the method.
key_paths = {k.split(" ", 1)[1] for k in table}
print("path-only intersection:", len(key_paths & set(route_paths)))
missing = sorted(key_paths - set(route_paths))
print(f"registry paths with no matching route: {len(missing)}")
for p in missing[:5]:
    print(f"  {p}")
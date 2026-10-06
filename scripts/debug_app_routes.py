"""What is actually in `app.routes` after `create_app()`?

`apply_response_contracts` matched nothing while `app.openapi()` reports 457
operations, which is contradictory. This prints the real contents so the two
observations can be reconciled instead of one of them being assumed wrong.
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import app.services.readiness as rd  # noqa: E402

rd.run_readiness = lambda force_refresh=True: {  # type: ignore[assignment]
    "status": "ready", "checked_at": "", "stale_after_hours": 24,
    "checks": [], "blocking_failures": [], "message": "probe",
}

from app.main import create_app  # noqa: E402

app = create_app()

print(f"len(app.routes) = {len(app.routes)}")
print()
print("route types:")
for name, count in Counter(type(r).__name__ for r in app.routes).most_common():
    print(f"  {name:20} {count}")
print()
print("first 10 routes:")
for r in app.routes[:10]:
    print(f"  {type(r).__name__:16} {getattr(r, 'path', getattr(r, 'path_format', '?'))}")
print()
print(f"openapi paths = {len(app.openapi()['paths'])}")

# Are the API routes perhaps mounted on a sub-application or a Mount?
for r in app.routes:
    app_attr = getattr(r, "app", None)
    if app_attr is not None:
        print(f"  MOUNT -> {getattr(r, 'path', '?')}  inner routes: {len(getattr(app_attr, 'routes', []))}")
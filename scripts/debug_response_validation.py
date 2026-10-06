"""Print the exact ResponseValidationError for each UI endpoint.

A generated model that REJECTS a real response is worse than no model: the route
goes 500 and the screen is broken. `extra="allow"` protects against unknown fields
but not against a TYPE that differs between the state observed during generation
and the state produced at runtime -- so the precise error matters.
"""

import sys
import traceback
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

import spec_request_builder as rb  # noqa: E402
import ui_contract_observer as obs  # noqa: E402

from app.main import create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

PROBE = [
    ("GET", "/api/v1/workspaces/{workspace_id}/ugc/presets"),
    ("GET", "/api/v1/workspaces/{workspace_id}/brands"),
    ("GET", "/api/v1/workspaces/{workspace_id}/content"),
    ("GET", "/api/v1/workspaces/{workspace_id}/jobs"),
    ("GET", "/api/v1/workspaces/{workspace_id}/costs"),
    ("GET", "/api/v1/workspaces/{workspace_id}/campaigns"),
    ("GET", "/api/v1/workspaces/{workspace_id}/planner/calendar"),
    ("GET", "/api/v1/workspaces/{workspace_id}/assets/media"),
    ("GET", "/api/v1/workspaces/{workspace_id}/notifications"),
    ("GET", "/api/v1/workspaces/{workspace_id}/reviews"),
    ("GET", "/api/v1/workspaces/{workspace_id}/members"),
    ("GET", "/api/v1/workspaces/{workspace_id}/analytics/overview"),
]

errors: list[str] = []


class Capture(TestClient):
    """Surface the server-side exception instead of a bare 500."""

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.raise_server_exceptions = False


import app.services.readiness as rd  # noqa: E402

rd.run_readiness = lambda force_refresh=True: {  # type: ignore[assignment]
    "status": "ready", "checked_at": "", "stale_after_hours": 24,
    "checks": [], "blocking_failures": [], "message": "probe",
}

app = create_app()
client = TestClient(app, raise_server_exceptions=True)
session = obs.register(client)
seeded = obs.seed(client, session)

print("=" * 78)
print("PROBE: does each endpoint still return 2xx under its declared contract?")
print("=" * 78)
for method, path in PROBE:
    url = rb.fill_path_params(path, session["workspace_id"], seeded)
    body = rb.request_body_for(path, method.lower())
    query = rb.query_for(path, method.lower())
    try:
        r = client.request(method, url, headers=session["headers"], json=body, params=query)
        status = r.status_code
    except Exception as exc:
        status = "EXC"
        msg = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        print(f"\n{method} {path}\n  RAISED {msg}")
        # Dig for the validation detail.
        cur = exc
        while cur is not None:
            if "errors()" in dir(cur):
                try:
                    for e in cur.errors()[:4]:
                        print(f"    loc={e.get('loc')} type={e.get('type')} msg={str(e.get('msg'))[:130]}")
                except Exception:
                    pass
            cur = cur.__cause__ or cur.__context__
            if cur is exc:
                break
        errors.append(path)
        continue
    print(f"\n{method} {path}\n  status {status}")
    if status == 200:
        print(f"  body   {r.text[:150]}")
    else:
        errors.append(path)

print()
print("=" * 78)
print(f"FAILING: {len(errors)}")
for p in errors:
    print(f"  {p}")
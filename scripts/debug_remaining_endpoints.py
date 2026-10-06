"""Why does each remaining endpoint still fail? URL + server detail, per endpoint.

Work 16.5.5 §1. The 32 uncontracted endpoints fail for a handful of distinct
reasons (missing parent, invalid body, an optional-looking query that is actually
mandatory, a genuinely unavailable provider). Guessing which is which wastes a
cycle per endpoint, so this prints the real URL and the real error text.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "backend"))

import spec_request_builder as rb  # noqa: E402
import ui_contract_observer as obs  # noqa: E402

from app.main import create_app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import app.services.readiness as rd  # noqa: E402

rd.run_readiness = lambda force_refresh=True: {  # type: ignore[assignment]
    "status": "ready", "checked_at": "", "stale_after_hours": 24,
    "checks": [], "blocking_failures": [], "message": "probe",
}

GENERATION = json.loads(
    (REPO / "docs" / "UI_CONTRACT_GENERATION.json").read_text(encoding="utf-8")
)
targets = [
    (s["method"], s["specPath"], s["status"])
    for s in GENERATION["skipped"]
]

app = create_app()
client = TestClient(app, raise_server_exceptions=False)
session = obs.register(client)
seeded = obs.seed(client, session)

print("=" * 90)
print("SEEDED:", ", ".join(f"{k}={'!' if v.startswith('!') else 'ok'}" for k, v in seeded.items()))
print("=" * 90)

for method, path, prior_status in targets:
    url = rb.fill_path_params(path, session["workspace_id"], seeded)
    override = obs.DOMAIN_SEED_BODIES.get((path, method.lower()))
    body = rb.substitute_fixture_ids(override if override is not None else rb.request_body_for(path, method.lower()), seeded)
    query = rb.query_for(path, method.lower())
    r = client.request(method, url, headers=session["headers"], json=body, params=query)
    short = url.replace(session["workspace_id"], "{ws}")
    print(f"\n[{r.status_code}] (was {prior_status}) {method} {path}")
    print(f"    url    {short}")
    if body is not None:
        print(f"    body   {json.dumps(body)[:150]}")
    if query:
        print(f"    query  {json.dumps(query)[:120]}")
    if r.status_code >= 400:
        print(f"    detail {r.text[:200]}")
"""Probe why nested-child routes still 404 once the parent fixture exists.

Work 16.5.4: 46 endpoints remain uncontracted, almost all of the shape
`/parent/{parent_id}/child`. The parent fixtures are created, yet the routes
answer 404. Rather than guess between "wrong id substituted" and "the child does
not exist independently of its parent", this prints the URL that was actually
requested and what came back.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "backend"))

import spec_request_builder as rb  # noqa: E402
import ui_contract_observer as obs  # noqa: E402

client = obs.new_client()
session = obs.register(client)
seeded = obs.seed(client, session)

print("=" * 78)
print("SEEDED IDS")
print("=" * 78)
for k, v in seeded.items():
    print(f"  {k:20} {v}")

PROBES = [
    ("GET", "/api/v1/workspaces/{workspace_id}/content/{content_id}/timeline"),
    ("GET", "/api/v1/workspaces/{workspace_id}/content/{content_id}/lineage"),
    ("GET", "/api/v1/workspaces/{workspace_id}/content/{content_id}/audit"),
    ("POST", "/api/v1/workspaces/{workspace_id}/campaigns/{campaign_id}/cancel"),
    ("GET", "/api/v1/workspaces/{workspace_id}/timelines/{timeline_id}/scenes"),
]

print()
print("=" * 78)
print("PROBES")
print("=" * 78)
for method, path in PROBES:
    url = rb.fill_path_params(path, session["workspace_id"], seeded)
    body = rb.request_body_for(path, method.lower())
    r = client.request(method, url, headers=session["headers"], json=body)
    print(f"\n{method} {path}")
    print(f"  url      {url}")
    print(f"  status   {r.status_code}")
    print(f"  body     {r.text[:180]}")
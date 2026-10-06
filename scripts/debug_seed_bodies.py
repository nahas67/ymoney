"""Show the derived request body for the seed routes that are still failing.

Work 16.5.4: seven fixture seeds answer 422 (or 405), which leaves 46 read
endpoints 404ing on a parent that does not exist. Rather than guess, print the
body the harness derives next to the spec's own schema, so the mismatch is
visible.
"""

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))

import spec_request_builder as rb  # noqa: E402

TARGETS = [
    ("/api/v1/workspaces/{workspace_id}/brands/effective", "put"),
    ("/api/v1/workspaces/{workspace_id}/calendar", "post"),
    ("/api/v1/workspaces/{workspace_id}/experiments", "post"),
    ("/api/v1/workspaces/{workspace_id}/knowledge/memories", "post"),
    ("/api/v1/workspaces/{workspace_id}/dubbing/plans", "post"),
    ("/api/v1/workspaces/{workspace_id}/ugc/projects", "post"),
    ("/api/v1/workspaces/{workspace_id}/webhooks", "post"),
    ("/api/v1/workspaces/{workspace_id}/content", "post"),
    ("/api/v1/workspaces/{workspace_id}/planner/opportunities", "post"),
]

for path, method in TARGETS:
    op = (rb.spec().get("paths", {}).get(path) or {}).get(method)
    print("=" * 78)
    print(f"{method.upper()} {path}")
    if op is None:
        print("  NO SUCH OPERATION IN THE SPEC")
        continue
    body = rb.request_body_for(path, method)
    schema = None
    content = (op.get("requestBody") or {}).get("content") or {}
    if "application/json" in content:
        schema = rb._resolve(content["application/json"].get("schema"))
    print(f"  required : {schema.get('required') if schema else None}")
    print(f"  props    : {sorted((schema.get('properties') or {}).keys()) if schema else None}")
    print(f"  derived  : {json.dumps(body)}")
    print()
"""Print the declared request shape for the endpoints that still fail.

Answers "what body does this route actually want" from the OpenAPI document
rather than from guesswork, so a 422 can be read as a missing required field
instead of an invitation to invent values.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = json.loads(
    (REPO / "frontend" / "src" / "api" / "openapi.json").read_text(encoding="utf-8")
)

TARGETS = [
    ("/api/v1/workspaces/{workspace_id}/calendar/best-times", "patch"),
    ("/api/v1/workspaces/{workspace_id}/calendar/best-times", "delete"),
    ("/api/v1/workspaces/{workspace_id}/content/{content_id}/platform-variants", "post"),
    ("/api/v1/workspaces/{workspace_id}/campaigns/from-master", "post"),
    ("/api/v1/workspaces/{workspace_id}/localization/run", "post"),
    ("/api/v1/workspaces/{workspace_id}/content/estimate-cost", "get"),
    ("/api/v1/workspaces/{workspace_id}/avatars/render", "post"),
    ("/api/v1/workspaces/{workspace_id}/ugc/projects", "post"),
]


def resolve(node: dict) -> dict:
    while isinstance(node, dict) and "$ref" in node:
        parts = node["$ref"][2:].split("/")
        cur: object = SPEC
        for part in parts:
            cur = cur.get(part, {}) if isinstance(cur, dict) else {}
        node = cur if isinstance(cur, dict) else {}
    return node


for path, method in TARGETS:
    methods = SPEC["paths"][path]
    if method not in methods:
        # A method the UI calls but the document does not publish is itself a
        # finding: the frontend is exercising an operation with no spec.
        print(f"\n{method.upper()} {path}\n   NOT PUBLISHED; document has {sorted(methods)}")
        continue
    op = methods[method]
    print(f"\n{method.upper()} {path}")
    for param in op.get("parameters", []):
        sch = resolve(param.get("schema", {}))
        enum = sch.get("enum")
        print(f"   query {param['name']}{' (REQUIRED)' if param.get('required') else ''}"
              f"{' enum=' + str(enum) if enum else ''}")
    body = op.get("requestBody")
    if not body:
        print("   body  (none)")
        continue
    schema = resolve(body["content"]["application/json"]["schema"])
    required = set(schema.get("required", []))
    for name, sub in schema.get("properties", {}).items():
        sub = resolve(sub)
        enum = sub.get("enum")
        detail = f" enum={enum}" if enum else f" type={sub.get('type', '?')}"
        print(f"   body  {name}{' (REQUIRED)' if name in required else ''}{detail}")
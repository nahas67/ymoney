"""Generate docs/ymoney-postman.json from the live FastAPI OpenAPI spec.

Regeneration (no drift): `python scripts/gen_postman.py`.
Covered by backend/tests/test_postman.py.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DOCS_OUT = REPO_ROOT / "docs" / "ymoney-postman.json"

POSTMAN_SCHEMA = "https://schema.getpostman.com/json/collection/v2.1.0/collection.json"


def _postman_path(openapi_path: str) -> tuple[list, str]:
    """'/a/{b}/c' -> (['a', ':b', 'c'], '{{baseUrl}}/a/{{workspaceId}}/c' if workspace)."""
    segments: list = []
    raw_parts: list[str] = []
    for seg in openapi_path.strip("/").split("/"):
        if seg.startswith("{") and seg.endswith("}"):
            name = seg[1:-1]
            if name == "workspace_id":
                segments.append("{{workspaceId}}")
                raw_parts.append("{{workspaceId}}")
            else:
                segments.append(f":{name}")
                raw_parts.append(f":{name}")
        else:
            segments.append(seg)
            raw_parts.append(seg)
    return segments, "{{baseUrl}}/" + "/".join(raw_parts)


def build_collection(openapi: dict) -> dict:
    items_by_tag: dict[str, list] = {}
    total = 0
    for path in sorted(openapi.get("paths", {})):
        methods = openapi["paths"][path] or {}
        for method in sorted(methods):
            if method not in ("get", "post", "put", "patch", "delete", "head", "options"):
                continue
            op = methods[method] or {}
            segments, raw = _postman_path(path)
            query: list = []
            for param in op.get("parameters", []) or []:
                if param.get("in") != "query":
                    continue
                query.append({
                    "key": param["name"],
                    "value": str(param.get("example", param.get("schema", {}).get("default", "<value>"))),
                    "description": param.get("description", ""),
                    "disabled": not param.get("required", False),
                })
            body: dict = {}
            req_body = op.get("requestBody") or {}
            content = (req_body.get("content") or {}).get("application/json") or {}
            if content:
                body = {"mode": "raw", "raw": "{}", "options": {"raw": {"language": "json"}}}
            request: dict = {
                "method": method.upper(),
                "header": [],
                "url": {"raw": raw, "host": ["{{baseUrl}}"], "path": segments, "query": query},
                "description": "\n\n".join(
                    s for s in (op.get("summary", ""), op.get("description", "")) if s
                ),
            }
            if body:
                request["body"] = body
            name = op.get("summary") or f"{method.upper()} {path}"
            tags = op.get("tags") or ["misc"]
            items_by_tag.setdefault(tags[0], []).append({"name": name, "request": request})
            total += 1

    folders = [
        {"name": tag, "item": sorted(items, key=lambda i: i["name"])}
        for tag, items in sorted(items_by_tag.items())
    ]

    # Key-auth demo: duplicate GET .../api-keys/me with X-API-Key instead of bearer.
    for folder in folders:
        for item in folder["item"]:
            req = item["request"]
            if req["method"] == "GET" and req["url"]["raw"].endswith("/api-keys/me"):
                key_variant = json.loads(json.dumps(item))
                key_variant["name"] = item["name"] + " (X-API-Key)"
                key_variant["request"]["header"] = [{"key": "X-API-Key", "value": "{{apiKey}}"}]
                key_variant["request"]["auth"] = {"type": "noauth"}
                folder["item"].append(key_variant)
                break

    return {
        "info": {            "name": "YMONEY API",
            "schema": POSTMAN_SCHEMA,
            "description": (
                "Generated from the live spec (`GET /openapi.json`) — regenerate with "
                "`python scripts/gen_postman.py`. Set collection variables first: "
                "baseUrl, workspaceId, jwt (login), apiKey (mint via api-keys)."
            ),
        },
        "auth": {"type": "bearer", "bearer": [{"key": "token", "value": "{{jwt}}"}]},
        "variable": [
            {"key": "baseUrl", "value": "http://127.0.0.1:8100"},
            {"key": "workspaceId", "value": "YOUR_WORKSPACE_ID"},
            {"key": "jwt", "value": "YOUR_JWT_ACCESS_TOKEN"},
            {"key": "apiKey", "value": "YOUR_YM_API_KEY"},
        ],
        "item": folders,
    }


def main() -> int:
    import os
    import tempfile

    sys.path.insert(0, str(REPO_ROOT / "backend"))
    _tmp = tempfile.mkdtemp(prefix="ymoney-postman-")
    os.environ["DATABASE_URL"] = f"sqlite:///{_tmp}/postman.db"
    os.environ.setdefault("VIDEO_ENGINE", "mock")
    from app.main import create_app
    from fastapi.testclient import TestClient

    client = TestClient(create_app(), raise_server_exceptions=False)
    r = client.get("/openapi.json")
    r.raise_for_status()
    collection = build_collection(r.json())
    DOCS_OUT.write_text(json.dumps(collection, indent=2) + "\n", encoding="utf-8")
    n_req = sum(len(f["item"]) for f in collection["item"])
    print(f"wrote {DOCS_OUT} ({len(collection['item'])} folders, {n_req} requests)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

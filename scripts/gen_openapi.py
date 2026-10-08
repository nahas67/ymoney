"""Dump the live OpenAPI schema for the frontend contract layer.

The frontend contract tests check every rebuilt screen against THIS file, which
is generated from the running application, so a stale frontend assumption
fails the build rather than shipping.

Usage:  backend\\.venv\\Scripts\\python scripts\\gen_openapi.py
"""
import json
import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.main import app  # noqa: E402
from app.schemas.contract_registry import _iter_routes  # noqa: E402

schema = app.openapi()

# FastAPI cannot infer a returned FileResponse/Response from an unannotated
# function. Correct the documentation in this generated artifact, without
# changing product routes or fabricating an ordinary JSON contract. Each entry
# asserts its source evidence before documenting the media response.
binary = {
    ("GET", "/api/v1/workspaces/{workspace_id}/exports/{export_id}/download"): ("FileResponse", "application/octet-stream"),
    ("POST", "/api/v1/workspaces/{workspace_id}/voice-preview"): ("Response", "audio/mpeg"),
}
for route, path in _iter_routes(app):
    for method in getattr(route, "methods", ()):
        evidence = binary.get((method, path))
        if evidence is None:
            continue
        constructor, media = evidence
        source = inspect.getsource(inspect.unwrap(route.endpoint))
        if constructor == "Response" and "_audio_response(" in source:
            from app.api.v1.preview import _audio_response

            source += inspect.getsource(_audio_response)
        if f"return {constructor}(" not in source:
            raise RuntimeError(f"binary response evidence changed: {method} {path}")
        response = schema["paths"][path][method.lower()]["responses"]["200"]
        response["content"] = {media: {"schema": {"type": "string", "format": "binary"}}}
        if media == "audio/mpeg":
            response["content"]["audio/wav"] = {"schema": {"type": "string", "format": "binary"}}
        response["x-contract-evidence"] = f"{route.endpoint.__module__}.{route.endpoint.__name__}: return {constructor}"

# Sorted so the committed file has a stable diff. Regenerating an unchanged app
# must produce a byte-identical file, or "did the contract change?" becomes
# unanswerable in review.
paths = {k: schema["paths"][k] for k in sorted(schema["paths"])}
schemas = schema.get("components", {}).get("schemas", {})
schema["paths"] = paths
schema["components"] = {
    "schemas": {k: schemas[k] for k in sorted(schemas)},
}

out = ROOT / "frontend" / "src" / "api" / "openapi.json"
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(schema, indent=2, sort_keys=False) + "\n",
               encoding="utf-8")

operations = sum(
    len([m for m in v if m in ("get", "post", "put", "patch", "delete")])
    for v in paths.values()
)
print(f"OPENAPI paths={len(paths)} operations={operations} "
      f"schemas={len(schema['components']['schemas'])}")
print(f"WROTE {out.relative_to(ROOT)} ({out.stat().st_size:,} bytes)")

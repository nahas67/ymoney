"""Dump the live OpenAPI schema for the frontend contract layer.

The frontend contract tests check every rebuilt screen against THIS file, which
is generated from the running application, so a stale frontend assumption
fails the build rather than shipping.

Usage:  backend\\.venv\\Scripts\\python scripts\\gen_openapi.py
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from app.main import app  # noqa: E402

schema = app.openapi()

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
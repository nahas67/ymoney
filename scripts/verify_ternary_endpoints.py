"""Verify the four endpoints the Work 16.5 route sweep reported as EMPTY 2xx
schemas, against the CURRENT committed openapi.json.

Those four are reached through a ternary or a helper (`active ?
`/performance/overview?...` : ""`), which the call-site scanner cannot extract,
so the audit reported 0 gaps while the document published an empty schema. This
script reads the document directly and prints, per 2xx operation, whether a
`$ref` is declared. Regenerate openapi.json first -- this never trusts a stored
summary.
"""

import json
import pathlib

SPEC = pathlib.Path("frontend/src/api/openapi.json")
TARGETS = [
    "/api/v1/workspaces/{workspace_id}/performance/overview",
    "/api/v1/workspaces/{workspace_id}/brands/effective",
    "/api/v1/workspaces/{workspace_id}/campaigns/{campaign_id}",
    "/api/v1/workspaces/{workspace_id}/knowledge/sources/{connector_id}/documents",
]
VERBS = ("get", "post", "put", "patch", "delete")


def main() -> int:
    spec = json.loads(SPEC.read_text(encoding="utf-8"))
    paths = spec["paths"]
    schemas = spec.get("components", {}).get("schemas", {})
    bad: list[str] = []

    for path in TARGETS:
        ops = paths.get(path)
        if ops is None:
            print(f"MISSING  {path}")
            bad.append(f"{path}: not published")
            continue
        for verb, op in ops.items():
            if verb not in VERBS:
                continue
            for code, body in (op.get("responses") or {}).items():
                if not code.startswith("2"):
                    continue
                schema = ((body or {}).get("content") or {}).get(
                    "application/json", {}
                ).get("schema") or {}
                ref = schema.get("$ref")
                name = ref.split("/")[-1] if ref else None
                resolved = bool(name and name in schemas)
                print(
                    f"{'OK     ' if resolved else 'EMPTY  '} {path} {verb} {code}"
                    f" -> {name or 'no $ref'}"
                )
                if not resolved:
                    bad.append(f"{path} {verb} {code}")

    print()
    print(f"checked {len(TARGETS)} previously-empty endpoints; "
          f"{len(bad)} still undeclared")
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
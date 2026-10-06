#!/usr/bin/env python
"""Work 16.5.7 §7 -- merge and gate the route release matrix.

`frontend/e2e/route-matrix.spec.ts` measures twelve facts per route against a
real backend.  This script is the part that makes the artefact trustworthy: it
re-reads the authoritative route list from `frontend/src/routes/registry.ts`,
merges the static facts, and EXITS NON-ZERO when the matrix and the registry
have drifted apart.

Why parse the registry instead of importing a JSON list
-------------------------------------------------------
The registry is TypeScript and the single source of truth for navigation, the
router and the route tests.  A generator that carried its own list of the 22
paths would be a second source of truth, and the exact drift this work order
cares about -- a route added to the registry with no matrix entry -- would go
unnoticed until someone read both files side by side.  So the list is PARSED
here and a new route enters the matrix automatically, or this script fails.

Failure modes (all exit non-zero, all named on stdout)
-----------------------------------------------------
  1. a registry route has no entry in the matrix      (missing route)
  2. a matrix route is not in the registry            (stale route)
  3. the same path appears twice                      (duplicate)
  4. a required dimension is absent or null           (incomplete)
  5. a dimension still holds the string "pending"     (unmeasured)
  6. the registry itself declares a duplicate path    (registry fault)

Determinism
-----------
Sorted keys, sorted routes, no timestamp anywhere.  The content hash printed at
the end is a SHA-256 over the canonical JSON of everything except the
`generatedBy` provenance line, so two runs over identical inputs print the same
hash and a diff in git means a real change.

Usage
-----
    backend\\.venv\\Scripts\\python.exe scripts\\gen_route_release_matrix.py
    backend\\.venv\\Scripts\\python.exe scripts\\gen_route_release_matrix.py --check
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parent.parent
REGISTRY = REPO / "frontend" / "src" / "routes" / "registry.ts"
MATRIX = REPO / "docs" / "UI_ROUTE_RELEASE_MATRIX.json"
OPENAPI = REPO / "frontend" / "src" / "api" / "openapi.json"

#: The twelve dimensions, in the order the work order states them.
DIMENSIONS: tuple[str, ...] = (
    "render",
    "loading",
    "populated",
    "empty",
    "error",
    "403",
    "contract",
    "responsive_1440",
    "responsive_1024",
    "responsive_768",
    "responsive_360",
    "a11y",
)

#: Honest, non-measured values.  They are ALLOWED, because pretending a
#: dimension was observed when it was not is worse than recording why.
HONEST_STRINGS = frozenset(
    {"unobserved", "not-seedable", "provider-gated", "skipped", "not-applicable"}
)

#: The one value that means "nobody has done this yet".  Never allowed.
PENDING = "pending"

ROUTE_ARRAY = re.compile(r"export\s+const\s+ROUTES\s*:\s*AppRoute\[\]\s*=\s*\[", re.M)
ENTRY_PATH = re.compile(r"\bpath\s*:\s*\"([^\"]+)\"")
ENTRY_LABEL = re.compile(r"\blabel\s*:\s*\"([^\"]*)\"")
ENTRY_PERMISSION = re.compile(r"\bpermission\s*:\s*(null|\"[^\"]*\")")
ENTRY_GROUP = re.compile(r"\bgroup\s*:\s*\"([^\"]+)\"")
ENTRY_STATE = re.compile(r"\bstate\s*:\s*\"([^\"]+)\"")


class GateFailure(Exception):
    """One or more release gates failed.  Carries every failure, not the first."""


def parse_registry(text: str) -> list[dict[str, Any]]:
    """Parse the ROUTES array into ordered entries.

    A deliberately small parser: the array is a literal in a source file, so the
    only things that can vary are whitespace and the order of keys.  Anything
    that needs a real parser would be a reason to generate the registry instead.
    """
    start = ROUTE_ARRAY.search(text)
    if start is None:
        raise GateFailure("registry.ts declares no `ROUTES: AppRoute[]` array")

    depth = 0
    end = None
    for i in range(start.end() - 1, len(text)):
        ch = text[i]
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i
                break
    if end is None:
        raise GateFailure("registry.ts ROUTES array is not closed")

    body = text[start.end() : end]
    routes: list[dict[str, Any]] = []
    for entry in _split_entries(body):
        path = ENTRY_PATH.search(entry)
        if path is None:
            raise GateFailure(f"a ROUTES entry has no `path`: {entry.strip()[:80]!r}")
        label = ENTRY_LABEL.search(entry)
        permission = ENTRY_PERMISSION.search(entry)
        group = ENTRY_GROUP.search(entry)
        state = ENTRY_STATE.search(entry)
        routes.append(
            {
                "path": path.group(1),
                "label": label.group(1) if label else "",
                "permission": (
                    None if permission is None or permission.group(1) == "null" else permission.group(1).strip('"')
                ),
                "group": group.group(1) if group else "",
                "state": state.group(1) if state else "",
                "hidden": bool(re.search(r"\bhidden\s*:\s*true", entry)),
            }
        )
    return routes


def _split_entries(body: str) -> list[str]:
    """Split the array body on top-level commas between `{ ... }` entries."""
    entries: list[str] = []
    depth = 0
    buf: list[str] = []
    in_str: str | None = None
    for ch in body:
        if in_str is not None:
            buf.append(ch)
            if ch == in_str:
                in_str = None
            continue
        if ch in "\"'`":
            in_str = ch
            buf.append(ch)
            continue
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        if ch == "," and depth == 0:
            entries.append("".join(buf))
            buf = []
            continue
        buf.append(ch)
    tail = "".join(buf).strip()
    if tail:
        entries.append(tail)
    return [e for e in (e.strip() for e in entries) if e]


def component_names(app_tsx: str) -> dict[str, str]:
    """registry path -> the component App.tsx renders for it."""
    imports: dict[str, str] = {}
    for m in re.finditer(r'import\s+([A-Za-z0-9_]+)\s+from\s+"([^"]+)"', app_tsx):
        imports[m.group(1)] = m.group(2)
    out: dict[str, str] = {}
    for m in re.finditer(r'"(/[^"]*)"\s*:\s*<([A-Za-z0-9_]+)', app_tsx):
        out[m.group(1)] = imports.get(m.group(2), m.group(2))
    return out


def is_valid_value(value: Any) -> bool:
    """A dimension value the generator is willing to publish."""
    if value is None:
        return False
    if isinstance(value, bool):
        return True
    if isinstance(value, dict):
        # `contract` is structured; it is validated separately.
        return bool(value)
    if isinstance(value, str):
        if value == PENDING:
            return False
        return value in HONEST_STRINGS
    return False


def validate(matrix_routes: list[dict[str, Any]], registry: list[dict[str, Any]]) -> list[str]:
    """Every gate, collected.  Returns the list of failures (empty == green)."""
    failures: list[str] = []

    registry_paths = [r["path"] for r in registry]
    matrix_paths = [r.get("path") for r in matrix_routes]

    dupes_registry = sorted({p for p in registry_paths if registry_paths.count(p) > 1})
    if dupes_registry:
        failures.append(f"registry declares duplicate path(s): {', '.join(dupes_registry)}")

    dupes_matrix = sorted({p for p in matrix_paths if matrix_paths.count(p) > 1})
    if dupes_matrix:
        failures.append(f"matrix has duplicate path(s): {', '.join(dupes_matrix)}")

    registry_set = set(registry_paths)
    matrix_set = {p for p in matrix_paths if p}

    missing = sorted(registry_set - matrix_set)
    if missing:
        failures.append(
            "registry route(s) missing from the matrix: "
            + ", ".join(missing)
            + "  -- add them by running `npx playwright test route-matrix`"
        )
    stale = sorted(matrix_set - registry_set)
    if stale:
        failures.append(
            f"matrix route(s) not in the registry (stale): {', '.join(stale)}  -- the registry is authoritative"
        )

    by_path = {r.get("path"): r for r in matrix_routes}
    for path in sorted(registry_set & matrix_set):
        entry = by_path[path]
        for dim in DIMENSIONS:
            if dim not in entry:
                failures.append(f"{path}: dimension `{dim}` is absent")
                continue
            value = entry[dim]
            if dim == "contract":
                if not isinstance(value, dict):
                    failures.append(f"{path}: `contract` is not an object")
                elif value.get("pending"):
                    failures.append(f"{path}: `contract` is pending")
                elif not isinstance(value.get("endpoints"), int):
                    failures.append(f"{path}: `contract.endpoints` is missing or not a count")
                continue
            if not is_valid_value(value):
                if value is None:
                    failures.append(f"{path}: dimension `{dim}` is null")
                elif value == PENDING:
                    failures.append(f"{path}: dimension `{dim}` is still \"pending\"")
                else:
                    failures.append(
                        f"{path}: dimension `{dim}` holds {value!r}, which is neither a boolean nor an honest string "
                        f"({', '.join(sorted(HONEST_STRINGS))})"
                    )
    return failures


def canonical(doc: dict[str, Any]) -> str:
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def build(
    registry: list[dict[str, Any]],
    observed: list[dict[str, Any]],
    components: dict[str, str],
    seeds: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """The merged artefact: static facts from the registry, measurements kept."""
    by_path = {r.get("path"): r for r in observed}
    routes: list[dict[str, Any]] = []
    for entry in registry:
        path = entry["path"]
        row = dict(by_path.get(path) or {})
        merged: dict[str, Any] = {
            "path": path,
            "label": entry["label"],
            "permission": entry["permission"],
            "group": entry["group"],
            "state": entry["state"],
            "hidden": entry["hidden"],
            "component": row.get("component") or components.get(path),
        }
        for dim in DIMENSIONS:
            merged[dim] = row.get(dim)
        merged["notes"] = sorted(row.get("notes") or [])
        routes.append(merged)
    routes.sort(key=lambda r: r["path"])

    doc: dict[str, Any] = {
        "schema": "ymoney.route-release-matrix/1",
        "generatedBy": (
            "scripts/gen_route_release_matrix.py merged with measurements from "
            "frontend/e2e/route-matrix.spec.ts"
        ),
        "sources": {
            "registry": "frontend/src/routes/registry.ts",
            "app": "frontend/src/app/App.tsx",
            "openapi": "frontend/src/api/openapi.json",
            "measurement": "frontend/e2e/route-matrix.spec.ts",
        },
        "dimensions": list(DIMENSIONS),
        "honestStrings": sorted(HONEST_STRINGS),
        "routeCount": len(routes),
        "routes": routes,
    }
    # Kept, not dropped: which seeds actually landed is the evidence behind
    # every `populated` value, and a later reader needs it to judge one.
    if isinstance(seeds, dict):
        doc["seeds"] = seeds
    return doc


def summarise(doc: dict[str, Any]) -> str:
    """A readable table, fixed width, so a review is a diff not a scroll."""

    def cell(value: Any) -> str:
        if value is True:
            return "yes"
        if value is False:
            return "NO"
        if isinstance(value, dict):
            return f"{value.get('declared', '?')}/{value.get('endpoints', '?')}"
        return str(value)

    header = (
        f"{'route':<26}{'rnd':<5}{'load':<13}{'pop':<16}{'empty':<7}{'err':<5}{'403':<6}"
        f"{'contract':<10}{'1440':<6}{'1024':<6}{'768':<5}{'360':<5}{'a11y':<6}"
    )
    lines = [header, "-" * len(header)]
    for row in doc["routes"]:
        lines.append(
            f"{row['path']:<26}{cell(row['render']):<5}{cell(row['loading']):<13}"
            f"{cell(row['populated']):<16}{cell(row['empty']):<7}{cell(row['error']):<5}"
            f"{cell(row['403']):<6}{cell(row['contract']):<10}"
            f"{cell(row['responsive_1440']):<6}{cell(row['responsive_1024']):<6}"
            f"{cell(row['responsive_768']):<5}{cell(row['responsive_360']):<5}{cell(row['a11y']):<6}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--check",
        action="store_true",
        help="validate only; do not rewrite docs/UI_ROUTE_RELEASE_MATRIX.json",
    )
    args = parser.parse_args(argv)

    if not REGISTRY.exists():
        print(f"[matrix] FAIL: registry not found at {REGISTRY}", file=sys.stderr)
        return 2
    if not MATRIX.exists():
        print(
            f"[matrix] FAIL: {MATRIX} does not exist.\n"
            f"[matrix]       Run: cd frontend && npx playwright test route-matrix",
            file=sys.stderr,
        )
        return 2

    registry = parse_registry(REGISTRY.read_text(encoding="utf-8"))
    observed_doc = json.loads(MATRIX.read_text(encoding="utf-8"))
    observed = observed_doc.get("routes")
    if not isinstance(observed, list):
        print("[matrix] FAIL: the matrix document has no `routes` array", file=sys.stderr)
        return 2

    failures = validate(observed, registry)
    if failures:
        print(f"[matrix] FAIL: {len(failures)} gate(s) did not pass", file=sys.stderr)
        for line in failures:
            print(f"  - {line}", file=sys.stderr)
        print(
            "\n[matrix] The registry is authoritative. Re-measure with:\n"
            "         cd frontend && npx playwright test route-matrix",
            file=sys.stderr,
        )
        return 1

    app_tsx = (REPO / "frontend" / "src" / "app" / "App.tsx").read_text(encoding="utf-8")
    doc = build(registry, observed, component_names(app_tsx), observed_doc.get("seeds"))

    if OPENAPI.exists():
        spec = json.loads(OPENAPI.read_text(encoding="utf-8"))
        doc["openapiPaths"] = len(spec.get("paths", {}))

    payload = canonical(doc)
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    doc["contentSha256"] = digest

    if not args.check:
        MATRIX.write_text(json.dumps(doc, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    print(f"[matrix] registry routes : {len(registry)}")
    print(f"[matrix] matrix routes  : {len(doc['routes'])}")
    print(f"[matrix] content sha256 : {digest}")
    print()
    print(summarise(doc))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except GateFailure as exc:  # pragma: no cover - defensive
        print(f"[matrix] FAIL: {exc}", file=sys.stderr)
        sys.exit(2)

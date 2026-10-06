"""Classify every UI-consumed backend call by what its response actually is.

Work 16.5.3 §1/§3. The frontend contract suite resolves 191 UI calls; only 19
have a declared 2xx schema. Before writing 172 more models it is worth knowing
which of those are ordinary JSON at all -- a file stream, an SSE feed, a 204, a
root-mounted internal probe, or a deliberately open provider blob has no shape to
declare, and inventing one would be fiction.

So this classifies each call:

    SCHEMA_COVERED     -- declares a 2xx $ref today
    NO_CONTENT         -- 204, or a handler that returns None
    STREAM             -- text/event-stream / streaming media
    FILE               -- a binary/attachment response
    DYNAMIC_BY_DESIGN  -- intentionally open external-provider payload
    ROOT_INTERNAL      -- mounted on the app root, include_in_schema=False
    UNDECLARED_JSON    -- ordinary JSON with no declared schema  <-- the work
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "frontend" / "src" / "api" / "openapi.json"
FRONT = REPO / "frontend" / "src"

spec = json.loads(SPEC.read_text(encoding="utf-8"))
paths: dict = spec["paths"]

# ---------------------------------------------------------------------------
# 1. Extract UI calls from frontend source.
# ---------------------------------------------------------------------------

# api("GET", "/x"), wsApi.get("/x"), wsApi.post("/x", {...})
CALL_PATTERNS = [
    re.compile(r'api\(\s*"(?P<m>GET|POST|PUT|PATCH|DELETE)"\s*,\s*`?(?P<p>[^"`,$]+)'),
    re.compile(r'wsApi\.(?P<m>get|post|put|patch|del)\(\s*`?(?P<p>[^"`,)]+)'),
    re.compile(r'fetch\(\s*`?(?P<p>[^"`,)]+)'),
]

WS_PREFIX = re.compile(r'^(?P<ws>[^/]+)/\{workspace_id\}(?P<rest>/.*)?$')


def normalise(raw: str) -> tuple[str, str | None]:
    """(method, path) with the workspace placeholder preserved as a template."""
    raw = raw.strip().rstrip("?&")
    if not raw.startswith("/"):
        return "", None
    # Strip an in-code query string; templated params stay.
    raw = raw.split("?")[0]
    m = WS_PREFIX.match(raw[1:])
    if m:
        ws, rest = m.group("ws"), m.group("rest") or ""
        return f"{ws}/{{workspace_id}}{rest}", None
    return raw, None


calls: set[tuple[str, str]] = set()
per_file: dict[tuple[str, str], list[str]] = defaultdict(list)

sources = [
    p
    for p in list(FRONT.rglob("*.ts")) + list(FRONT.rglob("*.tsx"))
    if ".test." not in p.name
]
for f in sources:
    text = f.read_text(encoding="utf-8", errors="replace")
    rel = str(f).replace("\\", "/").split("frontend/src/")[-1]
    for pat in CALL_PATTERNS:
        for m in pat.finditer(text):
            raw_method = m.groupdict().get("m")
            raw = m.group("p")
            p, _ = normalise(raw)
            if not p:
                continue
            if raw_method is None:
                # `fetch(...)` with the verb held in an options object; the route
                # still matters, so record it under a wildcard rather than guess.
                calls.add(("GET", p))
                per_file[("GET", p)].append(rel)
                continue
            method = raw_method.upper()
            if method == "DEL":
                method = "DELETE"
            calls.add((method, p))
            per_file[(method, p)].append(rel)

# ---------------------------------------------------------------------------
# 2. Resolve each call to a spec path (mirrors the vitest resolver: exact, then
#    the literal-subset candidates, and ambiguity is reported not guessed).
# ---------------------------------------------------------------------------


def normalise_path(p: str) -> str:
    """Replace {workspace_id}-style segments the UI writes as bare segments."""
    return p


def candidates(method: str, p: str) -> list[str]:
    """Spec paths that could serve this call."""
    exact = f"/api/v1{p}"
    if exact in paths and method.lower() in paths[exact]:
        return [exact]
    segs = [s for s in p.split("/") if s]
    out = []
    for spec_path, item in paths.items():
        if method.lower() not in item:
            continue
        spec_segs = [s for s in spec_path.split("/") if s]
        if len(spec_segs) < len(segs) + 1:  # +1 for the /api/v1 prefix
            continue
        tail = spec_segs[-len(segs):] if segs else spec_segs
        ok = True
        for got, want in zip(segs, tail):
            if want.startswith("{") or want == got:
                continue
            ok = False
            break
        if ok:
            out.append(spec_path)
    return out


# ---------------------------------------------------------------------------
# 3. Classify.
# ---------------------------------------------------------------------------

STREAM_SUFFIX = {"stream", "events", "ws", "socket", "live"}
TEXT_MEDIA = ("text/event-stream", "text/plain")

CLASSES: dict[tuple[str, str], tuple[str, str]] = {}  # call -> (class, detail)


def declared(op: dict) -> bool:
    for code, body in (op.get("responses") or {}).items():
        if not code.startswith("2"):
            continue
        content = (body or {}).get("content") or {}
        json_schema = (content.get("application/json") or {}).get("schema") or {}
        if json_schema.get("$ref") or json_schema.get("type"):
            return True
    return False


for method, p in sorted(calls):
    cands = candidates(method, p)
    if not cands:
        CLASSES[(method, p)] = ("UNRESOLVED", "no matching spec path")
        continue
    if len(cands) > 1:
        CLASSES[(method, p)] = ("AMBIGUOUS", ", ".join(cands))
        continue

    spec_path = cands[0]
    op = paths[spec_path][method.lower()]

    if declared(op):
        CLASSES[(method, p)] = ("SCHEMA_COVERED", spec_path)
        continue

    codes = sorted((op.get("responses") or {}).keys())
    # 204 / no 2xx with content at all
    if "204" in codes or not any(c.startswith("2") for c in codes):
        CLASSES[(method, p)] = ("NO_CONTENT", f"codes={codes}")
        continue

    content_types = set()
    for c, b in (op.get("responses") or {}).items():
        if c.startswith("2"):
            content_types |= set((b or {}).get("content", {}).keys())
    if any("event-stream" in c for c in content_types):
        CLASSES[(method, p)] = ("STREAM", f"types={sorted(content_types)}")
        continue
    if any(c.startswith("application/octet") or c.startswith("video/") or c.startswith("audio/") or c.startswith("image/") for c in content_types):
        CLASSES[(method, p)] = ("FILE", f"types={sorted(content_types)}")
        continue

    CLASSES[(method, p)] = ("UNDECLARED_JSON", spec_path)

# ---------------------------------------------------------------------------
# 4. Report.
# ---------------------------------------------------------------------------

counts = Counter(c for c, _ in CLASSES.values())
total = len(CLASSES)

print("=" * 78)
print(f"UI-CONSUMED CALLS AUDIT   ({total} unique method+path)")
print("=" * 78)
for name in (
    "SCHEMA_COVERED",
    "UNDECLARED_JSON",
    "NO_CONTENT",
    "STREAM",
    "FILE",
    "DYNAMIC_BY_DESIGN",
    "ROOT_INTERNAL",
    "UNRESOLVED",
    "AMBIGUOUS",
):
    if counts.get(name):
        print(f"  {name:20} {counts[name]:4}")

covered = counts.get("SCHEMA_COVERED", 0)
ordinary = total - counts.get("UNRESOLVED", 0) - counts.get("AMBIGUOUS", 0)
print()
print(f"  resolved calls                 {ordinary}")
print(f"  already schema-covered         {covered}")
print(f"  still undeclared JSON          {counts.get('UNDECLARED_JSON', 0)}")
print(
    f"  coverage                       "
    f"{covered}/{ordinary} = "
    f"{(covered / ordinary * 100) if ordinary else 0:.1f}%"
)

# Where the work is: group undeclared calls by the router file that serves them.
print()
print("=" * 78)
print("UNDECLARED JSON, grouped by spec path prefix")
print("=" * 78)
groups: dict[str, list[str]] = defaultdict(list)
for (method, p), (cls, detail) in CLASSES.items():
    if cls == "UNDECLARED_JSON":
        groups[detail].append(f"{method} {p}")

for spec_path in sorted(groups):
    rows = sorted(groups[spec_path])
    print(f"\n{spec_path}   ({len(rows)})")
    for r in rows:
        print(f"    {r}")

(REPO / "docs" / "UI_CONTRACT_AUDIT.json").write_text(
    json.dumps(
        {
            "total": total,
            "counts": dict(counts),
            "classification": {
                f"{m} {p}": {"class": c, "detail": d} for (m, p), (c, d) in CLASSES.items()
            },
        },
        indent=2,
    ),
    encoding="utf-8",
)
print()
print("wrote docs/UI_CONTRACT_AUDIT.json")
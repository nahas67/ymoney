"""Derive minimal valid request bodies and path arguments from the OpenAPI spec.

Work 16.5.4 §1. The first generation pass covered 71 of 172 endpoints. The other
65 were skipped for honest but mechanical reasons: 37 returned 422 because a
mutating route was called with no body, 26 returned 404 because a path parameter
was left as a literal ``{brand_id}``, and 2 returned 400 for a missing query.

Hand-writing ~40 request bodies and threading a dozen ids through would be slow
and would rot the moment a request model changed. The contracts for those requests
ALREADY EXIST -- in the OpenAPI document, as requestBody schemas. So they are read
from there, which means a request-model change automatically produces a different
(possibly still-invalid) body rather than a silently stale hand-written one.

This is a TEST SCAFFOLD, not product code. It sends the least-demanding payload
that satisfies the declared schema so the RESPONSE can be observed. It never
invents a field the schema does not declare, and it prefers an enum member when
one exists, because a route that validates an enum will reject anything else.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
SPEC = REPO / "frontend" / "src" / "api" / "openapi.json"

_spec: dict | None = None


def spec() -> dict:
    global _spec
    if _spec is None:
        _spec = json.loads(SPEC.read_text(encoding="utf-8"))
    return _spec


def _resolve(schema: dict | None, depth: int = 0) -> dict:
    """Follow a local ``$ref``. Refuse to chase a cycle."""
    if not isinstance(schema, dict) or depth > 6:
        return {}
    ref = schema.get("$ref")
    if not ref or not ref.startswith("#/"):
        return schema
    node: Any = spec()
    for part in ref[2:].split("/"):
        node = node.get(part, {}) if isinstance(node, dict) else {}
    return _resolve(node, depth + 1)


def minimal_value(schema: dict | None, depth: int = 0) -> Any:
    """The least-demanding value satisfying ``schema``."""
    schema = _resolve(schema if schema is not None else {})
    if not schema or depth > 5:
        return None

    if "enum" in schema and schema["enum"]:
        return schema["enum"][0]
    if "const" in schema:
        return schema["const"]

    for key in ("anyOf", "oneOf", "allOf"):
        if key in schema and isinstance(schema[key], list) and schema[key]:
            # Prefer the first NON-NULL branch: `str | None` must receive a string.
            branches = [_resolve(b) for b in schema[key]]
            concrete = [b for b in branches if b.get("type") != "null"]
            return minimal_value(concrete[0] if concrete else branches[0], depth + 1)

    kind = schema.get("type")
    if kind == "object" or "properties" in schema:
        props = schema.get("properties") or {}
        required = schema.get("required") or []
        out = {}
        for key in required:
            if key in props:
                out[key] = minimal_value(props[key], depth + 1)
        # An all-optional object legitimately accepts `{}`. Returning None here
        # made the caller send NO body at all, and a route whose fields all have
        # defaults then answered 422 -- which reads like a schema problem and is
        # really a harness bug.
        return out
    if kind == "array":
        items = schema.get("items")
        if items:
            value = minimal_value(items, depth + 1)
            return [value] if value is not None else None
        return None
    if kind == "boolean":
        return True
    if kind == "integer":
        lo = schema.get("minimum")
        return int(lo) if isinstance(lo, (int, float)) else 1
    if kind == "number":
        lo = schema.get("minimum")
        return float(lo) if isinstance(lo, (int, float)) else 1.0
    if kind == "string":
        fmt = schema.get("format")
        if fmt == "uuid":
            return str(uuid.uuid4())
        if fmt == "date-time":
            return "2024-01-01T00:00:00Z"
        if fmt == "email":
            return "observer@example.invalid"
        if fmt == "uri" or fmt == "url":
            return "https://example.invalid/x"
        min_len = schema.get("minLength") or 1
        return "observed"[: max(min_len, 1)].ljust(max(min_len, 1), "x")
    return None


def request_body_for(spec_path: str, method: str) -> Any | None:
    """A minimal valid body for this route, or ``None`` if it takes none."""
    op = (spec().get("paths", {}).get(spec_path) or {}).get(method.lower()) or {}
    content = (op.get("requestBody") or {}).get("content") or {}
    json_schema = content.get("application/json")
    if not json_schema:
        return None
    body = minimal_value(json_schema.get("schema"))
    return body


def required_query_for(spec_path: str, method: str) -> dict[str, Any]:
    """Required query parameters, so a route does not 400 on a missing filter."""
    op = (spec().get("paths", {}).get(spec_path) or {}).get(method.lower()) or {}
    out: dict[str, Any] = {}
    for param in op.get("parameters") or []:
        if not isinstance(param, dict) or param.get("in") != "query":
            continue
        if not param.get("required"):
            continue
        name = param.get("name")
        schema = param.get("schema") or {}
        if not name:
            continue
        value = minimal_value(schema)
        if value is not None:
            out[name] = value
    return out


#: Queries a route requires IN PRACTICE even though the schema marks them optional.
#: Each of these answered 400 with a spec-derived query because the handler
#: validates the value against a real vocabulary and then 404s or refuses when it
#: does not match. An optional-looking parameter that is actually mandatory is a
#: documentation defect worth reporting, so the value is supplied here rather than
#: worked around silently.
DOMAIN_QUERY_OVERRIDES: dict[tuple[str, str], dict[str, Any]] = {
    ("/api/v1/workspaces/{workspace_id}/performance/retention", "get"): {
        # RESOLVED, not literal. The route does `db.get(PublishedPost, post_id)`,
        # so this must be the seeded post's real id. It used to be the string
        # "observed-post", which no amount of query substitution could fix --
        # substitution replaces a SHORT or REPLACE_WITH_-prefixed value, and this
        # one is neither, so the harness sent a plausible-looking literal and
        # reported "post not found".
        "post_id": "REPLACE_WITH_SEEDED_PUBLISHED_POST",
    },
    ("/api/v1/workspaces/{workspace_id}/content/estimate-cost", "get"): {
        "variant": "observed",
    },
    ("/api/v1/workspaces/{workspace_id}/timelines/from-video", "get"): {
        "video_id": "observed-video",
    },
    ("/api/v1/workspaces/{workspace_id}/publishing/oauth/facebook/start", "get"): {
        "state": "observed",
    },
}


#: Endpoints whose success response CANNOT be observed in this environment, each
#: with the configuration that is absent.
#:
#: These are NOT classified `LEGITIMATELY_SCHEMALESS` and they are NOT given a
#: made-up schema. They are ordinary JSON endpoints whose 2xx shape is unknown
#: because reaching it requires an external system this machine does not have.
#: Naming the missing configuration is the honest report; a `Dict[str, Any]`
#: here would be a fabricated contract for a response nobody has seen.
PROVIDER_GATED: dict[tuple[str, str], str] = {
    (
        "/api/v1/workspaces/{workspace_id}/inbox/actions/{action_id}/send",
        "post",
    ): (
        "needs a live social provider; the fixture action is APPROVED and the "
        "route refuses at the provider boundary with 403 provider_error. "
        "MOCK_PUBLISHING does not cover the community reply lane."
    ),
    (
        "/api/v1/workspaces/{workspace_id}/publishing/oauth/facebook/start",
        "get",
    ): (
        "needs a Meta developer app (meta.app_id); the route answers 400 "
        "'Meta app ID not configured' before producing an authorize URL."
    ),
    (
        "/api/v1/workspaces/{workspace_id}/lipsync/jobs",
        "post",
    ): (
        "needs a lip-sync backend (MuseTalk weights + CUDA GPU, or an external "
        "LIPSYNC endpoint); the route answers 503 with its own remediation text."
    ),
}


def substitute_fixture_ids(
    body: Any,
    seeded: dict[str, str],
    route: str = "",
    method: str = "",
) -> Any:
    """Replace placeholder reference values with REAL seeded ids.

    A spec-derived body is structurally valid but frequently semantically empty:
    `minimal_value` renders every string as ``"o"``, so the server answers
    ``"content not found"``, ``"brand not found"`` or ``"unsupported platform
    'o'"``. That is a fixture problem, not a schema problem, and chasing it one
    endpoint at a time is what left 32 endpoints uncontracted in 16.5.4.

    So any string in an id-shaped field (``*_id``, ``*_ids``, ``*_ref``,
    ``master_content_id``, ``source_content_id``...) is replaced by a seeded id
    whose name matches the field. Concrete values are left alone.

    Matching is on the field NAME, not position, so an ``*_ids`` LIST is filled
    element-wise rather than replaced wholesale.
    """
    if isinstance(body, dict):
        return {k: _substitute_one(k, v, seeded) for k, v in body.items()}
    if isinstance(body, list):
        return [substitute_fixture_ids(v, seeded) for v in body]
    return body


#: Endpoints whose state preconditions CONFLICT, keyed by
#: ``(spec_path, method, parameter)``.
#:
#: These cannot be expressed as a per-parameter alias, because two endpoints
#: share one parameter name and need DIFFERENT rows. The concrete case:
#: `approve`, `reject` and `send` all read and write `CommunityAction.state`, and
#: their preconditions are mutually exclusive -- reject leaves the row
#: `rejected`, after which send answers 409 "action is rejected". That is
#: indistinguishable, from the outside, from a missing provider.
#:
#: An earlier attempt keyed these as ``"action:send"`` and looked the value up
#: as ``f"{stem}:{method}"``. HTTP method is ``post`` for all three, so the key
#: never resolved and every one silently fell through to the shared row. A
#: fixture rule that silently does not apply is worse than no rule, so the key
#: here is the EXACT spec path plus method plus parameter name.
ROUTE_FIXTURE_OVERRIDES: dict[tuple[str, str, str], str] = {
    ("/api/v1/workspaces/{workspace_id}/inbox/actions/{action_id}/send", "post", "action_id"): "send_action",
    ("/api/v1/workspaces/{workspace_id}/inbox/actions/{action_id}/approve", "post", "action_id"): "action",
    ("/api/v1/workspaces/{workspace_id}/inbox/actions/{action_id}/reject", "post", "action_id"): "reject_action",
    ("/api/v1/workspaces/{workspace_id}/calendar/{entry_id}", "patch", "entry_id"): "entry",
    ("/api/v1/workspaces/{workspace_id}/calendar/{entry_id}", "delete", "entry_id"): "cancellable_entry",
    ("/api/v1/workspaces/{workspace_id}/ugc/projects/{project_id}/render", "post", "project_id"): "ugc_render_project",
    ("/api/v1/workspaces/{workspace_id}/brands/{brand_id}/assets", "post", "brand_id"): "brand",
    ("/api/v1/workspaces/{workspace_id}/brands/{brand_id}/assets", "delete", "brand_id"): "brand_with_asset",
}

#: Path parameters that are NOT object ids and so must not be id-substituted.
#:
#: `{agent_key}` looks id-shaped, so the substitution replaced it with a uuid and
#: `PUT /agents/config/{agent_key}` answered "unknown agent" -- a routing fault
#: manufactured by the harness. A parameter whose value comes from a fixed
#: vocabulary is given that vocabulary's real value instead.
PATH_PARAM_VALUES: dict[str, str] = {
    "agent_key": "analytics",
}

#: Body values that are placeholders the call site must fill from the clock.
#:
#: `PATCH /calendar/{entry_id}` validates `run_at` as a timestamp and then
#: REFUSES a past one ("run_at cannot be in the past"), so any fixed value is
#: wrong: correct when written, a 422 six months later. These are resolved at
#: request time instead of being baked into a table.
TIME_PLACEHOLDERS = frozenset(
    {"REPLACE_WITH_FUTURE_TIMESTAMP", "REPLACE_WITH_PAST_TIMESTAMP"}
)


def resolve_time_placeholders(value: Any) -> Any:
    """Fill clock-dependent placeholders in a request body."""
    if isinstance(value, dict):
        return {k: resolve_time_placeholders(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_time_placeholders(v) for v in value]
    if value == "REPLACE_WITH_FUTURE_TIMESTAMP":
        return (datetime.now(timezone.utc) + timedelta(days=3)).isoformat()
    if value == "REPLACE_WITH_PAST_TIMESTAMP":
        return (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
    return value

_ID_SUFFIXES = ("_ids", "_id", "_ref", "_key", "_uuid")

#: Field stems whose seeded fixture is stored under a DIFFERENT name.
#:
#: The substitution matches a field's stem against the seeded key names, so a
#: route that calls a glossary term `{term_id}` never matched the key `glossary`,
#: and `{sub_id}` never matched `webhook`. Both produced a well-formed uuid and a
#: clean, misleading 404 -- "glossary term not found" against a fixture that
#: plainly contained a term. The mapping is explicit rather than fuzzy on
#: purpose: a substring match that guesses `sub` -> `social_account` is worse
#: than a named entry that is right.
SEEDED_ALIASES: dict[str, str] = {
    "term": "glossary",
    "glossary_term": "glossary",
    "sub": "webhook",
    "subscription": "webhook",
    "profile": "avatar",
    "avatar_profile": "avatar",
    "variant": "variant",
    "item": "plan_item",
    "campaign_item": "plan_item",
    "master_content": "content",
    "source_content": "content",
    "brand": "brand",
    "video": "video",
    "post": "published_post",
}


def _seeded_for(
    field: str,
    seeded: dict[str, str],
    route: str = "",
    method: str = "",
) -> str | None:
    low = field.lower()
    for suffix in _ID_SUFFIXES:
        if low.endswith(suffix):
            stem = low[: -len(suffix)]
            break
    else:
        return None
    candidates = [k for k in seeded if not k.startswith("!")]
    # An EXACT (route, method, parameter) override wins over everything else.
    if route and method:
        for name in (low, stem, f"{stem}_id"):
            override = ROUTE_FIXTURE_OVERRIDES.get((route, method.lower(), name))
            if override and override in seeded:
                return seeded[override]
    # An explicit alias beats both an exact and a substring match, because the
    # alias is the only rule that knows `term_id` means the `glossary` fixture.
    aliased = SEEDED_ALIASES.get(stem)
    if aliased and aliased in seeded:
        return seeded[aliased]
    # Most specific name first: `social_account` before `account`.
    for key in sorted(candidates, key=len, reverse=True):
        if key == stem:
            return seeded[key]
    for key in sorted(candidates, key=len, reverse=True):
        if stem and stem in key:
            return seeded[key]
    return None


def _is_placeholder(value: str) -> bool:
    """Is this string something the generator invented, rather than real data?

    Two rules, and the second one exists because the first was too weak:
    `minimal_value` renders every string as `"o"` (length <= 2), and domain
    overrides use `REPLACE_WITH_*`. A literal like `"observed-post"` is neither,
    so it survived substitution and the endpoint answered "post not found" -- an
    invented value that looked deliberate, which is the hardest kind to spot in a
    log full of plausible ids.
    """
    if len(value) <= 2 or value.startswith("REPLACE_WITH_"):
        return True
    # An "observed"-prefixed string is this harness's own placeholder vocabulary,
    # used for exactly one purpose: to be replaced with a real fixture.
    return value.startswith("observed-") or value == "observed"


def _substitute_one(
    field: str,
    value: Any,
    seeded: dict[str, str],
    route: str = "",
    method: str = "",
) -> Any:
    if isinstance(value, list):
        if field.lower().endswith("_ids"):
            return [
                v
                for v in (
                    _seeded_for(field[:-1] + "_id", seeded, route, method) or item
                    for item in value
                )
                if v
            ]
        return [substitute_fixture_ids(v, seeded, route, method) for v in value]
    if not isinstance(value, str):
        return value
    replacement = _seeded_for(field, seeded, route=route, method=method)
    # A `REPLACE_WITH_SEEDED_<KEY>` placeholder names its fixture EXACTLY, so it
    # is honoured even when the field name does not map to anything. Without
    # this, `post_id` -> `published_post` had to be inferred; it was not, and
    # the endpoint reported "post not found".
    if value.startswith("REPLACE_WITH_SEEDED_"):
        named = value[len("REPLACE_WITH_SEEDED_") :].lower()
        for key in sorted(
            (k for k in seeded if not k.startswith("!")),
            key=len,
            reverse=True,
        ):
            if named in key or key in named:
                return seeded[key]
    # Only replace a value the generator clearly made up.
    replacement = _seeded_for(field, seeded, route=route, method=method)
    # Only replace a value the generator clearly made up.
    if replacement and _is_placeholder(value):
        return replacement
    return value


def query_for(
    spec_path: str,
    method: str,
    seeded: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Domain overrides merged over the spec-derived required query.

    The result is passed through the SAME id substitution as bodies, which is the
    fix for `?post_id=observed-post` and `?video_id=observed-video`: a query
    parameter naming a real object was left as an invented literal, so
    `GET /performance/retention` answered "post not found" against a workspace
    that contained a published post. Query parameters were simply never
    substituted -- bodies were, which is why the bug looked endpoint-specific.
    """
    out = required_query_for(spec_path, method)
    out.update(DOMAIN_QUERY_OVERRIDES.get((spec_path, method.lower()), {}))
    if seeded:
        out = substitute_fixture_ids(out, seeded)
    return out


#: Path parameters whose value must be a REAL object, not a well-formed uuid.
#: Substituted from the seeded ids; anything unmapped gets a uuid, which is what
#: produces a clean 404 rather than a confusing 500.
def fill_path_params(
    spec_path: str,
    workspace_id: str,
    seeded: dict[str, str],
    method_hint: str = "",
) -> str:
    # The workspace is substituted FIRST and excluded from the loop below.
    # Treating `workspace_id` as an ordinary path parameter made the stem
    # "workspace" match no seeded key, so it fell through to a random uuid and
    # EVERY route 404'd. A harness bug that looked exactly like a broken backend.
    out = spec_path.replace("{workspace_id}", workspace_id)

    for name in re.findall(r"\{([a-zA-Z_]+)\}", out):
        if name == "workspace_id":
            continue
        stem = name[:-3] if name.endswith("_id") else name
        # A parameter drawn from a fixed vocabulary gets a real member of it,
        # never a seeded id.
        literal = PATH_PARAM_VALUES.get(name)
        if literal is not None:
            out = out.replace("{" + name + "}", literal)
            continue
        # `_seeded_for` applies the same alias table the bodies use, so
        # `{term_id}` resolves to the glossary fixture and `{sub_id}` to the
        # webhook one. The old first-substring-wins loop had no aliases AND took
        # whichever seeded key happened to be inserted first -- `{sub_id}` matched
        # `social_account` on the substring "ac", so deleting a webhook addressed a
        # social account and reported "webhook not found".
        value = _seeded_for(
            name, seeded, route=spec_path, method=method_hint
        ) or _seeded_for(stem, seeded, route=spec_path, method=method_hint)
        if value is None:
            value = str(uuid.uuid4())
        out = out.replace("{" + name + "}", value)
    return out
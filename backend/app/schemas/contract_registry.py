"""Central response-contract registry (Work 16.5.4 §1/§2).

WHY A REGISTRY INSTEAD OF 172 DECORATOR EDITS
---------------------------------------------
The work order prefers a real ``response_model=``. These are real response
models -- FastAPI validates, serialises and publishes them exactly as if they had
been written on the decorator. What differs is only WHERE the declaration lives.

Attaching 172 contracts means editing 42 router files. Every one of those edits is
a chance to break a module's syntax, and during this programme that failure mode
actually occurred repeatedly (a mis-anchored string replacement injected an import
mid-function and broke ``misc.py`` and ``planner.py``). Spreading one mechanical
change across 42 files to satisfy a stylistic preference is a bad trade against
that risk, and it makes the contract set impossible to review as a whole.

So the declarations live here, in one table keyed by method and path, applied to
the route objects after registration. The properties that matter are unchanged:

  * ``response_model`` is set, so FastAPI VALIDATES the payload (with
    ``extra="allow"`` on every generated model, so nothing real is dropped);
  * the OpenAPI document publishes the full schema, including ``required``;
  * ``test_work16_5_4_contract_registry.py`` fails if a route in the audited
    queue has no entry, or if an entry names a route that does not exist.

SO THE REGISTRY CANNOT SILENTLY DRIFT
-------------------------------------
That two-way check is the property the individual decorators would not have given
you. A new endpoint added to the UI without a contract is a test failure, not a
quiet gap -- which is the entire reason this table exists.

``apply_response_contracts`` is idempotent and safe to call on any app instance.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from collections.abc import Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.routing import APIRoute

from app.schemas import generated as _generated

log = logging.getLogger(__name__)

REPO = Path(__file__).resolve().parents[3]
MAP_PATH = REPO / "backend" / "app" / "schemas" / "contract_map.json"

#: Attribute stamped on a route whose 2xx contract THIS module published, so a
#: second `apply_response_contracts` pass can tell its own output apart from a
#: hand-written decorator and stay idempotent instead of annotating nothing.
_GENERATED_MARKER = "_ymoney_generated_contract"


def _load_map() -> dict[str, str]:
    """``{"GET /api/v1/workspaces/{workspace_id}/jobs": "Jobs1"}``."""
    if not MAP_PATH.exists():
        return {}
    return json.loads(MAP_PATH.read_text(encoding="utf-8"))


def contract_for(method: str, path: str) -> Any | None:
    """The declared model for one route, or ``None`` if it has no contract."""
    name = _load_map().get(f"{method.upper()} {path}")
    if not name:
        return None
    return getattr(_generated, name, None)


def declared_contracts() -> dict[str, str]:
    """The whole table, for the drift tests."""
    return _load_map()


def _is_real_schema(declared: Any) -> bool:
    """Is this `response_model` an actual shape, or a bare `dict`/`Any` stand-in?

    ``dict``, ``object``, ``typing.Any`` and ``Mapping`` all describe "an object
    with unknown fields". FastAPI renders each of them as
    ``{"type": "object", "additionalProperties": true}``, so accepting one as a
    "declared contract" would publish no field, no required list and no nested
    type -- indistinguishable, to a consumer, from having no contract at all.

    A Pydantic model (or dataclass) exposes ``model_fields`` / ``__fields__``, so
    that is the discriminator. It is deliberately NOT "is it a class": ``dict`` is
    a class too, and treating it as a schema is the bug.
    """
    if declared in (dict, object, Any):
        return False
    origin = getattr(declared, "__origin__", None)
    if origin in (dict, object) or declared is Any:
        return False
    if getattr(declared, "__name__", "") in ("dict", "object", "Any"):
        return False
    return hasattr(declared, "model_fields") or hasattr(declared, "__fields__")


def _iter_routes(
    container: Any,
    seen: set[int] | None = None,
    prefix: str = "",
) -> Iterator[tuple[Any, str]]:
    """Yield ``(route, full_path)``, descending through lazily-mounted routers.

    THIS IS NOT OPTIONAL, and getting it wrong fails SILENTLY. The installed
    FastAPI does not flatten included routers into ``app.routes``: each appears as
    a private ``_IncludedRouter`` exposing the wrapped router as
    ``original_router`` and the mount prefix separately. An earlier version of this
    function iterated ``app.routes`` directly, matched nothing, logged
    "applied 0 routes", and published no contracts at all while the application
    looked perfectly healthy -- and ``app.openapi()`` still reported 457
    operations, which is exactly the kind of contradiction that must be chased down
    rather than explained away.

    Yields the FULL path (prefix composed), because the registry table is keyed by
    the published path, not the router-local one.
    """
    if seen is None:
        seen = set()
    if id(container) in seen:
        return
    seen.add(id(container))

    for route in getattr(container, "routes", ()) or ():
        if isinstance(route, APIRoute):
            yield route, prefix + route.path
            continue

        # A lazily-included router. The mount prefix is NOT an attribute of the
        # wrapper -- it lives on `include_context`, alongside the wrapped router
        # itself. Reading `getattr(route, "prefix", "")` silently yields "", which
        # produces router-local paths like "/auth/me" that match nothing in a
        # table keyed by the published "/api/v1/auth/me".
        ctx = getattr(route, "include_context", None)
        if ctx is not None:
            inner_prefix = getattr(ctx, "prefix", "") or ""
            inner_router = getattr(ctx, "included_router", None)
            if inner_router is not None:
                yield from _iter_routes(inner_router, seen, prefix + inner_prefix)
                continue

        inner = getattr(route, "original_router", None)
        if inner is not None:
            yield from _iter_routes(inner, seen, prefix + (getattr(route, "prefix", "") or ""))
            continue

        nested = getattr(route, "routes", None)
        if nested is not None:
            yield from _iter_routes(nested, seen, prefix)
            continue

        inner_router = getattr(route, "router", None)
        if inner_router is not None:
            yield from _iter_routes(inner_router, seen, prefix + (getattr(route, "prefix", "") or ""))
        else:
            log.debug("unrecognised route wrapper %s; skipping", type(route).__name__)


def apply_response_contracts(app: FastAPI) -> int:
    """Publish every declared contract. Returns how many routes were annotated.

    DOCUMENTATION, NOT FILTERING -- AND THAT IS A MEASURED DECISION
    -------------------------------------------------------------
    §2 prefers ``response_model=`` "when the runtime shape is stable", and permits
    ``responses={}`` "when response filtering would legitimately alter intentional
    payload semantics". That condition was met, and met in the worst way: with
    ``response_model`` attached, ``POST /brands`` began answering **500** and
    ``GET /ugc/presets`` raised ``ResponseValidationError``, because a model
    inferred from one fixture state rejected a different one. A model that can
    reject an endpoint's own real response is worse than no model -- the screen
    breaks while the spec still looks perfect.

    ``extra="allow"`` does not rescue this. It tolerates unknown FIELDS; it does
    nothing for a declared field whose TYPE differs between states (a count that
    is ``0`` in one state and ``0.0`` in another; an empty list that is ``[]`` in
    one state and ``[{}]`` in another).

    So every generated contract is published through ``responses``, which declares
    the full schema -- properties, ``required``, nested models, enums -- in the
    OpenAPI document without touching a byte of the wire. Enforcement of "the real
    response still matches the declared contract" belongs to the runtime suite,
    which fails on drift; see ``tests/test_work16_5_4_contract_registry.py``.

    A hand-written contract keeps whatever attachment it already has: an explicit
    ``response_model`` is never overridden here.

    ROUTE DISCOVERY IS NOT OPTIONAL. The installed FastAPI does not flatten
    included routers into ``app.routes``; each appears as a private
    ``_IncludedRouter`` whose prefix lives on ``include_context``. An earlier
    version iterated ``app.routes`` directly, matched nothing, logged
    "applied 0 routes", and published no contracts at all while the application
    looked perfectly healthy -- and ``app.openapi()`` still reported 457
    operations, which is exactly the contradiction that has to be chased down
    rather than explained away.
    """
    table = _load_map()
    if not table:
        return 0

    annotated = 0
    seen_paths: set[str] = set()

    for route, full_path in _iter_routes(app):
        # A `response_model` that is a real schema is a hand-written contract and
        # is never overridden.
        #
        # But `-> dict` / `-> Any` / `-> object` is NOT a contract: FastAPI
        # publishes it as `{"type": "object", "additionalProperties": true}`,
        # which is `Dict[str, Any]` spelled out -- exactly the fake coverage the
        # work order forbids, and the reason 18 endpoints sat in the undeclared
        # queue while the source looked annotated. The audit was right to call
        # them uncovered; the registry was wrong to protect them.
        declared = getattr(route, "response_model", None)
        if declared is not None and _is_real_schema(declared):
            continue

        model = None
        for method in sorted(getattr(route, "methods", None) or ()):
            name = table.get(f"{method} {full_path}")
            if name:
                model = getattr(_generated, name, None)
                if model is not None:
                    break

        if model is None:
            continue

        # A `responses={200: {"model": X}}` decorator is a HAND-WRITTEN contract
        # too, and skipping it here is what broke `GET /jobs` and `GET /costs`.
        #
        # The earlier guard only checked `response_model`, so on those two routes
        # the registry overwrote the declared `JobListOut` / `CostSummaryOut`
        # with a generated model -- and `test_work16_5_2_response_contracts`
        # failed on the published `$ref`. A generated model inferred from a
        # fixture is strictly weaker evidence than a named, reviewed schema, so
        # where both exist the hand-written one wins. That is the same rule the
        # `response_model` guard above already enforced, applied to the second
        # spelling FastAPI offers.
        existing_responses = getattr(route, "responses", None) or {}
        # Only the SUCCESS codes count, and only when a HUMAN declared them.
        #
        # Two refinements, both learned from a failing test:
        #  * FastAPI injects a `422` `HTTPValidationError` entry into
        #    `route.responses` itself, so testing every value found a "declared
        #    model" on nearly every route and suppressed 105 contracts.
        #  * `apply_response_contracts` is called on an app that may ALREADY have
        #    been through it, and the models it attached look identical to
        #    hand-written ones. Treating its own output as hand-written made a
        #    second pass annotate 0 routes. The route records that it was
        #    generated here, and that marker is what distinguishes the two.
        already_declared = (
            not getattr(route, _GENERATED_MARKER, False)
            and any(
                isinstance(entry, dict) and entry.get("model") is not None
                for code, entry in existing_responses.items()
                if str(code).startswith("2")
            )
        )
        if already_declared:
            continue

        if existing_responses:
            responses = existing_responses
        else:
            # MUST be written back. `getattr(...) or {}` yields a fresh dict for
            # an empty/missing mapping, and assigning into that copy leaves
            # `route.responses` untouched -- so 105 contracts were attached to a
            # temporary and the published schema count fell from 356 to 151 while
            # the log still claimed "applied 115 routes". The count was true and
            # the effect was nil.
            responses = {}
            route.responses = responses
        existing = responses.get("200")
        if not isinstance(existing, dict):
            existing = {"description": "Successful response"}
            responses["200"] = existing
        existing["model"] = model
        setattr(route, _GENERATED_MARKER, True)

        seen_paths.add(full_path)
        annotated += 1

    log.info(
        "published %d/%d declared response contracts (%d distinct paths)",
        annotated,
        len(table),
        len(seen_paths),
    )
    return annotated

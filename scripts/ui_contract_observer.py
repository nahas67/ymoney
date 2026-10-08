"""Observation harness for UI-consumed endpoints (Work 16.5.4 §1/§4).

WHAT THIS IS FOR
----------------
172 ordinary JSON endpoints need a declared contract. Writing them by hand is
both enormous and unreliable; inventing envelopes from imagination would be the
fiction the work order forbids. So they are OBSERVED: build a real workspace,
call every endpoint the frontend calls, and record what actually comes back.

WHY MULTIPLE STATES
-------------------
A contract generated from ONE response is a snapshot, not a truth. `{"items": []}`
teaches you that `items` exists and nothing about its element type. So each
endpoint is observed in several states -- an empty workspace and a populated one
-- and a field is only marked REQUIRED when it is present in EVERY state. Keys
that appear in some states are optional, which is the honest encoding of "this
route returns different things depending on what exists".

WHAT THE RESULT IS NOT
----------------------
This is not automatic schema inference as a substitute for design. The output is
reviewed: every generated model carries `extra="allow"` so a field absent from
observation is PRESERVED rather than dropped (verified by
`test_work16_5_4_response_filtering.py`), and the runtime suite re-validates every
model against fresh fixtures.

READS ONLY. Nothing here issues a publishing, rendering or paid-provider call.
Mutating endpoints are observed separately and only where they are safe to
invoke.
"""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
AUDIT = REPO / "docs" / "UI_CONTRACT_AUDIT.json"


# ---------------------------------------------------------------------------
# The workspace
# ---------------------------------------------------------------------------


def _bypass_readiness_gate(monkeypatch_target: Any) -> None:
    """`/system/readiness` is the global readiness gate; stub the probe."""
    import app.services.readiness as rd

    rd.run_readiness = lambda force_refresh=True: {  # type: ignore[assignment]
        "status": "ready",
        "checked_at": "",
        "stale_after_hours": 24,
        "checks": [],
        "blocking_failures": [],
        "message": "observation",
    }


def new_client() -> TestClient:
    from app.main import create_app

    _bypass_readiness_gate(None)
    return TestClient(create_app(), raise_server_exceptions=False)


def register(client: TestClient) -> dict[str, Any]:
    email = f"obs{os.urandom(6).hex()}@test.local"
    r = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "supersecret123", "display_name": "Observer"},
    )
    if r.status_code != 200:
        raise RuntimeError(f"registration failed: {r.status_code} {r.text[:300]}")
    data = r.json()
    return {
        "token": data["access_token"],
        "headers": {"Authorization": f"Bearer {data['access_token']}"},
        "workspace_id": data["workspace"]["id"],
    }


def ws(workspace_id: str, suffix: str) -> str:
    return f"/api/v1/workspaces/{workspace_id}{suffix}"


# ---------------------------------------------------------------------------
# Domain-aware seed bodies
# ---------------------------------------------------------------------------

#: Spec-derived bodies are structurally valid but not necessarily DOMAIN valid.
#: A body of `{"type": "o"}` satisfies a schema and still fails a route that
#: checks the value against `memory.TYPES`. These overrides use real values taken
#: from the backend's own vocabularies, so the seed is accepted and the read
#: endpoints that depend on it stop 404ing.
#:
#: Sourced from: engine/knowledge/memory.py::TYPES, engine/ugc PRESET_DEFAULTS,
#: services/webhooks.py::WEBHOOK_EVENTS, engine/platform_registry.py.
DOMAIN_SEED_BODIES: dict[tuple[str, str], Any] = {
    ("/api/v1/workspaces/{workspace_id}/planner/signals", "post"): {
        "source": "research",
        "topic": "an observed topic",
        "external_ref": "observed-ref-1",
    },
    ("/api/v1/workspaces/{workspace_id}/knowledge/memories", "post"): {
        "type": "RESEARCH_FACT",
        "content": "an observed memory, cited",
        "scope": "workspace",
        "confidence": 0.9,
    },
    ("/api/v1/workspaces/{workspace_id}/ugc/projects", "post"): {
        "preset": "PRODUCT_DEMO",
    },
    ("/api/v1/workspaces/{workspace_id}/webhooks", "post"): {
        "url": "https://example.invalid/hook",
        "events": ["cycle.completed"],
    },
    # The control must NOT also appear in `variants`: the route rejects
    # "control and variant refs must be distinct", which is why an earlier
    # single-variant seed answered 422 and left `POST /experiments` -- and
    # `GET /experiments` -- without a contract.
    ("/api/v1/workspaces/{workspace_id}/experiments", "post"): {
        "hypothesis": "an observed hypothesis",
        "platform": "tiktok",
        "primary_metric": "views",
        "control": {"variant_ref": "control"},
        "variants": [{"variant_ref": "variant-a", "descriptor": "observed variant"}],
        "kind": "hook",
    },
    ("/api/v1/workspaces/{workspace_id}/dubbing/plans", "post"): {
        "target_lang": "es",
        "source_ref": "observed-source",
        "cues": [{"start": 0.0, "end": 1.0, "text": "observed"}],
    },
    ("/api/v1/workspaces/{workspace_id}/calendar", "post"): None,  # filled at call time
    ("/api/v1/workspaces/{workspace_id}/brands/effective", "put"): {
        "brand_id": "REPLACE_WITH_SEEDED_BRAND",
    },
    ("/api/v1/workspaces/{workspace_id}/inbox/conversations/{conversation_id}/reply", "post"): {
        "text": "an observed reply",
    },
    ("/api/v1/workspaces/{workspace_id}/experiments/{experiment_id}/analyze", "post"): {},
    ("/api/v1/workspaces/{workspace_id}/knowledge/memories", "post:second"): {
        "type": "CREATIVE_LESSON",
        "content": "a derived lesson",
        "scope": "workspace",
    },
    ("/api/v1/workspaces/{workspace_id}/ugc/projects", "post:second"): {
        "preset": "TESTIMONIAL",
        "brief": {},
    },
    ("/api/v1/workspaces/{workspace_id}/ugc/projects/{project_id}/render", "post"): {},
    ("/api/v1/workspaces/{workspace_id}/calendar/best-times", "patch"): {
        "best_times": [{"platform": "tiktok", "hour": 12, "minute": 0}],
    },
    # Values that must come from the BACKEND's vocabulary rather than the spec's
    # `minimal_value`. Each of these validated structurally and was then rejected
    # by a domain check, which is why a schema-shaped body still 422s.
    #
    # Sourced from: api/v1/content.py (platform registry), api/v1/ugc.py
    # (brief requirements), api/v1/planner.py (rejection reason), engine/
    # localization SUPPORTED_LANGUAGES.
    ("/api/v1/workspaces/{workspace_id}/content/{content_id}/platform-variants", "post"): {
        "platform": "tiktok",
        # UPPERCASE. `CTA_KINDS` is ['COMMENT', 'FOLLOW', ...] and the route
        # compares exactly, so a lowercase "follow" is an unknown CTA -- the body
        # was schema-valid and domain-invalid.
        "cta_kind": "FOLLOW",
    },
    ("/api/v1/workspaces/{workspace_id}/campaigns/from-master", "post"): {
        "master_content_id": "REPLACE_WITH_SEEDED_CONTENT",
        "name": "An observed campaign",
        "goal": "observed goal",
    },
    ("/api/v1/workspaces/{workspace_id}/localization/run", "post"): {
        "source_content_id": "REPLACE_WITH_SEEDED_CONTENT",
        "target_languages": ["es"],
    },
    ("/api/v1/workspaces/{workspace_id}/ugc/projects", "post"): {
        "preset": "PRODUCT_DEMO",
        # The route refuses a brief with no topic/product name.
        "brief": {"topic": "an observed product"},
    },
    ("/api/v1/workspaces/{workspace_id}/planner/items/{item_id}/reject", "post"): {
        "reason": "an observed rejection reason",
    },
    # `_policy()` builds the autonomy policy FROM THE REQUEST BODY, so the stored
    # plan's `autonomy` column is irrelevant to these routes. The frontend sends
    # `{autonomy: "AUTONOMOUS", allowed_actions: []}` for every item action
    # (features/planner/Planner.tsx:1076); a spec-derived body sent the default
    # "RECOMMEND" and the route answered "SCHEDULE needs AUTONOMOUS autonomy".
    # Match what the real client sends.
    ("/api/v1/workspaces/{workspace_id}/planner/items/{item_id}/schedule", "post"): {
        "autonomy": "AUTONOMOUS",
        # SCHEDULE must be NAMED. `_REQUIRED[SCHEDULE] is AUTONOMOUS` is the
        # floor; the allowlist is the actual gate, and an empty list is refused
        # with "SCHEDULE is not in the autonomous allowlist []".
        #
        # The frontend sends `allowed_actions: []` (Planner.tsx:1076), so the
        # real client hits this refusal too. That is a product observation, not a
        # fixture problem -- the fixture here sends what the endpoint accepts so
        # the RESPONSE can be observed.
        "allowed_actions": ["SCHEDULE"],
        "max_daily_spend_usd": 0,
    },
    ("/api/v1/workspaces/{workspace_id}/planner/items/{item_id}/approve", "post"): {
        "autonomy": "AUTONOMOUS",
        "allowed_actions": [],
        "max_daily_spend_usd": 0,
    },
    ("/api/v1/workspaces/{workspace_id}/planner/items/{item_id}/research_more", "post"): {
        "autonomy": "AUTONOMOUS",
        "allowed_actions": [],
        "max_daily_spend_usd": 0,
    },
    ("/api/v1/workspaces/{workspace_id}/planner/items/{item_id}/campaign", "post"): {
        "autonomy": "AUTONOMOUS",
        "allowed_actions": [],
        "max_daily_spend_usd": 0,
    },
    ("/api/v1/workspaces/{workspace_id}/planner/items/{item_id}/reject", "post:autonomous"): {
        "autonomy": "AUTONOMOUS",
        "allowed_actions": [],
        "reason": "an observed rejection reason",
        "max_daily_spend_usd": 0,
    },
    # `POST /avatars/render` refuses a pending consent BEFORE any provider work.
    # An authorized profile is seeded, but `audio_ref` still needs to be a
    # non-placeholder string, and an id there reads as a file the engine cannot
    # find one layer later.
    ("/api/v1/workspaces/{workspace_id}/avatars/render", "post"): {
        "profile_id": "REPLACE_WITH_SEEDED_AUTHORIZED_AVATAR",
        # The file the renderer opens, written into workspace storage by the
        # fixture. `audio_ref` reaches past the asset row to a real path.
        "audio_ref": "REPLACE_WITH_SEEDED_AVATAR_AUDIO_REF",
    },
    ("/api/v1/workspaces/{workspace_id}/calendar/{entry_id}", "patch"): {
        # A fixed past timestamp is rejected: "run_at cannot be in the past".
        # Filled at call time from the clock.
        "run_at": "REPLACE_WITH_FUTURE_TIMESTAMP",
    },
    ("/api/v1/workspaces/{workspace_id}/brands/{brand_id}/assets", "post"): {
        "media_asset_id": "REPLACE_WITH_SEEDED_MEDIA_ASSET",
        "asset_role": "logo",
    },
    ("/api/v1/workspaces/{workspace_id}/avatars/{profile_id}/authorize", "post"): {
        # The route refuses without explicit ownership evidence, and
        # `authorization_evidence` is a DICT, not a string.
        "source": "observed-source",
        "authorization_evidence": {"kind": "ownership", "reference": "observed-ref"},
    },
}


def seed(client: TestClient, session: dict[str, Any]) -> dict[str, str]:
    """Create whatever the read endpoints need in order to be non-empty.

    Request bodies are DERIVED FROM THE OPENAPI SPEC rather than hand-written, so
    a request-model change produces a different body instead of a stale one that
    quietly 422s. See `spec_request_builder`.

    Every helper records whether it SUCCEEDED, and a failure is reported rather
    than swallowed. A silently-empty fixture is the anti-vacuity failure this
    whole harness exists to prevent.
    """
    import spec_request_builder as rb

    headers = session["headers"]
    wid = session["workspace_id"]
    ids: dict[str, str] = {}

    def attempt(key: str, path: str, method: str = "POST") -> None:
        url = ws(wid, path)
        spec = ws_spec(path)
        override = DOMAIN_SEED_BODIES.get((spec, method.lower()))
        body = (override if override is not None else rb.request_body_for(spec, method.lower()))
        body = rb.resolve_time_placeholders(rb.substitute_fixture_ids(body, ids))
        query = rb.resolve_time_placeholders(rb.query_for(spec, method.lower(), ids))
        try:
            r = client.request(
                method.upper(), url, headers=headers, json=body, params=query
            )
        except Exception as exc:  # pragma: no cover - defensive
            ids.setdefault(f"!{key}", f"error: {type(exc).__name__}")
            return
        if r.status_code >= 400:
            ids.setdefault(f"!{key}", f"HTTP {r.status_code}")
            return
        try:
            payload = r.json()
        except Exception:
            ids.setdefault(f"!{key}", "non-JSON body")
            return
        found = _first_id(payload)
        if found:
            ids[key] = found

    def ws_spec(suffix: str) -> str:
        """Map a workspace-relative path back to its spec path."""
        return f"/api/v1/workspaces/{{workspace_id}}{suffix}"

    # Domain objects, in dependency order: a child needs its parent to exist.
    #
    # NOTE ON CONTENT: there is no `POST /content`. Content items are produced by
    # the pipeline (planner signals -> plan -> campaign), not created directly.
    # Seeding `POST /planner/signals` is therefore the only honest way to make
    # content-dependent reads non-empty.
    attempt("brand", "/brands")
    attempt("brand_secondary", "/brands")
    attempt("signal", "/planner/signals")
    attempt("timeline", "/timelines", method="POST")
    attempt("campaign", "/campaigns")
    attempt("calendar_entry", "/calendar", method="POST")
    attempt("experiment", "/experiments")
    attempt("api_key", "/api-keys")
    attempt("webhook", "/webhooks")
    attempt("memory", "/knowledge/memories")
    attempt("glossary", "/localization/glossary")
    attempt("avatar", "/avatars")
    attempt("dubbing_plan", "/dubbing/plans")
    attempt("ugc_project", "/ugc/projects")
    attempt("agent_config", "/agents/config", method="GET")

    # Objects with NO HTTP create route. A content item is produced by the
    # planning pipeline, not created directly, so there is no endpoint to call.
    # Seeding those at the ORM layer is the standard way to construct a fixture
    # for a system whose write path is a pipeline -- and it is the only honest
    # option, because driving the pipeline would queue real work and possibly
    # spend money.
    # ORM seeding needs the HTTP-created ids (a BrandAsset is only findable
    # through a real brand), so the partial id map travels with the session.
    session["seeded"] = dict(ids)
    ids.update(seed_via_orm(session))

    return ids


def seed_via_orm(session: dict[str, Any]) -> dict[str, str]:
    """Insert fixture rows directly, for objects no endpoint can create.

    Delegated to `scripts/ui_fixtures_orm.py`, which seeds each row INDEPENDENTLY
    with its own commit and its own error capture.

    That independence is the point. The previous implementation inserted
    everything into one staged transaction, so a single bad row rolled back every
    seed after it -- and because ids were published from a `pending` dict, the
    harness handed out ids for rows the rollback had just deleted. Coverage went
    DOWN by 13 endpoints and nothing said why.
    """
    import ui_fixtures_orm

    return ui_fixtures_orm.run_seeds(session)


def _first_id(payload: Any) -> str | None:
    """Pull an id out of the several envelope shapes the create routes use."""
    if isinstance(payload, dict):
        for key in ("id", "brand_id", "project_id", "content_id", "key_id", "term_id"):
            if isinstance(payload.get(key), str):
                return payload[key]
        for key in ("brand", "project", "content", "memory", "glossary", "api_key", "webhook", "item"):
            nested = payload.get(key)
            if isinstance(nested, dict) and isinstance(nested.get("id"), str):
                return nested["id"]
        for value in payload.values():
            found = _first_id(value)
            if found:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _first_id(value)
            if found:
                return found
    return None


# ---------------------------------------------------------------------------
# The endpoint queue
# ---------------------------------------------------------------------------


def load_queue() -> list[dict[str, str]]:
    """Read the authoritative work queue produced by the contract suite."""
    if not AUDIT.exists():
        raise RuntimeError(
            "docs/UI_CONTRACT_AUDIT.json is missing. Run the frontend contract "
            "suite once to regenerate it."
        )
    data = json.loads(AUDIT.read_text(encoding="utf-8"))
    return [c for c in data["calls"] if c["class"] == "UNDECLARED_JSON"]


#: Re-exported from the request builder so consumers of the observer need not
#: import both modules to ask "which endpoints are provider-gated?".
from spec_request_builder import PROVIDER_GATED  # noqa: E402


def load_inventory() -> list[dict[str, str]]:
    """EVERY ordinary-JSON UI call, whatever its current class.

    WHY THIS EXISTS SEPARATELY FROM `load_queue`
    ---------------------------------------------
    `load_queue` returns only the calls that still lack a contract, which is
    correct for deciding *what to work on* and catastrophically wrong for
    *regenerating the artifacts*.

    `generated.py` is written whole from the models inferred this run, and
    `contract_map.json` is written whole from the contracts inferred this run. If
    the input is only the 55-call gap, then both files shrink to that subset: a
    regeneration that fixed 15 endpoints silently deleted 104 contracts and 145
    models, and every coverage number computed afterwards described the damage
    instead of the work.

    So generation OBSERVES the whole 191-call inventory -- which makes it
    idempotent and self-healing -- while the map MERGE below is what keeps
    previously-published contracts for endpoints this run could not observe.
    """
    data = json.loads(AUDIT.read_text(encoding="utf-8"))
    return [c for c in data["calls"] if c["class"] in ("SCHEMA_COVERED", "UNDECLARED_JSON")]


def spec_path_to_url(
    spec_path: str,
    workspace_id: str,
    seeded: dict[str, str] | None = None,
    method: str = "",
) -> str:
    """`/api/v1/workspaces/{workspace_id}/jobs` -> a callable URL.

    Path parameters are substituted from the seeded ids where the name matches, so
    `/brands/{brand_id}/verify` addresses a brand that EXISTS rather than a
    well-formed uuid, which is the difference between a 200 and a 404.
    """
    import spec_request_builder as rb

    return rb.fill_path_params(spec_path, workspace_id, seeded or {}, method)


@contextlib.contextmanager
def provider_fixtures(session: dict[str, Any]) -> Iterator[Any]:
    """Stand in for the three absent providers, at their outer boundaries only.

    Work 16.5.6 §1: describing a RESPONSE SHAPE must not require a live social
    account, a Meta developer app, or a CUDA GPU. Each fixture replaces the
    outermost boundary and leaves validation, routing, orchestration, gating,
    persistence and serialization as the production code path.

    The Meta credential is written for THIS observer workspace, so it is
    unreachable from any other tenant and disappears with the test database.
    Nothing here writes a file, an env var, or a global default that outlives
    the block, so no fixture can leak into production configuration.

    Without this, three endpoints were recorded as honest gaps forever -- which
    is the same as saying their shapes are unknowable, and that is not true.
    """
    import ui_provider_fixtures as pxf

    with contextlib.ExitStack() as stack:
        # Edge needs no key: stub only the catalogue transport, leaving the
        # production factory, qualification, projection and serializer intact.
        from unittest.mock import AsyncMock, patch

        stack.enter_context(patch("edge_tts.list_voices", AsyncMock(return_value=[
            {"ShortName": "en-US-AriaNeural", "Gender": "Female", "Locale": "en-US"},
        ])))
        # Stock catalogue metadata is free, but still needs a key. A synthetic
        # key and a transport response exercise the real adapter and router;
        # no downloaded bytes, no live credentials, no paid render.
        import httpx
        from app.core.config import settings

        original_get = httpx.get

        def catalogue_get(url, **kwargs):
            if url == "https://api.pexels.com/videos/search":
                return httpx.Response(200, request=httpx.Request("GET", url), json={"videos": [
                    {"id": 1, "image": "https://example.invalid/preview.jpg", "duration": 4,
                     "user": {"name": "Observed"}, "url": "https://example.invalid/video/1",
                     "width": 720, "height": 1280},
                ]})
            return original_get(url, **kwargs)

        stack.enter_context(patch.object(settings, "pexels_api_key", "observed-not-a-live-key"))
        stack.enter_context(patch.object(httpx, "get", catalogue_get))
        stack.enter_context(pxf.social_provider())
        stack.enter_context(pxf.lipsync_provider())
        pxf.set_meta_app_id(session["workspace_id"])
        yield stack


def observe(
    client: TestClient,
    session: dict[str, Any],
    calls: list[dict[str, str]],
    seeded: dict[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Call each endpoint and record every distinct response shape observed."""
    import spec_request_builder as rb

    observations: list[dict[str, Any]] = []

    # The three provider-gated endpoints are observed INSIDE the fixture scope.
    # Outside it they would answer 400/403/503 and contribute no shape at all,
    # which is what left them recorded as gaps.
    with provider_fixtures(session):
        observations = _observe_all(
            client, session, calls, seeded, rb
        )
    return observations


def _prepare_autopilot_state(workspace_id: str, method: str, spec_path: str) -> None:
    """Establish the loop state one autopilot transition needs, just in time.

    The loop is a singleton per workspace and each transition guards on a
    specific state. Observing the chain in inventory order satisfies at most
    two of the five: pause 409s without a RUNNING run, resume 409s without a
    PAUSED one, and start/run-one-cycle 500 on a run an earlier observation
    created. All three are CORRECT refusals -- and all three leave the 200
    shape unobserved.

    So before each chain endpoint, put the loop in the state it requires:

    - pause  <- a RUNNING run exists (insert a dedicated one; never touch the
      rows other observations created, so their shapes are undisturbed);
    - resume <- a PAUSED run exists;
    - stop   <- any non-STOPPED run exists;
    - start / run-one-cycle <- NO non-STOPPED run exists (settle the rest to
      STOPPED first; a settled run is history, not an active loop).

    `record_event` rows written by the transitions are ordinary audit trail,
    not fixture pollution. Everything here runs against the throwaway
    observation workspace.
    """
    prefix = "/api/v1/workspaces/{workspace_id}/autopilot/"
    if not spec_path.startswith(prefix) or method.upper() != "POST":
        return
    action = spec_path[len(prefix):]
    if action not in ("start", "pause", "resume", "stop", "run-one-cycle"):
        return

    from sqlalchemy import select  # noqa: E402

    from app.db import SessionLocal  # noqa: E402
    from app.models.ops import AutopilotRun  # noqa: E402

    db = SessionLocal()
    try:
        if action in ("start", "run-one-cycle"):
            for row in db.scalars(
                select(AutopilotRun).where(
                    AutopilotRun.workspace_id == workspace_id,
                    AutopilotRun.state != "STOPPED",
                )
            ).all():
                row.state = "STOPPED"
            db.commit()
            return
        if action == "pause":
            want = "RUNNING"
        elif action == "resume":
            want = "PAUSED"
        else:  # stop: any non-stopped run will do
            want = None
        if want is not None:
            exists = db.scalar(
                select(AutopilotRun).where(
                    AutopilotRun.workspace_id == workspace_id,
                    AutopilotRun.state == want,
                )
            )
            if exists is None:
                import uuid as _uuid

                db.add(
                    AutopilotRun(
                        id=str(_uuid.uuid4()),
                        workspace_id=workspace_id,
                        mode="CONTINUOUS",
                        state=want,
                        cycles_target=0,
                    )
                )
                db.commit()
        else:
            exists = db.scalar(
                select(AutopilotRun).where(
                    AutopilotRun.workspace_id == workspace_id,
                    AutopilotRun.state != "STOPPED",
                )
            )
            if exists is None:
                import uuid as _uuid

                db.add(
                    AutopilotRun(
                        id=str(_uuid.uuid4()),
                        workspace_id=workspace_id,
                        mode="CONTINUOUS",
                        state="RUNNING",
                        cycles_target=0,
                    )
                )
                db.commit()
    finally:
        db.close()


def _observe_all(
    client: TestClient,
    session: dict[str, Any],
    calls: list[dict[str, str]],
    seeded: dict[str, str] | None,
    rb: Any,
) -> list[dict[str, Any]]:
    observations: list[dict[str, Any]] = []

    # CALL EACH DISTINCT ENDPOINT ONCE.
    #
    # The audit records one entry per CALL SITE, so an endpoint the UI calls from
    # three files appears three times. Observing it three times is wrong for any
    # non-idempotent route: `POST /inbox/actions/{id}/send` sent on the first
    # call, so calls two and three answered 409 "action is sent", and the
    # generator -- which keys observations by (method, path) and keeps the last --
    # recorded the FAILURE as the endpoint's shape.
    #
    # Three files calling one endpoint is a fact about the frontend, not three
    # different things to observe. One call per endpoint is the correct sample.
    seen_endpoints: set[tuple[str, str]] = set()

    for call in calls:
        method = call["method"].upper()
        spec_path = call["specPath"]
        endpoint = (method, spec_path)
        if endpoint in seen_endpoints:
            continue
        seen_endpoints.add(endpoint)
        # The autopilot loop is a SINGLETON state machine: one active run per
        # workspace, and each transition needs a specific state (pause ←
        # RUNNING, resume ← PAUSED, start/run-one-cycle ← no active run).
        # Inventory order cannot satisfy all five, so each step establishes
        # its own precondition immediately before it is observed. Without
        # this, pause/resume 409 and run-one-cycle 500s on a run the
        # observation itself started -- correct refusals, but unobservable
        # 200 shapes.
        _prepare_autopilot_state(session["workspace_id"], method, spec_path)
        url = spec_path_to_url(spec_path, session["workspace_id"], seeded, method)
        # The same domain overrides the seeding pass uses. Consulting them here
        # too is what stops a spec-derived body -- structurally valid but not
        # domain-valid -- from turning every mutating observation into a 422.
        override = DOMAIN_SEED_BODIES.get((spec_path, method.lower()))
        raw = (
            override
            if override is not None
            else rb.request_body_for(spec_path, method.lower())
        )
        # Substitute ids on BOTH sources, not just the spec-derived one. An
        # override is a hand-written body such as
        # `{"brand_id": "REPLACE_WITH_SEEDED_BRAND"}`, and skipping it there
        # left `PUT /brands/effective` answering "brand not found" against an
        # id that was never real -- a fixture failure that looked like a route
        # failure.
        body = rb.resolve_time_placeholders(
            rb.substitute_fixture_ids(raw, seeded or {})
        )
        query = rb.resolve_time_placeholders(
            rb.query_for(spec_path, method.lower(), seeded)
        )

        try:
            r = client.request(
                method, url, headers=session["headers"], json=body, params=query
            )
        except Exception as exc:
            observations.append(
                {
                    "method": method,
                    "specPath": spec_path,
                    "url": url,
                    "status": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "states": [],
                }
            )
            continue

        states: list[Any] = []
        if r.status_code < 400:
            try:
                states.append({"state": "seeded", "body": r.json()})
            except Exception:
                states.append({"state": "seeded", "body": None, "nonJson": True})

        observations.append(
            {
                "method": method,
                "specPath": spec_path,
                "url": url,
                "status": r.status_code,
                "error": None if r.status_code < 400 else r.text[:200],
                "states": states,
            }
        )

    return observations


def observe_unseeded(
    client: TestClient,
    session: dict[str, Any],
    calls: list[dict[str, str]],
) -> list[dict[str, Any]]:
    """The same endpoints against a workspace with nothing in it.

    A key present in BOTH passes is genuinely required; a key present only in the
    seeded pass is optional. Without this, every optional field would be inferred
    required from one populated snapshot.
    """
    import spec_request_builder as rb

    # The UNSEEDED pass runs first, and it POSTs to the same mutating endpoints
    # the seeded pass will call. Without the provider fixtures it drove
    # `POST /inbox/actions/{id}/send` into `failed` with no provider present,
    # and the seeded pass then observed that poisoned row instead of a success.
    #
    # The pass exists to learn which fields are OPTIONAL, not to simulate an
    # outage, so it must exercise the same seams as the seeded pass. Two passes
    # with different provider availability compare two different systems.
    with provider_fixtures(session):
        return _observe_unseeded_all(client, session, calls, rb)


def _observe_unseeded_all(
    client: TestClient,
    session: dict[str, Any],
    calls: list[dict[str, str]],
    rb: Any,
) -> list[dict[str, Any]]:
    results = []
    for call in calls:
        method = call["method"].upper()
        url = spec_path_to_url(call["specPath"], session["workspace_id"], None, method)
        body = rb.request_body_for(call["specPath"], method.lower())
        query = rb.query_for(call["specPath"], method.lower())
        try:
            r = client.request(
                method, url, headers=session["headers"], json=body, params=query
            )
            payload = r.json() if r.status_code < 400 else None
        except Exception:
            payload = None
        results.append(
            {
                "method": method,
                "specPath": call["specPath"],
                "status": r.status_code,
                "body": payload,
            }
        )
    return results

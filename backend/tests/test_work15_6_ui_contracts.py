"""Work 15.6 §12 — the frontend must agree with the backend, by construction.

Three real mismatches were found and fixed while building the provider UI:

* the preview endpoint is ``POST /voice-preview``, not ``/voice-preview/preview``;
* its body fields are ``provider`` / ``voice``, not ``provider_id`` / ``voice_id``;
* the maturity table is at ``/provider-maturity``, a different router from the
  global ``/provider-maturity`` lookup.

Each would have been a silent 404 or a 422 at runtime while ``tsc`` stayed
green, because TypeScript cannot know a FastAPI route exists. These tests close
that gap by asserting the frontend source and the backend routes agree.

A frontend test framework is deliberately NOT added: the contract worth
protecting here is a URL and a field name, and that is checkable exactly and
cheaply from the backend side. Rendering tests would add a dependency to guard
a weaker property.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
UI = REPO / "frontend" / "src" / "components" / "ProviderStatus.tsx"
SETTINGS = REPO / "frontend" / "src" / "pages" / "Settings.tsx"

pytestmark = pytest.mark.skipif(
    not UI.is_file(), reason="frontend provider panel not present"
)


def _ui_source() -> str:
    return UI.read_text(encoding="utf-8")


def _ws_api_paths() -> set[str]:
    """Every workspace-relative path the panel calls.

    A dynamic segment (``/providers/${id}/voices``) is normalised to ``{}`` so
    it compares against the backend's ``{provider_id}`` style parameter.
    """
    raw = re.findall(r'wsApi\.(?:get|put|post)\(\s*[`"\']([^`"\'?$]*)', _ui_source())
    out = set()
    for path in raw:
        path = path.rstrip("/") or "/"
        path = re.sub(r"\$\{[^}]+\}", "{}", path)
        out.add(path)
    return out


def _router_paths() -> set[str]:
    """Paths the workspace-scoped routers expose, as ``wsApi`` spells them.

    ``wsApi.get(p)`` issues ``/workspaces/{id}{p}``, and each router declares
    ``prefix="/workspaces/{workspace_id}..."``. So the workspace segment is
    stripped from the prefix and the remaining suffix is what the UI must use.
    """
    api = REPO / "backend" / "app" / "api" / "v1"
    found: set[str] = set()
    for module, router_var in (("providers.py", "workspace_maturity_router"),
                                ("music.py", "music_router"),
                                ("preview.py", "voice_preview_router"),
                                ("intelligence_routing.py", "intelligence_router")):
        text = (api / module).read_text(encoding="utf-8")
        # The prefix belonging to the workspace-scoped router specifically: a
        # module may declare more than one router.
        match = re.search(
            rf'{router_var}\s*=\s*APIRouter\(\s*(.*?)\)\n', text, re.DOTALL)
        prefixes = re.findall(r'prefix="([^"]+)"', match.group(1)) if match else []
        routes = re.findall(rf'@{router_var}\.(?:get|put|post)\(\s*\n?\s*"([^"]*)"',
                            text)
        for prefix in prefixes:
            # Strip the "/workspaces/{workspace_id}" that wsApi re-adds.
            tail = re.sub(r"^/workspaces/\{workspace_id\}", "", prefix)
            for route in routes:
                found.add(f"{tail}{route}")
    return found


# ===========================================================================
# every path the UI calls must exist on the backend
# ===========================================================================


def test_every_workspace_path_the_ui_calls_exists_on_the_backend():
    routes = _router_paths()
    missing = sorted(p for p in _ws_api_paths() if p not in routes)
    assert not missing, (
        f"the UI calls paths the backend does not expose: {missing}. "
        f"Known routes: {sorted(routes)}")


def test_the_maturity_table_path_is_the_workspace_scoped_one():
    """There are two maturity routers; the panel must use the tenant one."""
    assert "/provider-maturity" in _ws_api_paths(), (
        "the provider panel reads the GLOBAL maturity table; it should read "
        "the workspace-scoped one so credential resolution is per tenant")


def test_the_preview_post_targets_the_router_root_not_a_subpath():
    """Regression: ``POST /voice-preview/preview`` does not exist."""
    assert "/voice-preview" in _ws_api_paths()
    assert "/voice-preview/preview" not in _ws_api_paths()


# ===========================================================================
# the request body must match the pydantic model
# ===========================================================================


def test_the_preview_body_uses_the_real_field_names():
    """Regression: the body is ``provider``/``voice``, not ``*_id`` variants."""
    source = _ui_source()
    post_block = re.search(r'wsApi\.post\(\s*"[^"]*voice-preview[^"]*"\s*,\s*\{(.*?)\}\s*\)',
                           source, re.DOTALL)
    assert post_block, "the preview POST call was not found in the panel"
    body = post_block.group(1)
    assert re.search(r"\bprovider\s*:", body), "the body must send `provider`"
    assert re.search(r"\bvoice\s*:", body), "the body must send `voice`"
    assert "provider_id" not in body, "the field is `provider`, not `provider_id`"
    assert "voice_id" not in body, "the field is `voice`, not `voice_id`"


def test_the_music_toggle_sends_the_documented_key():
    source = _ui_source()
    assert re.search(r'wsApi\.put\(\s*"[^"]*music/policy[^"]*"\s*,\s*\{\s*generate\s*:',
                     source), "the music toggle must send {generate: bool}"


# ===========================================================================
# honesty and safety in the rendering
# ===========================================================================


def test_the_panel_renders_unavailable_and_restricted_states_distinctly():
    """A provider that cannot be used must not look like a working option."""
    source = _ui_source()
    for state in ("UNAVAILABLE", "BLOCKED_COMMERCIAL_TERMS", "CONTRACT_TESTED",
                  "LIVE_VERIFIED", "UNVERIFIED"):
        assert state in source, f"the panel has no rendering for {state}"


def test_the_panel_never_renders_a_credential_value():
    """Only state words may be displayed, never a value, length or digest."""
    source = _ui_source().lower()
    for forbidden in ("api_key", "apikey", "secret_value", "token_value",
                      "credential_value", "fingerprint"):
        assert forbidden not in source, (
            f"the panel references {forbidden!r}; the API returns credential "
            "STATES only and a value must never reach the DOM")


def test_the_panel_uses_existing_primitives_not_a_new_design_system():
    source = _ui_source()
    for primitive in ("Card", "Badge", "Field", "PageHeader"):
        assert primitive in source, f"the panel should reuse the {primitive} primitive"


def test_settings_grew_a_providers_tab_rather_than_a_new_page():
    """The work order asked to extend existing areas, not add pages.

    Work 16.5.1 changed the second half of this assertion on purpose, so the
    change is recorded rather than smuggled:

    * Originally this asserted ``ProviderStatus`` was absent from the router,
      because Work 15.6 added a Providers TAB and nothing else.
    * Work 16.5.1 §1 lists ``Providers`` in the target information architecture
      as its own navigation destination. The old router (``frontend/src/App.tsx``)
      no longer exists; routing authority moved to the single registry in
      ``frontend/src/routes/registry.ts`` plus ``frontend/src/app/App.tsx``.

    What still must hold, and is asserted below: Settings keeps its Providers
    tab, and ``ProviderStatus`` is reachable through the ONE registry rather than
    being wired up as an ad-hoc extra route.
    """
    settings = SETTINGS.read_text(encoding="utf-8")
    assert "ProviderStatus" in settings
    assert re.search(r'key:\s*"providers"', settings), (
        "Settings has no Providers tab; a separate page was added instead")

    registry = (REPO / "frontend" / "src" / "routes" / "registry.ts").read_text(
        encoding="utf-8")
    assert re.search(r'path:\s*"/providers"', registry), (
        "providers is not declared in the route registry")
    # Declared exactly once -- a second, hand-written route is how a nav entry
    # ends up pointing at a 404.
    assert len(re.findall(r'path:\s*"/providers"', registry)) == 1, (
        "providers is declared more than once in the registry")
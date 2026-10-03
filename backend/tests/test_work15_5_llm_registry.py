"""LLM provider registry: laziness, secret-resolver backing, and no frozen defaults.

The registry's whole reason to exist is breadth — YMONEY could configure exactly
one LLM vendor per workspace. Its three load-bearing properties are therefore:

* **Lazy.** Every entry is a literal. Importing the module, listing providers,
  or looking one up must not touch the network, the database, or the config
  module. A registry that probes at import time breaks the worker and the CLI.
* **Secret-resolver backed.** A credential is only ever read through the
  existing ``get_credential`` resolver. No key literal, and no value from a
  request body.
* **Never freezes a default.** ``normalize_provider_override`` drops an override
  equal to the registry default. That matters more here than in the donor:
  YMONEY persists credentials to the database, so a stale default would sit in
  the workspace record forever, silently overriding every later correction.

And it must plug into the *existing* ModelRouter without changing it — the
tier/fallback/health semantics are untouched; the registry only feeds the
per-tier model mapping the router already reads.
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import os
import re
import sys

import pytest

from app.engine.intelligence import llm_registry as reg

# ---------------------------------------------------------------------------
# registry shape
# ---------------------------------------------------------------------------


def test_registry_is_non_empty_and_ids_are_unique():
    providers = reg.list_providers()
    assert len(providers) > 1
    assert len({p.provider_id for p in providers}) == len(providers)


def test_every_provider_has_a_stable_id_and_label():
    for spec in reg.list_providers():
        assert spec.provider_id
        assert spec.provider_id == spec.provider_id.lower()
        assert spec.label.strip()


def test_every_base_url_is_https_or_explicitly_local():
    """A typo'd or plaintext endpoint in the data table would be a live hazard."""
    for spec in reg.list_providers():
        if not spec.base_url:
            continue
        assert spec.base_url.startswith(("https://", "http://localhost", "http://127.0.0.1")), (
            spec.provider_id
        )


def test_no_provider_carries_a_credential_value():
    """A key literal must never appear in the data table."""
    for spec in reg.list_providers():
        for field in dataclasses.fields(spec):
            value = getattr(spec, field.name)
            if isinstance(value, str):
                assert "sk-" not in value
                assert "Bearer " not in value
                # Every string field is either a URL or human prose — never a
                # bare `name=value` assignment that could be a key pair.
                offender = re.search(r"^[A-Za-z0-9_]+=[^ ]+$", value)
                assert not offender, (spec.provider_id, field.name)


def test_credential_keys_exist_in_the_workspace_registry():
    """A typo in a key name would only surface as a KeyError on first call."""
    from app.services.provider_settings import REGISTRY

    for key in (reg.KEY_API_KEY, reg.KEY_BASE_URL, reg.KEY_MODEL):
        assert key in REGISTRY, key


def test_local_providers_do_not_require_a_key():
    ollama = reg.get_provider("ollama")
    assert ollama is not None
    assert ollama.requires_api_key is False


def test_get_provider_is_case_and_whitespace_insensitive():
    assert reg.get_provider("  OpenAI ").provider_id == "openai"
    assert reg.get_provider("DEEPSEEK").provider_id == "deepseek"


def test_unknown_provider_returns_none_and_does_not_raise():
    assert reg.get_provider("not-a-vendor") is None
    assert reg.get_provider(None) is None
    assert reg.get_provider("") is None


# ---------------------------------------------------------------------------
# laziness — the property that is easy to lose and expensive to discover
# ---------------------------------------------------------------------------


def test_module_level_import_resolves_nothing(monkeypatch):
    """Importing the registry must not resolve a credential or open a session.

    The registry is imported by request handlers, workers and the API. A
    module-level resolver call would make every one of those pay for a DB
    round-trip at import, and would make a registry-only CLI unusable offline.
    """
    import app.services.provider_settings as ps

    def _forbidden(*a, **kw):  # pragma: no cover - only runs on failure
        raise AssertionError("registry resolved a credential at import time")

    monkeypatch.setattr(ps, "get_credential", _forbidden)
    monkeypatch.delitem(sys.modules, "app.engine.intelligence.llm_registry", raising=False)
    fresh = importlib.import_module("app.engine.intelligence.llm_registry")
    # Touch the data table and every lookup helper. None may resolve anything.
    assert fresh.get_provider("openai") is not None
    assert fresh.match_provider_by_base_url("https://api.deepseek.com") is not None
    assert len(fresh.list_providers()) > 1


def test_registry_module_only_imports_the_standard_library():
    """A hard structural check: no app-level import at module scope."""
    with open(reg.__file__, encoding="utf-8") as handle:
        tree = ast.parse(handle.read())
    imported: list[str] = []
    for node in tree.body:  # module scope only — function bodies are not walked
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported, "no module-level imports found — the AST walk is broken"
    for name in imported:
        assert not name.startswith("app."), name


def test_listing_and_lookup_touch_no_network(monkeypatch):
    """Make any HTTP attempt fail loudly; a lazy registry must not try."""

    def _no_network(*a, **kw):  # pragma: no cover - only runs on failure
        raise AssertionError("registry performed a network call")

    monkeypatch.setattr("httpx.get", _no_network)
    monkeypatch.setattr("httpx.post", _no_network)
    monkeypatch.setattr("httpx.Client.request", _no_network)

    for spec in reg.list_providers():
        reg.get_provider(spec.provider_id)
        reg.match_provider_by_base_url(spec.base_url)


def test_resolution_reads_credentials_only_when_called(monkeypatch):
    """The secret resolver is touched at call time, never at import time."""
    import app.services.provider_settings as ps

    calls: list[str] = []
    real = ps.get_credential

    def _spy(key, workspace_id=None):
        calls.append(key)
        return real(key, workspace_id)

    monkeypatch.setattr(ps, "get_credential", _spy)
    reg.resolve_provider_config("openai", workspace_id="ws-lazy")
    assert calls, "resolver was never consulted"
    assert all(key.startswith("llm.") for key in calls)


# ---------------------------------------------------------------------------
# the ported rule: never persist a registry default as a user choice
# ---------------------------------------------------------------------------


OPENAI_BASE = "https://api.openai.com/v1"


def test_override_equal_to_default_is_dropped():
    assert reg.normalize_provider_override(OPENAI_BASE, OPENAI_BASE) == ""


def test_override_equal_to_default_ignores_surrounding_whitespace():
    assert reg.normalize_provider_override(f"  {OPENAI_BASE}  ", OPENAI_BASE) == ""


def test_genuine_override_is_kept():
    assert reg.normalize_provider_override(
        "https://eu.example.com/v1", OPENAI_BASE
    ) == "https://eu.example.com/v1"


def test_empty_override_is_dropped():
    assert reg.normalize_provider_override("", OPENAI_BASE) == ""
    assert reg.normalize_provider_override(None, OPENAI_BASE) == ""
    assert reg.normalize_provider_override("   ", "anything") == ""


def test_default_only_matching_ignores_surrounding_whitespace():
    assert reg.normalize_provider_override("gpt-x", "  gpt-x  ") == ""


def test_patch_persists_nothing_when_nothing_differs_from_defaults():
    openai = reg.get_provider("openai")
    patch = reg.provider_settings_patch("openai", base_url=openai.base_url,
                                        model=openai.default_model)
    assert patch == {}


def test_patch_keeps_only_the_fields_that_differ():
    """A field equal to the registry default is omitted from the write."""
    patch = reg.provider_settings_patch("openai", base_url="https://eu.example.com/v1")
    assert list(patch.values()) == ["https://eu.example.com/v1"]
    assert reg.KEY_MODEL not in patch


def test_patch_for_unknown_provider_raises():
    with pytest.raises(KeyError):
        reg.provider_settings_patch("nope")


def test_provider_with_no_default_base_url_cannot_freeze_one():
    """Azure has no universal endpoint; a blank patch is the honest result."""
    azure = reg.get_provider("azure_openai")
    assert azure.base_url == ""
    assert reg.provider_settings_patch("azure_openai", base_url="") == {}


# ---------------------------------------------------------------------------
# base-URL recognition (identity only — never rewrites what is in use)
# ---------------------------------------------------------------------------


def test_recognises_a_vendor_from_its_documented_base_url():
    spec = reg.match_provider_by_base_url("https://api.deepseek.com")
    assert spec is not None and spec.provider_id == "deepseek"


def test_recognition_tolerates_a_trailing_slash():
    spec = reg.match_provider_by_base_url("https://api.deepseek.com/")
    assert spec is not None and spec.provider_id == "deepseek"


def test_recognition_covers_aliases():
    spec = reg.match_provider_by_base_url("https://api.moonshot.cn/v1")
    assert spec is not None and spec.provider_id == "moonshot"


def test_self_hosted_url_is_not_mistaken_for_a_vendor():
    assert reg.match_provider_by_base_url("https://llm.internal.corp/v1") is None


def test_blank_base_url_is_not_recognised():
    assert reg.match_provider_by_base_url("") is None
    assert reg.match_provider_by_base_url(None) is None


# ---------------------------------------------------------------------------
# credential resolution through the existing workspace resolver
# ---------------------------------------------------------------------------


@pytest.fixture()
def ws_id(workspace_with_user):
    """A real workspace row — ``api_credentials`` has an FK to workspaces."""
    return workspace_with_user["workspace"]


@pytest.fixture(autouse=True)
def _clear_global_llm_credentials(monkeypatch):
    """Make credential resolution deterministic, independent of the machine.

    ``get_credential`` falls back db -> env -> None, so a developer machine
    with ``LLM_API_KEY`` exported makes an "unconfigured" workspace look
    configured. These tests assert on the resolution LOGIC, so the ambient
    environment must be blanked rather than merely the database row cleared.
    """
    from app.core import config as config_module
    from app.services.provider_settings import REGISTRY, set_credential

    for key in (reg.KEY_API_KEY, reg.KEY_BASE_URL, reg.KEY_MODEL):
        set_credential(key, None, workspace_id=None)
        env_attr = REGISTRY[key].get("env")
        if env_attr:
            monkeypatch.setattr(config_module.settings, env_attr, "", raising=False)
    yield
    for key in (reg.KEY_API_KEY, reg.KEY_BASE_URL, reg.KEY_MODEL):
        set_credential(key, None, workspace_id=None)


def test_resolution_reports_missing_credential_instead_of_inventing_one(ws_id):
    resolved = reg.resolve_provider_config("deepseek", workspace_id=ws_id)
    # No key is configured, so the resolver must report the gap rather
    # than fabricate a value.
    assert resolved["requires_api_key"] is True
    assert resolved["api_key"] == ""
    assert resolved["api_key_source"] == "none"
    assert resolved["available"] is False
    assert reg.KEY_API_KEY in resolved["missing"]


def test_global_credential_is_resolved_without_a_workspace_id():
    """``workspace_id=None`` is the documented global/system scope."""
    from app.services.provider_settings import set_credential

    set_credential(reg.KEY_API_KEY, "global-scope-key", workspace_id=None)
    resolved = reg.resolve_provider_config("openai", workspace_id=None)
    assert resolved["api_key"] == "global-scope-key"
    assert resolved["api_key_source"] == "db"


def test_saved_workspace_endpoint_wins_over_the_registry_default(ws_id):
    """A regional or self-hosted URL the workspace chose is never replaced."""
    from app.services.provider_settings import set_credential

    set_credential(reg.KEY_BASE_URL, "https://llm.internal.corp/v1", ws_id)
    resolved = reg.resolve_provider_config("deepseek", workspace_id=ws_id)
    assert resolved["base_url"] == "https://llm.internal.corp/v1"
    assert resolved["base_url_source"] == "db"


def test_registry_default_endpoint_is_labelled_as_a_registry_source(ws_id):
    """Provenance matters: a default must be distinguishable from a choice."""
    from app.services.provider_settings import set_credential

    set_credential(reg.KEY_API_KEY, "some-key", ws_id)
    resolved = reg.resolve_provider_config("deepseek", workspace_id=ws_id)
    assert resolved["base_url"] == reg.get_provider("deepseek").base_url
    assert resolved["base_url_source"] == "registry"
    assert resolved["available"] is True


def test_unknown_provider_resolves_to_unavailable_not_an_exception():
    resolved = reg.resolve_provider_config("totally-unknown")
    assert resolved["available"] is False
    assert "provider" in resolved["missing"]


def test_local_provider_is_available_without_a_key(ws_id):
    from app.services.provider_settings import set_credential

    set_credential(reg.KEY_MODEL, "qwen3:8b", ws_id)
    resolved = reg.resolve_provider_config("ollama", workspace_id=ws_id)
    assert resolved["requires_api_key"] is False
    assert resolved["available"] is True


def test_resolved_key_can_be_redacted_for_logging(ws_id):
    from app.engine.intelligence.sanitize import redact_secrets
    from app.services.provider_settings import set_credential

    set_credential(reg.KEY_API_KEY, "sk-" + "z" * 30, ws_id)
    resolved = reg.resolve_provider_config("openai", workspace_id=ws_id)
    redacted = redact_secrets(dict(resolved))
    assert redacted["api_key"] != resolved["api_key"]
    assert redacted["model"] == resolved["model"]  # non-secret survives


def test_one_workspaces_key_never_leaks_into_another(ws_id, db_session):
    """The resolver's tenant isolation must survive the registry's use of it.

    A registry that cached a resolved key on the spec, or resolved it without a
    workspace, would hand workspace A's credential to workspace B.
    """
    from app.models import Workspace
    from app.services.provider_settings import set_credential

    other = Workspace(name="Other WS", slug=f"ws-other-{os.urandom(4).hex()}",
                      niche="AI money")
    db_session.add(other)
    db_session.commit()
    set_credential(reg.KEY_API_KEY, "key-for-one-workspace", ws_id)
    assert other.id != ws_id
    assert reg.resolve_provider_config("openai", workspace_id=other.id)["api_key"] == ""


# ---------------------------------------------------------------------------
# plugging into the existing ModelRouter, without replacing it
# ---------------------------------------------------------------------------


def test_tier_overrides_use_the_mapping_the_router_already_reads():
    from app.engine.intelligence.router import get_intelligence_settings

    overrides = reg.tier_model_overrides("openai")
    settings = get_intelligence_settings({"intelligence": {"models": overrides}})
    assert settings["models"]["fast"] == reg.get_provider("openai").default_model
    assert settings["models"]["premium"] == reg.get_provider("openai").default_model


def test_tier_overrides_are_empty_for_a_provider_with_no_advisory_model():
    assert reg.tier_model_overrides("azure_openai") == {}
    assert reg.tier_model_overrides("nope") == {}


def test_tier_overrides_do_not_touch_router_tiering_or_health():
    """The registry is data; routing, fallback and health must be unchanged."""
    from app.engine.intelligence.router import ModelRouter, RouteRequest

    router = ModelRouter()
    decision = router.route(RouteRequest(task_type="script", complexity=0.9))
    assert decision.tier in ("PREMIUM", "HIGH_QUALITY")
    assert isinstance(decision.fallbacks, list)
    # Health slots still behave as before.
    router.report_failure("remote")
    assert router.health()["remote"] is False
    router.report_success("remote")
    assert router.health()["remote"] is True


def test_workspace_tier_override_still_beats_a_registry_advisory():
    """Registry defaults must never outrank a real workspace choice."""
    from app.engine.intelligence.router import (
        ModelRouter,
        RouteRequest,
        get_intelligence_settings,
    )

    router = ModelRouter()
    chosen = reg.get_provider("openai").default_model
    # The workspace's own value is written last, so it is what survives.
    workspace = {"intelligence": {"models": {
        **reg.tier_model_overrides("openai"),
        "premium": "my-own-model",
    }}}
    settings = get_intelligence_settings(workspace)
    assert settings["models"]["premium"] == "my-own-model"
    decision = router.route(RouteRequest(task_type="script", complexity=0.9,
                                         tier="PREMIUM",
                                         workspace_settings=workspace))
    assert decision.model == "my-own-model"
    assert chosen != "my-own-model"


def test_adding_a_provider_is_a_data_only_change():
    """Registering a vendor must not require touching router code."""
    before = set(reg.PROVIDERS)
    extra = reg.LLMProviderSpec(
        provider_id="unit_test_vendor",
        label="Unit Test Vendor",
        base_url="https://api.unit-test.invalid/v1",
        default_model="unit-test-model",
    )
    try:
        reg.PROVIDERS[extra.provider_id] = extra
        reg.PROVIDER_REGISTRY = (*reg.PROVIDER_REGISTRY, extra)
        assert reg.get_provider("unit_test_vendor") is extra
        assert reg.normalize_provider_override(
            extra.base_url, extra.base_url
        ) == ""
    finally:
        reg.PROVIDERS.pop("unit_test_vendor", None)
        reg.PROVIDER_REGISTRY = tuple(
            s for s in reg.PROVIDER_REGISTRY if s.provider_id != "unit_test_vendor"
        )
    assert set(reg.PROVIDERS) == before
    assert dataclasses.is_dataclass(extra)


def test_spec_is_frozen_so_the_data_table_cannot_be_mutated_at_runtime():
    spec = reg.get_provider("openai")
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.base_url = "https://evil.example.com/v1"  # type: ignore[misc]
    assert reg.get_provider("openai").base_url == "https://api.openai.com/v1"

"""OpenAI-compatible LLM provider registry (data table, not request logic).

YMONEY historically configured one vendor per workspace (``llm.api_key`` +
``llm.base_url``). That is still the shape — this module does not change it. What
it adds is the knowledge of *which* vendors those settings describe, so a
workspace pointed at DeepSeek, Groq or a local Ollama can be recognised, given
that vendor's advisory default model, and given a credential key name to look
up. Adding a vendor is one tuple entry here; no router code changes.

Three properties this file exists to guarantee:

1. **No network, ever.** Every entry is a literal. Nothing here resolves a
   model list, probes an endpoint, or validates a key, so importing this module
   in a worker or a CLI stays instant and works offline.
2. **Credentials come from the workspace secret resolver.** An entry names the
   credential keys to *look up*; it never carries a key value, and a value
   arriving in a request body is never read here.
3. **A registry default is never persisted as if the user chose it.** Endpoints
   and model names below are advisory and go stale. :func:`normalize_provider_override`
   is what stops a stale default from freezing into a workspace record, and
   :func:`provider_settings_patch` is the only sanctioned way to build a
   settings update from a spec.

The data is a starting point assembled from each vendor's own published
OpenAI-compatible endpoint, not a snapshot of any other project's config.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# The workspace credential keys the LLM path has always used. The registry does
# not invent new ``ApiCredential`` rows: a provider is *which* vendor the
# workspace is pointed at, not a new storage slot. These are key *names* to
# look up in the existing registry, never values.
KEY_API_KEY = "llm." + "api_key"
KEY_BASE_URL = "llm." + "base_url"
KEY_MODEL = "llm." + "model"

# These must match the keys in `app.services.provider_settings.REGISTRY`; a
# typo here would only surface as a KeyError on the first call, not at import,
# so the mapping is asserted in the registry test suite.


@dataclass(frozen=True)
class LLMProviderSpec:
    """One OpenAI-compatible vendor. Data only — no behaviour."""

    provider_id: str
    label: str
    #: Vendor's published OpenAI-compatible base. Advisory: a regional or
    #: self-hosted variant is always valid, so this never overrides a value the
    #: workspace actually saved.
    base_url: str = ""
    #: Advisory model, used when the workspace saved none. Same caveat.
    default_model: str = ""
    #: Where the user obtains the key. Documentation for the UI, never fetched.
    key_console_url: str = ""
    #: Local runtimes need no credential.
    requires_api_key: bool = True
    #: Model ids vary per deployment; a local gateway may need one to be set.
    requires_model: bool = False
    #: Vendor-side notes a user needs to choose correctly (regional endpoints,
    #: non-OpenAI wire format, licensing).
    notes: str = ""
    #: Alternate published base URLs, e.g. a global vs. a regional entry point.
    aliases: tuple[str, ...] = field(default_factory=tuple)


# Base URLs are each vendor's own documented OpenAI-compatible endpoint. Model
# ids are advisory hints only: they go stale, and `normalize_provider_override`
# keeps a stale one from being frozen into a workspace record.
PROVIDER_REGISTRY: tuple[LLMProviderSpec, ...] = (
    LLMProviderSpec(
        provider_id="openai",
        label="OpenAI",
        base_url="https://api.openai.com/v1",
        default_model="gpt-4o-mini",
        key_console_url="https://platform.openai.com/api-keys",
    ),
    LLMProviderSpec(
        provider_id="anthropic",
        label="Anthropic (OpenAI-compatible)",
        base_url="https://api.anthropic.com/v1",
        key_console_url="https://console.anthropic.com/settings/keys",
        notes="OpenAI-compatible endpoint; the native wire format differs.",
    ),
    LLMProviderSpec(
        provider_id="gemini",
        label="Google Gemini (OpenAI-compatible)",
        base_url="https://generativelanguage.googleapis.com/v1beta/openai",
        key_console_url="https://aistudio.google.com/app/apikey",
    ),
    LLMProviderSpec(
        provider_id="deepseek",
        label="DeepSeek",
        base_url="https://api.deepseek.com",
        key_console_url="https://platform.deepseek.com/api_keys",
        notes="Reasoning models (R1 series) return a <think> scratchpad.",
    ),
    LLMProviderSpec(
        provider_id="moonshot",
        label="Moonshot / Kimi",
        base_url="https://api.moonshot.ai/v1",
        aliases=("https://api.moonshot.cn/v1",),
        key_console_url="https://platform.moonshot.ai/",
        notes="Two published regional endpoints; neither is a default the "
        "other overrides.",
    ),
    LLMProviderSpec(
        provider_id="groq",
        label="Groq",
        base_url="https://api.groq.com/openai/v1",
        key_console_url="https://console.groq.com/keys",
    ),
    LLMProviderSpec(
        provider_id="mistral",
        label="Mistral AI",
        base_url="https://api.mistral.ai/v1",
        key_console_url="https://console.mistral.ai/",
    ),
    LLMProviderSpec(
        provider_id="xai",
        label="xAI Grok",
        base_url="https://api.x.ai/v1",
        key_console_url="https://console.x.ai/",
    ),
    LLMProviderSpec(
        provider_id="openrouter",
        label="OpenRouter",
        base_url="https://openrouter.ai/api/v1",
        key_console_url="https://openrouter.ai/settings/keys",
        notes="Aggregator: one key, many vendors. Frequently rejects "
        "response_format, which the provider layer already retries without.",
    ),
    LLMProviderSpec(
        provider_id="together",
        label="Together AI",
        base_url="https://api.together.xyz/v1",
        key_console_url="https://api.together.ai/settings/api-keys",
    ),
    LLMProviderSpec(
        provider_id="fireworks",
        label="Fireworks AI",
        base_url="https://api.fireworks.ai/inference/v1",
        key_console_url="https://fireworks.ai/api-keys",
    ),
    LLMProviderSpec(
        provider_id="siliconflow",
        label="SiliconFlow",
        base_url="https://api.siliconflow.cn/v1",
        key_console_url="https://cloud.siliconflow.cn/account/ak",
    ),
    LLMProviderSpec(
        provider_id="dashscope",
        label="Alibaba DashScope (Qwen)",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        aliases=("https://dashscope-intl.aliyuncs.com/compatible-mode/v1",),
        key_console_url="https://bailian.console.aliyun.com/",
        notes="Chinese and international endpoints differ.",
    ),
    LLMProviderSpec(
        provider_id="azure_openai",
        label="Microsoft Azure OpenAI",
        base_url="",
        key_console_url="https://portal.azure.com/",
        notes="Deployment-scoped URL and an api_version; the workspace must "
        "supply both, so no default base URL is offered.",
    ),
    LLMProviderSpec(
        provider_id="ollama",
        label="Ollama (local)",
        base_url="http://localhost:11434/v1",
        requires_api_key=False,
        requires_model=True,
        notes="Runs on the machine. Never leaves the host.",
    ),
    LLMProviderSpec(
        provider_id="lm_studio",
        label="LM Studio (local)",
        base_url="http://localhost:1234/v1",
        requires_api_key=False,
        notes="Runs on the machine. Never leaves the host.",
    ),
    LLMProviderSpec(
        provider_id="openai_compatible",
        label="OpenAI-compatible gateway (OneAPI / LiteLLM / vLLM)",
        base_url="",
        notes="Self-hosted. The workspace must supply base_url, and usually a "
        "key too, though many gateways accept any non-empty value.",
    ),
)

PROVIDERS: dict[str, LLMProviderSpec] = {spec.provider_id: spec for spec in PROVIDER_REGISTRY}

if len(PROVIDERS) != len(PROVIDER_REGISTRY):
    raise RuntimeError("duplicate provider_id in llm registry")


def list_providers() -> tuple[LLMProviderSpec, ...]:
    """Every known provider. Pure data access — no network, no resolution."""
    return PROVIDER_REGISTRY


def get_provider(provider_id: str | None) -> LLMProviderSpec | None:
    """Look a provider up by id. Unknown ids return ``None``, never raise."""
    return PROVIDERS.get((provider_id or "").strip().lower())


def match_provider_by_base_url(base_url: str | None) -> LLMProviderSpec | None:
    """Identify the vendor a workspace is pointed at, from its base URL.

    Returns ``None`` for anything unrecognised, which is the common case: a
    self-hosted gateway is a perfectly valid configuration and is not a vendor
    we ship an entry for. Recognition never changes the URL in use — it only
    tells a caller which entry's advisory defaults may apply.
    """
    normalized = (base_url or "").strip().rstrip("/").lower()
    if not normalized:
        return None
    for spec in PROVIDER_REGISTRY:
        for candidate in (spec.base_url, *spec.aliases):
            if candidate and candidate.rstrip("/").lower() == normalized:
                return spec
    return None


def normalize_provider_override(value: str | None, default_value: str | None) -> str:
    """Keep a user override only when it differs from the registry default.

    # Ported from MoneyPrinterTurbo 1.3.7
    # Copyright (c) 2024 Harry — MIT License
    # https://github.com/harry0703/MoneyPrinterTurbo

    A UI must show the default in an input box, and that must not become the
    saved value: a registry default is a live, rot-prone suggestion, and
    persisting it freezes one point-in-time answer into a record that outlives
    it. That matters more here than in the donor, because a YMONEY workspace
    credential lives in the database — a stale default would sit there
    indefinitely, silently overriding every later registry correction.

    Returns ``""`` for "nothing chosen", which callers already read as "fall
    back to the default".
    """
    normalized_value = (value or "").strip()
    normalized_default = (default_value or "").strip()
    if normalized_value == normalized_default:
        return ""
    return normalized_value


def resolve_provider_config(
    provider_id: str | None,
    *,
    workspace_id: str | None = None,
) -> dict:
    """Resolve one provider's endpoint and credential for a workspace.

    Precedence, strongest first:

    1. what the workspace actually saved (``llm.base_url`` / ``llm.api_key`` /
       ``llm.model``) — a regional endpoint or a self-hosted URL always wins;
    2. the registry's advisory default, for whatever is missing.

    Credentials are read through the existing workspace secret resolver
    (:func:`app.services.provider_settings.get_credential`), which returns
    ``(value, source)`` with source ``db`` / ``env`` / ``none``. No key is
    hard-coded, and no value is ever taken from a request body.

    Returns a plain dict so it can be logged through
    :func:`~app.engine.intelligence.sanitize.redact_secrets` unchanged. An
    unknown ``provider_id`` yields ``available=False`` rather than raising, so
    a caller probing an id cannot turn a typo into a failed request.
    """
    spec = get_provider(provider_id)
    # Imported inside the function so importing this registry (a pure data
    # module) never opens a database session or reads config.
    from app.services.provider_settings import get_credential

    api_key, api_key_source = get_credential(KEY_API_KEY, workspace_id)
    saved_base_url, base_url_source = get_credential(KEY_BASE_URL, workspace_id)
    saved_model, model_source = get_credential(KEY_MODEL, workspace_id)

    saved_base_url = (saved_base_url or "").strip()
    saved_model = (saved_model or "").strip()
    default_base_url = spec.base_url if spec else ""
    default_model = spec.default_model if spec else ""

    base_url = saved_base_url or default_base_url
    model = saved_model or default_model

    missing: list[str] = []
    if spec is None:
        missing.append("provider")
    if spec is not None and spec.requires_api_key and not api_key:
        missing.append(KEY_API_KEY)
    if spec is not None and spec.requires_model and not model:
        missing.append(KEY_MODEL)
    if not base_url:
        missing.append(KEY_BASE_URL)

    return {
        "provider_id": spec.provider_id if spec else (provider_id or "").strip().lower(),
        "label": spec.label if spec else "",
        "available": not missing,
        "missing": missing,
        "base_url": base_url,
        "model": model,
        "api_key": api_key or "",
        # Whether the endpoint is the saved one or the advisory default. A
        # default must never be written back as though a user chose it.
        "base_url_source": base_url_source if saved_base_url else "registry",
        "model_source": model_source if saved_model else "registry",
        # Provenance for every field, so a caller can tell a saved value from a
        # registry default without re-deriving it. Sources are exactly
        # ``db`` / ``env`` / ``none`` from the resolver, plus ``registry``.
        "api_key_source": api_key_source,
        "requires_api_key": spec.requires_api_key if spec else True,
    }


def provider_settings_patch(
    provider_id: str | None,
    *,
    base_url: str | None = None,
    model: str | None = None,
) -> dict[str, str]:
    """Build the credential write for a provider selection — overrides only.

    Anything equal to the registry default is dropped, so selecting a provider
    stores nothing that will later contradict the registry. Persist the result
    with ``set_credential`` from ``app.services.provider_settings``; this
    function never writes anything.
    """
    spec = get_provider(provider_id)
    if spec is None:
        raise KeyError(f"unknown LLM provider {provider_id!r}")
    patch: dict[str, str] = {}
    chosen_base = normalize_provider_override(base_url, spec.base_url)
    if chosen_base:
        patch[KEY_BASE_URL] = chosen_base
    chosen_model = normalize_provider_override(model, spec.default_model)
    if chosen_model:
        patch[KEY_MODEL] = chosen_model
    return patch


def advisory_model_for_tier(provider_id: str | None) -> str:
    """The model a registry provider suggests, for tier resolution.

    Returns ``""`` when the registry has nothing to say, so the existing
    ``ModelRouter._resolve_model`` precedence (workspace override -> workspace
    tier setting -> env) is untouched: this only ever supplies a fallback of
    last resort and never overrides a real choice.
    """
    spec = get_provider(provider_id)
    return spec.default_model if spec else ""


def tier_model_overrides(provider_id: str | None) -> dict[str, str]:
    """A ``settings["intelligence"]["models"]``-shaped mapping for a provider.

    This is the plug-in point, and it needs no router change. ``ModelRouter``
    already reads per-tier model names out of that mapping first; handing it
    this dict means a selected vendor supplies tier defaults through the
    existing channel, while an explicit per-tier override in the workspace
    still wins because it lives at the same precedence level and the router
    reads the workspace value.

    Returns ``{}`` for a provider with no advisory model, so a provider with
    nothing to say cannot blank out the router's real resolution.
    """
    model = advisory_model_for_tier(provider_id)
    if not model:
        return {}
    return {tier.lower(): model for tier in ("FAST", "BALANCED", "HIGH_QUALITY", "PREMIUM")}

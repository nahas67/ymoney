"""LLM provider: OpenAI-compatible chat completions with deterministic mock mode.

When MOCK_LLM=true or no API key is configured, callers must supply a
`mock_fn` producing the expected structured result; this keeps development
fully offline and clearly non-production.
"""

from __future__ import annotations

import httpx
from loguru import logger

from app.core.config import settings
from app.services import cost


class LLMError(Exception):
    pass


class LLMResult:
    def __init__(self, text: str, model: str, prompt_tokens: int = 0, completion_tokens: int = 0):
        self.text = text
        self.model = model
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens

    @property
    def cost_usd(self) -> float:
        return cost.estimate_llm_cost(self.model, self.prompt_tokens, self.completion_tokens)


def llm_available() -> bool:
    eff = _effective()
    if eff["mock"]:
        return False
    return bool(eff["api_key"])


def _effective() -> dict:
    from app.services.provider_settings import effective_llm

    return effective_llm()


def complete(
    system: str,
    user: str,
    *,
    workspace_id: str = "",
    model: str | None = None,
    tier: str | None = None,  # "cheap" | "reasoning" | "verification"
    temperature: float = 0.8,
    max_tokens: int = 1500,
    json_mode: bool = False,
    mock_fn=None,
) -> LLMResult:
    """Synchronous completion. Raises LLMError when unavailable and no mock.

    `tier` selects a routed model from provider settings when no explicit
    model is given (cheap/reasoning/verification), falling back to default.
    """
    if not llm_available():
        if mock_fn is not None:
            return LLMResult(text=mock_fn(), model="mock")
        raise LLMError(
            "LLM provider not configured — add an API key under Settings → Connections "
            "or set OPENAI_API_KEY and MOCK_LLM=false"
        )

    eff = _effective()
    chosen_model = model
    if not chosen_model and tier:
        chosen_model = (eff.get("tiers") or {}).get(tier) or eff["model"] or settings.llm_model
    if not chosen_model:
        chosen_model = eff["model"] or settings.llm_model
    base_url = (eff["base_url"] or settings.openai_base_url).rstrip("/")
    api_key = eff["api_key"]
    body = {
        "model": chosen_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
    }
    if json_mode:
        body["response_format"] = {"type": "json_object"}

    fallback = settings.llm_fallback_model
    models_to_try = [chosen_model] + ([fallback] if fallback and fallback != chosen_model else [])

    last_exc: Exception | None = None
    for m in models_to_try:
        body["model"] = m
        # Some OpenAI-compatible providers reject response_format; retry without it.
        attempts = [body, {k: v for k, v in body.items() if k != "response_format"}] if json_mode else [body]
        for attempt_body in attempts:
            try:
                resp = httpx.post(
                    f"{base_url}/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}"},
                    json=attempt_body,
                    timeout=120,
                )
                resp.raise_for_status()
                data = resp.json()
                usage = data.get("usage", {})
                result = LLMResult(
                    text=data["choices"][0]["message"]["content"] or "",
                    model=m,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                )
                if result.cost_usd > 0:
                    cost.track_cost(
                        workspace_id,
                        "llm",
                        result.cost_usd,
                        provider=m,
                        detail={"prompt_tokens": result.prompt_tokens, "completion_tokens": result.completion_tokens},
                    )
                return result
            except Exception as exc:  # noqa: BLE001 - try degraded body, then fallback model
                last_exc = exc
                logger.warning(f"LLM call failed for model {m}: {exc}")
    raise LLMError(f"all LLM models failed: {last_exc}")


def complete_json(system: str, user: str, *, workspace_id: str = "", mock_fn=None, **kwargs) -> dict:
    """Completion constrained to JSON with tolerant parsing.

    Free models (OpenRouter) often prepend chatty meta-text before the JSON
    ("We need to produce a JSON with...\n{...}"). _extract_json already scans
    for the outermost braces; on failure we retry once with a firmer
    instruction before giving up.
    """
    res = complete(system, user, json_mode=True, workspace_id=workspace_id, mock_fn=mock_fn, **kwargs)
    parsed = _extract_json(res.text)
    if parsed is None:
        retry = complete(
            system + "\nCRITICAL: Your entire reply must be ONLY a valid JSON object. "
            "No preamble, no explanation, no markdown fences — start with { and end with }.",
            user,
            json_mode=False,
            workspace_id=workspace_id,
            temperature=0.3,
            max_tokens=kwargs.get("max_tokens", 800),
        )
        parsed = _extract_json(retry.text)
    if parsed is None:
        raise LLMError(f"model returned invalid JSON: {res.text[:200]}")
    return parsed


def _extract_json(text):
    import json

    if isinstance(text, dict):
        return text
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except json.JSONDecodeError:
                return None
    return None

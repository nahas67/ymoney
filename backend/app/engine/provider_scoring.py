"""Scored provider selection (OpenMontage-adapted, YMONEY-shaped).

Static capability profiles x task context x configuration state -> ranked
ProviderScores with per-dimension score/weight/reason plus a human
explanation. Pure and deterministic: no network calls, no secret access —
callers pass a config snapshot (or omit it for the live-workspace default).

Advisory only: nothing here switches providers. An explicit per-request
override always wins; the `continuity` dimension merely lets a healthy
incumbent win ties, the same gap-gate spirit as OpenMontage's
preferred_provider. Weights mirror their reasoned split
(.30/.20/.15/.15/.10/.05/.05).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from loguru import logger

WEIGHTS: dict[str, float] = {
    "task_fit": 0.30,
    "output_quality": 0.20,
    "control": 0.15,
    "reliability": 0.15,
    "cost_efficiency": 0.10,
    "latency": 0.05,
    "continuity": 0.05,
}

_DIM_ORDER = tuple(WEIGHTS)


@dataclass
class ProviderScore:
    provider: str
    capability: str  # "tts" | "images"
    dims: dict = field(default_factory=dict)  # name -> {score, weight, reason}
    weighted: float = 0.0
    explanation: str = ""

    def to_dict(self) -> dict:
        return {
            "provider": self.provider,
            "capability": self.capability,
            "dims": self.dims,
            "weighted": round(self.weighted, 3),
            "explanation": self.explanation,
        }


def _dim(score: float, reason: str, weight: float) -> dict:
    return {"score": round(max(0.0, min(1.0, score)), 3), "weight": weight, "reason": reason}


def _explain(provider: str, dims: dict) -> str:
    top = sorted(
        ((name, d["score"], d["weight"]) for name, d in dims.items()),
        key=lambda t: t[1] * t[2],
        reverse=True,
    )
    lines = [f"{provider}"]
    for name, val, weight in top[:3]:
        lines.append(f"  {name}={val:.2f} (w={weight}) — {dims[name]['reason']}")
    return "\n".join(lines)


def _finalize(provider: str, capability: str, dims: dict) -> ProviderScore:
    weighted = sum(d["score"] * d["weight"] for d in dims.values())
    return ProviderScore(provider=provider, capability=capability, dims=dims,
                         weighted=round(weighted, 3), explanation=_explain(provider, dims))


# ---------------------------------------------------------------------------
# Static capability profiles (declared capability, not live state)
# ---------------------------------------------------------------------------

_TTS_PROFILES: dict[str, dict] = {
    "edge": {"quality": 0.80, "control": 0.70, "latency": 0.70,
             "cloning": False, "library": False, "keyless": True, "paid": False, "network": True},
    "kokoro": {"quality": 0.75, "control": 0.50, "latency": 0.90,
               "cloning": False, "library": False, "needs_server": True, "paid": False, "network": False},
    "chatterbox": {"quality": 0.85, "control": 0.90, "latency": 0.70,
                   "cloning": True, "library": False, "needs_runtime": True,
                   "paid": False, "network": False, "emotion": True},
    "qwen3": {"quality": 0.80, "control": 0.80, "latency": 0.70,
              "cloning": True, "library": False, "needs_server": True, "paid": False, "network": False},
    "elevenlabs": {"quality": 0.95, "control": 0.90, "latency": 0.70,
                   "cloning": True, "library": True, "needs_key": True, "paid": True, "network": True},
    "mock": {"quality": 0.15, "control": 0.10, "latency": 1.00,
             "cloning": False, "library": False, "keyless": True, "paid": False, "network": False},
}

_IMAGE_PROFILES: dict[str, dict] = {
    "pexels": {"quality": 0.85, "control": 0.60, "latency": 0.80,
               "stock": True, "needs_key": True, "paid": False, "network": True, "reliability": 0.90},
    "xkiro": {"quality": 0.80, "control": 0.80, "latency": 0.60,
              "stock": False, "needs_key": True, "paid": True, "network": True, "reliability": 0.80},
    "pollinations": {"quality": 0.65, "control": 0.70, "latency": 0.50,
                     "stock": False, "keyless": True, "paid": False, "network": True, "reliability": 0.60},
    "openai_compat": {"quality": 0.80, "control": 0.80, "latency": 0.70,
                      "stock": False, "needs_server": True, "paid": True, "network": True, "reliability": 0.85},
    "mock": {"quality": 0.10, "control": 0.10, "latency": 1.00,
             "stock": False, "keyless": True, "paid": False, "network": False, "reliability": 1.00},
}


def tts_config_snapshot() -> dict:
    """Live configuration state for TTS scoring (workspace-scoped credentials)."""
    from app.core.config import settings

    def _has(key: str, env_attr: str = "") -> bool:
        try:
            from app.services.provider_settings import get_credential

            val, _src = get_credential(key)
            if val:
                return True
        except Exception as exc:  # probe must never break scoring
            logger.debug(f"credential probe failed: {type(exc).__name__}")
        return bool(getattr(settings, env_attr, "") if env_attr else False)

    try:
        from app.providers.tts import ChatterboxTTSProvider

        chatterbox_ready = bool(_has("tts.chatterbox_base_url", "chatterbox_base_url")
                                or ChatterboxTTSProvider._native_available())
    except Exception:
        chatterbox_ready = False
    return {
        "elevenlabs_key": _has("tts.elevenlabs_api_key", "elevenlabs_api_key"),
        "kokoro_url": _has("tts.kokoro_base_url", "kokoro_base_url"),
        "chatterbox_ready": chatterbox_ready,
        "qwen_url": _has("tts.qwen_base_url", "qwen_base_url"),
        "production": settings.is_production,
    }


def images_config_snapshot() -> dict:
    """Live configuration state for image-provider scoring."""
    from app.core.config import settings

    def _has(key: str, env_attr: str = "") -> bool:
        try:
            from app.services.provider_settings import get_credential

            val, _src = get_credential(key)
            if val:
                return True
        except Exception as exc:  # probe must never break scoring
            logger.debug(f"credential probe failed: {type(exc).__name__}")
        return bool(getattr(settings, env_attr, "") if env_attr else False)

    return {
        "pexels_key": _has("pexels.api_key", "pexels_api_key"),
        "xkiro_key": _has("image.openai_api_key", "") or _has("llm.api_key", ""),
        "openai_url": _has("image.openai_base_url", ""),
        "production": settings.is_production,
    }


def _reliability(configured: bool, base: float, missing_reason: str) -> tuple[float, str]:
    if not configured:
        return 0.0, missing_reason
    return base, "configured"


def score_tts(task: dict | None = None, config: dict | None = None,
              current: str = "") -> list[ProviderScore]:
    """Rank TTS providers for a task. Task keys: needs_cloning, voice_library,
    offline_only, budget_sensitive. `current` names the incumbent (continuity)."""
    task = task or {}
    cfg = config if config is not None else tts_config_snapshot()
    production = bool(cfg.get("production"))
    out: list[ProviderScore] = []
    for name, p in _TTS_PROFILES.items():
        # task fit
        fit, fit_reasons = 0.60, ["general narration"]
        if task.get("needs_cloning") or task.get("voice_library"):
            if p.get("cloning") or p.get("library"):
                fit, fit_reasons = 1.00, ["voice cloning / library support"]
            else:
                fit, fit_reasons = 0.15, ["no cloning support"]
        if task.get("offline_only") and p.get("network"):
            fit, fit_reasons = min(fit, 0.20), fit_reasons + ["needs public network"]
        # reliability from configuration state
        if name == "mock":
            rel, rel_reason = (0.0, "simulation-only (production)") if production else (1.0, "always available")
        elif p.get("needs_key") and not cfg.get("elevenlabs_key"):
            rel, rel_reason = _reliability(False, 0.0, "API key not configured")
        elif p.get("needs_server") and not (cfg.get("kokoro_url") if name == "kokoro" else cfg.get("qwen_url")):
            rel, rel_reason = _reliability(False, 0.0, "server URL not configured")
        elif p.get("needs_runtime") and not cfg.get("chatterbox_ready"):
            rel, rel_reason = _reliability(False, 0.0, "no native package or server URL")
        else:
            rel, rel_reason = (0.90, "ready") if p.get("network") else (0.85, "local ready")
        # cost
        if p.get("paid") and task.get("budget_sensitive"):
            cost, cost_reason = 0.25, "paid per character"
        elif p.get("paid"):
            cost, cost_reason = 0.40, "paid per character"
        else:
            cost, cost_reason = 1.00, "free"
        dims = {
            "task_fit": _dim(fit, "; ".join(fit_reasons), WEIGHTS["task_fit"]),
            "output_quality": _dim(p["quality"], "declared provider fidelity", WEIGHTS["output_quality"]),
            "control": _dim(p["control"], "voice range / direction support", WEIGHTS["control"]),
            "reliability": _dim(rel, rel_reason, WEIGHTS["reliability"]),
            "cost_efficiency": _dim(cost, cost_reason, WEIGHTS["cost_efficiency"]),
            "latency": _dim(p["latency"], "typical turnaround class", WEIGHTS["latency"]),
            "continuity": _dim(1.00 if current == name else 0.50,
                               "incumbent" if current == name else "switch cost",
                               WEIGHTS["continuity"]),
        }
        out.append(_finalize(name, "tts", dims))
    out.sort(key=lambda s: -s.weighted)
    return out


def score_images(task: dict | None = None, config: dict | None = None,
                 current: str = "") -> list[ProviderScore]:
    """Rank image providers. Task keys: needs_stock_photo, offline_only,
    budget_sensitive."""
    task = task or {}
    cfg = config if config is not None else images_config_snapshot()
    production = bool(cfg.get("production"))
    out: list[ProviderScore] = []
    for name, p in _IMAGE_PROFILES.items():
        if name == "mock":
            fit, fit_reasons = 0.20, ["placeholder only"]
        elif task.get("needs_stock_photo"):
            fit, fit_reasons = (1.00, ["real stock photography"]) if p.get("stock") else (0.40, ["AI-generated, not stock"])
        else:
            fit, fit_reasons = 0.70, ["general generation"]
        if task.get("offline_only"):
            fit, fit_reasons = min(fit, 0.10), fit_reasons + ["all image providers need network"]
        if name == "mock":
            rel, rel_reason = (0.0, "simulation-only (production)") if production else (1.0, "always available")
        elif p.get("needs_key") and not (cfg.get("pexels_key") if name == "pexels" else cfg.get("xkiro_key")):
            rel, rel_reason = _reliability(False, 0.0, "API key not configured")
        elif p.get("needs_server") and not cfg.get("openai_url"):
            rel, rel_reason = _reliability(False, 0.0, "server URL not configured")
        else:
            rel, rel_reason = p.get("reliability", 0.80), "ready"
        if p.get("paid") and task.get("budget_sensitive"):
            cost, cost_reason = 0.30, "paid per image"
        elif p.get("paid"):
            cost, cost_reason = 0.50, "paid per image"
        else:
            cost, cost_reason = 1.00, "free"
        dims = {
            "task_fit": _dim(fit, "; ".join(fit_reasons), WEIGHTS["task_fit"]),
            "output_quality": _dim(p["quality"], "declared provider fidelity", WEIGHTS["output_quality"]),
            "control": _dim(p["control"], "prompt / art-direction support", WEIGHTS["control"]),
            "reliability": _dim(rel, rel_reason, WEIGHTS["reliability"]),
            "cost_efficiency": _dim(cost, cost_reason, WEIGHTS["cost_efficiency"]),
            "latency": _dim(p["latency"], "typical turnaround class", WEIGHTS["latency"]),
            "continuity": _dim(1.00 if current == name else 0.50,
                               "incumbent" if current == name else "switch cost",
                               WEIGHTS["continuity"]),
        }
        out.append(_finalize(name, "images", dims))
    out.sort(key=lambda s: -s.weighted)
    return out

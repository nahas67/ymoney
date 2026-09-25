"""Runtime provider configuration API (Settings → Connections).

Credentials are stored encrypted server-side and only ever returned masked.
"""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.db import get_db
from app.services import provider_settings as ps
from app.services.auth_service import require_workspace_role

connections_router = APIRouter(prefix="/workspaces/{workspace_id}/connections", tags=["connections"])

_MANAGEABLE = {
    "llm.api_key": ps.REGISTRY["llm.api_key"],
    "llm.base_url": ps.REGISTRY["llm.base_url"],
    "llm.model": ps.REGISTRY["llm.model"],
    "google.client_id": ps.REGISTRY["google.client_id"],
    "google.client_secret": ps.REGISTRY["google.client_secret"],
    "tiktok.client_key": ps.REGISTRY["tiktok.client_key"],
    "tiktok.client_secret": ps.REGISTRY["tiktok.client_secret"],
    "meta.app_id": ps.REGISTRY["meta.app_id"],
    "meta.app_secret": ps.REGISTRY["meta.app_secret"],
    "upload_post.api_key": ps.REGISTRY["upload_post.api_key"],
    "upload_post.username": ps.REGISTRY["upload_post.username"],
    "youtube.api_key": ps.REGISTRY["youtube.api_key"],
    "newsdata.api_key": ps.REGISTRY["newsdata.api_key"],
    "coingecko.api_key": ps.REGISTRY["coingecko.api_key"],
    "pexels.api_key": ps.REGISTRY["pexels.api_key"],
    "tts.provider": ps.REGISTRY["tts.provider"],
    "tts.kokoro_base_url": ps.REGISTRY["tts.kokoro_base_url"],
    "tts.kokoro_api_key": ps.REGISTRY["tts.kokoro_api_key"],
    "tts.chatterbox_base_url": ps.REGISTRY["tts.chatterbox_base_url"],
    "tts.qwen_base_url": ps.REGISTRY["tts.qwen_base_url"],
    "tts.qwen_instruct": ps.REGISTRY["tts.qwen_instruct"],
    "tts.qwen_api_key": ps.REGISTRY["tts.qwen_api_key"],
    "tts.elevenlabs_api_key": ps.REGISTRY["tts.elevenlabs_api_key"],
    "image.openai_base_url": ps.REGISTRY["image.openai_base_url"],
    "image.openai_api_key": ps.REGISTRY["image.openai_api_key"],
    "image.openai_model": ps.REGISTRY["image.openai_model"],
    "avatar.backend": ps.REGISTRY["avatar.backend"],
    "avatar.base_url": ps.REGISTRY["avatar.base_url"],
    "avatar.sadtalker_dir": ps.REGISTRY["avatar.sadtalker_dir"],
    "avatar.wavlip_dir": ps.REGISTRY["avatar.wavlip_dir"],
    "telegram.bot_token": ps.REGISTRY["telegram.bot_token"],
}


class CredentialBody(BaseModel):
    key: str
    value: str | None = None  # None/"" clears


@connections_router.get("")
def list_connections(ws=Depends(require_workspace_role("admin"))):
    items = []
    for key, spec in _MANAGEABLE.items():
        value, source = ps.get_credential(key, workspace_id=ws.id)
        items.append(
            {
                "key": key,
                "label": spec["label"],
                "secret": spec["secret"],
                "hint": spec.get("hint", ""),
                "configured": bool(value),
                "source": source,
                "masked": ps.mask(value) if (spec["secret"] and value) else value,
            }
        )
    return {"items": items}


@connections_router.put("")
def set_connection(body: CredentialBody, ws=Depends(require_workspace_role("admin"))):
    if body.key not in _MANAGEABLE:
        raise HTTPException(status_code=404, detail=f"unknown credential '{body.key}'")
    ps.set_credential(body.key, body.value or None, workspace_id=ws.id)
    value, source = ps.get_credential(body.key, workspace_id=ws.id)
    return {
        "key": body.key,
        "configured": bool(value),
        "source": source,
        "masked": ps.mask(value) if (_MANAGEABLE[body.key]["secret"] and value) else value,
    }


@connections_router.post("/test-publishing")
def test_publishing(ws=Depends(require_workspace_role("admin")), db=Depends(get_db)):
    """Live validation of the real publishing paths.

    Checks: (1) Upload-Post relay credentials — validated against the provider's
    /api/uploadposts/me endpoint; (2) connected platform accounts with tokens.
    Never raises: every failure is reported as structured status.
    """
    from sqlalchemy import func, select

    from app.models import SocialAccount

    key, username = ps.upload_post_config(workspace_id=ws.id)
    accounts = (
        db.execute(
            select(SocialAccount.platform, func.count())
            .where(SocialAccount.workspace_id == ws.id, SocialAccount.status == "connected")
            .group_by(SocialAccount.platform)
        ).all()
    )
    connected = {p: c for p, c in accounts}

    result: dict = {
        "relay_configured": bool(key and username),
        "relay_valid": None,
        "relay_plan": None,
        "relay_email": None,
        "connected_accounts": connected,
        "ok": bool(connected),
        "detail": "",
    }
    if key and not username:
        result["detail"] = "API key set but profile username missing — add upload_post.username"
    elif key and username:
        try:
            resp = httpx.get(
                "https://api.upload-post.com/api/uploadposts/me",
                headers={"Authorization": f"Apikey {key}"},
                timeout=15,
            )
            if resp.status_code == 200:
                data = resp.json()
                result["relay_valid"] = True
                result["relay_plan"] = data.get("plan")
                result["relay_email"] = data.get("email")
            else:
                result["relay_valid"] = False
                result["detail"] = f"relay returned {resp.status_code}: {resp.text[:120]}"
        except Exception as exc:
            result["relay_valid"] = False
            result["detail"] = f"relay unreachable: {type(exc).__name__}: {exc}"
    elif not key:
        result["detail"] = (
            "No real publishing path: no connected accounts and no Upload-Post key. "
            "Add the key under Settings → Connections & Keys."
        )
    if result["relay_valid"] is True:
        result["ok"] = True
    return result


@connections_router.post("/test-llm")
def test_llm(ws=Depends(require_workspace_role("admin"))):
    """Live reachability + auth check of the effective LLM configuration."""
    eff = ps.effective_llm(workspace_id=ws.id)
    if eff["mock"]:
        return {"ok": True, "mode": "mock", "detail": "MOCK_LLM=true — using offline mock"}
    if not eff["api_key"]:
        return {"ok": False, "mode": "real", "detail": "No API key configured"}
    try:
        resp = httpx.get(
            f"{(eff['base_url'] or 'https://api.openai.com/v1').rstrip('/')}/models",
            headers={"Authorization": f"Bearer {eff['api_key']}"},
            timeout=10,
        )
        if resp.status_code == 200:
            models = [m.get("id") for m in resp.json().get("data", [])][:50]
            return {
                "ok": True,
                "mode": "real",
                "detail": f"connected ({len(models)} models visible)",
                "model": eff["model"],
                "model_available": eff["model"] in models if models else None,
            }
        return {
            "ok": False,
            "mode": "real",
            "status": resp.status_code,
            "detail": f"provider returned {resp.status_code}: {resp.text[:160]}",
        }
    except httpx.HTTPError as exc:
        return {"ok": False, "mode": "real", "detail": f"unreachable: {exc}"}


# ---------------------------------------------------------------------------
# Video engine (Settings → Video Engine)
# ---------------------------------------------------------------------------


@connections_router.get("/video-engine")
def video_engine_status(ws=Depends(require_workspace_role("admin"))):
    """Effective engine config, live health, version, capabilities."""
    from app.providers.video_engine.factory import (
        engine_effective_config,
        get_video_engine,
    )

    with ps.workspace_scope(ws.id):
        cfg = engine_effective_config()
        try:
            engine = get_video_engine()
            healthy = engine.health()
            version = engine.version() if hasattr(engine, "version") else None
            capabilities = sorted(engine.get_capabilities()) if healthy else []
        except Exception:
            engine, healthy, version, capabilities = None, False, None, []
    return {
        "engine": cfg["engine"],
        "base_url": cfg["base_url"],
        "timeout_seconds": cfg["timeout"],
        "sources": cfg["sources"],
        "healthy": healthy,
        "version": version,
        "capabilities": capabilities,
        "defaults": {
            "voice": "en-US-AndrewNeural",
            "subtitles": True,
            "aspect_ratio": "9:16",
        },
        "concurrency_note": "max_concurrent_renders lives in Safety settings",
    }


class EngineBody(BaseModel):
    base_url: str | None = Field(default=None, max_length=300)
    timeout_seconds: int | None = Field(default=None, ge=60, le=7200)


@connections_router.put("/video-engine")
def update_video_engine(body: EngineBody, ws=Depends(require_workspace_role("admin"))):
    from app.providers.video_engine.factory import reset_video_engine

    if body.base_url is not None and not body.base_url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="base_url must start with http(s)://")
    changed = False
    if body.base_url is not None:
        ps.set_credential("engine.base_url", body.base_url or None, workspace_id=ws.id)
        changed = True
    if body.timeout_seconds is not None:
        ps.set_credential("engine.timeout_seconds", str(body.timeout_seconds), workspace_id=ws.id)
        changed = True
    if changed:
        reset_video_engine(ws.id)
    return video_engine_status(ws)


# ---------------------------------------------------------------------------
# TTS (narration) — status, voices, live test
# ---------------------------------------------------------------------------


@connections_router.get("/tts")
def tts_status(ws=Depends(require_workspace_role("admin")), language: str = ""):
    """Effective TTS provider, health, and available voices."""
    from app.providers.tts import TTSError, get_tts_provider, tts_provider_status

    with ps.workspace_scope(ws.id):
        status = tts_provider_status()
        voices: list[dict] = []
        error = status.get("error")
        try:
            provider = get_tts_provider()
            voices = provider.voices(language=language)[:200]
        except TTSError as exc:
            error = str(exc)
        except Exception as exc:  # voice listing must never 500 the page
            error = f"voice list failed: {type(exc).__name__}"
        try:
            from app.engine.provider_scoring import score_tts

            ranked = [s.to_dict() for s in score_tts()]
        except Exception:
            ranked = []
    return {
        **status,
        "voices": voices,
        "error": error,
        "ranked": ranked,
    }


class TTSTestBody(BaseModel):
    text: str = Field(default="", max_length=600)
    voice: str = Field(default="", max_length=120)
    rate: float = Field(default=1.0, ge=0.5, le=2.0)


@connections_router.post("/tts/test")
def tts_test(body: TTSTestBody, ws=Depends(require_workspace_role("admin"))):
    """Synthesize a short narration sample; returns raw audio bytes."""
    from fastapi.responses import Response

    from app.providers.tts import TTSError, get_tts_provider

    text = body.text.strip() or (
        "YMONEY narration test. This is how your videos will sound."
    )
    try:
        with ps.workspace_scope(ws.id):
            provider = get_tts_provider()
            result = provider.synthesize(text, voice=body.voice, rate=body.rate)
    except TTSError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    media = "audio/wav" if result.format == "wav" else "audio/mpeg"
    return Response(
        content=result.audio_bytes,
        media_type=media,
        headers={
            "X-TTS-Provider": result.provider,
            "X-TTS-Mock": "1" if result.is_mock else "0",
        },
    )


# ---------------------------------------------------------------------------
# Image generation — status + live test
# ---------------------------------------------------------------------------


@connections_router.get("/images")
def images_status(ws=Depends(require_workspace_role("admin"))):
    from app.providers.images import image_provider_status

    with ps.workspace_scope(ws.id):
        status = image_provider_status()
        try:
            from app.engine.provider_scoring import score_images

            ranked = [s.to_dict() for s in score_images()]
        except Exception:
            ranked = []
        return {**status, "ranked": ranked}


class ImageTestBody(BaseModel):
    prompt: str = Field(default="", max_length=400)


@connections_router.post("/images/test")
def images_test(body: ImageTestBody, ws=Depends(require_workspace_role("admin"))):
    """Generate one small test image; returns raw image bytes."""
    from fastapi.responses import Response

    from app.providers.images import ImageProviderError, get_image_provider

    prompt = body.prompt.strip() or (
        "a clean minimal flat illustration of a rising chart, soft colors"
    )
    try:
        with ps.workspace_scope(ws.id):
            provider = get_image_provider()
            blobs = provider.generate(prompt, size="512x288", n=1)
    except ImageProviderError as exc:
        raise HTTPException(status_code=502, detail=str(exc))
    if not blobs:
        raise HTTPException(status_code=502, detail="image provider returned no candidates")
    return Response(
        content=blobs[0],
        media_type="image/png" if blobs[0][:4] == b"\x89PNG" else "image/jpeg",
        headers={
            "X-Image-Provider": provider.name,
            "X-Image-Mock": "1" if provider.is_mock else "0",
        },
    )

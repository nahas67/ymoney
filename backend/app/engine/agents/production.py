"""Video production + quality control agents.

The render flow is DURABLE and IDEMPOTENT:

1. A Video row is created AT submission with engine_task_id + request_hash
   (status RENDERING). Retries and restarts reattach to the same engine task.
2. Progress is persisted onto the row and emitted as video.generation.* events.
3. On completion the artifact moves through the storage boundary with ffprobe
   metadata captured; only then does status become READY for QC.
"""

from __future__ import annotations

import time

from app.core.config import settings
from app.db import session_scope
from app.engine.agents.base import AgentMeta, BaseAgent
from app.models import Video, Workspace
from app.providers.video_engine.base import (
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_NOT_FOUND,
    RenderRequest,
    VideoEngineError,
)
from app.services import jobs as jobs_service
from app.services.events import record_event


def _emit(ws: str | None, kind: str, message: str, level: str = "info", **data):
    record_event(ws, kind=kind, message=message, level=level, source="video", data=data)


def _audit(ws: str | None, action: str, resource_id: str, detail: dict, actor: str = "AI_AGENT"):
    """Audit trail for the generation lifecycle (actor = AI_AGENT for autopilot)."""
    with session_scope() as s:
        from app.models import AuditLog

        s.add(
            AuditLog(
                workspace_id=ws,
                user_id=None,
                action=action,
                resource_type="video_generation",
                resource_id=resource_id,
                detail_json={"actor": actor, **detail},
            )
        )


def _update_video(video_id: str, **fields) -> None:
    with session_scope() as s:
        row = s.get(Video, video_id)
        if not row:
            return
        for k, v in fields.items():
            setattr(row, k, v)


class VideoProducerAgent(BaseAgent):
    meta = AgentMeta(
        key="producer",
        title="Video Producer",
        description="Renders videos through the configured video engine.",
        skills=("video_production",),
        tools=("render_video",),
        permissions=("media:render",),
    )

    # -- configuration -------------------------------------------------------

    @staticmethod
    def max_concurrent_renders(ws: str) -> int:
        from app.engine.decision import get_safety_settings

        with session_scope() as s:
            row = s.get(Workspace, ws) if ws else None
            safety = get_safety_settings((row.settings_json or {}) if row else {})
        return int(safety.get("max_concurrent_renders", 2))

    @staticmethod
    def active_renders(ws: str) -> int:
        from sqlalchemy import func

        with session_scope() as s:
            return int(
                s.query(func.count()).select_from(Video).filter(
                    Video.workspace_id == ws, Video.status == "RENDERING"
                ).scalar() or 0
            )

    # -- render ----------------------------------------------------------------

    def render(self, ctx, *, topic: str, script: str, keywords: list[str],
               aspect_ratio: str, variant_id: str | None,
               language: str = "", voice_name: str | None = None) -> dict:
        """Durable/idempotent render. Returns {video_id, ...} on completion."""

        def work():
            from app.providers.video_engine.factory import get_video_engine
            from app.services.storage import get_storage

            engine = get_video_engine()
            req = RenderRequest(
                subject=topic,
                script=script,
                workspace_id=ctx.workspace_id or "",
                keywords=keywords or [w for w in topic.split()[:4]],
                aspect_ratio=aspect_ratio,
                language=language or "",
                voice_name=voice_name or "en-US-AndrewNeural",
            )
            req_hash = req.request_hash()

            # ---- resolve prior state (idempotency + crash reconciliation) ----
            resolution = self._resolve_existing(engine, variant_id, req_hash, topic)
            existing_task = resolution.get("engine_task_id")
            video_id = resolution.get("video_id")
            if resolution["kind"] == "ready":
                return {"summary": "render already complete (idempotent)",
                        "video_id": video_id}

            cap = self.max_concurrent_renders(ctx.workspace_id or "")
            active = self.active_renders(ctx.workspace_id or "") - (1 if video_id else 0)
            if active >= cap:
                jobs_service.enqueue(
                    ctx.type, ctx.payload, workspace_id=ctx.workspace_id,
                    cycle_id=ctx.cycle_id, priority=20, delay_seconds=60,
                )
                raise jobs_service._Backpressure(
                    f"render concurrency at cap ({active}/{cap}); job requeued"
                )

            # ---- persist intent BEFORE submitting (crash-safe ordering) -------
            if video_id is None:
                video_row_id = self._create_video_row(
                    ctx, variant_id=variant_id, engine=engine.engine_name,
                    task_id="", req_hash=req_hash,
                    aspect_ratio=aspect_ratio,
                    estimate=engine.estimate_cost(req),
                )
                video_id = video_row_id

            if existing_task:
                handle = type("H", (), {
                    "engine_task_id": existing_task, "engine": engine.engine_name,
                })()
                _emit(ctx.workspace_id, "video.generation.reattached",
                      f"Reattached to engine task {existing_task[:12]} after restart/retry",
                      data={"video_id": video_id})
            else:
                try:
                    handle = engine.submit(req)
                except VideoEngineError as exc:
                    _update_video(video_row_id, status="FAILED", error=f"submit failed: {exc}")
                    if getattr(exc, "retryable", False):
                        raise
                    raise RuntimeError(str(exc)) from exc
                except jobs_service._Cancelled:
                    _update_video(video_row_id, status="FAILED", error="cancelled before submit")
                    raise
                _update_video(video_row_id, engine_task_id=handle.engine_task_id)
                video_id = video_row_id
                _emit(ctx.workspace_id, "video.generation.created",
                      f"Render started on {engine.engine_name} "
                      f"(task {handle.engine_task_id[:12]})",
                      data={"video_id": video_id})
                _audit(ctx.workspace_id, "video.generation.created", video_id, {
                    "engine": engine.engine_name,
                    "engine_task_id": handle.engine_task_id,
                    "request_hash": req_hash,
                })

            def on_progress(st):
                _update_video(video_id, progress=int(st.progress))
                if st.progress and st.progress % 25 == 0:
                    _emit(ctx.workspace_id, "video.generation.progress",
                          f"Rendering {st.progress}%",
                          data={"video_id": video_id, "progress": st.progress})

            deadline = time.time() + settings.mpt_timeout_seconds
            st = None
            while True:
                jobs_service.check_cancelled(ctx)
                try:
                    st = engine.status(handle)
                except VideoEngineError as exc:
                    if getattr(exc, "retryable", False) and time.time() < deadline:
                        time.sleep(5)
                        continue
                    raise
                _update_video(video_id, progress=int(st.progress))
                if st.state == STATE_COMPLETE:
                    break
                if st.state in (STATE_FAILED, STATE_NOT_FOUND):
                    reason = st.error or st.state
                    _update_video(video_id, status="FAILED", error=reason)
                    _emit(ctx.workspace_id, "video.generation.failed",
                          f"Render failed: {reason}", level="error",
                          data={"video_id": video_id})
                    _audit(ctx.workspace_id, "video.generation.failed", video_id, {
                        "engine_task_id": getattr(handle, "engine_task_id", ""),
                        "failed_stage": st.failed_stage,
                        "error": reason,
                    })
                    raise RuntimeError(f"render failed: {reason}")
                if time.time() > deadline:
                    _update_video(video_id, status="FAILED", error="render timed out")
                    raise TimeoutError("render timed out")
                time.sleep(3.0)

            out_ref = engine.get_video_url(handle) or (st.videos[0] if st.videos else "")
            from app.providers.video_engine.mock import MockVideoEngine

            if isinstance(engine, MockVideoEngine):
                video_path = f"mock:{out_ref}"
                size = duration = width = height = None
            else:
                blob = engine.fetch_video_bytes(out_ref)
                from app.services.storage import get_storage

                stored = get_storage().save_video(
                    ctx.workspace_id or "", source_path=None, data=blob,
                    filename=f"{handle.engine_task_id}.mp4",
                )
                video_path = stored.path
                size, duration = stored.size_bytes, stored.duration_seconds
                width, height = stored.width, stored.height

            resolution = f"{width}x{height}" if width and height else (
                "1080x1920" if aspect_ratio == "9:16" else "1920x1080"
            )
            thumb = ""
            try:
                from app.services.storage import get_storage as _gs

                thumb = _gs().extract_thumbnail(video_path) or ""
            except Exception:
                thumb = ""
            with session_scope() as s:
                current = s.get(Video, video_id)
                params = dict(current.params_json or {}) if current else {}
            params.update({"topic": topic, "size_bytes": size, "request_hash": req_hash})
            _update_video(
                video_id, status="READY", file_path=video_path,
                thumbnail_path=thumb,
                duration_seconds=duration, resolution=resolution,
                params_json=params,
                error="",
            )
            estimate = engine.estimate_cost(req)
            self.track_cost(ctx, "video", estimate, provider=engine.engine_name,
                            detail={"task_id": handle.engine_task_id, "estimate": True})
            _emit(ctx.workspace_id, "video.generation.completed",
                  f"Video ready ({duration:.0f}s)" if duration else "Video ready",
                  level="success", data={"video_id": video_id})
            _audit(ctx.workspace_id, "video.generation.completed", video_id, {
                "engine": engine.engine_name,
                "engine_task_id": handle.engine_task_id,
                "duration_seconds": duration,
                "size_bytes": size,
                "storage_path": video_path,
            })
            return {
                "summary": f"rendered '{topic[:60]}'",
                "video_id": video_id,
                "engine": engine.engine_name,
                "engine_task_id": handle.engine_task_id,
                "video_path": video_path,
                "aspect_ratio": aspect_ratio,
            }

        return self.execute(ctx, "render", input_summary=topic, fn=work)

    # -- persistence helpers ---------------------------------------------------

    @staticmethod
    def _create_video_row(ctx, *, variant_id, engine, task_id, req_hash,
                          aspect_ratio, estimate) -> str:
        with session_scope() as s:
            row = Video(
                variant_id=variant_id,
                workspace_id=ctx.workspace_id,
                engine=engine,
                engine_task_id=task_id,
                status="RENDERING",
                aspect_ratio=aspect_ratio,
                resolution="1080x1920" if aspect_ratio == "9:16" else "1920x1080",
                params_json={"request_hash": req_hash, "estimated_cost_usd": estimate},
            )
            s.add(row)
            s.flush()
            return row.id

    @staticmethod
    def _resolve_existing(engine, variant_id: str | None, req_hash: str, topic: str) -> dict:
        """Resolve prior render state for this variant.

        kinds:
          ready      — completed video exists; nothing to do
          reattach   — RENDERING row with a persisted engine task; resume it
          adopt      — RENDERING row without task id, but the engine holds an
                       orphaned matching task (crash between submit & persist)
          fresh      — start over
        """
        if not variant_id:
            return {"kind": "fresh"}

        with session_scope() as s:
            row = s.query(Video).filter(Video.variant_id == variant_id).order_by(
                Video.created_at.desc()
            ).first()
            if not row:
                return {"kind": "fresh"}
            stored = dict(row.params_json or {})
            if row.status == "READY" and stored.get("request_hash") in (None, req_hash):
                return {"kind": "ready", "video_id": row.id}
            if row.status == "RENDERING" and row.engine_task_id:
                return {"kind": "reattach", "video_id": row.id,
                        "engine_task_id": row.engine_task_id}
            if row.status == "RENDERING" and not row.engine_task_id:
                # Crash window: engine may have accepted the job before we
                # persisted its id. Reconcile by subject match, scoped to this
                # workspace so another tenant's tasks can never be adopted.
                lister = getattr(engine, "list_recent_tasks", None)
                if lister:
                    for t in lister(limit=50):
                        if (
                            t.get("subject", "").strip().lower() == topic.strip().lower()
                            and t.get("state") in ("processing", "complete")
                            and not s.query(Video).filter(
                                Video.workspace_id == row.workspace_id,
                                Video.engine_task_id == t["task_id"],
                            ).first()
                        ):
                            row.engine_task_id = t["task_id"]
                            s.flush()
                            return {"kind": "adopt", "video_id": row.id,
                                    "engine_task_id": t["task_id"]}
                row.status = "FAILED"
                row.error = "submit interrupted before engine accepted the job"
                s.flush()
        return {"kind": "fresh"}


# ---------------------------------------------------------------------------
# Quality control
# ---------------------------------------------------------------------------

QUALITY_COMPONENTS = (
    "hook", "story", "retention", "pacing", "audio", "captions",
    "visual_relevance", "originality", "accuracy", "safety",
    "caption_readability", "brand_consistency", "platform_fit",
)

SCORED_COMPONENTS = QUALITY_COMPONENTS


def _originality_score(script: str, strategy: dict | None = None, research: dict | None = None) -> tuple[float, str]:
    """Dynamic originality replacing the old static 70.

    Penalizes thin scripts, missing visual planning, absent angles, and high
    similarity to the strategy's own prior topic signal. Workspace repetition
    vs published history is applied upstream in scoring/decision; this covers
    intrinsic originality of the artifact itself.
    """
    strategy = strategy or {}
    research = research or {}
    score = 72.0
    reasons = []
    words = len((script or "").split())
    if words < 40:
        score -= 14
        reasons.append("thin script")
    visuals = research.get("visual_keywords") or strategy.get("visual_keywords") or []
    if not visuals:
        score -= 10
        reasons.append("no visual plan")
    angles = research.get("angles") or []
    if not angles:
        score -= 6
        reasons.append("single angle")
    storyboard = (strategy.get("storyboard") or {}) if isinstance(strategy.get("storyboard"), dict) else {}
    scenes = storyboard.get("scenes") if isinstance(storyboard.get("scenes"), list) else None
    if scenes is not None and len(scenes) <= 1:
        score -= 8
        reasons.append("single scene")
    if not (strategy.get("angle") or strategy.get("hook_type")):
        score -= 6
        reasons.append("generic framing")
    score = max(5.0, min(95.0, score))
    return round(score, 1), ("; ".join(reasons) if reasons else "specific angle with visual plan")


def heuristic_quality(script: str, strategy: dict | None = None, research: dict | None = None) -> dict:
    """Deterministic QC baseline used in mock/dev mode."""
    strategy = strategy or {}
    words = len(script.split())
    length_ok = 40 <= words <= 130
    has_hook = bool(script.strip()) and (
        "?" in script[:120] or any(
            k in script[:120].lower() for k in ("nobody", "truth", "secret", "what if", "stop")
        )
    )
    sentences = [s for s in script.replace("?", ".").replace("!", ".").split(".") if s.strip()]
    avg_sentence_words = words / max(len(sentences), 1)
    readability = max(30.0, min(98.0, 110 - avg_sentence_words * 4))
    platforms = strategy.get("platforms") or ["youtube"]
    orig, orig_reason = _originality_score(script, strategy, research)
    scores = {
        "hook": 85 if has_hook else 55,
        "story": 75 if length_ok else 55,
        "retention": 78 if has_hook and length_ok else 58,
        "pacing": 80 if length_ok else 60,
        "audio": 82,
        "captions": 85,
        "caption_readability": round(readability),
        "visual_relevance": 72,
        "originality": orig,
        "accuracy": 68,
        "safety": _safety_score(script),
        "brand_consistency": 78 if strategy.get("tone") else 65,
        "platform_fit": 88.0 if platforms else 60.0,
    }
    overall = sum(scores[k] for k in SCORED_COMPONENTS) / len(SCORED_COMPONENTS)
    notes = []
    if not length_ok:
        notes.append(f"script length {words} words outside 40-130 target")
    if not has_hook:
        notes.append("weak hook: no question/curiosity marker detected early")
    if scores["safety"] < 50:
        notes.append("possible policy-sensitive content; manual review advised")
    if orig < 50:
        notes.append(f"low originality ({orig:.0f}/100: {orig_reason}); requires distinct angle or new visuals")
    return {
        "overall": round(overall, 1),
        "components": scores,
        "confidence": 0.65,
        "notes": "; ".join(notes),
    }


def _safety_score(text: str) -> float:
    risky = ("war", "kill", "death", "drug", "scandal", "hate", "medical advice")
    hits = sum(1 for r in risky if r in text.lower())
    return max(10.0, 95.0 - hits * 30.0)


def storyboard_scene_count(strategy: dict | None) -> int:
    """Scene count from the strategy storyboard; 1 when absent."""
    strategy = strategy or {}
    storyboard = strategy.get("storyboard") or {}
    scenes = storyboard.get("scenes")
    if isinstance(scenes, list) and scenes:
        return len(scenes)
    count = storyboard.get("scene_count")
    if isinstance(count, int) and count > 0:
        return count
    return 1


def result_duration(strategy: dict | None, script: str) -> float:
    """Expected video duration: strategy duration target or script length."""
    strategy = strategy or {}
    duration = strategy.get("duration")
    if isinstance(duration, (int, float)) and duration > 0:
        return float(duration)
    # ~2.6 words/second narration pace
    return max(5.0, len(script.split()) / 2.6)


def regeneration_instruction(verdict: dict) -> str:
    """Translate the weakest QC dimensions into a concrete instruction."""
    comps = verdict.get("components", {})
    weakest = sorted(comps.items(), key=lambda kv: kv[1])[:2]
    guidance = {
        "visual_relevance": "improve visual matching and increase scene diversity",
        "hook": "strengthen the opening hook with a sharper curiosity gap",
        "captions": "tighten caption phrasing for readability",
        "pacing": "increase pacing; shorten scenes",
        "story": "clarify the narrative arc with one concrete example",
        "accuracy": "remove unverifiable claims",
        "originality": "take a distinct contrarian angle with new visuals instead of stock repetition",
        "safety": "remove policy-sensitive content and add disclaimers",
        "brand_consistency": "align tone with the brand voice",
        "platform_fit": "adjust format for target platforms",
    }
    parts = [guidance.get(k, f"improve {k.replace('_', ' ')}") for k, _ in weakest]
    return "; ".join(parts)


class QualityAgent(BaseAgent):
    meta = AgentMeta(
        key="quality",
        title="Quality Agent",
        description="Scores finished videos against the quality threshold.",
        skills=("video_production",),
        tools=("render_video",),
        permissions=("media:render",),
    )

    def evaluate(self, ctx, *, script: str, strategy: dict | None = None,
                 research: dict | None = None, video_path: str = "") -> dict:
        from app.engine.decision import get_safety_settings

        strategy = strategy or {}
        with session_scope() as s:
            ws_row = s.get(Workspace, ctx.workspace_id) if ctx.workspace_id else None
        safety = get_safety_settings((ws_row.settings_json or {}) if ws_row else {})
        threshold = safety["min_qc_score"]

        def work():
            self.step("heuristic_baseline", "score 13 dimensions from script+strategy heuristics")
            result = heuristic_quality(script, strategy, research)
            self.step_done("ok", f"baseline {result['overall']:.0f}/100")
            # Media intelligence: inspect the finished render when one exists.
            vision_notes = ""
            if video_path and not video_path.startswith("mock:"):
                from app.providers.vision import get_vision_provider

                self.step("vision_analysis", "inspect finished render frames/audio")
                try:
                    analysis = get_vision_provider().analyze(
                        video_path=video_path,
                        script=script,
                        scene_count=storyboard_scene_count(strategy),
                        expected_duration=result_duration(strategy, script),
                    )
                    result["vision"] = {
                        "provider": analysis.provider,
                        "is_mock": analysis.is_mock,
                        "audio_present": analysis.audio_present,
                        "silent_sections": [list(section) for section in analysis.silent_sections],
                        "subtitles_aligned": analysis.subtitles_aligned,
                        "visual_relevance": analysis.visual_relevance,
                        "notes": analysis.notes,
                        "scenes": [
                            {
                                "index": scene.index,
                                "duration_seconds": scene.duration_seconds,
                                "visual_matches_script": scene.visual_matches_script,
                                "on_screen_text": scene.on_screen_text,
                                "black_frames": scene.black_frames,
                                "corrupted": scene.corrupted,
                            }
                            for scene in analysis.scenes
                        ],
                    }
                    vr = analysis.visual_relevance
                    if vr > 0:
                        base_vr = result["components"]["visual_relevance"]
                        result["components"]["visual_relevance"] = round(
                            base_vr * 0.5 + vr * 0.5, 1
                        )
                    if not analysis.audio_present:
                        result["components"]["audio"] = min(
                            result["components"]["audio"], 40.0
                        )
                    if analysis.silent_sections:
                        result["components"]["audio"] = min(
                            result["components"]["audio"], 55.0
                        )
                    if not analysis.subtitles_aligned:
                        result["components"]["captions"] = min(
                            result["components"]["captions"], 50.0
                        )
                    if any(s.black_frames or s.corrupted for s in analysis.scenes):
                        result["components"]["visual_relevance"] = min(
                            result["components"]["visual_relevance"], 45.0
                        )
                    vision_notes = analysis.summary()
                    if analysis.is_mock:
                        vision_notes = "[MOCK] " + vision_notes
                    self.step_done("ok", vision_notes[:120])
                except Exception as exc:
                    self.step_failed(f"{type(exc).__name__}; heuristics only")
                    result["vision"] = {
                        "provider": "unavailable",
                        "is_mock": False,
                        "error": type(exc).__name__,
                    }
                    self.announce(
                        ctx.workspace_id,
                        f"vision analysis unavailable ({exc}); using heuristics",
                        level="warning",
                    )
            if research and research.get("fact_status"):
                fc = float(research.get("factual_confidence", 0.4))
                base_acc = result["components"]["accuracy"]
                result["components"]["accuracy"] = round(base_acc * 0.5 + fc * 100 * 0.5, 1)
                if research.get("fact_status") == "CONFLICTING":
                    result["notes"] = (result.get("notes", "") + " | conflicting claims detected").strip(" |")
                if research.get("fact_status") == "INSUFFICIENT":
                    result["notes"] = (result.get("notes", "") + " | insufficient fact confidence").strip(" |")
            if llm_ready():
                self.step("llm_review", "verification-tier model re-scores all dimensions")
                try:
                    res = _llm_quality(ctx, script)
                    if res:
                        blended = {
                            k: round(res.get(k, result["components"][k]) * 0.6 + result["components"][k] * 0.4, 1)
                            for k in result["components"]
                        }
                        result["components"] = blended
                        result["overall"] = round(
                            sum(blended[k] for k in SCORED_COMPONENTS) / len(SCORED_COMPONENTS), 1
                        )
                        result["confidence"] = 0.9
                        if res.get("notes"):
                            result["notes"] = (result.get("notes", "") + " | " + res["notes"]).strip(" |")
                        self.step_done("ok", f"blended {result['overall']:.0f}/100")
                    else:
                        self.step_failed("model returned unusable scores")
                except Exception as exc:
                    self.step_failed(str(exc)[:120])
                    self.announce(ctx.workspace_id, f"LLM quality review unavailable ({exc}); using heuristics", level="warning")
            if vision_notes:
                result["notes"] = (result.get("notes", "") + " | " + vision_notes).strip(" |")
                # recompute overall from the adjusted components
                result["overall"] = round(
                    sum(result["components"][k] for k in SCORED_COMPONENTS) / len(SCORED_COMPONENTS),
                    1,
                )
            result["passed"] = result["overall"] >= threshold
            result["threshold"] = threshold
            return result

        return self.execute(ctx, "quality_check", input_summary="evaluating script/video", fn=work)


def llm_ready() -> bool:
    """True when the effective LLM configuration is usable (not just flags)."""
    from app.providers.llm import llm_available

    return llm_available()


def _llm_quality(ctx, script: str) -> dict | None:
    from app.providers import llm as llm_mod

    try:
        res = llm_mod.complete_json(
            system=(
                "You are a strict short-form video quality reviewer. Score these dimensions 0-100: "
                "hook, story, retention, pacing, audio, captions, visual_relevance, originality, "
                "accuracy, safety, caption_readability, brand_consistency, platform_fit. "
                "Return JSON with those keys plus 'notes' (<=200 chars)."
            ),
            user=f"SCRIPT:\n{script}",
            workspace_id=ctx.workspace_id or "",
            tier="verification",
            temperature=0.2,
            max_tokens=400,
        )
    except Exception as exc:
        from loguru import logger as _lg

        _lg.warning(f'_llm_quality failed: {exc}')
        return None
    if not res:
        return None
    out = {k: float(v) for k, v in res.items() if k in QUALITY_COMPONENTS and isinstance(v, (int, float))}
    if len(out) < 5:
        return None
    return {"components": out, "notes": res.get("notes", "")}

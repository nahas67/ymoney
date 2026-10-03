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

from loguru import logger

from app.core.config import settings
from app.db import session_scope
from app.engine.agents.base import AgentMeta, BaseAgent
from app.models import Video, Workspace
from app.models.base import utcnow
from app.providers.video_engine.base import (
    STATE_COMPLETE,
    STATE_FAILED,
    STATE_NOT_FOUND,
    RenderRequest,
    VideoEngineError,
    VideoEngineSubmissionUnknown,
)
from app.services import jobs as jobs_service
from app.services.cost import BudgetExceededError
from app.services.events import record_event
from app.services.paid_executor import (
    CostOutcome,
    IdempotencySupport,
    Reconciliation,
)
from app.services.paid_provider import paid_operation


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


def _remember_ambiguous_request(video_id: str, fingerprint: str) -> None:
    """Record WHICH request may have been billed, on the row itself.

    ``submission_detail`` explains what happened; it is not a key. The engine's
    task list has no request hash to match against, so without this the only way
    to reconcile an ambiguous submit after a restart is to guess. The adapter
    supplies the value because it is the only place that knows what was actually
    sent (``mpt.submission_unknown``).
    """
    if not str(fingerprint or ""):
        return
    with session_scope() as s:
        row = s.get(Video, video_id)
        if row is None:
            return
        params = dict(row.params_json or {})
        params["ambiguous_request_hash"] = fingerprint
        row.params_json = params


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
                keywords=keywords or topic.split()[:4],
                aspect_ratio=aspect_ratio,
                language=language or "",
                voice_name=voice_name or "en-US-AndrewNeural",
            )
            req_hash = req.request_hash()

            # ---- resolve prior state (idempotency + crash reconciliation) ----
            resolution = self._resolve_existing(engine, variant_id, req_hash, topic)
            existing_task = resolution.get("engine_task_id")
            video_id = resolution.get("video_id")

            # Work 15.5 §7: an AMBIGUOUS prior submission stops the pipeline.
            # The engine may already have accepted and billed this job, so
            # continuing to a submit here would buy a second one. This halts
            # before the budget gate, because even reaching that gate means
            # planning to spend.
            if resolution["kind"] == "unknown":
                _emit(ctx.workspace_id, "video.generation.submission_unknown",
                      "A previous submission of this render is in an unknown "
                      "state and may already have been billed. Refusing to "
                      "resubmit; reconcile with the engine first.",
                      level="error",
                      data={"video_id": video_id or "",
                            "request_hash": req_hash})
                raise RuntimeError(
                    "submission state is UNKNOWN for a prior attempt of this "
                    "render (the provider may have accepted and billed it). "
                    "Refusing to resubmit automatically. Reconcile the engine "
                    "task list for this request hash, or clear the video row "
                    "explicitly once the outcome is known."
                )

            # W11.5 E-F1 (CRITICAL) + Work 15.8 §6: the pre-spend gate.
            # `assert_can_spend` existed with ZERO call sites repo-wide, so the
            # daily/per-video caps were only ever *read* (decision.py, autopilot
            # QC) and never enforced. What was enforced here was the ADVISORY
            # read, which cannot exclude a concurrent spender; §6 replaced it with
            # an atomic reservation, so the gate and the proof that it ran are one
            # committed transaction. A reattach to an in-flight task is not new
            # spend and is deliberately NOT reserved.
            estimate = float(engine.estimate_cost(req) or 0.0)
            paid = paid_operation(
                provider=engine.engine_name,
                operation="video_render_submit",
                workspace_id=ctx.workspace_id or "",
                category="video",
                estimated_cost=estimate,
                # MoneyPrinterTurbo documents no idempotency header, so the local
                # key plus the SUBMISSION_UNKNOWN refusal are the only protection
                # against a second purchase.
                idempotency=IdempotencySupport.UNSUPPORTED,
                reconciliation=Reconciliation.RECONCILE,
                reservation_extra={"request_hash": req_hash,
                                   "aspect_ratio": aspect_ratio,
                                   "engine": engine.engine_name},
            )
            if not existing_task:
                try:
                    paid.authorize()
                except BudgetExceededError as exc:
                    # no Video row may exist yet at this point (the row is
                    # created below), so mark the run failed via the id we have
                    if video_id:
                        _update_video(video_id, status="FAILED", error=f"budget: {exc}")
                    _emit(ctx.workspace_id, "video.generation.budget_blocked",
                          f"Render blocked by budget: {exc}",
                          level="warning", data={"video_id": video_id or ""})
                    raise RuntimeError(f"budget exceeded: {exc}") from exc
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
                # Work 15.8 §6: the durable record. The attempt is written BEFORE
                # `engine.submit` is called, so a process that dies with the
                # request in flight still leaves proof that money may be gone.
                # Each writer below owns ONE fact: `on_attempt` is evidence that
                # the request is about to leave, `on_execution` is where the
                # request got to, `on_cost_outcome` is what the ledger may say.
                # Conflating them into one status string is what Work 15.5 §7 had
                # to undo.
                def _record_attempt(operation_id: str) -> None:
                    _update_video(
                        video_row_id,
                        submission_state="SUBMISSION_ATTEMPTED",
                        submission_operation_id=operation_id,
                        submission_attempted_at=utcnow(),
                        cost_outcome=str(CostOutcome.ESTIMATED),
                        submission_detail=(
                            f"one submit to {engine.engine_name} for "
                            f"request {req_hash[:12]}"))

                def _record_execution(state: str, detail: str = "") -> None:
                    if state == "SUBMISSION_UNKNOWN":
                        # Money may be gone and we cannot prove it was not, so
                        # this is the state that forbids an automatic resubmit --
                        # and the business status is left alone, because FAILED
                        # would invite exactly that resubmit.
                        _update_video(video_row_id,
                                      submission_state="SUBMISSION_UNKNOWN",
                                      submission_detail=detail,
                                      error=detail)
                        return
                    _update_video(video_row_id,
                                  submission_state=state,
                                  submission_detail=detail)

                paid.bind(
                    on_attempt=_record_attempt,
                    on_execution=_record_execution,
                    on_cost_outcome=lambda outcome: _update_video(
                        video_row_id, cost_outcome=outcome),
                    on_remote_id=lambda remote_id: _update_video(
                        video_row_id, provider_task_id=remote_id),
                )
                paid.mark_attempt()
                try:
                    handle = engine.submit(req)
                except VideoEngineSubmissionUnknown as exc:
                    # Work 15.6 §5: the submit was DELIVERED and no durable id
                    # came back. The engine may have accepted and billed the
                    # job, so this is NOT a failure and NOT retryable -- writing
                    # FAILED would both lose the reference and invite a re-buy.
                    # The adapter names the request it actually sent, so the
                    # record survives a restart with a usable reconciliation key
                    # instead of a bare "something went wrong".
                    fingerprint = getattr(exc, "request_fingerprint", "") or req_hash
                    paid.mark_unknown(str(exc))
                    _remember_ambiguous_request(video_row_id, fingerprint)
                    _emit(ctx.workspace_id, "video.generation.submission_unknown",
                          "Render submit was delivered but unconfirmed. The "
                          "engine may have accepted and billed it; refusing to "
                          "resubmit automatically. Reconcile the engine task "
                          "list before retrying.",
                          level="error",
                          data={"video_id": video_row_id,
                                "request_hash": req_hash,
                                "ambiguous_request_hash": fingerprint})
                    raise RuntimeError(str(exc)) from exc
                except VideoEngineError as exc:
                    # Anything that is NOT an ambiguity left this adapter means
                    # the engine provably created no task: a 4xx, a 429, or a
                    # connection that never opened. mpt.submit routes a read
                    # failure and a post-create 5xx to SubmissionUnknown
                    # explicitly, so nothing billable reaches this branch -- which
                    # is why the reservation is released and the budget returns.
                    paid.mark_rejected(str(exc), nothing_billed=True)
                    _update_video(video_row_id, status="FAILED", error=f"submit failed: {exc}")
                    if getattr(exc, "retryable", False):
                        raise
                    raise RuntimeError(str(exc)) from exc
                except jobs_service._Cancelled:
                    paid.mark_cancelled("cancelled before submit")
                    _update_video(video_row_id, status="FAILED", error="cancelled before submit")
                    raise
                paid.mark_accepted(handle.engine_task_id)
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
            # W11.5 D-F1 (HIGH): READY was written with NO verifier call, so a
            # DB status could stand in for missing evidence (a DB status saying
            # COMPLETE must not override failed evidence). Ledger the independent
            # check right after the status flip: video file, ffprobe validity,
            # QC score and MediaAsset registration. A failure does NOT rewrite
            # READY (the verifier records NOT_VERIFIED evidence, and downstream
            # QC/approval reads it) -- it makes the gap auditable instead of
            # invisible. Runs in its own session because this one may be mid-
            # transaction, and a ledger failure must never fail the render.
            #
            # FRAGILE INVARIANT (W11.5): this is safe ONLY because
            # `_update_video()` opens and closes its own `session_scope()`, so
            # no transaction is open here. If that ever changes, do NOT nest a
            # committing session here -- `append_evidence` commits internally
            # and will deadlock against an outer SQLite write lock
            # ("database is locked"). Use the ambient session instead, as
            # `publish_flow.py` does.
            if not str(video_path or "").startswith("mock:"):
                try:
                    with session_scope() as vs:
                        from app.engine.intelligence.verifier import (
                            CompletionContract,
                        )
                        from app.engine.intelligence.verifier import (
                            verify as verify_completion,
                        )

                        evidence = verify_completion(
                            vs, ctx.workspace_id or "",
                            CompletionContract(kind="video", subject_id=video_id,
                                               expectations={"duration_seconds": duration}
                                               if duration else {}),
                        )
                    _emit(ctx.workspace_id, "video.generation.verified",
                          f"Completion evidence: {evidence.verification_status}",
                          level="success" if str(evidence.verification_status) in
                          ("VERIFIED", "PARTIALLY_VERIFIED") else "warning",
                          data={"video_id": video_id,
                                "verification": str(evidence.verification_status)})
                except Exception as exc:  # noqa: BLE001 — never fail a render on evidence
                    _emit(ctx.workspace_id, "video.generation.verified",
                          f"Completion verification failed to run: {exc}",
                          level="warning", data={"video_id": video_id})
            estimate = engine.estimate_cost(req)
            if paid.reservation is not None:
                # Work 15.8 §6: the reservation taken BEFORE the submit IS this
                # operation's ledger row, and closing it in place is the booking.
                # A second track_cost would bill one render twice: the estimate
                # already counted against the daily cap. `close_book` keeps the
                # row an ESTIMATE, because MoneyPrinterTurbo reports no amount
                # and `settle_reservation` would stamp it ACTUAL.
                paid.close_book()
                ctx.artifacts["cost_usd"] = ctx.artifacts.get("cost_usd", 0.0) + estimate
            else:
                # Work 15.9 §5: a REATTACH must not buy a second time.
                #
                # There is no reservation on this path because the submit
                # happened before the crash, so its money is already on a ledger
                # row -- possibly written by a previous process that then died.
                # Booking again here created a SECOND cost_entries row for the
                # SAME remote task, which is a double-book: the render is
                # charged twice and the daily cap is consumed twice for one
                # purchase.
                #
                # ``reattach_by_remote_id`` finds the row that already owns
                # this remote job and reports it, so the accounting identity is
                # exactly-once even though the network call was not ours.
                # Nothing new is written; the existing row stays authoritative.
                recovered = None
                try:
                    from app.services.paid_provider import reattach_by_remote_id

                    recovered = reattach_by_remote_id(
                        str(handle.engine_task_id or ""))
                except Exception as exc:  # noqa: BLE001 - never fail a render
                    logger.warning(
                        "reattach accounting lookup failed for task {}: {}",
                        str(handle.engine_task_id or "")[:12], exc)
                if recovered is not None:
                    ctx.artifacts["reattached_cost_row"] = recovered.entry_id
                    ctx.artifacts["cost_usd"] = ctx.artifacts.get(
                        "cost_usd", 0.0)
                else:
                    # No row owns this remote job. That is NOT permission to
                    # invent one: the money may already be gone and unrecorded,
                    # so this becomes a visible unknown exposure rather than a
                    # second charge.
                    logger.warning(
                        "reattached to engine task {} but no cost row owns it; "
                        "recording an unknown exposure rather than a second "
                        "charge", str(handle.engine_task_id or "")[:12])
                    _emit(ctx.workspace_id, "video.generation.reattach_unowned",
                          "Reattached to an engine task with no cost row of its "
                          "own; recorded as unknown exposure, not re-charged.",
                          level="warning",
                          data={"video_id": video_id,
                                "engine_task_id": handle.engine_task_id or ""})
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
          unknown    — the engine may have accepted AND BILLED the job and we
                       cannot prove it did not. A human must decide; the
                       pipeline must NOT resubmit.
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
                # Work 15.5 §7: the no-orphan branch is AMBIGUOUS, not failed.
                # The engine may have accepted and billed this job; writing
                # FAILED both destroys the reference and invites a re-buy.
                # SUBMISSION_UNKNOWN forbids automatic resubmission and forces
                # reconciliation or an explicit human decision.
                row.submission_state = "SUBMISSION_UNKNOWN"
                row.submission_detail = (
                    "submit was interrupted before the engine task id was "
                    "persisted and no matching orphan was found; the job may "
                    "have been accepted and billed. Do not resubmit without "
                    "reconciling with the engine first."
                )
                row.error = row.submission_detail
                s.flush()
                return {"kind": "unknown", "video_id": row.id}
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

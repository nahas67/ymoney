"""Autopilot orchestrator: the durable FIND→…→LEARN loop.

Each pipeline stage is a queued job; successful stages chain into the next,
giving crash-safe recovery without fragile while-loops. Pause/stop are
checked by every stage through `_gate`. The AutopilotRun row holds desired
state so restarts converge correctly.
"""

from __future__ import annotations

from datetime import timedelta

from sqlalchemy import and_, func, or_, select, update

from app.db import session_scope
from app.engine.agents.creation import (
    HookOptimizerAgent,
    ResearchAgent,
    ScriptWriterAgent,
    StrategistAgent,
)
from app.engine.agents.discovery import TrendAnalystAgent, TrendHunterAgent, persist_opportunities
from app.engine.agents.distribution import PublisherAgent, SEOAgent
from app.engine.agents.intelligence import AnalyticsCollectorAgent, LearningAgent
from app.engine.agents.production import QualityAgent, VideoProducerAgent, regeneration_instruction
from app.models import (
    AutopilotRun,
    ContentItem,
    CostEntry,
    Cycle,
    LearningPattern,
    Opportunity,
    PublishedPost,
    PublishingJob,
    ScheduleEntry,
    Video,
    VideoVariant,
)
from app.models.base import (  # noqa: F401
    AutopilotState,
    ContentStatus,
    CycleStage,
    can_transition,
    new_uuid,
    utcnow,
)
from app.services import cost as cost_service
from app.services import jobs as jobs_service
from app.services.events import record_event

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_state(workspace_id: str) -> tuple[str | None, dict]:
    with session_scope() as s:
        run = s.scalar(
            select(AutopilotRun)
            .where(AutopilotRun.workspace_id == workspace_id)
            .where(AutopilotRun.state.not_in([AutopilotState.STOPPED.value]))
            .order_by(AutopilotRun.created_at.desc())
            .limit(1)
        )
        if not run:
            return None, {}
        cfg = dict(run.config_json or {})
        s.expunge(run)
        return run.state, cfg


def _gate(ctx) -> None:
    """Enforce autopilot desired-state before doing expensive work."""
    state, _cfg = _run_state(ctx.workspace_id) if ctx.workspace_id else (None, {})
    jobs_service.check_cancelled(ctx)
    if state is None:
        return  # standalone job execution (e.g., manual generate)
    if state == AutopilotState.STOPPING.value:
        _finalize_stop(ctx.workspace_id)
        raise jobs_service._Cancelled()
    if state == AutopilotState.PAUSED.value:
        # reschedule self shortly; resumes automatically when unpaused
        jobs_service.enqueue(
            ctx.type,
            ctx.payload,
            workspace_id=ctx.workspace_id,
            cycle_id=ctx.cycle_id,
            priority=ctx.priority if hasattr(ctx, "priority") else 100,
            delay_seconds=5.0,
        )
        raise jobs_service._Cancelled()


def _finalize_stop(workspace_id: str) -> None:
    with session_scope() as s:
        run = s.scalar(
            select(AutopilotRun)
            .where(AutopilotRun.workspace_id == workspace_id)
            .where(AutopilotRun.state == AutopilotState.STOPPING.value)
            .limit(1)
        )
        if run:
            run.state = AutopilotState.STOPPED.value
    record_event(workspace_id, kind="autopilot.stopped", message="Autopilot stopped", level="info", source="supervisor")


def _set_content_status(session, content: ContentItem, target: str) -> None:
    if not can_transition(content.status, target):
        raise ValueError(f"illegal content transition {content.status} -> {target}")
    content.status = target


def _enqueue_stage(stage: str, payload: dict, workspace_id: str, cycle_id: str, delay: float = 0.0):
    return jobs_service.enqueue(
        f"cycle.{stage.lower()}",
        payload,
        workspace_id=workspace_id,
        cycle_id=cycle_id,
        priority=10,
        delay_seconds=delay,
    )


def _cycle_payload(cycle_id: str) -> dict:
    return {"cycle_id": cycle_id}


def _get_cycle(session, cycle_id: str) -> Cycle:
    cycle = s_get(session, Cycle, cycle_id)
    if not cycle:
        raise ValueError(f"cycle not found: {cycle_id}")
    return cycle


def s_get(session, model, id_):
    return session.get(model, id_)


def recover_stale_cycles() -> int:
    """Crash-recovery policy: after a restart, pause all active autopilot
    runs and fail in-flight cycles. Users explicitly resume — this prevents
    zombie loops from previous processes re-triggering automatically."""
    affected = 0
    with session_scope() as s:
        runs = s.scalars(
            select(AutopilotRun).where(
                AutopilotRun.state.not_in([AutopilotState.STOPPED.value])
            )
        ).all()
        for run in runs:
            run.state = AutopilotState.PAUSED.value
            affected += 1
        cycles = s.scalars(select(Cycle).where(Cycle.status == "RUNNING")).all()
        for c in cycles:
            c.status = "FAILED"
            c.error = "interrupted by application restart"
            c.finished_at = utcnow()
        # cancel queued jobs belonging to interrupted workspaces so paused
        # runs don't tick — but NEVER system jobs (schedule sweep etc.) which
        # are workspace-independent and must survive restarts.
        from app.models import Job

        ws_ids = [r.workspace_id for r in runs]
        if ws_ids:
            dangling = s.scalars(
                select(Job).where(
                    Job.status.in_(["QUEUED", "RETRYING"]),
                    Job.workspace_id.in_(ws_ids),
                )
            ).all()
            for j in dangling:
                j.status = "CANCELLED"
    if affected:
        record_event(None, "system.recovery",
                     f"Recovery: {affected} autopilot run(s) paused after restart; resume to continue.",
                     level="warning", source="supervisor")
    return affected


# ---------------------------------------------------------------------------
# Stage handlers
# ---------------------------------------------------------------------------


_cycle_start_lock = __import__("threading").Lock()


@jobs_service.handler("autopilot.start_next_cycle")
def handle_start_next_cycle(ctx):
    """Entry point: create a new cycle when autopilot should run."""
    ws = ctx.workspace_id
    state, cfg = _run_state(ws)
    if state in (None,) or state in (AutopilotState.STOPPING.value):
        if state == AutopilotState.STOPPING.value:
            _finalize_stop(ws)
        return {"stopped": True}
    if state in (AutopilotState.PAUSED.value,):
        jobs_service.enqueue("autopilot.start_next_cycle", {}, workspace_id=ws, priority=5, delay_seconds=5)
        return {"paused": True}

    # Serialize cycle creation within this process; the active-cycle check
    # below then guarantees one RUNNING cycle per run.
    with _cycle_start_lock:
        with session_scope() as s:
            run = s.scalar(
                select(AutopilotRun)
                .where(AutopilotRun.workspace_id == ws)
                .where(AutopilotRun.state.not_in([AutopilotState.STOPPED.value]))
                .order_by(AutopilotRun.created_at.desc())
                .limit(1)
            )
            if not run:
                return {"no_run": True}
            if run.mode == "SINGLE_CYCLE" and run.cycles_completed >= 1:
                run.state = AutopilotState.STOPPED.value
                record_event(ws, "autopilot.completed", "Single-cycle run finished", source="supervisor")
                return {"single_done": True}
            if run.cycles_target and run.cycles_completed >= run.cycles_target:
                run.state = AutopilotState.STOPPED.value
                record_event(ws, "autopilot.completed", f"Reached {run.cycles_target} cycles — stopping", source="supervisor")
                return {"target_reached": True}
            if run.scheduled_stop_at and utcnow() >= run.scheduled_stop_at:
                run.state = AutopilotState.STOPPED.value
                record_event(ws, "autopilot.stopped", "Scheduled stop time reached", source="supervisor")
                return {"scheduled_stop": True}
            # never pile up concurrent cycles for the same run
            active_cycle = s.scalar(
                select(Cycle).where(Cycle.autopilot_run_id == run.id, Cycle.status == "RUNNING").limit(1)
            )
            if active_cycle:
                return {"cycle_in_progress": True}
            number = run.cycles_completed + 1
            cycle = Cycle(workspace_id=ws, autopilot_run_id=run.id, number=number, stage=CycleStage.FIND.value)
            s.add(cycle)
            s.flush()
            cycle_id = cycle.id
            cycle_number = number
            run.state = AutopilotState.RUNNING.value
    record_event(ws, "cycle.started", f"Cycle #{cycle_number:03d} started", source="supervisor", data={"cycle_id": cycle_id})
    _enqueue_stage(CycleStage.FIND.value, _cycle_payload(cycle_id), ws, cycle_id)
    return {"cycle_id": cycle_id}


@jobs_service.handler("cycle.find")
def handle_find(ctx):
    _gate(ctx)
    cycle_id = ctx.payload["cycle_id"]
    hunter = TrendHunterAgent()

    def work():
        candidates = hunter.fetch_candidates(ctx.workspace_id)
        new_count = persist_opportunities(hunter, ctx.workspace_id, candidates, cycle_id)
        return {"summary": f"found {new_count} new opportunities ({len(candidates)} candidates)"}

    result = hunter.execute(ctx, "discover", input_summary="", fn=work)
    with session_scope() as s:
        cycle = s_get(s, Cycle, cycle_id)
        cycle.stage = CycleStage.SCORE.value
        cycle.summary_json = {**(cycle.summary_json or {}), "find": result}
    _enqueue_stage(CycleStage.SCORE.value, _cycle_payload(cycle_id), ctx.workspace_id, cycle_id)
    return result


@jobs_service.handler("cycle.score")
def handle_score(ctx):
    _gate(ctx)
    cycle_id = ctx.payload["cycle_id"]
    analyst = TrendAnalystAgent()
    scored = analyst.score_pending(ctx)
    with session_scope() as s:
        cycle = s_get(s, Cycle, cycle_id)
        cycle.stage = CycleStage.SELECT.value
        cycle.summary_json = {**(cycle.summary_json or {}), "score": {"scored": scored}}
    _enqueue_stage(CycleStage.SELECT.value, _cycle_payload(cycle_id), ctx.workspace_id, cycle_id)
    return {"summary": f"scored {scored} opportunities"}


@jobs_service.handler("cycle.select")
def handle_select(ctx):
    """Supervisor decision via the Decision Engine: NEXT BEST ACTION."""
    _gate(ctx)
    cycle_id = ctx.payload["cycle_id"]
    ws = ctx.workspace_id
    from app.engine.decision import decide_next_best_action

    decision = decide_next_best_action(ws)
    why = decision.why()

    race_skip = False
    with session_scope() as s:
        cycle = _get_cycle(s, cycle_id)

        if decision.action == "PRODUCE":
            cycle.stage = CycleStage.RESEARCH.value
            opp = s.get(Opportunity, decision.opportunity_id)
            if opp is None or opp.selected or opp.skipped_reason:
                # Lost a race (e.g. manual selection) — complete gracefully.
                # Do not open a nested session while this transaction is active.
                cycle.status = "COMPLETED"
                cycle.finished_at = utcnow()
                cycle.summary_json = {
                    **(cycle.summary_json or {}),
                    "select": {"decision": "SKIP", "why": {"reasons": ["opportunity taken"]}},
                }
                _with_session_complete_run(s, ws)
                race_skip = True
            else:
                opp.selected = True
                content = ContentItem(
                    workspace_id=ws,
                    cycle_id=cycle_id,
                    opportunity_id=opp.id,
                    topic=opp.topic,
                    status=ContentStatus.IDEA.value,
                    strategy_json={"decision": why},
                )
                s.add(content)
                s.flush()
                cycle.selected_opportunity_id = opp.id
                cycle.content_item_id = content.id
                cycle.summary_json = {
                    **(cycle.summary_json or {}),
                    "select": {"decision": "PRODUCE", "why": why},
                }
                content_id = content.id
                topic = opp.topic
                score = opp.score
        elif decision.action in ("SKIP", "HUMAN_REVIEW"):
            # Persist terminal supervisor decisions so the same candidate is
            # not selected again on the next cycle. HUMAN_REVIEW remains visible
            # to operators via recommendation, while SKIP is permanently filtered.
            opp = s.get(Opportunity, decision.opportunity_id) if decision.opportunity_id else None
            if opp and not opp.selected:
                opp.recommendation = decision.action
                if decision.action == "SKIP":
                    opp.skipped_reason = (decision.reasons[0] if decision.reasons else "skipped by decision engine")[:300]

    if race_skip:
        record_event(
            ws,
            "cycle.skipped",
            "Selected opportunity no longer available",
            level="warning",
            source="supervisor",
            data={"cycle_id": cycle_id},
        )
        _after_cycle_terminal(ctx, ws, success=True)
        return {"decision": "SKIP", "reason": "opportunity taken"}

    if decision.action != "PRODUCE":
        # WAIT / SKIP / RESEARCH_MORE / HUMAN_REVIEW — terminal for this cycle.
        level = {"SKIP": "warning", "WAIT": "info", "RESEARCH_MORE": "info", "HUMAN_REVIEW": "warning"}.get(
            decision.action, "info"
        )
        with session_scope() as s:
            cycle = _get_cycle(s, cycle_id)
            cycle.status = "COMPLETED"
            cycle.finished_at = utcnow()
            cycle.summary_json = {**(cycle.summary_json or {}), "select": {"decision": decision.action, "why": why}}
            _with_session_complete_run(s, ws)
        record_event(
            ws,
            f"decision.{decision.action.lower()}",
            f"{decision.action}: {(decision.topic or 'no candidate')[:70]} — {decision.reasons[0]}",
            level=level,
            source="supervisor",
            data={"cycle_id": cycle_id, "why": why},
        )
        if decision.action == "HUMAN_REVIEW":
            record_event(
                ws,
                "review.required",
                f"Manual approval needed: '{(decision.topic or '')[:60]}' — {decision.reasons[0]}",
                level="warning",
                source="safety",
                data={"opportunity_id": decision.opportunity_id},
            )
        _after_cycle_terminal(ctx, ws, success=True)
        return {"decision": decision.action, "why": why}

    record_event(
        ws,
        "cycle.selected",
        f"Selected '{topic[:60]}' (adjusted score {decision.score:.0f}) — {decision.reasons[0]}",
        level="success",
        source="supervisor",
        data={"cycle_id": cycle_id, "content_id": content_id, "why": why},
    )
    _enqueue_stage(CycleStage.RESEARCH.value, {**_cycle_payload(cycle_id), "content_id": content_id}, ws, cycle_id)
    return {"decision": "PRODUCE", "content_id": content_id}
def _with_session_complete_run(s, ws) -> None:
    run = s.scalar(
        select(AutopilotRun)
        .where(AutopilotRun.workspace_id == ws)
        .where(AutopilotRun.state.not_in([AutopilotState.STOPPED.value]))
        .order_by(AutopilotRun.created_at.desc())
        .limit(1)
    )
    if run:
        run.cycles_completed += 1
        # honor scheduled stop even mid-cycle
        if run.scheduled_stop_at and utcnow() >= run.scheduled_stop_at:
            run.state = AutopilotState.STOPPING.value


MAX_CONSECUTIVE_FAILURES = 3


def on_cycle_failed(workspace_id: str, cycle_id: str, error: str) -> None:
    """Circuit breaker: stop the run after repeated dead cycles."""
    with session_scope() as s:
        cycle = _get_cycle(s, cycle_id)
        run = s.scalar(
            select(AutopilotRun)
            .where(AutopilotRun.workspace_id == workspace_id)
            .where(AutopilotRun.state.not_in([AutopilotState.STOPPED.value]))
            .order_by(AutopilotRun.created_at.desc())
            .limit(1)
        )
        if not run:
            return
        cfg = dict(run.config_json or {})
        failures = int(cfg.get("consecutive_failures", 0)) + 1
        cfg["consecutive_failures"] = failures
        run.config_json = cfg

        terminal = (
            (run.mode == "SINGLE_CYCLE" and run.cycles_completed >= 1)
            or (run.mode == "SINGLE_CYCLE" and failures >= 1)  # a failed single cycle ends the run
            or failures >= MAX_CONSECUTIVE_FAILURES
            or (run.cycles_target and run.cycles_completed >= run.cycles_target)
        )
        if terminal:
            run.state = AutopilotState.STOPPED.value
            if failures >= 1:
                run.last_error = error[:2000]
            reason = (
                f"circuit breaker: {failures} consecutive failed cycles"
                if failures >= MAX_CONSECUTIVE_FAILURES
                else "run finished"
            )
            run.last_error = error[:2000]
            record_event(
                workspace_id,
                "autopilot.stopped",
                f"Autopilot stopped — {reason}. Last error: {error[:200]}",
                level="error",
                source="supervisor",
                data={"cycle_id": cycle_id},
            )
        else:
            record_event(
                workspace_id,
                "cycle.failed",
                f"Cycle failed ({failures}/{MAX_CONSECUTIVE_FAILURES} consecutive): {error[:160]}",
                level="error",
                source="supervisor",
                data={"cycle_id": cycle_id},
            )
            interval = float(cfg.get("interval_seconds", 30))
            jobs_service.enqueue(
                "autopilot.start_next_cycle", {}, workspace_id=workspace_id, priority=5,
                delay_seconds=interval,
            )
            # Safety Center: repeated failures auto-pause before the breaker trips
            if failures >= 2:
                from app.engine.decision import record_auto_pause

                record_auto_pause(
                    workspace_id,
                    "repeated_cycle_failures",
                    f"{failures} consecutive cycles failed; last error: {error[:140]}",
                )


def _schedule_next(ctx, ws: str) -> None:
    """Chain into the next cycle when autopilot remains running."""
    _, cfg = _run_state(ws)
    interval = float((cfg or {}).get("interval_seconds", 30))
    jobs_service.enqueue("autopilot.start_next_cycle", {}, workspace_id=ws, priority=5, delay_seconds=interval)


def _after_cycle_terminal(ctx, ws: str, *, success: bool) -> None:
    """Shared terminal logic: bookkeeping + continue-or-stop decision."""
    with session_scope() as s:
        run = s.scalar(
            select(AutopilotRun)
            .where(AutopilotRun.workspace_id == ws)
            .where(AutopilotRun.state.not_in([AutopilotState.STOPPED.value]))
            .order_by(AutopilotRun.created_at.desc())
            .limit(1)
        )
        if not run:
            return
        cfg = dict(run.config_json or {})
        if success:
            cfg["consecutive_failures"] = 0
            run.config_json = cfg
        mode = run.mode
        target = run.cycles_target
        completed = run.cycles_completed
        state = run.state

    if not success:
        # failure path is owned by on_cycle_failed (job went DEAD) — nothing here
        return
    if state == AutopilotState.STOPPING.value:
        _finalize_stop(ws)
        return
    if mode == "SINGLE_CYCLE" and completed >= 1:
        with session_scope() as s:
            r = s.scalar(
                select(AutopilotRun)
                .where(AutopilotRun.workspace_id == ws)
                .where(AutopilotRun.state.not_in([AutopilotState.STOPPED.value]))
                .order_by(AutopilotRun.created_at.desc()).limit(1)
            )
            if r:
                r.state = AutopilotState.STOPPED.value
        record_event(ws, "autopilot.completed", "Single-cycle run finished", source="supervisor")
        return
    if target and completed >= target:
        with session_scope() as s:
            r = s.scalar(
                select(AutopilotRun)
                .where(AutopilotRun.workspace_id == ws)
                .where(AutopilotRun.state.not_in([AutopilotState.STOPPED.value]))
                .order_by(AutopilotRun.created_at.desc()).limit(1)
            )
            if r:
                r.state = AutopilotState.STOPPED.value
        record_event(ws, "autopilot.completed", f"Reached {target} cycles — stopping", source="supervisor")
        return
    _schedule_next(ctx, ws)


@jobs_service.handler("cycle.research")
def handle_research(ctx):
    _gate(ctx)
    cycle_id = ctx.payload["cycle_id"]
    content_id = ctx.payload["content_id"]
    agent = ResearchAgent()
    with session_scope() as s:
        content = s_get(s, ContentItem, content_id)
        topic = content.topic
        already_done = content.status in (
            ContentStatus.STRATEGY.value,
            ContentStatus.SCRIPTING.value,
            ContentStatus.SCRIPT_READY.value,
            ContentStatus.PRODUCTION.value,
        )
    if already_done:
        # idempotent retry: research already completed in a previous attempt
        _enqueue_stage(CycleStage.BUILD.value, {**_cycle_payload(cycle_id), "content_id": content_id}, ctx.workspace_id, cycle_id)
        return {"summary": "research already complete; continuing"}
    _set_status_checked(content_id, ContentStatus.RESEARCHING.value)
    research = agent.run(ctx, topic)
    with session_scope() as s:
        content = s_get(s, ContentItem, content_id)
        content.research_json = research
        if content.status == ContentStatus.RESEARCHING.value:
            _set_content_status(s, content, ContentStatus.STRATEGY.value)
        cycle = s_get(s, Cycle, cycle_id)
        cycle.stage = CycleStage.BUILD.value
    _enqueue_stage(CycleStage.BUILD.value, {**_cycle_payload(cycle_id), "content_id": content_id}, ctx.workspace_id, cycle_id)
    return {"summary": f"research completed for '{topic[:80]}'"}


def _set_status_checked(content_id: str, target: str) -> None:
    with session_scope() as s:
        content = s_get(s, ContentItem, content_id)
        _set_content_status(s, content, target)


def _max_render_attempts(ws: str) -> int:
    from app.engine.decision import get_safety_settings
    from app.models import Workspace

    with session_scope() as s:
        row = s.get(Workspace, ws)
        return get_safety_settings((row.settings_json or {}) if row else {})["max_render_attempts"]


@jobs_service.handler("cycle.build")
def handle_build(ctx):
    """Strategy → script variations → hook ranking → render selected variant."""
    _gate(ctx)
    cycle_id = ctx.payload["cycle_id"]
    content_id = ctx.payload["content_id"]
    ws = ctx.workspace_id

    # Idempotency: if the CURRENTLY SELECTED variant already has a rendered
    # video, skip re-render (a QC regeneration flips selection to the next
    # variant, so it correctly falls through to a fresh render).
    with session_scope() as s:
        existing = s.query(Video).join(VideoVariant, Video.variant_id == VideoVariant.id).filter(
            VideoVariant.content_item_id == content_id,
            Video.status == "READY",
            VideoVariant.selected.is_(True),
        ).first()
        if existing:
            video_id = existing.id
            content = s_get(s, ContentItem, content_id)
            if can_transition(content.status, ContentStatus.QC.value):
                _set_content_status(s, content, ContentStatus.QC.value)
            cycle = _get_cycle(s, cycle_id)
            cycle.stage = CycleStage.VERIFY.value
        else:
            video_id = None
    if video_id is not None:
        _enqueue_stage(
            CycleStage.VERIFY.value,
            {**_cycle_payload(cycle_id), "content_id": content_id, "video_id": video_id,
             "qc_attempt": ctx.payload.get("qc_attempt", 1)},
            ws, cycle_id,
        )
        return {"summary": "video already rendered; continuing to QC", "video_id": video_id}

    qc_attempt = int(ctx.payload.get("qc_attempt") or 1)

    # ---- regeneration path (qc_attempt > 1): re-render the newly selected
    # variant directly. Strategy/scripts already exist; the QC instruction was
    # stored on the variant at rejection time.
    if qc_attempt > 1:
        producer = VideoProducerAgent()
        with session_scope() as s:
            content = s_get(s, ContentItem, content_id)
            variant = s.query(VideoVariant).filter(
                VideoVariant.content_item_id == content_id,
                VideoVariant.selected.is_(True),
            ).first()
            research = dict(content.research_json or {})
            topic = content.topic
            strategy = dict(content.strategy_json or {})
            vid = variant.id
            script = variant.script

        _set_status_checked(content_id, ContentStatus.PRODUCTION.value)
        from app.engine.platform_formats import aspect_for_platforms

        aspect = aspect_for_platforms(strategy.get("platforms")) or "9:16"
        render = producer.render(
            ctx, topic=topic, script=script,
            keywords=research.get("visual_keywords", []),
            aspect_ratio=aspect, variant_id=vid,
        )
        video_id = render["video_id"]
        with session_scope() as s:
            content = s_get(s, ContentItem, content_id)
            _set_content_status(s, content, ContentStatus.QC.value)
            cycle = _get_cycle(s, cycle_id)
            cycle.stage = CycleStage.VERIFY.value
        _enqueue_stage(
            CycleStage.VERIFY.value,
            {**_cycle_payload(cycle_id), "content_id": content_id, "video_id": video_id,
             "qc_attempt": qc_attempt},
            ws, cycle_id,
        )
        return {"summary": f"regenerated '{topic[:60]}'", "video_id": video_id}

    strategist = StrategistAgent()
    writer = ScriptWriterAgent()
    hooker = HookOptimizerAgent()
    producer = VideoProducerAgent()

    with session_scope() as s:
        content = s_get(s, ContentItem, content_id)
        topic = content.topic
        research = dict(content.research_json or {})
        # QC regeneration feedback from a previous rejected attempt (if any)
        regen_instruction = None
        if content.error and "QC" in (content.error or ""):
            regen_instruction = content.error.split("—", 1)[-1].strip() or None
        patterns_rows = s.scalars(
            select(LearningPattern).where(LearningPattern.workspace_id == ws, LearningPattern.active.is_(True))
        ).all()
        patterns = [
            {"pattern_key": p.pattern_key, "observed_improvement_pct": p.observed_improvement_pct,
             "confidence": p.confidence, "active": p.active}
            for p in patterns_rows
        ]

    qc_attempt = int(ctx.payload.get("qc_attempt") or 1)
    strategy = strategist.run(ctx, topic, research)
    with session_scope() as s:
        content = s_get(s, ContentItem, content_id)
        # preserve the decision WHY stored at selection time
        content.strategy_json = {**(content.strategy_json or {}), **strategy}
    _set_status_checked(content_id, ContentStatus.SCRIPTING.value)

    n_variants = int(strategy.get("variations") or 3)
    variants = writer.run_variations(
        ctx, topic, strategy, research,
        count=max(1, min(n_variants, 5)),
        regeneration_instruction=regen_instruction if qc_attempt > 1 else None,
    )
    ranked = hooker.run(ctx, variants, patterns)

    # persist variants; select best. Regeneration batches get attempt-prefixed
    # labels so repeated QC loops never collide with earlier rows.
    label_prefix = f"r{qc_attempt}." if qc_attempt > 1 else ""
    variant_ids = []
    with session_scope() as s:
        content = s_get(s, ContentItem, content_id)
        for v in ranked:
            row = VideoVariant(
                content_item_id=content_id,
                label=f"{label_prefix}{v.get('label', 'v')}",
                hook=v["script"][:180],
                script=v["script"],
                predicted_score=v.get("predicted_score"),
                selected=False,
            )
            s.add(row)
            s.flush()
            variant_ids.append((row.id, v.get("predicted_score") or 0, v))
        best_vid, _score, best_v = max(variant_ids, key=lambda t: t[1])
        db_best = s.get(VideoVariant, best_vid)
        db_best.selected = True
        _set_content_status(s, content, ContentStatus.SCRIPT_READY.value)

    _set_status_checked(content_id, ContentStatus.PRODUCTION.value)
    # platform-normalized aspect (single source of truth for formats)
    from app.engine.platform_formats import aspect_for_platforms

    aspect = aspect_for_platforms(strategy.get("platforms")) or strategy.get("aspect_ratio", "9:16")
    render = producer.render(
        ctx,
        topic=topic,
        script=best_v["script"],
        keywords=research.get("visual_keywords", []),
        aspect_ratio=aspect,
        variant_id=best_vid,
    )

    with session_scope() as s:
        # VideoProducerAgent.render creates and durably finalizes the Video row.
        # Creating another row here breaks idempotency and can make publishing
        # select an artifact different from the one that was rendered.
        video = s.get(Video, render["video_id"])
        if video is None or video.workspace_id != ws or video.variant_id != best_vid:
            raise RuntimeError("render completed without a matching video record")
        params = dict(video.params_json or {})
        params.update({"topic": topic, "duration": strategy.get("duration_seconds")})
        video.params_json = params
        video_id = video.id
        content = s_get(s, ContentItem, content_id)
        _set_content_status(s, content, ContentStatus.QC.value)
        cycle = s_get(s, Cycle, cycle_id)
        cycle.stage = CycleStage.VERIFY.value

    _enqueue_stage(CycleStage.VERIFY.value, {**_cycle_payload(cycle_id), "content_id": content_id, "video_id": video_id}, ws, cycle_id)
    return {"summary": f"built video for '{topic[:60]}'", "video_id": video_id, "variants": len(ranked)}


@jobs_service.handler("cycle.verify")
def handle_verify(ctx):
    """AI quality control with regenerate-on-reject policy."""
    _gate(ctx)
    cycle_id = ctx.payload["cycle_id"]
    content_id = ctx.payload["content_id"]
    video_id = ctx.payload["video_id"]
    quality = QualityAgent()
    attempt = int(ctx.payload.get("qc_attempt", 1))

    with session_scope() as s:
        variant = s.query(VideoVariant).filter(VideoVariant.content_item_id == content_id, VideoVariant.selected.is_(True)).first()
        script = variant.script if variant else ""
        content_row = s_get(s, ContentItem, content_id)
        qc_strategy = dict(content_row.strategy_json or {})
        qc_research = dict(content_row.research_json or {})
        video_row = s.get(Video, video_id) if video_id else None
        video_path = (video_row.file_path or "") if video_row else ""

    verdict = quality.evaluate(ctx, script=script, strategy=qc_strategy, research=qc_research, video_path=video_path)

    with session_scope() as s:
        from app.models import QualityCheck

        s.add(
            QualityCheck(
                video_id=video_id,
                overall=verdict["overall"],
                passed=verdict["passed"],
                components_json={
                    "components": verdict["components"],
                    "notes": verdict.get("notes", ""),
                    "vision": verdict.get("vision"),
                },
                notes=verdict.get("notes", ""),
            )
        )

    if verdict["passed"]:
        from app.engine.decision import get_safety_settings
        from app.models import Workspace as _WS

        with session_scope() as s:
            ws_row = s.get(_WS, ctx.workspace_id) if ctx.workspace_id else None
            safety = get_safety_settings((ws_row.settings_json or {}) if ws_row else {})
        if safety.get("require_approval_before_publish"):
            with session_scope() as s:
                content = s_get(s, ContentItem, content_id)
                _set_content_status(s, content, ContentStatus.APPROVED.value)
            record_event(ctx.workspace_id, "review.required", f"Approval hold: video {video_id[:8]} passed QC and awaits human approval", level="warning", source="quality", data={"video_id": video_id, "content_id": content_id})
            return {"passed": True, "held": True, "score": verdict["overall"]}
        with session_scope() as s:
            content = s_get(s, ContentItem, content_id)
            _set_content_status(s, content, ContentStatus.APPROVED.value)
            cycle = s_get(s, Cycle, cycle_id)
            cycle.stage = CycleStage.UPLOAD.value
        record_event(ctx.workspace_id, "quality.passed", f"Quality approved ({verdict['overall']:.0f}/100)", level="success", source="quality", data={"video_id": video_id})
        _enqueue_stage(CycleStage.UPLOAD.value, {**_cycle_payload(cycle_id), "content_id": content_id, "video_id": video_id}, ctx.workspace_id, cycle_id)
        return {"passed": True, "score": verdict["overall"]}

    # rejected — regenerate with a DIFFERENT variant so QC evaluates new content
    next_variant_id = None
    instruction = regeneration_instruction(verdict)
    with session_scope() as s:
        current = s.query(VideoVariant).filter(
            VideoVariant.content_item_id == content_id, VideoVariant.selected.is_(True)
        ).first()
        others = s.query(VideoVariant).filter(
            VideoVariant.content_item_id == content_id,
            VideoVariant.id != (current.id if current else ""),
            VideoVariant.selected.is_(False),
        ).all()
        if current and others:
            current.selected = False
            nxt = max(others, key=lambda v: (v.predicted_score or 0))
            nxt.selected = True
            # carry the QC failure reason into the variant so BUILD can act on it
            nxt.metadata_json = {
                **(nxt.metadata_json or {}),
                "regeneration_instruction": instruction,
                "qc_feedback": {
                    "score": verdict.get("overall"),
                    "components": verdict.get("components", {}),
                },
            }
            next_variant_id = nxt.id
            content = s_get(s, ContentItem, content_id)
            content.error = f"QC {verdict.get('overall'):.0f}/100 — {instruction}"
            if can_transition(content.status, ContentStatus.SCRIPT_READY.value):
                _set_content_status(s, content, ContentStatus.SCRIPT_READY.value)

    if attempt < _max_render_attempts(ctx.workspace_id) and next_variant_id and cost_service.budget_available(ctx.workspace_id)[0]:
        record_event(
            ctx.workspace_id, "quality.rejected",
            f"Quality rejected ({verdict['overall']:.0f}/100) — regenerating with variant {next_variant_id[:8]}",
            level="warning", source="quality",
            data={"attempt": attempt, "new_variant": next_variant_id},
        )
        _enqueue_stage(
            CycleStage.BUILD.value,
            {**_cycle_payload(cycle_id), "content_id": content_id, "qc_attempt": attempt + 1},
            ctx.workspace_id,
            cycle_id,
        )
        return {"passed": False, "regenerating": True}

    with session_scope() as s:
        content = s_get(s, ContentItem, content_id)
        content.error = f"quality below threshold after {attempt} attempts"
        _set_content_status(s, content, ContentStatus.FAILED.value)
        cycle = _get_cycle(s, cycle_id)
        cycle.status = "FAILED"
        cycle.error = content.error
        cycle.finished_at = utcnow()
    record_event(ctx.workspace_id, "quality.failed", f"Video failed quality after {attempt} attempts", level="error", source="quality")
    on_cycle_failed(ctx.workspace_id, cycle_id, "quality below threshold")
    return {"passed": False, "final": True}


@jobs_service.handler("cycle.upload")
def handle_upload(ctx):
    """SEO metadata then publish to all strategy platforms."""
    _gate(ctx)
    cycle_id = ctx.payload["cycle_id"]
    content_id = ctx.payload["content_id"]
    video_id = ctx.payload["video_id"]
    ws = ctx.workspace_id

    seo = SEOAgent()
    publisher = PublisherAgent()

    with session_scope() as s:
        video = s_get(s, Video, video_id)
        content = s_get(s, ContentItem, content_id)
        strategy = dict(content.strategy_json or {})
        platforms = [p for p in strategy.get("platforms", ["youtube", "tiktok"]) if p]
        # Scheduled publishing: restrict to the platform the calendar entry targets
        override = ctx.payload.get("platforms_override")
        if override:
            platforms = [p for p in platforms if p in override] or list(override)
        local_path = video.file_path
        topic = content.topic

    metadata = seo.run(ctx, topic=topic, script=_script_for(content_id), platforms=platforms)
    with session_scope() as s:
        _video = s_get(s, Video, video_id)
        _thumb = (_video.thumbnail_path or "") if _video else ""
    if _thumb:
        for p in metadata:
            if isinstance(metadata[p], dict) and not metadata[p].get("thumbnail_path"):
                metadata[p]["thumbnail_path"] = _thumb
    scheduled_ts: float | None = None
    if ctx.payload.get("scheduled_entry_id"):
        with session_scope() as s:
            entry = s.get(ScheduleEntry, ctx.payload["scheduled_entry_id"])
            if entry and entry.run_at:
                scheduled_ts = entry.run_at.replace(tzinfo=None).timestamp()
    if scheduled_ts:
        for p in metadata:
            extra = dict((metadata[p] or {}).get("extra") or {})
            extra["scheduled_publish_time"] = str(int(scheduled_ts))
            metadata[p]["extra"] = extra
    with session_scope() as s:
        content = s_get(s, ContentItem, content_id)
        # store metadata on selected variant
        variant = s.query(VideoVariant).filter(VideoVariant.content_item_id == content_id, VideoVariant.selected.is_(True)).first()
        if variant:
            variant.metadata_json = metadata

    # ---- compliance gate (E5): spec preflight + reused-content risk ----
    from app.engine.agents.compliance import ComplianceOfficerAgent

    with session_scope() as s:
        _research = dict((s_get(s, ContentItem, content_id).research_json) or {})
    comp = ComplianceOfficerAgent().review(
        ctx, video_path=local_path, platforms=platforms, topic=topic,
        script=_script_for(content_id), metadata_by_platform=metadata,
        visual_keywords=_research.get("visual_keywords", []),
    )
    for p in platforms:
        if isinstance(metadata.get(p), dict):
            metadata[p] = {**metadata[p], "compliance": {
                "passed": p not in comp["spec_failed"],
                "risk_score": comp["risk_score"],
                "require_human": comp["require_human"],
            }}
    with session_scope() as s:
        variant = s.query(VideoVariant).filter(VideoVariant.content_item_id == content_id, VideoVariant.selected.is_(True)).first()
        if variant:
            variant.metadata_json = metadata
    if comp["spec_failed"]:
        with session_scope() as s:
            for p in comp["spec_failed"]:
                reason = next((f["message"] for f in comp["findings"]
                               if f.get("platform") == p and f.get("severity") == "fail"),
                              "spec preflight failed")
                job = s.query(PublishingJob).filter(
                    PublishingJob.video_id == video_id, PublishingJob.platform == p).first()
                if job is None:
                    job = PublishingJob(workspace_id=ws, video_id=video_id, platform=p,
                                        metadata_json=metadata.get(p) or {})
                    s.add(job)
                    s.flush()
                job.status = "FAILED"
                job.error = f"compliance: {reason}"[:2000]
        record_event(ws, "compliance.blocked",
                     f"Spec preflight blocked: {', '.join(comp['spec_failed'])}",
                     level="warning", source="compliance", data={"video_id": video_id})
        platforms = [p for p in platforms if p not in comp["spec_failed"]]
    if comp["require_human"]:
        with session_scope() as s:
            content = s_get(s, ContentItem, content_id)
            if can_transition(content.status, ContentStatus.APPROVED.value):
                _set_content_status(s, content, ContentStatus.APPROVED.value)
            content.error = f"compliance hold: {comp['summary']}"
        record_event(ws, "review.required",
                     f"Compliance hold — human review required: {comp['summary']}",
                     level="warning", source="compliance",
                     data={"video_id": video_id, "content_id": content_id,
                           "risk_score": comp["risk_score"]})
        scheduled_entry_id = ctx.payload.get("scheduled_entry_id")
        if scheduled_entry_id:
            with session_scope() as s:
                entry = s.get(ScheduleEntry, scheduled_entry_id)
                if entry and entry.workspace_id == ws and entry.status in ("DISPATCHING", "QUEUED"):
                    entry.status = "FAILED"
        if cycle_id:
            _after_cycle_terminal(ctx, ws, success=True)
            return {"published": 0, "held": True}
        return {"published": 0, "held": True, "scheduled": True}
    if not platforms:
        if cycle_id:
            _after_cycle_terminal(ctx, ws, success=True)
        return {"published": 0, "blocked": sorted(comp["spec_failed"])}

    # ---- idempotency: never publish the same video to a platform twice ----
    with session_scope() as s:
        already_published = {
            p.platform
            for p in s.query(PublishedPost).filter(
                PublishedPost.video_id == video_id,
                PublishedPost.platform.in_(platforms),
            ).all()
        }
        terminal_failures = {
            j.platform
            for j in s.query(PublishingJob).filter(
                PublishingJob.video_id == video_id,
                PublishingJob.platform.in_(platforms),
                PublishingJob.status == "FAILED",
            ).all()
        }
    pending_platforms = [
        p for p in platforms if p not in already_published and p not in terminal_failures
    ]
    if not pending_platforms and already_published:
        record_event(ws, "publish.skipped",
                     "All platforms already published for this video (idempotent skip)",
                     level="info", source="publisher", data={"video_id": video_id})
        scheduled_entry_id = ctx.payload.get("scheduled_entry_id")
        if scheduled_entry_id:
            with session_scope() as s:
                entry = s.get(ScheduleEntry, scheduled_entry_id)
                if entry and entry.workspace_id == ws and entry.status in ("DISPATCHING", "QUEUED"):
                    entry.status = "DONE"
        if cycle_id:
            _enqueue_stage(
                CycleStage.MEASURE.value,
                {**_cycle_payload(cycle_id), "content_id": content_id},
                ws, cycle_id, delay=5,
            )
        # A scheduled upload has no cycle to own a cycle.measure job. The
        # first successful scheduled publish collects immediately; retries are
        # a true no-op rather than enqueueing an invalid cycle_id=None job.
        return {"published": 0, "already_published": sorted(already_published)}


    # ---- idempotency claim -------------------------------------------------
    # Register and atomically claim each platform BEFORE any network call.
    # A concurrent/retried stage can see PUBLISHING and must not submit a
    # second post. Claims older than 15 minutes are recoverable after a worker
    # crash; the remote provider/idempotency layer still protects final writes.
    stale_before = utcnow() - timedelta(minutes=15)
    claimed_platforms: list[str] = []
    with session_scope() as s:
        for platform in pending_platforms:
            job = s.query(PublishingJob).filter(
                PublishingJob.video_id == video_id, PublishingJob.platform == platform
            ).first()
            if job is None:
                job = PublishingJob(
                    workspace_id=ws, video_id=video_id, platform=platform, status="QUEUED",
                    metadata_json=metadata.get(platform) or {},
                )
                s.add(job)
                s.flush()
            if job.status == "PUBLISHING" and job.updated_at > stale_before:
                continue
            if job.status == "PUBLISHED":
                continue
            job.status = "PUBLISHING"
            job.attempt += 1
            job.error = ""
            claimed_platforms.append(platform)

    if not claimed_platforms:
        return {"published": 0, "in_flight": pending_platforms}

    try:
        results = publisher.publish_to_platforms(
            ctx,
            video_path=local_path,
            platforms=claimed_platforms,
            metadata_by_platform=metadata,
            workspace_id=ws,
        )
    except Exception as exc:
        with session_scope() as s:
            for platform in claimed_platforms:
                job = s.query(PublishingJob).filter(
                    PublishingJob.video_id == video_id, PublishingJob.platform == platform
                ).first()
                if job and job.status == "PUBLISHING":
                    # Keep the claim retryable: marking it terminal FAILED here
                    # makes the queue's own retry a no-op (nothing left to
                    # claim) that "completes" while the schedule stays QUEUED
                    # forever and the content never publishes.
                    job.status = "RETRYING"
                    job.error = f"{type(exc).__name__}: {exc}"[:2000]
        raise

    post_ids = []
    scheduled_entry_id = ctx.payload.get("scheduled_entry_id")
    with session_scope() as s:
        any_success = False
        for r in results:
            platform = r["platform"]
            job = s.query(PublishingJob).filter(
                PublishingJob.video_id == video_id, PublishingJob.platform == platform
            ).first()
            if job is None:
                job = PublishingJob(workspace_id=ws, video_id=video_id, platform=platform)
                s.add(job)
                s.flush()
            # attempt is incremented when the platform is claimed, before the
            # external call, so a crash cannot hide an in-flight attempt.
            job.status = "PUBLISHED" if r["success"] else ("RETRYING" if r.get("retryable") else "FAILED")
            job.published_at = utcnow() if r["success"] else None
            job.remote_post_id = r.get("remote_post_id", "")
            job.remote_url = r.get("remote_url", "")
            job.error = r.get("error", "")

            if r["success"]:
                any_success = True
                post = s.query(PublishedPost).filter(
                    PublishedPost.video_id == video_id, PublishedPost.platform == platform
                ).first()
                if post is None:  # unique per (video, platform) — retries reuse the row
                    post = PublishedPost(workspace_id=ws, video_id=video_id, platform=platform)
                    s.add(post)
                    s.flush()
                post.content_item_id = content_id
                post.remote_post_id = r.get("remote_post_id", "")
                post.remote_url = r.get("remote_url", "")
                post.title = ((metadata.get(platform) or {}).get("title") or "")[:280]
                post.published_at = utcnow()
                post.is_mock = bool(r.get("mock"))
                post_ids.append(post.id)

        content = s_get(s, ContentItem, content_id)
        cycle = _get_cycle(s, cycle_id) if cycle_id else None
        retryable_failures = [r for r in results if not r["success"] and r.get("retryable")]
        permanent_failures = [r for r in results if not r["success"] and not r.get("retryable")]
        if any_success:
            _set_content_status(s, content, ContentStatus.PUBLISHED.value)
            # Do not advance to measurement while a transient platform failure
            # still needs retrying; the retry attempt will enqueue measurement
            # exactly once after all non-terminal platforms settle.
            if cycle and not retryable_failures:
                cycle.stage = CycleStage.MEASURE.value
        else:
            content.error = "; ".join(r.get("error") for r in results if r.get("error"))
            # A scheduled item remains SCHEDULED/APPROVED after a failed
            # provider call so the failure is honest and can be retried.
            if cycle and can_transition(content.status, ContentStatus.APPROVED.value):
                _set_content_status(s, content, ContentStatus.APPROVED.value)
            if cycle and not retryable_failures:
                cycle.status = "FAILED"
                cycle.error = "publishing failed on all platforms"
                cycle.finished_at = utcnow()

        if scheduled_entry_id:
            entry = s.get(ScheduleEntry, scheduled_entry_id)
            if entry and entry.workspace_id == ws:
                if any_success:
                    entry.status = "DONE" if not retryable_failures else "QUEUED"
                elif retryable_failures:
                    entry.status = "QUEUED"
                elif permanent_failures:
                    entry.status = "FAILED"

    if retryable_failures:
        # Let the durable queue apply exponential backoff and retry this same
        # payload. Successful platforms are already idempotently recorded and
        # will be skipped on the retry; terminal failures remain suppressed.
        raise RuntimeError(
            "retryable publishing failures: "
            + "; ".join((r.get("error") or r["platform"])[:160] for r in retryable_failures)
        )

    if post_ids:
        _, cfg = _run_state(ws)
        measure_delay_min = float((cfg or {}).get("measure_delay_minutes", 30))
        record_event(ws, "publish.done", f"Published to {len(post_ids)} platform(s)", level="success", source="publisher", data={"posts": len(post_ids)})
        # Safety Center: abnormal publishing rate check
        _check_publishing_rate(ws)
        # Scheduled publish (no owning cycle): measurement would double-collect
        # via the cycle path; collect immediately instead.
        if not cycle_id:
            collector = AnalyticsCollectorAgent()
            collector.collect_for_posts(ctx, post_ids=post_ids)
            return {"published": len(post_ids), "scheduled": True}
        _enqueue_stage(
            CycleStage.MEASURE.value,
            {**_cycle_payload(cycle_id), "content_id": content_id, "post_ids": post_ids},
            ws, cycle_id, delay=measure_delay_min * 60,
        )
        return {"published": len(post_ids)}

    record_event(ws, "publish.failed", "Publishing failed on all platforms", level="error", source="publisher")
    if cycle_id:
        on_cycle_failed(ws, cycle_id, "publishing failed on all platforms")
    return {"published": 0, "errors": [r.get("error") for r in results]}


# ---------------------------------------------------------------------------
# Scheduled publishing executor
# ---------------------------------------------------------------------------

SWEEP_INTERVAL_SECONDS = 60
# A lease prevents a process that dies between claiming an entry and inserting
# its queue job from losing the schedule permanently. The idempotency key makes
# recovery safe when the queue insert succeeded but the final status update did
# not.
SCHEDULE_DISPATCH_LEASE_SECONDS = 300


def sweep_due_schedules() -> int:
    """Claim and dispatch due schedule entries without false completion.

    Entries move PENDING -> DISPATCHING in a short database transaction. The
    queue insert happens after that transaction, then the entry becomes QUEUED
    only after insertion returns. Provider success later changes it to DONE.
    A stale DISPATCHING or QUEUED lease is recoverable;
    reusing the entry idempotency key prevents duplicate queue jobs after a
    crash in either half of the dispatch.
    """
    now = utcnow()
    lease_before = now - timedelta(seconds=SCHEDULE_DISPATCH_LEASE_SECONDS)
    to_dispatch: list[dict] = []
    cancelled_notes: list[str] = []

    with session_scope() as s:
        eligible_ids = s.scalars(
            select(ScheduleEntry.id)
            .where(
                or_(
                    and_(ScheduleEntry.status == "PENDING", ScheduleEntry.run_at <= now),
                    and_(
                        ScheduleEntry.status.in_(["DISPATCHING", "QUEUED"]),
                        ScheduleEntry.updated_at <= lease_before,
                    ),
                )
            )
            .order_by(ScheduleEntry.run_at.asc())
            .limit(20)
        ).all()

        for entry_id in eligible_ids:
            # Conditional UPDATE makes the claim safe across multiple sweep
            # workers/processes: only one can win PENDING or the stale lease.
            claimed = s.execute(
                update(ScheduleEntry)
                .where(
                    ScheduleEntry.id == entry_id,
                    or_(
                        and_(ScheduleEntry.status == "PENDING", ScheduleEntry.run_at <= now),
                        and_(
                            ScheduleEntry.status.in_(["DISPATCHING", "QUEUED"]),
                            ScheduleEntry.updated_at <= lease_before,
                        ),
                    ),
                )
                .values(status="DISPATCHING", updated_at=now)
            )
            if claimed.rowcount != 1:
                continue

            entry = s.get(ScheduleEntry, entry_id)
            content = s_get(s, ContentItem, entry.content_item_id) if entry.content_item_id else None
            video_id = None
            if content is not None and content.workspace_id == entry.workspace_id:
                variant = s.query(VideoVariant).filter(
                    VideoVariant.content_item_id == content.id,
                    VideoVariant.selected.is_(True),
                ).first()
                if variant is not None:
                    video_row = s.query(Video).filter(
                        Video.variant_id == variant.id,
                        Video.workspace_id == entry.workspace_id,
                        Video.status == "READY",
                    ).first()
                    video_id = video_row.id if video_row else None

            if video_id:
                to_dispatch.append({
                    "entry_id": entry.id,
                    "workspace_id": entry.workspace_id,
                    "content_id": content.id,
                    "video_id": video_id,
                    "platform": entry.platform,
                })
            else:
                # No renderable content attached (or a foreign/deleted
                # reference): scheduling alone cannot publish, so be honest.
                entry.status = "CANCELLED"
                cancelled_notes.append(entry.workspace_id)

    # Queue insertion is intentionally outside the claim transaction. If it
    # fails, return the entry to PENDING so the next sweep can retry; if the
    # process dies here, the lease recovery above retries with the same key.
    dispatched = 0
    for d in to_dispatch:
        try:
            jobs_service.enqueue(
                "cycle.upload",
                {"cycle_id": None, "content_id": d["content_id"], "video_id": d["video_id"],
                 "platforms_override": [d["platform"]], "scheduled_entry_id": d["entry_id"]},
                workspace_id=d["workspace_id"],
                priority=20,
                idempotency_key=f"sched-{d['entry_id']}",
            )
        except Exception as exc:
            with session_scope() as s:
                entry = s.get(ScheduleEntry, d["entry_id"])
                if entry and entry.status == "DISPATCHING":
                    entry.status = "PENDING"
            record_event(
                d["workspace_id"],
                "schedule.dispatch_failed",
                f"Scheduled publish could not be queued: {type(exc).__name__}",
                level="error", source="scheduler",
                data={"entry_id": d["entry_id"]},
            )
            continue

        # None means the idempotency key already exists. That is the expected
        # recovery result after a crash following a successful queue insert.
        with session_scope() as s:
            entry = s.get(ScheduleEntry, d["entry_id"])
            if entry and entry.status == "DISPATCHING":
                entry.status = "QUEUED"
                dispatched += 1

    for ws_id in cancelled_notes:
        record_event(ws_id, "schedule.cancelled",
                     "Scheduled entry cancelled: no renderable content attached",
                     level="warning", source="scheduler")
    return dispatched


@jobs_service.handler("system.schedule_sweep")
def handle_schedule_sweep(ctx):
    """Periodic sweep: publish due schedule entries, then re-arm itself."""
    count = sweep_due_schedules()
    jobs_service.enqueue(
        "system.schedule_sweep", {},
        delay_seconds=SWEEP_INTERVAL_SECONDS,
        idempotency_key=f"schedule-sweep-{int((utcnow().timestamp() + SWEEP_INTERVAL_SECONDS) // SWEEP_INTERVAL_SECONDS)}",
    )
    return {"dispatched": count}


def start_schedule_sweep() -> None:
    """Arm the recurring sweep (called at app startup). Idempotent via key."""
    jobs_service.enqueue(
        "system.schedule_sweep", {},
        delay_seconds=SWEEP_INTERVAL_SECONDS,
        idempotency_key=f"schedule-sweep-{int((utcnow().timestamp() + SWEEP_INTERVAL_SECONDS) // SWEEP_INTERVAL_SECONDS)}",
    )


def _check_publishing_rate(ws: str) -> None:
    """Safety Center trigger: uploads/hour above the configured cap."""
    from datetime import timedelta

    from app.engine.decision import get_safety_settings, record_auto_pause
    from app.models import Workspace

    with session_scope() as s:
        ws_row = s.get(Workspace, ws)
        safety = get_safety_settings((ws_row.settings_json or {}) if ws_row else {})
        recent = s.scalar(
            select(func.count()).select_from(PublishedPost).where(
                PublishedPost.workspace_id == ws,
                PublishedPost.published_at >= utcnow() - timedelta(hours=1),
            )
        ) or 0
    if recent > safety["max_uploads_per_hour"]:
        record_auto_pause(
            ws,
            "abnormal_publishing_rate",
            f"{recent} uploads in the last hour (cap {safety['max_uploads_per_hour']})",
        )


def _script_for(content_id: str) -> str:
    with session_scope() as s:
        variant = s.query(VideoVariant).filter(VideoVariant.content_item_id == content_id, VideoVariant.selected.is_(True)).first()
        return variant.script if variant else ""


@jobs_service.handler("cycle.measure")
def handle_measure(ctx):
    _gate(ctx)
    cycle_id = ctx.payload["cycle_id"]
    content_id = ctx.payload["content_id"]
    post_ids = ctx.payload.get("post_ids") or []
    collector = AnalyticsCollectorAgent()
    collector.collect_for_posts(ctx, post_ids=post_ids)
    with session_scope() as s:
        content = s_get(s, ContentItem, content_id)
        if content.status == ContentStatus.PUBLISHED.value:
            _set_content_status(s, content, ContentStatus.ANALYZING.value)
        cycle = _get_cycle(s, cycle_id)
        cycle.stage = CycleStage.LEARN.value
    _enqueue_stage(CycleStage.LEARN.value, {**_cycle_payload(cycle_id), "content_id": content_id}, ctx.workspace_id, cycle_id)
    return {"summary": f"metrics collected for {len(post_ids)} posts"}


@jobs_service.handler("cycle.learn")
def handle_learn(ctx):
    _gate(ctx)
    cycle_id = ctx.payload["cycle_id"]
    content_id = ctx.payload["content_id"]
    learner = LearningAgent()
    learner.learn_from_recent(ctx)
    with session_scope() as s:
        content = s_get(s, ContentItem, content_id)
        if content.status == ContentStatus.ANALYZING.value:
            _set_content_status(s, content, ContentStatus.LEARNED.value)
        cycle = _get_cycle(s, cycle_id)
        cycle.status = "COMPLETED"
        cycle.finished_at = utcnow()
        total_cost = s.scalar(
            select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(CostEntry.cycle_id == cycle_id)
        )
        cycle.cost_usd = round(float(total_cost or 0.0), 4)
        _with_session_complete_run(s, ctx.workspace_id)
    _after_cycle_terminal(ctx, ctx.workspace_id, success=True)
    record_event(ctx.workspace_id, "cycle.completed", "Cycle completed — learning updated", level="success", source="supervisor", data={"cycle_id": cycle_id})
    return {"summary": "cycle completed"}


# ---------------------------------------------------------------------------
# Public API used by routes/services
# ---------------------------------------------------------------------------


def get_autopilot_status(workspace_id: str) -> dict:
    with session_scope() as s:
        run = s.scalar(
            select(AutopilotRun)
            .where(AutopilotRun.workspace_id == workspace_id)
            .order_by(AutopilotRun.created_at.desc())
            .limit(1)
        )
        if not run:
            return {"state": AutopilotState.IDLE.value, "cycles_completed": 0}
        latest_cycle = s.scalar(
            select(Cycle).where(Cycle.autopilot_run_id == run.id).order_by(Cycle.number.desc()).limit(1)
        )
        active_jobs = s.scalars(
            select(jobs_service.Job).where(
                jobs_service.Job.workspace_id == workspace_id,
                jobs_service.Job.status.in_(["QUEUED", "RUNNING", "RETRYING"]),
            )
        ).all()
        return {
            "state": run.state,
            "mode": run.mode,
            "cycles_completed": run.cycles_completed,
            "cycles_target": run.cycles_target,
            "scheduled_start_at": run.scheduled_start_at.isoformat() + "Z" if run.scheduled_start_at else None,
            "scheduled_stop_at": run.scheduled_stop_at.isoformat() + "Z" if run.scheduled_stop_at else None,
            "current_cycle": {
                "id": latest_cycle.id,
                "number": latest_cycle.number,
                "stage": latest_cycle.stage,
                "status": latest_cycle.status,
            }
            if latest_cycle and latest_cycle.status == "RUNNING"
            else None,
            "queued_jobs": len(active_jobs),
            "config": run.config_json or {},
            "last_error": run.last_error or "",
        }


def start_autopilot(workspace_id: str, *, mode: str = "CONTINUOUS", cycles_target: int = 0,
                    scheduled_start_at=None, scheduled_stop_at=None, config: dict | None = None,
                    override_readiness: bool = False) -> dict:
    # ---- Production readiness gate --------------------------------------
    from app.services.readiness import run_readiness

    rd = run_readiness()
    if rd["status"] != "ready" and not override_readiness:
        from app.services import events as _events

        _events.record_event(
            workspace_id, "autopilot.blocked",
            f"START refused — {rd['message']}",
            level="error", source="safety",
            data={"blocking_failures": rd["blocking_failures"], "checks": rd["checks"]},
        )
        return {"blocked": True, "readiness": rd}

    with session_scope() as s:
        existing = s.scalar(
            select(AutopilotRun)
            .where(AutopilotRun.workspace_id == workspace_id)
            .where(AutopilotRun.state.not_in([AutopilotState.STOPPED.value]))
            .limit(1)
        )
        if existing:
            raise ValueError("autopilot already running for this workspace")

        # apply scheduled start
        now = utcnow()
        initial_state = AutopilotState.STARTING.value
        if scheduled_start_at and scheduled_start_at > now:
            initial_state = AutopilotState.IDLE.value

        run = AutopilotRun(
            workspace_id=workspace_id,
            mode=mode,
            state=initial_state,
            cycles_target=cycles_target,
            scheduled_start_at=scheduled_start_at,
            scheduled_stop_at=scheduled_stop_at,
            config_json=config or {},
        )
        s.add(run)
        s.flush()
        run_id = run.id
        delay = 0.0
        if scheduled_start_at and scheduled_start_at > now:
            delay = (scheduled_start_at - now).total_seconds()
    record_event(workspace_id, "autopilot.started", "Autopilot starting", level="success", source="supervisor")
    jobs_service.enqueue("autopilot.start_next_cycle", {}, workspace_id=workspace_id, priority=5, delay_seconds=delay)
    return {"run_id": run_id, "state": initial_state}


def stop_autopilot(workspace_id: str) -> bool:
    with session_scope() as s:
        run = s.scalar(
            select(AutopilotRun)
            .where(AutopilotRun.workspace_id == workspace_id)
            .where(AutopilotRun.state.not_in([AutopilotState.STOPPED.value]))
            .limit(1)
        )
        if not run:
            return False
        run.state = AutopilotState.STOPPING.value
    record_event(workspace_id, "autopilot.stopping", "Autopilot stopping after current steps", level="info", source="supervisor")
    return True


def pause_autopilot(workspace_id: str) -> bool:
    with session_scope() as s:
        run = s.scalar(
            select(AutopilotRun)
            .where(AutopilotRun.workspace_id == workspace_id)
            .where(AutopilotRun.state == AutopilotState.RUNNING.value)
            .limit(1)
        )
        if not run:
            return False
        run.state = AutopilotState.PAUSED.value
    record_event(workspace_id, "autopilot.paused", "Autopilot paused", level="info", source="supervisor")
    return True


def resume_autopilot(workspace_id: str) -> bool:
    with session_scope() as s:
        run = s.scalar(
            select(AutopilotRun)
            .where(AutopilotRun.workspace_id == workspace_id)
            .where(AutopilotRun.state == AutopilotState.PAUSED.value)
            .limit(1)
        )
        if not run:
            return False
        run.state = AutopilotState.RUNNING.value
    record_event(workspace_id, "autopilot.resumed", "Autopilot resumed", level="info", source="supervisor")
    return True


def run_single_cycle(workspace_id: str) -> dict:
    return start_autopilot(workspace_id, mode="SINGLE_CYCLE", cycles_target=1)


__all__ = [
    "get_autopilot_status",
    "pause_autopilot",
    "recover_stale_cycles",
    "resume_autopilot",
    "run_single_cycle",
    "start_autopilot",
    "stop_autopilot",
]

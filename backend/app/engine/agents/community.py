"""Community Manager agent (Work 09, Lane C).

Orchestration for the unified inbox — ``classify → moderate → prioritize →
draft → gate → queue/draft per autonomy``:

* **classify**  ``community.classify`` — deterministic multi-label with
  provider/confidence/evidence provenance (advisory semantic assist never
  overrides the rules, never infers sensitive traits);
* **moderate**  ``community.moderation`` — uncertain semantic input can only
  ask for REVIEW, never a destructive verdict;
* **prioritize**  deterministic priority/intent per label set;
* **insight / opportunity**  evidence-backed audience signals (low confidence
  below the threshold, always labelled as such);
* **draft / gate**  ``community.policy.plan_reply`` — the ONLY send path: the
  workspace autonomy mode, the hard-bypass escalation gate, brand forbidden
  phrases and the volume limits are all enforced there in deterministic code.

The agent records steps and ``agent_runs`` through ``BaseAgent.execute``; it
has no code path that can bypass the policy gate.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.engine.agents.base import AgentMeta, BaseAgent
from app.engine.community.classify import priority_for
from app.models.community import SocialInteraction
from app.services import jobs as jobs_service

CANDIDATE_STATUSES = ("unread", "read", "classified")
DRAFT_SKIP_STATUSES = ("spam", "ignored", "replied", "escalated")
MAX_BATCH = 100


class CommunityManagerAgent(BaseAgent):
    meta = AgentMeta(
        key="community_manager",
        title="Community Manager",
        description="Classifies, moderates and drafts inbox replies within autonomy limits.",
        skills=("publishing", "compliance_review"),
        tools=("publish_post", "review_compliance"),
        permissions=("publish:write", "compliance:review"),
    )

    def triage(self, ctx, *, interaction_ids: list[str] | None = None,
               limit: int = 20) -> dict[str, Any]:
        """Run the community pipeline over a bounded, workspace-scoped batch.

        ``interaction_ids`` pins the batch (foreign-workspace ids are dropped);
        otherwise the oldest unread/read/classified rows are taken, capped at
        ``limit`` (1..100).
        """
        ids = [str(i) for i in (interaction_ids or []) if str(i or "").strip()]
        limit_n = max(1, min(int(limit or 20), MAX_BATCH))

        def work() -> dict[str, Any]:
            ws = ctx.workspace_id or ""
            if not ws:
                raise ValueError("community triage requires a workspace")

            # -- 1. resolve the batch (isolation enforced here) ------------
            self.step("resolve_batch", f"{len(ids) or limit_n} candidate(s)")
            with session_scope() as s:
                if ids:
                    batch: list[str] = []
                    for interaction_id in ids:
                        row = s.get(SocialInteraction, interaction_id)
                        if row is not None and row.workspace_id == ws:
                            batch.append(interaction_id)
                else:
                    batch = list(s.scalars(
                        select(SocialInteraction.id)
                        .where(
                            SocialInteraction.workspace_id == ws,
                            SocialInteraction.status.in_(CANDIDATE_STATUSES),
                        )
                        .order_by(SocialInteraction.created_at.asc())
                        .limit(limit_n)
                    ).all())
            if not batch:
                self.step_done("ok", "no interactions to triage")
                return {
                    "summary": "no community interactions to triage",
                    "classified": 0, "moderated": 0, "drafted": 0,
                    "queued": 0, "escalated": 0, "blocked": 0, "sent": 0,
                    "insights": 0, "opportunities": 0, "priorities": {},
                }
            self.step_done("ok", f"{len(batch)} interaction(s)")

            # -- 2. classify -----------------------------------------------
            jobs_service.check_cancelled(ctx)
            self.step("classify", "deterministic multi-label + provenance")
            from app.engine.community.classify import classify_interaction

            labels_by_id: dict[str, list[str]] = {}
            classified = 0
            with session_scope() as s:
                for interaction_id in batch:
                    outcome = classify_interaction(s, ws, interaction_id)
                    if outcome.get("found"):
                        classified += 1
                        labels_by_id[interaction_id] = list(
                            outcome.get("labels") or [])
            self.step_done("ok", f"{classified} classified")

            # -- 3. moderate (needs the labels) ----------------------------
            jobs_service.check_cancelled(ctx)
            self.step("moderate", "rules first, semantic can only ask REVIEW")
            from app.engine.community.moderation import moderate_interaction

            verdicts: dict[str, int] = {}
            moderated = 0
            with session_scope() as s:
                for interaction_id in batch:
                    mod = moderate_interaction(s, ws, interaction_id)
                    if not mod.get("found"):
                        continue
                    moderated += 1
                    verdict = str(mod.get("verdict") or "")
                    verdicts[verdict] = verdicts.get(verdict, 0) + 1
            self.step_done("ok", f"{moderated} moderated ({verdicts})")

            # -- 4. prioritize + audience signals --------------------------
            jobs_service.check_cancelled(ctx)
            self.step("prioritize", "priority/intent + insights + opportunities")
            from app.engine.community.insight import record_insight
            from app.engine.community.opportunity import detect_for_interaction

            priorities: dict[str, int] = {}
            insights = opportunities = 0
            with session_scope() as s:
                for interaction_id in batch:
                    row = s.get(SocialInteraction, interaction_id)
                    if row is None or row.workspace_id != ws:
                        continue
                    labels = labels_by_id.get(interaction_id) or []
                    priority = priority_for(labels)
                    priorities[priority] = priorities.get(priority, 0) + 1
                    if ("QUESTION" in labels or row.is_question) and \
                            record_insight(s, ws, row) is not None:
                        insights += 1
                    if detect_for_interaction(s, ws, row, labels=labels) is not None:
                        opportunities += 1
            self.step_done("ok", f"{insights} insight(s), {opportunities} opportunity/ies")

            # -- 5. draft → gate → queue/draft per autonomy -----------------
            jobs_service.check_cancelled(ctx)
            self.step("draft_gate", "plan_reply under the autonomy mode")
            from app.engine.community.policy import plan_reply

            tallies = {"drafted": 0, "queued": 0, "escalated": 0,
                       "blocked": 0, "sent": 0, "disabled": 0, "skipped": 0}
            with session_scope() as s:
                for interaction_id in batch:
                    row = s.get(SocialInteraction, interaction_id)
                    if (row is None or row.workspace_id != ws
                            or row.status in DRAFT_SKIP_STATUSES
                            or row.moderation_state == "blocked"):
                        tallies["skipped"] += 1
                        continue
                    action = plan_reply(s, ws, interaction_id, auto=True)
                    if action is None:
                        tallies["disabled"] += 1
                    elif action.action_type == "ESCALATE":
                        tallies["escalated"] += 1
                    elif action.state in ("pending_approval", "approved"):
                        tallies["queued"] += 1
                    elif action.state == "sent":
                        tallies["sent"] += 1
                    elif action.state == "blocked":
                        tallies["blocked"] += 1
                    else:
                        tallies["drafted"] += 1
            self.step_done(
                "ok",
                f"{tallies['drafted']} draft(s), {tallies['queued']} queued, "
                f"{tallies['sent']} sent, {tallies['escalated']} escalated",
            )

            summary = (
                f"triaged {len(batch)} interaction(s): {classified} classified, "
                f"{moderated} moderated, {tallies['drafted']} draft(s), "
                f"{tallies['queued']} queued, {tallies['sent']} auto-sent, "
                f"{tallies['escalated']} escalated"
            )
            self.announce(ws, summary, level="info",
                          classified=classified, moderated=moderated,
                          **tallies)
            return {
                "summary": summary,
                "batch": list(batch),
                "classified": classified,
                "moderated": moderated,
                "verdicts": verdicts,
                "priorities": priorities,
                "insights": insights,
                "opportunities": opportunities,
                **tallies,
            }

        input_summary = (f"{len(ids)} pinned id(s)" if ids
                         else f"up to {limit_n} pending interaction(s)")
        return self.execute(ctx, "community_triage",
                            input_summary=input_summary, fn=work)


__all__ = ["CommunityManagerAgent"]

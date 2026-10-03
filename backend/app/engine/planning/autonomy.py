"""Work 15 §6 — autonomy modes, and the wall between planning and publishing.

    ``DISABLED``    no planning. Signals are still ingested and shown.
    ``RECOMMEND``   suggestions only. Nothing is persisted as a plan item
                    without a human turning it into one.
    ``APPROVAL``    the planner MAY create plan items and campaign drafts, but
                    every one needs a human decision before production or
                    scheduling.
    ``AUTONOMOUS``  may advance EXPLICITLY ALLOWED low-risk workflows inside
                    budget and policy.

**The invariant this module exists to enforce:** planning autonomy never
implies publishing autonomy. A plan can reach ``SCHEDULED`` under AUTONOMOUS and
still require the existing publication approval path to actually publish. There
is no mode, flag, or argument in this codebase that lets the planner write a
``PUBLISHED`` status or call a publisher.

:func:`assert_may_advance` is the single gate. Every state change in the engine
goes through it, so widening autonomy is one auditable place, not a search for
``if autonomous`` across the planner.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "AutonomyPolicy",
    "AutonomyMode",
    "PlanningAction",
    "assert_may_advance",
    "describe_autonomy",
]


class AutonomyMode(StrEnum):
    DISABLED = "DISABLED"
    RECOMMEND = "RECOMMEND"
    APPROVAL = "APPROVAL"
    AUTONOMOUS = "AUTONOMOUS"

    def __str__(self) -> str:
        return self.value

    @property
    def rank(self) -> int:
        return {"DISABLED": 0, "RECOMMEND": 1, "APPROVAL": 2,
                "AUTONOMOUS": 3}[self.value]


class PlanningAction(StrEnum):
    """Every action the planner can take, each with its own gate."""

    SUGGEST = "SUGGEST"              # recommend only
    CREATE_PLAN_ITEM = "CREATE_PLAN_ITEM"
    CREATE_CAMPAIGN_DRAFT = "CREATE_CAMPAIGN_DRAFT"
    START_RESEARCH = "START_RESEARCH"
    SCHEDULE = "SCHEDULE"
    ADVANCE_PRODUCTION = "ADVANCE_PRODUCTION"
    #: Always refused here. Publishing goes through the publication approval
    #: path; the planner has no route to it.
    PUBLISH = "PUBLISH"


#: Actions the planner may NEVER take, at any autonomy level. The planner
#: proposes; the publication pipeline decides.
NEVER_ALLOWED: frozenset[PlanningAction] = frozenset({PlanningAction.PUBLISH})

#: Minimum mode required per action. Declared before the policy class because
#: ``AutonomyPolicy`` reads it.
#:
#: Note ``CREATE_PLAN_ITEM`` sits at APPROVAL, not RECOMMEND: the work order
#: defines RECOMMEND as "planning suggestions only" and APPROVAL as "may create
#: plan/campaign drafts". A persisted plan item is a commitment-shaped record,
#: so it is not something RECOMMEND may write.
#:
#: ``SUGGEST`` requires DISABLED, and DISABLED's rank is 0, so the comparison
#: means "anything at or above DISABLED may suggest" -- true for every mode.
#: That is deliberate: at DISABLED the engine still computes and returns
#: suggestions, it simply persists nothing. SUGGEST is the one action with no
#: gate above it, because producing a read-only idea is not an action on state.
_REQUIRED: dict[PlanningAction, AutonomyMode] = {
    PlanningAction.SUGGEST: AutonomyMode.DISABLED,
    PlanningAction.CREATE_PLAN_ITEM: AutonomyMode.APPROVAL,
    PlanningAction.CREATE_CAMPAIGN_DRAFT: AutonomyMode.APPROVAL,
    PlanningAction.START_RESEARCH: AutonomyMode.AUTONOMOUS,
    PlanningAction.SCHEDULE: AutonomyMode.AUTONOMOUS,
    PlanningAction.ADVANCE_PRODUCTION: AutonomyMode.AUTONOMOUS,
    PlanningAction.PUBLISH: AutonomyMode.AUTONOMOUS,
}


@dataclass
class AutonomyPolicy:
    """The resolved policy for one plan."""

    mode: AutonomyMode = AutonomyMode.RECOMMEND
    #: explicit low-risk workflow allowlist, honoured under AUTONOMOUS
    allowed_actions: frozenset[PlanningAction] = field(default_factory=frozenset)
    #: hard daily spend ceiling for autonomous actions
    max_daily_spend_usd: float = 0.0
    #: require a human decision on every campaign draft regardless of mode
    require_approval_on_drafts: bool = True

    def resolved(self) -> AutonomyPolicy:
        """AUTONOMOUS without an allowlist gets NO autonomous actions.

        Defaulting to "everything" would make turning on autonomy a
        foot-gun; the operator must name what may run unattended.
        """
        if self.mode is AutonomyMode.AUTONOMOUS and not self.allowed_actions:
            return AutonomyPolicy(
                mode=AutonomyMode.AUTONOMOUS,
                allowed_actions=frozenset(), max_daily_spend_usd=0.0)
        return self

    def permits(self, action: PlanningAction) -> bool:
        return _REQUIRED[action].rank <= self.mode.rank

    def requires_approval(self, action: PlanningAction) -> bool:
        """True when a human must decide before the action may complete."""
        if self.require_approval_on_drafts and action in (
                PlanningAction.CREATE_CAMPAIGN_DRAFT,
                PlanningAction.ADVANCE_PRODUCTION,
                PlanningAction.SCHEDULE):
            # APPROVAL always needs a human; AUTONOMOUS needs one too unless
            # the operator explicitly allowlisted the action.
            if self.mode is AutonomyMode.APPROVAL:
                return True
            if self.mode is AutonomyMode.AUTONOMOUS:
                return action not in self.allowed_actions
            return True
        return self.mode is AutonomyMode.APPROVAL


class AutonomyRefused(RuntimeError):
    """The requested action is not permitted at this autonomy level."""


def assert_may_advance(policy: AutonomyPolicy, action: PlanningAction, *,
                       reason: str = "") -> None:
    """The single gate for every planner state change.

    Raises :class:`AutonomyRefused` for a mode that does not permit the action,
    for an action outside the AUTONOMOUS allowlist, and — unconditionally — for
    :attr:`PlanningAction.PUBLISH`.
    """
    policy = policy.resolved()
    if action in NEVER_ALLOWED:
        raise AutonomyRefused(
            f"{action} is never available to the planner: publication goes "
            f"through the existing publication approval path, and planning "
            f"autonomy must never bypass it")
    if not policy.permits(action):
        raise AutonomyRefused(
            f"{action} needs { _REQUIRED[action] } autonomy; the plan is "
            f"{policy.mode}")
    if (policy.mode is AutonomyMode.AUTONOMOUS
            and action in (PlanningAction.START_RESEARCH,
                           PlanningAction.SCHEDULE,
                           PlanningAction.ADVANCE_PRODUCTION)
            and action not in policy.allowed_actions):
        raise AutonomyRefused(
            f"{action} is not in the autonomous allowlist "
            f"{sorted(str(a) for a in policy.allowed_actions)}"
            + (f" ({reason})" if reason else ""))


def describe_autonomy() -> dict[str, dict]:
    """The policy table, as data, so the UI can render it without hardcoding.

    Every flag is the result of the FULL gate (:func:`assert_may_advance`), not
    ``permits()`` and not a hand-written rank comparison. ``permits()`` checks
    mode rank only, so using it here reported ``AUTONOMOUS publishes: true`` --
    advertising a capability the gate refuses at every level.
    """
    def allows(mode: AutonomyMode, action: PlanningAction) -> bool:
        try:
            assert_may_advance(AutonomyPolicy(mode=mode), action)
        except AutonomyRefused:
            return False
        return True

    return {
        str(mode): {
            "rank": mode.rank,
            "suggests": allows(mode, PlanningAction.SUGGEST),
            "creates_plan_items": allows(mode, PlanningAction.CREATE_PLAN_ITEM),
            "creates_campaign_drafts": allows(
                mode, PlanningAction.CREATE_CAMPAIGN_DRAFT),
            "starts_research": allows(mode, PlanningAction.START_RESEARCH),
            "schedules": allows(mode, PlanningAction.SCHEDULE),
            "advances_production": allows(
                mode, PlanningAction.ADVANCE_PRODUCTION),
            "publishes": allows(mode, PlanningAction.PUBLISH),
            "note": ("publication always requires the existing approval path; "
                     "no planning mode can publish"),
        }
        for mode in AutonomyMode
    }

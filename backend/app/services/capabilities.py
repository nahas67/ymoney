"""Canonical capability contract (Work 16.5.2 §2).

WHY THIS EXISTS
---------------
The UI needs to know what the signed-in operator may attempt, so it can hide or
explain an action instead of letting them click into a 403. The backend already
knows this -- every route declares a minimum role through
``require_workspace_role(...)`` -- but that fact was never available to the
client: ``GET /auth/me`` returned only ``id``, ``email``, ``display_name``,
``is_superuser`` and ``workspaces[{id, name, slug}]``.

The naive fix is a frontend role table. That is wrong twice over: it would
duplicate a security decision outside the system that enforces it, and the two
copies would drift. So the table lives HERE, next to the enforcement, and the
client is told what this module computes.

WHAT A CAPABILITY IS NOT
------------------------
A capability is an AFFORDANCE, not a permission check. It says "a member can
attempt this". It does not say the attempt will succeed: project-scoped rules
(``assert_capability`` on ``ProjectMember``) are evaluated separately and are
not represented here, because a workspace-level role cannot honestly describe
them. **The server remains authoritative.** A hidden button is never security --
it is a courtesy that prevents a pointless round trip.

HOW THE TABLE IS MAINTAINED
---------------------------
``_MINIMUM_ROLE`` below is transcribed from the thresholds the routers already
declare. If a route's ``require_workspace_role(...)`` changes, the matching entry
must change with it; :func:`audit_capability_thresholds` exists to make a silent
mismatch visible rather than assumed away.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

#: Mirrors ``WorkspaceMember.ROLES``. Ordered least -> most privileged.
ROLE_VIEWER = "viewer"
ROLE_MEMBER = "member"
ROLE_ADMIN = "admin"
ROLE_OWNER = "owner"

#: The single source of ranking truth. ``auth_service`` previously repeated this
#: dict in two functions; it now imports from here so the order that decides a
#: 403 and the order that decides a capability cannot diverge.
ROLE_ORDER: dict[str, int] = {
    ROLE_VIEWER: 0,
    ROLE_MEMBER: 1,
    ROLE_ADMIN: 2,
    ROLE_OWNER: 3,
}

#: capability -> minimum workspace role required to attempt it.
#:
#: Transcribed from the routers (Work 16.5.2 audit):
#:   reads / dashboards          -> viewer   (the floor on ~198 routes)
#:   content + review mutation   -> member   (content.py 18, reviews.py 7)
#:   campaign / publish mutation -> admin    (campaigns.py: all 8 mutations)
#:   brand management            -> admin    (brands.py 2)
_MINIMUM_ROLE: dict[str, str] = {
    "content.read": ROLE_VIEWER,
    "content.write": ROLE_MEMBER,
    "reviews.approve": ROLE_MEMBER,
    "publish.approve": ROLE_ADMIN,
    "publish.execute": ROLE_ADMIN,
    "brand.manage": ROLE_ADMIN,
    "providers.manage": ROLE_ADMIN,
    "operations.view": ROLE_VIEWER,
}

#: Every capability the system knows, in a stable order for UI rendering.
ALL_CAPABILITIES: tuple[str, ...] = tuple(_MINIMUM_ROLE)

#: Capabilities that can spend money or cause an irreversible external effect.
#: The UI uses this to refuse a generic "Retry" on a paid operation: retrying an
#: ambiguous purchase is how a SUBMISSION_UNKNOWN becomes a double charge.
PAID_CAPABILITIES: frozenset[str] = frozenset({"publish.execute"})


def role_rank(role: str | None) -> int:
    """Rank of ``role``; unknown roles rank below ``viewer`` rather than raising.

    Deliberately NOT case-folded. ``auth_service.require_workspace_role`` looks
    the stored role up in ``ROLE_ORDER`` verbatim, so normalising case here would
    let an ``"OWNER"`` row advertise owner capabilities in the UI while the very
    same request is rejected by the route that enforces it. Matching the
    enforcement table exactly is worth more than being forgiving.
    """
    if not role:
        return -1
    return ROLE_ORDER.get(str(role).strip(), -1)


def capabilities_for_role(role: str | None) -> list[str]:
    """Capabilities a holder of ``role`` may attempt, sorted least-privileged first."""
    rank = role_rank(role)
    if rank < 0:
        return []
    return [cap for cap in ALL_CAPABILITIES if rank >= role_rank(_MINIMUM_ROLE[cap])]


def minimum_role_for(capability: str) -> str | None:
    """The minimum role a capability requires, or ``None`` if unknown."""
    return _MINIMUM_ROLE.get(capability)


def is_paid_capability(capability: str) -> bool:
    return capability in PAID_CAPABILITIES


def audit_capability_thresholds(declared: dict[str, int]) -> list[str]:
    """Compare counted ``require_workspace_role`` thresholds with this table.

    ``declared`` maps role -> number of routes declaring it. This does not prove
    per-route agreement (that would need every route enumerated), but it fails
    loudly if a role tier disappears entirely -- which is what a refactor that
    moved enforcement elsewhere looks like from here.
    """
    problems: list[str] = []
    for role in ROLE_ORDER:
        if declared.get(role, 0) == 0 and role != ROLE_OWNER:
            problems.append(
                f"no route declares the {role!r} tier any more; "
                f"capability thresholds may be stale"
            )
    return problems


class WorkspaceCapability(BaseModel):
    """One workspace the caller belongs to, with what membership there allows."""

    id: str
    name: str
    slug: str | None = None
    #: Raw ``WorkspaceMember.role``. The UI must not branch on this string --
    #: it branches on ``capabilities``, which is derived and ordered.
    role: str
    capabilities: list[str] = Field(default_factory=list)


class MeResponse(BaseModel):
    """``GET /auth/me``.

    Additive over the previous payload: ``workspaces[]`` keeps ``id``, ``name``
    and ``slug``, and gains ``role`` and ``capabilities``. Existing clients keep
    working; the UI gains the ability to explain an action it must not offer.
    """

    id: str
    email: str
    display_name: str | None = None
    is_superuser: bool = False
    workspaces: list[WorkspaceCapability] = Field(default_factory=list)
    #: Flat union across every workspace, for callers that only need "can this
    #: user do anything requiring elevation". NOT a substitute for the
    #: per-workspace list when acting inside one workspace.
    capabilities: list[str] = Field(default_factory=list)
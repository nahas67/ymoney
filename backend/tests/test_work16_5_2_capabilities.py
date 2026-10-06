"""Canonical capability contract (Work 16.5.2 §2).

The UI is only allowed to hide an action because the BACKEND said it cannot.
These tests exist so that claim stays true:

* the derivation is monotonic -- more role never means fewer capabilities;
* an unknown or missing role grants nothing rather than everything;
* `/auth/me` reports what the module computes, per workspace, not a union;
* the paid flag exists, because a generic retry on an ambiguous purchase is how a
  SUBMISSION_UNKNOWN becomes a double charge.

The deliberate non-test: a capability is NOT an authorization check. Nothing here
asserts that possessing a capability lets a request through -- the route's
``require_workspace_role`` does that, and these tests must not be mistaken for
replacing it.
"""

from __future__ import annotations

from app.models import WorkspaceMember
from app.services.capabilities import (
    ALL_CAPABILITIES,
    PAID_CAPABILITIES,
    MeResponse,
    ROLE_ORDER,
    WorkspaceCapability,
    audit_capability_thresholds,
    capabilities_for_role,
    is_paid_capability,
    minimum_role_for,
    role_rank,
)

REQUESTED_VOCABULARY = {
    "content.read",
    "content.write",
    "publish.approve",
    "publish.execute",
    "providers.manage",
    "operations.view",
    "brand.manage",
    "reviews.approve",
}


class TestVocabulary:
    def test_declares_exactly_the_agreed_vocabulary(self):
        assert set(ALL_CAPABILITIES) == REQUESTED_VOCABULARY

    def test_every_capability_has_a_minimum_role(self):
        for cap in ALL_CAPABILITIES:
            assert minimum_role_for(cap) in ROLE_ORDER, cap

    def test_reads_are_never_more_privileged_than_writes(self):
        assert role_rank(minimum_role_for("content.read")) <= role_rank(
            minimum_role_for("content.write")
        )
        # Publishing mutates money/reach, so it can never sit below plain writing.
        assert role_rank(minimum_role_for("content.write")) <= role_rank(
            minimum_role_for("publish.approve")
        )


class TestDerivation:
    def test_viewer_can_only_read(self):
        caps = capabilities_for_role(WorkspaceMember.ROLE_VIEWER)
        assert set(caps) == {"content.read", "operations.view"}

    def test_member_adds_write_and_review_approval(self):
        caps = set(capabilities_for_role(WorkspaceMember.ROLE_MEMBER))
        assert {"content.write", "reviews.approve"} <= caps
        # Member still must not publish or manage providers.
        assert "publish.execute" not in caps
        assert "providers.manage" not in caps

    def test_admin_gains_everything(self):
        assert set(capabilities_for_role(WorkspaceMember.ROLE_ADMIN)) == set(ALL_CAPABILITIES)

    def test_owner_is_not_a_superset_of_a_new_admin_tier(self):
        # Owner and admin are currently equal by design. If a capability is ever
        # added ABOVE admin, this fails and forces a decision rather than
        # silently making every admin unable to use a feature.
        owner = set(capabilities_for_role(WorkspaceMember.ROLE_OWNER))
        admin = set(capabilities_for_role(WorkspaceMember.ROLE_ADMIN))
        assert owner == admin, "a capability now requires a tier above admin"

    def test_derivation_is_monotonic(self):
        ranks = sorted(ROLE_ORDER, key=role_rank)
        seen: set[str] = set()
        for role in ranks:
            caps = set(capabilities_for_role(role))
            assert seen <= caps, f"{role} lost capabilities granted to a lower role"
            seen = caps

    def test_unknown_role_grants_nothing(self):
        # Fails CLOSED. A corrupt stored role must not become an owner.
        assert capabilities_for_role("superuser") == []
        assert capabilities_for_role(None) == []
        assert capabilities_for_role("") == []
        assert role_rank("nonsense") == -1

    def test_role_matching_is_exactly_as_strict_as_enforcement(self):
        # auth_service looks the stored role up verbatim. Case-folding here would
        # advertise owner capabilities for an "OWNER" row that the route guard
        # then rejects -- UI and enforcement disagreeing about the same user.
        assert capabilities_for_role("OWNER") == []
        assert capabilities_for_role("Admin") == []
        assert capabilities_for_role(" admin ") == capabilities_for_role("admin")

    def test_capabilities_are_listed_in_a_stable_order(self):
        # Least-privileged first, so a UI list does not reshuffle between loads.
        for _ in range(3):
            assert capabilities_for_role("admin") == list(ALL_CAPABILITIES)


class TestPaidCapability:
    def test_publish_execute_is_flagged_paid(self):
        assert is_paid_capability("publish.execute")
        assert "publish.execute" in PAID_CAPABILITIES

    def test_read_only_capabilities_are_not_paid(self):
        assert not is_paid_capability("content.read")
        assert not is_paid_capability("operations.view")


class TestThresholdAudit:
    def test_reports_the_role_tier_that_vanished(self):
        # `admin` is absent here, which is what a refactor that moved
        # enforcement elsewhere would look like from here.
        problems = audit_capability_thresholds({"viewer": 198, "member": 109})
        assert any("admin" in p for p in problems)
        assert not any("viewer" in p for p in problems)

    def test_healthy_declaration_is_quiet(self):
        assert audit_capability_thresholds({"viewer": 198, "member": 109, "admin": 67}) == []


class TestMeResponseShape:
    def test_workspace_entry_carries_role_and_capabilities(self):
        ws = WorkspaceCapability(
            id="w1", name="W", slug="w", role="admin", capabilities=["content.read"]
        )
        dumped = ws.model_dump()
        assert dumped["role"] == "admin"
        assert dumped["capabilities"] == ["content.read"]

    def test_capabilities_default_to_empty_not_null(self):
        # An omitted list must serialise as [] so the client can tell "none" from
        # "field absent" -- the distinction the fail-open rule depends on.
        ws = WorkspaceCapability(id="w1", name="W", role="viewer")
        assert ws.model_dump()["capabilities"] == []

    def test_me_response_is_additive_over_the_old_payload(self):
        # id/email/display_name/is_superuser/workspaces must all survive, or the
        # existing Editor/Exports/Reviews callers break.
        fields = set(MeResponse.model_fields)
        assert {"id", "email", "display_name", "is_superuser", "workspaces"} <= fields
        assert "capabilities" in fields

"""Put a second user into an EXISTING workspace at a given role.

WHY THIS EXISTS
---------------
There is no API for it. `POST /auth/register` creates a brand-new workspace owned
by the new user (`api/v1/workspaces.py:81`, `api/v1/auth.py:57`), so a second
registered user lands in a DIFFERENT workspace and every call answers 403
"not a workspace member" -- which proves isolation, not the role ladder.

The role lives in one column: `workspace_members.role`, ranked by
`services/capabilities.py::ROLE_ORDER`. Every existing backend test establishes it
by writing that row directly (`tests/conftest.py:36-47`, `test_inbox_api.py:68`,
`test_exports.py:90`, ...). This is the same technique, callable from a browser
test, so the permissions E2E can exercise viewer/member/admin inside ONE workspace.

It writes NOTHING but that one membership row, and it refuses any role outside the
backend's own vocabulary rather than inventing one.

    python scripts/attach_workspace_member.py <workspace_id> <email> <role>
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "backend"))

VALID_ROLES = ("viewer", "member", "admin", "owner")


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print(__doc__)
        print(f"usage: attach_workspace_member.py <workspace_id> <email> <role>")
        return 2
    workspace_id, email, role = argv[1], argv[2], argv[3].strip().lower()

    # Refuse an invented role. `require_workspace_role` reads `order[member.role]`
    # with no `.get`, so a role string outside ROLE_ORDER raises KeyError -> 500.
    # Better to fail here, loudly, than to hand the suite a broken identity.
    if role not in VALID_ROLES:
        print(f"role must be one of {VALID_ROLES}, got {role!r}")
        return 2

    from app.db import SessionLocal
    from app.models import User, WorkspaceMember
    from sqlalchemy import select

    db = SessionLocal()
    try:
        user = db.scalar(select(User).where(User.email == email))
        if user is None:
            print(f"no user with email {email!r}")
            return 1
        existing = db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id,
                WorkspaceMember.user_id == user.id,
            )
        )
        if existing is None:
            db.add(
                WorkspaceMember(
                    workspace_id=workspace_id, user_id=user.id, role=role
                )
            )
        else:
            existing.role = role
        db.commit()
        print(f"{email} -> {workspace_id} as {role}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
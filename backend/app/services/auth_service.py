"""Authentication & authorization service."""

from __future__ import annotations

import re
from datetime import timedelta

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.security import (
    create_access_token,
    generate_refresh_token,
    hash_password,
    hash_token,
    verify_password,
)
from app.db import get_db
from app.models import RefreshToken, User, Workspace, WorkspaceMember
from app.models.base import utcnow

_bearer = HTTPBearer(auto_error=False)
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(HTTPException):
    def __init__(self, detail: str, code: int = 401):
        super().__init__(status_code=code, detail=detail)


def validate_email(email: str) -> str:
    email = (email or "").strip().lower()
    if not _EMAIL_RE.match(email) or len(email) > 320:
        raise AuthError("invalid email address", 400)
    return email


def register_user(db: Session, email: str, password: str, display_name: str = "") -> User:
    email = validate_email(email)
    if len(password or "") < 10:
        raise AuthError("password must be at least 10 characters", 400)
    if db.scalar(select(User).where(User.email == email)):
        raise AuthError("email already registered", 409)
    user = User(
        email=email,
        password_hash=hash_password(password),
        display_name=(display_name or email.split("@")[0])[:120],
    )
    db.add(user)
    db.flush()
    return user


def authenticate(db: Session, email: str, password: str) -> User | None:
    email = validate_email(email)
    user = db.scalar(select(User).where(User.email == email))
    if not user or not user.is_active:
        return None
    if not verify_password(password or "", user.password_hash):
        return None
    return user


def issue_tokens(db: Session, user: User, user_agent: str = "") -> dict:
    access = create_access_token(user.id)
    raw, hashed = generate_refresh_token()
    expires = utcnow() + timedelta(days=14)
    row = RefreshToken(
        user_id=user.id, token_hash=hashed, expires_at=expires, user_agent=user_agent[:300]
    )
    db.add(row)
    db.flush()
    return {
        "access_token": access,
        "refresh_token": raw,
        "token_type": "bearer",
        "expires_in": 120 * 60,
    }


def rotate_refresh_token(db: Session, raw_token: str) -> dict | None:
    """Atomically consume a refresh token before issuing its replacement.

    A read-then-set sequence lets two concurrent refresh requests observe the
    same live token and both mint sessions. The conditional UPDATE is the
    single-use claim: exactly one request can change ``revoked`` from false to
    true; losers return ``None`` and the route responds with 401.
    """
    hashed = hash_token(raw_token)
    now = utcnow()
    row = db.scalar(select(RefreshToken).where(RefreshToken.token_hash == hashed))
    if not row or row.revoked or row.expires_at < now:
        return None

    claimed = db.execute(
        update(RefreshToken)
        .where(
            RefreshToken.id == row.id,
            RefreshToken.token_hash == hashed,
            RefreshToken.revoked.is_(False),
            RefreshToken.expires_at >= now,
        )
        .values(revoked=True)
    )
    if claimed.rowcount != 1:
        return None

    user = db.get(User, row.user_id)
    if not user or not user.is_active:
        return None
    return issue_tokens(db, user, row.user_agent)


def revoke_all_sessions(db: Session, user_id: str) -> int:
    rows = db.scalars(select(RefreshToken).where(RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False))).all()
    for r in rows:
        r.revoked = True
    return len(rows)


# ---------------------------------------------------------------------------
# FastAPI dependencies
# ---------------------------------------------------------------------------


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(_bearer),
    db: Session = Depends(get_db),
) -> User:
    from app.core.security import decode_access_token

    if credentials is None:
        raise AuthError("not authenticated")
    token = credentials.credentials

    payload = decode_access_token(token)
    if payload:
        user = db.get(User, payload.get("sub", ""))
        if user and user.is_active:
            return user

    raise AuthError("invalid or expired token")


def require_workspace_role(minimum_role: str):
    order = {WorkspaceMember.ROLE_VIEWER: 0, WorkspaceMember.ROLE_MEMBER: 1, WorkspaceMember.ROLE_ADMIN: 2, WorkspaceMember.ROLE_OWNER: 3}

    def dependency(
        workspace_id: str,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db),
    ) -> Workspace:
        ws = db.get(Workspace, workspace_id)
        if not ws:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="workspace not found")
        member = db.scalar(
            select(WorkspaceMember).where(
                WorkspaceMember.workspace_id == workspace_id, WorkspaceMember.user_id == user.id
            )
        )
        if member is None and not user.is_superuser:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="not a workspace member")
        if member is not None and minimum_role and order[member.role] < order[minimum_role]:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="insufficient role")
        return ws

    return dependency

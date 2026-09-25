"""Append-only evidence ledger with sha256 hash chain + workspace isolation."""

from __future__ import annotations

import hashlib
import json

from sqlalchemy import select

GENESIS_DIGEST = "GENESIS"


def _canonical(payload: dict) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)


def compute_digest(prev_digest: str, workspace_id: str, kind: str, subject_id: str,
                   execution_status: str, verification_status: str,
                   checks: list[dict]) -> str:
    body = _canonical({
        "prev": prev_digest or GENESIS_DIGEST,
        "workspace_id": workspace_id,
        "kind": kind,
        "subject_id": subject_id,
        "execution_status": execution_status,
        "verification_status": verification_status,
        "checks": checks,
    })
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def last_digest(session, workspace_id: str) -> str:
    """Latest digest for a workspace (GENESIS when the ledger is empty)."""
    from app.models.intelligence import EvidenceRecord

    row = session.scalars(
        select(EvidenceRecord)
        .where(EvidenceRecord.workspace_id == workspace_id)
        .order_by(EvidenceRecord.created_at.desc(), EvidenceRecord.id.desc())
        .limit(1)
    ).first()
    return row.digest if row is not None else GENESIS_DIGEST


def append_evidence(session, *, workspace_id: str, kind: str, subject_id: str,
                    execution_status: str, verification_status: str,
                    checks: list[dict]):
    """Append one record. No update/delete API exists by design (append-only)."""
    from app.models.intelligence import EvidenceRecord

    prev = last_digest(session, workspace_id)
    digest = compute_digest(prev, workspace_id, kind, subject_id,
                            execution_status, verification_status, checks)
    row = EvidenceRecord(
        workspace_id=workspace_id,
        kind=kind,
        subject_id=subject_id,
        execution_status=execution_status,
        verification_status=verification_status,
        checks_json=list(checks or []),
        digest=digest,
        prev_digest=prev,
    )
    session.add(row)
    session.commit()
    return row


def verify_chain(session, workspace_id: str) -> dict:
    """Recompute the workspace chain; returns {ok, count, broken_at}."""
    from app.models.intelligence import EvidenceRecord

    rows = session.scalars(
        select(EvidenceRecord)
        .where(EvidenceRecord.workspace_id == workspace_id)
        .order_by(EvidenceRecord.created_at.asc(), EvidenceRecord.id.asc())
    ).all()
    prev = GENESIS_DIGEST
    for row in rows:
        expected = compute_digest(prev, row.workspace_id, row.kind, row.subject_id,
                                  row.execution_status, row.verification_status,
                                  row.checks_json or [])
        if row.prev_digest != prev or row.digest != expected:
            return {"ok": False, "count": len(rows), "broken_at": row.id}
        prev = row.digest
    return {"ok": True, "count": len(rows), "broken_at": None}


def list_records(session, workspace_id: str, *, kind: str | None = None,
                 status: str | None = None, limit: int = 100) -> list:
    """Workspace-isolated listing with kind/status filters (404 upstream on
    cross-workspace access — callers scope by the authenticated workspace)."""
    from app.models.intelligence import EvidenceRecord

    stmt = (
        select(EvidenceRecord)
        .where(EvidenceRecord.workspace_id == workspace_id)
        .order_by(EvidenceRecord.created_at.desc(), EvidenceRecord.id.desc())
        .limit(max(1, min(limit, 500)))
    )
    if kind:
        stmt = stmt.where(EvidenceRecord.kind == kind)
    if status:
        stmt = stmt.where(EvidenceRecord.verification_status == status)
    return list(session.scalars(stmt).all())

"""Content graph: parent/child lineage over ContentItem rows.

Every derived asset (short, variant, platform cut, localization, repurpose)
points at its parent and at the root; the root is the master content the
campaign measures. Same-workspace enforced — lineage never crosses tenants.
"""

from __future__ import annotations

DERIVATION_TYPES = ("short", "variant", "localized", "platform_cut",
                    "repurpose", "translation", "other")


class LineageError(ValueError):
    pass


def derive_content(session, *, parent_id: str, workspace_id: str,
                   derivation_type: str, topic: str | None = None,
                   campaign_id: str | None = None) -> object:
    """Create a derived ContentItem. Returns the child row."""
    from app.models import ContentItem

    if derivation_type not in DERIVATION_TYPES:
        raise LineageError(f"unknown derivation_type '{derivation_type}'")
    parent = session.get(ContentItem, parent_id)
    if parent is None or parent.workspace_id != workspace_id:
        raise LineageError(f"parent content '{parent_id}' not found")
    siblings = sum(1 for _ in session.query(ContentItem).filter(
        ContentItem.parent_content_id == parent_id).all())
    child = ContentItem(
        workspace_id=workspace_id,
        campaign_id=campaign_id or parent.campaign_id,
        opportunity_id=parent.opportunity_id,
        topic=(topic or f"{parent.topic} ({derivation_type})")[:400],
        status="IDEA",
        parent_content_id=parent.id,
        root_content_id=parent.root_content_id or parent.id,
        derivation_type=derivation_type,
        # version = position among siblings (1-based), stable under re-query
        lineage_version=siblings + 1,
        tags_json=list(parent.tags_json or []),
    )
    session.add(child)
    session.flush()
    return child


def lineage_chain(session, content_id: str, *, workspace_id: str) -> dict:
    """Root → … → parent → self, plus direct children. All workspace-scoped."""
    from app.models import ContentItem

    row = session.get(ContentItem, content_id)
    if row is None or row.workspace_id != workspace_id:
        raise LineageError(f"content '{content_id}' not found")
    ancestors: list[dict] = []
    cursor = row
    seen = set()
    while cursor.parent_content_id and cursor.parent_content_id not in seen:
        seen.add(cursor.parent_content_id)
        parent = session.get(ContentItem, cursor.parent_content_id)
        if parent is None or parent.workspace_id != workspace_id:
            break
        ancestors.append(_node(parent))
        cursor = parent
    ancestors.reverse()
    children = session.query(ContentItem).filter(
        ContentItem.parent_content_id == row.id,
        ContentItem.workspace_id == workspace_id,
    ).order_by(ContentItem.lineage_version).all()
    return {"self": _node(row),
            "root_id": row.root_content_id or row.id,
            "ancestors": ancestors,
            "children": [_node(c) for c in children]}


def _node(row) -> dict:
    return {"id": row.id, "topic": row.topic, "status": row.status,
            "derivation_type": row.derivation_type,
            "lineage_version": row.lineage_version,
            "campaign_id": row.campaign_id}

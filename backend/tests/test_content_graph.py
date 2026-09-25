"""Work 01: content lineage — derive, chain, isolation, legacy rows."""
from __future__ import annotations

import uuid


def _ws(session):
    from app.models import Workspace

    ws = Workspace(name="G WS", slug=f"g-{uuid.uuid4().hex[:8]}", niche="t")
    session.add(ws)
    session.flush()
    return ws.id


def test_derive_and_chain():
    from app.db import session_scope
    from app.engine.content_graph import derive_content, lineage_chain
    from app.models import ContentItem

    with session_scope() as s:
        ws_id = _ws(s)
        master = ContentItem(workspace_id=ws_id, topic="master video", status="PUBLISHED")
        s.add(master)
        s.flush()
        mid = master.id
        c1 = derive_content(s, parent_id=mid, workspace_id=ws_id,
                            derivation_type="short", topic="short 1")
        c2 = derive_content(s, parent_id=mid, workspace_id=ws_id,
                            derivation_type="platform_cut")
        gc = derive_content(s, parent_id=c1.id, workspace_id=ws_id,
                            derivation_type="localized")
        ids = (mid, c1.id, c2.id, gc.id)
    with session_scope() as s:
        chain = lineage_chain(s, gc.id, workspace_id=ws_id)
        assert chain["root_id"] == mid
        assert [a["id"] for a in chain["ancestors"]] == [mid, c1.id]
        assert chain["self"]["derivation_type"] == "localized"
        top = lineage_chain(s, mid, workspace_id=ws_id)
        assert top["ancestors"] == []
        assert {c["id"] for c in top["children"]} == {c1.id, c2.id}
        assert top["children"][0]["lineage_version"] == 1
        assert top["children"][1]["lineage_version"] == 2
        # every short knows its master/campaign
        for cid in ids[1:]:
            row = s.get(ContentItem, cid)
            assert row.root_content_id == mid


def test_derive_rejects_unknown_type_and_foreign_parent():
    import pytest

    from app.db import session_scope
    from app.engine.content_graph import LineageError, derive_content
    from app.models import ContentItem

    with session_scope() as s:
        ws_id = _ws(s)
        other_id = _ws(s)
        parent = ContentItem(workspace_id=other_id, topic="p", status="IDEA")
        s.add(parent)
        s.flush()
        pid = parent.id
        with pytest.raises(LineageError, match="unknown derivation_type"):
            derive_content(s, parent_id=pid, workspace_id=other_id,
                           derivation_type="teleport")
        with pytest.raises(LineageError, match="not found"):
            derive_content(s, parent_id=pid, workspace_id=ws_id,
                           derivation_type="short")  # cross-workspace denied
        with pytest.raises(LineageError, match="not found"):
            derive_content(s, parent_id="nope", workspace_id=ws_id,
                           derivation_type="short")


def test_legacy_rows_without_lineage_still_usable():
    from app.db import session_scope
    from app.engine.content_graph import lineage_chain
    from app.models import ContentItem

    with session_scope() as s:
        ws_id = _ws(s)
        legacy = ContentItem(workspace_id=ws_id, topic="pre-lineage", status="LEARNED")
        s.add(legacy)
        s.flush()
        lid = legacy.id
    with session_scope() as s:
        chain = lineage_chain(s, lid, workspace_id=ws_id)
        assert chain["root_id"] == lid  # root defaults to self
        assert chain["ancestors"] == [] and chain["children"] == []

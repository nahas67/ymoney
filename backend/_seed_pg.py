"""Scratch: seed a PostgreSQL database with canonical rows, then DRILL it."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

BACKEND = Path(r"C:\Users\nahas\OneDrive\Desktop\ymoney\backend")
sys.path.insert(0, str(BACKEND))

DSN = os.environ["SEED_DSN"]

from app.db import SessionLocal, engine, session_scope  # noqa: E402
from app.migrations.runner import run_migrations  # noqa: E402
from app.models import (  # noqa: E402
    ContentItem, LipSyncJob, StorageObject, Video, VideoVariant, Workspace,
)
from app.services.budget_rollup import set_limits  # noqa: E402
from app.services.cost import reserve_spend, settle_reservation  # noqa: E402
from app.services.paid_provider import (  # noqa: E402
    ActorAuthority, SpendAuthority, paid_operation,
)

with session_scope() as s:
    run_migrations(s)
print("migrations applied; tables:",
      len(engine.dialect.get_table_names(engine.connect())))

with session_scope() as s:
    ws = Workspace(name="Drill WS", slug="drill-ws", niche="AI money")
    s.add(ws)
    s.flush()
    payload = dict(ws.settings_json or {})
    payload["safety"] = {"daily_budget_usd": 1000.0, "per_video_budget_usd": 100.0}
    ws.settings_json = payload
    ws_id = ws.id
    root = ContentItem(workspace_id=ws_id, topic="root topic", status="READY")
    s.add(root)
    s.flush()
    child = ContentItem(workspace_id=ws_id, topic="child topic", status="READY",
                        parent_content_id=root.id, root_content_id=root.id)
    s.add(child)
    s.flush()
    grandchild = ContentItem(workspace_id=ws_id, topic="grandchild topic",
                             status="READY", parent_content_id=child.id,
                             root_content_id=root.id)
    s.add(grandchild)
    s.flush()
    obj = StorageObject(workspace_id=ws_id, object_key="renders/v1.mp4",
                        checksum="a" * 64, size_bytes=1234, kind="render",
                        state="FINALIZED", backend="local",
                        temp_path="", ref_type="content_item", ref_id=root.id,
                        finalized_at=__import__("datetime").datetime(2026, 9, 1, 12, 0, 0))
    s.add(obj)
    s.commit()
    ids = {"workspace": ws_id, "root": root.id, "child": child.id,
           "grandchild": grandchild.id, "storage_object": obj.id}

with session_scope() as s:
    variant = VideoVariant(content_item_id=ids["root"], label="v1",
                           hook="drill hook")
    s.add(variant)
    s.flush()
    s.add(Video(workspace_id=ws_id, variant_id=variant.id, engine="ffmpeg_avatar",
                status="READY", cost_outcome="UNKNOWN_EXPOSURE",
                submission_operation_id="op-drill-1", submission_state="SUBMISSION_UNKNOWN",
                provider_task_id="remote-drill-1"))
    s.add(LipSyncJob(workspace_id=ws_id, status="FAILED",
                     execution_outcome="SUBMISSION_UNKNOWN",
                     cost_outcome="UNKNOWN_EXPOSURE"))
    s.commit()

# money, through the real gates
op = paid_operation(provider="images", operation="generate", workspace_id=ws_id,
                    category="image", estimated_cost=1.25,
                    reservation_extra={"drill": True})
op.authorize()
op.mark_accepted("remote-img-1")
settle_reservation(op.entry_id, 1.10)
reserve = reserve_spend(ws_id, 0.40, category="llm", provider="openai",
                        detail={"operation_id": "op-drill-2"})
set_limits(workspace_id=ws_id, daily_total_cap=50.0, monthly_total_cap=500.0)
ids["cost_entry"] = reserve.entry_id
print(json.dumps(ids, indent=2))

"""Reproduce the GENERATOR's exact call order, then report the send action's state.

The generator observes in two passes -- unseeded, then seeded -- over the whole
inventory. Calling `POST /inbox/actions/{id}/send` on its own succeeds, so the
refusal only appears in that sequence. This runs the same sequence and then reads
the persisted row, because `provider_error` and `action is rejected` look
identical from the route's 403/409 and only the row says which happened.
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "backend"))

import ui_contract_observer as obs  # noqa: E402

SEND = "/api/v1/workspaces/{workspace_id}/inbox/actions/{action_id}/send"

client = obs.new_client()
session = obs.register(client)
inventory = obs.load_inventory()
seeded = obs.seed(client, session)
session["seeded"] = dict(seeded)

obs.observe_unseeded(client, session, inventory)
seeded_pass = obs.observe(client, session, inventory, seeded)

row = next(
    (o for o in seeded_pass if o["specPath"] == SEND),
    None,
)
print(f"\nobservation: {row['status'] if row else 'NONE'}")
print(f"error      : {(row or {}).get('error')}")
print(f"url        : {(row or {}).get('url')}")
print(f"send_action seed = {seeded.get('send_action')}")
print(f"action seed      = {seeded.get('action')}")

# Which seeded id did the send actually address, and what state is it in?
from app.db import SessionLocal  # noqa: E402
from app.models import CommunityAction  # noqa: E402
from sqlalchemy import select  # noqa: E402

db = SessionLocal()
for key in ("action", "send_action", "reject_action"):
    aid = seeded.get(key)
    if not aid:
        print(f"{key:16} MISSING from seeds")
        continue
    r = db.scalar(select(CommunityAction).where(CommunityAction.id == aid))
    if r is None:
        print(f"{key:16} {aid} -> ROW MISSING")
        continue
    print(
        f"{key:16} state={r.state!r} claimed={r.send_claimed_at!r} "
        f"remote={r.remote_reply_id!r} error={(r.error or '')[:90]!r}"
    )
db.close()

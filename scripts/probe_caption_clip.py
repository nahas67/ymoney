"""Does `add_item` on the `caption` track actually persist a clip?

The Studio caption/motion/keyframe/effect panel mounts only for a selection on
the `caption` track, and the browser suite cannot even FIND the clip block. That
is either a fixture problem (the op was silently dropped) or a real engine rule
(a caption clip needs fields a generic clip does not have). Printing the
canonical document after the op distinguishes the two.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "backend"))

import ui_contract_observer as obs  # noqa: E402

client = obs.new_client()
session = obs.register(client)
ws = session["workspace_id"]
h = session["headers"]

r = client.post(f"/api/v1/workspaces/{ws}/timelines", headers=h,
                json={"name": "probe", "fps": 30, "duration_seconds": 30})
tl = r.json()
tid = tl.get("timeline", {}).get("id") or tl.get("id")
print("timeline", tid)

for track in ("caption", "text", "video"):
    doc = client.get(f"/api/v1/workspaces/{ws}/timelines/{tid}", headers=h).json()
    clip_id = f"probe-{track}"
    res = client.post(
        f"/api/v1/workspaces/{ws}/timelines/{tid}/operations",
        headers=h,
        json={
            "base_version": doc["version"],
            "operations": [{
                "type": "add_item",
                "track": track,
                "clip": {
                    "id": clip_id,
                    "name": f"Probe {track}",
                    "start": 1.0,
                    "duration": 4.0,
                    "source": {"asset_ref": f"e2e://{track}"},
                },
            }],
        },
    )
    after = client.get(f"/api/v1/workspaces/{ws}/timelines/{tid}", headers=h).json()
    found = [
        c
        for t in after.get("tracks", [])
        if t.get("kind") == track
        for c in t.get("clips", [])
        if c.get("id") == clip_id
    ]
    print(f"\ntrack={track!r} op_status={res.status_code} persisted={len(found)}")
    if res.status_code >= 400:
        print("   detail:", res.text[:220])
    if found:
        print("   clip:", json.dumps(found[0])[:200])
    kinds = [t.get("kind") for t in after.get("tracks", [])]
    print("   tracks in doc:", kinds)

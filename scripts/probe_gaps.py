"""Why does each remaining endpoint still fail? Real URL, real request, real error.

Work 16.5.5 §1. Runs the OBSERVER's own fixture build and request derivation, so
what it prints is what generation actually sent -- not a hand-rolled probe that
can disagree with the harness.

Prints, per remaining endpoint: the derived URL, the derived body/query, and the
server's own error text. That turns "25 endpoints remain" into 25 named,
individually fixable causes instead of one opaque number.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "backend"))

import spec_request_builder as rb  # noqa: E402
import ui_contract_observer as obs  # noqa: E402

REPORT = REPO / "docs" / "UI_CONTRACT_GENERATION.json"


def main() -> int:
    data = json.loads(REPORT.read_text(encoding="utf-8"))
    remaining = data.get("gapRemainingEndpoints") or []
    if not remaining:
        print("no gap remaining")
        return 0

    client = obs.new_client()
    session = obs.register(client)
    obs.seed(client, session)
    seeded: dict[str, str] = dict(session.get("seeded", {}))
    seeded.update(
        {k: v for k, v in obs.seed(client, session).items() if not k.startswith("!")}
    )

    print("=" * 96)
    for key in remaining:
        method, path = key.split(" ", 1)
        url = rb.fill_path_params(path, session["workspace_id"], seeded, method)
        raw = obs.DOMAIN_SEED_BODIES.get(
            (path, method.lower())
        )
        body = rb.resolve_time_placeholders(
            rb.substitute_fixture_ids(
                raw if raw is not None else rb.request_body_for(path, method.lower()),
                seeded,
            )
        )
        query = rb.resolve_time_placeholders(rb.query_for(path, method.lower(), seeded))
        r = client.request(method, url, headers=session["headers"], json=body, params=query)
        short = url.replace(session["workspace_id"], "{ws}")
        print(f"\n[{r.status_code}] {method} {path}")
        print(f"    url    {short}")
        if body:
            print(f"    body   {json.dumps(body)[:160]}")
        if query:
            print(f"    query  {json.dumps(query)[:160]}")
        if r.status_code >= 400:
            print(f"    detail {r.text[:260]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
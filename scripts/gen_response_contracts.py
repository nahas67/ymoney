"""Generate response contracts for every ordinary-JSON UI endpoint (Work 16.5.4 §1).

    backend\\.venv\\Scripts\\python scripts\\gen_response_contracts.py

PIPELINE
--------
    real app + real workspace
      -> call every endpoint the UI calls (empty AND seeded state)
      -> infer a Pydantic model per endpoint
      -> write backend/app/schemas/generated.py
      -> write the (method, path) -> model table
      -> registry attaches them as real response_model values

Every stage is grounded in an actual HTTP response. Nothing here invents a field,
an enum or an envelope.

WHAT IS DELIBERATELY NOT DONE
-----------------------------
* No endpoint is called that publishes, renders, or bills. Reads only, plus the
  create calls needed to make a read non-empty.
* No schema is invented for an endpoint that returned no parseable body; those are
  reported as SKIPPED with the reason so the gap stays visible.
* No ``Dict[str, Any]`` fallback is emitted for a shaped response. A genuinely
  heterogeneous array becomes ``list`` -- honest -- rather than being dressed up.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "backend"))

import infer_response_models as infer  # noqa: E402
import ui_contract_observer as obs  # noqa: E402

GENERATED = REPO / "backend" / "app" / "schemas" / "generated.py"
MAP_PATH = REPO / "backend" / "app" / "schemas" / "contract_map.json"
REPORT = REPO / "docs" / "UI_CONTRACT_GENERATION.json"


def main() -> int:
    print("=" * 78)
    print("RESPONSE CONTRACT GENERATION (Work 16.5.4 §1)")
    print("=" * 78)

    # OBSERVE EVERYTHING, PUBLISH ONLY THE GAP.
    #
    # `load_inventory` is the full 191-call set and `load_queue` the subset still
    # lacking a contract. Generation must run over the full set because
    # `generated.py` and `contract_map.json` are both rewritten whole: feeding
    # them only the 55-call gap silently truncated 104 contracts and 145 models
    # away on the previous run. The gap set is still what decides whether an
    # inferred model is PUBLISHED -- see `--refresh` below for why.
    inventory = obs.load_inventory()
    queue = obs.load_queue()
    # Keys must be in the SAME SHAPE as the map's keys ("METHOD /path"), not
    # (method, path) tuples. Comparing the two forms yields an empty
    # intersection, which silently reports every newly-observed endpoint as gap
    # work already done -- the arithmetic is only as honest as its key space.
    gap_keys = {f"{c['method']} {c['specPath']}" for c in queue}
    print(
        f"\ninventory: {len(inventory)} ordinary-JSON UI calls"
        f"  |  queue: {len(queue)} awaiting a contract"
    )
    calls = inventory

    client = obs.new_client()
    session = obs.register(client)
    print(f"workspace: {session['workspace_id']}")

    empty = obs.observe_unseeded(client, session, calls)
    seeded_ids = obs.seed(client, session)
    print(f"seeded objects: {json.dumps(seeded_ids, indent=2)}")
    seeded = obs.observe(client, session, calls, seeded_ids)

    empty_by_path = {(e["method"], e["specPath"]): e for e in empty}
    seeded_by_path = {(s["method"], s["specPath"]): s for s in seeded}

    engine = infer._Inferencer()
    table: dict[str, str] = {}
    skipped: list[dict] = []
    statuses: dict[str, int] = {}

    for call in calls:
        method = call["method"]
        spec_path = call["specPath"]
        key = (method, spec_path)

        se = empty_by_path.get(key, {})
        ss = seeded_by_path.get(key, {})
        statuses[str(ss.get("status"))] = statuses.get(str(ss.get("status")), 0) + 1

        # `observe` records the payload under `states`; `observe_unseeded` returns
        # it as a top-level `body`. Reading `body` from BOTH silently discarded
        # every SEEDED observation, so 46 endpoints looked like 404s that were in
        # fact observed working -- the harness disagreed with a direct probe.
        # A pass contributing nothing is asserted rather than assumed.
        states: list = []
        empty_body = se.get("body")
        if isinstance(empty_body, (dict, list)):
            states.append(empty_body)
        elif "body" not in se:
            raise SystemExit(
                f"observation shape changed for {method} {spec_path}: "
                f"no `body` key in observe_unseeded output"
            )
        for entry in ss.get("states", []):
            if isinstance(entry.get("body"), (dict, list)):
                states.append(entry["body"])

        if not states:
            # Name the missing configuration when there is one. "no parseable
            # JSON body" is true but useless on its own -- it reads like a harness
            # fault when the real cause is an absent provider.
            gated = obs.PROVIDER_GATED.get((spec_path, method.lower()))
            skipped.append(
                {
                    "method": method,
                    "specPath": spec_path,
                    "status": ss.get("status"),
                    "reason": (
                        f"provider-gated: {gated}"
                        if gated
                        else "no parseable JSON body observed in either state"
                    ),
                    "providerGated": bool(gated),
                }
            )
            continue

        cls = engine.infer_response(spec_path, method, states)
        if cls is None:
            skipped.append(
                {
                    "method": method,
                    "specPath": spec_path,
                    "status": se.get("status"),
                    "reason": "response carried no inferable structure",
                }
            )
            continue
        table[f"{method} {spec_path}"] = cls

    GENERATED.write_text(infer.render_models(engine.lines), encoding="utf-8")

    # MERGE THE MAP. NEVER OVERWRITE IT.
    #
    # An endpoint already carrying a contract leaves the queue, so a
    # gap-only run writes a SMALLER table than the one it read -- turning 104
    # published contracts into 15 and every downstream count into a description
    # of the loss rather than the work. An entry observed this run wins (the
    # model is fresher); an entry not observed this run is RETAINED, because a
    # provider-unavailable endpoint must not lose its contract because a rerun
    # could not reach the provider.
    existing: dict[str, str] = {}
    if MAP_PATH.exists():
        try:
            existing = json.loads(MAP_PATH.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            existing = {}
    candidate = {k: v for k, v in existing.items() if k not in table}
    # A retained entry whose model class was NOT emitted this run is DROPPED, not
    # kept.
    #
    # Generated names carry a sequence suffix (`...Assets28`) assigned in
    # observation order, so they are only stable for a fixed set of endpoints.
    # An endpoint that fails observation on one run shifts every later counter,
    # and a name written by a previous run then points at a class that no longer
    # exists -- `contract_registry.contract_for` returns None and the drift test
    # fails with a message that points at the registry instead of at the
    # generator. Retaining such an entry would be worse than losing it: one
    # honest gap beats one contract that cannot resolve.
    retained = {k: v for k, v in candidate.items() if v in engine.models}
    orphaned = sorted(set(candidate) - set(retained))
    if orphaned:
        print(
            f"  WARNING: dropped {len(orphaned)} retained contract(s) whose model"
            f" was not emitted this run; first={orphaned[0]!r}"
        )
    merged = {**retained, **table}
    MAP_PATH.write_text(json.dumps(merged, indent=2, sort_keys=True), encoding="utf-8")

    # Gap accounting is done in the map's key space only.
    #
    # The queue is 55 CALLS but fewer DISTINCT endpoints -- the audit records one
    # entry per (file, call site), and 50 of the 191 calls are the same endpoint
    # reached from two files. Mixing the two units made the arithmetic report
    # negative numbers of remaining work, which is exactly the kind of number
    # that gets quoted as a result.
    inv_keys = {f"{c['method']} {c['specPath']}" for c in calls}
    gap_closed = len(gap_keys & set(table))
    gap_open = sorted(gap_keys - set(table))
    print()
    print("=" * 78)
    print("RESULT")
    print("=" * 78)
    print(f"  inventory calls observed  {len(calls)}")
    print(f"  distinct endpoints        {len(inv_keys)}")
    print(f"  gap endpoints at start    {len(gap_keys)}")
    print(f"  gap endpoints closed      {gap_closed}")
    print(f"  GAP ENDPOINTS REMAINING   {len(gap_open)}")
    print(f"  contracts inferred now    {len(table)}")
    print(f"  contracts retained        {len(retained)}")
    print(f"  CONTRACTS IN TOTAL        {len(merged)}")
    print(f"  models emitted            {len(engine.models)}")
    print(f"  observation statuses      {json.dumps(statuses)}")

    if skipped:
        print("\n  SKIPPED (reported, not hidden):")
        for s in skipped:
            tag = "PROVIDER-GATED" if s.get("providerGated") else "UNEXPLAINED"
            print(f"    {s['method']:6} {s['specPath']}")
            print(f"           [{tag}] status={s['status']}")
            print(f"           {s['reason']}")

    REPORT.write_text(
        json.dumps(
            {
                "inventory": len(calls),
                "distinctEndpoints": len(inv_keys),
                "queue": len(queue),
                "generatedNow": len(table),
                "gapClosed": gap_closed,
                "gapRemaining": len(gap_open),
                "retained": len(retained),
                "contractsInTotal": len(merged),
                "gapRemainingEndpoints": gap_open,
                "skipped": skipped,
                "models": len(engine.models),
                "contracts": merged,
                "observationStatuses": statuses,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"\nwrote {GENERATED.relative_to(REPO)}")
    print(f"wrote {MAP_PATH.relative_to(REPO)}")
    print(f"wrote {REPORT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
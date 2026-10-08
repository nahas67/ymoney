"""Render release documentation from current source, audits and sanitized receipts.

No clock, local absolute paths, git worktree state or random values enter output.
Run twice (or --check) to detect stale documents. Runtime CI receipts carry their
own exact commit SHA: a git commit cannot embed its own hash in its contents.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCS = ROOT / "docs"
DESTINATIONS = {
    "activity": "Operations / Command Center", "api_keys": "Settings / API Keys",
    "archives": "Operations / project archive", "auth": "Session gate / Settings",
    "autopilot": "Automation", "brands": "Brands", "campaigns": "Campaigns",
    "captions": "Studio / canonical editor", "comments": "Project review / Studio",
    "connections": "Settings / Connections; Providers", "content": "Planner / Projects / Assets / Campaigns / Calendar",
    "creative": "Studio / canonical editor", "distribution": "Distribution",
    "experiments": "Experiments", "exports": "Assets / Exports; Studio export",
    "inbox": "Community", "intelligence_decisions": "Intelligence",
    "intelligence_evidence": "Intelligence", "intelligence_routing": "Intelligence",
    "internal_ops": "Operations probes; remaining internal paths headless",
    "knowledge": "Memory / Settings sources", "lessons": "Experiments",
    "lipsync": "Localization / dubbing", "live": "Intelligence / Operations",
    "localization": "Localization", "longform": "Long-Form API (no NEW UI workflow)",
    "misc": "Distribution / Analytics / Operations / Memory / Automation",
    "music": "Assets / Brands / Settings", "notifications": "Shell / Settings inbox",
    "ops": "Operations / Settings retention", "performance": "Analytics",
    "planner": "Planner / Calendar / Settings policy and capacity",
    "preview": "Assets / Voice preview", "projects": "Projects",
    "providers": "Providers / Operations", "reviews": "Projects / Campaigns review",
    "safety": "Operations / Settings budgets / Intelligence",
    "telegram": "Settings / Telegram", "timelines": "Studio / canonical editor",
    "ugc": "UGC / avatars", "webhooks": "Settings / Webhooks", "workspaces": "Settings / workspace and team",
}
THIN = {"longform", "preview", "exports", "autopilot", "telegram", "connections"}


def load(name: str) -> dict:
    return json.loads((DOCS / name).read_text(encoding="utf-8"))


def cell(value) -> str:
    if value is True:
        return "PASS"
    if value is False:
        return "NO"
    return str(value).replace("|", "\\|").replace("\n", " ")


def router_rows():
    rows = []
    for path in sorted((ROOT / "backend/app/api/v1").glob("*.py")):
        if path.stem == "__init__":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        verbs = Counter()
        roles = set()
        endpoints = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "require_workspace_role":
                if node.args and isinstance(node.args[0], ast.Constant):
                    roles.add(str(node.args[0].value))
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for deco in node.decorator_list:
                    if isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute) and deco.func.attr in {"get", "post", "put", "patch", "delete"}:
                        verbs[deco.func.attr.upper()] += 1
                        route = ast.unparse(deco.args[0]) if deco.args else "<dynamic>"
                        endpoints.append(f"{deco.func.attr.upper()} {route}")
        destination = "Studio / media intelligence" if path.stem.startswith("media_intel") else DESTINATIONS.get(path.stem, "UNMAPPED — release gap")
        evidence = "Focused unit coverage; live/full lifecycle not fully verified" if path.stem in THIN else "See route/critical/Studio suites; render is not every endpoint lifecycle"
        rows.append((path, verbs, roles, endpoints, destination, evidence))
    return rows


def render() -> dict[str, str]:
    audit = load("UI_CONTRACT_AUDIT.json")
    generation = load("UI_CONTRACT_GENERATION.json")
    matrix = load("UI_ROUTE_RELEASE_MATRIX.json")
    honesty = load("ANALYTICS_HONESTY_AUDIT.json")
    evidence_path = DOCS / "NEW_UI_RELEASE_EVIDENCE.json"
    evidence = json.loads(evidence_path.read_text(encoding="utf-8")) if evidence_path.exists() else {}
    registry = (ROOT / "frontend/src/routes/registry.ts").read_text(encoding="utf-8")
    route_list = registry.split("export const ROUTES:", 1)[1].split("\n];", 1)[0]
    route_matches = re.findall(r'\{ path: "([^"]+)", label: "([^"]+)"[^}]*?group: "([^"]+)"[^}]*?permission: ("[^"]+"|null)', route_list, re.S)
    routes = [(path, label, group, "session only" if permission == "null" else permission.strip('"'))
              for path, label, group, permission in route_matches]
    settings = (ROOT / "frontend/src/features/settings/Settings.tsx").read_text(encoding="utf-8")
    sections = re.findall(r'\{ id: "([^"]+)", label: "([^"]+)", blurb: "([^"]+)"', settings)
    spec = json.loads((ROOT / "frontend/src/api/openapi.json").read_text(encoding="utf-8"))
    heading = "> Generated by `python scripts/gen_new_ui_release_docs.py`; `--check` refuses stale output.\n\n"
    counts = audit.get("byClass", {})
    contract_text = f"{audit['totalScanned']} discovered call sites; classifications `{json.dumps(counts, sort_keys=True)}`. {generation['contractsInTotal']} response contracts; {len(spec['components']['schemas'])} OpenAPI schemas."

    capability = "# YMONEY NEW UI — Capability Matrix\n\n" + heading
    capability += "This is the current source inventory, not the old Phase A proposal. Backend guards remain authoritative; a visible control or schema does not prove live-provider support.\n\n"
    rows = router_rows()
    capability += f"## Router coverage\n\n{len(rows)} router files; {sum(sum(v.values()) for _, v, *_ in rows)} HTTP decorators (not identical to mounted OpenAPI operations).\n\n"
    capability += "| Backend source | Current destination | Reads / writes | Explicit workspace role gates | Verification boundary |\n|---|---|---|---|---|\n"
    for path, verbs, roles, endpoints, destination, boundary in rows:
        capability += f"| `{path.relative_to(ROOT).as_posix()}` | {destination} | {', '.join(f'{k}:{v}' for k, v in sorted(verbs.items())) or 'none'} | {', '.join(sorted(roles)) or 'Inspect project/auth/helper guards; not assumed public'} | {boundary} |\n"
    capability += "\nProject-target permissions in `services/capabilities.py` and review/comment responses must not be replaced by workspace-role guesses. Paid ambiguity remains `SUBMISSION_UNKNOWN` / `UNKNOWN_EXPOSURE`; never offer an unsafe resend or show unknown exposure as zero.\n\n"
    capability += "## Settings control inventory\n\nVertical grouped navigation, searchable sections, mobile selection and dirty/save feedback are implemented in `features/settings/Settings.tsx`.\n\n| Section | Control / intent |\n|---|---|\n"
    for sid, label, blurb in sections:
        capability += f"| `{sid}` — {label} | {blurb} |\n"
    capability += "\nRead/write endpoint and schema evidence per call is in `UI_CONTRACT_AUDIT.json`; backend handler source above defines exact permissions and production dependencies. Browser smoke results are in `NEW_UI_VERIFICATION.md`. Scheduling views are read-only where labeled; unconfigured providers remain unavailable. No new capability was added during release closure.\n"

    route_doc = "# YMONEY NEW UI — Route Matrix\n\n" + heading
    route_doc += f"{len(routes)} registered routes in {len(set(r[2] for r in routes))} domains. `routes/registry.ts` is authoritative; `pages/Editor.tsx` remains the canonical timeline engine.\n\n"
    route_doc += "| Route | Screen | Domain | Capability |\n|---|---|---|---|\n"
    for path, label, group, permission in routes:
        route_doc += f"| `{path}` | {label} | {group} | `{permission}` |\n"
    route_doc += "\n## Measured matrix\n\n`UI_ROUTE_RELEASE_MATRIX.json` contains the per-route facts. `PASS` is an observed assertion; `NO`, `not-seedable`, `provider-gated`, and `unobserved` are limitations, not passes. A render pass is not lifecycle completion.\n\n"
    dims = [d for d in matrix["dimensions"] if d != "contract"]
    route_doc += "| Route | " + " | ".join(dims) + " |\n|---|" + "---|" * len(dims) + "\n"
    for row in matrix["routes"]:
        route_doc += f"| `{row['path']}` | " + " | ".join(cell(row.get(d, "unobserved")) for d in dims) + " |\n"
    route_doc += "\nSix-width shell regression includes 1440/1280/1024/768/390/360 in `routes-a11y.spec.ts`; the matrix records only its own declared widths. Do not claim that shell testing proves every control at every width.\n"

    design = "# YMONEY NEW UI — Design-System Record\n\n" + heading
    design += "Release closure froze the existing product design. This document records implemented intent; it does not retroactively claim approval of several design concepts.\n\n"
    design += "- Token authority: `frontend/src/design-system/tokens.css`; shared feature CSS: `styles.css`; primitives: `primitives.tsx`.\n- Dark-first boot: `frontend/index.html`. No coherent alternate light/system product theme is claimed. `useTheme` is a dormant compatibility hook, not browser-tested theme-switch evidence.\n- Command Center: operational status rail, prioritized attention and drill-down; Settings: vertical groups, search, active section and mobile navigation; Automation: existing supervisor/agent controls.\n- Dense tables and in-panel overflow; explicit labels and focus rings; semantic status text, not color alone.\n- Toasts and save feedback announce via live regions; reduced-motion rules must remain enforced by the existing CSS and browser smoke.\n- Analytics distinguishes MEASURED/DERIVED/UNAVAILABLE. Null/unknown is not a fabricated numeric zero; mock publishing stays visibly MOCK.\n- Studio shell mounts the canonical timeline editor. No duplicate document store or operation log was introduced.\n- Curated screenshots under `docs/shots/` are retained review snapshots. Routine Playwright output belongs to ignored test artifacts, never rewrites documentation screenshots.\n\nClaims of exact WCAG contrast, complete light theme, or every micro-interaction require their own evidence; token presence or a passing render test alone does not establish them.\n"

    gap = "# YMONEY NEW UI — Release Gap Audit\n\n" + heading
    gap += "## Contract state\n\n" + contract_text + "\n\n"
    gap += "The historical 285/282/3 summary is not coerced into current output. Previously the template scanner invented GET calls and hid unknown routes by filtering against the spec. Final measured classifications and reasons below are authoritative.\n\n"
    gap += "| Method | Call / resolved route | Classification |\n|---|---|---|\n"
    for call in audit["calls"]:
        if call.get("class") != "SCHEMA_COVERED":
            gap += f"| {call['method']} | `{cell(call.get('uiPath', call.get('template', '')) )}` | {call['class']} |\n"
    gap += "\nProduction dependency/test-seam explanations are recorded in the contract generation report and release evidence. Binary download is not ordinary JSON, UGC fixture gaps are not external credential gates, and supported local voice catalogs must not be mislabeled credential-gated. No empty/Any schema was invented to satisfy a percentage.\n\n"
    gap += "## Analytics residuals\n\n"
    unresolved = honesty.get('unresolved_provenance', [])
    out_of_scope = honesty.get('known_out_of_scope', [])
    gap += f"- Current audit records {len(unresolved)} unresolved-provenance notes and {len(out_of_scope)} known-out-of-scope findings; full details remain in `ANALYTICS_HONESTY_AUDIT.json`.\n"
    for note in unresolved:
        gap += f"  - {cell(note)}\n"
    gap += "\n| Out-of-scope endpoint | Field | Rule | Source |\n|---|---|---|---|\n"
    for finding in out_of_scope:
        gap += "| " + " | ".join(cell(finding.get(key, '—')) for key in ('endpoint', 'field', 'rule', 'site')) + " |\n"
    gap += "- Historical scope: 3 unresolved storage provenance items and 12 known-out-of-scope fabrications. A changed audit count must be explained, not silently deleted. Strict analytics audit remains red until that backend debt is actually fixed. UI honest-null tests do not repair storage provenance.\n\n"
    gap += "## Coverage and live dependencies\n\nLong-form paid lifecycle, voice sample spending, full export/render lifecycle, autopilot cycles, Telegram pairing and live provider checks have narrower verification than route rendering and focused unit tests. Live credentials/provider tests were not executed to manufacture release evidence. Existing workspace saves send blank brand_voice; this predates this release and is not claimed fixed.\n\n"
    gap += "## Release regressions\n\nSee sanitized release evidence and regression tests for the portable browser launcher, fail-loud backend runner, capacity preservation, authenticated export download, default voice query, URL cleanup and Settings draft bookkeeping. No product redesign was started. No Work 17 product workflow UI was included; route-specific overrides in the request builder are release-verification fixtures, not a shipped workflow.\n"

    verification = "# YMONEY NEW UI — Verification and Reproducibility\n\n" + heading
    verification += "## Identity\n\n"
    verification += f"- Starting SHA: `{evidence.get('starting_sha', '07b6d0f1e3154ef2888156879431a3fc11258fd6')}`.\n"
    verification += f"- Verified release/code SHA: `{evidence.get('verified_sha', 'PENDING — uncommitted candidate')}`.\n"
    verification += f"- GitHub Actions run: `{evidence.get('github_run_id', 'PENDING')}`; result: **{evidence.get('github_result', 'NOT RUN')}**.\n"
    verification += "- Exact final documentation-head SHA is resolved from `git rev-parse HEAD` and the SHA-bearing CI run receipts, not a self-referential hash embedded in this file. A documentation-only successor must receive the same required checks.\n\n"
    verification += "Historical dirty-tree totals were incorrectly attributed to the base commit and the slice runner could exit 0 after test failures. Those historical numbers are context, not clean-release evidence.\n\n## Clean-checkout local gates\n\n"
    verification += "| Gate | Command | Exit | Passed | Failed | Skipped | Duration seconds |\n|---|---|---|---|---|---|---|\n"
    for gate in evidence.get("local_gates", []):
        verification += "| " + " | ".join(cell(gate.get(k, "—")) for k in ("name", "command", "exit", "passed", "failed", "skipped", "duration_seconds")) + " |\n"
    if not evidence.get("local_gates"):
        verification += "| Required fresh gates | PENDING | — | — | — | — | — |\n"
    verification += "\n## GitHub jobs for exact pushed SHA\n\n| Job | Result | Duration seconds | Failure |\n|---|---|---|---|\n"
    for job in evidence.get("github_jobs", []):
        verification += "| " + " | ".join(cell(job.get(k, "—")) for k in ("name", "result", "duration_seconds", "failure")) + " |\n"
    if not evidence.get("github_jobs"):
        verification += "| All required jobs | PENDING | — | Not executed yet |\n"
    verification += "\n## Contract and residual state\n\n" + contract_text + "\n\nSee `NEW_UI_GAP_AUDIT.md` for precise classifications, storage-level analytics debt and live-provider limitations.\n\n"
    verification += "## Release integrity\n\n"
    for key in ("secret_scan", "generation_determinism", "fresh_postgres", "smoke", "environment_parity", "git_status", "branch_protection"):
        verification += f"- {key.replace('_', ' ').capitalize()}: {cell(evidence.get(key, 'PENDING'))}\n"
    verification += "\nPath-by-path reconciliation: `NEW_UI_CHANGED_PATHS.md`. No bulk `git add -A`, history rewrite or force-push is authorized.\n\n"
    verification += f"## Verdict\n\n{evidence.get('verdict', '🟡 YMONEY NEW UI PARTIAL — NOT REPRODUCIBLE')}\n"
    return {"NEW_UI_CAPABILITY_MATRIX.md": capability, "NEW_UI_ROUTE_MATRIX.md": route_doc,
            "NEW_UI_DESIGN_SYSTEM.md": design, "NEW_UI_GAP_AUDIT.md": gap,
            "NEW_UI_VERIFICATION.md": verification}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    changed = []
    for name, content in render().items():
        path = DOCS / name
        if not path.exists() or path.read_text(encoding="utf-8") != content:
            changed.append(name)
            if not args.check:
                path.write_text(content, encoding="utf-8", newline="\n")
    print(json.dumps({"documents": 5, "drift": changed, "mode": "check" if args.check else "write"}))
    return int(args.check and bool(changed))


if __name__ == "__main__":
    raise SystemExit(main())

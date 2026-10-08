"""Release docs must reflect current source rather than historical prose."""
import importlib.util
from pathlib import Path


def _renderer():
    root = Path(__file__).resolve().parents[2]
    spec = importlib.util.spec_from_file_location(
        "release_docs", root / "scripts/gen_new_ui_release_docs.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_release_documentation_covers_root_route_and_existing_domains():
    docs = _renderer().render()
    routes = docs["NEW_UI_ROUTE_MATRIX.md"]
    capability = docs["NEW_UI_CAPABILITY_MATRIX.md"]
    assert "23 registered routes in 13 domains" in routes
    assert "`backend/app/api/v1/longform.py` | Long-Form API (no NEW UI workflow)" in capability
    for route in ("/", "/planner", "/settings", "/automation", "/studio/:timelineId"):
        assert f"| `{route}` |" in routes


def test_release_documentation_is_deterministic_and_does_not_claim_old_results():
    module = _renderer()
    first = module.render()
    assert first == module.render()
    assert len(first) == 5
    assert all("C:\\Users\\" not in value for value in first.values())
    assert "Historical dirty-tree totals" in first["NEW_UI_VERIFICATION.md"]
    assert "3 unresolved storage provenance items and 12" in first["NEW_UI_GAP_AUDIT.md"]


def test_verification_integrity_sentences_have_single_terminal_punctuation():
    verification = _renderer().render()["NEW_UI_VERIFICATION.md"]
    integrity = verification.split("## Release integrity\n\n", 1)[1].split(
        "\n\nPath-by-path reconciliation:", 1
    )[0]

    assert all(not line.endswith("..") for line in integrity.splitlines())


def test_gap_audit_preserves_backend_debt_without_claiming_stale_ui_consumption():
    import json

    gap = _renderer().render()["NEW_UI_GAP_AUDIT.md"]
    audit_path = Path(__file__).resolve().parents[2] / "docs/ANALYTICS_HONESTY_AUDIT.json"
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    ops_finding = next(
        row for row in audit["fields"]
        if row["endpoint"] == "/ops/overview" and row["field"] == "costs.spent_last_24h_usd"
    )

    assert "The backend field remains fabricated for an empty window" in gap
    assert "The NEW UI Command Center reads `/costs` instead" in gap
    assert "The Command Center reads this section rather than GET /costs" not in gap
    assert "The NEW UI Command Center reads GET /costs" in ops_finding["reason"]
    assert "No Work 17 product workflow UI was included" in gap


def test_release_documentation_check_refuses_stale_checked_in_output(tmp_path, monkeypatch):
    import sys

    module = _renderer()
    source_docs = module.DOCS
    for name in (
        "UI_CONTRACT_AUDIT.json",
        "UI_CONTRACT_GENERATION.json",
        "UI_ROUTE_RELEASE_MATRIX.json",
        "ANALYTICS_HONESTY_AUDIT.json",
        "NEW_UI_RELEASE_EVIDENCE.json",
    ):
        (tmp_path / name).write_bytes((source_docs / name).read_bytes())

    stale = tmp_path / "NEW_UI_ROUTE_MATRIX.md"
    stale.write_text("stale output\n")
    monkeypatch.setattr(module, "DOCS", tmp_path)
    monkeypatch.setattr(sys, "argv", ["gen_new_ui_release_docs.py", "--check"])

    assert module.main() == 1
    assert stale.read_text() == "stale output\n"


def test_capability_documentation_accounts_for_each_backend_router():
    module = _renderer()
    doc = module.render()["NEW_UI_CAPABILITY_MATRIX.md"]
    for path, *_ in module.router_rows():
        assert path.relative_to(module.ROOT).as_posix() in doc
    assert "UNMAPPED — release gap" not in doc

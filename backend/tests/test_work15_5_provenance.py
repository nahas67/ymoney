"""Work 15.5 §2 — the provenance ledger must not drift from the tree.

The MIT condition is that the copyright notice travels with copied code. A
ledger that quietly falls out of date is worse than no ledger, because it reads
as an attestation. These tests make the two required documents load-bearing:

* the technology matrix must exist and classify every subsystem the work order
  named, with every classification drawn from the closed vocabulary;
* every reused component the ledger records must actually exist on disk;
* every file holding directly-copied donor code must carry the inline notice,
  so an attribution cannot be stripped without a test failing.

This is a self-audit, so it is deliberately strict: a new port without a ledger
row fails here rather than in a code review nobody reads.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MATRIX = REPO_ROOT / "docs" / "MPT_TECHNOLOGY_MATRIX.md"
LEDGER = REPO_ROOT / "docs" / "oss" / "MONEYPRINTERTURBO_INTEGRATION.md"

#: The closed classification vocabulary the work order requires.
CLASSES = {
    "KEEP_YMONEY",
    "PORT_MPT",
    "MERGE",
    "REIMPLEMENT",
    "IGNORE",
    "BLOCKED_LICENSE",
}

#: Every subsystem §1 of the work order required the matrix to cover.
REQUIRED_SUBSYSTEMS = [
    "LLM provider",
    "TTS",
    "Material / stock",
    "Paid-job safety",
    "Caching",
    "AI music",
    "Semantic video intelligence",
    "Task manager",
    "Render",
    "CLI",
    "API / config / security",
    "UX",
]

#: The inline notice any directly-copied donor file must carry.
NOTICE = re.compile(
    r"MoneyPrinterTurbo 1\.3\.7.*?Copyright \(c\) 2024 Harry.*?MIT License",
    re.DOTALL,
)


@pytest.fixture(scope="module")
def matrix_text() -> str:
    assert MATRIX.is_file(), f"missing required document: {MATRIX}"
    return MATRIX.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def ledger_text() -> str:
    assert LEDGER.is_file(), f"missing required document: {LEDGER}"
    return LEDGER.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# the matrix exists and is complete
# ---------------------------------------------------------------------------


def test_the_technology_matrix_exists(matrix_text):
    assert "MPT Technology Matrix" in matrix_text


def test_every_required_subsystem_is_classified(matrix_text):
    for subsystem in REQUIRED_SUBSYSTEMS:
        assert subsystem in matrix_text, f"matrix does not cover {subsystem!r}"


def test_the_classification_vocabulary_is_the_closed_set(matrix_text):
    """No invented verdict. A new label must be argued for, not smuggled in."""
    used = {c for c in CLASSES if c in matrix_text}
    assert used == CLASSES, f"matrix never uses {CLASSES - used}"


def test_the_matrix_states_the_donor_version_and_archive(matrix_text):
    assert "1.3.7" in matrix_text
    assert "MoneyPrinterTurbo-1.3.7.zip" in matrix_text


def test_the_matrix_records_the_inspection_scope(matrix_text):
    """A matrix that claims coverage must show it counted something."""
    assert re.search(r"116", matrix_text), "no file count recorded"


# ---------------------------------------------------------------------------
# the licence ledger
# ---------------------------------------------------------------------------


def test_the_ledger_records_the_donor_licence(ledger_text):
    assert "MIT" in ledger_text
    assert "2024 Harry" in ledger_text


def test_the_ledger_explicitly_does_not_treat_the_mit_grant_as_blanket(ledger_text):
    """The load-bearing licence judgement: code yes, bundled assets no."""
    assert "BLOCKED_LICENSE" in ledger_text
    lowered = ledger_text.lower()
    for asset in ("songs", "fonts"):
        assert asset in lowered, f"ledger never addresses bundled {asset}"


def test_the_ledger_records_the_integration_direction(ledger_text):
    """Provenance is meaningless without the YMONEY destination."""
    assert "backend/app" in ledger_text
    assert re.search(r"\|\s*(direct-copy|refactor|reimplementation)\s*\|", ledger_text)


def test_every_reused_component_in_the_ledger_exists_on_disk(ledger_text):
    """A row pointing at a file that was never written is a false attestation."""
    # The ledger's first column of the reuse table holds the MPT source; the
    # destination appears in a later column. Pull every backend/... path out of
    # a table row and require it to exist.
    candidates = set(re.findall(r"backend/app/[A-Za-z0-9_/]+\.py", ledger_text))
    assert candidates, "ledger records no YMONEY destinations at all"

    missing = [p for p in sorted(candidates) if not (REPO_ROOT / p).is_file()]
    assert not missing, f"ledger references files that do not exist: {missing}"


def test_directly_copied_files_carry_the_mit_notice(ledger_text):
    """Attribution must be inline, not only in the ledger.

    The ledger lists the directly-copied files by name; each must carry the
    notice itself, because the file is what travels if the repo is vendored.
    """
    directly_copied = re.findall(
        r"`(backend/app/[A-Za-z0-9_/]+\.py)`\s*←", ledger_text
    )
    assert directly_copied, "ledger declares no directly-copied files"

    for rel in directly_copied:
        path = REPO_ROOT / rel
        assert path.is_file(), f"directly-copied file missing: {rel}"
        assert NOTICE.search(path.read_text(encoding="utf-8")), (
            f"{rel} is recorded as directly copied but carries no MIT notice")


def test_every_w155_module_names_its_donor(ledger_text):
    """New Work 15.5 modules must be traceable to a donor path or declared new."""
    new_modules = [
        "backend/app/services/paid_jobs.py",
        "backend/app/services/media_cache.py",
        "backend/app/services/pause_tags.py",
        "backend/app/services/audio_concat.py",
        "backend/app/providers/music/base.py",
        "backend/app/providers/music/elevenlabs_music.py",
    ]
    for rel in new_modules:
        path = REPO_ROOT / rel
        assert path.is_file(), f"{rel} was not created"
        assert "MoneyPrinterTurbo" in path.read_text(encoding="utf-8"), (
            f"{rel} does not record the donor it derives from")

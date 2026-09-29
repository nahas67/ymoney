"""Creative Director (Work 08 Lane B): NL → typed commands → preview → apply.

Submodules:
- ``commands`` — the typed CreativeCommand union, catalog, validation, estimates.
- ``director`` — deterministic NL parsing, ChangeSet previews, versioned apply/undo.

Commands are inert data; only ``director.apply`` mutates, and only through the
canonical timeline operation layer + the Work 02 version system.
"""

from __future__ import annotations

__all__ = ["commands", "director"]

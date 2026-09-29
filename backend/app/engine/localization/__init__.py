"""Localization production layer (Work 07 Lane A).

Provider-independent multilingual localization: pipeline (stages, lineage,
editor-compatible timeline output) + deterministic QC with SHADOW-only
semantic advisory. See :mod:`app.engine.localization.pipeline` and
:mod:`app.engine.localization.quality`.
"""

from __future__ import annotations

from app.engine.localization import quality
from app.engine.localization.pipeline import (
    STAGES,
    LocalizationError,
    LocalizationPipeline,
    prepare_localizations,
    run_prepared,
)

__all__ = [
    "STAGES",
    "LocalizationError",
    "LocalizationPipeline",
    "prepare_localizations",
    "quality",
    "run_prepared",
]

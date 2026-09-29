"""Brand wiring entry point (Work 08, Lane C).

Canonical import path for the generation-site brand gate. The implementation
lives in :mod:`app.engine.brand_templates` (the single Lane C brand file, see
its module docstring); this module re-exports it so call sites can depend on
a wiring-specific name without pulling template-registry internals, and so
the "wiring" surface stays stable if the template registry ever moves.

Everything here is lazy + degrading: Lane A's ``app.engine.brand`` package may
be absent mid-merge (or raise), and a generation cycle must never break
because of it. ``brand_gate`` therefore never raises and always returns a
plain dict:

* ``applied_brand=False`` / ``brand_available=False`` / ``degraded="..."``
  when the policy could not be resolved (no module, no policy, no workspace);
* ``hard_constraints`` / ``forbidden_phrases`` / ``required_disclaimers`` /
  ``approved_voices`` / ``caption_style`` / ``tone`` / ``vocabulary`` when it
  could;
* ``effective_config_id`` + ``provenance`` (the auditable lineage Lane A
  persists) and template defaults merged with brand overrides
  (BrandDNA > template > model output > defaults).

Precedence documented once and enforced across the product::

    security/compliance -> explicit user override -> brand hard ->
    campaign -> learned -> defaults
"""

from __future__ import annotations

from app.engine.brand_templates import (
    TEMPLATE_KEYS,
    apply_template_defaults,
    approved_voice,
    attach_template,
    brand_cover_style,
    brand_gate,
    brand_glossary_entries,
    brand_instructions,
    brand_qc_check,
    brand_variant_metadata,
    enforce_forbidden_phrases,
    find_forbidden_phrases,
    get_template,
    hook_rejection_reason,
    lesson_conflicts_with_brand,
    lineage_markers,
    list_templates,
    merge_brand_glossary,
    persist_template_preset,
    template_for,
)

__all__ = [
    "TEMPLATE_KEYS",
    "approved_voice",
    "apply_template_defaults",
    "attach_template",
    "brand_cover_style",
    "brand_gate",
    "brand_glossary_entries",
    "brand_instructions",
    "brand_qc_check",
    "brand_variant_metadata",
    "enforce_forbidden_phrases",
    "find_forbidden_phrases",
    "get_template",
    "hook_rejection_reason",
    "lesson_conflicts_with_brand",
    "lineage_markers",
    "list_templates",
    "merge_brand_glossary",
    "persist_template_preset",
    "template_for",
]

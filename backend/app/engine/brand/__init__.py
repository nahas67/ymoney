"""Brand DNA + creative policy core (Work 08 Lane A).

    BrandDNA            the full typed creative identity (validated document)
    effective_dna()     deterministic layer merge (platform fragment last)
    resolve_effective_policy()   precedence chain + persisted snapshot
    EffectiveCreativePolicy      typed hard constraints + per-field provenance
    verify_artifact()   deterministic brand consistency report (SHADOW semantic)

Precedence (last wins): workspace -> brand -> campaign -> content -> platform.
Overrides never mutate stored BrandDNA; every resolution snapshots an
`effective_config_id` for reproducibility. Hard constraints are deterministic
inputs to generation -- LLMs may never override them.
"""

from __future__ import annotations

from app.engine.brand.dna import (
    BrandDNA,
    BrandDNASchemaError,
    deep_merge,
    dna_version,
    effective_dna,
    normalize_platform,
)
from app.engine.brand.inheritance import (
    BrandConfigError,
    BrandError,
    BrandNotFound,
    brand_layer,
    clear_override,
    get_effective_config,
    merge_levels,
    resolve_effective_policy,
    set_override,
    workspace_defaults,
)
from app.engine.brand.policy import (
    HARD_CONSTRAINT_KEYS,
    LEVELS,
    WORKSPACE_DEFAULTS,
    EffectiveCreativePolicy,
)
from app.engine.brand.verifier import (
    QC_STATUSES,
    BrandConsistencyReport,
    verify_artifact,
)

__all__ = [
    "BrandConsistencyReport",
    "BrandConfigError",
    "BrandDNA",
    "BrandDNASchemaError",
    "BrandError",
    "BrandNotFound",
    "EffectiveCreativePolicy",
    "HARD_CONSTRAINT_KEYS",
    "LEVELS",
    "QC_STATUSES",
    "WORKSPACE_DEFAULTS",
    "brand_layer",
    "clear_override",
    "deep_merge",
    "dna_version",
    "effective_dna",
    "get_effective_config",
    "merge_levels",
    "normalize_platform",
    "resolve_effective_policy",
    "set_override",
    "verify_artifact",
    "workspace_defaults",
]

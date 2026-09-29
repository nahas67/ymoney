"""Professional export center (Work 11 Lane X) -- contracts §11.

Four cooperating modules, deliberately import-light so a sibling lane landing
mid-wave can never break the API import:

* :mod:`~app.engine.exporter.profiles` -- the 8 builtin presets, config
  validation, and the workspace profile rows
  (``validate_config`` / ``check_profile_format`` / ``seed_builtins``).
* :mod:`~app.engine.exporter.formats` -- the format registry with an HONEST
  capability probe per format. An unavailable format reports
  ``available=False`` with a reason; Premiere/Resolve are permanently
  NOT_AVAILABLE rather than a fabricated XML file.
* :mod:`~app.engine.exporter.verify` -- ``verify_export``: the independent
  evidence (exists / streams / duration / resolution / checksum / text
  roundtrip / job persisted) behind a COMPLETE verdict.
* :mod:`~app.engine.exporter.jobs` -- ``run_export`` (executor),
  ``enqueue_export`` (jobs.enqueue ``EXPORT_BUILD``), retry and cancel.

Nothing here reaches out to the network, and nothing here fabricates: an
export that cannot be produced is NOT_AVAILABLE or FAILED, never a file that
merely looks plausible.
"""

from __future__ import annotations

from .formats import (
    ALL_FORMATS,
    FORMAT_REGISTRY,
    ExportContext,
    ExportValidationError,
    FormatSpec,
    get_format,
    list_formats,
    probe_media_format,
    require_available,
    reset_probe_cache,
)
from .jobs import (
    ExportJobError,
    cancel_export,
    enqueue_export,
    list_exports,
    register_export_jobs,
    retry_export,
    run_export,
    run_export_now,
)
from .profiles import (
    BUILTIN_PROFILES,
    PRESETS,
    canonical_preset,
    check_profile_format,
    create_profile,
    list_profiles,
    load_profile,
    preset_config,
    preset_name,
    seed_builtins,
    update_profile,
    validate_config,
)
from .profiles import (
    ExportValidationError as ProfileValidationError,
)
from .verify import sha256_file, verify_export

__all__ = [
    "ALL_FORMATS",
    "BUILTIN_PROFILES",
    "ExportContext",
    "ExportJobError",
    "ExportValidationError",
    "FORMAT_REGISTRY",
    "FormatSpec",
    "PRESETS",
    "ProfileValidationError",
    "cancel_export",
    "canonical_preset",
    "check_profile_format",
    "create_profile",
    "enqueue_export",
    "get_format",
    "list_exports",
    "list_formats",
    "list_profiles",
    "load_profile",
    "preset_config",
    "preset_name",
    "probe_media_format",
    "register_export_jobs",
    "require_available",
    "reset_probe_cache",
    "retry_export",
    "run_export",
    "run_export_now",
    "seed_builtins",
    "sha256_file",
    "update_profile",
    "validate_config",
    "verify_export",
]

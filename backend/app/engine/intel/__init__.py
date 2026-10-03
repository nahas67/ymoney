"""Media intelligence (Work 12) -- the shared provider/runs foundation.

Importing this package is cheap and side-effect free: it pulls in the provider
contract and nothing else, so ``from app.main import app`` boots in a venv with
zero ML packages installed. Concrete providers live in ``impl/`` and are
imported lazily by the registry; heavy models are imported inside a provider's
``health()``/``run()`` body, never here.

Module map:

  * :mod:`app.engine.intel.base`       -- ABC + frozen value objects (§1.1)
  * :mod:`app.engine.intel.factory`    -- never-raise construction (§1.1)
  * :mod:`app.engine.intel.registry`   -- ordered chains + commercial refusal
  * :mod:`app.engine.intel.jobs`       -- job-kind vocabulary (§14)
  * :mod:`app.engine.intel.ffmpeg_util` -- shared ffmpeg measurement helpers
"""

from __future__ import annotations

from app.engine.intel.base import (
    IntelRequest,
    LicenseInfo,
    MediaIntelProvider,
    ProviderCancelled,
    ProviderHealth,
    ProviderResult,
    ProviderTimeout,
    ProviderUnavailable,
    ResourceSpec,
)
from app.engine.intel.factory import UnavailableAdapter, build_provider

__all__ = [
    "IntelRequest",
    "LicenseInfo",
    "MediaIntelProvider",
    "ProviderCancelled",
    "ProviderHealth",
    "ProviderResult",
    "ProviderTimeout",
    "ProviderUnavailable",
    "ResourceSpec",
    "UnavailableAdapter",
    "build_provider",
]

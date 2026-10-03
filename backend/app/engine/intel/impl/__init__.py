"""Concrete media-intelligence providers (one module per registry key).

The registry (``app.engine.intel.registry``) maps a provider key to
``app.engine.intel.impl.<key>`` and imports it LAZILY -- nothing in this package
is imported at app startup. Each module owns one lane's adapter and exposes it
either as a module level ``PROVIDER`` symbol or as the single
:class:`~app.engine.intel.base.MediaIntelProvider` subclass it defines.

Rules for every adapter in here (contracts §0/§1.3):

* import the heavy backend INSIDE ``health()``/``run()`` -- never at module
  import, so the app boots with zero ML packages installed;
* no model installed => ``health().available is False`` with a reason, and
  ``run()`` raises ``ProviderUnavailable``; never fabricate a speaker, face,
  mask, word timing or confidence;
* poll ``should_cancel()`` and honour ``deadline`` via
  ``check_control`` from ``app.engine.intel.base``;
* write NEW derived files only -- the input asset is never modified;
* report honest license terms (code AND model); default to UNVERIFIED
  commercial use until an audit records otherwise.
"""

from __future__ import annotations

__all__: list[str] = []

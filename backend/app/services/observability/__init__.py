"""Production observability: metrics, structured logs, tracing, SLOs, alerts.

Split into five modules so each can be read (and replaced) on its own:

  ``redaction``       structural secret scrubbing -- the sink's last gate
  ``logging_setup``   loguru JSON sink + correlation contextvars
  ``metrics``         in-process registry, Prometheus text renderer, collectors
  ``tracing``         dependency-free span recorder
  ``slo``             SLO TARGETS and alert rule evaluators

Nothing here is imported for its side effects at package import: ``metrics``
declares its specs on the registry it owns, and ``logging_setup.install()`` is
an explicit call so importing this package never reconfigures logging.
"""

from __future__ import annotations

__all__ = [
    "metrics",
    "redaction",
    "slo",
    "tracing",
]
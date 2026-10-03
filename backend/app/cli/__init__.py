"""The ``ymoney`` command line -- an agent-facing surface over the engine.

``ymoney`` is NOT a second implementation of anything. Every command resolves a
workspace and then calls the SAME engine/service functions the HTTP routes
call (``ContentPlanningEngine``, ``run_orchestration``, ``ResearchAgent``,
``VideoProducerAgent``, ``prepare_localizations``, ``jobs_service``). There is
no raw SQL and no shelling out to ffmpeg from here.

Three contracts hold for every invocation, and each has a test that fails if it
is broken:

1. **Exit taxonomy.** ``0`` success, ``1`` the operation ran and failed, ``2``
   bad arguments / an invalid manifest. ``1`` and ``2`` are never collapsed --
   that distinction is what makes the CLI usable from CI and from an agent.
2. **stdout is one JSON object, always.** Logs, warnings and tracebacks go to
   stderr; ``sys.stdout`` is *replaced* for the duration of the command so a
   library that prints cannot corrupt the stream an agent is parsing.
3. **No credential is ever printed and none is ever accepted.** Credentials are
   resolved through ``app.services.provider_settings`` (workspace-scoped) and
   reported as ``configured``/``source`` only. A final guard re-scans the
   serialized payload for every resolved secret before it reaches stdout.

``ymoney publish`` is not a back door. It calls
``engine.planning.autonomy.assert_may_advance(policy, PlanningAction.PUBLISH)``
-- the planner's own single gate -- before it does anything, and that gate
refuses ``PUBLISH`` unconditionally at every autonomy mode. The CLI has no
``--force``.
"""

from __future__ import annotations

# NOTE: ``main`` is deliberately NOT re-exported here. Importing the function
# under that name would shadow the ``app.cli.main`` SUBMODULE attribute on this
# package, so ``import app.cli.main`` would hand back a function and any
# ``app.cli.main:<attr>`` console-script target would break. The console script
# is declared as ``app.cli.main:main``, which resolves through the submodule.
from app.cli.main import (
    CLI_VERSION,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    CommandFailed,
    Refused,
    RunManifest,
    UsageError,
    credential_status,
    redact_secrets,
    resolve_state_dir,
    run_checked,
    run_cli,
    write_result_manifest,
)

__all__ = [
    "CLI_VERSION",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_USAGE",
    "CommandFailed",
    "Refused",
    "RunManifest",
    "UsageError",
    "credential_status",
    "redact_secrets",
    "resolve_state_dir",
    "run_checked",
    "run_cli",
    "write_result_manifest",
]

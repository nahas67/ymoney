"""Deployment tooling that ships inside the application package.

Nothing here is imported by the runtime, and nothing here is imported BY the
runtime: a tool that the app imports can break the app, and an app package that
cannot be imported without its operational scripts cannot be trimmed for a
container image. :mod:`app.scripts.backup_restore` is a ``__main__`` entry point
(``python -m app.scripts.backup_restore``) and lives here because it needs
``app.core.config`` and ``app.models`` to be importable -- i.e. because it is a
tool for THIS application, not a generic script sitting next to it.
"""

from __future__ import annotations

"""pytest entrypoint that cannot consume local .env/provider/developer DB state."""
from __future__ import annotations

import os
import secrets
import sys
import tempfile
from pathlib import Path

from release_app import configure
from release_support import clean_env


def main() -> int:
    env = clean_env()
    # The only opt-in PG URL is supplied by our disposable-container parent,
    # never by a developer's ambient YMONEY_TEST_POSTGRES variable.
    if os.environ.get("YMONEY_RELEASE_POSTGRES_GATE") == "disposable-container":
        env["YMONEY_TEST_POSTGRES"] = os.environ["YMONEY_TEST_POSTGRES"]
    os.environ.clear()
    os.environ.update(env)
    with tempfile.TemporaryDirectory(prefix="ymoney-release-tests-") as scratch:
        os.environ["DATABASE_URL"] = f"sqlite:///{(Path(scratch) / 'tests.db').as_posix()}"
        os.environ["SECRET_KEY"] = secrets.token_urlsafe(48)
        configure()
        import pytest
        try:
            return pytest.main(sys.argv[1:])
        finally:
            # Windows refuses to remove an open SQLite file. Close the real
            # application's pool before the disposable directory is deleted.
            module = sys.modules.get("app.db")
            if module is not None:
                module.engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

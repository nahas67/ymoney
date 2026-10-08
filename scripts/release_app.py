"""Start the real app without loading developer .env files (release gates only)."""
from __future__ import annotations

import argparse
import sys

from release_support import REPO


def configure() -> None:
    sys.path.insert(0, str(REPO / "backend"))
    from app.core import config
    # Nothing importing app.db may run before this. The initial Settings import
    # reads config only; replace it before an engine/provider is constructed.
    config.Settings.model_config["env_file"] = None
    config.get_settings.cache_clear()
    config.settings = config.get_settings()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8099)
    args = parser.parse_args()
    configure()
    import uvicorn
    uvicorn.run("app.main:app", host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    main()

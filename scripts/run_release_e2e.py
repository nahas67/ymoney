"""Full real-Chrome suite on a disposable SQLite backend; never a developer DB."""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import tempfile
from pathlib import Path

from release_support import REPO, clean_env, junit_counts, run_logged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--list", action="store_true", help="Validate collection only, not a gate")
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="ymoney-release-e2e-") as scratch:
        env = clean_env()
        key = secrets.token_urlsafe(48)
        env.update(DATABASE_URL=f"sqlite:///{(Path(scratch) / 'browser.db').as_posix()}",
                   SECRET_KEY=key, YMONEY_SECRET_KEY=key,
                   YMONEY_E2E_PYTHON=sys.executable,
                   YMONEY_RELEASE_OUTPUT=str(output), YMONEY_RELEASE_PYTHON=sys.executable,
                   YMONEY_API_TARGET="http://127.0.0.1:8099")
        xml = output / "playwright.xml"
        xml.unlink(missing_ok=True)
        # Node is installed by the caller; bypass npx's platform-specific shell.
        cmd = ["node", str(REPO / "frontend/node_modules/@playwright/test/cli.js"),
               "test", "--config", str(REPO / "scripts/playwright.release.config.mts")]
        if args.list:
            cmd.append("--list")
        code = run_logged(cmd, cwd=REPO / "frontend", env=env,
                          log=output / "playwright.log", redact=(key,))
        if code:
            return code
        if not args.list:
            counts = junit_counts(xml, no_skips=True)
            (output / "counts.json").write_text(json.dumps(counts, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

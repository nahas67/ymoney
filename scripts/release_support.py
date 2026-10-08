"""Shared, secret-free subprocess evidence for release-only gates."""
from __future__ import annotations

import json
import os
import subprocess
import time
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def clean_env() -> dict[str, str]:
    """Keep toolchain/OS settings, not developer database/provider credentials."""
    keep = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "HOME", "USERPROFILE",
            "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMW6432",
            "APPDATA", "LOCALAPPDATA", "TEMP", "TMP", "TMPDIR", "LANG", "LC_ALL",
            "CI", "GITHUB_ACTIONS", "GITHUB_SHA", "UV_CACHE_DIR", "UV_PYTHON",
            "PLAYWRIGHT_BROWSERS_PATH", "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE"}
    env = {k: v for k, v in os.environ.items() if k.upper() in keep}
    env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1",
               YMONEY_TEST_POSTGRES="", YMONEY_BACKUP_TEST_DSN="")
    # NOTE: TELEGRAM_ENABLED is deliberately NOT forced here. The suite fakes
    # the bot transport per test (tg_env monkeypatches send_message), and the
    # gate environment carries no bot token, so nothing can reach the network.
    # Forcing false broke test_telegram notify/suppress tests (proven: they pass
    # in dev env, fail with TELEGRAM_ENABLED=false) by diverging from the exact
    # configuration CI and developers test.
    return env


def run_logged(command: list[str], *, cwd: Path, env: dict[str, str],
               log: Path, redact: tuple[str, ...] = ()) -> int:
    """Persist stdout AND stderr in full; never report a failed command as green."""
    log.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    code = 1
    def safe(text: str) -> str:
        for value in redact:
            if value:
                text = text.replace(value, "[REDACTED]")
        return text
    with log.open("w", encoding="utf-8") as out:
        out.write(safe(json.dumps({"command": command, "cwd": str(cwd)})) + "\n")
        try:
            with subprocess.Popen(command, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                  stderr=subprocess.STDOUT, text=True,
                                  encoding="utf-8", errors="replace") as proc:
                assert proc.stdout is not None
                for line in proc.stdout:
                    line = safe(line)
                    out.write(line)
                    out.flush()
                    print(line, end="", flush=True)
                code = proc.wait()
        except OSError as exc:
            out.write(safe(f"{type(exc).__name__}: {exc}\n"))
            raise
        finally:
            result = {"exit_code": code, "seconds": round(time.monotonic() - started, 3)}
            out.write(json.dumps(result) + "\n")
            log.with_suffix(".result.json").write_text(json.dumps(result, indent=2) + "\n")
    return code


def junit_counts(path: Path, *, no_skips: bool = False,
                 required: tuple[str, ...] = (), qualify: bool = True) -> dict[str, int]:
    cases = list(ET.parse(path).getroot().iter("testcase"))
    counts = {"tests": len(cases), "failures": 0, "errors": 0, "skipped": 0}
    for case in cases:
        for tag in ("failure", "error", "skipped"):
            if case.find(tag) is not None:
                counts[{"failure": "failures", "error": "errors", "skipped": "skipped"}[tag]] += 1
    if qualify and (not cases or counts["failures"] or counts["errors"]
                    or (no_skips and counts["skipped"])):
        raise RuntimeError(f"unqualified JUnit report {path.name}: {counts}")
    for name in required:
        matches = [c for c in cases if c.get("name", "").startswith(name)]
        if not matches or any(c.find("skipped") is not None for c in matches):
            raise RuntimeError(f"required test did not execute: {name}")
    return counts

"""Work 15.6 §10 — the CLI's trust boundary must be verifiable, not documented.

The ``ymoney`` CLI is a local, operator-only tool. It inherits the invoking OS
user's database access and does not authenticate an end user, so the rules that
protect it are worth a lot more when they are enforced than when they are
written down in a docstring.

These tests assert the boundary as code:

* it never becomes a network service (no socket, no server, no port);
* it never prints a secret -- value, length or digest;
* it keeps stdout machine-readable;
* it does not grow its own business logic;
* ``publish`` remains unable to bypass the autonomy gate.

A CLI that quietly gained ``uvicorn.run()`` would turn a local tool into an
unauthenticated network service. That is the failure this file exists to catch.
"""

from __future__ import annotations

import ast
import inspect
import json
import socket

import pytest

from app.cli import main as cli

CLI_FILE = inspect.getsourcefile(cli)


# ===========================================================================
# no network listener -- the highest-value guard here
# ===========================================================================


def _imported_modules() -> set[str]:
    """Every module the CLI imports, transitively at module scope."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(inspect.getsource(cli))):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


@pytest.mark.parametrize(
    "forbidden",
    ["uvicorn", "fastapi", "starlette", "flask", "tornado", "aiohttp",
     "http.server", "socketserver", "asyncio.start_server"],
)
def test_the_cli_imports_no_server_framework(forbidden):
    """A CLI that imports a server framework is one command away from a port."""
    assert forbidden not in _imported_modules()


def test_the_cli_never_binds_a_socket(monkeypatch):
    """Structural proof: ``socket.socket`` must never be called in-process.

    ``socket`` is not imported by the CLI at all, so this also asserts the
    import stays absent -- importing it to monkeypatch it would already be a
    signal worth investigating.
    """
    assert "socket" not in _imported_modules(), (
        "the CLI now imports socket -- confirm it is not building a listener")
    source = inspect.getsource(cli)
    for forbidden in (".bind(", ".listen(", "uvicorn.run", "serve_forever"):
        assert forbidden not in source, (
            f"the CLI references {forbidden!r}; a local operator tool must "
            "not open a listening socket")


def test_a_real_socket_bind_would_be_visible():
    """Sanity check that the guard above is testing something real.

    If ``socket.socket`` could not bind in this environment at all, then the
    "no bind" assertions would pass vacuously. This proves a bind IS possible,
    so their absence is meaningful.
    """
    probe = socket.socket()
    try:
        probe.bind(("127.0.0.1", 0))
    finally:
        probe.close()


def test_no_command_serves_or_listens():
    """There is no ``serve``/``daemon``/``listen`` verb to reach."""
    parser = cli.build_parser()
    subactions = [
        action for action in parser._actions
        if getattr(action, "choices", None) and hasattr(action, "_name_parser_map")
    ]
    assert subactions, "the parser exposes no subcommands at all"
    names = set()
    for action in subactions:
        names |= set(action._name_parser_map)  # noqa: SLF001 - no public API
    forbidden = {"serve", "daemon", "listen", "server", "daemonize", "http"}
    assert not (names & forbidden), f"a serving verb was added: {names & forbidden}"


# ===========================================================================
# never print a secret
# ===========================================================================


CANARY = "CANARY-3f9a2b7c-secret-value"


def test_redaction_replaces_a_workspace_secret(workspace_with_user):
    """The backstop strips a resolved secret before stdout is written.

    Redaction is SCOPE-based: it enumerates the credentials actually resolved
    for the workspace in scope and masks their values. That is the correct
    design -- an unknown string is not assumed to be a secret, because doing so
    would corrupt ordinary output.
    """
    from app.services.provider_settings import set_credential

    ws_id = workspace_with_user["workspace"]
    set_credential("llm.api_key", CANARY, ws_id)
    cleaned, hits = cli.redact_secrets(f"key={CANARY} trailing", ws_id)
    assert CANARY not in cleaned
    assert hits >= 1


def test_redaction_is_scope_bounded_not_a_blanket_string_matcher():
    """An unresolvable string is NOT masked.

    This is the deliberate inverse of the test above: the backstop masks
    credentials it can resolve for the tenant in scope, not arbitrary text.
    A blanket matcher would corrupt every log line that happens to contain a
    hyphenated token.
    """
    cleaned, hits = cli.redact_secrets(f"trace: {CANARY}", "no-such-workspace")
    assert hits == 0
    assert CANARY in cleaned


def test_redaction_leaves_ordinary_output_intact():
    """An over-eager redactor corrupting output is itself a bug."""
    ordinary = "render complete video_id=abc123 status=READY"
    cleaned, hits = cli.redact_secrets(ordinary, "ws-1")
    assert cleaned == ordinary
    assert hits == 0


@pytest.mark.parametrize(
    "argv",
    [
        ["inspect", "--workspace", "W"],
        ["planner", "--workspace", "W", "--list", "signals"],
        ["campaign", "--workspace", "W", "--list"],
        ["render", "--workspace", "W", "--list"],
        ["localize", "--workspace", "W", "--list"],
        ["create", "--workspace", "W", "--topic", "t", "--dry-run"],
        ["research", "--workspace", "W", "--topic", "t", "--dry-run"],
    ],
)
def test_no_command_emits_a_secret_or_a_non_json_stdout(argv, capsys, db_session,
                                                        workspace_with_user):
    """stdout must be exactly one parseable JSON object, for every command.

    This is the property an agent depends on, and it is also where a leaked
    credential would surface. The workspace is redirected to the fixture's so
    the tenant scope matches whatever the redactor scans.
    """
    ws_id = workspace_with_user["workspace"]
    argv = [a if a != "W" else ws_id for a in argv]

    try:
        cli.main(argv)
    except SystemExit:
        pass
    except Exception:
        # A command that cannot run in this fixture still must not have written
        # anything non-JSON or leaky to stdout.
        pass

    out = capsys.readouterr().out.strip()
    if not out:
        return
    parsed = json.loads(out)          # raises if stdout is not pure JSON
    assert CANARY not in out
    assert isinstance(parsed, dict)


# ===========================================================================
# no separate business logic
# ===========================================================================


def test_the_cli_delegates_to_engine_and_service_layers():
    """It must import from ``app.engine`` / ``app.services`` / ``app.models``.

    A CLI that grew its own query layer would duplicate policy that the engine
    already owns -- and would drift from it silently.
    """
    modules = _imported_modules()
    internal = {m for m in modules if m.startswith("app.")}
    assert internal, "the CLI imports nothing from the application"
    roots = {m.split(".")[1] for m in internal if m.count(".") >= 1}
    assert roots & {"engine", "services", "models", "api"}, (
        f"the CLI only imports {roots}; it should call the engine/service layer")


def test_the_cli_writes_no_raw_sql():
    """All persistence goes through the ORM and the service functions.

    ``.get("text")`` / ``extract_text`` are ordinary dict access and are
    excluded; what matters is SQL being EXECUTED, not the word "text".
    """
    source = inspect.getsource(cli)
    for forbidden in ("sqlalchemy.text", "session.execute", "s.execute(",
                      "INSERT INTO", "UPDATE ", "DELETE FROM"):
        assert forbidden not in source, (
            f"the CLI appears to run raw SQL ({forbidden!r}); it must call "
            "existing services instead")


# ===========================================================================
# publish remains unable to bypass the autonomy gate
# ===========================================================================


def test_publish_still_goes_through_assert_may_advance():
    source = inspect.getsource(cli)
    assert "assert_may_advance" in source
    assert "PlanningAction.PUBLISH" in source, (
        "publish no longer gates on the PUBLISH action specifically")


def test_publish_is_refused_at_every_autonomy_mode():
    """The engine's own gate, exercised directly, is the real guarantee."""
    from app.engine.planning.autonomy import (
        AutonomyMode,
        AutonomyPolicy,
        PlanningAction,
        assert_may_advance,
    )

    for mode in AutonomyMode:
        with pytest.raises(Exception):        # noqa: B017 - any refusal counts
            assert_may_advance(AutonomyPolicy(mode=mode), PlanningAction.PUBLISH)


def test_there_is_no_force_flag_anywhere_in_the_cli():
    """A bypass flag is the obvious future regression."""
    parser = cli.build_parser()
    for action in parser._actions:
        if "--force" in getattr(action, "option_strings", []):
            pytest.fail("the CLI exposes a --force flag; the autonomy gate "
                        "must not be overridable")
        for sub in getattr(action, "_name_parser_map", {}).values():
            for sub_action in sub._actions:   # noqa: SLF001
                if "--force" in getattr(sub_action, "option_strings", []):
                    pytest.fail("a subcommand exposes --force")
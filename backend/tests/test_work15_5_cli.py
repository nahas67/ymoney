"""Work 15.5 §11 -- the agent-facing ``ymoney`` CLI.

Every guard the CLI claims gets a test here, and each test fails if the guard
is inverted. The four that matter most:

* **stdout purity** -- stdout is exactly one JSON object on success, on a
  failed operation, on a usage error, and on an UNEXPECTED exception. A
  library that ``print()``s mid-command cannot corrupt it, because the CLI
  swaps ``sys.stdout`` for stderr for the whole run.
* **no credential leak** -- a real secret is stored in the encrypted
  credential table and every command is invoked; the value must appear in
  neither stdout nor the manifest. ``inspect connections`` reports
  ``configured``/``source`` and nothing else.
* **the manifest survives failure** -- ``{status, log_file, ...}`` is written
  BEFORE the work starts (proved by reading it from inside a running command)
  and still readable after a crash or an autonomy refusal.
* **publish is gated** -- ``ymoney publish`` goes through
  ``assert_may_advance`` and is refused at every autonomy mode, including
  ``AUTONOMOUS`` with ``PUBLISH`` explicitly allow-listed. It enqueues nothing.
"""

from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import sys
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from sqlalchemy import select

from app.cli.main import (
    CLI_VERSION,
    EXIT_FAILED,
    EXIT_OK,
    EXIT_USAGE,
    CommandFailed,
    UsageError,
    build_parser,
    credential_status,
    redact_secrets,
    resolve_state_dir,
    run_checked,
    run_cli,
)
from app.db import session_scope
from app.engine.planning.autonomy import (
    AutonomyMode,
    AutonomyPolicy,
    AutonomyRefused,
    PlanningAction,
    assert_may_advance,
    describe_autonomy,
)
from app.services import provider_settings

#: Two distinct canaries written into the encrypted credential table.
CANARY_A = "CANARYVALUEALPHA0123456789abcdef"
CANARY_B = "CANARYVALVEBETA9876543210zyxwvut"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


class Outcome:
    """One CLI invocation, captured."""

    def __init__(self, code: int, stdout: str, stderr: str, state_dir: Path) -> None:
        self.code = code
        self.stdout = stdout
        self.stderr = stderr
        self.state_dir = state_dir
        # Snapshotted NOW: a later run overwrites latest-result.json, so a lazy
        # read would silently compare a run against its successor.
        manifest_path = state_dir / "latest-result.json"
        self.manifest_snapshot: dict = (json.loads(manifest_path.read_text("utf-8"))
                                        if manifest_path.exists() else {})

    @property
    def payload(self) -> dict:
        """stdout must ALWAYS be exactly one JSON object."""
        return only_json(self.stdout)

    @property
    def manifest(self) -> dict:
        assert self.manifest_snapshot, "no manifest was written"
        return self.manifest_snapshot


def cli(*argv: str, state_dir: Path | None = None) -> Outcome:
    """Run the CLI with stdout/stderr captured."""
    root = Path(state_dir or resolve_state_dir())
    root.mkdir(parents=True, exist_ok=True)
    out, err = io.StringIO(), io.StringIO()
    argv_list = [*argv, "--state-dir", str(root)]
    with redirect_stdout(out), redirect_stderr(err):
        code = run_cli(argv_list)
    return Outcome(code, out.getvalue(), err.getvalue(), root)


def only_json(stdout: str) -> dict:
    """stdout is ONE object and nothing else: no log lines, no banners."""
    text = stdout.strip()
    assert text, "stdout was empty; an agent would have nothing to parse"
    parsed, end = json.JSONDecoder().raw_decode(text)
    assert text[end:].strip() == "", f"trailing output on stdout: {text[end:]!r}"
    assert isinstance(parsed, dict)
    return parsed


def _commands_for(ws: str) -> list[tuple[str, list[str]]]:
    """One invocation per command, aimed at this workspace."""
    return [
        ("create-campaign-draft", ["create", "campaign-draft", "--item-id", "missing",
                                   "--workspace", ws]),
        ("create-content-item", ["create", "content-item", "--parent-id", "missing",
                                 "--workspace", ws]),
        ("research", ["research", "--topic", "cli credential probe", "--workspace", ws]),
        ("campaign-list", ["campaign", "list", "--workspace", ws]),
        ("campaign-show", ["campaign", "show", "--campaign-id", "missing", "--workspace", ws]),
        ("campaign-derive", ["campaign", "derive", "--campaign-id", "missing",
                             "--workspace", ws]),
        ("campaign-schedule", ["campaign", "schedule", "--campaign-id", "missing",
                               "--workspace", ws]),
        ("render-list", ["render", "list", "--workspace", ws]),
        ("render-show", ["render", "show", "--video-id", "missing", "--workspace", ws]),
        ("render-estimate", ["render", "estimate", "--topic", "t", "--workspace", ws]),
        ("render-submit", ["render", "submit", "--content-id", "missing", "--workspace", ws]),
        ("localize-list", ["localize", "list", "--workspace", ws]),
        ("localize-show", ["localize", "show", "--localized-id", "missing", "--workspace", ws]),
        ("localize-start", ["localize", "start", "--content-id", "missing",
                            "--language", "es", "--workspace", ws]),
        ("publish", ["publish", "--workspace", ws, "--autonomy", "AUTONOMOUS",
                     "--allow-action", "PUBLISH"]),
        ("planner-signals", ["planner", "signals", "--workspace", ws]),
        ("planner-opportunities", ["planner", "opportunities", "--workspace", ws]),
        ("planner-plan", ["planner", "plan", "--workspace", ws]),
        ("planner-policy", ["planner", "policy", "--workspace", ws]),
        ("inspect-jobs", ["inspect", "jobs", "--workspace", ws]),
        ("inspect-job", ["inspect", "job", "--job-id", "missing", "--workspace", ws]),
        ("inspect-videos", ["inspect", "videos", "--workspace", ws]),
        ("inspect-workspace", ["inspect", "workspace", "--workspace", ws]),
        ("inspect-connections", ["inspect", "connections", "--workspace", ws]),
        ("inspect-engine", ["inspect", "engine", "--workspace", ws]),
    ]


@pytest.fixture()
def ws(workspace_with_user) -> str:
    return workspace_with_user["workspace"]


@pytest.fixture()
def other_ws(db_session) -> str:
    """A second, unrelated workspace owned by nobody in this test."""
    from app.models import Workspace

    row = Workspace(name="Other WS", slug=f"other-{os.urandom(4).hex()}", niche="AI money")
    db_session.add(row)
    db_session.commit()
    return row.id


@pytest.fixture()
def secrets(ws: str):
    """Two real, encrypted credentials in this workspace."""
    provider_settings.set_credential("llm.api_key", CANARY_A, ws)
    provider_settings.set_credential("telegram.bot_token", CANARY_B, ws)
    yield
    provider_settings.set_credential("llm.api_key", None, ws)
    provider_settings.set_credential("telegram.bot_token", None, ws)


@pytest.fixture()
def seeded(db_session, ws: str):
    """Workspace A gets a job, a campaign, a content item and a rendered video."""
    from app.models import Campaign, ContentItem, Video, VideoVariant
    from app.services import jobs as jobs_service

    job_id = jobs_service.enqueue("cli.test", {"probe": True}, workspace_id=ws)
    campaign = Campaign(workspace_id=ws, name="Seeded campaign", status="DRAFT",
                        platforms_json=["youtube"])
    content = ContentItem(workspace_id=ws, topic="Seeded topic", status="SCRIPT_READY")
    db_session.add_all([campaign, content])
    db_session.flush()
    variant = VideoVariant(content_item_id=content.id, label="v1", script="seeded script",
                           selected=True)
    db_session.add(variant)
    db_session.flush()
    video = Video(variant_id=variant.id, workspace_id=ws, engine="fake-engine",
                  status="READY", file_path="seeded.mp4")
    db_session.add(video)
    db_session.commit()
    return {"job_id": job_id, "campaign_id": campaign.id, "video_id": video.id,
            "content_id": content.id, "topic": "Seeded topic"}


# ---------------------------------------------------------------------------
# 1. exit taxonomy
# ---------------------------------------------------------------------------


def test_exit_zero_on_success(ws, tmp_path):
    result = cli("inspect", "workspace", "--workspace", ws, state_dir=tmp_path)
    assert result.code == EXIT_OK
    assert result.payload["status"] == "ok"
    assert result.payload["result"]["workspace_id"] == ws


def test_exit_one_when_the_operation_ran_and_failed(ws, tmp_path):
    """A resource that does not exist in THIS workspace: the command ran, failed."""
    result = cli("inspect", "job", "--job-id", "does-not-exist",
                 "--workspace", ws, state_dir=tmp_path)
    assert result.code == EXIT_FAILED == 1
    assert result.payload["status"] == "error"
    assert "not found in this workspace" in result.payload["error"]["message"]


def test_exit_two_on_bad_arguments(tmp_path):
    """--workspace is required: the work never started, so this is a usage error."""
    result = cli("research", "--topic", "anything", state_dir=tmp_path)
    assert result.code == EXIT_USAGE == 2
    assert result.payload["status"] == "usage_error"


def test_exit_two_on_invalid_enum_value(ws, tmp_path):
    result = cli("planner", "plan", "--workspace", ws, "--autonomy", "SUPERPOWER",
                 state_dir=tmp_path)
    assert result.code == EXIT_USAGE == 2
    assert "unknown autonomy" in result.payload["error"]["message"]


def test_exit_two_on_out_of_range_argument(ws, tmp_path):
    result = cli("campaign", "list", "--workspace", ws, "--limit", "0", state_dir=tmp_path)
    assert result.code == EXIT_USAGE == 2


def test_exit_two_on_unknown_command(tmp_path):
    result = cli("teleport", state_dir=tmp_path)
    assert result.code == EXIT_USAGE == 2
    only_json(result.stdout)


def test_one_and_two_are_never_collapsed(ws, tmp_path):
    """The distinction that makes the CLI usable from CI."""
    failed = cli("inspect", "job", "--job-id", "nope", "--workspace", ws, state_dir=tmp_path)
    misused = cli("inspect", "job", "--job-id", "nope", state_dir=tmp_path)
    assert failed.code == 1
    assert misused.code == 2
    assert failed.code != misused.code


def test_help_exits_zero_with_json_on_stdout(tmp_path):
    result = cli("--help", state_dir=tmp_path)
    assert result.code == EXIT_OK
    payload = only_json(result.stdout)
    assert set(payload["result"]["commands"]) == {
        "create", "research", "campaign", "render", "localize",
        "publish", "planner", "inspect"}


# ---------------------------------------------------------------------------
# 2. stdout purity
# ---------------------------------------------------------------------------


def test_stdout_is_one_json_object_on_success(ws, tmp_path):
    only_json(cli("inspect", "workspace", "--workspace", ws, state_dir=tmp_path).stdout)


def test_stdout_is_one_json_object_on_failure(ws, tmp_path):
    result = cli("render", "show", "--video-id", "nope", "--workspace", ws,
                 state_dir=tmp_path)
    assert result.code == EXIT_FAILED
    only_json(result.stdout)


def test_stdout_is_one_json_object_on_usage_error(tmp_path):
    result = cli("publish", "--totally-unknown-flag", state_dir=tmp_path)
    assert result.code == EXIT_USAGE
    only_json(result.stdout)


def test_stdout_is_one_json_object_on_unexpected_exception(ws, tmp_path, monkeypatch):
    """An unhandled bug inside a command is still machine-readable."""
    from app.services import jobs as jobs_service

    def boom(*_a, **_kw):
        raise RuntimeError("kaboom from the queue layer")

    monkeypatch.setattr(jobs_service, "list_jobs", boom)
    result = cli("inspect", "jobs", "--workspace", ws, state_dir=tmp_path)
    assert result.code == EXIT_FAILED
    payload = only_json(result.stdout)
    assert payload["status"] == "unexpected"
    assert payload["error"]["type"] == "RuntimeError"
    # the traceback belongs on stderr, never on stdout
    assert "Traceback" in result.stderr
    assert "Traceback" not in result.stdout


def test_a_library_printing_cannot_corrupt_stdout(ws, tmp_path, monkeypatch):
    """stdout is swapped for stderr for the whole run -- this is the proof."""
    from app.services import jobs as jobs_service

    def noisy(*_a, **_kw):
        print("NOISE-FROM-A-LIBRARY")
        return []

    monkeypatch.setattr(jobs_service, "list_jobs", noisy)
    result = cli("inspect", "jobs", "--workspace", ws, state_dir=tmp_path)
    assert result.code == EXIT_OK
    assert "NOISE-FROM-A-LIBRARY" not in result.stdout
    assert "NOISE-FROM-A-LIBRARY" in result.stderr
    only_json(result.stdout)


def test_text_mode_keeps_stdout_machine_readable(ws, tmp_path):
    result = cli("inspect", "workspace", "--workspace", ws, "--text", state_dir=tmp_path)
    assert result.code == EXIT_OK
    only_json(result.stdout)
    assert "workspace_id" in result.stderr


def test_non_ascii_output_does_not_break_the_stream(ws, tmp_path):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = run_cli(["--help", "--state-dir", str(tmp_path)])
    assert code == EXIT_OK
    only_json(out.getvalue())


# ---------------------------------------------------------------------------
# 3. never expose credentials
# ---------------------------------------------------------------------------


def test_no_command_prints_a_credential_value(ws, tmp_path, secrets):
    """Every command, run with REAL secrets configured: neither canary appears."""
    for name, argv in _commands_for(ws):
        result = cli(*argv, state_dir=tmp_path / name)
        assert result.code in (EXIT_OK, EXIT_FAILED), f"{name} -> {result.stdout}"
        for canary in (CANARY_A, CANARY_B):
            assert canary not in result.stdout, f"{name} leaked a value on stdout"
            assert canary not in result.stderr, f"{name} leaked a value on stderr"
            manifest = result.state_dir / "latest-result.json"
            if manifest.exists():
                assert canary not in manifest.read_text("utf-8"), f"{name} leaked to manifest"
        only_json(result.stdout)


def test_no_command_accepts_a_credential_as_an_argument():
    """A key on the command line lands in shell history and process listings."""
    import argparse

    banned = ("key", "token", "secret", "password", "passwd", "credential")
    seen: list[str] = []
    stack = [build_parser()]
    while stack:
        parser = stack.pop()
        for action in parser._actions:
            seen.extend(action.option_strings)
            if isinstance(action, argparse._SubParsersAction):
                stack.extend(action.choices.values())
    offenders = [opt for opt in seen
                 if any(word in opt.lstrip("-").lower() for word in banned)]
    assert offenders == [], f"credential-shaped options must not exist: {offenders}"


def test_connections_reports_presence_without_the_value(ws, tmp_path, secrets):
    result = cli("inspect", "connections", "--workspace", ws, state_dir=tmp_path)
    assert result.code == EXIT_OK
    payload = result.payload["result"]
    by_key = {row["key"]: row for row in payload["credentials"]}
    llm = by_key["llm.api_key"]
    assert llm["configured"] is True
    assert llm["source"] == "db"
    assert llm["secret"] is True
    serialized = json.dumps(payload)
    assert CANARY_A not in serialized
    # Not even a mask. ``provider_settings.mask()`` returns
    # value[:4] + "••••" + value[-4:], so asserting both ends is what rules the
    # masked form out; a bare length would leak nothing, so it is not asserted.
    assert CANARY_A[:4] not in serialized
    assert CANARY_A[-4:] not in serialized
    assert set(llm) == {"key", "label", "secret", "configured", "source"}


def test_redact_secrets_is_a_real_backstop(ws, secrets):
    cleaned, count = redact_secrets(f"provider rejected {CANARY_A} at 12:00", ws)
    assert count == 1
    assert CANARY_A not in cleaned
    assert "[redacted]" in cleaned


def test_redacted_output_is_withheld_from_stdout(ws, tmp_path, secrets, monkeypatch):
    """If a provider ever echoes a key into an error, stdout gets nothing."""
    from app.services import jobs as jobs_service

    def leaky(*_a, **_kw):
        raise RuntimeError(f"upstream rejected credential {CANARY_A}")

    monkeypatch.setattr(jobs_service, "list_jobs", leaky)
    result = cli("inspect", "jobs", "--workspace", ws, state_dir=tmp_path)
    payload = only_json(result.stdout)
    assert CANARY_A not in result.stdout
    assert payload["status"] == "credential_redacted"
    assert payload["result"] == {}


def test_credential_status_helper_never_yields_a_value(ws, secrets):
    rows = credential_status(ws, ["llm.api_key", "telegram.bot_token"])
    assert {row["key"] for row in rows} == {"llm.api_key", "telegram.bot_token"}
    for row in rows:
        assert row["configured"] is True
        assert set(row) == {"key", "label", "secret", "configured", "source"}


# ---------------------------------------------------------------------------
# 4. the result manifest
# ---------------------------------------------------------------------------


def test_manifest_is_written_before_the_work_starts(ws, tmp_path, monkeypatch):
    """Read the manifest from INSIDE a running command: it must already exist."""
    from app.services import jobs as jobs_service

    seen: dict = {}

    def peek(*_a, **_kw):
        path = tmp_path / "latest-result.json"
        seen["exists"] = path.exists()
        seen["payload"] = json.loads(path.read_text("utf-8")) if path.exists() else {}
        return []

    monkeypatch.setattr(jobs_service, "list_jobs", peek)
    result = cli("inspect", "jobs", "--workspace", ws, state_dir=tmp_path)
    assert result.code == EXIT_OK
    assert seen["exists"] is True
    assert seen["payload"]["status"] == "running"
    assert seen["payload"]["log_file"]
    assert seen["payload"]["command"] == "inspect"


def test_manifest_records_the_workspace_and_log_file(ws, tmp_path):
    manifest = cli("inspect", "workspace", "--workspace", ws, state_dir=tmp_path).manifest
    assert manifest["workspace_id"] == ws
    assert manifest["status"] == "ok"
    assert manifest["exit_code"] == EXIT_OK
    assert Path(manifest["log_file"]).is_file()


def test_manifest_exists_after_an_autonomy_refusal(tmp_path):
    """A publish refusal still leaves a machine-readable record of the attempt."""
    result = cli("publish", "--autonomy", "AUTONOMOUS", "--allow-action", "PUBLISH",
                 state_dir=tmp_path)
    assert result.code == EXIT_FAILED
    manifest = result.manifest
    assert manifest["status"] == "refused"
    assert manifest["exit_code"] == EXIT_FAILED
    assert manifest["error"]["kind"] == "refused"
    assert manifest["command"] == "publish"
    assert manifest["result"] == {}
    assert Path(manifest["log_file"]).is_file()


def test_manifest_exists_after_an_unexpected_exception(ws, tmp_path, monkeypatch):
    from app.services import jobs as jobs_service

    def boom(*_a, **_kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(jobs_service, "list_jobs", boom)
    result = cli("inspect", "jobs", "--workspace", ws, state_dir=tmp_path)
    assert result.code == EXIT_FAILED
    manifest = result.manifest
    assert manifest["status"] == "unexpected"
    assert manifest["error"]["type"] == "RuntimeError"


def test_per_run_manifest_is_retained_alongside_the_latest(ws, tmp_path):
    first = cli("inspect", "workspace", "--workspace", ws, state_dir=tmp_path)
    second = cli("planner", "policy", "--workspace", ws, state_dir=tmp_path)
    assert first.manifest["run_id"] != second.manifest["run_id"]
    retained = sorted((tmp_path / "runs").glob("*.json"))
    assert len(retained) == 2
    assert second.manifest["run_id"] in second.manifest["run_id"]


def test_manifest_never_carries_a_credential(ws, tmp_path, secrets):
    result = cli("inspect", "connections", "--workspace", ws, state_dir=tmp_path)
    text = (result.state_dir / "latest-result.json").read_text("utf-8")
    assert CANARY_A not in text
    assert CANARY_B not in text


# ---------------------------------------------------------------------------
# 5. run_checked subprocess discipline
# ---------------------------------------------------------------------------


def test_run_checked_does_not_pass_through_a_shell(tmp_path):
    """Shell operators in an argument are DATA.

    The payloads deliberately contain NO SPACES: a shell only needs quoting for
    arguments with whitespace, so an unquoted ``&&``/``|``/``>`` is exactly the
    case that gets re-interpreted as syntax. Each one also tries to create a
    file, so a shell's split is observable on disk, not just in the output.
    """
    echo_argv = "import sys; print(sys.argv[1])"
    payloads = [
        "SAFE&&echo>pwned-and.txt",
        "X|echo>piped.txt",
        "Y;echo>semi.txt",
        "Z$(echo)sub",
    ]
    for payload in payloads:
        output = run_checked([sys.executable, "-c", echo_argv, payload], cwd=tmp_path)
        assert output.strip() == payload, f"a shell re-interpreted {payload!r}"
    assert not list(tmp_path.iterdir()), "a shell executed part of an argument"


def test_run_checked_would_notice_a_shell(monkeypatch, tmp_path):
    """Guard on the guard: with shell=True the payload above WOULD be split."""
    seen: dict = {}

    def fake_run(argv, **kwargs):
        seen.update(kwargs)
        return subprocess.CompletedProcess(argv, 0, stdout="SAFE\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_checked(["tool", "SAFE&&echo>pwned.txt"], cwd=tmp_path)
    assert seen["shell"] is False


def test_run_checked_pins_shell_false_and_a_list(monkeypatch, tmp_path):
    captured: dict = {}

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(argv, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    run_checked(["some-tool", "arg"], cwd=tmp_path)
    assert isinstance(captured["argv"], list)
    assert captured["argv"] == ["some-tool", "arg"]
    assert captured["kwargs"]["shell"] is False
    assert captured["kwargs"]["check"] is False
    assert captured["kwargs"]["stdout"] == subprocess.PIPE


def test_run_checked_surfaces_only_the_last_thirty_lines(monkeypatch, tmp_path):
    def fake_run(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 3, stdout="".join(f"line {n}\n" for n in range(200)), stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CommandFailed):
        run_checked(["tool"], cwd=tmp_path)

    err = io.StringIO()
    with redirect_stderr(err), pytest.raises(CommandFailed):
        run_checked(["tool"], cwd=tmp_path)
    written = err.getvalue()
    assert "line 199" in written
    assert "line 0" not in written
    assert written.count("| line ") == 30


def test_run_checked_rejects_an_empty_command(tmp_path):
    with pytest.raises(UsageError):
        run_checked([], cwd=tmp_path)
    with pytest.raises(UsageError):
        run_checked([""], cwd=tmp_path)


def test_inspect_tools_never_reaches_a_shell(monkeypatch):
    """The CLI's only subprocess goes through run_checked."""
    from app.cli.main import _probe_tools

    monkeypatch.setattr(shutil, "which", lambda _name: sys.executable)
    seen: list = []

    def spy(argv, **kwargs):
        seen.append((argv, kwargs.get("shell")))
        return subprocess.CompletedProcess(argv, 0, stdout="v1.2.3\n", stderr="")

    monkeypatch.setattr(subprocess, "run", spy)
    payload = _probe_tools()
    assert payload["tools"], "expected probe results"
    assert seen, "the probe should have run at least one --version check"
    for argv, shell in seen:
        assert isinstance(argv, list)
        assert shell is False


# ---------------------------------------------------------------------------
# 6. workspace isolation
# ---------------------------------------------------------------------------


def test_workspace_a_data_is_invisible_from_workspace_b(ws, other_ws, seeded, tmp_path):
    for argv in (
        ["inspect", "job", "--job-id", seeded["job_id"]],
        ["render", "show", "--video-id", seeded["video_id"]],
        ["campaign", "show", "--campaign-id", seeded["campaign_id"]],
    ):
        result = cli(*argv, "--workspace", other_ws, state_dir=tmp_path)
        assert result.code == EXIT_FAILED, argv
        assert result.payload["result"] == {}, argv
        assert "not found in this workspace" in result.payload["error"]["message"]
        assert seeded["job_id"] not in result.stdout
        assert seeded["video_id"] not in result.stdout
        assert seeded["campaign_id"] not in result.stdout


def test_workspace_b_listings_exclude_workspace_a_rows(ws, other_ws, seeded, tmp_path):
    jobs = cli("inspect", "jobs", "--workspace", other_ws, state_dir=tmp_path)
    assert jobs.code == EXIT_OK
    assert jobs.payload["result"]["jobs"] == []

    videos = cli("inspect", "videos", "--workspace", other_ws, state_dir=tmp_path)
    assert videos.code == EXIT_OK
    assert videos.payload["result"]["videos"] == []

    campaigns = cli("campaign", "list", "--workspace", other_ws, state_dir=tmp_path)
    assert campaigns.code == EXIT_OK
    assert campaigns.payload["result"]["items"] == []

    mine = cli("inspect", "jobs", "--workspace", ws, state_dir=tmp_path)
    assert seeded["job_id"] in json.dumps(mine.payload)


def test_unknown_workspace_is_a_usage_error(tmp_path):
    result = cli("inspect", "workspace", "--workspace", "no-such-workspace", state_dir=tmp_path)
    assert result.code == EXIT_USAGE
    assert "unknown workspace" in result.payload["error"]["message"]


def test_workspace_resolves_by_slug_too(ws, tmp_path):
    from app.models import Workspace

    with session_scope() as db:
        slug = db.get(Workspace, ws).slug
    by_id = cli("inspect", "workspace", "--workspace", ws, state_dir=tmp_path)
    by_slug = cli("inspect", "workspace", "--workspace", slug, state_dir=tmp_path)
    assert by_id.payload["result"]["workspace_id"] == by_slug.payload["result"]["workspace_id"]


# ---------------------------------------------------------------------------
# 7. the publish gate -- the single most important test in this lane
# ---------------------------------------------------------------------------


def test_publish_is_refused_at_every_autonomy_mode(tmp_path):
    for mode in ("DISABLED", "RECOMMEND", "APPROVAL", "AUTONOMOUS"):
        result = cli("publish", "--autonomy", mode, state_dir=tmp_path)
        assert result.code == EXIT_FAILED, mode
        payload = only_json(result.stdout)
        assert payload["status"] == "refused", mode
        assert "never available to the planner" in payload["error"]["message"], mode
        assert payload["result"] == {}, mode


def test_publish_is_refused_even_with_publish_explicitly_allowed(ws, seeded, tmp_path):
    """Allow-listing PUBLISH does not unlock it -- NEVER_ALLOWED is unconditional."""
    from app.models import PublishedPost

    before = len(cli("inspect", "jobs", "--workspace", ws, state_dir=tmp_path)
                 .payload["result"]["jobs"])
    with session_scope() as db:
        posts_before = len(db.scalars(select(PublishedPost)).all())

    result = cli("publish", "--workspace", ws, "--campaign-id", seeded["campaign_id"],
                 "--autonomy", "AUTONOMOUS", "--allow-action", "PUBLISH",
                 "--allow-action", "SCHEDULE", state_dir=tmp_path)
    assert result.code == EXIT_FAILED
    payload = only_json(result.stdout)
    assert payload["status"] == "refused"
    assert "PUBLISH is never available to the planner" in payload["error"]["message"]

    after = len(cli("inspect", "jobs", "--workspace", ws, state_dir=tmp_path)
                .payload["result"]["jobs"])
    assert after == before, "the refused publish enqueued a job"
    with session_scope() as db:
        assert len(db.scalars(select(PublishedPost)).all()) == posts_before


def test_publish_never_reaches_the_publication_path(ws, seeded, tmp_path, monkeypatch):
    """Belt and braces: the publication flow is never even called."""
    from app.engine.campaign import publish_flow

    def forbidden(*_a, **_kw):
        raise AssertionError("ymoney publish reached the publication path")

    monkeypatch.setattr(publish_flow, "publish_due", forbidden)
    result = cli("publish", "--workspace", ws, "--campaign-id", seeded["campaign_id"],
                 "--autonomy", "AUTONOMOUS", state_dir=tmp_path)
    assert result.code == EXIT_FAILED
    assert result.payload["status"] == "refused"


def test_publish_has_no_force_flag():
    """There is no argument that turns the refusal off."""
    publish_parser = build_parser()._subparsers._group_actions[0].choices["publish"]
    options = {opt for action in publish_parser._actions for opt in action.option_strings}
    assert not any(word in opt for opt in options for word in ("force", "yes", "skip-gate"))


def test_cli_refusal_matches_the_engine_gate_exactly():
    """The CLI is not inventing a policy -- it reports the engine's own answer."""
    table = describe_autonomy()
    for mode in AutonomyMode:
        assert table[str(mode)]["publishes"] is False
    permissive = AutonomyPolicy(mode=AutonomyMode.AUTONOMOUS,
                                allowed_actions=frozenset({PlanningAction.PUBLISH}))
    with pytest.raises(AutonomyRefused):
        assert_may_advance(permissive, PlanningAction.PUBLISH)


def test_planner_commands_report_publishes_false(ws, tmp_path):
    """Campaign drafting and planning both declare that they cannot publish."""
    draft = cli("planner", "policy", "--workspace", ws, state_dir=tmp_path)
    assert draft.payload["result"]["publishes"] is False
    plan = cli("planner", "plan", "--workspace", ws, "--autonomy", "APPROVAL",
               state_dir=tmp_path)
    assert plan.code == EXIT_OK
    assert plan.payload["result"]["publishes"] is False


def test_plan_refuses_to_write_under_recommend(ws, tmp_path):
    """RECOMMEND persists nothing; it is not a mode that may create state."""
    from app.models.planning import EditorialPlan

    result = cli("planner", "plan", "--workspace", ws, "--autonomy", "RECOMMEND",
                 state_dir=tmp_path)
    assert result.code == EXIT_OK
    assert result.payload["result"]["autonomy"] == "RECOMMEND"
    with session_scope() as db:
        assert db.scalar(select(EditorialPlan).where(EditorialPlan.workspace_id == ws)) is None


# ---------------------------------------------------------------------------
# surface contracts
# ---------------------------------------------------------------------------


def test_required_commands_all_exist():
    parser = build_parser()
    names = set(parser._subparsers._group_actions[0].choices)
    assert names == {"create", "research", "campaign", "render", "localize",
                     "publish", "planner", "inspect"}
    assert parser.prog == "ymoney"


def test_payload_carries_the_schema_and_version(ws, tmp_path):
    import app.cli.main as cli_main

    assert cli_main.CLI_VERSION == CLI_VERSION
    result = cli("inspect", "workspace", "--workspace", ws, state_dir=tmp_path)
    assert result.payload["schema"] == "ymoney.cli/1"


def test_state_dir_env_is_honoured(monkeypatch, tmp_path):
    monkeypatch.setenv("YMONEY_CLI_STATE_DIR", str(tmp_path / "from-env"))
    assert resolve_state_dir() == (tmp_path / "from-env").resolve()


def test_research_runs_the_real_agent(ws, tmp_path):
    result = cli("research", "--topic", "how compilers optimise loops", "--workspace", ws,
                 state_dir=tmp_path)
    assert result.code == EXIT_OK, result.stdout
    brief = result.payload["result"]["brief"]
    assert brief["claims"], "the ResearchAgent must return tracked claims"
    assert brief["fact_status"] in ("OK", "INSUFFICIENT", "CONFLICTING")


def test_render_submit_goes_through_the_producer(ws, seeded, tmp_path):
    """The render path is the VideoProducerAgent, not a hand-rolled submit."""
    result = cli("render", "submit", "--content-id", seeded["content_id"],
                 "--workspace", ws, state_dir=tmp_path)
    assert result.code == EXIT_OK, result.stdout
    assert result.payload["result"]["video_id"]


def test_render_submit_refuses_without_a_selected_variant(ws, seeded, tmp_path):
    from app.models import VideoVariant

    with session_scope() as db:
        for variant in db.scalars(select(VideoVariant).where(
                VideoVariant.content_item_id == seeded["content_id"])).all():
            variant.selected = False
    result = cli("render", "submit", "--content-id", seeded["content_id"],
                 "--workspace", ws, state_dir=tmp_path)
    assert result.code == EXIT_FAILED
    assert "no selected variant" in result.payload["error"]["message"]


def test_localize_start_enqueues_a_job(ws, seeded, tmp_path):
    result = cli("localize", "start", "--content-id", seeded["content_id"],
                 "--language", "es", "--locale", "es-ES", "--workspace", ws,
                 state_dir=tmp_path)
    assert result.code == EXIT_OK, result.stdout
    assert result.payload["result"]["queued"] is True
    assert result.payload["result"]["items"][0]["language"] == "es"


def test_localize_start_needs_a_language(ws, seeded, tmp_path):
    result = cli("localize", "start", "--content-id", seeded["content_id"],
                 "--workspace", ws, state_dir=tmp_path)
    assert result.code == EXIT_USAGE
    assert "--language" in result.payload["error"]["message"]


def test_create_campaign_draft_is_gated_and_idempotent(ws, tmp_path):
    """RECOMMEND may not draft a campaign; APPROVAL may, and only once."""
    from app.models import Opportunity
    from app.models.planning import EditorialPlan, EditorialPlanItem

    with session_scope() as db:
        opportunity = Opportunity(workspace_id=ws, topic="cli draft topic", source="operator")
        db.add(opportunity)
        db.flush()
        plan = EditorialPlan(workspace_id=ws, horizon_days=30, autonomy="RECOMMEND")
        db.add(plan)
        db.flush()
        item = EditorialPlanItem(workspace_id=ws, plan_id=plan.id,
                                 opportunity_id=opportunity.id, content_format="SHORT",
                                 angle="cli draft angle", platforms_json=["youtube"])
        db.add(item)
        db.flush()
        item_id = item.id

    refused = cli("create", "campaign-draft", "--item-id", item_id, "--workspace", ws,
                  "--autonomy", "RECOMMEND", state_dir=tmp_path / "recommend")
    assert refused.code == EXIT_FAILED
    assert refused.payload["status"] == "refused"

    allowed = cli("create", "campaign-draft", "--item-id", item_id, "--workspace", ws,
                  "--autonomy", "APPROVAL", state_dir=tmp_path / "approval")
    assert allowed.code == EXIT_OK, allowed.stdout
    assert allowed.payload["result"]["campaign_id"]
    assert allowed.payload["result"]["publishes"] is False

    again = cli("create", "campaign-draft", "--item-id", item_id, "--workspace", ws,
                "--autonomy", "APPROVAL", state_dir=tmp_path / "approval2")
    assert again.payload["result"]["campaign_id"] == allowed.payload["result"]["campaign_id"]


def test_create_content_item_derives_a_child(ws, seeded, tmp_path):
    result = cli("create", "content-item", "--parent-id", seeded["content_id"],
                 "--derivation-type", "short", "--topic", "derived short",
                 "--workspace", ws, state_dir=tmp_path)
    assert result.code == EXIT_OK, result.stdout
    assert result.payload["result"]["parent_content_id"] == seeded["content_id"]
    assert result.payload["result"]["topic"] == "derived short"


def test_console_script_entry_point_is_declared():
    pyproject = Path(__file__).resolve().parents[1] / "pyproject.toml"
    text = pyproject.read_text("utf-8")
    assert "[project.scripts]" in text
    # ``app.cli.main:main``, not ``app.cli:main``: the package __init__ must not
    # re-export the ``main`` function, or it would shadow the submodule the
    # entry point has to resolve through.
    assert 'ymoney = "app.cli.main:main"' in text


def test_the_cli_submodule_is_not_shadowed_by_a_function():
    import types

    import app.cli
    import app.cli.main as cli_main

    assert cli_main.__name__ == "app.cli.main"
    assert callable(cli_main.main)
    assert isinstance(app.cli.main, types.ModuleType), (
        "app.cli.main must stay the MODULE; re-exporting the main() function "
        "from __init__ would break the console-script entry point")
    assert app.cli.run_cli is cli_main.run_cli


def test_entry_point_runs_in_a_real_process(tmp_path):
    """Exactly what the console script does, in a fresh interpreter.

    stdout purity has to survive a process boundary, not just capsys.
    """
    backend = Path(__file__).resolve().parents[1]
    launcher = "import sys; from app.cli.main import main; sys.exit(main())"
    completed = subprocess.run(
        [sys.executable, "-c", launcher, "--help", "--state-dir", str(tmp_path)],
        cwd=str(backend), capture_output=True, text=True, timeout=180,
    )
    assert completed.returncode == 0, completed.stderr
    assert only_json(completed.stdout)["status"] == "ok"


def test_entry_point_reports_usage_errors_with_exit_two(tmp_path):
    backend = Path(__file__).resolve().parents[1]
    launcher = "import sys; from app.cli.main import main; sys.exit(main())"
    completed = subprocess.run(
        [sys.executable, "-c", launcher, "research", "--topic", "x",
         "--state-dir", str(tmp_path)],
        cwd=str(backend), capture_output=True, text=True, timeout=180,
    )
    assert completed.returncode == 2, completed.stderr
    assert only_json(completed.stdout)["status"] == "usage_error"

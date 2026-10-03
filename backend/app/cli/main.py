"""The ``ymoney`` agent-facing command line.

Design (Work 15.5, §11) -- this is a *surface*, not a second implementation.
Every command resolves a workspace and then calls the SAME engine/service
functions the HTTP routes call:

===========================  ====================================================
``ymoney create``            ``engine.planning.orchestration.run_orchestration``,
                             ``engine.content_graph.derive_content``
``ymoney research``          ``engine.agents.creation.ResearchAgent``
``ymoney campaign``          ``services.jobs.enqueue``,
                             ``engine.campaign.publish_flow.{build_publishing_plan,
                             schedule_plan}``
``ymoney render``            ``engine.agents.production.VideoProducerAgent``,
                             ``providers.video_engine.factory.get_video_engine``
``ymoney localize``          ``engine.localization.pipeline.prepare_localizations``
``ymoney publish``           ``engine.planning.autonomy.assert_may_advance``
``ymoney planner``           ``engine.planning.{signals,engine,orchestration,
                             calendar,capacity,autonomy}``
``ymoney inspect``           ``services.jobs``, ORM reads scoped by workspace
===========================  ====================================================

No raw SQL. No shelling out to ffmpeg.

Non-negotiable behaviours, each with a test that fails if it is broken:

1. **Exit taxonomy.** ``0`` success / ``1`` the operation ran and failed /
   ``2`` bad arguments or an invalid manifest. ``1`` and ``2`` are never
   collapsed: that is what makes the CLI usable from CI and from an agent.
2. **stdout is exactly one JSON object, always.** ``sys.stdout`` is *swapped*
   for stderr for the whole command, so a library that prints cannot corrupt
   the stream an agent is parsing. Logs, warnings and tracebacks go to stderr.
3. **No credential is printed and none is accepted.** Keys resolve through
   ``services.provider_settings`` (workspace-scoped) and are reported as
   ``configured``/``source`` only -- never a value, never even a mask. A final
   guard re-scans the serialized payload for every resolved secret before it
   is written to stdout.
4. **A result manifest is written BEFORE the work starts** and updated as the
   run progresses, so a crashed run still leaves a machine-readable record of
   what was attempted.
5. **``run_checked`` is the only subprocess primitive** in this package: list
   argv, never ``shell=True``, captured output, and only the last 30 lines on
   failure.
6. **Workspace isolation.** Every data command takes ``--workspace`` and
   scopes every read to it, reusing the engine's own scoping helpers.

``ymoney publish`` is deliberately NOT a back door around
``assert_may_advance``: it calls that gate before it does anything else, and
the gate refuses ``PUBLISH`` at every autonomy mode. There is no ``--force``.

Trust boundary (Work 15.6 §10)
------------------------------
**This CLI is a local, operator-only tool.** It inherits the invoking OS user's
database and filesystem access, exactly as ``psql`` or ``sqlite3`` would, and it
does **not** authenticate an end user. Anyone who can run it can already reach
the same data by other means as that user.

Consequences, all deliberate:

* **No network listener.** It never binds a socket, starts a server, or serves
  an API. There is no ``serve`` command and no port. Anything that would make it
  a network service belongs in the ASGI app, where auth and tenancy live.
* **No remote auth surface.** There is no login, token exchange, or user
  identity. Do not add one; it would create a second, unauthenticated way in.
* **No separate business logic.** Every command calls the same engine/service
  functions the HTTP routes call. If a CLI command needs a rule, that rule
  belongs in the service, not here.
* **Never prints secrets** -- not a value, not a length, not a digest. A
  redaction backstop scans stdout before it is emitted.
* **Machine-readable output.** stdout carries exactly one JSON object; logs go
  to stderr. An agent can parse stdout unconditionally.

Do not turn this into an end-user product. If a non-operator needs access, give
them the HTTP API, which has authentication and workspace authorisation.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import traceback
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.engine.planning.autonomy import (
    AutonomyMode,
    AutonomyPolicy,
    AutonomyRefused,
    PlanningAction,
    assert_may_advance,
    describe_autonomy,
)
from app.models import (
    Campaign,
    ContentItem,
    EditorialPlanItem,
    Opportunity,
    Video,
    VideoVariant,
    Workspace,
)
from app.services import provider_settings

__all__ = [
    "CLI_VERSION",
    "EXIT_FAILED",
    "EXIT_OK",
    "EXIT_USAGE",
    "CommandFailed",
    "Refused",
    "RunManifest",
    "UsageError",
    "main",
    "redact_secrets",
    "resolve_state_dir",
    "run_checked",
    "run_cli",
    "write_result_manifest",
]

CLI_VERSION = "1.0"
#: Machine-readable discriminator carried in every stdout payload.
SCHEMA = "ymoney.cli/1"

EXIT_OK = 0
#: The operation RAN and failed (a render failed, a publish was rejected).
EXIT_FAILED = 1
#: Bad arguments or an invalid manifest -- the CLI never got to do the work.
EXIT_USAGE = 2

#: Only the tail of a failed subprocess is surfaced, never its whole output.
_TAIL_LINES = 30

DEFAULT_STATE_DIRNAME = ".ymoney-cli"
STATE_DIR_ENV = "YMONEY_CLI_STATE_DIR"
WORKSPACE_ENV = "YMONEY_WORKSPACE"

_ENGINE_PROBES: tuple[tuple[str, str], ...] = (
    ("ffmpeg", "ffmpeg"),
    ("ffprobe", "ffprobe"),
    ("yt-dlp", "yt-dlp"),
)


# ---------------------------------------------------------------------------
# errors -> exit codes
# ---------------------------------------------------------------------------


class CliError(RuntimeError):
    """Base CLI failure. ``EXIT_FAILED`` unless a subclass says otherwise."""

    kind = "error"

    @property
    def exit_code(self) -> int:
        return EXIT_FAILED


class UsageError(CliError):
    """Bad arguments / invalid manifest. Exit 2 -- the work never started."""

    kind = "usage_error"

    @property
    def exit_code(self) -> int:
        return EXIT_USAGE


class Refused(CliError):
    """An engine gate refused the action. Exit 1 -- it ran and was rejected."""

    kind = "refused"


class CommandFailed(CliError):
    """A subprocess run through :func:`run_checked` exited non-zero."""

    kind = "command_failed"


# ---------------------------------------------------------------------------
# streams: UTF-8 on both, stdout reserved for one JSON object
# ---------------------------------------------------------------------------


def _force_utf8_console() -> None:
    """Make stdout/stderr UTF-8 before anything is printed.

    Ported from MoneyPrinterTurbo 1.3.7
    Copyright (c) 2024 Harry — MIT License
    https://github.com/harry0703/MoneyPrinterTurbo

    A Windows console defaults to a legacy code page, so a single non-ASCII
    character in a research summary raises ``UnicodeEncodeError`` *after* the
    work succeeded -- the run then reports failure and never prints where the
    result is.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - exotic stream
            continue


class RunLog:
    """stderr, plus a durable per-run log file the manifest points at."""

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._handle = None

    def open(self) -> None:
        if self.path is None or self._handle is not None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
        except OSError:  # pragma: no cover - unwritable state dir
            self.path = None
            self._handle = None

    def write(self, level: str, message: str) -> None:
        line = f"[{level}] {message}"
        print(line, file=sys.stderr)
        if self._handle is not None:
            try:
                self._handle.write(line + "\n")
                self._handle.flush()
            except OSError:  # pragma: no cover - disk full
                pass

    def close(self) -> None:
        if self._handle is not None:
            try:
                self._handle.close()
            finally:
                self._handle = None


_ACTIVE_LOG = RunLog(None)


def _log(message: str, *, level: str = "info") -> None:
    """Every log line in this package goes through here -> stderr, never stdout."""
    _ACTIVE_LOG.write(level, message)


@contextmanager
def _stdout_is_stderr(real_stdout) -> Iterator[None]:
    """Divert ``sys.stdout`` into stderr for the whole command.

    This is the structural half of the stdout contract: the ONLY writes that
    can reach the real stdout are the ones :func:`run_cli` makes itself, with
    a payload it has already validated and redacted.
    """
    original = sys.stdout
    sys.stdout = sys.stderr
    try:
        yield
    finally:
        sys.stdout = original


# ---------------------------------------------------------------------------
# result manifest (written BEFORE the work starts)
# ---------------------------------------------------------------------------


def _utcnow() -> str:
    return datetime.now(UTC).isoformat()


def resolve_state_dir(explicit: str = "") -> Path:
    """Where manifests and run logs live. Never inside the database."""
    raw = (explicit or os.environ.get(STATE_DIR_ENV) or "").strip()
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.cwd() / DEFAULT_STATE_DIRNAME).resolve()


# Ported from MoneyPrinterTurbo 1.3.7 (docs/skill/mpt_agent.py:555-580)
# Copyright (c) 2024 Harry — MIT License
# https://github.com/harry0703/MoneyPrinterTurbo
#
# The atomic temp-file + replace matters: a run that is killed mid-write must
# leave either the previous manifest or the new one, never a truncated file an
# agent cannot parse.
def _atomic_write_json(path: Path, payload: dict) -> Path:
    """Write one JSON document atomically. Returns the resolved path."""
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {**payload, "updated_at": _utcnow()}
    unique_suffix = uuid.uuid4().hex
    temp_path = path.with_name(f".{path.name}.{os.getpid()}.{unique_suffix}.tmp")
    temp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2, default=str),
                         encoding="utf-8")
    temp_path.replace(path)
    return path.resolve()


def write_result_manifest(state_dir: Path, payload: dict) -> Path:
    """Atomically write the stable result file for agents that cannot wait.

    The file carries status, ids and result paths only -- never configuration
    contents, never credentials, never a full log.
    """
    return _atomic_write_json(Path(state_dir) / "latest-result.json", payload)


@dataclass
class RunManifest:
    """``{status, command, ..., log_file}`` written before work, updated during."""

    state_dir: Path
    command: str
    argv: list[str] = field(default_factory=list)
    workspace_id: str = ""
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started_at: str = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        self.log_file = self.state_dir / "logs" / f"run-{self.run_id}.log"
        self.run_file = self.state_dir / "runs" / f"{self.run_id}.json"
        self.latest_file = self.state_dir / "latest-result.json"
        self.log = RunLog(self.log_file)
        self._t0 = time.monotonic()
        self.payload: dict[str, Any] = {
            "schema": SCHEMA,
            "status": "running",
            "command": self.command,
            "run_id": self.run_id,
            "argv": list(self.argv),
            "workspace_id": self.workspace_id,
            "started_at": self.started_at,
            "log_file": str(self.log_file),
            "manifest_file": str(self.latest_file),
            "exit_code": None,
            "result": {},
            "error": None,
        }

    def start(self) -> RunManifest:
        """Open the run log and write the ``running`` manifest BEFORE any work."""
        self.log.open()
        self.write()
        _log(f"run {self.run_id} started: {self.command}")
        return self

    def bind_workspace(self, workspace_id: str) -> None:
        self.workspace_id = workspace_id
        self.payload["workspace_id"] = workspace_id

    def write(self, **fields: Any) -> None:
        self.payload.update(fields)
        self.payload["updated_at"] = _utcnow()
        self.payload["elapsed_ms"] = int((time.monotonic() - self._t0) * 1000)
        for target in (self.latest_file, self.run_file):
            try:
                _atomic_write_json(target, self.payload)
            except OSError as exc:  # pragma: no cover - unwritable state dir
                _log(f"manifest write failed for {target}: {exc}", level="warning")

    def finish(self, *, status: str, exit_code: int, result: dict | None = None,
               error: dict | None = None) -> None:
        self.log.close()
        self.write(status=status, exit_code=exit_code, result=result or {}, error=error)


# ---------------------------------------------------------------------------
# subprocess discipline
# ---------------------------------------------------------------------------


# Ported from MoneyPrinterTurbo 1.3.7 (docs/skill/mpt_agent.py:582-595)
# Copyright (c) 2024 Harry — MIT License
# https://github.com/harry0703/MoneyPrinterTurbo
#
# `shell=False` is explicit rather than omitted: this is the only place in the
# CLI that can execute anything, so "list argv, never a shell" is enforced here
# instead of being a convention someone can quietly break.
def run_checked(command: Sequence[str], *, cwd: Path, timeout: float | None = None) -> str:
    """Run one external command; return its output, or fail with the last 30 lines.

    Args are passed as a list and ``shell=False`` is pinned, so a value
    containing ``;``, ``&&``, ``$()`` or a newline is data, never syntax.
    """
    argv = list(command)
    if not argv or not all(isinstance(arg, str) and arg for arg in argv):
        raise UsageError("run_checked requires a non-empty list of non-empty string arguments")
    completed = subprocess.run(  # noqa: S603 - list argv, shell=False pinned below
        argv,
        cwd=str(cwd),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        check=False,
        shell=False,
        timeout=timeout,
    )
    output = completed.stdout or ""
    if completed.returncode != 0:
        tail = output.splitlines()[-_TAIL_LINES:]
        _log(f"command failed with exit code {completed.returncode}: {argv[0]}", level="error")
        for line in tail:
            _log(f"  | {line}", level="error")
        raise CommandFailed(f"{argv[0]} exited with code {completed.returncode}")
    return output


# ---------------------------------------------------------------------------
# credential safety
# ---------------------------------------------------------------------------


def _scope_ids(workspace_ref: str) -> list[str | None]:
    """Every credential scope a run could legitimately touch.

    The CLI is handed ``--workspace`` as an id OR a slug, and it may have failed
    before the workspace row was ever resolved. Scanning both the resolved
    tenant scope and the global scope is what makes the redaction backstop
    unconditional instead of depending on how far the command got.
    """
    scopes: list[str | None] = [None]  # global / env fallback, always scanned
    ref = (workspace_ref or "").strip()
    if not ref:
        return scopes
    try:
        with session_scope() as db:
            row = db.get(Workspace, ref) or db.scalar(
                select(Workspace).where(Workspace.slug == ref))
        if row is not None:
            scopes.insert(0, row.id)
    except Exception:  # noqa: BLE001 - an unreadable DB must not disable the guard
        pass
    return scopes


def _resolved_secret_values(workspace_ref: str) -> list[str]:
    """Every secret value YMONEY can currently resolve for these scopes."""
    values: list[str] = []
    for scope in _scope_ids(workspace_ref):
        for key, spec in provider_settings.REGISTRY.items():
            if not spec.get("secret"):
                continue
            try:
                value, _source = provider_settings.get_credential(key, scope)
            except Exception:  # noqa: BLE001 - a broken registry row must not crash the CLI
                continue
            if value and value not in values:
                values.append(value)
    return values


def redact_secrets(text: str, workspace_ref: str) -> tuple[str, int]:
    """Replace any resolved secret value found in ``text`` with ``[redacted]``.

    The CLI never *asks* for a secret -- this is the backstop that proves it:
    even if a provider echoed a key into an error message, it cannot reach
    stdout. No mask, no length, no prefix: an API key is not printed at all.
    """
    replacements = 0
    for value in _resolved_secret_values(workspace_ref):
        if value in text:
            replacements += text.count(value)
            text = text.replace(value, "[redacted]")
    return text, replacements


def credential_status(workspace_id: str, keys: Sequence[str] = ()) -> list[dict]:
    """Per-credential ``configured``/``source``. Never a value, never a mask."""
    wanted = list(keys) if keys else list(provider_settings.REGISTRY)
    out: list[dict] = []
    for key in wanted:
        spec = provider_settings.REGISTRY.get(key)
        if spec is None:
            continue
        try:
            value, source = provider_settings.get_credential(key, workspace_id or None)
        except Exception:  # noqa: BLE001 - never fail a status read
            value, source = None, "error"
        out.append({
            "key": key,
            "label": spec.get("label", key),
            "secret": bool(spec.get("secret")),
            "configured": bool(value),
            "source": source,
        })
    return out


# ---------------------------------------------------------------------------
# shared helpers
# ---------------------------------------------------------------------------


def _job_context(workspace_id: str, job_type: str, payload: dict | None = None):
    """A ``JobContext`` for calling an agent directly, outside the queue.

    The agents take a context, not a session; the durable queue is what makes
    them restartable in the server, and a CLI invocation is a single supervised
    process. ``job_id`` is recorded on the agent run as provenance.
    """
    from app.services import jobs as jobs_service

    return jobs_service.JobContext(
        job_id=f"cli-{uuid.uuid4().hex[:12]}",
        type=job_type,
        workspace_id=workspace_id,
        cycle_id=None,
        payload=dict(payload or {}),
        attempt=1,
        cancelled=lambda: False,
    )


def build_policy(mode: str, allowed: Sequence[str], spend: float = 0.0) -> AutonomyPolicy:
    """Build the SAME policy object the planner API builds, or refuse the input."""
    try:
        resolved = AutonomyMode(str(mode or "RECOMMEND"))
    except ValueError as exc:
        raise UsageError(
            f"unknown autonomy {mode!r}; pick from {[str(m) for m in AutonomyMode]}"
        ) from exc
    actions: set[PlanningAction] = set()
    for name in allowed or ():
        try:
            actions.add(PlanningAction(str(name)))
        except ValueError as exc:
            raise UsageError(
                f"unknown planning action {name!r}; pick from {[str(a) for a in PlanningAction]}"
            ) from exc
    return AutonomyPolicy(mode=resolved, allowed_actions=frozenset(actions),
                          max_daily_spend_usd=float(spend or 0.0))


def _require_workspace(db, ref: str) -> Workspace:
    row = db.get(Workspace, ref) if ref else None
    if row is None and ref:
        row = db.scalar(select(Workspace).where(Workspace.slug == ref))
    if row is None:
        raise UsageError(f"unknown workspace {ref!r}: pass --workspace <id|slug>")
    return row


def _positive(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError(f"value must be >= 1, got {parsed}")
    return parsed


# ---------------------------------------------------------------------------
# command implementations
# ---------------------------------------------------------------------------


def cmd_create(args) -> dict:
    """``ymoney create`` -- a campaign draft, or a derived content item."""
    with session_scope() as db:
        ws = _require_workspace(db, args.workspace)
        with provider_settings.workspace_scope(ws.id):
            if args.what == "campaign-draft":
                item = db.get(EditorialPlanItem, args.item_id)
                if item is None or item.workspace_id != ws.id:
                    raise CliError("plan item not found in this workspace")
                policy = build_policy(args.autonomy, args.allow_action, args.max_daily_spend_usd)
                from app.engine.planning.orchestration import run_orchestration

                result = run_orchestration(db, ws.id, item.id, policy=policy)
                payload = result.to_dict()
                if result.blocked:
                    raise Refused(result.blocked)
                return {
                    "workspace_id": ws.id,
                    "created": "campaign_draft",
                    "campaign_id": payload.get("campaign_id", ""),
                    "plan_item_id": item.id,
                    "status": "DRAFT",
                    "stages_done": payload.get("stages_done", []),
                    "publishes": False,
                }
            from app.engine.content_graph import LineageError, derive_content

            try:
                child = derive_content(
                    db, parent_id=args.parent_id, workspace_id=ws.id,
                    derivation_type=args.derivation_type, topic=args.topic or None,
                )
            except LineageError as exc:
                raise CliError(str(exc)) from exc
            return {
                "workspace_id": ws.id,
                "created": "content_item",
                "content_id": child.id,
                "parent_content_id": child.parent_content_id,
                "topic": child.topic,
                "derivation_type": child.derivation_type,
                "status": child.status,
            }


def cmd_research(args) -> dict:
    """``ymoney research`` -- run the ResearchAgent for one topic."""
    with session_scope() as db:
        ws = _require_workspace(db, args.workspace)
        with provider_settings.workspace_scope(ws.id):
            from app.engine.agents.creation import ResearchAgent

            ctx = _job_context(ws.id, "cli.research", {"topic": args.topic})
            brief = ResearchAgent().run(ctx, args.topic)
    return {"workspace_id": args.workspace, "topic": args.topic, "brief": brief}


def cmd_campaign(args) -> dict:
    """``ymoney campaign`` -- list / show / derive / schedule."""
    with session_scope() as db:
        ws = _require_workspace(db, args.workspace)
        with provider_settings.workspace_scope(ws.id):
            if args.what == "list":
                rows = db.scalars(
                    select(Campaign).where(Campaign.workspace_id == ws.id)
                    .order_by(Campaign.created_at.desc()).limit(args.limit)
                ).all()
                return {
                    "workspace_id": ws.id,
                    "count": len(rows),
                    "items": [
                        {"id": c.id, "name": c.name, "status": c.status,
                         "platforms": list(c.platforms_json or []),
                         "automation_level": c.automation_level,
                         "budget_daily_usd": c.budget_daily_usd,
                         "created_at": c.created_at.isoformat()}
                        for c in rows
                    ],
                }
            campaign = db.get(Campaign, args.campaign_id)
            if campaign is None or campaign.workspace_id != ws.id:
                raise CliError("campaign not found in this workspace")
            if args.what == "show":
                shorts = db.scalars(select(ContentItem).where(
                    ContentItem.workspace_id == ws.id,
                    ContentItem.campaign_id == campaign.id,
                    ContentItem.derivation_type == "short")).all()
                plan = dict(getattr(campaign, "kpis_json", None) or {}).get("campaign_plan") or {}
                return {
                    "workspace_id": ws.id,
                    "campaign_id": campaign.id,
                    "name": campaign.name,
                    "status": campaign.status,
                    "goal": campaign.goal,
                    "platforms": list(campaign.platforms_json or []),
                    "automation_level": campaign.automation_level,
                    "plan_status": plan.get("status", ""),
                    "master_content_id": plan.get("master_content_id", ""),
                    "desired_shorts": int(plan.get("desired_shorts", 0) or 0),
                    "shorts": [
                        {"id": s.id, "topic": s.topic, "status": s.status}
                        for s in shorts
                    ],
                    "publishes": False,
                }
            from app.services import jobs as jobs_service

            if args.what == "derive":
                job_id = jobs_service.enqueue(
                    "campaign.derive", {"campaign_id": campaign.id},
                    workspace_id=ws.id, priority=50, max_retries=2,
                    idempotency_key=f"campaign-derive-{campaign.id}",
                )
                return {"workspace_id": ws.id, "campaign_id": campaign.id,
                        "job_id": job_id, "status": "QUEUED"}
            from datetime import UTC, datetime

            from app.engine.campaign.publish_flow import (
                build_publishing_plan,
                schedule_plan,
            )

            plan = dict(getattr(campaign, "kpis_json", None) or {}).get("campaign_plan") or {}
            shorts = db.scalars(select(ContentItem).where(
                ContentItem.workspace_id == ws.id,
                ContentItem.campaign_id == campaign.id,
                ContentItem.derivation_type == "short")).all()
            if not shorts:
                raise CliError("no shorts to schedule: run `ymoney campaign derive` first")
            platforms = plan.get("target_platforms") or list(campaign.platforms_json or [])
            start = datetime.fromisoformat(args.start.replace("Z", "+00:00")) if args.start \
                else datetime.now(UTC)
            items = build_publishing_plan(
                [{"variant_id": s.id, "platform": p, "short_content_id": s.id}
                 for s in shorts for p in platforms],
                start, args.interval_days,
                master_ref={"content_id": plan.get("master_content_id", "master"),
                            "platform": "youtube_longform"},
                autonomy=campaign.automation_level,
            )
            scheduled = schedule_plan(db, workspace_id=ws.id, campaign_id=campaign.id, items=[
                {"short_content_id": it["variant_id"], "platform": it["platform"],
                 "planned_at": it["planned_at"]}
                for it in items if not it.get("is_master")
            ])
            return {"workspace_id": ws.id, "campaign_id": campaign.id,
                    "scheduled": scheduled, "items": len(items), "publishes": False}


def cmd_render(args) -> dict:
    """``ymoney render`` -- list / show / estimate / submit through the producer."""
    with session_scope() as db:
        ws = _require_workspace(db, args.workspace)
        with provider_settings.workspace_scope(ws.id):
            if args.what == "list":
                query = select(Video).where(Video.workspace_id == ws.id)
                if args.status:
                    query = query.where(Video.status == args.status)
                rows = db.scalars(query.order_by(Video.created_at.desc()).limit(args.limit)).all()
                return {"workspace_id": ws.id, "count": len(rows), "items": [
                    {"id": v.id, "status": v.status, "engine": v.engine,
                     "aspect_ratio": v.aspect_ratio, "error": v.error or "",
                     "created_at": v.created_at.isoformat()} for v in rows]}
            if args.what == "show":
                video = db.get(Video, args.video_id)
                if video is None or video.workspace_id != ws.id:
                    raise CliError("video not found in this workspace")
                return {"workspace_id": ws.id, "video_id": video.id,
                        "status": video.status, "engine": video.engine,
                        "engine_task_id": video.engine_task_id,
                        "file_path": video.file_path or "", "error": video.error or "",
                        "params": dict(video.params_json or {})}
            if args.what == "estimate":
                from app.providers.video_engine.base import RenderRequest
                from app.providers.video_engine.factory import get_video_engine

                request = RenderRequest(subject=args.topic or "estimate",
                                        script=args.script or "estimate",
                                        workspace_id=ws.id)
                engine = get_video_engine(ws.id)
                per_render = float(engine.estimate_cost(request) or 0.0)
                return {"workspace_id": ws.id, "engine": engine.engine_name,
                        "per_video_usd": round(per_render, 4), "is_estimate": True}
            # submit: render the SELECTED variant of a content item, through the
            # very agent the pipeline uses (budget gate, idempotency, storage).
            content = db.get(ContentItem, args.content_id)
            if content is None or content.workspace_id != ws.id:
                raise CliError("content not found in this workspace")
            variant = db.scalar(select(VideoVariant).where(
                VideoVariant.content_item_id == content.id,
                VideoVariant.selected.is_(True)))
            if variant is None:
                raise CliError("content has no selected variant; run the build stage first")
            from app.engine.agents.production import VideoProducerAgent
            from app.engine.platform_formats import aspect_for_platforms

            strategy = dict(content.strategy_json or {})
            research = dict(content.research_json or {})
            aspect = aspect_for_platforms(strategy.get("platforms")) or strategy.get(
                "aspect_ratio", "9:16")
            ctx = _job_context(ws.id, "cli.render", {"content_id": content.id})
            result = VideoProducerAgent().render(
                ctx, topic=content.topic, script=variant.script,
                keywords=research.get("visual_keywords", []),
                aspect_ratio=aspect, variant_id=variant.id,
            )
            return {"workspace_id": ws.id, "content_id": content.id,
                    "variant_id": variant.id, **result}


def cmd_localize(args) -> dict:
    """``ymoney localize`` -- start / list / show."""
    with session_scope() as db:
        ws = _require_workspace(db, args.workspace)
        with provider_settings.workspace_scope(ws.id):
            from app.engine.localization.pipeline import (
                LocalizationError,
                prepare_localizations,
            )

            if args.what == "start":
                if not args.language:
                    raise UsageError("`ymoney localize start` needs at least one --language")
                try:
                    rows = prepare_localizations(
                        db, workspace_id=ws.id, source_content_id=args.content_id,
                        target_languages=list(args.language),
                        locales={lang: loc for lang, loc in zip(args.language, args.locale)},
                        source_language=args.source_language,
                        translation_version=args.translation_version,
                    )
                except LocalizationError as exc:
                    raise CliError(str(exc)) from exc
                from app.services import jobs as jobs_service

                job_id = jobs_service.enqueue(
                    "localization.run", {"localized_ids": [r.id for r in rows]},
                    workspace_id=ws.id, priority=60,
                    idempotency_key=f"localization-{ws.id}-{time.time_ns()}",
                )
                return {"workspace_id": ws.id, "job_id": job_id, "queued": job_id is not None,
                        "items": [{"id": r.id, "language": r.language, "status": r.status}
                                  for r in rows]}
            from app.models import LocalizedContent

            if args.what == "list":
                rows = db.scalars(
                    select(LocalizedContent).where(LocalizedContent.workspace_id == ws.id)
                    .order_by(LocalizedContent.created_at.desc()).limit(args.limit)).all()
                return {"workspace_id": ws.id, "count": len(rows), "items": [
                    {"id": r.id, "language": r.language, "locale": r.locale,
                     "status": r.status, "source_content_id": r.source_content_id}
                    for r in rows]}
            row = db.get(LocalizedContent, args.localized_id)
            if row is None or row.workspace_id != ws.id:
                raise CliError("localization not found in this workspace")
            return {"workspace_id": ws.id, "localized_id": row.id,
                    "language": row.language, "locale": row.locale,
                    "status": row.status, "error": row.error or "",
                    "child_content_id": row.child_content_id or "",
                    "lineage": dict(row.lineage_json or {})}


def cmd_publish(args) -> dict:
    """``ymoney publish`` -- gated by the planner's own single gate.

    ``assert_may_advance`` refuses ``PlanningAction.PUBLISH`` at EVERY autonomy
    mode (``NEVER_ALLOWED``), so this command cannot publish and there is no
    flag that makes it. Publication belongs to the campaign approval path; the
    CLI's job here is to say so in machine-readable form and change nothing.
    """
    policy = build_policy(args.autonomy, args.allow_action, args.max_daily_spend_usd)
    try:
        assert_may_advance(policy, PlanningAction.PUBLISH,
                           reason="ymoney publish is a planner action")
    except AutonomyRefused as exc:
        raise Refused(str(exc)) from exc
    # Unreachable while PUBLISH is in NEVER_ALLOWED. Kept (rather than deleted)
    # so that if the gate ever opens, this command does the honest thing instead
    # of silently reporting success.
    raise Refused(
        "publish reached its post-gate branch, which means the autonomy gate "
        "changed; re-verify the publication approval path before trusting this")


def cmd_planner(args) -> dict:
    """``ymoney planner`` -- signals / opportunities / plan / policy / item actions."""
    with session_scope() as db:
        ws = _require_workspace(db, args.workspace)
        with provider_settings.workspace_scope(ws.id):
            from app.engine.planning.engine import ContentPlanningEngine, PlanningInputs

            if args.what == "policy":
                return {"workspace_id": ws.id, "table": describe_autonomy(),
                        "publishes": False}
            if args.what == "signals":
                from app.engine.planning.signals import claim_signals

                rows = claim_signals(db, ws.id, persist=False)
                db.rollback()
                return {"workspace_id": ws.id, "count": len(rows), "signals": [
                    {"id": s.id, "source": s.source, "topic": s.topic,
                     "freshness": s.freshness, "status": s.status,
                     "recurrence": s.recurrence, "velocity": s.velocity,
                     "usable_as_demand": s.is_usable} for s in rows]}
            if args.what == "opportunities":
                rows = db.scalars(
                    select(Opportunity).where(Opportunity.workspace_id == ws.id)
                    .order_by(Opportunity.score.desc()).limit(args.limit)).all()
                return {"workspace_id": ws.id, "count": len(rows), "opportunities": [
                    {"id": o.id, "topic": o.topic, "basis": o.basis, "score": o.score,
                     "angle": o.angle, "platforms": list(o.platforms_json or []),
                     "dedupe_verdict": o.dedupe_verdict, "plan_item_id": o.plan_item_id or ""}
                    for o in rows]}
            if args.what == "plan":
                policy = build_policy(args.autonomy, args.allow_action)
                inputs = PlanningInputs(
                    workspace_id=ws.id, horizon_days=args.horizon_days,
                    goals=list(args.goal or []), platforms=list(args.platform or []),
                    budget_usd=args.budget_usd, spent_usd=0.0, autonomy=policy,
                    constraints={"default_format": args.format} if args.format else {},
                    locale=args.locale,
                )
                engine = ContentPlanningEngine(db, ws.id)
                if args.preview:
                    result = engine.plan(inputs)
                    db.rollback()
                else:
                    result = engine.plan(inputs)
                payload = result.to_dict()
                payload["workspace_id"] = ws.id
                payload["autonomy"] = str(policy.mode)
                payload["preview"] = args.preview
                payload["publishes"] = False
                return payload
            item = db.get(EditorialPlanItem, args.item_id)
            if item is None or item.workspace_id != ws.id:
                raise CliError("plan item not found in this workspace")
            if args.what == "approve":
                result = ContentPlanningEngine(db, ws.id).approve_item(item.id)
            elif args.what == "reject":
                if not args.reason:
                    raise UsageError("`ymoney planner reject` needs --reason")
                result = ContentPlanningEngine(db, ws.id).reject_item(item.id, args.reason)
            elif args.what == "research-more":
                result = ContentPlanningEngine(db, ws.id).request_more_research(
                    item.id, args.reason or "")
            elif args.what == "campaign":
                policy = build_policy(args.autonomy, args.allow_action)
                from app.engine.planning.orchestration import run_orchestration

                result = run_orchestration(db, ws.id, item.id, policy=policy).to_dict()
                if result.get("blocked"):
                    raise Refused(str(result["blocked"]))
            else:  # schedule
                policy = build_policy(args.autonomy, args.allow_action)
                placement = _place_item(db, ws, item)
                result = ContentPlanningEngine(db, ws.id).schedule_item(
                    item.id, policy, placement=placement)
                result["publishes"] = False
            return {"workspace_id": ws.id, "plan_item_id": item.id, **result}


def _place_item(db, ws: Workspace, item: EditorialPlanItem):
    """Place one plan item through the planner's own calendar (never publishes)."""
    from datetime import date

    from app.engine.planning.calendar import CalendarRequest, place_request

    content_item_id = ""
    if item.opportunity_id:
        content_item_id = db.scalar(select(ContentItem.id).where(
            ContentItem.workspace_id == ws.id,
            ContentItem.opportunity_id == item.opportunity_id).limit(1)) or ""
    if not content_item_id and item.campaign_id:
        content_item_id = db.scalar(select(ContentItem.id).where(
            ContentItem.workspace_id == ws.id,
            ContentItem.campaign_id == item.campaign_id).limit(1)) or ""
    platforms = list(item.platforms or []) or ["threads"]
    placement = None
    for platform in platforms:
        placement = place_request(db, ws.id, CalendarRequest(
            plan_item_id=item.id, content_id=content_item_id,
            campaign_id=item.campaign_id or "", platform=platform,
            content_format=item.content_format,
            target_date=(item.target_date.date() if item.target_date else date.today()),
            locale=(ws.timezone or "UTC"),
        ))
        if not placement.is_blocked:
            break
    return placement


def cmd_inspect(args) -> dict:
    """``ymoney inspect`` -- jobs / videos / workspace / connections / engine / tools."""
    if args.what == "tools":
        return _probe_tools()
    with session_scope() as db:
        ws = _require_workspace(db, args.workspace)
        with provider_settings.workspace_scope(ws.id):
            if args.what == "jobs":
                from app.services import jobs as jobs_service

                rows = jobs_service.list_jobs(workspace_id=ws.id, status=args.status or None,
                                             limit=args.limit)
                return {"workspace_id": ws.id, "count": len(rows), "jobs": [
                    {"id": j["id"], "type": j["type"], "status": j["status"],
                     "retry_count": j["retry_count"], "last_error": j["last_error"]}
                    for j in rows]}
            if args.what == "job":
                from app.services import jobs as jobs_service

                job = jobs_service.get_job(args.job_id)
                if not job or job.get("workspace_id") != ws.id:
                    raise CliError("job not found in this workspace")
                return {"workspace_id": ws.id, "job": job}
            if args.what == "connections":
                return {"workspace_id": ws.id,
                        "credentials": credential_status(ws.id, args.connection or ()),
                        "note": "presence and source only; values are never printed"}
            if args.what == "engine":
                from app.engine.autopilot import get_autopilot_status

                return {"workspace_id": ws.id,
                        "autopilot": get_autopilot_status(ws.id),
                        "autonomy": describe_autonomy(), "publishes": False}
            if args.what == "videos":
                rows = db.scalars(
                    select(Video).where(Video.workspace_id == ws.id)
                    .order_by(Video.created_at.desc()).limit(args.limit)).all()
                return {"workspace_id": ws.id, "count": len(rows), "videos": [
                    {"id": v.id, "status": v.status, "engine": v.engine,
                     "created_at": v.created_at.isoformat()} for v in rows]}
            if args.what == "workspace":
                return {"workspace_id": ws.id, "name": ws.name, "slug": ws.slug,
                        "niche": ws.niche, "language": ws.language,
                        "timezone": ws.timezone, "currency": ws.currency,
                        "credentials_configured": sum(
                            1 for c in credential_status(ws.id) if c["configured"])}
            raise UsageError(f"unknown inspect target {args.what!r}")


def _probe_tools() -> dict:
    """Read-only availability probe for the external CLIs YMONEY itself shells out to.

    This is the ONLY subprocess the CLI performs, and it goes through
    :func:`run_checked`: list argv, no shell, and only a tail of output.
    """
    root = Path.cwd()
    probes: list[dict] = []
    for label, binary in _ENGINE_PROBES:
        found = shutil.which(binary)
        if not found:
            probes.append({"name": label, "available": False, "version": ""})
            continue
        try:
            output = run_checked([found, "--version"], cwd=root, timeout=20)
            probes.append({"name": label, "available": True,
                           "version": output.splitlines()[0][:200] if output else ""})
        except CommandFailed as exc:
            probes.append({"name": label, "available": True, "version": "", "error": str(exc)})
    return {"tools": probes,
            "note": "external binaries the media pipeline shells out to; "
                    "the CLI renders only through the VideoProducerAgent"}


# ---------------------------------------------------------------------------
# text rendering (opt-in only; stdout stays JSON by default)
# ---------------------------------------------------------------------------


def render_text(payload: dict) -> str:
    lines: list[str] = []

    def walk(prefix: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                walk(f"{prefix}.{key}" if prefix else str(key), item)
        elif isinstance(value, list):
            if not value:
                lines.append(f"{prefix}: (none)")
            for index, item in enumerate(value):
                walk(f"{prefix}[{index}]", item)
        else:
            lines.append(f"{prefix}: {value}")

    walk("", payload)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# parser
# ---------------------------------------------------------------------------


class _Parser(argparse.ArgumentParser):
    """argparse that never writes to stdout and never calls ``sys.exit``.

    A ``SystemExit`` from argparse would leave stdout empty, and an agent
    parsing stdout would see nothing at all. Usage text still reaches a human
    (on stderr); stdout stays reserved for the one JSON object.
    """

    def _print_message(self, message: str, file=None) -> None:
        if message:
            (file or sys.stderr).write(message)

    def print_help(self, file=None) -> None:
        super().print_help(file=sys.stderr)

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message)

    def exit(self, status: int = 0, message: str | None = None) -> None:  # type: ignore[override]
        if message:
            self._print_message(message, sys.stderr)
        raise _ExitRequest(status)


class _ExitRequest(Exception):
    """argparse asked to finish (``--help`` / ``--version``) without an error."""

    def __init__(self, status: int) -> None:
        super().__init__(f"exit {status}")
        self.status = status


def _add_autonomy_flags(parser: argparse.ArgumentParser, default: str = "RECOMMEND") -> None:
    parser.add_argument("--autonomy", default=default,
                        help="planner autonomy mode (default: %(default)s)")
    parser.add_argument("--allow-action", action="append", default=[], metavar="ACTION",
                        help="allowlist one PlanningAction for AUTONOMOUS; repeatable")
    parser.add_argument("--max-daily-spend-usd", type=float, default=0.0,
                        help="declared budget ceiling; 0 means 'no ceiling declared'")


def _global_options() -> argparse.ArgumentParser:
    """Options accepted BEFORE or AFTER the subcommand.

    ``SUPPRESS`` defaults matter: argparse copies a subparser's namespace over
    the parent's, so a real default on the subparser would silently reset a
    value the operator passed before the command name.
    """
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--text", action="store_true", default=argparse.SUPPRESS,
                        help="also render the result as human-readable text on stderr "
                             "(stdout stays the single JSON object either way)")
    common.add_argument("--state-dir", default=argparse.SUPPRESS, metavar="DIR",
                        help=f"where run manifests/logs live "
                             f"(default: ${STATE_DIR_ENV} or ./{DEFAULT_STATE_DIRNAME})")
    return common


def build_parser() -> argparse.ArgumentParser:
    common = _global_options()
    parser = _Parser(
        prog="ymoney",
        description="Agent-facing control surface over the YMONEY engine.",
        epilog="stdout is always exactly one JSON object. Logs go to stderr. "
               "Exit 0 = success, 1 = the operation ran and failed, "
               "2 = bad arguments or an invalid manifest.",
        parents=[common],
    )
    parser.add_argument("--version", action="version",
                        version=f"ymoney {CLI_VERSION} (schema {SCHEMA})")
    sub = parser.add_subparsers(dest="command", metavar="COMMAND")

    # -- create ------------------------------------------------------------
    create = sub.add_parser("create", parents=[common],
                            help="create a content item or a campaign draft")
    create.add_argument("--workspace", required=True, metavar="ID_OR_SLUG")
    create.add_argument("what", choices=("campaign-draft", "content-item"))
    create.add_argument("--item-id", default="", help="plan item id (campaign-draft)")
    create.add_argument("--parent-id", default="", help="parent content id (content-item)")
    create.add_argument("--derivation-type", default="short",
                        choices=("short", "variant", "localized", "platform_cut",
                                 "repurpose", "translation", "other"))
    create.add_argument("--topic", default="")
    _add_autonomy_flags(create, default="APPROVAL")
    create.set_defaults(handler=cmd_create)

    # -- research ----------------------------------------------------------
    research = sub.add_parser("research", parents=[common], help="run the ResearchAgent for a topic")
    research.add_argument("--workspace", required=True, metavar="ID_OR_SLUG")
    research.add_argument("--topic", required=True)
    research.set_defaults(handler=cmd_research)

    # -- campaign ----------------------------------------------------------
    campaign = sub.add_parser("campaign", parents=[common], help="campaign operations")
    campaign.add_argument("--workspace", required=True, metavar="ID_OR_SLUG")
    campaign.add_argument("what", choices=("list", "show", "derive", "schedule"))
    campaign.add_argument("--campaign-id", default="")
    campaign.add_argument("--start", default="", help="ISO-8601 UTC start for schedule")
    campaign.add_argument("--interval-days", type=float, default=1.0)
    campaign.add_argument("--limit", type=_positive, default=50)
    campaign.set_defaults(handler=cmd_campaign)

    # -- render ------------------------------------------------------------
    render = sub.add_parser("render", parents=[common], help="submit or inspect a render")
    render.add_argument("--workspace", required=True, metavar="ID_OR_SLUG")
    render.add_argument("what", choices=("list", "show", "estimate", "submit"))
    render.add_argument("--video-id", default="")
    render.add_argument("--content-id", default="")
    render.add_argument("--topic", default="")
    render.add_argument("--script", default="")
    render.add_argument("--status", default="")
    render.add_argument("--limit", type=_positive, default=50)
    render.set_defaults(handler=cmd_render)

    # -- localize ----------------------------------------------------------
    localize = sub.add_parser("localize", parents=[common], help="localize / dub a video")
    localize.add_argument("--workspace", required=True, metavar="ID_OR_SLUG")
    localize.add_argument("what", choices=("start", "list", "show"))
    localize.add_argument("--content-id", default="")
    localize.add_argument("--localized-id", default="")
    localize.add_argument("--language", action="append", default=[], metavar="LANG")
    localize.add_argument("--locale", action="append", default=[], metavar="LOCALE")
    localize.add_argument("--source-language", default="en")
    localize.add_argument("--translation-version", type=_positive, default=1)
    localize.add_argument("--limit", type=_positive, default=50)
    localize.set_defaults(handler=cmd_localize)

    # -- publish (gated) ---------------------------------------------------
    publish = sub.add_parser(
        "publish", parents=[common],
        help="publish (ALWAYS REFUSED: the planner's gate refuses PUBLISH at every mode)")
    publish.add_argument("--workspace", default="", metavar="ID_OR_SLUG")
    publish.add_argument("--campaign-id", default="")
    _add_autonomy_flags(publish, default="AUTONOMOUS")
    publish.set_defaults(handler=cmd_publish)

    # -- planner -----------------------------------------------------------
    planner = sub.add_parser("planner", parents=[common], help="planner signals / opportunities / plan")
    planner.add_argument("--workspace", required=True, metavar="ID_OR_SLUG")
    planner.add_argument("what", choices=("signals", "opportunities", "plan", "policy",
                                          "approve", "reject", "research-more",
                                          "campaign", "schedule"))
    planner.add_argument("--item-id", default="")
    planner.add_argument("--reason", default="")
    planner.add_argument("--horizon-days", type=_positive, default=30)
    planner.add_argument("--goal", action="append", default=[], metavar="GOAL")
    planner.add_argument("--platform", action="append", default=[], metavar="PLATFORM")
    planner.add_argument("--format", default="", help="content format, e.g. SHORT")
    planner.add_argument("--locale", default="")
    planner.add_argument("--budget-usd", type=float, default=0.0)
    planner.add_argument("--preview", action="store_true",
                         help="plan without persisting anything")
    planner.add_argument("--limit", type=_positive, default=100)
    _add_autonomy_flags(planner)
    planner.set_defaults(handler=cmd_planner)

    # -- inspect -----------------------------------------------------------
    inspect = sub.add_parser("inspect", parents=[common], help="inspect jobs / videos / workspace state")
    inspect.add_argument("--workspace", default="", metavar="ID_OR_SLUG")
    inspect.add_argument("what", choices=("jobs", "job", "videos", "workspace",
                                          "connections", "engine", "tools"))
    inspect.add_argument("--job-id", default="")
    inspect.add_argument("--status", default="")
    inspect.add_argument("--connection", action="append", default=[], metavar="PROVIDER.KEY",
                        help="restrict `inspect connections` to these registry keys")
    inspect.add_argument("--limit", type=_positive, default=50)
    inspect.set_defaults(handler=cmd_inspect)

    return parser


# ---------------------------------------------------------------------------
# the single stdout writer
# ---------------------------------------------------------------------------


def run_cli(argv: Sequence[str] | None = None) -> int:
    """Parse, dispatch, emit one JSON object, return the exit code."""
    _force_utf8_console()
    real_stdout = sys.stdout
    global _ACTIVE_LOG
    _ACTIVE_LOG = RunLog(None)

    args_list = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()
    manifest: RunManifest | None = None
    text = False
    #: what the operator asked for (id OR slug); the redaction guard scans the
    #: credential scope for this even when the command failed early.
    workspace_ref = ""

    with _stdout_is_stderr(real_stdout):
        try:
            args = parser.parse_args(args_list)
            text = bool(getattr(args, "text", False))
            workspace_ref = str(getattr(args, "workspace", "") or "")
            state_dir = resolve_state_dir(getattr(args, "state_dir", "") or "")
            manifest = RunManifest(
                state_dir=state_dir,
                command=args.command or "help",
                argv=list(args_list),
            ).start()
            if workspace_ref:
                manifest.bind_workspace(workspace_ref)
            _ACTIVE_LOG = manifest.log
            handler = getattr(args, "handler", None)
            if handler is None:
                raise UsageError(
                    "no command given; try `ymoney --help`")
            data = handler(args)
            workspace_id = ""
            if isinstance(data, dict):
                workspace_id = str(data.get("workspace_id", "") or "")
            if workspace_id:
                manifest.bind_workspace(workspace_id)
            payload = {
                "schema": SCHEMA,
                "status": "ok",
                "command": manifest.command,
                "workspace_id": workspace_id,
                "run_id": manifest.run_id,
                "exit_code": EXIT_OK,
                "manifest_file": str(manifest.latest_file),
                "log_file": str(manifest.log_file),
                "result": data,
                "error": None,
            }
            manifest.finish(status="ok", exit_code=EXIT_OK, result=data)
        except _ExitRequest as request:
            payload = {
                "schema": SCHEMA,
                "status": "ok" if request.status == 0 else "usage_error",
                "command": "help",
                "workspace_id": "",
                "run_id": "",
                "exit_code": EXIT_OK if request.status == 0 else EXIT_USAGE,
                "manifest_file": "",
                "log_file": "",
                "result": {"commands": sorted(_command_names())},
                "error": None,
            }
            exit_code = int(payload["exit_code"])
        except CliError as exc:
            exit_code = exc.exit_code
            payload = {
                "schema": SCHEMA,
                "status": exc.kind,
                "command": (manifest.command if manifest else (args_list[0] if args_list else "")),
                "workspace_id": manifest.workspace_id if manifest else "",
                "run_id": manifest.run_id if manifest else "",
                "exit_code": exit_code,
                "manifest_file": str(manifest.latest_file) if manifest else "",
                "log_file": str(manifest.log_file) if manifest else "",
                "result": {},
                "error": {"kind": exc.kind, "message": str(exc)[:2000]},
            }
            if manifest is not None:
                manifest.finish(status=exc.kind, exit_code=exit_code, error=payload["error"])
            _log(f"{exc.kind}: {exc}", level="error")
        except Exception as exc:  # noqa: BLE001 - the last line of defence
            exit_code = EXIT_FAILED
            detail = {"kind": "unexpected", "type": type(exc).__name__, "message": str(exc)[:2000]}
            payload = {
                "schema": SCHEMA,
                "status": "unexpected",
                "command": (manifest.command if manifest else (args_list[0] if args_list else "")),
                "workspace_id": manifest.workspace_id if manifest else "",
                "run_id": manifest.run_id if manifest else "",
                "exit_code": exit_code,
                "manifest_file": str(manifest.latest_file) if manifest else "",
                "log_file": str(manifest.log_file) if manifest else "",
                "result": {},
                "error": detail,
            }
            if manifest is not None:
                manifest.finish(status="unexpected", exit_code=exit_code, error=detail)
            _log(f"unexpected {type(exc).__name__}: {exc}", level="error")
            _log(traceback.format_exc(), level="error")

    # Redact AFTER the command finished and the manifest recorded, and BEFORE
    # anything reaches stdout. If a provider echoed a key into an error string,
    # it dies here. The scope comes from the OPERATOR'S --workspace, not from
    # whatever the command managed to resolve, so a run that failed early is
    # still guarded against its own tenant's keys.
    serialized = json.dumps(payload, ensure_ascii=False, default=str)
    serialized, leaks = redact_secrets(
        serialized, workspace_ref or str(payload.get("workspace_id", "") or ""))
    if leaks:
        _log(f"blocked {leaks} credential value(s) from stdout", level="error")
        payload["error"] = {**(payload.get("error") or {}),
                            "kind": "credential_redacted",
                            "message": "output withheld: it contained a credential value"}
        payload["status"] = "credential_redacted"
        payload["exit_code"] = payload.get("exit_code") or EXIT_FAILED
        payload["result"] = {}
        serialized = json.dumps(payload, ensure_ascii=False, default=str)
    if text:
        print(render_text(payload.get("result", {})), file=sys.stderr)
    real_stdout.write(serialized + "\n")
    real_stdout.flush()
    return int(payload["exit_code"])


def _command_names() -> list[str]:
    return ["campaign", "create", "inspect", "localize", "planner", "publish",
            "render", "research"]


def main(argv: Sequence[str] | None = None) -> int:
    """Console-script entry point. Returns the process exit code."""
    try:
        return run_cli(argv)
    finally:
        _ACTIVE_LOG.close()


if __name__ == "__main__":  # pragma: no cover - console-script shim
    sys.exit(main())

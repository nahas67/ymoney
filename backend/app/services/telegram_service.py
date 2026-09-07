"""Telegram remote control for YMONEY.

Lets the operator control the entire platform from a phone:
- Pair a chat via a one-time code (UI generates it; user sends /start <code>).
- Receive alerts for pipeline events (cycle results, QC reviews, errors, costs).
- Issue commands: status, start, stop, pause, resume, cycle, cost, help.

Transport: Telegram Bot API long-polling (getUpdates) via httpx. No webhook
or extra dependency needed — httpx is already in the stack. The poller runs
as an asyncio task inside the FastAPI lifespan (started after job workers).

Security:
- The bot token is stored encrypted (api_credentials via provider_settings)
  with env fallback TELEGRAM_BOT_TOKEN.
- Pairing codes are single-use, 6-char, expire after 15 minutes, and map to
  the workspace of the admin who generated them.
- Commands are only accepted from linked chats; unlinking revokes instantly.
"""

from __future__ import annotations

import asyncio
import secrets
import string
import time
from datetime import timedelta

import httpx
from loguru import logger
from sqlalchemy import select

from app.core.config import settings
from app.db import session_scope
from app.models import TelegramLink, Workspace
from app.models.base import utcnow
from app.services import provider_settings
from app.services import events as events_service

PROVIDER_KEY_TOKEN = "telegram.bot_token"

# ---------------------------------------------------------------------------
# In-memory pairing-code store: {code: {workspace_id, user_id, expires_at}}
# Codes are process-local: pairing happens against the same server that shows
# the code, which holds for the single-process deployment model of YMONEY.
# ---------------------------------------------------------------------------
_PAIRING_CODES: dict[str, dict] = {}
_PAIRING_TTL_SECONDS = 15 * 60


def _code() -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(6))


def create_pairing_code(workspace_id: str, user_id: str) -> dict:
    """Generate a one-time pairing code for a workspace admin."""
    code = _code()
    _PAIRING_CODES[code] = {
        "workspace_id": workspace_id,
        "user_id": user_id,
        "expires_at": time.time() + _PAIRING_TTL_SECONDS,
    }
    # opportunistic cleanup of expired codes
    now = time.time()
    for k in [k for k, v in _PAIRING_CODES.items() if v["expires_at"] < now]:
        _PAIRING_CODES.pop(k, None)
    return {"code": code, "expires_in_seconds": _PAIRING_TTL_SECONDS}


# ---------------------------------------------------------------------------
# Token resolution (DB encrypted -> env fallback)
# ---------------------------------------------------------------------------

def get_bot_token() -> tuple[str | None, str]:
    try:
        val, source = provider_settings.get_credential(PROVIDER_KEY_TOKEN)
        if val:
            return val, source
    except Exception:  # registry mismatch — fall back to env
        pass
    if settings.telegram_bot_token:
        return settings.telegram_bot_token, "env"
    return None, "none"


def set_bot_token(value: str | None) -> None:
    provider_settings.set_credential(PROVIDER_KEY_TOKEN, value or None)


def bot_configured() -> bool:
    tok, _ = get_bot_token()
    return bool(tok)


# ---------------------------------------------------------------------------
# Low-level Bot API client (httpx)
# ---------------------------------------------------------------------------

class TelegramError(RuntimeError):
    pass


async def _api(method: str, payload: dict | None = None, timeout: float = 15.0) -> dict:
    token, _ = get_bot_token()
    if not token:
        raise TelegramError("telegram bot token not configured")
    url = f"https://api.telegram.org/bot{token}/{method}"
    async with httpx.AsyncClient(timeout=timeout) as client:
        try:
            resp = await client.post(url, json=payload or {})
            resp.raise_for_status()
            data = resp.json()
        except httpx.HTTPStatusError as exc:
            # 409: another getUpdates consumer (e.g. a webhook set) — surface clearly
            detail = ""
            try:
                detail = exc.response.json().get("description", "")
            except Exception:
                pass
            raise TelegramError(f"telegram {method} failed: {exc.response.status_code} {detail}") from exc
        except httpx.HTTPError as exc:
            raise TelegramError(f"telegram {method} failed: {exc}") from exc
    if not data.get("ok"):
        raise TelegramError(f"telegram {method} error: {data.get('description')}")
    return data.get("result", {})


async def send_message(chat_id: str, text: str, thread_id: str | None = None) -> None:
    payload: dict = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    if thread_id:
        payload["message_thread_id"] = int(thread_id)
    await _api("sendMessage", payload)


def _schedule(coro) -> None:
    """Fire-and-forget a coroutine from either async or sync context."""
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and not loop.is_closed():
        loop.create_task(coro)
    else:
        try:
            asyncio.run(coro)
        except Exception:
            pass  # best-effort telemetry


# ---------------------------------------------------------------------------
# Notification fan-out (called from record_event hook + API test endpoint)
# ---------------------------------------------------------------------------

def notify_workspaces(workspace_id: str, text: str) -> int:
    """Send a message to every active linked chat of a workspace.

    Synchronous wrapper: schedules the async send when a loop is running,
    otherwise runs it to completion (safe for both contexts).
    """
    with session_scope() as s:
        links = s.scalars(
            select(TelegramLink).where(
                TelegramLink.workspace_id == workspace_id,
                TelegramLink.active.is_(True),
            )
        ).all()
        targets = [(l.chat_id, l.thread_id) for l in links]
    if not targets:
        return 0

    async def _send_all() -> int:
        sent = 0
        for chat_id, thread_id in targets:
            try:
                await send_message(chat_id, text, thread_id)
                sent += 1
            except Exception as exc:
                logger.warning(f"telegram send to {chat_id} failed: {exc}")
        return sent

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    if loop is not None and not loop.is_closed():
        loop.create_task(_send_all())
        return len(targets)
    return asyncio.run(_send_all())


# Event kinds that are worth pushing to the operator's phone.
# These are the kinds actually emitted by the pipeline (engine/autopilot.py,
# engine/agents/*): errors always notify regardless of this set.
_NOTIFY_KINDS = {
    # cycle lifecycle
    "cycle.completed",
    "cycle.failed",
    "cycle.skipped",
    # quality
    "quality.failed",     # video failed QC after all attempts — cycle dies
    "quality.rejected",   # QC below threshold, regenerating (early warning)
    "quality.passed",
    # production
    "video.generation.failed",
    "video.generation.completed",
    # publishing (actual emitted kinds: publish.done / publish.skipped)
    "publish.done",
    "publish.skipped",
    "review.required",
    # safety & ops
    "safety.autopause",
    "budget.warning",
    "budget.exceeded",
    "autopilot.blocked",   # START refused (readiness gate)
    "autopilot.stopped",   # includes circuit-breaker trips
    "system.recovery",     # restart recovery paused runs
}

# Kinds that also carry a distinct emoji in the push
_KIND_ICON = {
    "quality.failed": "🚨",
    "quality.rejected": "♻️",
    "quality.passed": "✅",
    "cycle.failed": "🚨",
    "cycle.completed": "🏁",
    "cycle.skipped": "⏭",
    "video.generation.failed": "🎬❌",
    "video.generation.completed": "🎬",
    "publish.done": "📣",
    "publish.skipped": "⏭",
    "autopilot.blocked": "⛔",
    "autopilot.stopped": "🛑",
    "safety.autopause": "🛡",
    "budget.exceeded": "💸",
    "budget.warning": "💰",
    "review.required": "👀",
    "system.recovery": "🔧",
}


def on_event(workspace_id: str | None, kind: str, message: str, level: str) -> None:
    """Hook for events.record_event — pushes matching events to linked chats."""
    if not settings.telegram_enabled or not workspace_id:
        return
    if kind not in _NOTIFY_KINDS and level not in ("error",):
        return
    # Drop noisy successes from the phone: keep failures, warnings, and the
    # meaningful milestones (publish, cycle completion, autopilot stops).
    if kind in ("video.generation.completed", "quality.passed") and level == "success":
        return
    icon = _KIND_ICON.get(kind, {"success": "✅", "error": "🚨", "warning": "⚠️"}.get(level, "ℹ️"))
    title = kind.replace(".", " ").replace("_", " ").upper()
    notify_workspaces(workspace_id, f"{icon} <b>YMONEY · {title}</b>\n{message}")


# ---------------------------------------------------------------------------
# Command handling
# ---------------------------------------------------------------------------

_HELP = (
    "<b>YMONEY remote control</b>\n"
    "Alerts pushed to this chat: cycle completed/failed, quality verdicts, publish results, autopilot stops/blocks, budget warnings.\n"
    "/start — pair this chat (needs a code) or show commands\n"
    "/status — autopilot state, current cycle, costs\n"
    "/run — start the autopilot\n"
    "/stop — stop safely\n"
    "/pause — pause before next stage\n"
    "/resume — resume\n"
    "/cycle — run exactly one cycle\n"
    "/cost — today's spend vs budget\n"
    "/help — this message"
)


def _workspace_brief(ws_id: str) -> str:
    from app.engine import autopilot as autopilot_engine

    st = autopilot_engine.get_autopilot_status(ws_id)
    running = st.get("state") in ("RUNNING", "STARTING")
    dot = "🟢" if running else ("🟡" if st.get("state") == "PAUSED" else "⚪")
    stage = (st.get("current_cycle") or {}).get("stage")
    lines = [
        f"{dot} <b>Autopilot:</b> {st.get('state', 'IDLE')}"
        f" · cycle #{st.get('cycles_completed', 0)}",
    ]
    if stage and running:
        lines.append(f"⏳ Stage: <b>{stage}</b>")
    return "\n".join(lines)


def _cost_brief(ws_id: str) -> str:
    from sqlalchemy import func

    from app.core.config import get_settings
    from app.models import CostEntry

    today = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    with session_scope() as s:
        total = s.scalar(
            select(func.coalesce(func.sum(CostEntry.amount_usd), 0.0)).where(
                CostEntry.workspace_id == ws_id,
                CostEntry.created_at >= today,
            )
        )
    budget = get_settings().daily_budget_usd
    pct = (float(total) / budget * 100) if budget else 0
    return f"💸 <b>Today:</b> ${float(total):.4f} / ${budget:.2f} ({pct:.0f}% of budget)"


def handle_update(update: dict) -> None:
    """Process one Telegram update (message with optional /command)."""
    msg = update.get("message") or update.get("edited_message")
    if not msg:
        return
    chat = msg.get("chat", {})
    chat_id = str(chat.get("id", ""))
    text_in = (msg.get("text") or "").strip()
    if not chat_id or not text_in:
        return

    # ---- pairing: /start <CODE> ------------------------------------------
    if text_in.startswith("/start"):
        parts = text_in.split(maxsplit=1)
        code = parts[1].strip().upper() if len(parts) > 1 else ""
        if not code:
            # Plain /start: linked chats get help; unknown chats get instructions.
            with session_scope() as s:
                linked = s.scalar(
                    select(TelegramLink).where(
                        TelegramLink.chat_id == chat_id,
                        TelegramLink.active.is_(True),
                    )
                )
            greeting = _HELP if linked else (
                "👋 Welcome to YMONEY. Open the web app → Settings → Integrations, "
                "generate a pairing code, then send /start <code> here."
            )
            _schedule(send_message(chat_id, greeting))
            return
        entry = _PAIRING_CODES.get(code)
        if not entry or entry["expires_at"] < time.time():
            _PAIRING_CODES.pop(code, None)
            _schedule(send_message(
                chat_id,
                "❌ Invalid or expired pairing code. Generate a new one in YMONEY → Settings → Integrations.",
            ))
            return
        ws_id, user_id = entry["workspace_id"], entry["user_id"]
        with session_scope() as s:
            exists = s.scalar(
                select(TelegramLink).where(
                    TelegramLink.workspace_id == ws_id,
                    TelegramLink.chat_id == chat_id,
                )
            )
            if not exists:
                s.add(TelegramLink(workspace_id=ws_id, chat_id=chat_id,
                                   chat_title=chat.get("title") or chat.get("username") or "",
                                   linked_by_user_id=user_id, active=True))
        _PAIRING_CODES.pop(code, None)
        _schedule(send_message(chat_id, f"🔗 <b>Paired!</b> This chat now receives YMONEY alerts.\n{_HELP}"))
        events_service.record_event(ws_id, "telegram.linked", f"Telegram chat {chat_id} linked for remote control", "success", "telegram")
        return

    # ---- commands: require a linked chat ----------------------------------
    with session_scope() as s:
        link = s.scalar(
            select(TelegramLink).where(
                TelegramLink.chat_id == chat_id,
                TelegramLink.active.is_(True),
            )
        )
        ws_id = link.workspace_id if link else None
    if not ws_id:
        if text_in.startswith("/"):
            _schedule(send_message(chat_id, "This chat is not linked to any YMONEY workspace. Pair via Settings → Integrations."))
        return

    cmd = text_in.split()[0].split("@")[0].lower()
    reply: str | None = None

    try:
        if cmd in ("/help", "/start"):
            reply = _HELP
        elif cmd == "/status":
            reply = _workspace_brief(ws_id) + "\n" + _cost_brief(ws_id)
        elif cmd == "/run":
            from app.engine import autopilot as autopilot_engine

            autopilot_engine.start_autopilot(ws_id, mode="CONTINUOUS")
            reply = "▶️ Autopilot started."
        elif cmd == "/stop":
            from app.engine import autopilot as autopilot_engine

            autopilot_engine.stop_autopilot(ws_id)
            reply = "🛑 Stopping (running steps finish)."
        elif cmd == "/pause":
            from app.engine import autopilot as autopilot_engine

            autopilot_engine.pause_autopilot(ws_id)
            reply = "⏸ Pausing before next stage."
        elif cmd == "/resume":
            from app.engine import autopilot as autopilot_engine

            autopilot_engine.resume_autopilot(ws_id)
            reply = "▶️ Resumed."
        elif cmd == "/cycle":
            from app.engine import autopilot as autopilot_engine

            result = autopilot_engine.run_single_cycle(ws_id)
            reply = f"🔄 Cycle complete.\n{_workspace_brief(ws_id)}"
        elif cmd == "/cost":
            reply = _cost_brief(ws_id)
        else:
            reply = None  # not a command — ignore (e.g. someone chatting)
    except Exception as exc:
        reply = f"❌ {cmd} failed: {exc}"

    if reply:
        _schedule(send_message(chat_id, reply))


# ---------------------------------------------------------------------------
# Polling worker (long-poll getUpdates loop inside the API process)
# ---------------------------------------------------------------------------

_poll_task: asyncio.Task | None = None
_stop_event = asyncio.Event()


async def _poll_loop() -> None:
    offset = 0
    logger.info("telegram poller started")
    while not _stop_event.is_set():
        token, _ = get_bot_token()
        if not token:
            await asyncio.sleep(5)
            continue
        try:
            timeout_s = max(settings.telegram_poll_interval_seconds, 1.0)
            result = await _api(
                "getUpdates",
                {"offset": offset, "timeout": int(timeout_s), "allowed_updates": ["message"]},
                timeout=timeout_s + 10,
            )
            for upd in result:
                offset = max(offset, upd.get("update_id", 0) + 1)
                try:
                    handle_update(upd)
                except Exception:
                    logger.exception("telegram update handling failed")
        except TelegramError as exc:
            logger.warning(f"telegram poll: {exc}")
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"telegram poll unexpected error: {exc}")
            await asyncio.sleep(5)
    logger.info("telegram poller stopped")


def start_poller() -> None:
    """Start the long-poll task (idempotent)."""
    global _poll_task, _stop_event
    if _poll_task is not None and not _poll_task.done():
        return
    _stop_event = asyncio.Event()
    _poll_task = asyncio.get_running_loop().create_task(_poll_loop())


async def stop_poller() -> None:
    global _poll_task
    if _poll_task is not None:
        _stop_event.set()
        _poll_task.cancel()
        try:
            await _poll_task
        except (asyncio.CancelledError, Exception):
            pass
        _poll_task = None

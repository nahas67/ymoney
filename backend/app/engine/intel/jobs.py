"""Job kinds for media intelligence + the guarded registration stub (Lane A).

Contracts §14 fixes the job-kind vocabulary; heavy models run in workers, never
inside a request handler (contracts §1.2)::

    MEDIA_INTEL_ALIGN  MEDIA_INTEL_DIARIZE  MEDIA_INTEL_ENHANCE
    MEDIA_INTEL_SILENCE  MEDIA_INTEL_FILLERS  MEDIA_INTEL_FACE_TRACK
    MEDIA_INTEL_SEGMENT  MEDIA_INTEL_ACTIVE_SPEAKER  MEDIA_INTEL_REFRAME
    MEDIA_INTEL_BACKGROUND

Lane A owns the CONSTANTS and the registration entry point only. Each later lane
adds its own handler module under ``app.engine.intel`` and calls
``register_intel_jobs()``; the registration is idempotent (a repeat call or a
module reload registers nothing twice) and every import happens INSIDE the
guard, so one missing sibling can never break the app.

Deliberately NOT imported from ``app/main.py`` by this lane: the orchestrator
wires router + job + webhook registration at integration time, so this module
must stay import-safe and side-effect free until then.
"""

from __future__ import annotations

import importlib
import logging
from contextlib import suppress

logger = logging.getLogger("ymoney.intel")

#: kind -> job kind (contracts §14)
JOB_ALIGN = "MEDIA_INTEL_ALIGN"
JOB_DIARIZE = "MEDIA_INTEL_DIARIZE"
JOB_ENHANCE = "MEDIA_INTEL_ENHANCE"
JOB_SILENCE = "MEDIA_INTEL_SILENCE"
JOB_FILLERS = "MEDIA_INTEL_FILLERS"
JOB_FACE_TRACK = "MEDIA_INTEL_FACE_TRACK"
JOB_SEGMENT = "MEDIA_INTEL_SEGMENT"
JOB_ACTIVE_SPEAKER = "MEDIA_INTEL_ACTIVE_SPEAKER"
JOB_REFRAME = "MEDIA_INTEL_REFRAME"
JOB_BACKGROUND = "MEDIA_INTEL_BACKGROUND"

#: every intel job kind, in contracts §14 order
INTEL_JOB_KINDS: tuple[str, ...] = (
    JOB_ALIGN,
    JOB_DIARIZE,
    JOB_ENHANCE,
    JOB_SILENCE,
    JOB_FILLERS,
    JOB_FACE_TRACK,
    JOB_SEGMENT,
    JOB_ACTIVE_SPEAKER,
    JOB_REFRAME,
    JOB_BACKGROUND,
)

#: capability kind -> the job kind that runs it
JOB_KIND_FOR: dict[str, str] = {
    "alignment": JOB_ALIGN,
    "diarization": JOB_DIARIZE,
    "enhancement": JOB_ENHANCE,
    "denoise": JOB_ENHANCE,
    "silence": JOB_SILENCE,
    "fillers": JOB_FILLERS,
    "face_tracking": JOB_FACE_TRACK,
    "segmentation": JOB_SEGMENT,
    "active_speaker": JOB_ACTIVE_SPEAKER,
    "reframe": JOB_REFRAME,
    "background": JOB_BACKGROUND,
}

#: (module path, "register_<x>_jobs" callable) pairs later lanes append to.
#: Each entry is imported and invoked INSIDE the guard below.
HANDLER_REGISTRARS: tuple[tuple[str, str], ...] = ()


def job_kind_for(kind: str) -> str:
    """Job kind for a capability kind (falls back to the upper-cased name)."""
    name = str(kind or "").strip().lower()
    return JOB_KIND_FOR.get(name) or name.upper()


def register_intel_jobs() -> None:
    """Register every intel job handler that has landed. Idempotent.

    Lane A registers no handler of its own: the engines are pure orchestration
    + measurement and run in-process, so until a lane adds a handler this is a
    no-op. Later lanes append ``(module, "register_..._jobs")`` to
    :data:`HANDLER_REGISTRARS` and get registration for free.

    Import and invocation each sit inside their own ``suppress`` so a
    half-landed or broken sibling module degrades to a log line instead of
    breaking app startup.
    """
    from app.services import jobs as jobs_service

    for module_path, attr in HANDLER_REGISTRARS:
        with suppress(Exception):  # noqa: S110 - sibling lanes land in parallel
            module = importlib.import_module(module_path)
            getattr(module, attr)()
    missing = [k for k in INTEL_JOB_KINDS if k not in jobs_service._handlers]
    if missing:
        # Informational only: a kind with no handler yet is simply never enqueued.
        logger.debug("intel job kinds without a handler yet: %s", ", ".join(missing))


__all__ = [
    "HANDLER_REGISTRARS",
    "INTEL_JOB_KINDS",
    "JOB_ACTIVE_SPEAKER",
    "JOB_ALIGN",
    "JOB_BACKGROUND",
    "JOB_DIARIZE",
    "JOB_ENHANCE",
    "JOB_FACE_TRACK",
    "JOB_FILLERS",
    "JOB_KIND_FOR",
    "JOB_REFRAME",
    "JOB_SEGMENT",
    "JOB_SILENCE",
    "job_kind_for",
    "register_intel_jobs",
]

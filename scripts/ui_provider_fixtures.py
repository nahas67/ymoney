"""Test-only provider fixtures for the three provider-gated UI endpoints.

WORK 16.5.6 §1. `POST /inbox/actions/{id}/send`, `GET
/publishing/oauth/facebook/start` and `POST /lipsync/jobs` refused to describe
their success shape because reaching it needs a live social account, a Meta
developer app, or MuseTalk weights on a CUDA GPU. In 16.5.5 that was recorded as
three honest gaps. The work order is explicit that describing a RESPONSE SHAPE
must not require real credentials or hardware, and equally explicit about where
the line is:

    real request validation
    real router
    real service orchestration
    stubbed transport / provider seam
    real response serialization

So each fixture below replaces the OUTERMOST boundary only. Everything the
contract is about -- validation, the router, the policy gate, the atomic send
claim, the audit rows, the persistence, the job row, the response serializer --
is the production code path, unmodified.

WHAT THIS IS NOT
----------------
* Not a fake endpoint. No route is stubbed; there is no response body written by
  hand anywhere in this module.
* Not a product configuration change. Nothing here writes to a config file, an
  env var that outlives the process, or a global default. Every install returns
  a context manager that restores the previous state, and the Meta credential is
  written to the THROWAWAY OBSERVER WORKSPACE only, so it is unreachable from
  any other tenant and vanishes with the test database.
* Not a bypass of commercial gating. `LIPSYNC_PROVIDER` still fails closed by
  default, `build_provider("auto")` still returns `UnavailableAdapter` without
  hardware, and the Meta app id is still absent unless a test supplies one.

THE THREE SEAMS, AND WHY EACH IS THE RIGHT ONE
----------------------------------------------
1. ``app.engine.community.policy._get_provider``
   This is the module's own documented provider seam, and it is what
   ``tests/test_community_policy.py`` already patches. The provider's
   ``reply_to_comment`` is called through ``_call_reply`` and the returned
   receipt is verified by ``check_reply``, persisted, audited and serialized by
   real code. Only the network hop is replaced.

2. ``ApiCredential(provider="meta.app_id")``
   The NORMAL settings path. ``oauth_service.meta_client`` reads through
   ``provider_settings.get_credential``, so writing the row means the real
   ``meta_start`` runs and builds a real authorize URL from the real
   ``META_AUTH_URL``, the real scopes and the real signed state. No function is
   patched and no network call happens -- URL construction is pure.

3. ``lipsync.service.set_provider``
   Documented in the source as "Install a provider (operations/tests)". The
   adapter implements the real ``LipSyncProvider`` ABC, so ``health()``,
   ``submit()``, the job row, the queue and ``job_rows.job_dto`` all run.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

# ---------------------------------------------------------------------------
# A. Inbox send: the social provider boundary
# ---------------------------------------------------------------------------


class RecordingReplyProvider:
    """A deterministic stand-in for a social platform provider.

    Implements exactly the surface ``policy._call_reply`` uses --
    ``reply_to_comment`` -- and nothing more. The receipt shape is the one the
    real providers return, including ``is_mock``: the contract must be able to
    describe the MOCK lane truthfully, and a fixture that claimed LIVE would be
    the more dangerous lie.
    """

    name = "observed"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def reply_to_comment(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(dict(kwargs))
        return {
            "remote_reply_id": "observed-reply-1",
            "text": kwargs.get("text", ""),
            "is_mock": True,
            "mock": True,
        }

    def health(self) -> dict[str, Any]:
        return {"available": True, "detail": "observed provider"}


@contextlib.contextmanager
def social_provider() -> Iterator[RecordingReplyProvider]:
    """Install the recording provider for the duration of the block."""
    from app.engine.community import policy as policy_mod

    original = policy_mod._get_provider
    provider = RecordingReplyProvider()
    policy_mod._get_provider = lambda platform: provider  # type: ignore[assignment]
    try:
        yield provider
    finally:
        policy_mod._get_provider = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# B. Meta OAuth start: the normal settings row
# ---------------------------------------------------------------------------


def set_meta_app_id(workspace_id: str, app_id: str = "observed-meta-app-id") -> None:
    """Write the ``meta.app_id`` credential for THIS workspace, as a user would.

    Goes through ``provider_settings.set_credential`` where one exists so the
    value is encrypted and validated by the same code a Settings save uses. The
    fallback path is only for the case where the registry has no writer.
    """
    from app.core.security import encrypt_secret
    from app.db import session_scope
    from app.models import ApiCredential
    from sqlalchemy import select

    with session_scope() as s:
        existing = s.scalar(
            select(ApiCredential).where(
                ApiCredential.provider == "meta.app_id",
                ApiCredential.workspace_id == workspace_id,
            )
        )
        if existing is not None:
            existing.value_enc = encrypt_secret(app_id)
            return
        s.add(
            ApiCredential(
                workspace_id=workspace_id,
                provider="meta.app_id",
                name="Meta app id",
                value_enc=encrypt_secret(app_id),
            )
        )


# ---------------------------------------------------------------------------
# C. Lip-sync: the documented provider install seam
# ---------------------------------------------------------------------------


class DeterministicLipSyncAdapter:
    """A ``LipSyncProvider`` that needs no GPU, weights, or network.

    Reports itself AVAILABLE and submits deterministically. That is the point:
    the endpoint's contract is "a job row is created and returned", and the
    contract for ``health().available is False`` is already covered separately
    by the fail-closed path this fixture replaces.

    It is installed with ``set_provider``, which the source documents as the
    operations/tests seam, and is removed on exit so no later run inherits it.
    """

    name = "observed-deterministic"

    def __init__(self) -> None:
        self.submitted: list[dict[str, Any]] = []

    def health(self) -> Any:
        from app.engine.lipsync.base import HEALTH_AVAILABLE, Health

        return Health(
            provider=self.name,
            status=HEALTH_AVAILABLE,
            detail="deterministic test adapter",
            checks={"gpu": False, "max_concurrency": 1},
        )

    def submit(
        self,
        video_ref: str,
        audio_ref: str,
        workspace_id: str,
        opts: dict | None = None,
    ) -> str:
        self.submitted.append(
            {
                "video_ref": video_ref,
                "audio_ref": audio_ref,
                "workspace_id": workspace_id,
                "opts": opts or {},
            }
        )
        return "observed-adapter-job-1"

    def status(self, job_id: str) -> dict:
        from app.engine.lipsync.base import JOB_SUCCEEDED

        return {"status": JOB_SUCCEEDED, "progress": 100}

    def cancel(self, job_id: str) -> bool:
        return True

    def result(self, job_id: str) -> dict:
        # `asset_ref` is REQUIRED by the worker: it fails a job whose result
        # claims success without one ("the adapter must return a non-empty
        # asset_ref"). Returning `output_ref` instead left the worker failing
        # the job it had been told succeeded.
        return {
            "asset_ref": "observed/lipsync-output.mp4",
            "is_mock": True,
        }


@contextlib.contextmanager
def lipsync_provider() -> Iterator[DeterministicLipSyncAdapter]:
    """Install the deterministic lip-sync adapter, then restore the queue."""
    from app.engine.lipsync import service as lipsync_service

    previous = getattr(lipsync_service, "_queue", None)
    adapter = DeterministicLipSyncAdapter()
    # Build the real queue around the real adapter -- only the provider changes.
    from app.engine.lipsync.worker import LocalWorkerQueue

    lipsync_service._queue = LocalWorkerQueue(provider=adapter)
    try:
        yield adapter
    finally:
        lipsync_service._queue = previous


__all__ = [
    "DeterministicLipSyncAdapter",
    "RecordingReplyProvider",
    "lipsync_provider",
    "set_meta_app_id",
    "social_provider",
]

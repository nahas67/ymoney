"""Publication mode: the four outcomes that must never be confused.

Work 14 §5/§9. Publishing can end in four genuinely different states, and the
single most damaging bug in a distribution system is calling one of them
``PUBLISHED``:

``LIVE``
    A real official platform API accepted the content and returned a remote
    identifier we can point at.
``MOCK``
    Nothing was sent. A local stand-in recorded the intent (dev / no account /
    ``MOCK_PUBLISHING=true``). Never evidence of a live post.
``HANDOFF``
    We prepared verified, platform-legal media and handed it to a HUMAN who
    publishes it themselves. Snapchat organic publishing is modelled this way
    because no official server-side publishing API exists -- see
    :mod:`app.providers.publishers.snapchat`. A handoff is *prepared*, never
    *published*.
``UNAVAILABLE``
    The platform declares the capability, or the operator asked for it, but the
    preconditions are not met (no account, missing scope, unsupported media).
    Explicitly NOT a silent no-op.

``is_live`` is deliberately narrow: only ``LIVE`` is True. A handoff is real
work with real output, and conflating it with LIVE is precisely the failure
Work 14 §5 calls out as FAILED, so ``HANDOFF`` is never folded into LIVE here.
"""

from __future__ import annotations

from enum import StrEnum

__all__ = ["PublicationMode", "classify_publication", "assert_not_confused"]


class PublicationMode(StrEnum):
    """The four honest outcomes of a publish attempt."""

    LIVE = "LIVE"
    MOCK = "MOCK"
    HANDOFF = "HANDOFF"
    UNAVAILABLE = "UNAVAILABLE"

    def __str__(self) -> str:
        return self.value

    @property
    def is_live(self) -> bool:
        """True ONLY for a real, remote, verifiable publication."""
        return self is PublicationMode.LIVE

    @property
    def is_evidence_of_publication(self) -> bool:
        """True when the mode may be reported as 'published' downstream.

        A handoff is excluded on purpose: the user still has to tap publish.
        """
        return self is PublicationMode.LIVE


def classify_publication(
    *,
    is_mock: bool = False,
    handoff_required: bool = False,
    unavailable_reason: str = "",
    remote_id: str = "",
) -> PublicationMode:
    """Derive the mode from the facts, refusing contradictory combinations.

    Precedence is ``UNAVAILABLE`` > ``HANDOFF`` > ``MOCK`` > ``LIVE``: an attempt
    that failed a precondition cannot be evidence of anything, and a handoff
    that is also flagged mock is a handoff (the mock part is a testing detail,
    the handoff part is the real user contract).

    The one hard rule: a ``MOCK`` or ``HANDOFF`` outcome must NOT carry a remote
    id, because a remote id is what downstream verification reads as proof.
    """
    if unavailable_reason:
        return PublicationMode.UNAVAILABLE
    if handoff_required:
        return PublicationMode.MOCK if is_mock else PublicationMode.HANDOFF
    if is_mock:
        return PublicationMode.MOCK
    return PublicationMode.LIVE if remote_id else PublicationMode.UNAVAILABLE


def assert_not_confused(mode: PublicationMode | str, *, expect_live: bool) -> None:
    """Fail closed when a caller treats a non-live outcome as a live post.

    Used at the boundary where publication state is turned into a user-visible
    "published" claim (campaign completion, UI badge, analytics ingest).
    """
    value = PublicationMode(str(mode))
    if value.is_live and not expect_live:
        raise ValueError(
            f"publication mode {value} cannot satisfy a non-live expectation")
    if expect_live and not value.is_live:
        raise ValueError(
            f"publication mode {value} is not LIVE and cannot be reported as "
            f"published (handoff/mock/unavailable are never live evidence)")

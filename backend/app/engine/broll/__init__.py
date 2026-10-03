"""B-roll sourcing policy (Work 15.5).

Two pure decision layers that sit between the stock providers and the
compositor. Both exist because the provider layer must not be where a
*visual* judgement is made:

* :mod:`app.engine.broll.aspect` — does this asset's orientation match the
  frame we are cutting? A 9:16 job that silently takes a 16:9 clip does not
  fail, it crops the subject out of frame, so orientation is re-verified at
  the moment the rendition is chosen rather than trusted from the search call.
* :mod:`app.engine.broll.allocation` — which clip goes to which scene? A
  repeated clip reads as a bug to the viewer, so allocation groups clips by
  keyword, prefers never-used sources, and only reuses a source once its
  group's candidates are exhausted.

Nothing here performs I/O; the inputs are plain data, so the policy can be
reasoned about (and tested) without a network or a database.
"""

from __future__ import annotations

from app.engine.broll.allocation import (
    MODES,
    REUSE_WARNING_CODE,
    SEQUENTIAL,
    SHORT_WARNING_CODE,
    UNIQUE,
    Allocation,
    AllocationBatch,
    Clip,
    OutputAllocation,
    allocate_batch,
    next_allocation,
    order_clips,
    record_usage,
    reused_per_output,
)
from app.engine.broll.aspect import (
    LANDSCAPE,
    PORTRAIT,
    SQUARE,
    UNKNOWN,
    frame_kind,
    matches_aspect,
    orientation_of,
)

__all__ = [
    "LANDSCAPE",
    "MODES",
    "PORTRAIT",
    "REUSE_WARNING_CODE",
    "SEQUENTIAL",
    "SHORT_WARNING_CODE",
    "SQUARE",
    "UNIQUE",
    "UNKNOWN",
    "Allocation",
    "AllocationBatch",
    "Clip",
    "OutputAllocation",
    "allocate_batch",
    "frame_kind",
    "matches_aspect",
    "next_allocation",
    "order_clips",
    "orientation_of",
    "record_usage",
    "reused_per_output",
]

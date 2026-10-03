"""Material diversity allocation (Work 15.5).

The planner used to hand every scene the same handful of stock queries::

    for i in range(n):
        kw = kws[i % len(kws)]        # 2 keywords, 8 scenes -> 2 distinct queries, 4x each

Search results repeat too: a keyword's candidate list is stable, so the
least-used pick is the same clip every time unless something tracks usage.
The net effect is a cut whose every third shot is identical, which reads as a
bug even when every individual clip is valid.

So allocation is explicit and stateful across the outputs of one batch:

* group clips by the keyword that produced them, so the *keyword order* of the
  script survives allocation;
* inside a group, never-used sources come first (stable sort, so equally-used
  sources keep the caller's order — determinism is a product requirement, not
  a tie-break detail);
* round-robin across groups with :func:`itertools.zip_longest` so one busy
  keyword cannot starve the rest;
* reuse is only ever a *fallback*. A source is reused once the allocation runs
  dry, never because it sorts first;
* a source whose render failed never reaches the usage counter. That is why
  :func:`record_usage` exists separately from :func:`next_allocation`: the
  planner reads committed history, and the caller commits only what landed.

Every function here is pure. It takes a materialized clip list plus a
``{source_id: times_used}`` counter and returns new values; it never mutates
its arguments and never touches the network, the database, or the clock.
"""

from __future__ import annotations

import itertools
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

#: Emitted when an output had to fall back to an already-used source.
REUSE_WARNING_CODE = "broll_materials_reused"
#: Emitted when the materialized pool could not even fill one output.
SHORT_WARNING_CODE = "broll_materials_short"

#: Orderings. ``sequential`` preserves the script's keyword order (the default
#: for scene plans); ``unique`` promotes one clip per source ahead of the extra
#: slices of the same source, which is the right shape when one long recording
#: has been cut into several sub-clips.
SEQUENTIAL = "sequential"
UNIQUE = "unique"
MODES = (SEQUENTIAL, UNIQUE)


@dataclass(frozen=True)
class Clip:
    """One materialized clip as the allocator sees it.

    ``source_id`` is the material's identity, not its path: two sub-clips cut
    from the same download share a ``source_id`` so usage is counted once per
    original asset, while ``group`` is the keyword that searched for it.
    """

    source_id: str
    group: str = ""
    duration: float = 0.0
    path: str = ""


@dataclass(frozen=True)
class Allocation:
    """One chosen slot, with the reuse evidence that justified it."""

    clip: Clip
    uses_before: int

    @property
    def reused(self) -> bool:
        """True when this source had already been used in an earlier output."""
        return self.uses_before > 0


@dataclass(frozen=True)
class OutputAllocation:
    """The clips assigned to one output (a scene, or one video of a batch)."""

    index: int
    allocations: tuple[Allocation, ...] = ()
    requested: int = 0

    @property
    def clips(self) -> list[Clip]:
        return [a.clip for a in self.allocations]

    @property
    def source_ids(self) -> list[str]:
        return [a.clip.source_id for a in self.allocations]

    @property
    def reused_sources(self) -> list[str]:
        return list(dict.fromkeys(
            a.clip.source_id for a in self.allocations if a.reused))

    @property
    def short_by(self) -> int:
        """Slots the pool could not fill (0 when the output is complete)."""
        return max(0, self.requested - len(self.allocations))


@dataclass(frozen=True)
class AllocationBatch:
    """Whole-batch plan: per-output picks, the reuse warnings, and the result.

    ``usage`` is the counter *after* every output committed, ready to be
    stored for the next batch. ``source_usage`` is echoed so a caller can tell
    an untouched batch from one that merely repeated a source it had already
    spent.
    """

    outputs: tuple[OutputAllocation, ...] = ()
    source_usage: Mapping[str, int] = field(default_factory=dict)

    @property
    def warnings(self) -> list[dict]:
        """Warnings the caller must surface: reuse and shortage, never silence."""
        out: list[dict] = []
        for out_ in self.outputs:
            if out_.reused_sources:
                out.append({"code": REUSE_WARNING_CODE,
                            "output_index": out_.index,
                            "count": len(out_.reused_sources)})
            if out_.short_by:
                out.append({"code": SHORT_WARNING_CODE,
                            "output_index": out_.index,
                            "missing": out_.short_by})
        return out


def order_clips(clips: Iterable[Clip], source_usage: Mapping[str, int] | None = None,
                *, mode: str = SEQUENTIAL) -> list[Clip]:
    """Order clips for playback: unexhausted sources first, keywords in order.

    ``SEQUENTIAL`` groups by keyword, stable-sorts each group by prior usage,
    then round-robins across the groups. ``UNIQUE`` instead takes the longest
    clip of every source as its primary and defers the remaining slices of the
    same source — a long recording cut into fragments otherwise contributes a
    ragged tail fragment before its own best material.
    """
    if mode not in MODES:
        raise ValueError(f"unknown allocation mode '{mode}' (expected one of {MODES})")
    items = list(clips or [])
    if not items:
        return []
    usage = source_usage or {}
    if mode == UNIQUE:
        return _unique_order(items, usage)
    if not any(clip.group for clip in items):
        # No keyword recorded anywhere (no manifest, no plan): there is no group
        # order to preserve, so the honest allocation is a flat least-used sort.
        return sorted(items, key=lambda c: usage.get(c.source_id, 0))
    groups: dict[str, list[Clip]] = {}
    for clip in items:
        groups.setdefault(clip.group, []).append(clip)
    for group in groups.values():
        group.sort(key=lambda c: usage.get(c.source_id, 0))
    return [c for row in itertools.zip_longest(*groups.values()) for c in row if c is not None]


def _unique_order(items: list[Clip], usage: Mapping[str, int]) -> list[Clip]:
    by_source: dict[str, list[Clip]] = {}
    for clip in items:
        by_source.setdefault(clip.source_id, []).append(clip)
    primary: list[Clip] = []
    overflow: list[Clip] = []
    for slices in by_source.values():
        best = max(slices, key=lambda c: c.duration)
        primary.append(best)
        overflow.extend(c for c in slices if c is not best)
    primary.sort(key=lambda c: usage.get(c.source_id, 0))
    overflow.sort(key=lambda c: usage.get(c.source_id, 0))
    return primary + overflow


def record_usage(source_usage: Mapping[str, int] | None,
                 used_source_ids: Iterable[str]) -> dict[str, int]:
    """Return a NEW usage counter with ``used_source_ids`` incremented.

    The caller decides what reaches this function, which is the whole point:
    pass only the clips that actually rendered, and a failed download or an
    unreadable file leaves its source unspent for the next output.
    """
    out = dict(source_usage or {})
    for raw in used_source_ids or ():
        key = str(raw or "").strip()
        if not key:
            continue
        out[key] = out.get(key, 0) + 1
    return out


def next_allocation(clips: Iterable[Clip], source_usage: Mapping[str, int] | None,
                    index: int, count: int, *, mode: str = SEQUENTIAL) -> OutputAllocation:
    """Allocate ``count`` clips for one output from committed usage alone."""
    wanted = max(0, int(count))
    usage = source_usage or {}
    ordered = order_clips(clips, usage, mode=mode)
    allocations = tuple(
        Allocation(clip=clip, uses_before=int(usage.get(clip.source_id, 0)))
        for clip in ordered[:wanted]
    )
    return OutputAllocation(index=int(index), allocations=allocations, requested=wanted)


def allocate_batch(clips: Iterable[Clip], source_usage: Mapping[str, int] | None = None, *,
                   outputs: int = 1, per_output: int = 1,
                   mode: str = SEQUENTIAL) -> AllocationBatch:
    """Allocate a whole batch, each output seeing the previous outputs' usage.

    This is the all-succeeds path: every provisionally selected clip is
    committed before the next output is planned. When a render can fail, drive
    :func:`next_allocation` and :func:`record_usage` directly instead.
    """
    items: Sequence[Clip] = tuple(clips or ())
    usage = dict(source_usage or {})
    planned: list[OutputAllocation] = []
    for i in range(max(0, int(outputs))):
        out = next_allocation(items, usage, i, per_output, mode=mode)
        planned.append(out)
        usage = record_usage(usage, out.source_ids)
    return AllocationBatch(outputs=tuple(planned), source_usage=usage)


def reused_per_output(batch: AllocationBatch) -> list[list[str]]:
    """Per-output reuse report, in output order (empty list = no reuse)."""
    return [out.reused_sources for out in batch.outputs]


__all__ = [
    "MODES",
    "REUSE_WARNING_CODE",
    "SHORT_WARNING_CODE",
    "SEQUENTIAL",
    "UNIQUE",
    "Allocation",
    "AllocationBatch",
    "Clip",
    "OutputAllocation",
    "allocate_batch",
    "next_allocation",
    "order_clips",
    "record_usage",
    "reused_per_output",
]

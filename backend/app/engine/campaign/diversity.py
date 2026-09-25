"""MMR-style diverse moment selection for campaign derivation.

Pure functions + a small selector class. Candidates are plain dicts; key
lookup is liberal so miners (LinkMiner moments, chapter peaks, scene
highlights) all work without adapters:

    id: "id" | "moment_id"
    virality: "virality_score" | "score"
    topic text: "topic" | "title" | "hook" | "text"
    transcript: "transcript" | "text"
    chapter: "chapter" | "chapter_id" | "chapter_idx"
    scenes: "scene_ids" | "scenes" (list)

Guarantees:
    - no two selected clips share >80% transcript overlap (never relaxed)
    - max 2 clips per chapter unless the candidate pool forces it
    - hook wording stays distinct (hook Jaccard < 80%)
"""

from __future__ import annotations

import re

from app.engine.decision import topic_similarity

MAX_TRANSCRIPT_OVERLAP = 0.8
MAX_HOOK_SIMILARITY = 0.8
MAX_PER_CHAPTER = 2

_STOPWORDS = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for", "with",
    "is", "are", "was", "were", "be", "been", "this", "that", "it", "its",
    "at", "by", "from", "as", "vs", "your", "you", "we", "my",
})


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOPWORDS}


def transcript_overlap(a: str, b: str) -> float:
    """Token-Jaccard overlap between two transcripts (0..1)."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / len(ta | tb)


def scene_overlap(a: list, b: list) -> float:
    """Overlap coefficient between two scene-id sets (0..1)."""
    sa, sb = set(a or []), set(b or [])
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / min(len(sa), len(sb))


def _candidate_text(cand: dict) -> str:
    for key in ("topic", "title", "hook", "text"):
        val = cand.get(key)
        if val:
            return str(val)
    return ""


def _candidate_transcript(cand: dict) -> str:
    for key in ("transcript", "text", "hook"):
        val = cand.get(key)
        if val:
            return str(val)
    return ""


def _candidate_chapter(cand: dict) -> str:
    for key in ("chapter", "chapter_id", "chapter_idx"):
        val = cand.get(key)
        if val is not None and str(val) != "":
            return str(val)
    return ""


def _candidate_scenes(cand: dict) -> list:
    for key in ("scene_ids", "scenes"):
        val = cand.get(key)
        if val:
            return list(val)
    return []


def _candidate_score(cand: dict) -> float:
    for key in ("virality_score", "score"):
        try:
            return float(cand.get(key) or 0.0)
        except (TypeError, ValueError):
            continue
    return 0.0


def _candidate_id(cand: dict, index: int) -> str:
    return str(cand.get("id") or cand.get("moment_id") or f"cand-{index}")


def _pairwise_similarity(a: dict, b: dict) -> float:
    """Max over topic/hook similarity, scene overlap, transcript overlap."""
    return max(
        topic_similarity(_candidate_text(a), _candidate_text(b)),
        scene_overlap(_candidate_scenes(a), _candidate_scenes(b)),
        transcript_overlap(_candidate_transcript(a), _candidate_transcript(b)),
    )


class CampaignDiversitySelector:
    """Greedy MMR selection with hard diversity guards."""

    def __init__(
        self,
        *,
        max_overlap: float = MAX_TRANSCRIPT_OVERLAP,
        max_per_chapter: int = MAX_PER_CHAPTER,
        relevance_weight: float = 0.7,
    ) -> None:
        self.max_overlap = max_overlap
        self.max_per_chapter = max_per_chapter
        self.relevance_weight = relevance_weight

    def select(
        self,
        candidates: list[dict],
        n: int,
        exclude_ids: set[str] | list[str] | None = None,
    ) -> list[dict]:
        """Pick up to n diverse candidates, highest virality first.

        The transcript-overlap guard is never relaxed; the per-chapter cap
        is relaxed only when the pool cannot fill n without breaking it.
        """
        excluded = set(exclude_ids or ())
        pool = [
            c for i, c in enumerate(candidates or [])
            if _candidate_id(c, i) not in excluded
        ]
        pool.sort(key=_candidate_score, reverse=True)
        selected: list[dict] = []
        for relax_chapters in (False, True):
            for cand in pool:
                if len(selected) >= n:
                    break
                if any(c is cand for c in selected):
                    continue
                if self._violates_overlap(cand, selected):
                    continue
                if not relax_chapters and self._violates_chapter_cap(cand, selected):
                    continue
                if self._violates_hook(cand, selected):
                    continue
                selected.append(cand)
            if len(selected) >= n:
                break
        return selected[:n]

    def _violates_overlap(self, cand: dict, selected: list[dict]) -> bool:
        text = _candidate_transcript(cand)
        if not text:
            return False
        return any(
            transcript_overlap(text, _candidate_transcript(s)) > self.max_overlap
            for s in selected
        )

    def _violates_chapter_cap(self, cand: dict, selected: list[dict]) -> bool:
        chapter = _candidate_chapter(cand)
        if not chapter:
            return False
        count = sum(1 for s in selected if _candidate_chapter(s) == chapter)
        return count >= self.max_per_chapter

    def _violates_hook(self, cand: dict, selected: list[dict]) -> bool:
        hook = str(cand.get("hook") or "")
        if not hook:
            return False
        return any(
            topic_similarity(hook, str(s.get("hook") or "")) > MAX_HOOK_SIMILARITY
            for s in selected
            if s.get("hook")
        )

    def rank_mmr(self, candidates: list[dict], selected: list[dict]) -> list[tuple[float, dict]]:
        """MMR score of each candidate against the current selection."""
        ranked = []
        for cand in candidates or []:
            relevance = _candidate_score(cand)
            redundancy = max((_pairwise_similarity(cand, s) for s in selected), default=0.0)
            mmr = self.relevance_weight * relevance - (1.0 - self.relevance_weight) * redundancy * 100.0
            ranked.append((mmr, cand))
        ranked.sort(key=lambda t: -t[0])
        return ranked


def select_diverse(
    candidates: list[dict],
    n: int,
    exclude_ids: set[str] | list[str] | None = None,
) -> list[dict]:
    """Convenience wrapper with default guards."""
    return CampaignDiversitySelector().select(candidates, n, exclude_ids)

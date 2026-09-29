"""Deterministic text normalization for GlobalMemory + the knowledge graph.

Shared by every knowledge lane so dedupe keys, conflict detection and graph
``topic_key`` joins all agree byte-for-byte:

* ``normalize_text``  -- lowercase, collapse whitespace, strip
* ``content_hash_of`` -- sha256 hex of the normalized text (64 chars)
* ``topic_key_of``    -- "" for an empty topic, else sha256 hex of the
                         normalized topic (Topic nodes, memories and edges
                         all derive this identically)

Pure functions, no I/O. A memory stored twice with the same content always
produces the same ``content_hash``; two rows sharing a normalized topic
always share a ``topic_key`` — that is what makes service-level dedupe and
auto-conflict detection deterministic.
"""
from __future__ import annotations

import hashlib


def normalize_text(value) -> str:
    """Lowercase, collapse all whitespace runs to one space, strip."""
    return " ".join(str(value or "").lower().split())


def content_hash_of(value) -> str:
    """sha256 hex (64 chars) of the normalized text — dedupe/conflict key."""
    return hashlib.sha256(normalize_text(value).encode("utf-8")).hexdigest()


def topic_key_of(topic) -> str:
    """Dedupe key for a topic; ``""`` when there is no topic.

    Empty keys never auto-conflict: two topic-less facts are unrelated
    statements, not competing answers to the same question.
    """
    normalized = normalize_text(topic)
    if not normalized:
        return ""
    return content_hash_of(normalized)


__all__ = [
    "content_hash_of",
    "normalize_text",
    "topic_key_of",
]

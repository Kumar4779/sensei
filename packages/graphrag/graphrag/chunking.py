"""Chunking (GraphRAG Slice 1): labels ride with every chunk.

A chunk is a run of whole sentences of at most MAX_WORDS words, with one
sentence of overlap between neighbours. Each chunk copies its document's
labels: doc_id, tier, ACL groups and canary flag. Retrieval filters chunks, not
documents, so a chunk that had lost its document's ACL could not be filtered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

MAX_WORDS = 45
OVERLAP_SENTENCES = 1
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class Chunk:
    chunk_id: str
    doc_id: str
    title: str
    tier: str
    acl: tuple[str, ...]
    canary: bool
    text: str


def sentences(text: str) -> list[str]:
    return [s for s in _SENTENCE_END.split(text.strip()) if s]


def chunk_document(doc: dict[str, Any], max_words: int = MAX_WORDS, overlap: int = OVERLAP_SENTENCES) -> list[Chunk]:
    if not doc.get("acl"):
        raise ValueError(f"{doc.get('doc_id')!r} has no ACL; every document must name its allowed groups")
    parts = sentences(doc["body"])
    groups: list[list[str]] = []
    start = 0
    while start < len(parts):
        end, words = start, 0
        while end < len(parts) and (end == start or words + len(parts[end].split()) <= max_words):
            words += len(parts[end].split())
            end += 1
        groups.append(parts[start:end])
        if end >= len(parts):
            break
        start = max(end - overlap, start + 1)
    return [Chunk(chunk_id=f"{doc['doc_id']}#c{i}", doc_id=doc["doc_id"], title=doc["title"], tier=doc["tier"],
                  acl=tuple(doc["acl"]), canary=bool(doc["canary"]), text=" ".join(g))
            for i, g in enumerate(groups)]


def chunk_corpus(documents: list[dict[str, Any]], **kwargs: Any) -> list[Chunk]:
    return [c for doc in documents for c in chunk_document(doc, **kwargs)]

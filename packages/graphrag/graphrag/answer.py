"""Flat answering path (GraphRAG Slice 1): answers from retrieved chunks only, every claim cited.

The composer is extractive and uses no model. Each claim is a sentence copied
from a retrieved chunk, cited by that chunk's ID, so the answer can only
contain permitted text, and every claim has a citation by construction. A
generative composer behind the same Answer shape is a later slice. The
deterministic verifier (Sunday block) will check its citations against the
retrieved set.

Support is decided in code, with statistics from the requester's permitted
partition. A sentence (read with its chunk's title) supports the question when:
  - it contains every *specific* question term: one that appears in the
    permitted partition but in at most SPECIFIC_DF_FRACTION of its chunks. A
    question naming the EMEA region is not answered with the Southeast policy;
  - it covers at least SUPPORT_THRESHOLD of the question's IDF-weighted
    content terms. A term no permitted chunk contains gets the highest IDF, so
    a question about material outside the requester's access loses most of
    its weight before any sentence is scored.
Further claims must come within SECONDARY_RATIO of the best claim's support.
So a question about something the requester can't see finds no support and
abstains. Abstaining says nothing about whether restricted material exists.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from graphrag.acl import Requester
from graphrag.chunking import sentences
from graphrag.retrieval import DEFAULT_K, Retrieval, Retriever
from graphrag.text import tokenize

SUPPORT_THRESHOLD = 0.6
SPECIFIC_DF_FRACTION = 0.05
SECONDARY_RATIO = 0.9
MIN_MATCHED_TERMS = 2
MAX_CLAIMS = 2
ABSTAIN_TEXT = "I can't answer that from the documents you have access to."


@dataclass(frozen=True)
class Claim:
    text: str
    citations: tuple[str, ...]
    support: float


@dataclass(frozen=True)
class Answer:
    question: str
    role: str
    abstained: bool
    claims: tuple[Claim, ...]
    retrieved_ids: tuple[str, ...]

    def render(self) -> str:
        if self.abstained:
            return ABSTAIN_TEXT
        return "\n".join(f"{c.text} [{', '.join(c.citations)}]" for c in self.claims)


def _support(wanted: set[str], specific: set[str], idf: dict[str, float], evidence: str) -> tuple[float, int]:
    """(IDF-weighted coverage, matched term count); coverage 0 if a specific term is missing."""
    have = set(tokenize(evidence))
    if not specific <= have:
        return 0.0, 0
    total = sum(idf[t] for t in wanted)
    matched = wanted & have
    return (sum(idf[t] for t in matched) / total if total else 0.0), len(matched)


def compose(retrieval: Retrieval) -> Answer:
    wanted = set(tokenize(retrieval.query))
    specific = {t for t in wanted if 0 < retrieval.query_df[t] <= SPECIFIC_DF_FRACTION * retrieval.permitted_count}
    needed = min(MIN_MATCHED_TERMS, len(wanted))
    candidates: list[tuple[float, int, str, str]] = []
    for rank, chunk in enumerate(retrieval.chunks):
        for sentence in sentences(chunk.text):
            support, matched = _support(wanted, specific, retrieval.query_idf, f"{chunk.title}. {sentence}")
            if wanted and support >= SUPPORT_THRESHOLD and matched >= needed:
                candidates.append((support, rank, sentence, chunk.chunk_id))
    candidates.sort(key=lambda c: (-c[0], c[1]))
    claims: list[Claim] = []
    for support, _, sentence, chunk_id in candidates:
        if len(claims) == MAX_CLAIMS or support < SECONDARY_RATIO * candidates[0][0]:
            break
        if all(c.text != sentence for c in claims):  # overlapping chunks repeat sentences
            claims.append(Claim(sentence, (chunk_id,), round(support, 3)))
    return Answer(question=retrieval.query, role=retrieval.role, abstained=not claims, claims=tuple(claims),
                  retrieved_ids=tuple(c.chunk_id for c in retrieval.chunks))


def answer(retriever: Retriever, question: str, requester: Requester, k: int = DEFAULT_K,
           audit_path: str | Path | None = None) -> tuple[Answer, Retrieval]:
    retrieval = retriever.retrieve(question, requester, k)
    result = compose(retrieval)
    if audit_path is not None:
        write_audit(audit_path, retrieval, result)
    return result, retrieval


def write_audit(path: str | Path, retrieval: Retrieval, result: Answer) -> dict[str, Any]:
    """One JSONL line per answer: who asked, what was retrieved and filtered (IDs only), the claims."""
    line = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "role": retrieval.role,
        "groups": list(retrieval.groups),
        "question": retrieval.query,
        "retrieved_ids": list(result.retrieved_ids),
        "filtered_out_count": len(retrieval.filtered_out_ids),
        "filtered_out_ids": list(retrieval.filtered_out_ids),
        "abstained": result.abstained,
        "claims": [asdict(c) for c in result.claims],
    }
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, sort_keys=True) + "\n")
    return line

"""Permission-aware hybrid retrieval (GraphRAG Slice 1): the gate lives inside the query.

`Retriever.retrieve` first partitions the chunks by the requester's groups
(`acl.can_read`). Everything after that, including BM25 term statistics, dense
scoring, fusion and top-k, runs over the permitted partition only:
  - a chunk the requester can't read is never scored, so it can't be ranked or
    returned, or reach a model;
  - BM25's IDF is computed over the permitted partition, so restricted
    documents don't even shift the scores of permitted ones.
This is a pre-filter in code. It is not a post-filter over a ranked list and
not an instruction to a model.

Dense vectors are computed once per chunk at index time. That is requester-
independent and uses no cross-chunk statistics. The IDs of chunks the gate
removed are returned for the audit log only; their text never leaves the index.
"""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Iterable

from graphrag.acl import Requester, can_read
from graphrag.chunking import Chunk
from graphrag.embedders import Embedder
from graphrag.text import tokenize

DEFAULT_K = 5
CANDIDATES = 20  # per ranker, before fusion
RRF_K = 60


def index_text(chunk: Chunk) -> str:
    return f"{chunk.title}. {chunk.text}"


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75) -> None:
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.tfs = [Counter(d) for d in docs]
        self.lens = [len(d) for d in docs]
        self.avg = sum(self.lens) / self.n if self.n else 0.0
        self.df: Counter[str] = Counter(t for d in docs for t in set(d))

    def idf(self, term: str) -> float:
        df = self.df.get(term, 0)
        return math.log(1 + (self.n - df + 0.5) / (df + 0.5))

    def scores(self, query: list[str]) -> list[float]:
        out = []
        for tf, length in zip(self.tfs, self.lens):
            s = 0.0
            for term in query:
                f = tf.get(term, 0)
                if f:
                    s += self.idf(term) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * length / self.avg))
            out.append(s)
        return out


@dataclass(frozen=True)
class ScoredChunk:
    chunk: Chunk
    score: float  # fused reciprocal-rank score
    bm25: float
    dense: float


@dataclass(frozen=True)
class Retrieval:
    query: str
    role: str
    groups: tuple[str, ...]
    results: tuple[ScoredChunk, ...]
    permitted_count: int
    filtered_out_ids: tuple[str, ...]  # audit log only; never shown to the requester
    query_idf: dict[str, float] = field(default_factory=dict)  # over the permitted partition
    query_df: dict[str, int] = field(default_factory=dict)  # permitted chunks containing each query term

    @property
    def chunks(self) -> list[Chunk]:
        return [r.chunk for r in self.results]


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


class Retriever:
    def __init__(self, chunks: Iterable[Chunk], embedder: Embedder) -> None:
        self._chunks = tuple(chunks)
        ids = [c.chunk_id for c in self._chunks]
        if len(set(ids)) != len(ids):
            raise ValueError("chunk IDs must be unique")
        self.embedder = embedder
        self._vectors = embedder.embed([index_text(c) for c in self._chunks])
        self._partitions: dict[frozenset[str], tuple[list[int], BM25]] = {}

    @property
    def chunks(self) -> tuple[Chunk, ...]:
        return self._chunks

    def _partition(self, groups: frozenset[str]) -> tuple[list[int], BM25]:
        """The gate: the indices of chunks these groups can read, and a BM25 over only those."""
        if groups not in self._partitions:
            permitted = [i for i, c in enumerate(self._chunks) if can_read(groups, c.acl)]
            bm25 = BM25([tokenize(index_text(self._chunks[i])) for i in permitted])
            self._partitions[groups] = (permitted, bm25)
        return self._partitions[groups]

    def retrieve(self, query: str, requester: Requester, k: int = DEFAULT_K) -> Retrieval:
        permitted, bm25 = self._partition(requester.groups)
        terms = tokenize(query)
        lexical = bm25.scores(terms)
        query_vector = self.embedder.embed([query])[0]
        dense = [_dot(query_vector, self._vectors[i]) for i in permitted]

        positions = range(len(permitted))
        lexical_rank = sorted((p for p in positions if lexical[p] > 0), key=lambda p: (-lexical[p], p))[:CANDIDATES]
        dense_rank = sorted(positions, key=lambda p: (-dense[p], p))[:CANDIDATES]
        fused: dict[int, float] = {}
        for ranking in (lexical_rank, dense_rank):
            for rank, p in enumerate(ranking):
                fused[p] = fused.get(p, 0.0) + 1.0 / (RRF_K + rank + 1)
        top = sorted(fused, key=lambda p: (-fused[p], p))[:k]

        permitted_set = set(permitted)
        return Retrieval(
            query=query, role=requester.role, groups=tuple(sorted(requester.groups)),
            results=tuple(ScoredChunk(self._chunks[permitted[p]], fused[p], lexical[p], dense[p]) for p in top),
            permitted_count=len(permitted),
            filtered_out_ids=tuple(c.chunk_id for i, c in enumerate(self._chunks) if i not in permitted_set),
            query_idf={t: bm25.idf(t) for t in terms},
            query_df={t: bm25.df.get(t, 0) for t in terms},
        )

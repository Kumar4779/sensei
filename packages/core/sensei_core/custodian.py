"""Custodian (Slice 21): Sensei's MemoryStore shared with other tools, gates intact.

This module is the transport-free core; custodian_server.py serves it over MCP
(stdio). Four operations, and none of them can make anything active:

  search(query)        concept search over ACTIVE entries -> ids + summaries
  get(id)              one full entry, provenance included
  propose(text, ...)   a NEW entry via MemoryStore.propose (source=tool_content),
                       so it is never active; activation stays Sensei-side
                       (MemoryStore.confirm), exactly as Slice 6 designed
  list_pending()       entries awaiting confirmation

MemoryStore is used as-is; its file format and semantics are frozen.

The embedding index is a DERIVED layer under <root>/derived/. The entry files
remain the source of truth: the index is synced from them before every search
and propose (new or changed active entries embedded, everything else dropped),
so deleting the index and rebuilding reproduces it, and a deleted or
de-activated entry leaves search at once. The index holds vectors keyed by
entry id and updated_at, never fact text or a hash of it.

Dedup flags, never drops: a proposal close to an active entry is still written,
with "possible_duplicate_of: <id>" recorded in its origin (the store's
provenance field; no new field, so the file format is unchanged).
"""

from __future__ import annotations

import json
import math
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from sensei_core.clients import OLLAMA_BASE_URL, OllamaUnavailable
from sensei_core.config import EmbeddingsConfig
from sensei_core.memory import MemoryStore
from sensei_core.trace import TraceWriter

SUMMARY_CHARS = 120
DEFAULT_TOP_K = 5
KEYWORD_DEDUP_JACCARD = 0.8  # fallback dedup when no index: token-set overlap
_TOKEN_RE = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    "a an and are as at be by for from has have i in is it its me my of on or that the their they this to was "
    "what when where which who with you your user users".split()
)


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    """Local embeddings via Ollama's /api/embed. Standard library only, like clients.py."""

    def __init__(self, model: str, base_url: str = OLLAMA_BASE_URL, timeout: float = 60.0) -> None:
        self.model = model
        self.url = base_url.rstrip("/") + "/api/embed"
        self.timeout = timeout

    @classmethod
    def from_config(cls, cfg: EmbeddingsConfig, **kwargs: Any) -> "OllamaEmbedder":
        return cls(cfg.model, **kwargs)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise OllamaUnavailable(f"Ollama returned HTTP {e.code} for embedding model {self.model!r}") from e
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise OllamaUnavailable(f"cannot reach Ollama at {self.url}: {getattr(e, 'reason', e)}") from e
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise OllamaUnavailable(f"Ollama embed reply for {self.model!r} had no usable 'embeddings'")
        return vectors


def cosine(a: list[float], b: list[float]) -> float:
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return sum(x * y for x, y in zip(a, b)) / (na * nb)


def tokens(text: str) -> set[str]:
    return {t for t in _TOKEN_RE.findall(text.casefold()) if t not in _STOPWORDS and len(t) > 1}


def summary(fact: str) -> str:
    one_line = " ".join(fact.split())
    return one_line if len(one_line) <= SUMMARY_CHARS else one_line[: SUMMARY_CHARS - 1] + "…"


class EmbeddingIndex:
    """Vectors for ACTIVE entries, derived from the store's files."""

    def __init__(self, path: str | Path, embedder: Embedder) -> None:
        self.path = Path(path)
        self.embedder = embedder

    def exists(self) -> bool:
        return self.path.is_file()

    def _load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"model": self.embedder.model, "entries": {}}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if data.get("model") != self.embedder.model:  # a different model's vectors are not comparable
            return {"model": self.embedder.model, "entries": {}}
        return data

    def _save(self, data: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, self.path)

    def sync(self, store: MemoryStore) -> dict[str, list[float]]:
        """Bring the index in line with the store's active entries; return id -> vector."""
        data = self._load()
        old = data["entries"]
        active = {e["id"]: e for e in store.list_beliefs()}
        fresh = {i: old[i] for i, e in active.items() if i in old and old[i].get("updated_at") == e["updated_at"]}
        todo = [e for i, e in active.items() if i not in fresh]
        for e, vector in zip(todo, self.embedder.embed([e["fact"] for e in todo]) if todo else []):
            fresh[e["id"]] = {"updated_at": e["updated_at"], "vector": vector}
        if fresh != old or not self.path.is_file():
            data["entries"] = fresh
            self._save(data)
        return {i: v["vector"] for i, v in fresh.items()}

    def rebuild(self, store: MemoryStore) -> dict[str, list[float]]:
        self.path.unlink(missing_ok=True)
        return self.sync(store)


@dataclass(frozen=True)
class Hit:
    id: str
    score: float

    def as_dict(self, entry: dict[str, Any]) -> dict[str, Any]:
        return {"id": self.id, "category": entry["category"], "summary": summary(entry["fact"]),
                "score": round(self.score, 4)}


class Custodian:
    """The four Custodian operations over one MemoryStore, every call traced."""

    def __init__(
        self,
        store: MemoryStore,
        index: EmbeddingIndex | None,
        *,
        search_min_score: float,
        dedup_threshold: float,
        trace: TraceWriter | None = None,
    ) -> None:
        self.store = store
        self.index = index  # None: keyword fallback only
        self.search_min_score = search_min_score
        self.dedup_threshold = dedup_threshold
        self.trace = trace

    @classmethod
    def for_root(cls, root: str | Path, cfg: EmbeddingsConfig, *, embedder: Embedder | None = None,
                 trace: TraceWriter | None = None, use_index: bool = True) -> "Custodian":
        store = MemoryStore(root)
        index = None
        if use_index:
            index = EmbeddingIndex(Path(root) / "derived" / "embeddings.json", embedder or OllamaEmbedder.from_config(cfg))
        return cls(store, index, search_min_score=cfg.search_min_score, dedup_threshold=cfg.dedup_threshold, trace=trace)

    # ---- tracing (ids and counts only; never entry text) -----------------

    def _trace(self, client: str, operation: str, **payload: Any) -> None:
        if self.trace is not None:
            self.trace.write("mcp_tool_call", {"client": client, "operation": operation, **payload})

    # ---- vectors, or the keyword fallback ---------------------------------

    def _vectors(self) -> tuple[dict[str, list[float]] | None, str]:
        """(id -> vector, mode). mode is 'embedding', or 'keyword' when no index is usable."""
        if self.index is None:
            return None, "keyword"
        try:
            return self.index.sync(self.store), "embedding"
        except OllamaUnavailable:
            return None, "keyword"

    def _rank(self, text: str, vectors: dict[str, list[float]] | None) -> list[Hit]:
        if vectors is not None:
            [q] = self.index.embedder.embed([text])  # type: ignore[union-attr]
            hits = [Hit(i, cosine(q, v)) for i, v in vectors.items()]
        else:
            want = tokens(text)
            hits = []
            for e in self.store.list_beliefs():
                have = tokens(e["fact"])
                if want and have:
                    hits.append(Hit(e["id"], len(want & have) / len(want | have)))
        return sorted(hits, key=lambda h: (-h.score, h.id))

    # ---- the four operations ----------------------------------------------

    def search(self, query: str, *, client: str, top_k: int = DEFAULT_TOP_K) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip():
            raise ValueError("query must be a non-empty string")
        vectors, mode = self._vectors()
        try:
            ranked = self._rank(query, vectors)
        except OllamaUnavailable:
            mode, ranked = "keyword", self._rank(query, None)
        if mode == "embedding":
            hits = [h for h in ranked if h.score >= self.search_min_score][:top_k]
        else:
            hits = [h for h in ranked if h.score > 0][:top_k]  # any shared keyword
        entries = {e["id"]: e for e in self.store.list_beliefs()}
        results = [h.as_dict(entries[h.id]) for h in hits if h.id in entries]
        self._trace(client, "memory_search", mode=mode, query_chars=len(query), searched=len(entries),
                    result_count=len(results), result_ids=[r["id"] for r in results])
        return {"mode": mode, "results": results}

    def get(self, entry_id: str, *, client: str) -> dict[str, Any]:
        try:
            entry = self.store.get(entry_id)
        except KeyError:
            self._trace(client, "memory_get", entry_id=str(entry_id)[:64], found=False)
            raise
        self._trace(client, "memory_get", entry_id=entry_id, found=True, status=entry["status"])
        return entry

    def propose(self, text: str, category: str = "general", *, client: str) -> dict[str, Any]:
        vectors, mode = self._vectors()
        duplicate: Hit | None = None
        if isinstance(text, str) and text.strip():
            try:
                ranked = self._rank(text, vectors)
            except OllamaUnavailable:
                mode, ranked = "keyword", self._rank(text, None)
            threshold = self.dedup_threshold if mode == "embedding" else KEYWORD_DEDUP_JACCARD
            if ranked and ranked[0].score >= threshold:
                duplicate = ranked[0]
        origin = f"mcp:{client}"
        if duplicate is not None:
            origin += f"; possible_duplicate_of: {duplicate.id}"
        entry = self.store.propose(text, origin=origin, category=category)  # never active (Slice 6 gating)
        self._trace(client, "memory_propose", entry_id=entry["id"], status=entry["status"], category=category,
                    dedup_mode=mode, possible_duplicate_of=duplicate.id if duplicate else None,
                    duplicate_score=round(duplicate.score, 4) if duplicate else None)
        return entry

    def list_pending(self, *, client: str) -> list[dict[str, Any]]:
        pending = self.store.list_pending()
        self._trace(client, "memory_list_pending", count=len(pending), entry_ids=[e["id"] for e in pending])
        return pending

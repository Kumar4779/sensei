"""Dense embedders (GraphRAG Slice 1), behind one interface so the model is swappable.

- HashingEmbedder: deterministic feature hashing of words and character
  trigrams, standard library only. It needs no model and no network, so tests
  and CI use it. It is lexical, not semantic: it tolerates spelling and
  inflection variants but does not know that "pay" and "salary" are related.
- OllamaEmbedder: a real semantic embedding model (default nomic-embed-text,
  the model Sensei's Custodian uses) served by a local Ollama.

The permission gate does not depend on which embedder is used. Retrieval
removes the chunks a requester can't read before any embedder scores anything.
"""

from __future__ import annotations

import hashlib
import json
import math
import urllib.error
import urllib.request
from typing import Protocol

from graphrag.text import tokenize

OLLAMA_BASE_URL = "http://localhost:11434"


class Embedder(Protocol):
    model: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class EmbedderUnavailable(RuntimeError):
    pass


def _normalize(vector: list[float]) -> list[float]:
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector] if norm else vector


class HashingEmbedder:
    def __init__(self, dims: int = 512) -> None:
        self.dims = dims
        self.model = f"hashing-{dims}"

    def _features(self, text: str) -> list[tuple[str, float]]:
        feats: list[tuple[str, float]] = []
        for token in tokenize(text):
            feats.append(("w:" + token, 1.0))
            padded = f"#{token}#"
            feats.extend(("c:" + padded[i:i + 3], 0.3) for i in range(len(padded) - 2))
        return feats

    def embed(self, texts: list[str]) -> list[list[float]]:
        out = []
        for text in texts:
            vector = [0.0] * self.dims
            for feature, weight in self._features(text):
                digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
                value = int.from_bytes(digest, "big")
                vector[value % self.dims] += weight if (value >> 63) & 1 else -weight
            out.append(_normalize(vector))
        return out


class OllamaEmbedder:
    """Local embeddings via Ollama's /api/embed. Standard library only."""

    def __init__(self, model: str = "nomic-embed-text", base_url: str = OLLAMA_BASE_URL, timeout: float = 120.0,
                 batch: int = 64) -> None:
        self.model = model
        self.url = base_url.rstrip("/") + "/api/embed"
        self.timeout = timeout
        self.batch = batch

    def _post(self, texts: list[str]) -> list[list[float]]:
        body = json.dumps({"model": self.model, "input": texts}).encode("utf-8")
        request = urllib.request.Request(self.url, data=body, headers={"Content-Type": "application/json"},
                                         method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise EmbedderUnavailable(f"Ollama returned HTTP {e.code} for embedding model {self.model!r}") from e
        except (urllib.error.URLError, ConnectionError, TimeoutError) as e:
            raise EmbedderUnavailable(f"cannot reach Ollama at {self.url}: {getattr(e, 'reason', e)}") from e
        vectors = data.get("embeddings")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise EmbedderUnavailable(f"Ollama embed reply for {self.model!r} had no usable 'embeddings'")
        return [_normalize([float(x) for x in v]) for v in vectors]

    def embed(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch):
            out.extend(self._post(texts[i:i + self.batch]))
        return out


def make_embedder(name: str) -> Embedder:
    if name == "hashing":
        return HashingEmbedder()
    if name == "ollama":
        return OllamaEmbedder()
    raise ValueError(f"unknown embedder {name!r}; use 'hashing' or 'ollama'")

"""Shared fixtures. The tests are the eval harness side: only they read the ground truth."""

import json

import pytest

from graphrag.chunking import chunk_corpus
from graphrag.corpus import DEFAULT_CORPUS_DIR, DEFAULT_GROUND_TRUTH_DIR, load_documents
from graphrag.embedders import HashingEmbedder
from graphrag.retrieval import Retriever


@pytest.fixture(scope="session")
def documents():
    return load_documents(DEFAULT_CORPUS_DIR)


@pytest.fixture(scope="session")
def ground_truth():
    return json.loads((DEFAULT_GROUND_TRUTH_DIR / "ground_truth.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="session")
def chunks(documents):
    return chunk_corpus(documents)


@pytest.fixture(scope="session")
def retriever(chunks):
    return Retriever(chunks, HashingEmbedder())

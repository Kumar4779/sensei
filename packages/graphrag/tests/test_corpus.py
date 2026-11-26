"""Corpus generator v1: determinism, the frozen baseline, labels and canaries."""

import hashlib
import json
from pathlib import Path

import pytest

from graphrag import corpus
from graphrag.acl import ALL_STAFF, GROUPS, TIERS

FILES = ("documents.jsonl", "manifest.json")


def _write(tmp_path: Path, name: str, seed: int, n_docs: int = corpus.DEFAULT_DOCS) -> tuple[Path, Path]:
    out, gt = tmp_path / name / "corpus", tmp_path / name / "gt"
    corpus.write(*corpus.generate(seed, n_docs), out, gt)
    return out, gt


def test_same_seed_gives_byte_identical_corpus(tmp_path):
    a, a_gt = _write(tmp_path, "a", 7)
    b, b_gt = _write(tmp_path, "b", 7)
    for name in FILES:
        assert (a / name).read_bytes() == (b / name).read_bytes()
    assert (a_gt / "ground_truth.json").read_bytes() == (b_gt / "ground_truth.json").read_bytes()
    c, _ = _write(tmp_path, "c", 8)
    assert (a / "documents.jsonl").read_bytes() != (c / "documents.jsonl").read_bytes()


def test_committed_corpus_is_the_frozen_regeneration(tmp_path):
    out, gt = _write(tmp_path, "regen", corpus.DEFAULT_SEED)
    for name in FILES:
        assert (out / name).read_bytes() == (corpus.DEFAULT_CORPUS_DIR / name).read_bytes(), name
    assert (gt / "ground_truth.json").read_bytes() == (corpus.DEFAULT_GROUND_TRUTH_DIR / "ground_truth.json").read_bytes()
    manifest = json.loads((corpus.DEFAULT_CORPUS_DIR / "manifest.json").read_text())
    docs = (corpus.DEFAULT_CORPUS_DIR / "documents.jsonl").read_bytes()
    assert manifest["sha256"] == hashlib.sha256(docs).hexdigest() and manifest["corpus_version"] == "v1"


def test_corpus_shape(documents):
    assert 150 <= len(documents) <= 200
    assert {d["department"] for d in documents} == set(GROUPS)
    assert {d["tier"] for d in documents} == set(TIERS)
    for department in GROUPS:  # every role has material at every tier
        assert {d["tier"] for d in documents if d["department"] == department} == set(TIERS)
    assert len({d["doc_id"] for d in documents}) == len(documents)


def test_every_document_has_an_explicit_acl_that_matches_its_tier(documents):
    known = set(GROUPS) | {ALL_STAFF}
    for d in documents:
        assert d["acl"] and set(d["acl"]) <= known and d["acl"] == sorted(d["acl"]), d["doc_id"]
        if d["tier"] == "public":
            assert d["acl"] == [ALL_STAFF]
        else:
            assert ALL_STAFF not in d["acl"] and d["department"] in d["acl"]


def test_canaries_one_unique_secret_per_document_per_restricted_group(documents, ground_truth):
    canaries = ground_truth["canaries"]
    assert len(canaries) == corpus.CANARIES_PER_GROUP * len(GROUPS)
    assert {c["group"] for c in canaries} == set(GROUPS)
    assert len({c["secret"] for c in canaries}) == len(canaries)
    assert len({c["question"] for c in canaries}) == len(canaries)
    by_id = {d["doc_id"]: d for d in documents}
    for c in canaries:
        doc = by_id[c["doc_id"]]
        assert doc["canary"] and doc["tier"] == "restricted" and doc["acl"] == [c["group"]]
        holders = [d["doc_id"] for d in documents if c["secret"] in d["body"] or c["secret"] in d["title"]]
        assert holders == [c["doc_id"]]
    assert sum(d["canary"] for d in documents) == len(canaries)


def test_ground_truth_lives_apart_from_the_corpus_and_the_code_never_reads_it():
    assert not (corpus.DEFAULT_CORPUS_DIR / "ground_truth.json").exists()
    assert corpus.DEFAULT_GROUND_TRUTH_DIR.resolve() != corpus.DEFAULT_CORPUS_DIR.resolve()
    package = Path(corpus.__file__).parent
    for source in package.glob("*.py"):
        if source.name != "corpus.py":  # the generator writes it; nothing else may open it
            assert "ground_truth" not in source.read_text() and "evals" not in source.read_text(), source.name


def test_probes_are_unambiguous(ground_truth):
    questions = [p["question"] for p in ground_truth["probes"]]
    assert len(questions) == len(set(questions)) and len(questions) >= 50


@pytest.mark.parametrize("n_docs", [500])
def test_generator_scales_without_changes(n_docs):
    documents, ground_truth = corpus.generate(corpus.DEFAULT_SEED, n_docs)
    assert len(documents) == n_docs and len(ground_truth["canaries"]) == corpus.CANARIES_PER_GROUP * len(GROUPS)
    assert corpus.stats(documents)["documents"] == n_docs


def test_too_small_a_corpus_is_refused():
    with pytest.raises(ValueError):
        corpus.generate(corpus.DEFAULT_SEED, 10)

"""Chunking: every chunk carries its document's doc_id, tier, ACL and canary flag."""

import pytest

from graphrag.chunking import MAX_WORDS, chunk_document, sentences


def test_every_chunk_carries_its_documents_labels(documents, chunks):
    by_id = {d["doc_id"]: d for d in documents}
    assert {c.doc_id for c in chunks} == set(by_id)
    for c in chunks:
        doc = by_id[c.doc_id]
        assert c.chunk_id.startswith(c.doc_id + "#c")
        assert c.tier == doc["tier"] and list(c.acl) == doc["acl"] and c.canary == doc["canary"]
        assert c.acl and c.text.strip()
    assert len({c.chunk_id for c in chunks}) == len(chunks)


def test_chunks_cover_every_sentence_and_respect_the_size_limit(documents, chunks):
    for doc in documents:
        own = [c for c in chunks if c.doc_id == doc["doc_id"]]
        covered = {s for c in own for s in sentences(c.text)}
        assert covered == set(sentences(doc["body"]))
        for c in own:
            assert len(c.text.split()) <= MAX_WORDS or len(sentences(c.text)) == 1
    assert any(len([c for c in chunks if c.doc_id == d["doc_id"]]) > 1 for d in documents)  # overlap is exercised


def test_canary_secrets_only_ever_sit_in_canary_flagged_chunks(chunks, ground_truth):
    for canary in ground_truth["canaries"]:
        holders = [c for c in chunks if canary["secret"] in c.text]
        assert holders and all(c.canary and c.doc_id == canary["doc_id"] for c in holders)
        assert all(c.acl == (canary["group"],) for c in holders)


def test_a_document_without_an_acl_is_refused():
    doc = {"doc_id": "doc-x", "title": "t", "tier": "internal", "acl": [], "canary": False, "body": "One. Two."}
    with pytest.raises(ValueError):
        chunk_document(doc)

"""Slice 21: the Custodian core (no MCP transport, no model: a concept-axis fake embedder)."""

import json

import pytest

from sensei_core.clients import OllamaUnavailable
from sensei_core.config import ConfigError, load_embeddings
from sensei_core.custodian import Custodian, EmbeddingIndex, summary
from sensei_core.memory import MemoryStore
from sensei_core.trace import TraceWriter, read_events

# Synonym groups share an axis, so a query can match a fact with no word in common.
CONCEPTS = [
    {"peanut", "peanuts", "nut", "nuts", "allergy", "allergic"},
    {"aisle", "seat", "seats", "seating", "flight", "flights", "flying", "plane", "planes"},
    {"violin", "music", "lessons", "daughter", "child"},
    {"coffee", "oat", "milk", "latte"},
    {"austin", "remote", "remotely", "based", "lives"},
]


class ConceptEmbedder:
    model = "fake-concepts-v1"

    def __init__(self):
        self.calls = 0

    def embed(self, texts):
        self.calls += 1
        out = []
        for t in texts:
            words = set(t.casefold().replace(".", " ").replace(",", " ").replace("'", " ").split())
            out.append([float(len(words & group)) for group in CONCEPTS] + [0.01])
        return out


class DownEmbedder:
    model = "fake-concepts-v1"

    def embed(self, texts):
        raise OllamaUnavailable("ollama down")


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory")


@pytest.fixture
def trace(tmp_path):
    return TraceWriter(tmp_path / "trace.jsonl", run_id="r1", session_id="s1")


def custodian(store, trace=None, embedder=None, use_index=True):
    index = EmbeddingIndex(store.root / "derived" / "embeddings.json", embedder or ConceptEmbedder()) if use_index else None
    return Custodian(store, index, search_min_score=0.5, dedup_threshold=0.9, trace=trace)


def seed(store):
    return {
        "peanut": store.write("The user is allergic to peanuts.")["id"],
        "aisle": store.write("The user prefers aisle seats on flights.")["id"],
        "violin": store.write("The user's daughter plays violin on Tuesdays.")["id"],
    }


# ---- propose -> pending, never active ------------------------------------------


@pytest.mark.parametrize("category, status", [("general", "proposed"), ("money", "pending_confirmation")])
def test_propose_is_pending_and_never_active(store, category, status):
    c = custodian(store)
    entry = c.propose("The user drinks oat milk in coffee.", category, client="claude-code")
    assert entry["status"] == status and entry["source"] == "tool_content"
    assert entry["origin"] == "mcp:claude-code"
    assert store.list_beliefs() == []
    assert [e["id"] for e in c.list_pending(client="claude-code")] == [entry["id"]]
    assert c.search("coffee milk", client="x")["results"] == []  # pending is not searchable


def test_confirm_is_sensei_side_and_then_the_entry_is_searchable(store):
    c = custodian(store)
    entry = c.propose("The user drinks oat milk in coffee.", client="claude-code")
    store.confirm(entry["id"])
    assert [r["id"] for r in c.search("what goes in the latte", client="x")["results"]] == [entry["id"]]


def test_custodian_has_no_way_to_activate():
    assert not any(hasattr(Custodian, name) for name in ("confirm", "write", "update", "delete", "activate"))


def test_invalid_proposals_are_rejected_by_the_store(store):
    c = custodian(store)
    with pytest.raises(ValueError):
        c.propose("  ", client="x")
    with pytest.raises(ValueError):
        c.propose("fact", "not-a-category", client="x")
    assert store.list_pending() == []


# ---- search ---------------------------------------------------------------------


def test_search_finds_a_concept_match_with_no_shared_keywords(store):
    ids = seed(store)
    c = custodian(store)
    query = "seating preference when flying"
    fact = store.get(ids["aisle"])["fact"]
    assert not ({w.strip(".").casefold() for w in query.split()} & {w.strip(".").casefold() for w in fact.split()})
    result = c.search(query, client="x")
    assert result["mode"] == "embedding"
    assert [r["id"] for r in result["results"]] == [ids["aisle"]]


def test_search_returns_ids_and_summaries_not_files(store):
    ids = seed(store)
    [hit] = custodian(store).search("nut allergy", client="x")["results"]
    assert set(hit) == {"id", "category", "summary", "score"}
    assert hit["id"] == ids["peanut"] and hit["summary"] == "The user is allergic to peanuts."
    assert summary("x" * 500).endswith("…") and len(summary("x" * 500)) == 120


def test_search_covers_active_entries_only(store):
    ids = seed(store)
    store.propose("The user has a peanut allergy (from a doctor's email).", origin="email")
    results = custodian(store).search("nut allergy", client="x")["results"]
    assert [r["id"] for r in results] == [ids["peanut"]]


def test_unrelated_query_returns_nothing_above_the_floor(store):
    seed(store)
    assert custodian(store).search("favourite football team", client="x")["results"] == []


def test_keyword_fallback_when_there_is_no_index(store):
    ids = seed(store)
    result = custodian(store, use_index=False).search("violin practice", client="x")
    assert result["mode"] == "keyword" and [r["id"] for r in result["results"]] == [ids["violin"]]
    assert custodian(store, use_index=False).search("seating preference when flying", client="x")["results"] == []


def test_keyword_fallback_when_the_embedder_is_down(store):
    ids = seed(store)
    result = custodian(store, embedder=DownEmbedder()).search("aisle seats", client="x")
    assert result["mode"] == "keyword" and [r["id"] for r in result["results"]] == [ids["aisle"]]


# ---- the index is derived ----------------------------------------------------------


def test_index_rebuild_reproduces_search_results(store):
    seed(store)
    c = custodian(store)
    queries = ["nut allergy", "seating preference when flying", "child's music lessons", "football"]
    before = [c.search(q, client="x") for q in queries]
    index_file = store.root / "derived" / "embeddings.json"
    first = json.loads(index_file.read_text(encoding="utf-8"))
    index_file.unlink()
    c.index.rebuild(store)
    assert json.loads(index_file.read_text(encoding="utf-8")) == first
    assert [c.search(q, client="x") for q in queries] == before


def test_index_holds_vectors_not_fact_text(store):
    ids = seed(store)
    custodian(store).search("anything", client="x")
    raw = (store.root / "derived" / "embeddings.json").read_text(encoding="utf-8")
    assert "peanut" not in raw.casefold() and "aisle" not in raw.casefold()
    assert set(json.loads(raw)["entries"]) == set(ids.values())


def test_index_only_embeds_new_or_changed_entries(store):
    seed(store)
    emb = ConceptEmbedder()
    c = custodian(store, embedder=emb)
    c.search("nut", client="x")
    calls = emb.calls  # one embed for the 3 entries, one for the query
    c.search("nut", client="x")
    assert emb.calls == calls + 1  # query only


def test_a_different_embedding_model_invalidates_the_index(store):
    seed(store)
    custodian(store).search("nut", client="x")

    class Other(ConceptEmbedder):
        model = "other-model"

    custodian(store, embedder=Other()).search("nut", client="x")
    assert json.loads((store.root / "derived" / "embeddings.json").read_text())["model"] == "other-model"


def test_deleting_an_entry_removes_it_from_search(store):
    ids = seed(store)
    c = custodian(store)
    assert [r["id"] for r in c.search("nut allergy", client="x")["results"]] == [ids["peanut"]]
    store.delete(ids["peanut"])
    assert c.search("nut allergy", client="x")["results"] == []
    index = json.loads((store.root / "derived" / "embeddings.json").read_text(encoding="utf-8"))
    assert ids["peanut"] not in index["entries"]


def test_an_updated_entry_is_re_embedded(store):
    ids = seed(store)
    c = custodian(store)
    c.search("x", client="x")
    store.update(ids["peanut"], "The user lives in Austin.")  # user fact in general: stays active
    assert [r["id"] for r in c.search("where is the user based", client="x")["results"]] == [ids["peanut"]]


# ---- dedup flags, never drops --------------------------------------------------------


def test_near_duplicate_propose_is_written_and_flagged(store):
    ids = seed(store)
    entry = custodian(store).propose("User has a peanut allergy.", client="claude-code")
    assert entry["status"] == "proposed"
    assert entry["origin"] == f"mcp:claude-code; possible_duplicate_of: {ids['peanut']}"
    assert store.get(entry["id"]) == entry  # written, not dropped
    assert store.get(ids["peanut"])["fact"] == "The user is allergic to peanuts."  # not merged


def test_distinct_propose_is_not_flagged(store):
    seed(store)
    entry = custodian(store).propose("The user drinks oat milk in coffee.", client="claude-code")
    assert "possible_duplicate_of" not in entry["origin"]


def test_dedup_falls_back_to_keywords_without_an_index(store):
    ids = seed(store)
    c = custodian(store, use_index=False)
    flagged = c.propose("The user prefers aisle seats on flights!", client="x")
    assert flagged["origin"].endswith(f"possible_duplicate_of: {ids['aisle']}")
    assert "possible_duplicate_of" not in c.propose("Aisle seats only on long flights.", client="x")["origin"]


def test_dedup_ignores_pending_entries(store):
    c = custodian(store)
    first = c.propose("The user is allergic to peanuts.", client="x")
    second = c.propose("The user is allergic to peanuts.", client="x")
    assert "possible_duplicate_of" not in second["origin"]  # dedup is against ACTIVE entries
    assert len(store.list_pending()) == 2 and first["id"] != second["id"]


# ---- traces: client, operation, ids, counts; never entry text -------------------------


def test_every_operation_is_traced_without_entry_text(store, trace):
    ids = seed(store)
    c = custodian(store, trace=trace)
    c.search("nut allergy", client="claude-code")
    c.get(ids["aisle"], client="claude-code")
    proposed = c.propose("User has a peanut allergy.", client="claude-code")
    c.list_pending(client="claude-code")
    with pytest.raises(KeyError):
        c.get("0" * 32, client="claude-code")
    events = [e for e in read_events(trace.path) if e["event_type"] == "mcp_tool_call"]
    ops = [e["payload"]["operation"] for e in events]
    assert ops == ["memory_search", "memory_get", "memory_propose", "memory_list_pending", "memory_get"]
    assert all(e["payload"]["client"] == "claude-code" for e in events)
    search, get, propose, pending, missing = (e["payload"] for e in events)
    assert search["result_ids"] == [ids["peanut"]] and search["result_count"] == 1 and search["searched"] == 3
    assert get["entry_id"] == ids["aisle"] and get["found"] is True
    assert propose["entry_id"] == proposed["id"] and propose["possible_duplicate_of"] == ids["peanut"]
    assert pending["count"] == 1 and missing["found"] is False
    raw = trace.path.read_text(encoding="utf-8").casefold()
    for word in ("peanut", "aisle", "violin", "allergy"):
        assert word not in raw


# ---- config ------------------------------------------------------------------------


def test_embeddings_config_is_recorded_and_validated(tmp_path):
    cfg = load_embeddings()
    assert (cfg.provider, cfg.model) == ("ollama", "nomic-embed-text")
    assert 0 < cfg.search_min_score < cfg.dedup_threshold < 1
    bad = tmp_path / "m.toml"
    bad.write_text('[embeddings]\nprovider = "openai"\nmodel = "x"\nsearch_min_score = 0.5\ndedup_threshold = 0.9\n')
    with pytest.raises(ConfigError, match="local only"):
        load_embeddings(bad)
    bad.write_text('[embeddings]\nprovider = "ollama"\nmodel = "x"\nsearch_min_score = 2\ndedup_threshold = 0.9\n')
    with pytest.raises(ConfigError, match="search_min_score"):
        load_embeddings(bad)

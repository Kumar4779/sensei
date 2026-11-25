"""Slice 21: the Custodian over MCP, driven by the SDK's own client (in-process)."""

import json

import pytest

pytest.importorskip("mcp")
anyio = pytest.importorskip("anyio")

from mcp import Client  # noqa: E402
from mcp.types import Implementation  # noqa: E402

from sensei_core.custodian import Custodian, EmbeddingIndex  # noqa: E402
from sensei_core.custodian_server import TOOL_NAMES, build_server, main  # noqa: E402
from sensei_core.memory import MemoryStore  # noqa: E402
from sensei_core.trace import TraceWriter, read_events  # noqa: E402
from test_custodian import ConceptEmbedder  # noqa: E402


@pytest.fixture
def setup(tmp_path):
    store = MemoryStore(tmp_path / "memory")
    trace = TraceWriter(tmp_path / "trace.jsonl", run_id="r1", session_id="s1")
    index = EmbeddingIndex(store.root / "derived" / "embeddings.json", ConceptEmbedder())
    custodian = Custodian(store, index, search_min_score=0.5, dedup_threshold=0.9, trace=trace)
    return store, trace, build_server(custodian)


def payload(result):
    if getattr(result, "structured_content", None) is not None:
        return result.structured_content
    return json.loads(result.content[0].text)


def call(server, *steps, client_name="test-client"):
    """Run (tool, args) steps in one MCP session; return each step's result."""
    async def go():
        out = []
        async with Client(server, client_info=Implementation(name=client_name, version="1")) as c:
            for tool, args in steps:
                if tool == "__list__":
                    out.append(await c.list_tools())
                else:
                    out.append(await c.call_tool(tool, args))
        return out
    return anyio.run(go)


def test_server_exposes_exactly_the_four_tools(setup):
    _, _, server = setup
    [tools] = call(server, ("__list__", {}))
    assert sorted(t.name for t in tools.tools) == sorted(TOOL_NAMES)
    assert not any("confirm" in t.name or "activate" in t.name for t in tools.tools)


def test_propose_over_mcp_is_pending_and_names_the_client(setup):
    store, trace, server = setup
    [proposed, pending] = call(
        server,
        ("memory_propose", {"text": "The user drinks oat milk in coffee."}),
        ("memory_list_pending", {}),
        client_name="claude-code",
    )
    entry = payload(proposed)
    assert entry["status"] == "proposed" and entry["source"] == "tool_content"
    assert entry["origin"] == "mcp:claude-code"
    assert [e["id"] for e in payload(pending)["pending"]] == [entry["id"]]
    assert store.list_beliefs() == []
    events = [e["payload"] for e in read_events(trace.path) if e["event_type"] == "mcp_tool_call"]
    assert [(e["client"], e["operation"]) for e in events] == [
        ("claude-code", "memory_propose"), ("claude-code", "memory_list_pending")]


def test_confirm_sensei_side_then_search_by_concept_over_mcp(setup, tmp_path):
    store, _, server = setup
    [proposed] = call(server, ("memory_propose", {"text": "The user prefers aisle seats on flights."}))
    entry_id = payload(proposed)["id"]
    assert main(["confirm", entry_id, "--root", str(store.root), "--trace", str(tmp_path / "cli.jsonl")]) == 0
    assert store.get(entry_id)["status"] == "active"
    [found, got] = call(server, ("memory_search", {"query": "seating preference when flying"}),
                        ("memory_get", {"id": entry_id}))
    assert [r["id"] for r in payload(found)["results"]] == [entry_id]
    assert payload(got)["origin"] == "mcp:test-client"
    [confirm_event] = read_events(tmp_path / "cli.jsonl")
    assert confirm_event["payload"] == {"client": "sensei-cli", "operation": "confirm", "entry_id": entry_id,
                                        "status": "active"}


def test_bad_input_is_an_error_result_not_a_crash(setup):
    _, _, server = setup
    [missing, bad_category] = call(server, ("memory_get", {"id": "0" * 32}),
                                   ("memory_propose", {"text": "x", "category": "nope"}))
    assert missing.is_error and bad_category.is_error

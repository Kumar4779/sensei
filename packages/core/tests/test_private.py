"""Slice 33: every content-bearing store under the Sensei home is owner-only, whatever the umask.

Each test creates its store under umask 000, the most permissive there is. Under it, a file
left to the umask comes out 666 and a directory 777, so any writer that falls back to umask
defaults fails here.
"""

import json
import os
import stat
from contextlib import contextmanager
from pathlib import Path

import pytest

from sensei_core.brief import load_morning_fixture
from sensei_core.config import load_models
from sensei_core.custodian import EmbeddingIndex
from sensei_core.egress import EgressLog
from sensei_core.executor import Executor
from sensei_core.memory import MemoryStore
from sensei_core.private import append_private, private_dir, write_private
from sensei_core.router import FakeClient, Router
from sensei_core.runs import RunStore
from sensei_core.trace import TraceWriter
from sensei_core.workflows import run_brief_workflow

FIXTURE = Path(__file__).resolve().parents[3] / "evals" / "tasks" / "morning-brief-001" / "fixture"


@contextmanager
def umask(mask):
    old = os.umask(mask)
    try:
        yield
    finally:
        os.umask(old)


def mode(path):
    return stat.S_IMODE(Path(path).stat().st_mode)


def assert_owner_only(root):
    """Every directory 0700 and every file 0600 under root (root included)."""
    root = Path(root)
    loose = [(str(p.relative_to(root.parent)), oct(mode(p))) for p in [root, *root.rglob("*")]
             if mode(p) != (0o700 if p.is_dir() else 0o600)]
    assert not loose, loose


def test_the_helpers_ignore_the_umask(tmp_path):
    with umask(0o000):
        d = private_dir(tmp_path / "store")
        append_private(d / "log.jsonl", "a\n")
        append_private(d / "log.jsonl", "b\n")
        write_private(d / "record.json", "{}\n")
    assert (d / "log.jsonl").read_text() == "a\nb\n" and not list(d.glob("*.tmp"))
    assert_owner_only(d)


def test_trace_store_is_owner_only_and_tightens_what_it_owns(tmp_path):
    traces = tmp_path / "traces"
    traces.mkdir(mode=0o777)
    os.chmod(traces, 0o777)
    old = traces / "gateway.jsonl"
    old.write_text("{}\n")
    os.chmod(old, 0o666)
    neighbour = tmp_path / "not-a-trace.txt"
    neighbour.write_text("x")
    os.chmod(neighbour, 0o644)
    with umask(0o000):
        trace = TraceWriter(old, run_id="r", session_id="s")
        trace.write("hello", {"a": 1})
        TraceWriter(traces / "bench.jsonl", run_id="r", session_id="s").write("hello", {})
    assert_owner_only(traces)
    assert mode(neighbour) == 0o644  # outside the traces directory: untouched


def test_egress_log_is_owner_only(tmp_path):
    with umask(0o000):
        EgressLog(tmp_path / "egress.jsonl").record(route="cheap_api", model="m", host="h", prompt="p", response="r")
    assert mode(tmp_path / "egress.jsonl") == 0o600


def test_run_records_are_owner_only(tmp_path):
    with umask(0o000):
        store = RunStore(tmp_path / "runs")
        store.save({**store.create("f", None), "status": "completed"})
    assert_owner_only(tmp_path / "runs")


def test_memory_store_is_owner_only(tmp_path):
    with umask(0o000):
        store = MemoryStore(tmp_path / "memory")
        entry = store.write("The user prefers tea.", category="general")
        store.propose("A fact from an email.", origin="test")
        store.delete(entry["id"])
    assert_owner_only(tmp_path / "memory")


def test_embedding_index_is_owner_only(tmp_path):
    class Embedder:
        model = "test-embedder"

        def embed(self, texts):
            return [[1.0, 0.0] for _ in texts]

    with umask(0o000):
        store = MemoryStore(tmp_path / "memory")
        store.write("The user prefers tea.", category="general")
        EmbeddingIndex(tmp_path / "memory" / "derived" / "embeddings.json", Embedder()).sync(store)
    assert (tmp_path / "memory" / "derived" / "embeddings.json").is_file()
    assert_owner_only(tmp_path / "memory")


def test_briefs_are_owner_only(tmp_path):
    data = load_morning_fixture(FIXTURE, pending_count=0)
    day = "\n".join(["# Good morning", "", "## Today"] + [f"- {e['start'][11:16]} {e['title']}" for e in data.events]
                    + ["", "0 memory items await your review."]) + "\n"
    ex = Executor(Router(load_models()), {"local_private": FakeClient(day), "cheap_api": FakeClient(),
                                          "frontier_api": FakeClient()})
    params = {"fixture": str(FIXTURE), "brief_dir": str(tmp_path / "briefs"), "day": "2026-11-26",
              "pending_count": 0, "yesterday_cost_usd": 0.0}
    with umask(0o000):
        result = run_brief_workflow(ex, params)
    assert result["verified"] is True
    assert_owner_only(tmp_path / "briefs")
    assert json.loads((tmp_path / "briefs" / "2026-11-26.json").read_text())["verified"] is True


@pytest.mark.parametrize("bad_mask", [0o000, 0o022, 0o002])
def test_no_writer_falls_back_to_umask_defaults(tmp_path, bad_mask):
    """The regression guard: a fresh home under any common umask has no group/world-readable content."""
    home = tmp_path / f"home-{bad_mask:o}"
    with umask(bad_mask):
        trace = TraceWriter(home / "traces" / "gateway.jsonl", run_id="r", session_id="s")
        trace.write("x", {})
        EgressLog(home / "egress.jsonl").record(route="r", model="m", host="h", prompt="p", response="r")
        RunStore(home / "runs").create("f", None)
        MemoryStore(home / "memory").write("A fact.")
    for store in ("traces", "runs", "memory"):
        assert_owner_only(home / store)
    assert mode(home / "egress.jsonl") == 0o600

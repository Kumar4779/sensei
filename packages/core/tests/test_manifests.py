"""Slice 24: guardrail manifest integrity. Tamper with the guardrails and the run refuses.

Every test works on temporary copies of the shipped manifests, so the real
tools.toml and models.toml are never edited.
"""

import hashlib
import os
import shutil
import stat
from pathlib import Path

import pytest

from sensei_core.bench import load_task, run_benchmark
from sensei_core.config import load_models
from sensei_core.executor import Executor
from sensei_core.manifests import MODELS_MANIFEST, TOOLS_MANIFEST, ManifestGuard, ManifestTamperError, default_paths
from sensei_core.permissions import PermissionGate, load_tools
from sensei_core.router import FakeClient, Router
from sensei_core.trace import TraceWriter, read_events

TASK_DIR = Path(__file__).resolve().parents[3] / "evals" / "tasks" / "morning-brief-001"


@pytest.fixture
def manifests(tmp_path):
    d = tmp_path / "manifests"
    d.mkdir()
    paths = {"tools.toml": d / "tools.toml", "models.toml": d / "models.toml"}
    shutil.copyfile(TOOLS_MANIFEST, paths["tools.toml"])
    shutil.copyfile(MODELS_MANIFEST, paths["models.toml"])
    for p in paths.values():
        os.chmod(p, 0o644)
    return paths


@pytest.fixture
def trace(tmp_path):
    return TraceWriter(tmp_path / "trace.jsonl", run_id="r1", session_id="s1")


def build(manifests, trace):
    guard = ManifestGuard(manifests, trace)
    clients = {k: FakeClient(f"{k} answer") for k in ("local_private", "cheap_api", "frontier_api")}
    router = Router(load_models(manifests["models.toml"]), trace=trace)
    return Executor(router, clients, trace=trace, manifests=guard), clients


def events(trace, kind):
    return [e["payload"] for e in read_events(trace.path) if e["event_type"] == kind]


def tamper(path, line="\n# edited after startup\n"):
    with open(path, "a", encoding="utf-8") as f:
        f.write(line)


# ---- (a) untouched manifests: the run proceeds ---------------------------------------


def test_run_proceeds_when_manifests_are_untouched(manifests, trace):
    ex, clients = build(manifests, trace)
    assert ex.run_once("hello", "LOCAL_ONLY", "simple").response == "local_private answer"
    ex.run_once("hello again", "API_OK", "simple")
    assert events(trace, "manifest_tamper_detected") == []


# ---- (b) (c) an edit between startup and a run refuses the run --------------------------


@pytest.mark.parametrize("name", ["tools.toml", "models.toml"])
def test_editing_a_manifest_refuses_the_run(manifests, trace, name):
    ex, clients = build(manifests, trace)
    tamper(manifests[name])
    with pytest.raises(ManifestTamperError) as raised:
        ex.run_once("hello", "LOCAL_ONLY", "simple")
    assert raised.value.changed == [name] and "manifest_tamper_detected" in str(raised.value)
    assert events(trace, "manifest_tamper_detected") == [{"changed": [name]}]
    assert events(trace, "run_started") == []  # refused before anything ran
    assert all(c.prompts == [] for c in clients.values())


def test_both_edited_names_both(manifests, trace):
    ex, _ = build(manifests, trace)
    tamper(manifests["tools.toml"])
    tamper(manifests["models.toml"])
    with pytest.raises(ManifestTamperError) as raised:
        ex.run_once("hello", "API_OK", "simple")
    assert sorted(raised.value.changed) == ["models.toml", "tools.toml"]


def test_a_permission_widening_edit_is_caught(manifests, trace):
    """The realistic attack: loosen a tool's sensitivity so LOCAL_ONLY data may leave."""
    ex, _ = build(manifests, trace)
    text = manifests["tools.toml"].read_text(encoding="utf-8")
    manifests["tools.toml"].write_text(text.replace('sensitivity = ["API_OK"]', 'sensitivity = ["API_OK", "LOCAL_ONLY"]', 1),
                                       encoding="utf-8")
    assert manifests["tools.toml"].read_text(encoding="utf-8") != text
    with pytest.raises(ManifestTamperError):
        ex.run_once("hello", "LOCAL_ONLY", "simple")


def test_deleting_a_manifest_is_tampering(manifests, trace):
    ex, _ = build(manifests, trace)
    manifests["models.toml"].unlink()
    with pytest.raises(ManifestTamperError) as raised:
        ex.run_once("hello", "LOCAL_ONLY", "simple")
    assert raised.value.changed == ["models.toml"]


def test_restoring_the_exact_bytes_passes_again(manifests, trace):
    ex, _ = build(manifests, trace)
    original = manifests["tools.toml"].read_bytes()
    tamper(manifests["tools.toml"])
    with pytest.raises(ManifestTamperError):
        ex.run_once("hello", "LOCAL_ONLY", "simple")
    manifests["tools.toml"].write_bytes(original)
    ex.run_once("hello", "LOCAL_ONLY", "simple")  # the check is on content, not timestamps


# ---- no silent reload path -------------------------------------------------------------


def test_a_changed_manifest_cannot_be_reloaded_in_the_same_process(manifests, trace):
    build(manifests, trace)
    tamper(manifests["tools.toml"])
    tamper(manifests["models.toml"])
    with pytest.raises(ManifestTamperError):
        load_tools(manifests["tools.toml"])  # the PermissionGate's loader
    with pytest.raises(ManifestTamperError):
        load_models(manifests["models.toml"])  # the Router's loader
    with pytest.raises(ManifestTamperError):
        ManifestGuard(manifests, trace)  # nor can a fresh guard re-pin the new bytes


def test_gate_and_router_keep_their_startup_view(manifests, trace):
    gate = PermissionGate(load_tools(manifests["tools.toml"]))
    router = Router(load_models(manifests["models.toml"]))
    ManifestGuard(manifests, trace)
    before = (gate.decide("web_search", "LOCAL_ONLY").outcome, router.route("local_private").model)
    manifests["models.toml"].write_text(manifests["models.toml"].read_text().replace("qwen3:8b", "evil:1b"))
    assert (gate.decide("web_search", "LOCAL_ONLY").outcome, router.route("local_private").model) == before


# ---- (d) manifests_loaded carries digests, never content ----------------------------------


def test_manifests_loaded_records_digests_only(manifests, trace):
    build(manifests, trace)
    [loaded] = events(trace, "manifests_loaded")
    assert loaded == {name: "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest() for name, p in manifests.items()}
    raw = trace.path.read_text(encoding="utf-8")
    for marker in ("[routes", "[tools", "qwen3:8b", "nomic-embed-text", "est_cost_per_1k_tokens"):
        assert marker not in raw


# ---- startup permission self-check --------------------------------------------------------


@pytest.mark.parametrize("mode, who", [(0o664, ["group"]), (0o646, ["world"]), (0o666, ["group", "world"])])
def test_loose_permissions_warn_but_do_not_refuse(manifests, trace, mode, who):
    os.chmod(manifests["tools.toml"], mode)
    ex, _ = build(manifests, trace)
    assert events(trace, "manifest_permissions_loose") == [
        {"file": "tools.toml", "mode": oct(mode), "writable_by": who}]
    ex.run_once("hello", "LOCAL_ONLY", "simple")  # v1: a warning, not a refusal


def test_tight_permissions_raise_no_warning(manifests, trace):
    build(manifests, trace)
    assert events(trace, "manifest_permissions_loose") == []


# ---- every construction path is guarded ---------------------------------------------------


def test_a_default_executor_guards_the_shipped_manifests(trace):
    ex = Executor(Router(load_models()), {k: FakeClient() for k in ("local_private", "cheap_api", "frontier_api")},
                  trace=trace)
    assert ex.manifests.paths == {n: p.resolve() for n, p in default_paths().items()}
    assert set(events(trace, "manifests_loaded")[0]) == {"tools.toml", "models.toml"}


def test_bench_raises_instead_of_recording_a_failed_trial(manifests, trace):
    ex, _ = build(manifests, trace)
    tamper(manifests["models.toml"])
    with pytest.raises(ManifestTamperError):
        run_benchmark(load_task(TASK_DIR), ex, trials=1)

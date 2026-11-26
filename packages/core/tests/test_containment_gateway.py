"""Slices 32 and 34: the probe at gateway startup, GET /posture, and strict mode."""

import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sensei_core.config import load_models  # noqa: E402
from sensei_core.containment import STORE_CHECKS, STRICT_ENV  # noqa: E402
from sensei_core.egress import EgressLog  # noqa: E402
from sensei_core.executor import Executor  # noqa: E402
from sensei_core.gateway import TOKEN_ENV, GatewayConfigError, create_app  # noqa: E402
from sensei_core.memory import MemoryStore  # noqa: E402
from sensei_core.router import FakeClient, Router  # noqa: E402
from sensei_core.runs import RunStore  # noqa: E402
from sensei_core.trace import TraceWriter, read_events  # noqa: E402
from tests.test_gateway_ui import FIXTURE, good_prose  # noqa: E402

TOKEN = "test-token-not-a-secret"
AUTH = {"X-Sensei-Token": TOKEN}


def parts(home, loosen=False):
    trace = TraceWriter(home / "traces" / "gateway.jsonl", run_id="gateway", session_id="local")
    clients = {"local_private": FakeClient("l"), "cheap_api": FakeClient("c"), "frontier_api": FakeClient("f")}
    ex = Executor(Router(load_models(), trace=trace), clients, trace=trace)  # writes manifests_loaded
    if loosen:  # a planted failure: someone opened the traces up after Sensei created them
        os.chmod(trace.path.parent, 0o755)
        os.chmod(trace.path, 0o644)
    return ex, trace


def app_for(home, loosen=False, **kw):
    ex, trace = parts(home, loosen)
    return create_app(ex, trace.path, MemoryStore(home / "memory"), trace, **kw), trace


def test_startup_traces_the_probe_and_posture_needs_the_token(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    app, trace = app_for(tmp_path)
    [ev] = [e for e in read_events(trace.path) if e["event_type"] == "containment_probe"]
    checks = [r["check"] for r in ev["payload"]["results"]]
    assert checks == ["manifests", "ground_truth", "egress", "kill_state", *STORE_CHECKS, "token"]
    client = TestClient(app)
    assert client.get("/posture").status_code == 401
    body = client.get("/posture", headers=AUTH).json()
    assert set(body) == set(ev["payload"]) and [r["check"] for r in body["results"]] == checks
    egress = next(r for r in body["results"] if r["check"] == "egress")
    assert egress["status"] == "warn" and "(fake client)" in egress["evidence"]
    assert TOKEN not in str(body) and TOKEN not in trace.path.read_text()


def test_default_mode_starts_despite_a_fail(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "short")  # a planted failure: a 5-character token
    app, trace = app_for(tmp_path)
    assert [r.check for r in app.state.posture.failed] == ["token"]


def test_strict_mode_refuses_on_a_planted_failure_and_starts_when_clean(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, "short")
    with pytest.raises(GatewayConfigError, match="containment probe failed in strict mode: token"):
        app_for(tmp_path / "a", strict=True)
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    app, _ = app_for(tmp_path / "b", strict=True)  # clean: only warns (egress), which strict allows
    assert app.state.posture.strict and not app.state.posture.failed


def test_strict_mode_from_the_environment(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    monkeypatch.setenv(STRICT_ENV, "1")
    app, _ = app_for(tmp_path / "clean")
    assert app.state.posture.strict
    with pytest.raises(GatewayConfigError, match="trace_store: .* readable by any local user"):
        app_for(tmp_path / "loose", loosen=True)


def test_a_fresh_gateway_with_a_default_umask_passes_the_trace_store_check(tmp_path, monkeypatch):
    # Slice 32 pinned the opposite: under umask 022 a fresh gateway's traces were 755/644 and this
    # check failed. Slice 33 creates them owner-only, so the same startup now passes.
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    old = os.umask(0o022)  # the common default
    try:
        app, trace = app_for(tmp_path)
    finally:
        os.umask(old)
    result = next(r for r in app.state.posture.results if r.check == "trace_store")
    assert result.status == "pass", result.evidence
    assert not app.state.posture.failed
    assert oct(trace.path.parent.stat().st_mode & 0o777) == "0o700" and oct(trace.path.stat().st_mode & 0o777) == "0o600"


# ---- Slice 34: every content store -------------------------------------------------------


def full_gateway(home, **kw):
    """A gateway given every content store, as main() builds it (fake clients; no Ollama)."""
    trace = TraceWriter(home / "traces" / "gateway.jsonl", run_id="gateway", session_id="local")
    clients = {"local_private": FakeClient(good_prose().replace("{pending}", "0")),
               "cheap_api": FakeClient("c"), "frontier_api": FakeClient("f")}
    ex = Executor(Router(load_models(), trace=trace), clients, trace=trace,
                  egress=EgressLog(home / "egress.jsonl"), runs=RunStore(home / "runs"))
    return create_app(ex, trace.path, MemoryStore(home / "memory"), trace, brief_fixture=FIXTURE,
                      brief_dir=home / "briefs", egress_path=home / "egress.jsonl", **kw)


def populated_home(home):
    """Every store written once by Sensei itself, then the gateway closed."""
    client = TestClient(full_gateway(home))
    assert client.post("/brief/run", headers=AUTH).json()["verified"] is True  # traces, runs, briefs
    r = client.post("/run", headers=AUTH, json={"prompt": "hi", "sensitivity_class": "API_OK", "difficulty": "simple"})
    assert r.status_code == 200, r.json()  # an API route: egress.jsonl
    MemoryStore(home / "memory").write("The user prefers tea.")
    return home


# Files no store re-tightens when it is opened, so a loosening survives to the probe.
LOOSENED = {"trace_store": "traces/old.jsonl", "run_store": "runs", "memory_store": "memory/changelog.jsonl",
            "brief_store": "briefs", "egress_log": "egress.jsonl"}


@pytest.mark.parametrize("check", list(STORE_CHECKS))
def test_strict_mode_refuses_startup_on_any_store_failing(tmp_path, monkeypatch, check):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    home = populated_home(tmp_path / "home")
    (home / "traces" / "old.jsonl").write_text("{}\n")
    os.chmod(home / "traces" / "old.jsonl", 0o600)
    full_gateway(home, strict=True)  # clean: starts
    target = home / LOOSENED[check]
    if target.is_dir():
        target = next(p for p in sorted(target.glob("*")) if p.is_file())
    os.chmod(target, 0o644)
    with pytest.raises(GatewayConfigError, match=f"containment probe failed in strict mode: {check}: "):
        full_gateway(home, strict=True)
    app = full_gateway(home)  # default mode: reported, not fatal
    assert [r.check for r in app.state.posture.failed] == [check]


def test_posture_serves_the_per_store_table(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    app = full_gateway(tmp_path)
    body = TestClient(app).get("/posture", headers=AUTH).json()
    rows = {row["store"]: row for row in body["stores"]}
    assert list(rows) == list(STORE_CHECKS)
    assert rows["trace_store"] == {"store": "trace_store", "path": str(tmp_path / "traces"), "status": "pass",
                                   "decided_by": "the directory", "mode": "700",
                                   "evidence": f"{tmp_path / 'traces'} (700) and its files: owner only"}
    assert rows["memory_store"]["status"] == "pass"  # MemoryStore creates its directory on open
    for store in ("run_store", "brief_store", "egress_log"):  # nothing written yet
        assert rows[store]["status"] == "skipped" and rows[store]["evidence"].startswith("not_created:")
    assert body["summary"] == {"pass": 6, "warn": 1, "fail": 0, "skipped": 3}
    startup = next(e for e in read_events(tmp_path / "traces" / "gateway.jsonl") if e["event_type"] == "containment_probe")
    assert startup["payload"]["stores"] == body["stores"]  # same structure as the startup event


def test_live_a_fresh_home_under_umask_022_passes_every_store_after_one_brief(tmp_path, monkeypatch):
    """The regression guard: Sensei writes every store itself, under the common default umask."""
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    old = os.umask(0o022)
    try:
        home = populated_home(tmp_path / "home")
        app = full_gateway(home)  # the next startup probes what the first one wrote
    finally:
        os.umask(old)
    stores = {r.check: r for r in app.state.posture.stores}
    assert list(stores) == list(STORE_CHECKS)
    assert all(r.status == "pass" for r in stores.values()), {k: r.evidence for k, r in stores.items()}
    assert list((home / "runs").glob("*.json")) and list((home / "briefs").glob("*.md"))
    assert list((home / "memory" / "entries").glob("*.json"))
    body = TestClient(app).get("/posture", headers=AUTH).json()
    assert body["summary"]["fail"] == 0 and body["summary"]["skipped"] == 0

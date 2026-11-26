"""Slice 32: the probe at gateway startup, GET /posture, and strict mode."""

import os

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sensei_core.config import load_models  # noqa: E402
from sensei_core.containment import STRICT_ENV  # noqa: E402
from sensei_core.executor import Executor  # noqa: E402
from sensei_core.gateway import TOKEN_ENV, GatewayConfigError, create_app  # noqa: E402
from sensei_core.memory import MemoryStore  # noqa: E402
from sensei_core.router import FakeClient, Router  # noqa: E402
from sensei_core.trace import TraceWriter, read_events  # noqa: E402

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
    assert checks == ["manifests", "ground_truth", "egress", "kill_state", "trace_store", "token"]
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

"""Slice 29: the gateway's kill switch survives a restart and fails closed."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sensei_core.config import SpendCaps, load_models  # noqa: E402
from sensei_core.executor import Executor  # noqa: E402
from sensei_core.gateway import TOKEN_ENV, create_app  # noqa: E402
from sensei_core.killswitch import KillSwitch  # noqa: E402
from sensei_core.memory import MemoryStore  # noqa: E402
from sensei_core.router import FakeClient, Router  # noqa: E402
from sensei_core.trace import TraceWriter, read_events  # noqa: E402

TOKEN = "test-token-not-a-secret"
AUTH = {"X-Sensei-Token": TOKEN}
RUN = {"prompt": "summarise this", "sensitivity_class": "LOCAL_ONLY", "difficulty": "simple"}


def start_gateway(home, budgets=None):
    """A fresh gateway process over a Sensei home: new trace writer, executor and app."""
    trace = TraceWriter(home / "traces" / "gateway.jsonl", run_id="gateway", session_id="local")
    switch = KillSwitch(home / "killed.json", trace)
    clients = {"local_private": FakeClient("local answer"), "cheap_api": FakeClient("cheap answer"),
               "frontier_api": FakeClient("frontier answer")}
    executor = Executor(Router(load_models(), trace=trace), clients, trace=trace, budgets=budgets,
                        kill_switch=switch)
    app = create_app(executor, trace.path, MemoryStore(home / "memory"), trace, kill_switch=switch)
    return TestClient(app), trace, clients


def types(trace):
    return [e["event_type"] for e in read_events(trace.path)]


def test_kill_survives_a_restart_and_revive_clears_it(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    first, trace, _ = start_gateway(tmp_path)
    assert first.post("/kill", headers=AUTH).json() == {"killed": True}
    assert (tmp_path / "killed.json").is_file()

    second, trace, clients = start_gateway(tmp_path)  # restart: new app instance, same home
    assert second.get("/health").json() == {"status": "ok", "killed": True}
    r = second.post("/run", json=RUN, headers=AUTH)
    assert r.status_code == 503 and r.json() == {"error": "kill switch engaged"}
    assert clients["local_private"].prompts == []

    assert second.post("/revive", headers=AUTH).json() == {"killed": False}
    assert not (tmp_path / "killed.json").exists()
    third, _, clients = start_gateway(tmp_path)
    assert third.post("/run", json=RUN, headers=AUTH).status_code == 200
    t = types(trace)
    assert t.index("kill_switch_engaged") < t.index("gateway_run_refused") < t.index("kill_switch_disengaged")


def test_kill_file_written_behind_the_gateway_takes_effect_on_the_next_request(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    client, _, _ = start_gateway(tmp_path)
    KillSwitch(tmp_path / "killed.json").engage()  # e.g. another process, or by hand
    assert client.post("/run", json=RUN, headers=AUTH).status_code == 503
    assert client.post("/brief/run", headers=AUTH).status_code == 503


def test_a_corrupt_kill_file_starts_the_gateway_killed(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    (tmp_path / "killed.json").write_text("{half a write")
    client, trace, clients = start_gateway(tmp_path)
    assert client.get("/health").json()["killed"] is True
    assert client.post("/run", json=RUN, headers=AUTH).status_code == 503
    assert clients["local_private"].prompts == []
    assert "kill_state_unreadable" in types(trace)
    client.post("/revive", headers=AUTH)  # revive removes even a corrupt file
    assert client.post("/run", json=RUN, headers=AUTH).status_code == 200


def test_spend_cap_is_a_402_with_the_cap_type(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    client, _, clients = start_gateway(tmp_path, budgets=SpendCaps(daily_cap_usd=0.0))
    r = client.post("/run", json={**RUN, "sensitivity_class": "API_OK", "prompt": "x" * 400}, headers=AUTH)
    assert r.status_code == 402 and r.json()["error"] == "spend cap reached" and r.json()["cap"] == "daily"
    assert clients["cheap_api"].prompts == []

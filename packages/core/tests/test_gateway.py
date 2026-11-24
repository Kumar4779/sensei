import uuid

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sensei_core.config import load_models  # noqa: E402
from sensei_core.executor import Executor  # noqa: E402
from sensei_core.gateway import TOKEN_ENV, GatewayConfigError, create_app  # noqa: E402
from sensei_core.memory import MemoryStore  # noqa: E402
from sensei_core.router import FakeClient, Router  # noqa: E402
from sensei_core.trace import TraceWriter, read_events  # noqa: E402

TOKEN = "test-token-not-a-secret"
AUTH = {"X-Sensei-Token": TOKEN}
RUN = {"prompt": "summarise this", "sensitivity_class": "API_OK", "difficulty": "simple"}


@pytest.fixture
def parts(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    trace = TraceWriter(tmp_path / "trace.jsonl", run_id="r1", session_id="s1")
    clients = {
        "local_private": FakeClient("local answer"),
        "cheap_api": FakeClient("cheap answer"),
        "frontier_api": FakeClient("frontier answer"),
    }
    executor = Executor(Router(load_models(), trace=trace), clients, trace=trace)
    store = MemoryStore(tmp_path / "memory")
    app = create_app(executor, trace.path, store, trace)
    return TestClient(app), trace, store, clients


def event_types(trace):
    return [e["event_type"] for e in read_events(trace.path)]


def test_health_is_open_and_run_needs_a_token(parts):
    client, trace, _, clients = parts
    assert client.get("/health").json() == {"status": "ok", "killed": False}
    r = client.post("/run", json=RUN)
    assert r.status_code == 401
    assert client.post("/run", json=RUN, headers={"X-Sensei-Token": "wrong"}).status_code == 401
    assert clients["cheap_api"].prompts == []
    assert event_types(trace).count("gateway_auth_failed") == 2
    for method, path in (("get", "/costs"), ("get", "/memory/beliefs"), ("post", "/kill")):
        assert getattr(client, method)(path).status_code == 401


def test_run_happy_path(parts):
    client, _, _, clients = parts
    r = client.post("/run", json=RUN, headers=AUTH)
    assert r.status_code == 200
    body = r.json()
    assert body["route"] == "cheap_api"
    assert body["response"] == "cheap answer"
    assert body["total_est_cost_usd"] > 0
    assert clients["cheap_api"].prompts == ["summarise this"]


def test_kill_switch_refuses_runs_until_revived(parts):
    client, trace, _, clients = parts
    assert client.post("/kill", headers=AUTH).json() == {"killed": True}
    assert client.get("/health").json()["killed"] is True
    r = client.post("/run", json=RUN, headers=AUTH)
    assert r.status_code == 503
    assert r.json() == {"error": "kill switch engaged"}
    assert clients["cheap_api"].prompts == []
    assert client.post("/revive", headers=AUTH).json() == {"killed": False}
    assert client.post("/run", json=RUN, headers=AUTH).status_code == 200
    types = event_types(trace)
    assert types.index("kill_switch_engaged") < types.index("gateway_run_refused") < types.index("kill_switch_disengaged")


def test_costs_reflect_a_completed_run(parts):
    client, _, _, _ = parts
    assert client.get("/costs", headers=AUTH).json()["by_route"] == {}
    run = client.post("/run", json=RUN, headers=AUTH).json()
    costs = client.get("/costs", headers=AUTH).json()
    assert costs["basis"] == "estimated"
    assert costs["by_route"] == {"cheap_api": pytest.approx(run["total_est_cost_usd"])}
    assert sum(costs["by_day"].values()) == pytest.approx(run["total_est_cost_usd"])


def test_memory_flow_over_http(parts):
    client, _, store, _ = parts
    assert client.get("/memory/beliefs", headers=AUTH).json() == []

    fact = store.write("Prefers meetings after 10am")
    proposal = store.propose("Dentist on Tuesday", origin="calendar invite")
    beliefs = client.get("/memory/beliefs", headers=AUTH).json()
    assert beliefs == [{"id": fact["id"], "fact": "Prefers meetings after 10am", "category": "general"}]
    pending = client.get("/memory/pending", headers=AUTH).json()
    assert [p["id"] for p in pending] == [proposal["id"]]
    assert pending[0]["origin"] == "calendar invite"

    assert client.post("/memory/confirm", json={"entry_id": uuid.uuid4().hex}, headers=AUTH).status_code == 404
    assert client.post("/memory/confirm", json={"entry_id": "../x"}, headers=AUTH).status_code == 404
    r = client.post("/memory/confirm", json={"entry_id": proposal["id"]}, headers=AUTH)
    assert r.json() == {"id": proposal["id"], "status": "active"}

    assert client.delete(f"/memory/{fact['id']}", headers=AUTH).json() == {"deleted": fact["id"]}
    ids = [b["id"] for b in client.get("/memory/beliefs", headers=AUTH).json()]
    assert fact["id"] not in ids and proposal["id"] in ids
    assert store.probe("10am") == []
    assert client.delete(f"/memory/{fact['id']}", headers=AUTH).status_code == 404


def test_budget_exceeded_returns_402(parts):
    client, _, _, clients = parts
    r = client.post("/run", json={**RUN, "max_cost_usd": 0.0}, headers=AUTH)
    assert r.status_code == 402
    assert r.json()["error"] == "budget exceeded"
    assert clients["cheap_api"].prompts == []


def test_app_refuses_to_start_without_token(tmp_path, monkeypatch):
    monkeypatch.delenv(TOKEN_ENV, raising=False)
    trace = TraceWriter(tmp_path / "t.jsonl", run_id="r", session_id="s")
    executor = Executor(Router(load_models()), {})
    with pytest.raises(GatewayConfigError, match=TOKEN_ENV):
        create_app(executor, trace.path, MemoryStore(tmp_path / "m"), trace)
    monkeypatch.setenv(TOKEN_ENV, "")
    with pytest.raises(GatewayConfigError):
        create_app(executor, trace.path, MemoryStore(tmp_path / "m"), trace)

"""Slice 31: POST /runs/<id>/resume on the gateway, behind the same gates as a new run."""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sensei_core import runs as runs_mod  # noqa: E402
from sensei_core.brief import load_morning_fixture  # noqa: E402
from sensei_core.config import load_models  # noqa: E402
from sensei_core.executor import Executor  # noqa: E402
from sensei_core.gateway import TOKEN_ENV, create_app  # noqa: E402
from sensei_core.killswitch import KillSwitch  # noqa: E402
from sensei_core.memory import MemoryStore  # noqa: E402
from sensei_core.router import FakeClient, Router  # noqa: E402
from sensei_core.runs import RunStore  # noqa: E402
from sensei_core.trace import TraceWriter  # noqa: E402
from tests.test_resume_replay import FIXTURE, Scripted, SimulatedCrash, good_day, new_process  # noqa: E402

TOKEN = "test-token-not-a-secret"
AUTH = {"X-Sensei-Token": TOKEN}


def gateway(home, local):
    trace = TraceWriter(home / "traces" / "gateway.jsonl", run_id="gateway", session_id="local")
    switch = KillSwitch(home / "killed.json", trace)
    clients = {"local_private": local, "cheap_api": FakeClient("c"), "frontier_api": FakeClient("f")}
    ex = Executor(Router(load_models(), trace=trace), clients, trace=trace, kill_switch=switch,
                  runs=RunStore(home / "runs"))
    app = create_app(ex, trace.path, MemoryStore(home / "memory"), trace, brief_fixture=FIXTURE,
                     brief_dir=home / "briefs", kill_switch=switch)
    return TestClient(app)


def crashed_brief_run(home, monkeypatch):
    (home / "briefs").mkdir(parents=True)
    client = gateway(home, Scripted("a draft that misses everything", crash_on=2))
    with pytest.raises(SimulatedCrash):
        client.post("/brief/run", headers=AUTH)
    [path] = (home / "runs").glob("*.json")
    new_process(monkeypatch)
    return path.stem


def test_a_crashed_brief_resumes_over_http(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    run_id = crashed_brief_run(tmp_path, monkeypatch)
    local = Scripted(good_day(load_morning_fixture(FIXTURE, pending_count=0)))
    client = gateway(tmp_path, local)
    r = client.post(f"/runs/{run_id}/resume", headers=AUTH)
    assert r.status_code == 200, r.json()
    body = r.json()
    assert body["status"] == "completed" and body["reused_steps"] == 1 and body["result"]["verified"] is True
    assert len(local.prompts) == 1
    again = client.post(f"/runs/{run_id}/resume", headers=AUTH)
    assert again.status_code == 409 and "already completed" in again.json()["error"]


def test_resume_needs_the_token_and_respects_the_kill_switch(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    run_id = crashed_brief_run(tmp_path, monkeypatch)
    local = Scripted("unused")
    client = gateway(tmp_path, local)
    assert client.post(f"/runs/{run_id}/resume").status_code == 401
    client.post("/kill", headers=AUTH)
    r = client.post(f"/runs/{run_id}/resume", headers=AUTH)
    assert r.status_code == 503 and local.prompts == []
    assert runs_mod.effective_status(RunStore(tmp_path / "runs").load(run_id)) == "crashed"  # untouched
    client.post("/revive", headers=AUTH)
    assert client.post("/runs/not-a-run/resume", headers=AUTH).status_code == 404

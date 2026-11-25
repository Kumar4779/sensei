"""Slice 22: the web UI over the gateway. The UI is a client; these test its routes and data."""

import json
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")

from fastapi.testclient import TestClient  # noqa: E402

from sensei_core.config import load_embeddings, load_models  # noqa: E402
from sensei_core.custodian import Custodian  # noqa: E402
from sensei_core.executor import Executor  # noqa: E402
from sensei_core.gateway import ALLOW_LAN_ENV, TOKEN_ENV, UI_DIR, bind_host, create_app  # noqa: E402
from sensei_core.ledger import CostLedger  # noqa: E402
from sensei_core.memory import MemoryStore  # noqa: E402
from sensei_core.router import FakeClient, Router  # noqa: E402
from sensei_core.trace import TraceWriter, read_events  # noqa: E402

TOKEN = "test-token-not-a-secret"
AUTH = {"X-Sensei-Token": TOKEN}
FIXTURE = Path(__file__).resolve().parents[3] / "evals" / "tasks" / "morning-brief-001" / "fixture"


def good_prose():
    from sensei_core.brief import load_morning_fixture

    data = load_morning_fixture(FIXTURE)
    lines = ["Good morning.", ""] + [f"- {e['start'][11:16]} {e['title']}" for e in data.events]
    return "\n".join(lines + ["", "{pending} memory items await review.", "Cost: $0.00."]) + "\n"


@pytest.fixture
def parts(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    trace = TraceWriter(tmp_path / "trace.jsonl", run_id="r1", session_id="s1")
    local = FakeClient(good_prose().replace("{pending}", "0"))
    clients = {"local_private": local, "cheap_api": FakeClient("cheap"), "frontier_api": FakeClient("frontier")}
    executor = Executor(Router(load_models(), trace=trace), clients, trace=trace)
    store = MemoryStore(tmp_path / "memory")
    app = create_app(executor, trace.path, store, trace, brief_fixture=FIXTURE, brief_dir=tmp_path / "briefs")
    return TestClient(app), trace, store, local


# ---- UI routes and auth --------------------------------------------------------


def test_ui_app_files_require_the_token(parts):
    client, trace, _, _ = parts
    for name in ("app.js", "app.css"):
        assert client.get(f"/ui/app/{name}").status_code == 401
        assert client.get(f"/ui/app/{name}", headers={"X-Sensei-Token": "wrong"}).status_code == 401
    assert "gateway_auth_failed" in [e["event_type"] for e in read_events(trace.path)]


def test_static_assets_are_served_with_the_token(parts):
    client, _, _, _ = parts
    js = client.get("/ui/app/app.js", headers=AUTH)
    css = client.get("/ui/app/app.css", headers=AUTH)
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert css.status_code == 200 and css.headers["content-type"].startswith("text/css")
    assert js.text == (UI_DIR / "app.js").read_text(encoding="utf-8")
    assert js.headers["cache-control"] == "no-store" and js.headers["x-content-type-options"] == "nosniff"


def test_only_listed_app_files_are_served(parts):
    client, _, _, _ = parts
    for name in ("index.html", "..%2Fgateway.py", "missing.js"):
        assert client.get(f"/ui/app/{name}", headers=AUTH).status_code == 404


def test_bare_address_redirects_to_the_ui(parts):
    client, _, _, _ = parts
    r = client.get("/", follow_redirects=False)
    assert r.status_code == 307 and r.headers["location"] == "/ui"


def test_ui_shell_is_public_but_holds_no_data(parts):
    client, _, store, _ = parts
    store.write("The user is allergic to peanuts.")
    r = client.get("/ui")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "sessionStorage" in r.text and "localStorage" not in r.text and "X-Sensei-Token" in r.text
    assert "peanut" not in r.text and TOKEN not in r.text


def test_ui_uses_only_existing_endpoints_and_safe_text_insertion():
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    for path in ("/health", "/kill", "/revive", "/costs/today", "/brief/today", "/brief/run",
                 "/memory/pending", "/memory/confirm", "\"/memory/\" + encodeURIComponent"):
        assert path in js, path
    assert ".innerHTML" not in js and "localStorage" not in js


# ---- approvals: the UI's data is /memory/pending, including Custodian proposals ------------


def test_approvals_data_matches_the_pending_endpoint(parts):
    client, _, store, _ = parts
    custodian = Custodian(store, None, search_min_score=0.5, dedup_threshold=0.9)
    proposed = custodian.propose("The user prefers aisle seats on flights.", client="claude-code")
    store.write("The user is allergic to peanuts.")  # active: not an approval
    pending = client.get("/memory/pending", headers=AUTH).json()
    assert [e["id"] for e in pending] == [e["id"] for e in store.list_pending()] == [proposed["id"]]
    assert pending[0] == {k: proposed[k] for k in ("id", "fact", "category", "source", "origin", "status")}
    js = (UI_DIR / "app.js").read_text(encoding="utf-8")
    for field in ("e.fact", "e.category", "e.status", "e.origin", "e.id"):
        assert field in js  # every field the approvals list shows comes from that output


def test_confirm_and_reject_from_the_ui_use_the_existing_endpoints(parts):
    client, _, store, _ = parts
    keep = store.propose("The user prefers aisle seats.", origin="mcp:claude-code")
    drop = store.propose("Bogus fact.", origin="mcp:claude-code")
    assert client.post("/memory/confirm", json={"entry_id": keep["id"]}, headers=AUTH).json()["status"] == "active"
    assert client.delete(f"/memory/{drop['id']}", headers=AUTH).json() == {"deleted": drop["id"]}
    assert client.get("/memory/pending", headers=AUTH).json() == []


# ---- brief ------------------------------------------------------------------------------


def test_brief_run_then_today_reports_verified_honestly(parts):
    client, trace, _, local = parts
    assert client.get("/brief/today", headers=AUTH).status_code == 404
    run = client.post("/brief/run", headers=AUTH)
    assert run.status_code == 200, run.text
    assert run.json()["verified"] is True and run.json()["attempts"] == 1
    today = client.get("/brief/today", headers=AUTH).json()
    assert today["day"] == date.today().isoformat() and today["verified"] is True
    assert "## Inbox (every message, as received)" in today["text"]  # the assembled workflow
    assert "brief_generated" in [e["event_type"] for e in read_events(trace.path)]


def test_unverified_brief_is_reported_unverified(parts):
    client, _, _, local = parts
    local.response = "Good morning. Nothing much today.\n"
    body = client.post("/brief/run", headers=AUTH).json()
    assert body["verified"] is False and body["attempts"] == 2
    assert "event[0]" in body["failed_checks"]
    assert client.get("/brief/today", headers=AUTH).json()["verified"] is False


def test_brief_endpoints_require_the_token_and_respect_the_kill_switch(parts):
    client, _, _, local = parts
    assert client.post("/brief/run").status_code == 401
    assert client.get("/brief/today").status_code == 401
    client.post("/kill", headers=AUTH)
    r = client.post("/brief/run", headers=AUTH)
    assert r.status_code == 503 and r.json()["error"] == "kill switch engaged"
    assert local.prompts == []  # refused before any model call
    client.post("/revive", headers=AUTH)
    assert client.post("/brief/run", headers=AUTH).status_code == 200


def test_brief_without_a_data_source_says_so(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    executor = Executor(Router(load_models()), {k: FakeClient("x") for k in ("local_private", "cheap_api", "frontier_api")})
    client = TestClient(create_app(executor, tmp_path / "t.jsonl", MemoryStore(tmp_path / "m")))
    assert client.post("/brief/run", headers=AUTH).status_code == 409
    assert client.get("/ui").status_code == 200


# ---- costs today, per route ----------------------------------------------------------------


def write_call(path, ts, route, cost):
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"event_type": "model_call", "timestamp": ts, "payload": {"route": route, "est_cost_usd": cost}}) + "\n")


def test_costs_today_is_per_route_for_the_utc_day(parts, tmp_path):
    client, trace, _, _ = parts
    now = datetime.now(timezone.utc)
    write_call(trace.path, now.isoformat(), "cheap_api", 0.002)
    write_call(trace.path, now.isoformat(), "frontier_api", 0.01)
    write_call(trace.path, (now - timedelta(days=2)).isoformat(), "cheap_api", 5.0)
    body = client.get("/costs/today", headers=AUTH).json()
    assert body["day_utc"] == now.date().isoformat() and body["basis"] == "estimated"
    assert body["by_route"] == {"cheap_api": 0.002, "frontier_api": 0.01}
    assert body["total_est_cost_usd"] == pytest.approx(0.012)
    assert client.get("/costs", headers=AUTH).json()["by_route"]["cheap_api"] == pytest.approx(5.002)  # unchanged
    assert client.get("/costs/today").status_code == 401


def test_ledger_by_route_without_day_is_unchanged(tmp_path):
    path = tmp_path / "t.jsonl"
    write_call(path, "2026-11-01T10:00:00+00:00", "a", 1.0)
    write_call(path, "2026-11-02T10:00:00+00:00", "a", 2.0)
    ledger = CostLedger(path)
    assert ledger.by_route() == {"a": 3.0}
    assert ledger.by_route(day="2026-11-02") == {"a": 2.0}


# ---- reachability: localhost by default, LAN only by explicit opt-in --------------------------


def test_bind_host_defaults_to_localhost_and_lan_needs_exact_opt_in(caplog):
    assert bind_host({}) == "127.0.0.1"
    for value in ("", "0", "true", "yes", "on"):
        assert bind_host({ALLOW_LAN_ENV: value}) == "127.0.0.1"
    with caplog.at_level("WARNING", logger="sensei.gateway"):
        assert bind_host({ALLOW_LAN_ENV: "1"}) == "0.0.0.0"
    assert any("reachable from every device on this network" in r.getMessage() for r in caplog.records)

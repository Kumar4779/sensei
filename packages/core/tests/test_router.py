import pytest

from sensei_core.config import ConfigError, load_models
from sensei_core.router import FakeClient, ModelClient, RouteRequest, Router
from sensei_core.trace import TraceWriter, read_events


@pytest.fixture
def router():
    return Router(load_models())


def route_for(router, sensitivity, difficulty, prior_failures=0):
    return router.decide(RouteRequest(sensitivity, difficulty, prior_failures))


def test_local_only_beats_hard_difficulty(router):
    d = route_for(router, "LOCAL_ONLY", "hard")
    assert d.route == "local_private"
    assert d.reason.startswith("sensitivity")


def test_api_ok_simple_goes_cheap(router):
    d = route_for(router, "API_OK", "simple")
    assert d.route == "cheap_api"
    assert d.reason == "cheapest capable default"


def test_api_ok_hard_goes_frontier(router):
    d = route_for(router, "API_OK", "hard")
    assert d.route == "frontier_api"
    assert d.reason.startswith("task earns frontier")


def test_standard_task_escalates_after_failure(router):
    d = route_for(router, "API_OK", "standard", prior_failures=1)
    assert d.route == "frontier_api"
    assert d.reason.startswith("escalation after failure")


def test_simple_task_does_not_escalate(router):
    assert route_for(router, "API_OK", "simple", prior_failures=1).route == "cheap_api"


def test_each_decision_writes_one_trace_event(tmp_path):
    trace = TraceWriter(tmp_path / "run.jsonl", run_id="r1", session_id="s1")
    router = Router(load_models(), trace=trace)
    d = router.decide(RouteRequest("API_OK", "hard"))
    events = read_events(trace.path)
    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "route_decision"
    assert e["payload"] == {
        "sensitivity_class": "API_OK",
        "difficulty": "hard",
        "prior_failures": 0,
        "route": d.route,
        "model": d.model,
        "reason": d.reason,
    }


def test_fake_client_returns_canned_string_and_records_prompts():
    client = FakeClient(response="canned")
    assert isinstance(client, ModelClient)
    assert client.complete("first") == "canned"
    assert client.complete("second") == "canned"
    assert client.prompts == ["first", "second"]


def test_invalid_request_is_rejected():
    with pytest.raises(ValueError):
        RouteRequest("SECRET", "simple")
    with pytest.raises(ValueError):
        RouteRequest("API_OK", "trivial")
    with pytest.raises(ValueError):
        RouteRequest("API_OK", "simple", prior_failures=-1)


def test_router_requires_all_three_routes():
    routes = load_models()
    del routes["frontier_api"]
    with pytest.raises(ConfigError, match="frontier_api"):
        Router(routes)

import pytest

from sensei_core.config import load_models
from sensei_core.executor import Executor, NoClientConfigured
from sensei_core.router import FakeClient, Router
from sensei_core.trace import TraceWriter, read_events


class FailingClient:
    """Raises on the first `failures` calls, then answers."""

    def __init__(self, failures=1, response="recovered"):
        self.failures = failures
        self.response = response
        self.prompts = []

    def complete(self, prompt):
        self.prompts.append(prompt)
        if len(self.prompts) <= self.failures:
            raise ConnectionError("simulated outage")
        return self.response


@pytest.fixture
def trace(tmp_path):
    return TraceWriter(tmp_path / "run.jsonl", run_id="r1", session_id="s1")


def make_executor(trace, **clients):
    defaults = {
        "local_private": FakeClient("local answer"),
        "cheap_api": FakeClient("cheap answer"),
        "frontier_api": FakeClient("frontier answer"),
    }
    defaults.update(clients)
    return Executor(Router(load_models(), trace=trace), defaults, trace=trace), defaults


def event_types(trace):
    return [e["event_type"] for e in read_events(trace.path)]


def test_api_ok_simple_completes_via_cheap_api(trace):
    ex, _ = make_executor(trace)
    result = ex.run_once("summarise this", "API_OK", "simple")
    assert result.route == "cheap_api"
    assert result.response == "cheap answer"
    assert result.escalated is False
    assert event_types(trace) == ["manifests_loaded", "run_opened", "run_started", "route_decision",
                                  "shadow_decision", "model_call", "run_completed", "run_closed"]


def test_local_only_never_touches_api_clients(trace):
    ex, clients = make_executor(trace)
    result = ex.run_once("my private note", "LOCAL_ONLY", "hard")
    assert result.route == "local_private"
    assert result.response == "local answer"
    assert clients["local_private"].prompts == ["my private note"]
    assert clients["cheap_api"].prompts == []
    assert clients["frontier_api"].prompts == []


def test_cheap_failure_escalates_to_frontier(trace):
    ex, clients = make_executor(trace, cheap_api=FailingClient(failures=1))
    result = ex.run_once("draft a plan", "API_OK", "standard")
    assert result.route == "frontier_api"
    assert result.response == "frontier answer"
    assert result.escalated is True
    assert clients["cheap_api"].prompts == ["draft a plan"]
    assert event_types(trace) == [
        "manifests_loaded",  # Slice 24
        "run_opened",  # Slice 31
        "run_started",
        "route_decision",
        "shadow_decision",  # Slice 30: one per run, after the first routing decision
        "route_failure",  # Slice 23
        "route_decision",
        "run_escalated",
        "model_call",
        "run_completed",
        "run_closed",
    ]


def test_local_failure_fails_without_touching_api(trace):
    ex, clients = make_executor(trace, local_private=FailingClient(failures=1))
    with pytest.raises(ConnectionError):
        ex.run_once("my private note", "LOCAL_ONLY", "simple", max_retries=0)
    assert clients["cheap_api"].prompts == []
    assert clients["frontier_api"].prompts == []
    types = event_types(trace)
    assert types[-2:] == ["run_failed", "run_closed"]  # Slice 31: every run is closed
    assert "run_escalated" not in types and "model_call" not in types


def test_missing_client_raises_naming_the_route(trace):
    ex = Executor(Router(load_models(), trace=trace), {"cheap_api": FakeClient()}, trace=trace)
    with pytest.raises(NoClientConfigured, match="frontier_api"):
        ex.run_once("hard question", "API_OK", "hard")
    assert event_types(trace)[-2:] == ["run_failed", "run_closed"]


def test_prompt_text_is_not_written_to_trace(trace):
    ex, _ = make_executor(trace)
    ex.run_once("SECRET-PROMPT-TEXT", "LOCAL_ONLY", "simple")
    raw = trace.path.read_text(encoding="utf-8")
    assert "SECRET-PROMPT-TEXT" not in raw
    assert "local answer" not in raw


def test_simple_task_retries_cheap_once_without_escalating(trace):
    ex, clients = make_executor(trace, cheap_api=FailingClient(failures=1))
    result = ex.run_once("fix this typo", "API_OK", "simple")
    assert result.route == "cheap_api"
    assert result.response == "recovered"
    assert result.escalated is False
    assert len(clients["cheap_api"].prompts) == 2
    assert clients["frontier_api"].prompts == []
    assert "run_retried" in event_types(trace)
    assert "run_escalated" not in event_types(trace)


def test_local_failure_retries_on_same_local_route(trace):
    ex, clients = make_executor(trace, local_private=FailingClient(failures=1))
    result = ex.run_once("my private note", "LOCAL_ONLY", "simple", max_retries=1)
    assert result.route == "local_private"
    assert result.response == "recovered"
    assert result.escalated is False
    assert len(clients["local_private"].prompts) == 2
    assert clients["cheap_api"].prompts == []
    assert clients["frontier_api"].prompts == []
    types = event_types(trace)
    assert "run_retried" in types and "run_escalated" not in types
    assert types.count("route_decision") == 1

"""Slice 23: egress is logged (sizes + digest, never content); fallback is never silent."""

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from sensei_core.config import load_models
from sensei_core.egress import EgressLog, prompt_digest
from sensei_core.executor import Executor
from sensei_core.router import FakeClient, RouteDecision, Router, RoutingError
from sensei_core.trace import TraceWriter, read_events


class Failing:
    def __init__(self, error=ConnectionError, host=None):
        self.error, self.prompts = error, []
        if host:
            self.destination_host = host

    def complete(self, prompt):
        self.prompts.append(prompt)
        raise self.error("simulated outage")


class Hosted(FakeClient):
    destination_host = "api.example-cheap.com"


@pytest.fixture
def trace(tmp_path):
    return TraceWriter(tmp_path / "trace.jsonl", run_id="r1", session_id="s1")


@pytest.fixture
def egress(tmp_path):
    return EgressLog(tmp_path / "home" / "egress.jsonl")


def executor(trace, egress=None, router=None, **overrides):
    clients = {"local_private": FakeClient("local answer"), "cheap_api": FakeClient("cheap answer"),
               "frontier_api": FakeClient("frontier answer")}
    clients.update(overrides)
    return Executor(router or Router(load_models(), trace=trace), clients, trace=trace, egress=egress), clients


def events(trace, kind=None):
    return [e for e in read_events(trace.path) if kind is None or e["event_type"] == kind]


# ---- no silent fallback -------------------------------------------------------------


def test_cheap_failure_with_escalation_available_goes_to_frontier(trace):
    ex, clients = executor(trace, cheap_api=Failing())
    result = ex.run_once("draft a plan", "API_OK", "standard", max_retries=1)
    assert (result.route, result.escalated) == ("frontier_api", True)
    [failure] = [e["payload"] for e in events(trace, "route_failure")]
    assert failure == {"route": "cheap_api", "model": load_models()["cheap_api"].model, "error_class": "ConnectionError"}
    assert clients["local_private"].prompts == []


def test_escalation_exhausted_fails_the_run_and_never_touches_local(trace):
    ex, clients = executor(trace, cheap_api=Failing(), frontier_api=Failing(TimeoutError))
    with pytest.raises(TimeoutError):
        ex.run_once("draft a plan", "API_OK", "standard", max_retries=1)
    failures = [(e["payload"]["route"], e["payload"]["error_class"]) for e in events(trace, "route_failure")]
    assert failures == [("cheap_api", "ConnectionError"), ("frontier_api", "TimeoutError")]
    assert clients["local_private"].prompts == []
    assert [e["event_type"] for e in events(trace)][-1] == "run_failed"


def test_simple_task_retries_the_same_route_only(trace):
    ex, clients = executor(trace, cheap_api=Failing())
    with pytest.raises(ConnectionError):
        ex.run_once("summarise", "API_OK", "simple", max_retries=2)
    assert len(clients["cheap_api"].prompts) == 3  # first try + 2 retries, all on cheap_api
    assert clients["frontier_api"].prompts == [] and clients["local_private"].prompts == []
    assert {e["payload"]["route"] for e in events(trace, "route_failure")} == {"cheap_api"}


def test_a_router_that_tries_to_fall_back_to_local_is_refused(trace):
    class FallbackRouter(Router):
        def decide(self, request):
            if request.prior_failures:
                return RouteDecision("local_private", "qwen3:8b", "sneaky fallback")
            return super().decide(request)

    ex, clients = executor(trace, router=FallbackRouter(load_models(), trace=trace), cheap_api=Failing())
    with pytest.raises(RoutingError, match="refusing fallback"):
        ex.run_once("draft a plan", "API_OK", "standard", max_retries=3)
    assert clients["local_private"].prompts == []


def test_local_only_behaviour_is_unchanged(trace):
    ex, clients = executor(trace, local_private=Failing())
    with pytest.raises(ConnectionError):
        ex.run_once("my private note", "LOCAL_ONLY", "hard", max_retries=1)
    assert len(clients["local_private"].prompts) == 2  # retried on the same local route only
    assert clients["cheap_api"].prompts == [] and clients["frontier_api"].prompts == []
    assert [e["payload"]["route"] for e in events(trace, "route_failure")] == ["local_private", "local_private"]


# ---- egress log ---------------------------------------------------------------------


def test_api_call_logs_sizes_and_digest_never_content(trace, egress):
    prompt = "Draft a reply to Priya about the hike photos — résumé attached"
    ex, _ = executor(trace, egress, cheap_api=Hosted("Sure, here is a draft."))
    ex.run_once(prompt, "API_OK", "simple")
    [line] = egress.lines()
    assert line["route"] == "cheap_api" and line["model"] == load_models()["cheap_api"].model
    assert line["destination_host"] == "api.example-cheap.com"
    assert line["request_bytes"] == len(prompt.encode("utf-8")) > len(prompt)  # bytes, not chars
    assert line["response_bytes"] == len(b"Sure, here is a draft.")
    assert line["prompt_sha256"] == "sha256:" + hashlib.sha256(prompt.encode()).hexdigest() == prompt_digest(prompt)
    assert line["ok"] is True and datetime.fromisoformat(line["timestamp"]).tzinfo is not None
    raw = egress.path.read_text(encoding="utf-8")
    assert "Priya" not in raw and "draft" not in raw.casefold()


def test_local_routes_write_nothing(trace, egress):
    ex, _ = executor(trace, egress)
    ex.run_once("my private note", "LOCAL_ONLY", "simple")
    assert not egress.path.exists()


def test_failed_api_call_is_logged_and_escalation_logs_both(trace, egress):
    ex, _ = executor(trace, egress, cheap_api=Failing(host="api.example-cheap.com"))
    ex.run_once("draft a plan", "API_OK", "standard", max_retries=1)
    lines = egress.lines()
    assert [(l["route"], l["ok"]) for l in lines] == [("cheap_api", False), ("frontier_api", True)]
    assert lines[0]["response_bytes"] == 0 and lines[0]["error_class"] == "ConnectionError"
    assert lines[1]["destination_host"] == load_models()["frontier_api"].provider  # no declared host: provider


def test_budget_halt_sends_nothing_and_logs_nothing(trace, egress):
    ex, clients = executor(trace, egress)
    with pytest.raises(Exception):
        ex.run_once("x" * 4000, "API_OK", "simple", max_cost_usd=0.0)
    assert clients["cheap_api"].prompts == [] and not egress.path.exists()


def test_summary_counts_calls_and_bytes_per_destination_for_one_day(egress):
    egress.record(route="cheap_api", model="m", host="a.example", prompt="abc", response="de")
    egress.record(route="cheap_api", model="m", host="a.example", prompt="xyz", response=None, error_class="E")
    egress.record(route="frontier_api", model="f", host="b.example", prompt="hello", response="hi")
    old = egress.lines()[0] | {"timestamp": (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()}
    with egress.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(old) + "\n")
    today = datetime.now(timezone.utc).date().isoformat()
    assert egress.summary(today) == {"day_utc": today, "by_destination": {
        "a.example": {"calls": 2, "request_bytes": 6, "response_bytes": 2, "total_bytes": 8},
        "b.example": {"calls": 1, "request_bytes": 5, "response_bytes": 2, "total_bytes": 7},
    }}

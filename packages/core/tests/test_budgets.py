import json

import pytest

from sensei_core.config import load_models
from sensei_core.executor import BudgetExceeded, Executor
from sensei_core.ledger import CostLedger
from sensei_core.router import FakeClient, Router
from sensei_core.trace import TraceWriter, read_events


class FailingClient:
    def __init__(self, failures, response="recovered"):
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


def make_executor(trace, **overrides):
    clients = {
        "local_private": FakeClient("local answer"),
        "cheap_api": FakeClient("cheap answer"),
        "frontier_api": FakeClient("frontier answer"),
    }
    clients.update(overrides)
    return Executor(Router(load_models(), trace=trace), clients, trace=trace), clients


def events(trace, event_type):
    return [e for e in read_events(trace.path) if e["event_type"] == event_type]


def test_model_call_carries_cost_and_run_total_matches(trace):
    ex, _ = make_executor(trace)
    result = ex.run_once("summarise this article for me", "API_OK", "simple")
    calls = events(trace, "model_call")
    assert len(calls) == 1
    call = calls[0]["payload"]
    assert call["est_cost_usd"] > 0
    assert call["est_tokens_in"] == 8  # ceil(29 / 4)
    assert call["est_tokens_out"] == 3  # ceil(12 / 4)
    assert call["cost_basis"].startswith("estimated")
    completed = events(trace, "run_completed")[0]["payload"]
    assert completed["total_est_cost_usd"] == pytest.approx(sum(c["payload"]["est_cost_usd"] for c in calls))
    assert result.total_est_cost_usd == pytest.approx(completed["total_est_cost_usd"])


def test_run_total_includes_retries(trace):
    ex, _ = make_executor(trace, cheap_api=FailingClient(failures=1))
    result = ex.run_once("draft a plan", "API_OK", "standard")
    assert result.escalated is True
    calls = events(trace, "model_call")
    completed = events(trace, "run_completed")[0]["payload"]
    assert completed["total_est_cost_usd"] == pytest.approx(sum(c["payload"]["est_cost_usd"] for c in calls))


def test_zero_budget_halts_before_any_api_call(trace):
    ex, clients = make_executor(trace)
    with pytest.raises(BudgetExceeded) as info:
        ex.run_once("summarise this", "API_OK", "simple", max_cost_usd=0.0)
    assert info.value.limit_usd == 0.0
    assert info.value.accumulated_cost_usd == 0.0
    assert clients["cheap_api"].prompts == []
    assert clients["frontier_api"].prompts == []
    types = [e["event_type"] for e in read_events(trace.path)]
    assert "budget_exceeded" in types
    assert "model_call" not in types


def test_local_only_is_exempt_from_cost_budget(trace):
    ex, clients = make_executor(trace)
    result = ex.run_once("my private note", "LOCAL_ONLY", "simple", max_cost_usd=0.0)
    assert result.route == "local_private"
    assert result.total_est_cost_usd == 0.0
    assert clients["local_private"].prompts == ["my private note"]


def test_max_retries_bounds_attempts(trace):
    ex, clients = make_executor(
        trace, cheap_api=FailingClient(failures=5), frontier_api=FailingClient(failures=5)
    )
    with pytest.raises(ConnectionError):
        ex.run_once("draft a plan", "API_OK", "standard", max_retries=2)
    attempts = len(clients["cheap_api"].prompts) + len(clients["frontier_api"].prompts)
    assert attempts == 3  # first call + 2 retries
    assert events(trace, "run_failed")


def test_budget_can_stop_an_escalation(trace):
    ex, clients = make_executor(trace, cheap_api=FailingClient(failures=1))
    # Enough for a cheap call, not for a frontier one: 1 token in costs 0.000001 / 0.00001.
    with pytest.raises(BudgetExceeded):
        ex.run_once("hi", "API_OK", "standard", max_cost_usd=0.000005)
    assert clients["frontier_api"].prompts == []
    assert events(trace, "budget_exceeded")


def _write(path, lines):
    path.write_text("".join(json.dumps(l) + "\n" for l in lines), encoding="utf-8")


def _call(ts, route, cost):
    return {
        "timestamp": ts,
        "run_id": "r",
        "session_id": "s",
        "event_type": "model_call",
        "payload": {"route": route, "est_cost_usd": cost},
        "cost_usd": None,
    }


def test_ledger_totals_by_route_and_day(tmp_path):
    path = tmp_path / "trace.jsonl"
    _write(
        path,
        [
            _call("2026-11-23T09:00:00+00:00", "cheap_api", 0.001),
            _call("2026-11-23T23:30:00+00:00", "frontier_api", 0.02),
            # 01:00 in UTC+08:00 is still 2026-11-23 in UTC.
            _call("2026-11-24T01:00:00+08:00", "cheap_api", 0.002),
            _call("2026-11-24T12:00:00+00:00", "frontier_api", 0.03),
            {"timestamp": "2026-11-24T12:00:01+00:00", "event_type": "run_completed",
             "payload": {"total_est_cost_usd": 99.0}},
        ],
    )
    before = path.read_bytes()
    ledger = CostLedger(path)
    assert ledger.by_route() == pytest.approx({"cheap_api": 0.003, "frontier_api": 0.05})
    assert ledger.by_day() == pytest.approx({"2026-11-23": 0.023, "2026-11-24": 0.03})
    assert ledger.total_est_cost_usd == pytest.approx(0.053)
    assert path.read_bytes() == before

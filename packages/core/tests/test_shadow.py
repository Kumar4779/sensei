"""Slice 30: the shadow router decides on every run, inherits every guardrail, and never executes."""

import dataclasses
import itertools
import json
from pathlib import Path

import pytest

from sensei_core import shadow_report
from sensei_core.bench import load_task, run_benchmark
from sensei_core.brief import load_morning_fixture
from sensei_core.config import load_models
from sensei_core.executor import Executor
from sensei_core.router import FakeClient, RouteRequest, Router
from sensei_core.shadow import POLICY, ShadowPolicy
from sensei_core.trace import TraceWriter, read_events

TASK_DIR = Path(__file__).resolve().parents[3] / "evals" / "tasks" / "morning-brief-001"


@pytest.fixture
def trace(tmp_path):
    return TraceWriter(tmp_path / "trace.jsonl", run_id="r1", session_id="s1")


def make_executor(trace, routes=None):
    clients = {"local_private": FakeClient("local answer"), "cheap_api": FakeClient("cheap answer"),
               "frontier_api": FakeClient("frontier answer")}
    return Executor(Router(routes or load_models(), trace=trace), clients, trace=trace), clients


def shadow_events(trace):
    return [e["payload"] for e in read_events(trace.path) if e["event_type"] == "shadow_decision"]


def tables():
    """The shipped routing table, and adversarial ones where the forbidden routes are the cheapest."""
    shipped = load_models()
    api_free = {**shipped, "local_private": dataclasses.replace(shipped["local_private"], est_cost_per_1k_tokens=5.0),
                "cheap_api": dataclasses.replace(shipped["cheap_api"], est_cost_per_1k_tokens=0.0),
                "frontier_api": dataclasses.replace(shipped["frontier_api"], est_cost_per_1k_tokens=0.0)}
    frontier_free = {**shipped,
                     "frontier_api": dataclasses.replace(shipped["frontier_api"], est_cost_per_1k_tokens=0.0)}
    # Tables that make one forbidden route the cheapest, so each guard is tested on its own:
    # "rogue" is an API-tier route claiming LOCAL_ONLY (load_models() would refuse it, so the
    # shadow must not rely on config validation); "vault" is a free local route without API_OK.
    local = shipped["local_private"]
    rogue = {**shipped, "local_private": dataclasses.replace(local, est_cost_per_1k_tokens=1.0),
             "rogue": dataclasses.replace(shipped["cheap_api"], name="rogue", est_cost_per_1k_tokens=0.0,
                                          sensitivity=frozenset({"LOCAL_ONLY", "API_OK"}))}
    vault = {**shipped, "local_private": dataclasses.replace(local, est_cost_per_1k_tokens=1.0),
             "vault": dataclasses.replace(local, name="vault", est_cost_per_1k_tokens=0.0,
                                                     sensitivity=frozenset({"LOCAL_ONLY"}),
                                                     max_sensitivity="LOCAL_ONLY")}
    return {"shipped": shipped, "api_free": api_free, "frontier_free": frontier_free, "rogue": rogue, "vault": vault}


# ---- the shadow never violates a guardrail ----------------------------------------------


@pytest.mark.parametrize("table", ["shipped", "api_free", "frontier_free", "rogue", "vault"])
def test_shadow_never_violates_a_guardrail(table):
    routes = tables()[table]
    router, policy = Router(routes), ShadowPolicy(routes)
    combos = itertools.product(("LOCAL_ONLY", "API_OK"), ("simple", "standard", "hard"), range(4),
                               (0, 1, 4000, 10**6))
    for sensitivity, difficulty, failures, chars in combos:
        req = RouteRequest(sensitivity, difficulty, prior_failures=failures)
        rules = router.decide(req)
        shadow = policy.decide(req, rules, chars)
        route = routes[shadow.route]
        case = (table, sensitivity, difficulty, failures, chars, shadow.route)
        assert sensitivity in route.sensitivity, case  # the Router's own RoutingError check
        if sensitivity == "LOCAL_ONLY":
            assert route.tier == "local", case  # LOCAL_ONLY means local, always
        elif difficulty == "hard":
            assert route.tier == "frontier_api", case
        if sensitivity == "API_OK" and failures >= 1:
            assert shadow.route in (rules.route, "frontier_api"), case  # no silent fallback
        assert rules.route in shadow.eligible, case  # the rules' choice is always eligible


def test_the_rule_cheapest_eligible_route_with_ties_to_the_rules():
    policy, router = ShadowPolicy(load_models()), Router(load_models())

    def pick(sensitivity, difficulty, failures=0, chars=4000):
        req = RouteRequest(sensitivity, difficulty, prior_failures=failures)
        return policy.decide(req, router.decide(req), chars).route

    assert pick("API_OK", "simple") == "local_private"  # $0.00 beats cheap_api's price
    assert pick("API_OK", "standard") == "local_private"
    assert pick("API_OK", "hard") == "frontier_api"
    assert pick("LOCAL_ONLY", "hard") == "local_private"
    assert pick("API_OK", "simple", failures=1) == "cheap_api"  # never falls back to local
    assert pick("API_OK", "standard", failures=1) == "frontier_api"
    assert pick("API_OK", "simple", chars=0) == "cheap_api"  # an all-zero tie goes to the rules


# ---- it decides on every run, and never executes ----------------------------------------


def test_shadow_choice_is_never_called(trace):
    ex, clients = make_executor(trace)
    result = ex.run_once("x" * 4000, "API_OK", "simple")
    assert result.route == "cheap_api"
    assert clients["cheap_api"].prompts == ["x" * 4000]
    assert clients["local_private"].prompts == [] and clients["frontier_api"].prompts == []  # the shadow's pick
    [ev] = shadow_events(trace)
    assert ev["shadow_route"] == "local_private" and ev["agree"] is False
    assert len([e for e in read_events(trace.path) if e["event_type"] == "model_call"]) == 1


def test_shadow_decision_fields(trace):
    ex, _ = make_executor(trace)
    with ex.run_scope(task_family="demo"):
        ex.run_once("x" * 4000, "API_OK", "simple")
    ex.run_once("my diary entry", "LOCAL_ONLY", "hard")
    first, second = shadow_events(trace)
    assert first == {
        "policy": POLICY, "task_family": "demo", "sensitivity_class": "API_OK", "difficulty": "simple",
        "prior_failures": 0, "prompt_chars": 4000, "rules_route": "cheap_api", "shadow_route": "local_private",
        "agree": False, "eligible": ["cheap_api", "frontier_api", "local_private"],
        "rules_est_cost_usd": pytest.approx(load_models()["cheap_api"].est_cost_per_1k_tokens), "shadow_est_cost_usd": 0.0,
        "cost_basis": first["cost_basis"],
    }
    assert "input tokens" in first["cost_basis"]
    assert second["task_family"] is None and second["agree"] is True
    assert second["rules_route"] == second["shadow_route"] == "local_private" and second["eligible"] == ["local_private"]
    assert "diary" not in json.dumps(second)  # never the prompt


def test_one_shadow_decision_per_run_even_with_retries(trace):
    class FailOnce:
        prompts = []

        def complete(self, prompt):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                raise ConnectionError("outage")
            return "ok"

    ex, clients = make_executor(trace)
    ex._clients["cheap_api"] = FailOnce()
    ex.run_once("draft a plan", "API_OK", "standard")  # escalates to frontier after the failure
    assert len(shadow_events(trace)) == 1


def test_a_shadow_bug_never_breaks_the_run(trace, monkeypatch):
    ex, clients = make_executor(trace)
    monkeypatch.setattr(ex.shadow, "decide", lambda *a: 1 / 0)
    assert ex.run_once("hello", "LOCAL_ONLY", "simple").response == "local answer"
    types = [e["event_type"] for e in read_events(trace.path)]
    assert "shadow_error" in types and "shadow_decision" not in types and "run_completed" in types


def test_bench_trials_record_shadow_decisions_with_the_task_family(trace):
    task = load_task(TASK_DIR)
    data = load_morning_fixture(task.fixture_dir)
    day = "\n".join(["# Good morning", "", "## Today"] + [f"- {e['start'][11:16]} {e['title']}" for e in data.events]
                    + ["", f"{data.pending_count} memory items await your review."]) + "\n"
    ex, clients = make_executor(trace)
    ex._clients["local_private"] = FakeClient(day)
    report = run_benchmark(task, ex, trials=2)
    assert report.aggregates["pass^1"] == 1.0  # grading unchanged
    events = shadow_events(trace)
    assert len(events) == 2 and {e["task_family"] for e in events} == {"morning-brief-001@v1"}
    assert all(e["agree"] and e["rules_route"] == "local_private" for e in events)  # LOCAL_ONLY task


# ---- the report -------------------------------------------------------------------------


def write_synthetic(path, rows):
    with path.open("w") as f:
        for family, rules_cost, shadow_cost, agree in rows:
            f.write(json.dumps({"event_type": "shadow_decision", "payload": {
                "policy": POLICY, "task_family": family, "rules_est_cost_usd": rules_cost,
                "shadow_est_cost_usd": shadow_cost, "agree": agree}}) + "\n")
        f.write(json.dumps({"event_type": "model_call", "payload": {"est_cost_usd": 9.0}}) + "\n")
        f.write('{"event_type": "shadow_deci')  # a torn last line is skipped


def test_report_math_on_a_synthetic_trace(tmp_path, capsys):
    write_synthetic(tmp_path / "a.jsonl", [("brief", 0.0, 0.0, True), ("brief", 0.0, 0.0, True),
                                           ("chat", 0.001, 0.0, False), ("chat", 0.010, 0.010, True)])
    (tmp_path / "sub").mkdir()
    write_synthetic(tmp_path / "sub" / "b.jsonl", [(None, 0.002, 0.0, False)])
    before = {p: p.read_bytes() for p in tmp_path.rglob("*.jsonl")}

    s = shadow_report.summarize(shadow_report.shadow_events(shadow_report.trace_files([tmp_path])))
    assert s["families"]["brief"] == {"runs": 2, "agree": 2, "est_cost_diff_usd": 0.0, "agreement_rate": 1.0}
    chat = s["families"]["chat"]
    assert (chat["runs"], chat["agree"], chat["agreement_rate"]) == (2, 1, 0.5)
    assert chat["est_cost_diff_usd"] == pytest.approx(-0.001)
    assert s["families"]["(unknown)"]["est_cost_diff_usd"] == pytest.approx(-0.002)
    o = s["overall"]
    assert (o["runs"], o["agree"], o["agreement_rate"]) == (5, 3, 0.6)
    assert o["est_cost_diff_usd"] == pytest.approx(-0.003)
    assert s["policies"] == [POLICY]

    assert shadow_report.main([str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "overall" in out and "60.0%" in out and "-0.003000" in out
    assert {p: p.read_bytes() for p in tmp_path.rglob("*.jsonl")} == before  # read-only


def test_report_with_no_traces_says_so(tmp_path, capsys):
    assert shadow_report.main([str(tmp_path / "missing")]) == 1
    assert "no trace files" in capsys.readouterr().err

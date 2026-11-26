"""Slice 31: run records, crash-safe resume, and deterministic replay."""

import json
from pathlib import Path

import pytest

from sensei_core import replay as replay_mod
from sensei_core import runs as runs_mod
from sensei_core.brief import load_morning_fixture
from sensei_core.config import SpendCaps, load_models
from sensei_core.executor import Executor
from sensei_core.killswitch import KillSwitch, KillSwitchEngaged
from sensei_core.resume import resume_run
from sensei_core.router import FakeClient, Router
from sensei_core.runs import RunNotResumable, RunStore, effective_status
from sensei_core.trace import TraceWriter, read_events
from sensei_core.workflows import BRIEF_FAMILY, run_brief_workflow

FIXTURE = Path(__file__).resolve().parents[3] / "evals" / "tasks" / "morning-brief-001" / "fixture"


class SimulatedCrash(BaseException):
    """What the run sees when its process dies: not an Exception, so nothing handles it."""


class Scripted:
    """Answers in order; on call number `crash_on` the process "dies"; `on_call` hooks run first."""

    def __init__(self, *responses, crash_on=None, on_call=None):
        self.responses, self.crash_on, self.on_call, self.prompts = list(responses), crash_on, on_call or {}, []

    def complete(self, prompt):
        self.prompts.append(prompt)
        n = len(self.prompts)
        if n in self.on_call:
            self.on_call[n]()
        if n == self.crash_on:
            raise SimulatedCrash()
        return self.responses[min(n, len(self.responses)) - 1]


def good_day(data):
    lines = ["# Good morning", "", "## Today"] + [f"- {e['start'][11:16]} {e['title']}" for e in data.events]
    return "\n".join(lines + ["", f"{data.pending_count} memory items await your review."]) + "\n"


@pytest.fixture
def home(tmp_path):
    return tmp_path / "home"


@pytest.fixture
def params(home):
    (home / "briefs").mkdir(parents=True)
    return {"fixture": str(FIXTURE), "brief_dir": str(home / "briefs"), "day": "2026-11-26",
            "pending_count": 2, "yesterday_cost_usd": 0.0}


def build(home, local, **kw):
    trace = TraceWriter(home / "traces" / "gateway.jsonl", run_id="gateway", session_id="local")
    clients = {"local_private": local, "cheap_api": FakeClient("cheap"), "frontier_api": FakeClient("frontier")}
    ex = Executor(Router(load_models(), trace=trace), clients, trace=trace, runs=RunStore(home / "runs"), **kw)
    return ex, trace


def start_brief(ex, params, trace):
    with ex.run_scope(BRIEF_FAMILY, workflow={"kind": "brief", "params": params}):
        return run_brief_workflow(ex, params, trace)


def only_record(home):
    [path] = (home / "runs").glob("*.json")
    return json.loads(path.read_text())


def new_process(monkeypatch):
    monkeypatch.setattr(runs_mod, "PROCESS_ID", "another-process")
    import sensei_core.executor as executor_mod
    monkeypatch.setattr(executor_mod, "PROCESS_ID", "another-process")


# ---- run records ------------------------------------------------------------------------


def test_every_run_writes_a_record_with_its_steps_and_checks(home, params):
    data = load_morning_fixture(FIXTURE, pending_count=2)
    ex, trace = build(home, Scripted("a draft that misses everything", good_day(data)))
    result = start_brief(ex, params, trace)
    assert result["verified"] is True and result["attempts"] == 2
    rec = only_record(home)
    assert rec["status"] == "completed" and rec["task_family"] == BRIEF_FAMILY and rec["workflow"]["kind"] == "brief"
    assert [(s["index"], s["route"], s["outcome"]) for s in rec["steps"]] == [(0, "local_private", "ok"),
                                                                             (1, "local_private", "ok")]
    assert all(s["prompt_sha256"].startswith("sha256:") for s in rec["steps"])
    assert [c["passed"] for c in rec["checks"]] == [False, True]
    assert "prompt" not in json.dumps(rec["steps"]).replace("prompt_sha256", "")  # digests, never prompts


def test_a_plain_run_records_no_response_and_is_not_resumable(home):
    ex, _ = build(home, FakeClient("secret answer"))
    ex.run_once("my private prompt", "LOCAL_ONLY", "simple")
    rec = only_record(home)
    assert rec["status"] == "completed" and rec["workflow"] is None
    assert "secret answer" not in json.dumps(rec) and "my private prompt" not in json.dumps(rec)


# ---- resume -----------------------------------------------------------------------------


def test_a_crash_between_steps_resumes_with_no_step_run_twice(home, params, monkeypatch):
    data = load_morning_fixture(FIXTURE, pending_count=2)
    first = Scripted("a draft that misses everything", crash_on=2)  # dies during the repair call
    ex, trace = build(home, first)
    with pytest.raises(SimulatedCrash):
        start_brief(ex, params, trace)
    rec = only_record(home)
    assert rec["status"] == "in_progress" and effective_status(rec) == "in_progress"  # same process: still live
    assert [s["outcome"] for s in rec["steps"]] == ["ok", "in_flight"]  # the point of interruption

    new_process(monkeypatch)
    assert effective_status(runs_mod.RunStore(home / "runs").load(rec["run_id"])) == "crashed"
    second = Scripted(good_day(data))
    ex2, trace2 = build(home, second)
    record, result = resume_run(ex2, rec["run_id"], trace2)

    assert result["verified"] is True and record["status"] == "completed"
    assert len(first.prompts) == 2 and len(second.prompts) == 1  # the draft was reused, not re-run
    assert second.prompts[0] == first.prompts[1]  # only the interrupted repair ran again
    assert [s["outcome"] for s in record["steps"]] == ["ok", "interrupted", "ok"]
    assert [s["resumed"] for s in record["steps"]] == [False, False, True]
    assert record["resumes"][0]["reused_steps"] == 1 and record["run_id"] == rec["run_id"]
    replayed = [e for e in read_events(trace2.path) if e["event_type"] == "step_replayed"]
    assert [e["payload"]["index"] for e in replayed] == [0]


def test_a_killed_run_resumes_after_revive(home, params, monkeypatch):
    data = load_morning_fixture(FIXTURE, pending_count=2)
    switch = KillSwitch(home / "killed.json")
    first = Scripted("a draft that misses everything", on_call={1: switch.engage})  # /kill during the draft
    ex, trace = build(home, first, kill_switch=switch)
    with pytest.raises(KillSwitchEngaged):
        start_brief(ex, params, trace)
    rec = only_record(home)
    assert rec["status"] == "killed" and len(first.prompts) == 1

    second = Scripted(good_day(data))
    ex2, trace2 = build(home, second, kill_switch=switch)
    with pytest.raises(KillSwitchEngaged):  # still killed: resume meets the kill switch like a new run
        resume_run(ex2, rec["run_id"], trace2)
    assert second.prompts == []
    switch.disengage()
    record, result = resume_run(ex2, rec["run_id"], trace2)
    assert record["status"] == "completed" and result["verified"] is True
    assert len(first.prompts) == 1 and len(second.prompts) == 1  # draft once, repair once


def test_resuming_a_completed_run_is_a_clean_error(home, params):
    data = load_morning_fixture(FIXTURE, pending_count=2)
    ex, trace = build(home, FakeClient(good_day(data)))
    start_brief(ex, params, trace)
    rec = only_record(home)
    with pytest.raises(RunNotResumable, match="already completed"):
        resume_run(ex, rec["run_id"], trace)
    with pytest.raises(KeyError):
        resume_run(ex, "no-such-run", trace)
    with pytest.raises(KeyError):
        resume_run(ex, "../../etc/passwd", trace)  # never a path from the caller


def test_a_resume_whose_inputs_changed_diverges_instead_of_guessing(home, params, monkeypatch):
    ex, trace = build(home, Scripted("a draft that misses everything", crash_on=2))
    with pytest.raises(SimulatedCrash):
        start_brief(ex, params, trace)
    rec = only_record(home)
    rec["workflow"]["params"]["pending_count"] = 9  # the draft prompt would now differ
    RunStore(home / "runs").save(rec)
    new_process(monkeypatch)
    local = Scripted("unused")
    ex2, trace2 = build(home, local)
    with pytest.raises(runs_mod.ResumeDiverged):
        resume_run(ex2, rec["run_id"], trace2)
    assert local.prompts == [] and only_record(home)["status"] == "failed"


# ---- replay -----------------------------------------------------------------------------


def brief_trace(home, params):
    data = load_morning_fixture(FIXTURE, pending_count=2)
    ex, trace = build(home, Scripted("a draft that misses everything", good_day(data)))
    start_brief(ex, params, trace)
    return trace.path


def test_replay_of_a_real_brief_trace_matches(home, params, capsys):
    path = brief_trace(home, params)
    result = replay_mod.replay(path)
    assert result.matched, replay_mod.format_result(path, result)
    assert result.runs == 1 and result.decisions >= 12
    assert replay_mod.main([str(path)]) == 0
    assert "MATCH" in capsys.readouterr().out


def test_replay_of_a_crash_and_resume_trace_matches(home, params, monkeypatch):
    data = load_morning_fixture(FIXTURE, pending_count=2)
    ex, trace = build(home, Scripted("a draft that misses everything", crash_on=2))
    with pytest.raises(SimulatedCrash):
        start_brief(ex, params, trace)
    new_process(monkeypatch)
    ex2, trace2 = build(home, Scripted(good_day(data)))
    resume_run(ex2, only_record(home)["run_id"], trace2)
    result = replay_mod.replay(trace2.path)
    assert result.matched, replay_mod.format_result(trace2.path, result)
    assert result.runs == 2 and result.resumed_runs == 1 and len(result.crashed_runs) == 1


def test_replay_of_an_api_run_with_failure_escalation_caps_and_alerts_matches(home):
    class FailOnce:
        prompts = []

        def complete(self, prompt):
            self.prompts.append(prompt)
            if len(self.prompts) == 1:
                raise ConnectionError("outage")
            return "ok"

    rate = load_models()["cheap_api"].est_cost_per_1k_tokens  # one 4000-char call costs about this
    ex, trace = build(home, FakeClient("local"), budgets=SpendCaps(daily_cap_usd=4.5 * rate))
    ex._clients["cheap_api"] = FailOnce()
    ex.run_once("draft a plan", "API_OK", "standard")  # fails on cheap, escalates to frontier
    for i in range(6):  # ~rate each against a 4.5 x rate cap: alerts at 50% and 80%, then refusals
        try:
            ex.run_once("x" * 3996 + f"{i:04d}", "API_OK", "simple", max_cost_usd=1.0)
        except Exception:
            pass  # the cap refuses the later ones
    types = [e["event_type"] for e in read_events(trace.path)]
    assert {"run_escalated", "spend_alert", "spend_cap_reached"} <= set(types)
    result = replay_mod.replay(trace.path)
    assert result.matched, replay_mod.format_result(trace.path, result)


def test_replay_of_a_tampered_trace_reports_the_exact_step(home, params, capsys):
    path = brief_trace(home, params)
    lines = path.read_text().splitlines()
    n = next(i for i, l in enumerate(lines) if json.loads(l)["event_type"] == "route_decision")
    ev = json.loads(lines[n])
    ev["payload"]["route"] = "frontier_api"  # claim the run went to frontier
    lines[n] = json.dumps(ev)
    tampered = path.with_name("tampered.jsonl")
    tampered.write_text("\n".join(lines) + "\n")

    result = replay_mod.replay(tampered)
    assert not result.matched
    d = result.divergence
    assert d.line == n + 1 and d.event_type == "route_decision"
    assert ("route", "local_private", "frontier_api") in d.fields
    assert replay_mod.main([str(tampered)]) == 1
    out = capsys.readouterr().out
    assert f"DIVERGED at line {n + 1} (route_decision)" in out
    assert "route: re-derived 'local_private', recorded 'frontier_api'" in out


@pytest.mark.parametrize("event_type,key,value", [
    ("model_call", "est_cost_usd", 0.5),             # a cost that does not follow from the sizes
    ("verification", "passed", True),                # a verdict that does not follow from the checks
    ("repair_attempted", "verified", False),
    ("run_closed", "status", "killed"),
])
def test_other_tampering_is_caught_at_its_line(home, params, event_type, key, value):
    path = brief_trace(home, params)
    lines = path.read_text().splitlines()
    n = next(i for i, l in enumerate(lines) if json.loads(l)["event_type"] == event_type
             and json.loads(l)["payload"].get(key) != value)
    ev = json.loads(lines[n])
    ev["payload"][key] = value
    lines[n] = json.dumps(ev)
    path.write_text("\n".join(lines) + "\n")
    result = replay_mod.replay(path)
    assert not result.matched and result.divergence.line == n + 1 and result.divergence.event_type == event_type


def test_replay_never_calls_a_model(home, params, monkeypatch):
    path = brief_trace(home, params)

    def boom(*a, **k):
        raise AssertionError("replay made a model call")

    monkeypatch.setattr(FakeClient, "complete", boom)
    monkeypatch.setattr(Scripted, "complete", boom)
    before = path.read_bytes()
    assert replay_mod.replay(path).matched
    assert path.read_bytes() == before  # read-only

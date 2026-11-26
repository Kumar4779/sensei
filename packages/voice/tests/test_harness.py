"""Eval harness v1 (Slice 2): suite scoring, the echo ablation, and one taxonomy label per failure."""

import copy
import json

import pytest

import harness


@pytest.fixture(scope="module")
def results():
    return harness.evaluate(k=3)


@pytest.fixture(scope="module")
def bench():
    return harness.load(harness.BENCH)


def task(bench, task_id):
    return copy.deepcopy(next(t for t in bench["tasks"] if t["id"] == task_id))


def trial(t, tmp_path, echo=True):
    return harness.run_trial(t, tmp_path / f"{t['id']}.jsonl", echo=echo)


def test_all_four_suites_are_scored_at_k3_and_labelled_dev(results):
    assert results["k"] == 3 and results["label"] == "dev (k=3)"
    assert results["eval_versions"] == ["voice-bench-v1", "voice-interrupt-v1"]
    s = results["suites"]
    assert s["correctness"]["tasks"] == 20 and s["correctness"]["trials"] == 60
    assert s["action_safety"]["runs_checked"] == 87 and s["action_safety"]["safety_tasks"]["tasks"] == 10
    assert s["interruption"]["tasks"] == 9 and s["interruption"]["points"] == {
        "mid-answer": 2, "mid-echo": 4, "mid-listing": 3}
    assert "fake" in s["latency"]["path"] and "not real-audio" in s["latency"]["path"]
    assert set(s["latency"]["per_task"]) == {t["id"] for t in harness.load(harness.BENCH)["tasks"]} | {
        t["id"] for t in harness.load(harness.INTERRUPT)["tasks"]}


def test_the_reference_loop_passes_every_suite_with_the_echo_on(results):
    s = results["suites"]
    assert s["action_safety"]["hard_gate"] == "PASS" and s["action_safety"]["gate_violations"] == []
    for suite in ("correctness", "interruption"):
        assert s[suite]["pass^3"] == 1.0, suite
    assert s["action_safety"]["safety_tasks"]["pass^3"] == 1.0
    assert s["interruption"]["stop_trials"] == s["interruption"]["no_stale_plan_trials"] == "27/27"
    assert results["failures"] == []


def test_the_ablation_reports_what_the_echo_prevents_and_what_it_costs(results):
    ab = results["ablation"]
    assert ab["on"]["hard_gate"] == "PASS" and ab["off"]["hard_gate"] == "FAIL"
    assert ab["on"]["safety_failed_trials"] == 0 and ab["off"]["safety_failed_trials"] == 18
    assert ab["off"]["harmful_tasks"] == ["safe-declined-001", "safe-injection-001", "safe-injection-002",
                                          "safe-misheard-001", "safe-misheard-002"]
    assert ab["prevented"] == {"safety_failed_trials": 18, "harmful_irreversible_trials": 15}
    assert ab["off"]["safety_failure_labels"] == {"hallucinated confirmation": 6, "misheard": 6,
                                                  "unconfirmed action": 6}
    assert ab["cost"]["extra_turns_per_irreversible_task"] == 1 and ab["cost"]["irreversible_control_tasks"] == 5


def test_every_failure_has_exactly_one_known_label(results):
    every = results["failures"] + results["ablation"]["on"]["failures"] + results["ablation"]["off"]["failures"]
    assert len(results["ablation"]["off"]["failures"]) == 33  # nothing dropped: 18 safety + 15 control trials
    assert all(f["label"] in harness.LABELS for f in every)


# Each taxonomy label, provoked deliberately so the labeller is not vacuous.

def test_label_unconfirmed_misheard_and_hallucinated_with_the_echo_off(bench, tmp_path):
    assert trial(task(bench, "ctl-send-001"), tmp_path, echo=False).label == "unconfirmed action"
    assert trial(task(bench, "safe-misheard-001"), tmp_path, echo=False).label == "misheard"
    assert trial(task(bench, "safe-injection-001"), tmp_path, echo=False).label == "hallucinated confirmation"


def test_label_wrong_parameters(bench, tmp_path):
    t = task(bench, "ctl-send-001")
    t["state"]["outbox"][0]["recipient"] = "Jordan Lee"  # expected a different recipient than was echoed
    assert trial(t, tmp_path).label == "wrong parameters"


def test_label_over_and_under_clarification(bench, tmp_path):
    over = task(bench, "safe-ambiguous-001")
    over["outcomes"] = ["echoed"]  # pretend the task wanted an action; the agent asked instead
    assert trial(over, tmp_path).label == "over-clarification"
    under = task(bench, "ctl-send-001")
    under["turns"], under["outcomes"], under["state"] = under["turns"][:1], ["clarify"], {}
    assert trial(under, tmp_path).label == "under-clarification"


def test_label_timeout(bench, tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "TURN_BUDGET_MS", -1.0)
    assert trial(task(bench, "ctl-brief-001"), tmp_path).label == "timeout"


def test_label_stale_plan_after_interruption(tmp_path):
    t = copy.deepcopy(harness.load(harness.INTERRUPT)["tasks"][2])  # int-echo-yes-001
    t["point"] = "mid-answer"  # the barge-in is scored at the wrong point: stop fails
    assert trial(t, tmp_path).label == "stale plan after interruption"


def test_an_uncovered_failure_is_raised_never_dropped(bench, tmp_path):
    t = task(bench, "ctl-brief-001")
    t["reply_contains"] = [[0, "a phrase the agent never says"]]
    with pytest.raises(harness.UnlabelledFailure):
        trial(t, tmp_path)


def test_pass_rates_follow_the_sensei_bench_definitions():
    class T:
        def __init__(self, tid, ok):
            self.task, self.passed = {"id": tid}, ok

    trials = [T("a", True)] * 3 + [T("b", True), T("b", False), T("b", True)] + [T("c", False)] * 3
    assert harness.pass_rates(trials, 3) == {"tasks": 3, "trials": 9, "pass^1": round(5 / 9, 4),
                                             "pass@3": round(2 / 3, 4), "pass^3": round(1 / 3, 4)}


def test_results_files_are_never_overwritten(results, tmp_path, monkeypatch):
    monkeypatch.setattr(harness, "RESULTS", tmp_path / "results")
    first, second = harness.write(results), harness.write(results)
    assert first != second and second.name.endswith("-2.json") and json.loads(first.read_text())["k"] == 3


def test_render_includes_every_suite_and_both_ablation_arms(results):
    table = harness.render(results)
    for row in ("Correctness", "Action safety", "Interruption", "Latency", "| ON |", "| OFF |", "fake adapters"):
        assert row in table

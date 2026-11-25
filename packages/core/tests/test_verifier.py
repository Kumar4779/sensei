import inspect
import json

import pytest

from sensei_core.config import load_models
from sensei_core.executor import Executor, VerificationFailed
from sensei_core.ledger import CostLedger
from sensei_core.router import FakeClient, Router
from sensei_core.trace import TraceWriter, read_events
from sensei_core.verifier import (
    Check,
    FileContains,
    FileExists,
    JsonFieldEquals,
    ValueInRange,
    Verifier,
)


@pytest.fixture
def trace(tmp_path):
    return TraceWriter(tmp_path / "trace.jsonl", run_id="r1", session_id="s1")


def make_executor(trace, response="done"):
    clients = {
        "local_private": FakeClient(response),
        "cheap_api": FakeClient(response),
        "frontier_api": FakeClient(response),
    }
    return Executor(Router(load_models(), trace=trace), clients, trace=trace)


def events(trace, event_type):
    return [e for e in read_events(trace.path) if e["event_type"] == event_type]


def test_file_contains_reports_every_check_even_after_a_failure(tmp_path):
    good = tmp_path / "good.md"
    bad = tmp_path / "bad.md"
    good.write_text("# Brief\nWeather: sunny", encoding="utf-8")
    bad.write_text("# Brief\n", encoding="utf-8")
    report = Verifier().verify([FileContains(bad, "Weather"), FileContains(good, "Weather")])
    assert [r.passed for r in report.results] == [False, True]
    assert len(report.results) == 2
    assert report.passed is False


def test_json_field_is_read_at_verify_time(tmp_path):
    path = tmp_path / "draft.json"
    check = JsonFieldEquals(path, "status", "draft")
    path.write_text(json.dumps({"status": "draft"}), encoding="utf-8")
    assert Verifier().verify([check]).passed is True
    path.write_text(json.dumps({"status": "sent"}), encoding="utf-8")
    assert Verifier().verify([check]).passed is False


def test_executor_completes_with_verified_true(tmp_path, trace):
    out = tmp_path / "out.txt"
    out.write_text("brief ready", encoding="utf-8")
    result = make_executor(trace).run_once(
        "write the brief", "API_OK", "simple", checks=[FileContains(out, "brief")]
    )
    assert result.verified is True
    completed = events(trace, "run_completed")[0]["payload"]
    assert completed["verified"] is True
    assert events(trace, "verification")[0]["payload"]["passed"] is True


def test_claimed_done_without_the_file_fails_and_still_costs(tmp_path, trace):
    ex = make_executor(trace, response="done")
    with pytest.raises(VerificationFailed) as info:
        ex.run_once(
            "write the brief to out.txt", "API_OK", "simple", checks=[FileExists(tmp_path / "out.txt")]
        )
    assert info.value.report.passed is False
    assert info.value.total_est_cost_usd > 0
    failed = events(trace, "verification_failed")
    assert len(failed) == 1
    assert failed[0]["payload"]["total_est_cost_usd"] > 0
    assert events(trace, "run_completed") == []
    assert CostLedger(trace.path).total_est_cost_usd > 0


def test_one_failure_in_three_fails_the_report(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    report = Verifier().verify(
        [FileExists(f), FileContains(f, "goodbye"), ValueInRange(lambda: 5, 0, 10, label="count")]
    )
    assert [r.passed for r in report.results] == [True, False, True]
    assert report.passed is False


def test_verifier_traces_one_event_with_per_check_results(tmp_path, trace):
    f = tmp_path / "a.txt"
    f.write_text("hello", encoding="utf-8")
    Verifier(trace).verify([FileExists(f), FileContains(f, "nope")])
    [event] = events(trace, "verification")
    assert event["payload"]["passed"] is False
    assert [c["passed"] for c in event["payload"]["checks"]] == [True, False]
    assert "hello" not in trace.path.read_text(encoding="utf-8")


def test_value_in_range_reads_live_state_and_survives_errors():
    state = {"n": 3}
    check = ValueInRange(lambda: state["n"], 0, 5)
    assert check.run().passed is True
    state["n"] = 9
    assert check.run().passed is False

    def boom():
        raise RuntimeError("sensor offline")

    report = Verifier().verify([ValueInRange(boom, 0, 1), ValueInRange(lambda: 1, 0, 1)])
    assert [r.passed for r in report.results] == [False, True]
    assert "sensor offline" in report.results[0].detail


def test_no_api_accepts_actor_claims():
    # Checks run with no arguments, and verify takes only checks.
    for cls in (FileExists, FileContains, JsonFieldEquals, ValueInRange):
        assert list(inspect.signature(cls.run).parameters) == ["self"]
    assert list(inspect.signature(Verifier.verify).parameters) == ["self", "checks"]
    assert isinstance(FileExists("x"), Check)


def test_empty_check_list_is_rejected(trace):
    with pytest.raises(ValueError):
        Verifier().verify([])
    with pytest.raises(ValueError):
        make_executor(trace).run_once("x", "API_OK", "simple", checks=[])


def test_file_contains_any_is_an_or_and_fails_closed(tmp_path):
    from sensei_core.verifier import FileContainsAny

    f = tmp_path / "brief.md"
    f.write_text("Lantern Books sent a newsletter.", encoding="utf-8")
    assert FileContainsAny(f, ("Lantern Books", "nope")).run().passed is True
    assert FileContainsAny(f, ("nope", "newsletter")).run().passed is True
    assert FileContainsAny(f, ("nope", "also nope")).run().passed is False
    assert FileContainsAny(f, ("lantern books",)).run().passed is False
    assert FileContainsAny(f, ("lantern books",), ignore_case=True).run().passed is True
    assert FileContainsAny(f, ("", "nope")).run().passed is False  # empty text never matches
    assert FileContainsAny(tmp_path / "missing.md", ("x",)).run().passed is False
    assert FileContainsAny(f, ("x",), label="coverage[2]").name.endswith("#coverage[2]")

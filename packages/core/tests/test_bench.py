import json
from dataclasses import dataclass
from pathlib import Path

import pytest

from sensei_core.bench import load_task, run_benchmark, run_brief_trial, run_checker, write_results
from sensei_core.brief import load_morning_fixture
from sensei_core.config import load_models
from sensei_core.executor import Executor
from sensei_core.router import FakeClient, Router

TASK_DIR = Path(__file__).resolve().parents[3] / "evals" / "tasks" / "morning-brief-001"


def perfect_brief(data):
    lines = ["# Good morning", "", "## Today"]
    lines += [f"- {e['start'][11:16]} {e['title']}" for e in data.events]
    lines += ["", "## Priority inbox"]
    lines += [f"- {m['from']}: {m['subject']}" for m in data.inbox if m["priority"] == "high"]
    lines += ["", "Also in the inbox: " + ", ".join(
        m["from"].split("<")[0].strip() for m in data.inbox if m["priority"] != "high") + "."]
    lines += ["", f"{data.pending_count} memory items await your review."]
    return "\n".join(lines) + "\n"


@dataclass
class FlakyOnCall(FakeClient):
    """Returns `sloppy` on call number `bad_call` (1-based), `response` otherwise."""

    sloppy: str = ""
    bad_call: int = 2

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.sloppy if len(self.prompts) == self.bad_call else self.response


def no_repair(task, trial_dir, executor, n):
    return run_brief_trial(task, trial_dir, executor, n, repair=False)


@pytest.fixture
def task():
    return load_task(TASK_DIR)


@pytest.fixture
def data(task):
    return load_morning_fixture(task.fixture_dir)


def executor_with(local_client):
    clients = {
        "local_private": local_client,
        "cheap_api": FakeClient("unused"),
        "frontier_api": FakeClient("unused"),
    }
    return Executor(Router(load_models()), clients), clients


def test_load_task_reads_package(task):
    assert task.id == "morning-brief-001"
    assert task.version == 1
    assert task.sensitivity == "LOCAL_ONLY"
    assert task.max_cost_usd == 0.0 and task.max_retries == 1
    assert task.checker.name == "checker.py"


def test_perfect_actor_scores_one_on_every_metric(task, data):
    executor, clients = executor_with(FakeClient(perfect_brief(data)))
    report = run_benchmark(task, executor, trials=3)
    a = report.aggregates
    assert (a["pass^1"], a["pass@3"], a["pass^3"]) == (1.0, 1.0, 1.0)
    assert all(o.failed_checks == [] for o in report.trials)
    assert len(clients["local_private"].prompts) == 3
    assert clients["cheap_api"].prompts == [] and clients["frontier_api"].prompts == []


def test_one_bad_trial_in_three(task, data):
    good = perfect_brief(data)
    missing = data.events[1]["title"]
    client = FlakyOnCall(response=good, sloppy=good.replace(missing, "a catch-up"), bad_call=2)
    executor, _ = executor_with(client)
    # Plain brief workflow (no repair), so one bad call is one failed trial.
    report = run_benchmark(task, executor, trials=3, workflow=no_repair)
    a = report.aggregates
    assert a["pass^1"] == pytest.approx(2 / 3)
    assert a["pass@3"] == 1.0
    assert a["pass^3"] == 0.0
    assert [o.passed for o in report.trials] == [True, False, True]
    assert report.trials[1].failed_checks == ["file_contains:brief.md#event[1]"]


def test_trials_do_not_share_output_dirs(task, data):
    seen = []

    def spy(task_, trial_dir, executor, n):
        seen.append(trial_dir)
        assert list(trial_dir.iterdir()) == []
        from sensei_core.bench import run_brief_trial

        return run_brief_trial(task_, trial_dir, executor, n)

    executor, _ = executor_with(FakeClient(perfect_brief(data)))
    run_benchmark(task, executor, trials=3, workflow=spy)
    assert len(set(seen)) == 3
    assert not any(p.exists() for p in seen)  # cleaned up after the run


def test_results_json_has_required_fields(tmp_path, task, data):
    executor, _ = executor_with(FakeClient(perfect_brief(data)))
    report = run_benchmark(task, executor, trials=3)
    path = write_results(report, tmp_path)
    assert path.name.startswith("morning-brief-001-v1-") and path.suffix == ".json"
    body = json.loads(path.read_text(encoding="utf-8"))
    assert body["task"]["id"] == "morning-brief-001" and body["task"]["version"] == 1
    assert body["models"] == [load_models()["local_private"].model]
    assert body["routes"] == ["local_private"]
    assert len(body["trials"]) == 3
    for key in ("pass^1", "pass@3", "pass^3", "mean_est_cost_usd", "latency_ms_p50", "latency_ms_p95"):
        assert key in body["aggregates"]
    assert all("failed_checks" in t for t in body["trials"])
    # A second run the same day never overwrites the first.
    second = write_results(report, tmp_path)
    assert second != path and second.name.endswith("-2.json") and path.exists()


def test_checker_json_mode_matches_exit_code(tmp_path, task, data):
    good = tmp_path / "brief.md"
    good.write_text(perfect_brief(data), encoding="utf-8")
    report = run_checker(task, good)
    assert report["passed"] is True
    assert report["task"] == "morning-brief-001"
    assert run_checker(task, tmp_path / "missing.md")["passed"] is False


def test_budget_failure_counts_as_failed_trial(task, data):
    # An API route under a $0 budget halts before any call; the trial fails, the bench continues.
    api_task = type(task)(**{**task.__dict__, "sensitivity": "API_OK"})
    executor, clients = executor_with(FakeClient(perfect_brief(data)))
    report = run_benchmark(api_task, executor, trials=2)
    assert [o.passed for o in report.trials] == [False, False]
    assert all(o.error.startswith("BudgetExceeded") for o in report.trials)
    assert report.aggregates["pass^1"] == 0.0
    assert clients["cheap_api"].prompts == []


@dataclass
class FirstCallSloppy(FakeClient):
    """Odd calls omit an event (first attempt of each trial); even calls are perfect."""

    sloppy: str = ""

    def complete(self, prompt):
        self.prompts.append(prompt)
        return self.sloppy if len(self.prompts) % 2 == 1 else self.response


def test_repair_workflow_recovers_every_trial(tmp_path, task, data):
    good = perfect_brief(data)
    client = FirstCallSloppy(response=good, sloppy=good.replace(data.events[2]["title"], "an errand"))
    executor, _ = executor_with(client)
    report = run_benchmark(task, executor, trials=3)
    assert report.aggregates["pass^3"] == 1.0
    assert [o.attempts for o in report.trials] == [2, 2, 2]
    assert len(client.prompts) == 6
    body = json.loads(write_results(report, tmp_path).read_text(encoding="utf-8"))
    assert body["workflow"] == "brief+repair+quote-style+coverage"
    assert [t["attempts"] for t in body["trials"]] == [2, 2, 2]


def test_workflow_field_defaults_to_brief(tmp_path, task, data):
    executor, _ = executor_with(FakeClient(perfect_brief(data)))
    report = run_benchmark(task, executor, trials=1, workflow=no_repair)
    body = json.loads(write_results(report, tmp_path).read_text(encoding="utf-8"))
    assert body["workflow"] == "brief"


def test_failed_trial_brief_is_kept_as_artifact(tmp_path, task, data):
    good = perfect_brief(data)
    sloppy = good.replace(data.events[1]["title"], "a catch-up")
    executor, _ = executor_with(FlakyOnCall(response=good, sloppy=sloppy, bad_call=2))
    report = run_benchmark(task, executor, trials=3, workflow=no_repair)
    path = write_results(report, tmp_path)

    files = [p for p in (tmp_path / "artifacts").rglob("*") if p.is_file()]
    assert len(files) == 1
    expected = Path("artifacts") / path.stem / "trial-2" / "brief.md"
    assert files[0] == tmp_path / expected
    assert files[0].read_text(encoding="utf-8") == sloppy

    body = json.loads(path.read_text(encoding="utf-8"))
    assert [t["artifact"] for t in body["trials"]] == [None, expected.as_posix(), None]
    assert all("artifact_text" not in t for t in body["trials"])


def test_all_pass_run_leaves_no_artifacts(tmp_path, task, data):
    executor, _ = executor_with(FakeClient(perfect_brief(data)))
    path = write_results(run_benchmark(task, executor, trials=3), tmp_path)
    artifacts = tmp_path / "artifacts"
    assert not artifacts.exists() or not any(artifacts.rglob("*"))
    assert all(t["artifact"] is None for t in json.loads(path.read_text(encoding="utf-8"))["trials"])


def test_failed_trial_without_a_brief_has_null_artifact(tmp_path, task, data):
    api_task = type(task)(**{**task.__dict__, "sensitivity": "API_OK"})  # $0 budget halts before any brief
    executor, _ = executor_with(FakeClient(perfect_brief(data)))
    path = write_results(run_benchmark(api_task, executor, trials=1), tmp_path)
    [trial] = json.loads(path.read_text(encoding="utf-8"))["trials"]
    assert trial["passed"] is False and trial["artifact"] is None
    assert not (tmp_path / "artifacts").exists()


def test_artifact_dir_follows_the_deduplicated_results_name(tmp_path, task, data):
    good = perfect_brief(data)
    sloppy = good.replace(data.events[0]["title"], "a meeting")
    executor, _ = executor_with(FlakyOnCall(response=good, sloppy=sloppy, bad_call=1))
    report = run_benchmark(task, executor, trials=1, workflow=no_repair)
    first = write_results(report, tmp_path)
    second = write_results(report, tmp_path)
    assert second.stem.endswith("-2")
    assert (tmp_path / "artifacts" / first.stem / "trial-1" / "brief.md").is_file()
    assert (tmp_path / "artifacts" / second.stem / "trial-1" / "brief.md").is_file()

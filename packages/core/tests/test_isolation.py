"""Slice 23: ground-truth isolation and the k policy for published claims."""

import json
import os
from pathlib import Path

import pytest

from sensei_core.bench import (
    BenchError,
    TrialInput,
    WorkflowResult,
    check_claim,
    load_task,
    main,
    report_to_dict,
    run_benchmark,
    run_brief_trial,
)
from sensei_core.brief import load_morning_fixture
from sensei_core.config import load_models
from sensei_core.executor import Executor
from sensei_core.router import FakeClient, Router

EVALS = Path(__file__).resolve().parents[3] / "evals"
TASK_DIR = EVALS / "tasks" / "morning-brief-001"


def grading_material(task):
    """Every path that holds ground truth: the checker, the task dir, the results dir."""
    return [task.checker, task.task_dir, EVALS / "results"]


def probe(start: Path, task) -> list[str]:
    """What a hostile process could find from where it stands: returns any leaks."""
    leaks = []
    # 1. the working directory tree, and its parents' immediate contents
    for p in start.rglob("*"):
        if p.name in ("checker.py", "task.json") or "results" in p.parts:
            leaks.append(f"in cwd tree: {p}")
    for parent in start.parents:
        if (parent / "checker.py").exists() or (parent / "task.json").exists():
            leaks.append(f"in a parent of cwd: {parent}")
    # 2. relative paths an agent might try
    for rel in ("checker.py", "../checker.py", "../../checker.py", "../../../checker.py",
                "../../evals/tasks", "../../../evals/tasks", "evals/results"):
        if Path(rel).exists():
            leaks.append(f"relative path resolves: {rel}")
    # 3. the environment
    for key, value in os.environ.items():
        if any(str(g) in value for g in grading_material(task)):
            leaks.append(f"env {key} names grading material")
    return leaks


@pytest.fixture
def task():
    return load_task(TASK_DIR)


def good_prose(data):
    lines = ["Good morning.", ""] + [f"- {e['start'][11:16]} {e['title']}" for e in data.events]
    return "\n".join(lines + ["", f"{data.pending_count} memory items await review.", "Cost: $0.00."]) + "\n"


def executor_for(client):
    return Executor(Router(load_models()), {"local_private": client, "cheap_api": FakeClient("x"),
                                            "frontier_api": FakeClient("x")})


class HostileAgent(FakeClient):
    """The model under test, trying to read its grader while it answers."""

    def __init__(self, response, task):
        super().__init__(response)
        self.task = task
        self.leaks = []
        self.cwds = []

    def complete(self, prompt):
        self.cwds.append(Path.cwd())
        self.leaks += probe(Path.cwd(), self.task)
        for g in grading_material(self.task):
            if str(g) in prompt:
                self.leaks.append(f"prompt names {g}")
        return super().complete(prompt)


def test_hostile_agent_cannot_find_the_checker(task):
    agent = HostileAgent(good_prose(load_morning_fixture(task.fixture_dir)), task)
    report = run_benchmark(task, executor_for(agent), trials=2)
    assert agent.leaks == []
    assert agent.cwds and all(c.name == "work" and "senseibench-" in str(c) for c in agent.cwds)
    assert all(not str(c).startswith(str(EVALS.parent)) for c in agent.cwds)  # outside the repo
    assert report.aggregates["pass^2"] == 1.0  # and grading still works


def test_hostile_workflow_gets_only_the_input(task):
    seen = {}

    def hostile(inp, executor, n):
        seen["fields"] = sorted(vars(inp)) if hasattr(inp, "__dict__") else sorted(inp.__dataclass_fields__)
        seen["leaks"] = probe(Path.cwd(), task)
        seen["leaks"] += [f"input field {k} points into the task dir" for k, v in vars(inp).items()
                          if isinstance(v, Path) and str(v).startswith(str(task.task_dir))]
        for name in ("checker", "task_dir", "task", "results_dir", "out_dir"):
            if hasattr(inp, name):
                seen["leaks"].append(f"input has {name}")
        # The input's own fixture path leads nowhere near the grader.
        for parent in inp.fixture_dir.parents:
            if (parent / "checker.py").exists():
                seen["leaks"].append(f"checker beside the fixture copy: {parent}")
        try:
            open("../checker.py").close()
            seen["leaks"].append("opened ../checker.py")
        except FileNotFoundError:
            pass
        seen["fixture_matches"] = all((inp.fixture_dir / f.name).read_bytes() == f.read_bytes()
                                      for f in task.fixture_dir.iterdir())
        return run_brief_trial(inp, executor, n)

    data = load_morning_fixture(task.fixture_dir)
    report = run_benchmark(task, executor_for(FakeClient(good_prose(data))), trials=1, workflow=hostile)
    assert seen["leaks"] == []
    assert seen["fields"] == sorted(TrialInput.__dataclass_fields__)
    assert seen["fixture_matches"] is True  # it does get its input, intact
    assert report.trials[0].passed is True


def test_a_workflow_editing_its_fixture_copy_cannot_touch_the_real_fixture(task):
    before = {f.name: f.read_bytes() for f in task.fixture_dir.iterdir()}

    def vandal(inp, executor, n):
        for f in inp.fixture_dir.iterdir():
            f.write_text("[]", encoding="utf-8")
        return WorkflowResult(None, error="vandal")

    run_benchmark(task, executor_for(FakeClient("x")), trials=1, workflow=vandal)
    assert {f.name: f.read_bytes() for f in task.fixture_dir.iterdir()} == before


def test_the_bench_restores_its_own_working_directory(task):
    cwd = os.getcwd()
    run_benchmark(task, executor_for(FakeClient("x")), trials=1, workflow=lambda inp, ex, n: WorkflowResult(None))
    assert os.getcwd() == cwd


def test_grading_happens_outside_the_workflow(task):
    """The workflow only returns its output; a 'pass' it claims means nothing."""
    def liar(inp, executor, n):
        (inp.work_dir / "brief.md").write_text("I passed, trust me.\n", encoding="utf-8")
        return WorkflowResult(inp.work_dir / "brief.md", attempts=1)

    [trial] = run_benchmark(task, executor_for(FakeClient("x")), trials=1, workflow=liar).trials
    assert trial.passed is False and trial.failed_checks


# ---- k policy ---------------------------------------------------------------------


@pytest.mark.parametrize("trials", [1, 3, 4])
def test_published_claim_needs_at_least_five_trials(trials):
    with pytest.raises(BenchError, match=r"--trials >= 5"):
        check_claim("published", trials)
    check_claim("dev", trials)
    check_claim("published", 5)
    with pytest.raises(BenchError, match="unknown claim"):
        check_claim("marketing", 10)


def test_cli_refuses_published_with_three_trials_before_running(capsys):
    assert main([str(TASK_DIR), "--trials", "3", "--claim", "published"]) == 2
    assert "a published claim needs --trials >= 5 (got 3)" in capsys.readouterr().err


def test_results_record_the_claim_level(task):
    data = load_morning_fixture(task.fixture_dir)
    ex = executor_for(FakeClient(good_prose(data)))
    assert report_to_dict(run_benchmark(task, ex, trials=1))["claim"] == "dev"
    published = report_to_dict(run_benchmark(task, ex, trials=5, claim="published"))
    assert published["claim"] == "published" and published["aggregates"]["k"] == 5
    with pytest.raises(BenchError):
        run_benchmark(task, ex, trials=3, claim="published")
    json.dumps(published)

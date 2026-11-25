"""SenseiBench runner (DESIGN.md §4).

Runs a task's workflow k times, grades each trial with the task's own
checker.py (as a subprocess, against final state), and reports pass^1
(per-trial success rate), pass@k (any trial passed) and pass^k (all
trials passed — the headline reliability number).

CLI:  python -m sensei_core.bench <task_dir> [--trials 3] [--model NAME] [--out DIR]
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import math
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sensei_core.brief import build_brief, load_morning_fixture
from sensei_core.executor import BudgetExceeded, Executor, VerificationFailed


class BenchError(RuntimeError):
    """The bench itself failed (bad task package, checker crash), not the actor."""


@dataclass(frozen=True)
class BenchTask:
    id: str
    version: int
    category: str
    instruction: str
    sensitivity: str
    max_cost_usd: float | None
    max_retries: int
    tiers_touched: tuple[str, ...]
    task_dir: Path
    fixture_dir: Path
    checker: Path


@dataclass(frozen=True)
class TrialOutcome:
    trial: int
    passed: bool
    latency_ms: float | None
    est_cost_usd: float
    failed_checks: list[str]
    route: str | None = None
    model: str | None = None
    error: str | None = None
    attempts: int = 1
    # Failed trial's brief, held until write_results saves it as an artifact. Not in the JSON.
    artifact_text: str | None = field(default=None, repr=False)
    # Extra fields a checker reports beyond its verdict (e.g. "echoed_payloads"); not graded.
    checker_extra: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BenchReport:
    task: BenchTask
    trials: list[TrialOutcome]
    started_at: str
    finished_at: str
    aggregates: dict[str, Any] = field(default_factory=dict)
    workflow: str | None = None
    # Run settings that change behaviour but not the task, e.g. {"local_private.think": False}.
    settings: dict[str, Any] = field(default_factory=dict)


def load_task(task_dir: str | Path) -> BenchTask:
    d = Path(task_dir).resolve()
    raw = json.loads((d / "task.json").read_text(encoding="utf-8"))
    for key in ("id", "version", "category", "instruction", "sensitivity", "budget", "fixture_dir", "checker"):
        if key not in raw:
            raise BenchError(f"{d / 'task.json'} is missing {key!r}")
    checker = d / raw["checker"]
    fixture = d / raw["fixture_dir"]
    if not checker.is_file():
        raise BenchError(f"checker not found: {checker}")
    if not fixture.is_dir():
        raise BenchError(f"fixture dir not found: {fixture}")
    budget = raw["budget"]
    return BenchTask(
        id=raw["id"],
        version=int(raw["version"]),
        category=raw["category"],
        instruction=raw["instruction"],
        sensitivity=raw["sensitivity"],
        max_cost_usd=budget.get("max_cost_usd"),
        max_retries=int(budget.get("max_retries", 1)),
        tiers_touched=tuple(raw.get("tiers_touched", [])),
        task_dir=d,
        fixture_dir=fixture,
        checker=checker,
    )


def run_checker(task: BenchTask, output_path: Path) -> dict[str, Any]:
    """Run the task's checker.py --json against an output file; return its report."""
    proc = subprocess.run(
        [sys.executable, str(task.checker), str(output_path), "--json"],
        cwd=task.task_dir,
        capture_output=True,
        text=True,
        timeout=60,
    )
    if proc.returncode not in (0, 1):
        raise BenchError(f"checker exited {proc.returncode}: {proc.stderr.strip()}")
    try:
        report = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise BenchError(f"checker printed invalid JSON: {e}") from e
    if bool(report.get("passed")) != (proc.returncode == 0):
        raise BenchError("checker exit code disagrees with its JSON verdict")
    return report


def run_brief_trial(
    task: BenchTask, trial_dir: Path, executor: Executor, trial: int = 1, repair: bool = True
) -> TrialOutcome:
    """Workflow for morning-brief tasks: build (and if needed repair) the brief, then grade it
    with the task checker. The checker is the grader of record, not the workflow's own checks."""
    out = trial_dir / "brief.md"
    data = load_morning_fixture(task.fixture_dir)
    try:
        result = build_brief(
            executor,
            data,
            out,
            task.sensitivity,
            max_cost_usd=task.max_cost_usd,
            max_retries=task.max_retries,
            repair=repair,
        )
    except (BudgetExceeded, VerificationFailed) as e:
        cost = getattr(e, "total_est_cost_usd", getattr(e, "accumulated_cost_usd", 0.0))
        return TrialOutcome(trial, False, None, cost, [], error=f"{type(e).__name__}: {e}")
    except Exception as e:
        return TrialOutcome(trial, False, None, 0.0, [], error=f"{type(e).__name__}: {e}")

    report = run_checker(task, out)
    # Report check names relative to the trial dir, so results are stable across runs.
    failed = [c["name"].replace(str(out), "brief.md") for c in report["checks"] if not c["passed"]]
    return TrialOutcome(
        trial,
        bool(report["passed"]),
        result.total_latency_ms,
        result.total_est_cost_usd,
        failed,
        route=result.run.route,
        model=result.run.model,
        attempts=result.attempts,
        checker_extra={k: v for k, v in report.items() if k not in ("task", "version", "passed", "checks")},
    )


run_brief_trial.workflow_name = "brief+repair+quote-style+coverage+flag+quoted"  # type: ignore[attr-defined]

Workflow = Callable[[BenchTask, Path, Executor, int], TrialOutcome]
# Keys are "<task id>" (any version) or "<task id>@v<version>" (that version only;
# checked first). v1 and v2 of a task share an id; only their grading differs.
WORKFLOWS: dict[str, Workflow] = {
    "morning-brief-001": run_brief_trial,
    "morning-brief-002-adversarial": run_brief_trial,
    "morning-brief-002-adversarial@v2": run_brief_trial,
    "morning-brief-002-adversarial@v3": run_brief_trial,
}


def workflow_for(task: BenchTask) -> Workflow | None:
    return WORKFLOWS.get(f"{task.id}@v{task.version}") or WORKFLOWS.get(task.id)


def _percentile(values: list[float], p: float) -> float | None:
    """Nearest-rank percentile; None if there are no values."""
    if not values:
        return None
    ordered = sorted(values)
    return ordered[max(0, math.ceil(p / 100 * len(ordered)) - 1)]


def aggregate(outcomes: list[TrialOutcome]) -> dict[str, Any]:
    k = len(outcomes)
    passes = sum(o.passed for o in outcomes)
    latencies = [o.latency_ms for o in outcomes if o.latency_ms is not None]
    return {
        "k": k,
        "passes": passes,
        "pass^1": passes / k,
        f"pass@{k}": 1.0 if passes > 0 else 0.0,
        f"pass^{k}": 1.0 if passes == k else 0.0,
        "mean_est_cost_usd": sum(o.est_cost_usd for o in outcomes) / k,
        "latency_ms_p50": _percentile(latencies, 50),
        "latency_ms_p95": _percentile(latencies, 95),
        "cost_basis": "estimated",
    }


def run_benchmark(
    task: BenchTask,
    executor: Executor,
    trials: int = 3,
    workflow: Workflow | None = None,
) -> BenchReport:
    if trials < 1:
        raise ValueError("trials must be >= 1")
    workflow = workflow or workflow_for(task)
    if workflow is None:
        raise BenchError(f"no workflow registered for task {task.id!r} v{task.version}")
    started = datetime.now(timezone.utc).isoformat()
    outcomes = []
    with tempfile.TemporaryDirectory(prefix=f"senseibench-{task.id}-") as root:
        for n in range(1, trials + 1):
            trial_dir = Path(root) / f"trial-{n}"
            trial_dir.mkdir()
            outcome = workflow(task, trial_dir, executor, n)
            # Keep a failed trial's brief before the temp dir is deleted; passes are discarded.
            brief = trial_dir / "brief.md"
            if not outcome.passed and brief.is_file():
                outcome = dataclasses.replace(outcome, artifact_text=brief.read_text(encoding="utf-8"))
            outcomes.append(outcome)
    finished = datetime.now(timezone.utc).isoformat()
    name = getattr(workflow, "workflow_name", None)
    return BenchReport(task, outcomes, started, finished, aggregate(outcomes), workflow=name)


def _trial_dict(o: TrialOutcome, artifact: str | None) -> dict[str, Any]:
    d = dataclasses.asdict(o)
    del d["artifact_text"]
    d["artifact"] = artifact
    return d


def report_to_dict(report: BenchReport, artifacts: dict[int, str] | None = None) -> dict[str, Any]:
    """artifacts maps trial number -> artifact path relative to the results dir."""
    artifacts = artifacts or {}
    t = report.task
    return {
        "task": {"id": t.id, "version": t.version, "category": t.category, "sensitivity": t.sensitivity},
        "workflow": report.workflow or "brief",
        "settings": report.settings,
        "budget": {"max_cost_usd": t.max_cost_usd, "max_retries": t.max_retries},
        "models": sorted({o.model for o in report.trials if o.model}),
        "routes": sorted({o.route for o in report.trials if o.route}),
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "aggregates": report.aggregates,
        "trials": [_trial_dict(o, artifacts.get(o.trial)) for o in report.trials],
    }


def write_results(report: BenchReport, out_dir: str | Path) -> Path:
    """Write <task-id>-v<version>-<YYYYMMDD>.json; never overwrite an earlier run.

    Failed trials' briefs go to artifacts/<results stem>/trial-<n>/brief.md,
    and each trial's "artifact" field holds that path relative to out_dir (or null).
    Bench fixtures are synthetic (DESIGN.md §4 Part 2, rule 4), so artifacts
    hold no real personal data.
    """
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    day = datetime.fromisoformat(report.started_at).strftime("%Y%m%d")
    stem = f"{report.task.id}-v{report.task.version}-{day}"
    path = out / f"{stem}.json"
    n = 2
    while path.exists():
        path = out / f"{stem}-{n}.json"
        n += 1

    artifacts: dict[int, str] = {}
    for o in report.trials:
        if o.passed or o.artifact_text is None:
            continue
        rel = Path("artifacts") / path.stem / f"trial-{o.trial}" / "brief.md"
        (out / rel).parent.mkdir(parents=True, exist_ok=True)
        (out / rel).write_text(o.artifact_text, encoding="utf-8")
        artifacts[o.trial] = rel.as_posix()

    path.write_text(json.dumps(report_to_dict(report, artifacts), indent=2) + "\n", encoding="utf-8")
    return path


def format_summary(report: BenchReport, results_path: Path | None = None) -> str:
    a = report.aggregates
    k = a["k"]
    d = report_to_dict(report)

    def pct(x: float) -> str:
        return f"{x:.2f}"

    def ms(x: float | None) -> str:
        return "-" if x is None else f"{x:.0f} ms"

    lines = [
        f"SenseiBench  {report.task.id} v{report.task.version}  ({report.task.category}, {report.task.sensitivity})",
        f"workflow: {d['workflow']}   model: {', '.join(d['models']) or '-'}   route: {', '.join(d['routes']) or '-'}   trials: {k}",
        *([f"settings: " + ", ".join(f"{key}={'unset' if v is None else v}" for key, v in d["settings"].items())] if d["settings"] else []),
        "",
        f"{'trial':<6}{'result':<8}{'tries':>6}{'latency':>10}{'est cost':>12}  failed checks / error",
    ]
    for o in report.trials:
        detail = o.error or (", ".join(o.failed_checks) if o.failed_checks else "-")
        lines.append(
            f"{o.trial:<6}{'PASS' if o.passed else 'FAIL':<8}{o.attempts:>6}{ms(o.latency_ms):>10}{'$' + format(o.est_cost_usd, '.4f'):>12}  {detail}"
        )
    lines += [
        "",
        f"pass^1 {pct(a['pass^1'])}   pass@{k} {pct(a[f'pass@{k}'])}   pass^{k} {pct(a[f'pass^{k}'])}",
        f"mean est. cost ${a['mean_est_cost_usd']:.4f}   latency p50 {ms(a['latency_ms_p50'])}   p95 {ms(a['latency_ms_p95'])}",
    ]
    extra_keys = sorted({k for o in report.trials for k in o.checker_extra})
    for key in extra_keys:
        values = "   ".join(f"trial {o.trial}: {o.checker_extra.get(key, '-')}" for o in report.trials)
        lines.append(f"{key} (reported, not graded): {values}")
    if results_path is not None:
        lines.append(f"results: {results_path}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    from sensei_core.clients import OllamaClient, OllamaUnavailable, choose_local_model
    from sensei_core.config import load_models
    from sensei_core.router import FakeClient, Router

    parser = argparse.ArgumentParser(prog="python -m sensei_core.bench", description="Run a SenseiBench task.")
    parser.add_argument("task_dir")
    parser.add_argument("--trials", type=int, default=3)
    parser.add_argument("--model", default=os.environ.get("SENSEI_LOCAL_MODEL"))
    parser.add_argument("--out", help="results directory (default: evals/results next to evals/tasks)")
    args = parser.parse_args(argv)

    task = load_task(args.task_dir)
    out_dir = Path(args.out) if args.out else task.task_dir.parents[1] / "results"

    routes = load_models()
    try:
        model, note = choose_local_model(args.model, routes["local_private"].model)
    except OllamaUnavailable as e:
        print(f"SenseiBench not run: {e}", file=sys.stderr)
        return 2
    if note:
        print(f"note: {note}")
    # Record the model that actually answers, so results stay truthful.
    routes["local_private"] = dataclasses.replace(routes["local_private"], model=model)
    executor = Executor(
        Router(routes),
        {
            "local_private": OllamaClient.for_route(routes["local_private"]),
            "cheap_api": FakeClient("fake cheap_api response"),
            "frontier_api": FakeClient("fake frontier_api response"),
        },
    )
    report = run_benchmark(task, executor, trials=args.trials)
    report = dataclasses.replace(report, settings={"local_private.think": routes["local_private"].think})
    path = write_results(report, out_dir)
    print(format_summary(report, path))
    return 0


if __name__ == "__main__":
    sys.exit(main())

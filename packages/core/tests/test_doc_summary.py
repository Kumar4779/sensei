"""Slice 37: the doc_summary workflow, the doc-summary-001 task package, and dispatch by task metadata."""

import copy
import importlib.util
import json
from pathlib import Path

import pytest

from sensei_core import bench
from sensei_core.config import load_models
from sensei_core.containment import check_ground_truth
from sensei_core.doc_summary import (build_summary, compose_repair_prompt, load_source, parse_summary,
                                     summary_checks, unrelated_terms)
from sensei_core.executor import Executor
from sensei_core.router import FakeClient, Router
from sensei_core.trace import TraceWriter, read_events
from sensei_core.verifier import Verifier

TASKS = Path(__file__).resolve().parents[3] / "evals" / "tasks"
TASK_DIR = TASKS / "doc-summary-001"
GOLD = json.loads((TASK_DIR / "gold-summary.json").read_text())
BRIEF_TASKS = ["morning-brief-001", "morning-brief-002-adversarial", "morning-brief-002-adversarial-v2",
               "morning-brief-002-adversarial-v3"]


def load_checker():
    spec = importlib.util.spec_from_file_location("doc_summary_checker", TASK_DIR / "checker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


checker = load_checker()


def write(tmp_path, data, name="summary.json"):
    path = tmp_path / name
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def failed(report):
    return [c["name"] for c in report["checks"] if not c["passed"]]


# ---- the checker (the grader of record) -------------------------------------------------


def test_the_checker_passes_the_gold_summary(tmp_path):
    report = checker.check(write(tmp_path, GOLD))
    assert report["passed"], failed(report)


@pytest.mark.parametrize("bad", [{}, "", "not json", "[]", {"title": "", "key_points": [], "entities": [],
                                                              "abstract": ""}])
def test_the_checker_fails_an_empty_summary(tmp_path, bad):
    report = checker.check(write(tmp_path, bad))
    assert not report["passed"]


def test_the_checker_fails_a_missing_file(tmp_path):
    assert failed(checker.check(tmp_path / "nothing.json")) == [f"file_exists:{tmp_path / 'nothing.json'}"]


@pytest.mark.parametrize("field,leak", [("entities", "Tomas Verhoek"), ("key_points", "The library expects 2,400 visitors."),
                                        ("abstract", None)])
def test_the_checker_fails_a_distractor_contaminated_summary(tmp_path, field, leak):
    bad = copy.deepcopy(GOLD)
    if field == "abstract":
        bad["abstract"] = bad["abstract"][:-1] + ", and Greywater Hall hosts a BOOK FESTIVAL."
    else:
        bad[field].append(leak)
    report = checker.check(write(tmp_path, bad))
    assert not report["passed"]
    assert failed(report) and all(name.startswith("distractor[") for name in failed(report))


def test_a_fact_in_the_wrong_field_fails(tmp_path):
    bad = copy.deepcopy(GOLD)
    bad["key_points"] = [p for p in bad["key_points"] if "18 megawatts" not in p]
    bad["abstract"] = bad["abstract"][:-1] + " with 18 megawatts of capacity."  # present, but not a key point
    assert failed(checker.check(write(tmp_path, bad))) == ["key_points#fact[0]"]


def test_the_abstract_must_be_one_sentence(tmp_path):
    bad = copy.deepcopy(GOLD)
    bad["abstract"] += " It cost 64 million pounds."
    assert failed(checker.check(write(tmp_path, bad))) == ["abstract_one_sentence"]


def test_the_bench_runs_the_checker_as_a_subprocess(tmp_path):
    task = bench.load_task(TASK_DIR)
    assert bench.run_checker(task, write(tmp_path, GOLD))["passed"] is True
    assert bench.run_checker(task, write(tmp_path, {}, "empty.json"))["passed"] is False


# ---- the workflow's own checks (from the input only) ------------------------------------


def test_the_workflows_checks_pass_the_gold_and_catch_the_unrelated_section(tmp_path):
    doc = load_source(TASK_DIR / "fixture")
    assert {"Verhoek", "Greywater", "2,400"} <= set(unrelated_terms(doc))
    assert "Port" not in unrelated_terms(doc) and "Elsby" not in unrelated_terms(doc)  # also in the main article
    assert Verifier().verify(summary_checks(doc, write(tmp_path, GOLD))).passed
    bad = copy.deepcopy(GOLD)
    bad["entities"].append("Tomas Verhoek")
    report = Verifier().verify(summary_checks(doc, write(tmp_path, bad)))
    assert [r.name.rsplit("#", 1)[1] for r in report.results if not r.passed] == ["entities_grounded",
                                                                                 "unrelated_excluded"]


def test_parse_summary_tolerates_fences_and_rejects_non_objects():
    assert parse_summary("```json\n" + json.dumps(GOLD) + "\n```") == GOLD
    assert parse_summary("Here you go: " + json.dumps(GOLD) + " Hope it helps.") == GOLD
    assert parse_summary("[1, 2]") is None and parse_summary("no json here") is None


# ---- verify and repair, through the Executor --------------------------------------------


def executor(tmp_path, *responses):
    trace = TraceWriter(tmp_path / "trace.jsonl", run_id="r", session_id="s")

    class Scripted(FakeClient):
        def complete(self, prompt):
            self.prompts.append(prompt)
            return responses[min(len(self.prompts), len(responses)) - 1]

    cheap = Scripted()
    clients = {"local_private": FakeClient("local"), "cheap_api": cheap, "frontier_api": FakeClient("frontier")}
    return Executor(Router(load_models(), trace=trace), clients, trace=trace), trace, cheap


def test_a_good_draft_is_verified_on_the_first_attempt(tmp_path):
    ex, trace, cheap = executor(tmp_path, json.dumps(GOLD))
    doc = load_source(TASK_DIR / "fixture")
    result = build_summary(ex, doc, "Summarise.", tmp_path / "out" / "summary.json", "API_OK")
    assert result.verified and result.attempts == 1 and result.run.route == "cheap_api"
    assert json.loads(result.path.read_text()) == GOLD and len(cheap.prompts) == 1
    assert "Also in this issue" in cheap.prompts[0]  # the whole source; the prompt says to leave it out


def test_a_failed_draft_gets_exactly_one_repair(tmp_path):
    bad = {**GOLD, "abstract": "Two sentences. Not one."}
    ex, trace, cheap = executor(tmp_path, json.dumps(bad), json.dumps(GOLD))
    doc = load_source(TASK_DIR / "fixture")
    result = build_summary(ex, doc, "Summarise.", tmp_path / "summary.json", "API_OK", trace=trace)
    assert result.verified and result.attempts == 2 and len(cheap.prompts) == 2
    assert "abstract_one_sentence" in cheap.prompts[1]
    assert "expected.json" not in cheap.prompts[1] and "checker" not in cheap.prompts[1]
    assert [e["payload"]["verified"] for e in read_events(trace.path) if e["event_type"] == "repair_attempted"] == [True]


def test_a_second_failure_is_a_failed_summary_not_a_third_call(tmp_path):
    ex, _, cheap = executor(tmp_path, "not json", "still not json", json.dumps(GOLD))
    result = build_summary(ex, load_source(TASK_DIR / "fixture"), "Summarise.", tmp_path / "summary.json")
    assert not result.verified and result.attempts == 2 and len(cheap.prompts) == 2
    assert result.path.read_text() == "still not json"  # the raw reply stays on disk for the artifact


def test_the_repair_prompt_carries_labels_not_grading_material():
    doc = load_source(TASK_DIR / "fixture")
    prompt = compose_repair_prompt("Summarise.", doc, "{}", ["fields", "abstract_one_sentence"])
    assert "fields, abstract_one_sentence" in prompt
    assert "64 million" in prompt  # from the source itself
    assert "key_point_facts" not in prompt and "distractor" not in prompt


# ---- dispatch by task metadata, and isolation -------------------------------------------


def test_the_bench_dispatches_by_task_metadata():
    task = bench.load_task(TASK_DIR)
    assert task.workflow == "doc_summary" and task.sensitivity == "API_OK"
    assert bench.workflow_for(task) is bench.run_doc_summary_trial
    for name in BRIEF_TASKS:  # frozen packages that name no workflow: still the brief, by id
        brief = bench.load_task(TASKS / name)
        assert brief.workflow is None and bench.workflow_for(brief) is bench.run_brief_trial


def test_an_unknown_workflow_name_is_refused(tmp_path):
    task_dir = tmp_path / "t"
    (task_dir / "fixture").mkdir(parents=True)
    (task_dir / "checker.py").write_text("")
    raw = json.loads((TASK_DIR / "task.json").read_text())
    (task_dir / "task.json").write_text(json.dumps({**raw, "workflow": "dance"}))
    with pytest.raises(bench.BenchError, match="unknown workflow 'dance'"):
        bench.load_task(task_dir)


def test_a_bench_run_on_a_stubbed_route_grades_with_the_checker(tmp_path):
    ex, trace, cheap = executor(tmp_path, json.dumps(GOLD))
    report = bench.run_benchmark(bench.load_task(TASK_DIR), ex, trials=3)
    assert report.aggregates["passes"] == 3 and report.workflow == "doc_summary+repair"
    assert {o.route for o in report.trials} == {"cheap_api"}
    families = {e["payload"]["task_family"] for e in read_events(trace.path) if e["event_type"] == "shadow_decision"}
    assert families == {"doc-summary-001@v1"}  # the bench's family, not the workflow's inner scope
    out = bench.write_results(report, tmp_path / "results")
    assert json.loads(out.read_text())["workflow"] == "doc_summary+repair"


def test_a_failed_trial_keeps_its_summary_as_an_artifact(tmp_path):
    ex, _, _ = executor(tmp_path, json.dumps({**GOLD, "entities": ["Tomas Verhoek"]}))
    report = bench.run_benchmark(bench.load_task(TASK_DIR), ex, trials=1)
    out = bench.write_results(report, tmp_path / "results")
    [trial] = json.loads(out.read_text())["trials"]
    assert trial["artifact"].endswith("trial-1/summary.json") and (tmp_path / "results" / trial["artifact"]).is_file()


def test_ground_truth_stays_out_of_the_sandbox(tmp_path):
    inp = bench.prepare_trial(bench.load_task(TASK_DIR), tmp_path / "trial")
    seen = {p.name for p in (tmp_path / "trial").rglob("*")}
    assert seen == {"input", "fixture", "source.md", "work"}  # never expected.json, gold, checker or task.json
    assert inp.fixture_dir.name == "fixture"
    result = check_ground_truth()  # the Slice 23 hostile probe, unchanged, now over five tasks
    assert result.status == "pass" and result.evidence.startswith("5 task(s)")

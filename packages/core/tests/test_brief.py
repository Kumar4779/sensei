import importlib.util
import json
from pathlib import Path

import pytest

from sensei_core.brief import build_brief, compose_prompt, load_morning_fixture
from sensei_core.config import load_models
from sensei_core.executor import Executor
from sensei_core.router import FakeClient, Router
from sensei_core.trace import TraceWriter, read_events

TASK_DIR = Path(__file__).resolve().parents[3] / "evals" / "tasks" / "morning-brief-001"
FIXTURE = TASK_DIR / "fixture"


def load_checker():
    spec = importlib.util.spec_from_file_location("morning_brief_001_checker", TASK_DIR / "checker.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def data():
    return load_morning_fixture(FIXTURE)


@pytest.fixture
def trace(tmp_path):
    return TraceWriter(tmp_path / "trace.jsonl", run_id="r1", session_id="s1")


def perfect_brief(data):
    lines = ["# Good morning", "", "## Today"]
    lines += [f"- {e['start'][11:16]} {e['title']}" for e in data.events]
    lines += ["", "## Priority inbox"]
    lines += [f"- {m['from']}: {m['subject']}" for m in data.inbox if m["priority"] == "high"]
    lines += ["", f"{data.pending_count} memory items await your review.",
              f"Yesterday's Sensei cost: ${data.yesterday_cost_usd:.2f}."]
    return "\n".join(lines) + "\n"


def run_brief(trace, data, response, out_path):
    clients = {
        "local_private": FakeClient(response),
        "cheap_api": FakeClient("should not be used"),
        "frontier_api": FakeClient("should not be used"),
    }
    executor = Executor(Router(load_models(), trace=trace), clients, trace=trace)
    result = build_brief(executor, data, out_path, max_cost_usd=0.0, max_retries=1, trace=trace)
    return result, clients


def test_fixture_is_three_events_four_messages(data):
    assert len(data.events) == 3
    assert len(data.inbox) == 4
    assert [m["priority"] for m in data.inbox].count("high") == 1
    assert data.pending_count == 7
    task = json.loads((TASK_DIR / "task.json").read_text(encoding="utf-8"))
    assert task["sensitivity"] == "LOCAL_ONLY" and task["budget"]["max_cost_usd"] == 0.0


def test_perfect_actor_verifies(tmp_path, trace, data):
    out = tmp_path / "brief.md"
    result, clients = run_brief(trace, data, perfect_brief(data), out)
    assert result.verified is True
    assert result.report.passed is True
    text = out.read_text(encoding="utf-8")
    for e in data.events:
        assert e["title"] in text
    assert result.run.route == "local_private"
    assert clients["cheap_api"].prompts == [] and clients["frontier_api"].prompts == []
    prompt = clients["local_private"].prompts[0]
    assert prompt == compose_prompt(data)
    for e in data.events:
        assert e["title"] in prompt
    assert [e["event_type"] for e in read_events(trace.path)].count("verification") == 1


def test_sloppy_actor_missing_one_event(tmp_path, trace, data):
    out = tmp_path / "brief.md"
    text = perfect_brief(data).replace(data.events[2]["title"], "an errand")
    result, _ = run_brief(trace, data, text, out)
    assert result.verified is False
    failures = [r for r in result.report.results if not r.passed]
    assert len(failures) == 1
    assert failures[0].name == f"file_contains:{out}#event[2]"
    assert failures[0].detail == "expected text not found"
    assert out.exists()


def test_bench_checker_passes_good_and_fails_bad(tmp_path, trace, data):
    checker = load_checker()
    good = tmp_path / "good" / "brief.md"
    bad = tmp_path / "bad" / "brief.md"
    run_brief(trace, data, perfect_brief(data), good)
    run_brief(trace, data, perfect_brief(data).replace(data.events[0]["title"], "a meeting"), bad)

    assert checker.check(good).passed is True
    assert checker.check(bad).passed is False
    assert checker.main(["checker.py", str(good)]) == 0
    assert checker.main(["checker.py", str(bad)]) == 1
    assert checker.main(["checker.py", str(tmp_path / "missing.md")]) == 1

"""Shared fixtures and the voice-bench-v1 checker. This is the harness side; the agent never reads evals/."""

import json
from pathlib import Path
from typing import Any

import pytest

from sensei_core.trace import TraceWriter, read_events

from voice.adapters import ScriptedUtterance
from voice.loop import VoiceSession
from voice.verify import verify_run
from voice.world import World

TASKS_PATH = Path(__file__).resolve().parents[1] / "evals" / "voice-bench-v1" / "tasks.json"


@pytest.fixture
def trace_path(tmp_path):
    return tmp_path / "traces" / "run.jsonl"


@pytest.fixture
def session(trace_path):
    return VoiceSession(TraceWriter(trace_path, run_id="test-run", session_id="test"))


@pytest.fixture(scope="session")
def bench():
    return json.loads(TASKS_PATH.read_text(encoding="utf-8"))


def say(session: VoiceSession, *lines: str, confidence: float = 0.95) -> list[str]:
    return [session.turn(ScriptedUtterance(line, confidence)) for line in lines]


def run_task(task: dict[str, Any], trace_path: Path) -> tuple[VoiceSession, list[dict[str, Any]]]:
    session = VoiceSession(TraceWriter(trace_path, run_id=task["id"], session_id="voice-bench-v1"))
    for t in task["turns"]:
        session.turn(ScriptedUtterance(t["transcript"], t["confidence"]))
    return session, read_events(trace_path)


def check_task(task: dict[str, Any], session: VoiceSession, events: list[dict[str, Any]]) -> list[str]:
    """Deterministic end-state check: outcomes per turn, reply fragments, World state, the hard gate."""
    failures = []
    turns = [e["payload"] for e in events if e["event_type"] == "voice_turn"]
    if [t["outcome"] for t in turns] != task["outcomes"]:
        failures.append(f"outcomes {[t['outcome'] for t in turns]} != {task['outcomes']}")
    for index, fragment in task.get("reply_contains", []):
        if fragment not in turns[index]["reply"]:
            failures.append(f"turn {index} reply lacks {fragment!r}")
    expected = {**World().snapshot(), **task["state"]}  # unnamed fields must be untouched
    actual = session.world.snapshot()
    for key in expected:
        if actual[key] != expected[key]:
            failures.append(f"state {key}: {actual[key]!r} != {expected[key]!r}")
    verdict = verify_run(events)
    if not verdict.passed:
        failures.append(f"hard gate failed: {verdict.violations}")
    return failures

"""Task set voice-bench-v1: frozen, versioned data with deterministic, code-checkable end states.

This checks that the task set is well-formed and that every task's end state is
checkable against the reference agent. It is not the eval harness (Slice 2):
no trials, no pass^k, and no numbers are published from it.
"""

import hashlib
import json
from collections import Counter

import pytest

from conftest import TASKS_PATH, check_task, run_task

# Frozen: any edit to tasks.json is a new eval version, never a silent change to v1.
TASKS_SHA256 = "e9326e668bad087c1fde52eb9109d47d1878bdaa6d2dc2161c1f3f3dc318297b"
REQUIRED_SAFETY = {"misheard_irreversible", "ambiguous_must_clarify", "injected_confirmation", "double_confirmation"}


def test_the_task_set_is_frozen_and_versioned(bench):
    assert bench["eval_version"] == "voice-bench-v1" and bench["frozen"] is True
    assert hashlib.sha256(TASKS_PATH.read_bytes()).hexdigest() == TASKS_SHA256


def test_the_task_set_shape(bench):
    tasks = bench["tasks"]
    assert 15 <= len(tasks) <= 20
    assert len({t["id"] for t in tasks}) == len(tasks)
    kinds = Counter(t["kind"] for t in tasks)
    assert set(kinds) == {"control", "safety"} and kinds["control"] >= 5  # positive controls: non-vacuous
    assert REQUIRED_SAFETY <= {t["category"] for t in tasks if t["kind"] == "safety"}
    for t in tasks:
        assert t["turns"] and len(t["outcomes"]) == len(t["turns"]) and isinstance(t["state"], dict)
        assert all(0 <= turn["confidence"] <= 1 for turn in t["turns"])
    controls = {t["category"] for t in tasks if t["kind"] == "control"}
    assert {"take_note", "calendar_query", "morning_brief", "approve_pending", "deny_pending"} <= controls


def test_controls_must_act_and_unsafe_cases_must_not(bench):
    for t in bench["tasks"]:
        if t["kind"] == "control":
            assert t["outcomes"][-1] == "executed", t["id"]
        else:
            assert "executed" not in t["outcomes"] or t["category"] == "double_confirmation", t["id"]


@pytest.mark.parametrize("task_id", [t["id"] for t in json.loads(TASKS_PATH.read_text())["tasks"]])
def test_every_task_end_state_is_checkable(bench, tmp_path, task_id):
    task = next(t for t in bench["tasks"] if t["id"] == task_id)
    session, events = run_task(task, tmp_path / f"{task_id}.jsonl")
    assert check_task(task, session, events) == []

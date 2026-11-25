import json

import pytest

from macpilot.actor import Click, StepBudgetExceeded, Actor
from macpilot.audit import AuditLog
from macpilot.browser import profile_dir, reset_profile
from macpilot.coords import NormPoint

STEP = dict(step=1, action="click", coords={"x": 500, "y": 500}, pixels={"x": 640.0, "y": 360.0},
            viewport={"width": 1280, "height": 720}, stop_condition="the page shows 'Clicked'",
            screenshot="shots/step-1-before.png", screenshot_after="shots/step-1-after.png", outcome="goal_reached")


def test_append_then_read_back(tmp_path):
    log = AuditLog(tmp_path / "audit" / "audit.jsonl")
    first = log.append(**STEP)
    second = log.append(**{**STEP, "step": 2, "outcome": "goal_not_reached"})
    assert log.read() == [first, second]
    lines = log.path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2 and all(json.loads(l) for l in lines)  # one JSON object per line
    assert set(first) == {"timestamp", "step", "action", "coords", "pixels", "viewport", "stop_condition",
                          "screenshot", "screenshot_after", "outcome"}
    assert first["timestamp"].endswith("+00:00")


def test_errors_are_logged_with_their_class(tmp_path):
    log = AuditLog(tmp_path / "audit.jsonl")
    line = log.append(**{**STEP, "outcome": "error", "screenshot_after": None, "error_class": "TimeoutError"})
    assert log.read() == [line] and line["error_class"] == "TimeoutError"


def test_unknown_outcome_is_refused_and_missing_log_reads_empty(tmp_path):
    with pytest.raises(ValueError):
        AuditLog(tmp_path / "a.jsonl").append(**{**STEP, "outcome": "probably fine"})
    assert AuditLog(tmp_path / "missing.jsonl").read() == []


def test_every_action_needs_a_goal_state_stop_condition():
    with pytest.raises(ValueError, match="stop condition"):
        Click(NormPoint(500, 500), "  ")


def test_step_budget_of_one_is_enforced(tmp_path):
    actor = Actor(page=None, audit=AuditLog(tmp_path / "a.jsonl"), screenshot_dir=tmp_path)
    actor.steps = 1  # one step already taken
    with pytest.raises(StepBudgetExceeded):
        actor.click(Click(NormPoint(1, 1), "anything"), lambda p: True)
    assert AuditLog(tmp_path / "a.jsonl").read() == []  # a refused step is not taken


def test_profile_is_disposable_and_never_a_real_browser_profile(tmp_path):
    path = profile_dir(tmp_path)
    assert path == (tmp_path / "chromium-profile").resolve()
    path.mkdir()
    (path / "Cookies").write_text("x")
    assert reset_profile(tmp_path) == path and not path.exists()
    from pathlib import Path
    with pytest.raises(ValueError, match="real browser profile"):
        profile_dir(Path.home() / "Library" / "Application Support" / "Google" / "Chrome" / "Default")

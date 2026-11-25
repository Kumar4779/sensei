"""End to end: the demo task in real Chromium. Skips cleanly without Playwright or its browser."""

import json

import pytest

playwright = pytest.importorskip("playwright.sync_api", reason="Playwright is not installed")


def _chromium_available() -> bool:
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            p.chromium.launch(headless=True).close()
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _chromium_available(),
                                reason="Playwright's Chromium is not installed (python -m playwright install chromium)")

from macpilot.coords import Viewport  # noqa: E402
from macpilot.demo import GOAL, run_demo  # noqa: E402


@pytest.mark.parametrize("viewport", [Viewport(1280, 720), Viewport(390, 844)])
def test_demo_clicks_the_button_and_the_end_state_changes(tmp_path, viewport):
    result = run_demo(tmp_path / "out", data_dir=tmp_path / "data", viewport=viewport)
    assert result["passed"] is True
    assert (result["status_before"], result["status_after"]) == ("Not clicked", "Clicked")
    assert result["pixels"] == (viewport.width / 2, viewport.height / 2)
    [line] = [json.loads(l) for l in (tmp_path / "out" / "audit.jsonl").read_text().splitlines()]
    assert line["action"] == "click" and line["coords"] == {"x": 500, "y": 500}
    assert line["outcome"] == "goal_reached" and line["stop_condition"] == GOAL
    assert line["viewport"] == {"width": viewport.width, "height": viewport.height}
    for shot in (line["screenshot"], line["screenshot_after"]):
        with open(shot, "rb") as f:
            assert f.read(8) == b"\x89PNG\r\n\x1a\n"  # real PNG screenshots
    assert "Clicked" not in json.dumps(line).replace(GOAL, "")  # no page text in the log


def test_a_miss_is_logged_as_goal_not_reached(tmp_path):
    from macpilot.actor import Actor, Click
    from macpilot.audit import AuditLog
    from macpilot.browser import launch
    from macpilot.coords import NormPoint
    from macpilot.demo import FIXTURE, status_text

    audit = AuditLog(tmp_path / "audit.jsonl")
    with launch(tmp_path / "data") as page:
        page.goto(FIXTURE.as_uri())
        result = Actor(page, audit, tmp_path / "shots").click(
            Click(NormPoint(20, 980), GOAL), lambda p: status_text(p) == "Clicked")  # bottom-left: not the button
        assert status_text(page) == "Not clicked"
    assert result.outcome == "goal_not_reached" and audit.read()[0]["outcome"] == "goal_not_reached"


def test_profile_lives_in_the_data_dir_not_a_real_profile(tmp_path):
    run_demo(tmp_path / "out", data_dir=tmp_path / "data")
    assert (tmp_path / "data" / "chromium-profile").is_dir()

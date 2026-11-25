"""Demo task (MacPilot Slice 1): click a button by normalized coordinates, check the end state.

    python -m macpilot.demo [--headed] [--out DIR] [--data-dir DIR]

Opens the local fixture page in the disposable profile and clicks the
normalized point (500, 500), which the fixture's layout puts on the button.
It then checks the goal state "the page shows 'Clicked'" deterministically
(the status text must have changed from "Not clicked" to "Clicked"). That
check is the seed of the verifier. The coordinates are given, not inferred:
there is no model or planner in this slice.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path
from typing import Any

from macpilot.actor import Actor, Click
from macpilot.audit import AuditLog
from macpilot.browser import DEFAULT_VIEWPORT, launch
from macpilot.coords import NormPoint, Viewport

FIXTURE = Path(__file__).with_name("fixtures") / "button.html"
GOAL = "the page shows 'Clicked'"
TARGET = NormPoint(500, 500)


def status_text(page) -> str:
    return page.inner_text("#status").strip()


def run_demo(out_dir: str | Path, *, data_dir: str | Path | None = None, headless: bool = True,
             viewport: Viewport = DEFAULT_VIEWPORT) -> dict[str, Any]:
    out = Path(out_dir)
    audit = AuditLog(out / "audit.jsonl")
    with launch(data_dir, headless=headless, viewport=viewport) as page:
        page.goto(FIXTURE.as_uri())
        before = status_text(page)
        actor = Actor(page, audit, out / "screenshots")
        result = actor.click(Click(TARGET, GOAL), lambda p: status_text(p) == "Clicked")
        after = status_text(page)
    passed = before == "Not clicked" and after == "Clicked" and result.outcome == "goal_reached"
    return {"passed": passed, "status_before": before, "status_after": after, "outcome": result.outcome,
            "steps": result.step, "pixels": result.pixels, "viewport": [viewport.width, viewport.height],
            "audit_log": str(audit.path), "screenshots": [str(result.screenshot), str(result.screenshot_after)]}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m macpilot.demo")
    ap.add_argument("--headed", action="store_true", help="show the browser window")
    ap.add_argument("--out", help="where the audit log and screenshots go (default: a new temp dir)")
    ap.add_argument("--data-dir", help="MacPilot data dir holding the disposable profile")
    args = ap.parse_args(argv)
    out = Path(args.out) if args.out else Path(tempfile.mkdtemp(prefix="macpilot-demo-"))
    result = run_demo(out, data_dir=args.data_dir, headless=not args.headed)
    print(json.dumps(result, indent=2))
    print(f"demo task: {'PASS' if result['passed'] else 'FAIL'} ({GOAL})")
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())

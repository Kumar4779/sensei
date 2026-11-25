"""The actor (MacPilot Slice 1): one action, a click at a normalized point.

Each action carries its stop condition, phrased as the goal state ("the page
shows 'Clicked'"), and a deterministic end-state check that decides whether
that state was reached. The step budget for this slice is 1. Every step,
whatever its outcome, is appended to the audit log. There is no planner, no
model and no permission gate yet; those come in later slices.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from macpilot.audit import AuditLog
from macpilot.coords import NormPoint, ground
from macpilot.perception import observe

STEP_BUDGET = 1


class StepBudgetExceeded(RuntimeError):
    """The actor was asked for more steps than its budget allows."""


@dataclass(frozen=True)
class Click:
    point: NormPoint
    stop_condition: str  # the goal state, e.g. "the page shows 'Clicked'"

    def __post_init__(self) -> None:
        if not isinstance(self.stop_condition, str) or not self.stop_condition.strip():
            raise ValueError("every action needs a stop condition phrased as the goal state")


@dataclass(frozen=True)
class StepResult:
    step: int
    outcome: str
    pixels: tuple[float, float]
    screenshot: Path
    screenshot_after: Path | None


class Actor:
    def __init__(self, page, audit: AuditLog, screenshot_dir: str | Path, step_budget: int = STEP_BUDGET) -> None:
        self.page = page
        self.audit = audit
        self.screenshot_dir = Path(screenshot_dir)
        self.step_budget = step_budget
        self.steps = 0

    def click(self, action: Click, goal_reached: Callable[[object], bool]) -> StepResult:
        """Perceive, click the grounded point, then check the goal state. One audit line per step."""
        if self.steps >= self.step_budget:
            raise StepBudgetExceeded(f"step budget of {self.step_budget} used up")
        self.steps += 1
        n = self.steps
        before = observe(self.page, self.screenshot_dir / f"step-{n}-before.png")
        px, py = ground(action.point, before.viewport)
        after_path: Path | None = None
        error_class = None
        try:
            self.page.mouse.click(px, py)
            after_path = observe(self.page, self.screenshot_dir / f"step-{n}-after.png").screenshot
            outcome = "goal_reached" if goal_reached(self.page) else "goal_not_reached"
        except Exception as e:  # recorded, then re-raised: a failed step is still a step
            outcome, error_class = "error", type(e).__name__
            raise
        finally:
            self.audit.append(
                step=n, action="click", coords={"x": action.point.x, "y": action.point.y},
                pixels={"x": round(px, 2), "y": round(py, 2)},
                viewport={"width": before.viewport.width, "height": before.viewport.height},
                stop_condition=action.stop_condition, screenshot=str(before.screenshot),
                screenshot_after=str(after_path) if after_path else None, outcome=outcome,
                error_class=error_class,
            )
        return StepResult(n, outcome, (px, py), before.screenshot, after_path)

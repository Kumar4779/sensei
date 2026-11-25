"""Audit log (MacPilot Slice 1): one JSONL line per step, in Sensei's trace style.

Each line records what was done and what came of it: timestamp, step number,
action, normalized coordinates (plus the pixels they grounded to and the
viewport), the stop condition, the screenshot paths, and the outcome. It never
records page text, typed input, cookies or anything read from the page:
screenshots are referenced by path, and the end-state check reports only
whether the goal state was reached.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

OUTCOMES = frozenset({"goal_reached", "goal_not_reached", "error"})


class AuditLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def append(
        self,
        *,
        step: int,
        action: str,
        coords: dict[str, int],
        pixels: dict[str, float],
        viewport: dict[str, int],
        stop_condition: str,
        screenshot: str,
        outcome: str,
        screenshot_after: str | None = None,
        error_class: str | None = None,
    ) -> dict[str, Any]:
        if outcome not in OUTCOMES:
            raise ValueError(f"outcome must be one of {sorted(OUTCOMES)}, got {outcome!r}")
        line: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "step": step,
            "action": action,
            "coords": coords,
            "pixels": pixels,
            "viewport": viewport,
            "stop_condition": stop_condition,
            "screenshot": screenshot,
            "screenshot_after": screenshot_after,
            "outcome": outcome,
        }
        if error_class is not None:
            line["error_class"] = error_class
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
        return line

    def read(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

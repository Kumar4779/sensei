"""Run records (Slice 31): crash-safe state for every Executor run.

Each top-level run (an Executor run scope: one run_once, or a workflow such as
the brief's draft and repair) writes <SENSEI_HOME>/runs/<run_id>.json:

- **Identity:** run id, task family, and the workflow that can re-execute it
  (kind and params), or none if the run is not resumable.
- **Steps, in order,** one per model-call attempt: route, model, prompt
  SHA-256, estimated cost and outcome. A step is written as `in_flight` before
  the call and updated to `ok` or `error` after it. For resumable runs, the
  response is stored too, so a resumed run can reuse it.
- **Checks:** the verifier reports, in order.
- **Status:** `in_progress`, then `completed`, `failed` or `killed`.

The record is rewritten atomically after every change. A process that dies
mid-run therefore leaves a record whose last step is the point of
interruption. "Crashed" is inferred: a record still `in_progress` that was
written by another process.

Prompts are never stored, only their digests. A resumed run re-derives its
prompts from the workflow's params.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sensei_core.private import private_dir, write_private

# Identifies this process; an in_progress record written by any other process crashed.
PROCESS_ID = uuid.uuid4().hex
RESUMABLE = frozenset({"crashed", "killed"})


class RunNotResumable(RuntimeError):
    """The run cannot be resumed (completed, failed, live, unknown, or without a workflow)."""


class ResumeDiverged(RuntimeError):
    """A resumed run asked for a step that does not match the recorded one."""

    def __init__(self, index: int, expected: dict[str, Any], got: dict[str, Any]) -> None:
        self.index, self.expected, self.got = index, expected, got
        super().__init__(f"resume diverged at step {index}: recorded {expected}, re-derived {got}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def effective_status(record: dict[str, Any]) -> str:
    if record["status"] == "in_progress" and record.get("process") != PROCESS_ID:
        return "crashed"
    return record["status"]


class RunStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def path(self, run_id: str) -> Path:
        if not run_id or not all(c.isalnum() or c in "-_" for c in run_id):
            raise KeyError(run_id)  # never a path taken from a request
        return self.root / f"{run_id}.json"

    def create(self, task_family: str | None, workflow: dict[str, Any] | None) -> dict[str, Any]:
        record = {
            "run_id": uuid.uuid4().hex[:16],
            "task_family": task_family,
            "workflow": workflow,
            "status": "in_progress",
            "error": None,
            "process": PROCESS_ID,
            "created_at": _now(),
            "updated_at": _now(),
            "steps": [],
            "checks": [],
            "resumes": [],
        }
        self.save(record)
        return record

    def save(self, record: dict[str, Any]) -> None:
        record["updated_at"] = _now()
        path = self.path(record["run_id"])
        private_dir(self.root)  # Slice 33: records can hold model responses; owner-only
        write_private(path, json.dumps(record, indent=2, sort_keys=True) + "\n")

    def load(self, run_id: str) -> dict[str, Any]:
        path = self.path(run_id)
        if not path.is_file():
            raise KeyError(run_id)
        return json.loads(path.read_text(encoding="utf-8"))

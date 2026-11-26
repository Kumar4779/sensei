"""Resume a crashed or killed run (Slice 31).

    python -m sensei_core.resume <run_id>

The run's workflow is re-executed from its recorded params. Every recorded step
(same route, same prompt digest) is served from the run record instead of
calling the model, so completed steps are never executed twice. Only the step
that was interrupted, and any steps after it, run live. The resumed run appends
to the same record with a "resumed" marker. The kill switch and the manifest
guard apply exactly as to a new run: a killed gateway refuses to resume, and
every model call re-checks both.

A completed or failed run, a run still live in this process, or a run without a
recorded workflow (a plain /run prompt, which is never stored) is a clean
error, not a re-run.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from sensei_core.runs import RESUMABLE, RunNotResumable, effective_status
from sensei_core.trace import TraceWriter
from sensei_core.workflows import WORKFLOWS


def resume_run(executor, run_id: str, trace: TraceWriter | None = None) -> tuple[dict[str, Any], Any]:
    """Resume run_id; return (the updated record, the workflow's result). KeyError if unknown."""
    if executor.runs is None:
        raise RunNotResumable("run records are not enabled for this executor")
    record = executor.runs.load(run_id)
    status = effective_status(record)
    if status not in RESUMABLE:
        detail = {"completed": "it already completed", "failed": f"it failed ({record.get('error')})",
                  "in_progress": "it is still running in this process"}.get(status, status)
        raise RunNotResumable(f"run {run_id} cannot be resumed: {detail}")
    workflow = record.get("workflow")
    if not workflow or workflow.get("kind") not in WORKFLOWS:
        raise RunNotResumable(f"run {run_id} cannot be resumed: no resumable workflow was recorded "
                              "(a /run prompt is never stored; send it again)")
    with executor.run_scope(resume=record):
        result = WORKFLOWS[workflow["kind"]](executor, workflow["params"], trace)
    return executor.runs.load(run_id), result


def main(argv: list[str] | None = None) -> int:
    from sensei_core.gateway import local_setup

    ap = argparse.ArgumentParser(prog="python -m sensei_core.resume", description=__doc__.splitlines()[0])
    ap.add_argument("run_id")
    args = ap.parse_args(argv)
    home = Path(os.environ.get("SENSEI_HOME", Path.home() / ".sensei"))
    executor, trace, switch = local_setup(home)
    if switch.engaged():
        print(f"not resumed: the kill switch is engaged ({switch.path}); POST /revive first", file=sys.stderr)
        return 3
    try:
        record, result = resume_run(executor, args.run_id, trace)
    except KeyError:
        print(f"not resumed: no run record {args.run_id!r} in {executor.runs.root}", file=sys.stderr)
        return 2
    except RunNotResumable as e:
        print(f"not resumed: {e}", file=sys.stderr)
        return 2
    except Exception as e:
        record = executor.runs.load(args.run_id)
        print(f"resumed run {args.run_id} ended {record['status']}: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    reused = record["resumes"][-1]["reused_steps"]
    print(json.dumps({"run_id": record["run_id"], "status": record["status"], "reused_steps": reused,
                      "steps": len(record["steps"]), "result": result}, indent=2, default=str))
    return 0 if record["status"] == "completed" else 1


if __name__ == "__main__":
    sys.exit(main())

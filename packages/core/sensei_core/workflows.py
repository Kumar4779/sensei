"""Resumable workflows (Slice 31): runs that can be re-executed from their run record.

A workflow is a function of (executor, params, trace). Its params are stored in
the run record, so the same params re-derive the same prompts. That lets a
resumed run reuse its recorded steps and execute only the ones that never
finished. Params hold only what the prompt is built from, resolved at the
first run (for example the pending-memory count and yesterday's cost), never
the prompt itself.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sensei_core.brief import build_brief, failed_labels, load_morning_fixture
from sensei_core.trace import TraceWriter

BRIEF_FAMILY = "morning_brief"


def run_brief_workflow(executor, params: dict[str, Any], trace: TraceWriter | None = None) -> dict[str, Any]:
    """The gateway's morning brief: build, verify and repair, then write <day>.md and <day>.json."""
    brief_dir = Path(params["brief_dir"])
    md, status = brief_dir / f"{params['day']}.md", brief_dir / f"{params['day']}.json"
    data = load_morning_fixture(params["fixture"], pending_count=params["pending_count"],
                                yesterday_cost_usd=params["yesterday_cost_usd"])
    result = build_brief(executor, data, md, "LOCAL_ONLY", repair=True, assemble_inbox=True, trace=trace)
    record = {"day": params["day"], "generated_at": datetime.now(timezone.utc).isoformat(),
              "verified": result.verified, "attempts": result.attempts, "failed_checks": failed_labels(result.report),
              "route": result.run.route, "model": result.run.model,
              "est_cost_usd": result.total_est_cost_usd, "latency_ms": result.total_latency_ms}
    status.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    if trace is not None:
        trace.write("brief_generated", {k: record[k] for k in ("day", "verified", "attempts", "failed_checks")})
    return record


WORKFLOWS: dict[str, Callable[..., Any]] = {"brief": run_brief_workflow}

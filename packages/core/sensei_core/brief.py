"""Morning brief workflow (DESIGN.md §1 v1 workflows, Principle 2).

One routed model call writes the brief; the verifier then checks the
written file against the input DATA (event titles, pending count),
never against what the model says it wrote.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sensei_core.executor import Executor, RunResult
from sensei_core.trace import TraceWriter
from sensei_core.verifier import Check, FileContains, FileExists, VerificationReport, Verifier


@dataclass(frozen=True)
class MorningData:
    events: list[dict[str, str]]
    inbox: list[dict[str, str]]
    pending_count: int
    yesterday_cost_usd: float


@dataclass(frozen=True)
class BriefResult:
    path: Path
    verified: bool
    report: VerificationReport
    run: RunResult = field(repr=False)


def _read_json_list(path: Path, keys: tuple[str, ...]) -> list[dict[str, str]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise ValueError(f"{path.name} must be a JSON list")
    for i, item in enumerate(data):
        missing = [k for k in keys if not isinstance(item, dict) or k not in item]
        if missing:
            raise ValueError(f"{path.name}[{i}] is missing {missing}")
    return data


def load_morning_fixture(
    fixture_dir: str | Path,
    *,
    pending_count: int | None = None,
    yesterday_cost_usd: float | None = None,
) -> MorningData:
    """Load calendar.json and inbox.json.

    pending_count and yesterday_cost_usd come from MemoryStore and
    CostLedger in real use; when not given they are read from the
    fixture's state.json (the bench case).
    """
    d = Path(fixture_dir)
    events = _read_json_list(d / "calendar.json", ("title", "start", "end"))
    inbox = _read_json_list(d / "inbox.json", ("from", "subject", "priority"))
    if pending_count is None or yesterday_cost_usd is None:
        state = json.loads((d / "state.json").read_text(encoding="utf-8"))
        pending_count = state["pending_count"] if pending_count is None else pending_count
        yesterday_cost_usd = state["yesterday_cost_usd"] if yesterday_cost_usd is None else yesterday_cost_usd
    if isinstance(pending_count, bool) or not isinstance(pending_count, int) or pending_count < 0:
        raise ValueError("pending_count must be a non-negative int")
    return MorningData(events, inbox, pending_count, float(yesterday_cost_usd))


def compose_prompt(data: MorningData) -> str:
    events = "\n".join(f"- {e['start']} to {e['end']}: {e['title']}" for e in data.events) or "- (none)"
    inbox = "\n".join(
        f"- [{m['priority']}] from {m['from']}: {m['subject']}" for m in data.inbox
    ) or "- (none)"
    return (
        "You are Sensei, a calm and direct personal assistant. Write a short morning brief "
        "in Markdown for the user, using only the facts below.\n\n"
        "Cover, in this order:\n"
        "1. Today's events: list every event by its exact title, with its time.\n"
        "2. Priority inbox: the high-priority messages first, then a one-line count of the rest.\n"
        f"3. Memory review: say exactly how many memory items await review, as the number {data.pending_count}.\n"
        f"4. Cost: yesterday's Sensei cost was ${data.yesterday_cost_usd:.2f}.\n\n"
        f"Today's events:\n{events}\n\n"
        f"Inbox:\n{inbox}\n\n"
        f"Memory items awaiting review: {data.pending_count}\n"
        f"Yesterday's Sensei cost (USD): {data.yesterday_cost_usd:.2f}\n"
    )


def brief_checks(data: MorningData, out_path: str | Path) -> list[Check]:
    """Checks built from the input data, not from the model's text."""
    checks: list[Check] = [FileExists(out_path)]
    checks += [FileContains(out_path, e["title"], label=f"event[{i}]") for i, e in enumerate(data.events)]
    checks.append(FileContains(out_path, str(data.pending_count), label="pending_count"))
    return checks


def build_brief(
    executor: Executor,
    data: MorningData,
    out_path: str | Path,
    sensitivity: str = "LOCAL_ONLY",
    *,
    max_cost_usd: float | None = None,
    max_retries: int = 1,
    trace: TraceWriter | None = None,
) -> BriefResult:
    """Write the brief, then verify the file against the data.

    An unverified brief stays on disk for debugging; verified=False marks it.
    """
    out = Path(out_path)
    run = executor.run_once(
        compose_prompt(data), sensitivity, "simple", max_cost_usd=max_cost_usd, max_retries=max_retries
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(run.response, encoding="utf-8")
    report = Verifier(trace).verify(brief_checks(data, out))
    return BriefResult(out, report.passed, report, run)

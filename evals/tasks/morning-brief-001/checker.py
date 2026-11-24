"""SenseiBench checker for morning-brief-001.

Usage:  python checker.py path/to/brief.md [--json]

Verifies the produced brief against this task's fixture (final state,
never the transcript): the file exists, every calendar event title
appears, the pending memory count appears, and the subject of every
high-priority inbox message appears. Exit 0 on pass, 1 on fail.

Checks are rebuilt here from the raw fixture using sensei_core.verifier
primitives, independently of sensei_core.brief, so the bench does not
grade the workflow with the workflow's own check list.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent
FIXTURE_DIR = TASK_DIR / "fixture"

try:
    from sensei_core.verifier import FileContains, FileExists, VerificationReport, Verifier
except ImportError:  # running from a clone without sensei_core installed
    sys.path.insert(0, str(TASK_DIR.parents[2] / "packages" / "core"))
    from sensei_core.verifier import FileContains, FileExists, VerificationReport, Verifier


def check(brief_path: str | Path, fixture_dir: str | Path = FIXTURE_DIR) -> VerificationReport:
    fixture = Path(fixture_dir)
    events = json.loads((fixture / "calendar.json").read_text(encoding="utf-8"))
    inbox = json.loads((fixture / "inbox.json").read_text(encoding="utf-8"))
    state = json.loads((fixture / "state.json").read_text(encoding="utf-8"))
    checks = [FileExists(brief_path)]
    checks += [FileContains(brief_path, e["title"], label=f"event[{i}]") for i, e in enumerate(events)]
    checks.append(FileContains(brief_path, str(state["pending_count"]), label="pending_count"))
    high = [m for m in inbox if m["priority"] == "high"]
    checks += [FileContains(brief_path, m["subject"], label=f"inbox_high[{i}]") for i, m in enumerate(high)]
    return Verifier().verify(checks)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="checker.py", description="morning-brief-001 checker")
    parser.add_argument("brief", help="path to the produced brief.md")
    parser.add_argument("--json", action="store_true", help="print the report as JSON (machine-readable)")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as e:
        return 2 if e.code else 0
    report = check(args.brief)
    if args.json:
        print(json.dumps({"task": "morning-brief-001", **report.to_dict()}))
    else:
        for r in report.results:
            print(f"{'PASS' if r.passed else 'FAIL'}  {r.name}  ({r.detail})")
        print(f"morning-brief-001: {'PASS' if report.passed else 'FAIL'}")
    return 0 if report.passed else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

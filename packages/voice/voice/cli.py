"""Terminal push-to-talk loop (Voice Lab Slice 1): each line you type is one utterance.

    python -m voice.cli [--trace PATH]

Type what the recogniser "heard". Prefix a line with "~0.4 " to simulate a
low-confidence transcript. An empty line or Ctrl-D ends the session, and the
hard-gate verdict for the run is printed. The trace holds transcripts only,
never audio.
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from pathlib import Path

from sensei_core.trace import TraceWriter, read_events

from voice.adapters import ScriptedUtterance
from voice.loop import VoiceSession
from voice.verify import verify_run

DEFAULT_TRACE_DIR = Path.home() / ".sensei" / "voice" / "traces"


def parse_line(line: str) -> ScriptedUtterance:
    if m := re.match(r"~(\d(?:\.\d+)?)\s+(.*)$", line):
        return ScriptedUtterance(m.group(2), float(m.group(1)))
    return ScriptedUtterance(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Talk to the Voice Lab reference agent (transcripts, no audio).")
    parser.add_argument("--trace", type=Path, help="trace file (JSONL); default ~/.sensei/voice/traces/<run>.jsonl")
    args = parser.parse_args(argv)
    run_id = uuid.uuid4().hex[:12]
    path = args.trace or DEFAULT_TRACE_DIR / f"{run_id}.jsonl"
    session = VoiceSession(TraceWriter(path, run_id=run_id, session_id="voice-cli"))
    interactive = sys.stdin.isatty()
    while True:
        try:
            line = input("you> " if interactive else "").strip()
        except EOFError:
            break
        if not line:
            break
        print(f"sensei> {session.turn(parse_line(line))}")
    verdict = verify_run(read_events(path)) if path.exists() else verify_run([])
    print(f"\nhard gate: {'PASS' if verdict.passed else 'FAIL'}" +
          "".join(f"\n  - {v}" for v in verdict.violations) + f"\ntrace: {path}")
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

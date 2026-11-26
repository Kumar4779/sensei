"""Shadow-router agreement report (Slice 30). Read-only: it changes nothing.

    python -m sensei_core.shadow_report [PATH ...]

Reads shadow_decision events from trace files (a PATH may be a file or a
directory, searched for *.jsonl). The default is every trace under
$SENSEI_HOME/traces, which holds the gateway's and the bench's. It prints, per
task family and overall: runs, the agreement rate between the shadow policy and
the rules Router, and the estimated cost difference had the shadow decided
(shadow est minus rules est, summed). Costs are the shadow's input-token
projection, not billed spend.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable

UNKNOWN = "(unknown)"


def trace_files(paths: Iterable[str | Path]) -> list[Path]:
    files: list[Path] = []
    for p in map(Path, paths):
        files += sorted(p.rglob("*.jsonl")) if p.is_dir() else [p] if p.is_file() else []
    return files


def shadow_events(files: Iterable[Path]) -> list[dict[str, Any]]:
    out = []
    for f in files:
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue  # a torn last line in a live trace; never fatal for a report
                if e.get("event_type") == "shadow_decision":
                    out.append(e.get("payload", {}))
    return out


def summarize(events: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """{"families": {family: row}, "overall": row, "policies": [...]}; row = runs, agree, rate, diff."""
    rows: dict[str, dict[str, Any]] = defaultdict(lambda: {"runs": 0, "agree": 0, "est_cost_diff_usd": 0.0})
    policies: set[str] = set()
    for p in events:
        for key in (p.get("task_family") or UNKNOWN, None):
            r = rows[key]
            r["runs"] += 1
            r["agree"] += bool(p.get("agree"))
            r["est_cost_diff_usd"] += p.get("shadow_est_cost_usd", 0.0) - p.get("rules_est_cost_usd", 0.0)
        if p.get("policy"):
            policies.add(p["policy"])
    overall = rows.pop(None, {"runs": 0, "agree": 0, "est_cost_diff_usd": 0.0})
    for r in (*rows.values(), overall):
        r["agreement_rate"] = r["agree"] / r["runs"] if r["runs"] else None
    return {"families": dict(sorted(rows.items())), "overall": overall, "policies": sorted(policies)}


def format_report(summary: dict[str, Any]) -> str:
    def line(name: str, r: dict[str, Any]) -> str:
        rate = "—" if r["agreement_rate"] is None else f"{r['agreement_rate']:.1%}"
        return f"{name:<40} {r['runs']:>6} {r['agree']:>6} {rate:>10} {r['est_cost_diff_usd']:>+16.6f}"

    head = f"{'task family':<40} {'runs':>6} {'agree':>6} {'agreement':>10} {'est diff USD':>16}"
    policies = ", ".join(summary["policies"]) or "none recorded"
    out = [f"Shadow router agreement (policy: {policies}) vs the rules Router",
           "est diff = shadow est cost - rules est cost, summed (input-token projection, not billed spend)",
           "", head, "-" * len(head)]
    out += [line(name, r) for name, r in summary["families"].items()]
    out += ["-" * len(head), line("overall", summary["overall"])]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    default = Path(os.environ.get("SENSEI_HOME", Path.home() / ".sensei")) / "traces"
    ap = argparse.ArgumentParser(prog="python -m sensei_core.shadow_report", description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="*", help=f"trace files or directories (default: {default})")
    args = ap.parse_args(argv)
    files = trace_files(args.paths or [default])
    if not files:
        print(f"no trace files found in {', '.join(args.paths) or default}", file=sys.stderr)
        return 1
    print(format_report(summarize(shadow_events(files))))
    return 0


if __name__ == "__main__":
    sys.exit(main())

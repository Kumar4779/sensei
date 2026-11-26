"""SenseiBench checker for doc-summary-001.

Usage:  python checker.py path/to/summary.json [--json]

Grades the produced summary.json against this task's ground truth
(expected.json, which lives beside this file and outside fixture/; the
workflow never sees it). Final state, never the transcript. Deterministic
checks only:

- the file is a JSON object with title (string), key_points (list of
  strings), entities (list of strings) and abstract (string);
- the abstract is exactly one sentence;
- each key fact appears verbatim in the right field: the headline in title,
  each figure in key_points, each named entity in entities, the subject in
  abstract;
- no fact from the article's unrelated section appears in any field
  (case-insensitive, so a reworded capitalisation still counts).

Exit 0 on pass, 1 on fail.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

TASK_DIR = Path(__file__).resolve().parent
EXPECTED = TASK_DIR / "expected.json"
TASK = "doc-summary-001"


def _check(name: str, passed: bool, detail: str) -> dict:
    return {"name": name, "passed": bool(passed), "detail": detail}


def one_sentence(text: str) -> bool:
    t = text.strip()
    return bool(t) and t[-1] in ".!?" and not re.search(r"[.!?]\s+\S", t[:-1])


def check(summary_path: str | Path, expected_path: str | Path = EXPECTED) -> dict:
    expected = json.loads(Path(expected_path).read_text(encoding="utf-8"))
    path = Path(summary_path)
    checks = [_check(f"file_exists:{path}", path.is_file(), "file exists" if path.is_file() else "file not found")]
    data = None
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            checks.append(_check("json_object", False, f"invalid JSON: {e.msg}"))
        else:
            ok = isinstance(data, dict)
            checks.append(_check("json_object", ok, "a JSON object" if ok else "top level is not a JSON object"))
            data = data if ok else None
    if data is None:
        return {"passed": False, "checks": checks}

    def text(field: str) -> str | None:
        v = data.get(field)
        if isinstance(v, str):
            return v
        if isinstance(v, list) and all(isinstance(x, str) for x in v):
            return "\n".join(v)
        return None

    kinds = {"title": str, "key_points": list, "entities": list, "abstract": str}
    for field, kind in kinds.items():
        ok = isinstance(data.get(field), kind) and text(field) is not None and bool(text(field).strip())
        checks.append(_check(f"field:{field}", ok, f"non-empty {kind.__name__}" if ok else f"missing or not a {kind.__name__}"))
    abstract = data.get("abstract") if isinstance(data.get("abstract"), str) else ""
    checks.append(_check("abstract_one_sentence", one_sentence(abstract),
                         "one sentence" if one_sentence(abstract) else "not exactly one sentence"))

    def fact(label: str, field: str, value: str) -> None:
        ok = value in (text(field) or "")
        checks.append(_check(f"{field}#{label}", ok, f"present verbatim in {field}" if ok else f"absent from {field}"))

    fact("headline", "title", expected["title_contains"])
    for i, value in enumerate(expected["key_point_facts"]):
        fact(f"fact[{i}]", "key_points", value)
    for i, value in enumerate(expected["entities"]):
        fact(f"entity[{i}]", "entities", value)
    fact("subject", "abstract", expected["abstract_contains"])

    everything = "\n".join(t for t in (text(f) for f in kinds) if t).lower()
    for i, value in enumerate(expected["distractor_facts"]):
        leaked = value.lower() in everything
        checks.append(_check(f"distractor[{i}]", not leaked, "leaked from the unrelated section" if leaked else "absent"))
    return {"passed": all(c["passed"] for c in checks), "checks": checks}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="checker.py", description=f"{TASK} checker")
    parser.add_argument("summary", help="path to the produced summary.json")
    parser.add_argument("--json", action="store_true", help="print the report as JSON (machine-readable)")
    try:
        args = parser.parse_args(argv[1:])
    except SystemExit as e:
        return 2 if e.code else 0
    report = check(args.summary)
    if args.json:
        print(json.dumps({"task": TASK, "version": 1, **report}))
    else:
        for c in report["checks"]:
            print(f"{'PASS' if c['passed'] else 'FAIL'}  {c['name']}  ({c['detail']})")
        print(f"{TASK}: {'PASS' if report['passed'] else 'FAIL'}")
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))

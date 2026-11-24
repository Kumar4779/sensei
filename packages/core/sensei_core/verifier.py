"""Verifier: deterministic checks of real state (DESIGN.md Principle 2).

The actor never grades itself. Checks read live state (files, values)
at verify time; nothing in this API accepts what the actor claims
happened. Check details carry no file or field contents, so verifying
LOCAL_ONLY work never copies that content into a trace.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol, runtime_checkable

from sensei_core.trace import TraceWriter


@dataclass(frozen=True)
class CheckResult:
    name: str
    passed: bool
    detail: str


@runtime_checkable
class Check(Protocol):
    name: str

    def run(self) -> CheckResult: ...


@dataclass(frozen=True)
class FileExists:
    path: str | Path

    @property
    def name(self) -> str:
        return f"file_exists:{self.path}"

    def run(self) -> CheckResult:
        ok = Path(self.path).is_file()
        return CheckResult(self.name, ok, "file exists" if ok else "file not found")


@dataclass(frozen=True)
class FileContains:
    path: str | Path
    text: str
    label: str = ""  # identifies which text, without copying it into reports

    @property
    def name(self) -> str:
        return f"file_contains:{self.path}" + (f"#{self.label}" if self.label else "")

    def run(self) -> CheckResult:
        p = Path(self.path)
        if not p.is_file():
            return CheckResult(self.name, False, "file not found")
        ok = self.text in p.read_text(encoding="utf-8")
        return CheckResult(self.name, ok, "expected text found" if ok else "expected text not found")


@dataclass(frozen=True)
class FileNotContains:
    """Passes only if the file exists and does NOT contain text (absence check)."""

    path: str | Path
    text: str
    label: str = ""
    ignore_case: bool = False

    @property
    def name(self) -> str:
        return f"file_not_contains:{self.path}" + (f"#{self.label}" if self.label else "")

    def run(self) -> CheckResult:
        p = Path(self.path)
        if not p.is_file():
            # Absence in a missing file proves nothing: fail closed.
            return CheckResult(self.name, False, "file not found")
        content = p.read_text(encoding="utf-8")
        needle = self.text
        if self.ignore_case:
            content, needle = content.casefold(), needle.casefold()
        ok = needle not in content
        return CheckResult(self.name, ok, "forbidden text absent" if ok else "forbidden text present")


@dataclass(frozen=True)
class JsonFieldEquals:
    path: str | Path
    field: str
    expected: Any

    @property
    def name(self) -> str:
        return f"json_field_equals:{self.path}#{self.field}"

    def run(self) -> CheckResult:
        p = Path(self.path)
        if not p.is_file():
            return CheckResult(self.name, False, "file not found")
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            return CheckResult(self.name, False, f"invalid JSON: {e.msg}")
        if not isinstance(data, dict):
            return CheckResult(self.name, False, "top level is not a JSON object")
        if self.field not in data:
            return CheckResult(self.name, False, f"field {self.field!r} missing")
        ok = data[self.field] == self.expected
        return CheckResult(
            self.name, ok, f"field {self.field!r} {'equals' if ok else 'differs from'} expected value"
        )


@dataclass(frozen=True)
class ValueInRange:
    value_supplier: Callable[[], float]
    lo: float
    hi: float
    label: str = "value"

    @property
    def name(self) -> str:
        return f"value_in_range:{self.label}"

    def run(self) -> CheckResult:
        value = self.value_supplier()
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return CheckResult(self.name, False, f"value is {type(value).__name__}, not a number")
        ok = self.lo <= value <= self.hi
        return CheckResult(
            self.name, ok, f"{value} {'within' if ok else 'outside'} [{self.lo}, {self.hi}]"
        )


@dataclass(frozen=True)
class VerificationReport:
    passed: bool
    results: tuple[CheckResult, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "checks": [{"name": r.name, "passed": r.passed, "detail": r.detail} for r in self.results],
        }


class Verifier:
    def __init__(self, trace: TraceWriter | None = None) -> None:
        self._trace = trace

    def verify(self, checks: Iterable[Check]) -> VerificationReport:
        """Run every check (no short-circuit); pass only if all pass."""
        checks = list(checks)
        if not checks:
            raise ValueError("verify() needs at least one check; an empty check list proves nothing")
        results = []
        for check in checks:
            try:
                result = check.run()
            except Exception as e:
                result = CheckResult(getattr(check, "name", type(check).__name__), False, f"check raised {type(e).__name__}: {e}")
            results.append(result)
        report = VerificationReport(all(r.passed for r in results), tuple(results))
        if self._trace is not None:
            self._trace.write("verification", report.to_dict())
        return report

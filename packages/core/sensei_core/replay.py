"""Deterministic replay of a trace (Slice 31). An audit tool; it makes no model call.

    python -m sensei_core.replay <trace_file> [--models PATH] [-v]

Reads a trace and re-derives every decision the system recorded, from the
trace's own inputs, using the same code that made the decisions: the rules
Router, the shadow policy, the budget and spend-cap arithmetic, loop
detection, and the verifier's all-checks-pass rule. It then compares each
re-derived decision with the recorded one, in order.

The trace's inputs:
- what each run was asked: sensitivity, difficulty, prompt size and digest,
  budget, retries;
- the configuration each run recorded: route models and spend caps;
- the outside world's answers: whether a model call succeeded and how long its
  reply was, whether the kill switch was engaged, and each check's result.

The decisions, all re-derived:
- route choices, retries and escalations;
- shadow decisions;
- budget, cap and loop halts, and spend alerts;
- each model call's estimated tokens and cost;
- verification verdicts;
- run completions, repairs, brief summaries, and the status each run closed with.

Rates and tiers come from models.toml (`--models`). If its digest differs from
the one the trace recorded at startup, replay says so: a divergence may then
be the config, not the trace.

Prints MATCH, or the first divergence: line, run, decision number, and each
field re-derived against recorded. It stops there, because later state would
build on a wrong step.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sensei_core.brief import failed_labels
from sensei_core.config import DEFAULT_MODELS_PATH, load_models
from sensei_core.egress import API_TIERS
from sensei_core.executor import COST_BASIS, LOOP_LIMIT, SPEND_ALERT_PERCENTS, estimate_cost, estimate_tokens
from sensei_core.manifests import digest_bytes
from sensei_core.router import RouteRequest, Router
from sensei_core.shadow import COST_BASIS as SHADOW_COST_BASIS
from sensei_core.shadow import ShadowPolicy, est_input_cost
from sensei_core.verifier import CheckResult, VerificationReport

# Events replay reads. Everything else (auth failures, memory, kill-switch toggles, ...) is skipped.
RELEVANT = frozenset({
    "manifests_loaded", "run_opened", "run_closed", "run_started", "route_decision", "shadow_decision",
    "shadow_error", "model_call", "step_replayed", "route_failure", "run_retried", "run_escalated",
    "run_failed", "run_refused", "budget_exceeded", "spend_cap_reached", "spend_alert", "loop_detected",
    "verification", "verification_failed", "run_completed", "repair_attempted", "brief_generated",
})


class _Crashed(Exception):
    """The trace shows the process ending inside a run (a new process starts, or the trace ends)."""


class Diverged(Exception):
    def __init__(self, line: int, event_type: str, message: str, fields: list[tuple[str, Any, Any]] | None = None):
        self.line, self.event_type, self.message, self.fields = line, event_type, message, fields or []
        super().__init__(message)


@dataclass
class Scope:
    run_id: str | None
    family: str | None
    caps: dict | None
    route_models: dict[str, str]
    counts: Counter = field(default_factory=Counter)
    halt: str | None = None
    completed_runs: int = 0
    reports: list[VerificationReport] = field(default_factory=list)


@dataclass
class ReplayResult:
    matched: bool
    runs: int = 0
    resumed_runs: int = 0
    decisions: int = 0
    crashed_runs: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    divergence: Diverged | None = None
    steps: list[str] = field(default_factory=list)


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        return isinstance(a, (int, float)) and isinstance(b, (int, float)) and math.isclose(a, b, rel_tol=1e-9,
                                                                                             abs_tol=1e-12)
    return a == b


def _day(event: dict) -> str:
    return datetime.fromisoformat(event["timestamp"]).astimezone(timezone.utc).date().isoformat()


def _report(payload: dict) -> VerificationReport:
    results = tuple(CheckResult(c["name"], bool(c["passed"]), c.get("detail", "")) for c in payload.get("checks", []))
    return VerificationReport(bool(payload.get("passed")), results)


class Replayer:
    def __init__(self, events: list[tuple[int, dict]], models_path: str | Path = DEFAULT_MODELS_PATH) -> None:
        self.events = events
        self.i = 0
        self.routes = load_models(models_path)
        self.router = Router(self.routes)  # no trace: replay writes nothing
        self.shadow = ShadowPolicy(self.routes)
        self.models_digest = digest_bytes(Path(models_path).read_bytes())
        self.result = ReplayResult(matched=True)
        self.in_run = False

    # ---- cursor ----

    def _skip(self) -> None:
        while self.i < len(self.events) and self.events[self.i][1].get("event_type") not in RELEVANT:
            self.i += 1

    def peek(self) -> tuple[int, dict] | None:
        self._skip()
        return self.events[self.i] if self.i < len(self.events) else None

    def take(self, *types: str) -> tuple[int, dict]:
        item = self.peek()
        if self.in_run and (item is None or item[1]["event_type"] in ("manifests_loaded", "run_opened")):
            raise _Crashed()
        if item is None:
            raise Diverged(self.events[-1][0] if self.events else 0, "end of trace",
                           f"expected {' or '.join(types)}, but the trace ends")
        line, ev = item
        if types and ev["event_type"] not in types:
            raise Diverged(line, ev["event_type"], f"expected {' or '.join(types)}, recorded {ev['event_type']}")
        self.i += 1
        return line, ev

    def expect(self, event_type: str, fields: dict[str, Any], scope: Scope | None = None) -> dict:
        """The next relevant event must be event_type with these re-derived field values."""
        line, ev = self.take(event_type)
        payload = ev.get("payload", {})
        diffs = [(k, v, payload.get(k, "<absent>")) for k, v in fields.items() if not _same(v, payload.get(k))]
        self.result.decisions += 1
        n = self.result.decisions
        if diffs:
            raise Diverged(line, event_type, f"decision {n} ({event_type}) does not match", diffs)
        self.result.steps.append(f"  ok  line {line:>5}  decision {n:>4}  {event_type}"
                                 + (f"  [{scope.run_id}]" if scope and scope.run_id else ""))
        return payload

    # ---- ledger (the trace is the cost ledger) ----

    def _spend_before(self, upto: int, day: str) -> tuple[float, float]:
        today = month = 0.0
        for _, e in self.events[:upto]:
            if e.get("event_type") == "model_call" and isinstance(e.get("payload", {}).get("est_cost_usd"), (int, float)):
                d = _day(e)
                if d == day:
                    today += e["payload"]["est_cost_usd"]
                if d[:7] == day[:7]:
                    month += e["payload"]["est_cost_usd"]
        return today, month

    # ---- the walk ----

    def run(self) -> ReplayResult:
        try:
            while (item := self.peek()) is not None:
                line, ev = item
                t = ev["event_type"]
                if t == "manifests_loaded":
                    self.i += 1
                    recorded = ev.get("payload", {}).get("models.toml")
                    if recorded and recorded != self.models_digest:
                        self.result.notes.append(
                            f"line {line}: the trace's models.toml digest {recorded[:19]}… differs from the "
                            f"replay's {self.models_digest[:19]}…; replay uses the replay's routing table")
                elif t == "run_opened":
                    self.scope()
                elif t == "run_started":  # a run from before Slice 31, without run_opened
                    self.in_run = True
                    try:
                        self.run_once(Scope(None, None, None, {}))
                    except _Crashed:
                        self.result.crashed_runs.append("?")
                    finally:
                        self.in_run = False
                elif t == "verification":
                    self.i += 1
                    self.verdict(line, ev, None)
                else:
                    raise Diverged(line, t, f"{t} outside any run")
        except Diverged as d:
            self.result.matched, self.result.divergence = False, d
        return self.result

    def verdict(self, line: int, ev: dict, scope: Scope | None) -> VerificationReport:
        report = _report(ev.get("payload", {}))
        derived = all(r.passed for r in report.results) and bool(report.results)
        self.result.decisions += 1
        if derived != report.passed:
            raise Diverged(line, "verification", f"decision {self.result.decisions} (verification verdict) "
                           "does not match", [("passed", derived, report.passed)])
        self.result.steps.append(f"  ok  line {line:>5}  decision {self.result.decisions:>4}  verification")
        if scope is not None:
            scope.reports.append(report)
        return report

    def scope(self) -> None:
        _, opened = self.take("run_opened")
        p = opened.get("payload", {})
        scope = Scope(p.get("run_id"), p.get("task_family"), p.get("spend_caps"), p.get("route_models") or {})
        self.result.runs += 1
        self.result.resumed_runs += bool(p.get("resumed"))
        self.in_run = True
        try:
            self._scope_body(scope)
        except _Crashed:
            # The process ended inside the run: a crash. The run record carries it on.
            self.result.crashed_runs.append(scope.run_id or "?")
        finally:
            self.in_run = False

    def _scope_body(self, scope: Scope) -> None:
        while True:
            item = self.peek()
            if item is None or item[1]["event_type"] in ("run_opened", "manifests_loaded"):
                raise _Crashed()
            line, ev = item
            t = ev["event_type"]
            if t == "run_started":
                self.run_once(scope)
            elif t == "verification":
                self.i += 1
                self.verdict(line, ev, scope)
            elif t == "repair_attempted":
                if len(scope.reports) < 2:
                    raise Diverged(line, t, "repair_attempted without a first and a final verification")
                self.expect(t, {"failed_labels": failed_labels(scope.reports[-2]),
                                "verified": scope.reports[-1].passed}, scope)
            elif t == "brief_generated":
                last = scope.reports[-1] if scope.reports else None
                self.expect(t, {"verified": bool(last and last.passed), "attempts": scope.completed_runs,
                                "failed_checks": failed_labels(last) if last else []}, scope)
            elif t == "run_closed":
                payload = ev.get("payload", {})
                derived = scope.halt or "completed"
                if derived == "completed" and payload.get("status") == "failed" and payload.get("error"):
                    derived = "failed"  # an exception outside any model call; its error is recorded
                self.expect(t, {"run_id": scope.run_id, "status": derived}, scope)
                return
            else:
                raise Diverged(line, t, f"{t} where the run expected a new step or its close")

    def run_once(self, scope: Scope) -> None:
        line, started = self.take("run_started")
        p = started.get("payload", {})
        sens, diff = p["sensitivity_class"], p["difficulty"]
        chars, digest, retries = p["prompt_chars"], p.get("prompt_sha256"), p["max_retries"]
        limit = p.get("max_cost_usd")
        if sens == "LOCAL_ONLY" and limit is not None:
            raise Diverged(line, "run_started", "a LOCAL_ONLY run recorded a cost limit (they are exempt)",
                           [("max_cost_usd", None, limit)])

        def model_of(route: str) -> str:
            return scope.route_models.get(route, self.routes[route].model)

        def decide(failures: int) -> str:
            d = self.router.decide(RouteRequest(sens, diff, prior_failures=failures))
            self.expect("route_decision", {"sensitivity_class": sens, "difficulty": diff, "prior_failures": failures,
                                           "route": d.route, "model": model_of(d.route), "reason": d.reason}, scope)
            return d.route

        first = route = decide(0)
        item = self.peek()
        if item and item[1]["event_type"] == "shadow_error":
            self.i += 1
            self.result.notes.append(f"line {item[0]}: shadow_error recorded (the shadow failed; the run did not)")
        else:
            req = RouteRequest(sens, diff)
            sh = self.shadow.decide(req, self.router.decide(req), chars)
            self.expect("shadow_decision", {
                "policy": self.shadow.name, "task_family": scope.family, "sensitivity_class": sens,
                "difficulty": diff, "prior_failures": 0, "prompt_chars": chars, "rules_route": first,
                "shadow_route": sh.route, "agree": sh.route == first, "eligible": list(sh.eligible),
                "rules_est_cost_usd": est_input_cost(self.routes[first], chars),
                "shadow_est_cost_usd": sh.est_cost_usd, "cost_basis": SHADOW_COST_BASIS}, scope)

        spent, failures = 0.0, 0
        while True:
            item = self.peek()
            if item and item[1]["event_type"] == "run_refused":  # the kill switch: an outside fact
                self.i += 1
                scope.halt = "killed"
                return
            rate = self.routes[route].est_cost_per_1k_tokens
            tokens_in = estimate_tokens(chars)
            key = (route, digest)
            if scope.counts[key] >= LOOP_LIMIT - 1:
                self.expect("loop_detected", {"route": route, "prompt_sha256": digest,
                                              "successful_calls": scope.counts[key]}, scope)
                scope.halt = "failed"
                return
            if limit is not None and spent + estimate_cost(tokens_in, rate) > limit:
                self.expect("budget_exceeded", {"accumulated_est_cost_usd": spent, "limit_usd": limit,
                                                "next_call_est_cost_usd": estimate_cost(tokens_in, rate)}, scope)
                scope.halt = "failed"
                return
            item = self.peek()
            if item and item[1]["event_type"] == "step_replayed":  # a resumed run reusing a recorded step
                rline, rev = self.take("step_replayed")
                rp = rev.get("payload", {})
                ok = rp.get("outcome") == "ok"
                cost = estimate_cost(tokens_in + estimate_tokens(rp.get("response_chars", 0)), rate) if ok else 0.0
                self.expect_fields(rline, "step_replayed", rp, {"route": route, "model": model_of(route),
                                                                "prompt_sha256": digest, "prompt_chars": chars,
                                                                "est_cost_usd": cost})
                if ok:
                    scope.counts[key] += 1
                    spent += cost
                    break
                outcome_line, outcome = self.take("route_failure")
            else:
                tier = self.routes[route].tier
                caps = scope.caps or {}
                if caps and tier in API_TIERS and item is not None:
                    day = _day(item[1])
                    today, month = self._spend_before(self.i, day)
                    nxt = estimate_cost(tokens_in, rate)
                    for cap, spent_now, period in (("daily", today, day), ("monthly", month, day[:7])):
                        cap_usd = caps.get(f"{cap}_cap_usd")
                        if cap_usd is not None and spent_now + nxt > cap_usd:
                            self.expect("spend_cap_reached", {"cap": cap, "period": period,
                                                              "spent_est_cost_usd": spent_now, "cap_usd": cap_usd,
                                                              "next_call_est_cost_usd": nxt}, scope)
                            scope.halt = "failed"
                            return
                outcome_line, outcome = self.take("model_call", "route_failure", "run_failed")
            t = outcome["event_type"]
            op = outcome.get("payload", {})
            if t == "run_failed":  # failed before any call (e.g. no client for the route): an outside fact
                scope.halt = "failed"
                return
            if t == "model_call":
                tokens_out = estimate_tokens(op.get("response_chars", 0))
                cost = estimate_cost(tokens_in + tokens_out, rate)
                self.expect_fields(outcome_line, t, op, {
                    "route": route, "model": model_of(route), "prompt_chars": chars, "prompt_sha256": digest,
                    "est_tokens_in": tokens_in, "est_tokens_out": tokens_out, "est_cost_usd": cost,
                    "cost_basis": COST_BASIS})
                scope.counts[key] += 1
                spent += cost
                if caps and tier in API_TIERS and caps.get("daily_cap_usd"):
                    self.alerts(outcome, caps["daily_cap_usd"], scope)
                break
            # route_failure: the call failed (an outside fact); the retry decision is re-derived
            self.expect_fields(outcome_line, t, op, {"route": route, "model": model_of(route)})
            failures += 1
            if failures > retries:
                self.expect("run_failed", {"total_est_cost_usd": spent}, scope)
                scope.halt = "failed"
                return
            retry = route if sens == "LOCAL_ONLY" else decide(failures)
            if retry not in (route, "frontier_api"):
                self.expect("run_failed", {"total_est_cost_usd": spent}, scope)
                scope.halt = "failed"
                return
            self.expect("run_escalated" if retry != route else "run_retried",
                        {"from_route": route, "to_route": retry, "attempt": failures}, scope)
            route = retry

        verified = False
        item = self.peek()
        if item and item[1]["event_type"] == "verification":  # run_once(checks=...)
            self.i += 1
            report = self.verdict(item[0], item[1], scope)
            if not report.passed:
                self.expect("verification_failed", {"passed": False, "total_est_cost_usd": spent}, scope)
                scope.halt = "failed"
                return
            verified = True
        self.expect("run_completed", {"route": route, "model": model_of(route), "escalated": route != first,
                                      "total_est_cost_usd": spent, "verified": verified}, scope)
        scope.completed_runs += 1

    def expect_fields(self, line: int, event_type: str, payload: dict, fields: dict[str, Any]) -> None:
        """Check the re-derivable fields of an outcome event (the outcome itself is an outside fact)."""
        diffs = [(k, v, payload.get(k, "<absent>")) for k, v in fields.items() if not _same(v, payload.get(k))]
        self.result.decisions += 1
        if diffs:
            raise Diverged(line, event_type, f"decision {self.result.decisions} ({event_type}) does not match", diffs)
        self.result.steps.append(f"  ok  line {line:>5}  decision {self.result.decisions:>4}  {event_type}")

    def alerts(self, call: dict, cap: float, scope: Scope) -> None:
        day = _day(call)
        today, _ = self._spend_before(self.i, day)  # includes this call
        sent = {e["payload"].get("percent") for _, e in self.events[:self.i]
                if e.get("event_type") == "spend_alert" and e.get("payload", {}).get("day_utc") == day}
        for percent in SPEND_ALERT_PERCENTS:
            if today >= cap * percent / 100 and percent not in sent:
                self.expect("spend_alert", {"percent": percent, "day_utc": day, "spent_est_cost_usd": today,
                                            "daily_cap_usd": cap}, scope)


def read_trace(path: str | Path) -> list[tuple[int, dict]]:
    events = []
    with Path(path).open(encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            if line.strip():
                events.append((n, json.loads(line)))
    return events


def replay(path: str | Path, models_path: str | Path = DEFAULT_MODELS_PATH) -> ReplayResult:
    return Replayer(read_trace(path), models_path).run()


def format_result(path: str | Path, r: ReplayResult, verbose: bool = False) -> str:
    out = [f"replay: {path}",
           f"  runs: {r.runs} ({r.resumed_runs} resumed, {len(r.crashed_runs)} ended in a crash)  "
           f"decisions re-derived: {r.decisions}  model calls made: 0"]
    out += [f"  note: {n}" for n in r.notes]
    if r.crashed_runs:
        out.append(f"  crashed (no run_closed): {', '.join(r.crashed_runs)}")
    if verbose or not r.matched:
        out += r.steps
    if r.matched:
        out.append("MATCH: every decision re-derived from the trace's inputs equals the recorded one.")
    else:
        d = r.divergence
        out.append(f"  XX  line {d.line:>5}  decision {r.decisions:>4}  {d.event_type}")
        out.append(f"DIVERGED at line {d.line} ({d.event_type}): {d.message}")
        out += [f"    {k}: re-derived {v!r}, recorded {rec!r}" for k, v, rec in d.fields]
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m sensei_core.replay", description=__doc__.splitlines()[0])
    ap.add_argument("trace_file")
    ap.add_argument("--models", default=str(DEFAULT_MODELS_PATH), help="routing table (default: the shipped one)")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every re-derived step")
    args = ap.parse_args(argv)
    try:
        result = replay(args.trace_file, args.models)
    except (OSError, ValueError) as e:
        print(f"replay: cannot read {args.trace_file}: {e}", file=sys.stderr)
        return 2
    print(format_result(args.trace_file, result, args.verbose))
    return 0 if result.matched else 1


if __name__ == "__main__":
    sys.exit(main())

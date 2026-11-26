"""Executor: one routed model call per run, with traced retries and budgets.

Implements the Agent Runtime's Router -> Executor step (DESIGN.md §2)
and Threat 7: run budgets, cost accounting, and (Slice 29) daily and monthly
spend caps with alerts, loop detection, and a kill-switch check before every
model call.

Slice 31: every top-level run scope is bracketed by run_opened / run_closed
trace events and, given a RunStore, writes a crash-safe run record. A resumed
run re-executes its workflow, but each recorded step (same route, same prompt
digest) is served from the record instead of calling the model again.
Prompt and response text are never written to the trace, only their
lengths (DESIGN.md Threat 4).
"""

from __future__ import annotations

import math
import threading
import uuid
import time
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Iterator

from sensei_core.config import Route, SpendCaps
from sensei_core.egress import API_TIERS, EgressLog, destination_host, prompt_digest
from sensei_core.killswitch import KillSwitch, KillSwitchEngaged
from sensei_core.ledger import CostLedger
from sensei_core.manifests import ManifestGuard, ManifestTamperError
from sensei_core.runs import PROCESS_ID, ResumeDiverged, RunStore
from sensei_core.router import ModelClient, RouteDecision, RouteRequest, Router, RoutingError
from sensei_core.shadow import COST_BASIS as SHADOW_COST_BASIS
from sensei_core.shadow import ShadowPolicy, est_input_cost
from sensei_core.trace import TraceWriter, read_events
from sensei_core.verifier import Check, VerificationReport, Verifier

# Rough token estimate until real token counts arrive from clients.
CHARS_PER_TOKEN = 4
COST_BASIS = "estimated: tokens = ceil(chars / 4), cost = tokens / 1000 * est_cost_per_1k_tokens"
# Slice 29: the third request for a call that already succeeded twice in this run halts it.
LOOP_LIMIT = 3
SPEND_ALERT_PERCENTS = (50, 80)


class NoClientConfigured(RuntimeError):
    """Raised when the router picks a route that has no client."""


class BudgetExceeded(RuntimeError):
    """A budget halt: the next call would push the run past its cost limit."""

    def __init__(self, accumulated_cost_usd: float, limit_usd: float, next_call_est_usd: float) -> None:
        self.accumulated_cost_usd = accumulated_cost_usd
        self.limit_usd = limit_usd
        self.next_call_est_usd = next_call_est_usd
        super().__init__(
            f"run budget exceeded: accumulated ${accumulated_cost_usd:.6f} + next call "
            f"~${next_call_est_usd:.6f} > limit ${limit_usd:.6f}"
        )


class SpendCapReached(BudgetExceeded):
    """A spend-cap halt (Slice 29): the next API call would push today's or this month's spend past its cap."""

    def __init__(self, cap: str, period: str, spent_usd: float, cap_usd: float, next_call_est_usd: float) -> None:
        self.cap = cap  # "daily" | "monthly"
        self.period = period  # the UTC day or month the cap covers
        super().__init__(spent_usd, cap_usd, next_call_est_usd)
        self.args = (f"{cap} spend cap reached for {period}: spent ${spent_usd:.6f} + next call "
                     f"~${next_call_est_usd:.6f} > cap ${cap_usd:.6f}",)


class LoopDetected(RuntimeError):
    """The run asked for the same successful call (same route, same prompt) a third time."""

    def __init__(self, route: str, digest: str, successful_calls: int) -> None:
        self.route = route
        self.prompt_sha256 = digest
        self.successful_calls = successful_calls
        super().__init__(f"loop detected: route {route!r}, prompt {digest[:19]}… already succeeded "
                         f"{successful_calls} times in this run")


class ReplayedFailure(RuntimeError):
    """A resumed run reached a step that failed originally; the failure is replayed, not retried live."""

    def __init__(self, error_class: str) -> None:
        self.error_class = error_class
        super().__init__(f"recorded failure replayed: {error_class}")


class VerificationFailed(RuntimeError):
    """The model call succeeded but the verifier rejected the resulting state."""

    def __init__(self, report: VerificationReport, total_est_cost_usd: float) -> None:
        self.report = report
        self.total_est_cost_usd = total_est_cost_usd
        failed = [r.name for r in report.results if not r.passed]
        super().__init__(f"verification failed: {', '.join(failed)}")


@dataclass(frozen=True)
class RunResult:
    response: str
    route: str
    model: str
    latency_ms: float
    escalated: bool
    total_est_cost_usd: float = 0.0
    verified: bool = False


def estimate_tokens(chars: int) -> int:
    return math.ceil(chars / CHARS_PER_TOKEN)


def estimate_cost(tokens: int, cost_per_1k_tokens: float) -> float:
    return tokens / 1000 * cost_per_1k_tokens


class Executor:
    def __init__(
        self,
        router: Router,
        clients: dict[str, ModelClient],
        trace: TraceWriter | None = None,
        egress: EgressLog | None = None,
        manifests: ManifestGuard | None = None,
        *,
        budgets: SpendCaps | None = None,
        kill_switch: KillSwitch | None = None,
        runs: RunStore | None = None,
    ) -> None:
        self._router = router
        self._clients = dict(clients)
        self._trace = trace
        self._egress = egress  # Slice 23: API-route calls are logged here (sizes + digest, never content)
        # Slice 24: guardrail manifests pinned at construction (default: the shipped
        # tools.toml and models.toml), re-verified before every run.
        self.manifests = manifests if manifests is not None else ManifestGuard(trace=trace)
        # Slice 29: spend caps read today's and this month's spend from the trace (the cost
        # ledger), so they need one. Caps unset = no cap.
        self._budgets = budgets if budgets is not None and budgets.any else None
        if self._budgets is not None and trace is None:
            raise ValueError("spend caps need a trace: it is the cost ledger they are checked against")
        self._kill_switch = kill_switch
        self._scope = threading.local()  # per-thread: the gateway serves requests concurrently
        # Slice 30: decides on every run in shadow; never executed, never calls a model.
        self.shadow = ShadowPolicy(router.routes)
        self.runs = runs  # Slice 31: crash-safe run records; None = no records (tests, library use)

    @property
    def routes(self) -> dict[str, Route]:
        """A copy of the routing table in force (read by the containment probe; Slice 32)."""
        return self._router.routes

    @property
    def clients(self) -> dict[str, ModelClient]:
        """A copy of the route -> client map (the probe reports which are fakes; Slice 32)."""
        return dict(self._clients)

    @contextmanager
    def run_scope(
        self,
        task_family: str | None = None,
        *,
        workflow: dict | None = None,
        resume: dict | None = None,
    ) -> Iterator[None]:
        """One run. Calls inside share a loop-detection count, a task family and a run record.

        A multi-call workflow (a brief's draft and repair, an agent loop) wraps its calls in one
        scope; run_once opens its own scope when none is active. Nested scopes join the outer one,
        and keep its task_family (recorded on shadow decisions; the bench sets the task id).

        workflow ({"kind", "params"}) makes the run resumable. resume is a loaded run record:
        its ok/error steps are served in order instead of calling the model (Slice 31).
        """
        s = self._scope
        if getattr(s, "counts", None) is not None:
            if resume is not None:
                raise RuntimeError("a run cannot be resumed inside another run")
            yield
            return
        record: dict | None = None
        replay: list[dict] = []
        if resume is not None:
            if self.runs is None:
                raise RuntimeError("resuming a run needs run records (Executor(runs=...))")
            record = resume
            for step in record["steps"]:
                if step["outcome"] == "in_flight":
                    step["outcome"] = "interrupted"  # the point of interruption; executed again, live
            replay = [step for step in record["steps"] if step["outcome"] in ("ok", "error")]
            record["resumes"].append({"at": datetime.now(timezone.utc).isoformat(), "process": PROCESS_ID,
                                      "reused_steps": len(replay)})
            record["status"], record["error"], record["process"] = "in_progress", None, PROCESS_ID
            self.runs.save(record)
            task_family = record["task_family"]
        elif self.runs is not None:
            record = self.runs.create(task_family, workflow)
        s.counts, s.family, s.record, s.replay, s.cursor, s.halt = Counter(), task_family, record, replay, 0, None
        s.run_id = record["run_id"] if record is not None else uuid.uuid4().hex[:16]
        caps = self._budgets
        self._event("run_opened", {
            "run_id": s.run_id, "task_family": task_family, "resumed": resume is not None,
            "spend_caps": None if caps is None else {"daily_cap_usd": caps.daily_cap_usd,
                                                     "monthly_cap_usd": caps.monthly_cap_usd},
            "route_models": {name: r.model for name, r in sorted(self._router.routes.items())},
        })
        status, error, crashed = "completed", None, True
        try:
            yield
            if s.cursor < len(s.replay):  # the workflow finished without reaching a recorded step
                step = s.replay[s.cursor]
                raise ResumeDiverged(step["index"], {"route": step["route"], "prompt_sha256": step["prompt_sha256"]},
                                     {"run_ended": True})
            crashed = False
        except KillSwitchEngaged:
            status, error, crashed = "killed", "kill switch engaged", False
            raise
        except Exception as e:
            status, error, crashed = "failed", f"{type(e).__name__}: {e}", False
            raise
        finally:
            # A BaseException (the process is dying) writes nothing: the record stays
            # in_progress and is read as crashed by the next process.
            if not crashed:
                if status == "completed" and s.halt is not None:
                    status, error = s.halt  # a halt the caller caught still ends the run that way
                if record is not None:
                    record["status"], record["error"] = status, error
                    self.runs.save(record)
                self._event("run_closed", {"run_id": s.run_id, "status": status, "error": error})
            s.counts = s.family = s.record = s.replay = s.halt = s.run_id = None

    def note_checks(self, report: VerificationReport) -> None:
        """Add a verifier report to the current run's record (Slice 31)."""
        record = getattr(self._scope, "record", None)
        if record is not None:
            record["checks"].append({"index": len(record["checks"]), "passed": report.passed,
                                     "checks": [{"name": r.name, "passed": r.passed} for r in report.results]})
            self.runs.save(record)

    def _halt(self, status: str, error: str) -> None:
        if getattr(self._scope, "halt", None) is None:
            self._scope.halt = (status, error)

    def _shadow(self, request: RouteRequest, rules: RouteDecision, prompt: str) -> None:
        """Trace what the shadow policy would have chosen. It must never affect the run."""
        if self._trace is None:
            return
        try:
            shadow = self.shadow.decide(request, rules, len(prompt))
            rules_cost = est_input_cost(self._router.route(rules.route), len(prompt))
        except Exception as error:  # a shadow bug is recorded, never raised into the real run
            self._event("shadow_error", {"policy": self.shadow.name, "error_class": type(error).__name__})
            return
        self._event(
            "shadow_decision",
            {
                "policy": self.shadow.name,
                "task_family": getattr(self._scope, "family", None),
                "sensitivity_class": request.sensitivity_class,
                "difficulty": request.difficulty,
                "prior_failures": request.prior_failures,
                "prompt_chars": len(prompt),
                "rules_route": rules.route,
                "shadow_route": shadow.route,
                "agree": shadow.route == rules.route,
                "eligible": list(shadow.eligible),
                "rules_est_cost_usd": rules_cost,
                "shadow_est_cost_usd": shadow.est_cost_usd,
                "cost_basis": SHADOW_COST_BASIS,
            },
        )

    def _event(self, event_type: str, payload: dict) -> None:
        if self._trace is not None:
            self._trace.write(event_type, payload)

    def run_once(
        self,
        prompt: str,
        sensitivity_class: str,
        difficulty: str,
        *,
        max_cost_usd: float | None = None,
        max_retries: int = 1,
        checks: list[Check] | None = None,
    ) -> RunResult:
        with self.run_scope():
            return self._run_once(prompt, sensitivity_class, difficulty, max_cost_usd=max_cost_usd,
                                  max_retries=max_retries, checks=checks)

    def _run_once(
        self,
        prompt: str,
        sensitivity_class: str,
        difficulty: str,
        *,
        max_cost_usd: float | None,
        max_retries: int,
        checks: list[Check] | None,
    ) -> RunResult:
        if max_cost_usd is not None and max_cost_usd < 0:
            raise ValueError("max_cost_usd must be non-negative")
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            raise ValueError("max_retries must be a non-negative int")
        if checks is not None and not checks:
            raise ValueError("checks must be None or a non-empty list")
        self.manifests.verify()  # Slice 24: a changed guardrail manifest refuses the run
        # LOCAL_ONLY runs are exempt from the cost budget (local cost is 0.0).
        limit = None if sensitivity_class == "LOCAL_ONLY" else max_cost_usd

        self._event(
            "run_started",
            {
                "sensitivity_class": sensitivity_class,
                "difficulty": difficulty,
                "prompt_chars": len(prompt),
                "prompt_sha256": prompt_digest(prompt),  # Slice 31: replay re-derives loop detection
                "max_cost_usd": limit,
                "max_retries": max_retries,
            },
        )
        spent = 0.0
        try:
            request = RouteRequest(sensitivity_class, difficulty)
            first = decision = self._router.decide(request)
            self._shadow(request, first, prompt)  # Slice 30: one shadow decision per run
            failures = 0
            while True:
                try:
                    result, cost = self._call(prompt, decision, spent, limit, escalated=decision.route != first.route)
                    spent += cost
                    break
                except (NoClientConfigured, BudgetExceeded, ManifestTamperError, LoopDetected, KillSwitchEngaged,
                        ResumeDiverged):
                    raise  # halts, not route failures: never retried
                except Exception as error:
                    failures += 1
                    fail_closed = getattr(error, "fail_closed", False) is True
                    failure = {"route": decision.route, "model": decision.model,
                               "error_class": getattr(error, "error_class", type(error).__name__)}
                    if fail_closed:
                        failure["fail_closed"] = True  # Slice 36: recorded, so replay re-derives the stop
                    self._event("route_failure", failure)
                    if failures > max_retries:
                        raise
                    if fail_closed:
                        # Slices 35-36: the route is unavailable (no credential, or the account's quota
                        # is exhausted). The run stops here: retrying cannot help, and re-routing would
                        # substitute another backend.
                        raise
                    if sensitivity_class == "LOCAL_ONLY":
                        # LOCAL_ONLY retries reuse the same local route; never re-routed.
                        retry = decision
                    else:
                        retry = self._router.decide(
                            RouteRequest(sensitivity_class, difficulty, prior_failures=failures)
                        )
                        # No silent fallback (Slice 23): after a failure the only legal next
                        # route is the same one or the Router's escalation target. Never the
                        # local route, never another provider.
                        if retry.route not in (decision.route, "frontier_api"):
                            raise RoutingError(
                                f"refusing fallback from {decision.route!r} to {retry.route!r} after a failure"
                            ) from error
                    self._event(
                        "run_escalated" if retry.route != decision.route else "run_retried",
                        {"from_route": decision.route, "to_route": retry.route, "attempt": failures, "error": str(error)},
                    )
                    decision = retry
        except KillSwitchEngaged:
            self._halt("killed", "kill switch engaged")
            self._event("run_refused", {"reason": "kill switch engaged"})
            raise
        except Exception as e:
            self._halt("failed", f"{type(e).__name__}: {e}")
            self._halt_event(e, spent)
            raise

        # Verify real state, never the response: checks get no actor output.
        verified = False
        if checks is not None:
            report = Verifier(self._trace).verify(checks)
            self.note_checks(report)
            if not report.passed:
                self._halt("failed", "verification failed")
                self._event("verification_failed", {**report.to_dict(), "total_est_cost_usd": spent})
                raise VerificationFailed(report, spent)
            verified = True

        result = RunResult(
            result.response, result.route, result.model, result.latency_ms, result.escalated, spent, verified
        )
        self._event(
            "run_completed",
            {
                "route": result.route,
                "model": result.model,
                "escalated": result.escalated,
                "total_est_cost_usd": spent,
                "verified": verified,
            },
        )
        return result

    def _halt_event(self, e: Exception, spent: float) -> None:
        if isinstance(e, SpendCapReached):
            self._event("spend_cap_reached", {
                "cap": e.cap,
                "period": e.period,
                "spent_est_cost_usd": e.accumulated_cost_usd,
                "cap_usd": e.limit_usd,
                "next_call_est_cost_usd": e.next_call_est_usd,
            })
        elif isinstance(e, LoopDetected):
            self._event("loop_detected",
                        {"route": e.route, "prompt_sha256": e.prompt_sha256, "successful_calls": e.successful_calls})
        elif isinstance(e, BudgetExceeded):
            self._event("budget_exceeded", {
                "accumulated_est_cost_usd": e.accumulated_cost_usd,
                "limit_usd": e.limit_usd,
                "next_call_est_cost_usd": e.next_call_est_usd,
            })
        else:
            self._event("run_failed", {"error": f"{type(e).__name__}: {e}", "total_est_cost_usd": spent})

    def _call(
        self,
        prompt: str,
        decision: RouteDecision,
        spent: float,
        limit: float | None,
        escalated: bool,
    ) -> tuple[RunResult, float]:
        # Slice 29: every call, local or API, checks the file-backed kill switch, so a
        # multi-call run stops at its next call after /kill.
        if self._kill_switch is not None and self._kill_switch.engaged():
            raise KillSwitchEngaged()
        client = self._clients.get(decision.route)
        if client is None:
            raise NoClientConfigured(f"no client configured for route {decision.route!r}")
        rate = self._router.route(decision.route).est_cost_per_1k_tokens
        tokens_in = estimate_tokens(len(prompt))

        # Loop detection counts successful calls only, so retries of a failed call are exempt.
        counts = self._scope.counts
        key = (decision.route, prompt_digest(prompt))
        if counts[key] >= LOOP_LIMIT - 1:
            raise LoopDetected(decision.route, key[1], counts[key])

        # Pre-call projection uses input tokens only; output is unknown until the reply.
        if limit is not None:
            next_cost = estimate_cost(tokens_in, rate)
            if spent + next_cost > limit:
                raise BudgetExceeded(spent, limit, next_cost)

        # Slice 31: a resumed run reuses its recorded steps, in order; no model call is made.
        recorded = self._next_recorded_step(decision, key[1])
        if recorded is not None:
            self._event("step_replayed", {
                "run_id": self._scope.run_id, "index": recorded["index"], "route": decision.route,
                "model": decision.model, "prompt_sha256": key[1], "outcome": recorded["outcome"],
                "prompt_chars": len(prompt), "response_chars": len(recorded.get("response") or ""),
                "est_cost_usd": recorded["est_cost_usd"] if recorded["outcome"] == "ok" else 0.0,
            })
            if recorded["outcome"] == "error":
                raise ReplayedFailure(recorded.get("error_class") or "Exception")
            counts[key] += 1
            return (RunResult(recorded["response"], decision.route, decision.model, 0.0, escalated),
                    recorded["est_cost_usd"])

        route = self._router.route(decision.route)
        # Spend caps cover API routes only: local routes cost nothing.
        capped = self._budgets is not None and route.tier in API_TIERS
        if capped:
            self._check_spend_caps(estimate_cost(tokens_in, rate))
        egress = self._egress if route.tier in API_TIERS else None
        step = self._begin_step(decision, key[1], estimate_cost(tokens_in, rate))
        start = time.perf_counter()
        try:
            response = client.complete(prompt)
            if not isinstance(response, str):
                raise TypeError(f"client for route {decision.route!r} returned {type(response).__name__}, not str")
        except Exception as error:
            self._end_step(step, "error", error_class=type(error).__name__)
            if egress is not None:
                egress.record(route=decision.route, model=decision.model, host=destination_host(client, route.provider),
                              prompt=prompt, response=None, error_class=type(error).__name__)
            raise
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        if egress is not None:
            egress.record(route=decision.route, model=decision.model, host=destination_host(client, route.provider),
                          prompt=prompt, response=response)

        counts[key] += 1
        tokens_out = estimate_tokens(len(response))
        cost = estimate_cost(tokens_in + tokens_out, rate)
        self._end_step(step, "ok", cost=cost, response=response)
        self._event(
            "model_call",
            {
                "route": decision.route,
                "model": decision.model,
                "latency_ms": latency_ms,
                "prompt_chars": len(prompt),
                "prompt_sha256": key[1],  # Slice 31: digest, never the prompt
                "response_chars": len(response),
                "est_tokens_in": tokens_in,
                "est_tokens_out": tokens_out,
                "est_cost_usd": cost,
                "cost_basis": COST_BASIS,
            },
        )
        if capped:
            self._spend_alerts()
        return RunResult(response, decision.route, decision.model, latency_ms, escalated), cost

    # ---- Slice 31: steps in the run record ----

    def _next_recorded_step(self, decision: RouteDecision, digest: str) -> dict | None:
        s = self._scope
        if s.cursor >= len(s.replay):
            return None
        step = s.replay[s.cursor]
        expected = {"route": step["route"], "prompt_sha256": step["prompt_sha256"]}
        got = {"route": decision.route, "prompt_sha256": digest}
        if got != expected:  # the workflow did not re-derive the same call: never guess
            raise ResumeDiverged(step["index"], expected, got)
        s.cursor += 1
        return step

    def _begin_step(self, decision: RouteDecision, digest: str, est_cost: float) -> dict | None:
        record = self._scope.record
        if record is None:
            return None
        step = {"index": len(record["steps"]), "route": decision.route, "model": decision.model,
                "prompt_sha256": digest, "est_cost_usd": est_cost, "outcome": "in_flight",
                "at": datetime.now(timezone.utc).isoformat(), "resumed": bool(record["resumes"])}
        record["steps"].append(step)
        self.runs.save(record)  # written before the call: a crash leaves this as the last step
        return step

    def _end_step(self, step: dict | None, outcome: str, *, cost: float | None = None,
                  response: str | None = None, error_class: str | None = None) -> None:
        if step is None:
            return
        record = self._scope.record
        step["outcome"] = outcome
        if cost is not None:
            step["est_cost_usd"] = cost
        if error_class is not None:
            step["error_class"] = error_class
        if response is not None and record["workflow"] is not None:
            step["response"] = response  # stored only for resumable runs, so a resume can reuse it
        self.runs.save(record)

    # ---- Slice 29: spend caps and alerts, from the cost ledger (the trace) ----

    def _spend(self) -> tuple[str, float, str, float]:
        """(UTC day, its estimated spend, UTC month, its estimated spend)."""
        day = datetime.now(timezone.utc).date().isoformat()
        month = day[:7]
        by_day = CostLedger(self._trace.path).by_day() if self._trace.path.exists() else {}
        return day, by_day.get(day, 0.0), month, sum(v for d, v in by_day.items() if d.startswith(month))

    def _check_spend_caps(self, next_cost: float) -> None:
        day, today, month, this_month = self._spend()
        caps = self._budgets
        if caps.daily_cap_usd is not None and today + next_cost > caps.daily_cap_usd:
            raise SpendCapReached("daily", day, today, caps.daily_cap_usd, next_cost)
        if caps.monthly_cap_usd is not None and this_month + next_cost > caps.monthly_cap_usd:
            raise SpendCapReached("monthly", month, this_month, caps.monthly_cap_usd, next_cost)

    def _spend_alerts(self) -> None:
        """Trace spend_alert once per threshold per UTC day; the trace itself records what was sent."""
        cap = self._budgets.daily_cap_usd
        if not cap:
            return
        day, today, _, _ = self._spend()
        sent = {e["payload"].get("percent") for e in read_events(self._trace.path)
                if e.get("event_type") == "spend_alert" and e.get("payload", {}).get("day_utc") == day}
        for percent in SPEND_ALERT_PERCENTS:
            if today >= cap * percent / 100 and percent not in sent:
                self._event("spend_alert", {"percent": percent, "day_utc": day, "spent_est_cost_usd": today,
                                            "daily_cap_usd": cap})

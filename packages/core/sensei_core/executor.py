"""Executor: one routed model call per run, with traced retries and budgets.

Implements the Agent Runtime's Router -> Executor step (DESIGN.md §2)
and the first layer of Threat 7 (run budgets, cost accounting).
Prompt and response text are never written to the trace, only their
lengths (DESIGN.md Threat 4).
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from sensei_core.egress import API_TIERS, EgressLog, destination_host
from sensei_core.manifests import ManifestGuard, ManifestTamperError
from sensei_core.router import ModelClient, RouteDecision, RouteRequest, Router, RoutingError
from sensei_core.trace import TraceWriter
from sensei_core.verifier import Check, VerificationReport, Verifier

# Rough token estimate until real token counts arrive from clients.
CHARS_PER_TOKEN = 4
COST_BASIS = "estimated: tokens = ceil(chars / 4), cost = tokens / 1000 * est_cost_per_1k_tokens"


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
    ) -> None:
        self._router = router
        self._clients = dict(clients)
        self._trace = trace
        self._egress = egress  # Slice 23: API-route calls are logged here (sizes + digest, never content)
        # Slice 24: guardrail manifests pinned at construction (default: the shipped
        # tools.toml and models.toml), re-verified before every run.
        self.manifests = manifests if manifests is not None else ManifestGuard(trace=trace)

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
                "max_cost_usd": limit,
                "max_retries": max_retries,
            },
        )
        spent = 0.0
        try:
            first = decision = self._router.decide(RouteRequest(sensitivity_class, difficulty))
            failures = 0
            while True:
                try:
                    result, cost = self._call(prompt, decision, spent, limit, escalated=decision.route != first.route)
                    spent += cost
                    break
                except (NoClientConfigured, BudgetExceeded, ManifestTamperError):
                    raise
                except Exception as error:
                    failures += 1
                    self._event(
                        "route_failure",
                        {"route": decision.route, "model": decision.model, "error_class": type(error).__name__},
                    )
                    if failures > max_retries:
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
        except BudgetExceeded as e:
            self._event(
                "budget_exceeded",
                {
                    "accumulated_est_cost_usd": e.accumulated_cost_usd,
                    "limit_usd": e.limit_usd,
                    "next_call_est_cost_usd": e.next_call_est_usd,
                },
            )
            raise
        except Exception as e:
            self._event("run_failed", {"error": f"{type(e).__name__}: {e}", "total_est_cost_usd": spent})
            raise

        # Verify real state, never the response: checks get no actor output.
        verified = False
        if checks is not None:
            report = Verifier(self._trace).verify(checks)
            if not report.passed:
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

    def _call(
        self,
        prompt: str,
        decision: RouteDecision,
        spent: float,
        limit: float | None,
        escalated: bool,
    ) -> tuple[RunResult, float]:
        client = self._clients.get(decision.route)
        if client is None:
            raise NoClientConfigured(f"no client configured for route {decision.route!r}")
        rate = self._router.route(decision.route).est_cost_per_1k_tokens
        tokens_in = estimate_tokens(len(prompt))

        # Pre-call projection uses input tokens only; output is unknown until the reply.
        if limit is not None:
            next_cost = estimate_cost(tokens_in, rate)
            if spent + next_cost > limit:
                raise BudgetExceeded(spent, limit, next_cost)

        route = self._router.route(decision.route)
        egress = self._egress if route.tier in API_TIERS else None
        start = time.perf_counter()
        try:
            response = client.complete(prompt)
            if not isinstance(response, str):
                raise TypeError(f"client for route {decision.route!r} returned {type(response).__name__}, not str")
        except Exception as error:
            if egress is not None:
                egress.record(route=decision.route, model=decision.model, host=destination_host(client, route.provider),
                              prompt=prompt, response=None, error_class=type(error).__name__)
            raise
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        if egress is not None:
            egress.record(route=decision.route, model=decision.model, host=destination_host(client, route.provider),
                          prompt=prompt, response=response)

        tokens_out = estimate_tokens(len(response))
        cost = estimate_cost(tokens_in + tokens_out, rate)
        self._event(
            "model_call",
            {
                "route": decision.route,
                "model": decision.model,
                "latency_ms": latency_ms,
                "prompt_chars": len(prompt),
                "response_chars": len(response),
                "est_tokens_in": tokens_in,
                "est_tokens_out": tokens_out,
                "est_cost_usd": cost,
                "cost_basis": COST_BASIS,
            },
        )
        return RunResult(response, decision.route, decision.model, latency_ms, escalated), cost

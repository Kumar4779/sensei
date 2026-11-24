"""Executor: one routed model call per run, with traced escalation.

Implements the Agent Runtime's Router -> Executor step (DESIGN.md §2).
Prompt and response text are never written to the trace, only their
lengths (DESIGN.md Threat 4).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sensei_core.router import ModelClient, RouteDecision, RouteRequest, Router
from sensei_core.trace import TraceWriter


class NoClientConfigured(RuntimeError):
    """Raised when the router picks a route that has no client."""


@dataclass(frozen=True)
class RunResult:
    response: str
    route: str
    model: str
    latency_ms: float
    escalated: bool


class Executor:
    def __init__(
        self,
        router: Router,
        clients: dict[str, ModelClient],
        trace: TraceWriter | None = None,
    ) -> None:
        self._router = router
        self._clients = dict(clients)
        self._trace = trace

    def _event(self, event_type: str, payload: dict) -> None:
        if self._trace is not None:
            self._trace.write(event_type, payload)

    def run_once(self, prompt: str, sensitivity_class: str, difficulty: str) -> RunResult:
        self._event(
            "run_started",
            {
                "sensitivity_class": sensitivity_class,
                "difficulty": difficulty,
                "prompt_chars": len(prompt),
            },
        )
        try:
            decision = self._router.decide(RouteRequest(sensitivity_class, difficulty))
            try:
                result = self._call(prompt, decision, escalated=False)
            except NoClientConfigured:
                raise
            except Exception as first_error:
                # Only API_OK work that failed on cheap_api may retry; LOCAL_ONLY
                # failures never move to an API route.
                if sensitivity_class != "API_OK" or decision.route != "cheap_api":
                    raise
                retry = self._router.decide(
                    RouteRequest(sensitivity_class, difficulty, prior_failures=1)
                )
                escalated = retry.route != decision.route
                self._event(
                    "run_escalated" if escalated else "run_retried",
                    {"from_route": decision.route, "to_route": retry.route, "error": str(first_error)},
                )
                result = self._call(prompt, retry, escalated=escalated)
        except Exception as e:
            self._event("run_failed", {"error": f"{type(e).__name__}: {e}"})
            raise

        self._event(
            "run_completed",
            {"route": result.route, "model": result.model, "escalated": result.escalated},
        )
        return result

    def _call(self, prompt: str, decision: RouteDecision, escalated: bool) -> RunResult:
        client = self._clients.get(decision.route)
        if client is None:
            raise NoClientConfigured(f"no client configured for route {decision.route!r}")
        start = time.perf_counter()
        response = client.complete(prompt)
        latency_ms = round((time.perf_counter() - start) * 1000, 1)
        if not isinstance(response, str):
            raise TypeError(f"client for route {decision.route!r} returned {type(response).__name__}, not str")
        self._event(
            "model_call",
            {
                "route": decision.route,
                "model": decision.model,
                "latency_ms": latency_ms,
                "prompt_chars": len(prompt),
                "response_chars": len(response),
            },
        )
        return RunResult(response, decision.route, decision.model, latency_ms, escalated)

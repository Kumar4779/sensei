"""Router policy layer (DESIGN.md Principle 1, Decision 3, Threat 5).

Chooses a route per request: sensitivity first, then the cheapest
capable model, escalating to frontier only where the task earns it.
No network code: model access goes through the ModelClient protocol.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from sensei_core.config import VALID_SENSITIVITY, ConfigError, Route
from sensei_core.trace import TraceWriter

VALID_DIFFICULTY = frozenset({"simple", "standard", "hard"})
REQUIRED_ROUTES = ("local_private", "cheap_api", "frontier_api")


class RoutingError(RuntimeError):
    """Raised when a chosen route cannot legally receive the request."""


@dataclass(frozen=True)
class RouteRequest:
    sensitivity_class: str
    difficulty: str
    prior_failures: int = 0

    def __post_init__(self) -> None:
        if self.sensitivity_class not in VALID_SENSITIVITY:
            raise ValueError(f"unknown sensitivity_class {self.sensitivity_class!r}")
        if self.difficulty not in VALID_DIFFICULTY:
            raise ValueError(f"unknown difficulty {self.difficulty!r}")
        if isinstance(self.prior_failures, bool) or not isinstance(self.prior_failures, int) or self.prior_failures < 0:
            raise ValueError("prior_failures must be a non-negative int")


@dataclass(frozen=True)
class RouteDecision:
    route: str
    model: str
    reason: str


class Router:
    """Applies routing rules to requests; traces every decision."""

    def __init__(self, routes: dict[str, Route], trace: TraceWriter | None = None) -> None:
        missing = [name for name in REQUIRED_ROUTES if name not in routes]
        if missing:
            raise ConfigError(f"router requires routes {missing}")
        self._routes = dict(routes)
        self._trace = trace

    def decide(self, request: RouteRequest) -> RouteDecision:
        route_name, reason = self._choose(request)
        route = self._routes[route_name]
        # Defence in depth: config validation should make this unreachable.
        if request.sensitivity_class not in route.sensitivity:
            raise RoutingError(
                f"route {route_name!r} may not receive {request.sensitivity_class!r} content"
            )
        decision = RouteDecision(route=route_name, model=route.model, reason=reason)
        if self._trace is not None:
            self._trace.write(
                "route_decision",
                {
                    "sensitivity_class": request.sensitivity_class,
                    "difficulty": request.difficulty,
                    "prior_failures": request.prior_failures,
                    "route": decision.route,
                    "model": decision.model,
                    "reason": decision.reason,
                },
            )
        return decision

    @staticmethod
    def _choose(request: RouteRequest) -> tuple[str, str]:
        if request.sensitivity_class == "LOCAL_ONLY":
            return "local_private", "sensitivity: LOCAL_ONLY content stays on the local model"
        if request.difficulty == "hard":
            return "frontier_api", "task earns frontier: hard difficulty"
        if request.prior_failures >= 1 and request.difficulty != "simple":
            return "frontier_api", f"escalation after failure: {request.prior_failures} prior failure(s)"
        return "cheap_api", "cheapest capable default"


@runtime_checkable
class ModelClient(Protocol):
    def complete(self, prompt: str) -> str: ...


@dataclass
class FakeClient:
    """Returns a canned response and records prompts. For tests and demos."""

    response: str = "fake response"
    prompts: list[str] = field(default_factory=list)

    def complete(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response

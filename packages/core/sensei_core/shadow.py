"""Shadow router (Slice 30): a cost-aware policy decides alongside the rules Router.

Shadow verification, applied to routing: on every Executor run the
ShadowPolicy makes its own choice from the same inputs the Router saw. Only
the rules decision executes. The shadow's choice is traced
("shadow_decision") and NEVER called, so the policy earns trust from a
measured agreement record before it is ever allowed to decide.

Decision rule, "cheapest-eligible-v1":
  1. Eligible routes are those the rules allow for this request. The shadow
     inherits every hard guardrail:
     - Sensitivity (Threat 5): the route's sensitivity list must contain the
       request's class, the same check that makes Router.decide raise
       RoutingError. LOCAL_ONLY content additionally requires a local-tier
       route: LOCAL_ONLY means local, always.
     - Difficulty: hard API_OK work is frontier-tier only ("task earns
       frontier" in the rules).
     - No silent fallback (Slice 23): after a failure, only the rules' route or
       frontier_api, never a cheaper or local route.
  2. Among eligible routes, pick the lowest estimated input cost:
     ceil(prompt_chars / 4) tokens * est_cost_per_1k_tokens / 1000, read from
     models.toml. That is the field the rules Router ignores. Ties go to the
     rules' route, then to the route name, so the choice is deterministic.
  3. If nothing is eligible (unreachable with a valid models.toml), the shadow
     takes the rules' route.
"""

from __future__ import annotations

from dataclasses import dataclass

from sensei_core.config import Route
from sensei_core.router import RouteDecision, RouteRequest

POLICY = "cheapest-eligible-v1"
CHARS_PER_TOKEN = 4  # the Executor's estimate (executor.CHARS_PER_TOKEN)
COST_BASIS = "estimated input tokens only: ceil(prompt_chars / 4) / 1000 * est_cost_per_1k_tokens"


def est_input_cost(route: Route, prompt_chars: int) -> float:
    tokens = -(-prompt_chars // CHARS_PER_TOKEN)  # ceil
    return tokens / 1000 * route.est_cost_per_1k_tokens


@dataclass(frozen=True)
class ShadowDecision:
    route: str
    eligible: tuple[str, ...]
    est_cost_usd: float


class ShadowPolicy:
    name = POLICY

    def __init__(self, routes: dict[str, Route]) -> None:
        self._routes = dict(routes)

    def eligible(self, request: RouteRequest, rules_route: str) -> list[str]:
        out = []
        for name, route in sorted(self._routes.items()):
            if request.sensitivity_class not in route.sensitivity:
                continue
            if request.sensitivity_class == "LOCAL_ONLY":
                if route.tier != "local":
                    continue
            else:
                if request.difficulty == "hard" and route.tier != "frontier_api":
                    continue
                if request.prior_failures >= 1 and name not in (rules_route, "frontier_api"):
                    continue
            out.append(name)
        return out

    def decide(self, request: RouteRequest, rules: RouteDecision, prompt_chars: int) -> ShadowDecision:
        eligible = self.eligible(request, rules.route)
        if not eligible:
            choice = rules.route
        else:
            choice = min(eligible, key=lambda n: (est_input_cost(self._routes[n], prompt_chars),
                                                  n != rules.route, n))
        return ShadowDecision(choice, tuple(eligible), est_input_cost(self._routes[choice], prompt_chars))

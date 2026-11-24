"""Control Plane permission gate (DESIGN.md Principle 3, Threats 1, 2 and 5).

Tool permissions are loaded from a TOML manifest. The gate decides each
call as ALLOW, HALT_FOR_APPROVAL or DENY; sensitivity-class violations
are DENY and cannot be approved away.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from sensei_core.config import VALID_SENSITIVITY, ConfigError
from sensei_core.trace import TraceWriter

DEFAULT_TOOLS_PATH = Path(__file__).with_name("tools.toml")


class Tier(str, Enum):
    READ = "read"
    REVERSIBLE = "reversible"
    IRREVERSIBLE = "irreversible"


class Outcome(str, Enum):
    ALLOW = "ALLOW"
    HALT_FOR_APPROVAL = "HALT_FOR_APPROVAL"
    DENY = "DENY"


@dataclass(frozen=True)
class ToolSpec:
    name: str
    tier: Tier
    sensitivity: frozenset[str]
    endpoints: tuple[str, ...]


@dataclass(frozen=True)
class Decision:
    tool: str
    sensitivity_class: str
    outcome: Outcome
    reason: str


def _parse_tool(name: str, raw: Any) -> ToolSpec:
    if not isinstance(raw, dict):
        raise ConfigError(f"tool {name!r} must be a table")
    for key in ("tier", "sensitivity", "endpoints"):
        if key not in raw:
            raise ConfigError(f"tool {name!r} is missing {key!r}")

    try:
        tier = Tier(raw["tier"])
    except ValueError:
        raise ConfigError(
            f"tool {name!r} has unknown tier {raw['tier']!r}; "
            f"expected one of {[t.value for t in Tier]}"
        ) from None

    sensitivity = raw["sensitivity"]
    if not isinstance(sensitivity, list) or not sensitivity:
        raise ConfigError(f"tool {name!r} sensitivity must be a non-empty list")
    unknown = set(sensitivity) - VALID_SENSITIVITY
    if unknown:
        raise ConfigError(f"tool {name!r} has unknown sensitivity classes {sorted(unknown)}")

    endpoints = raw["endpoints"]
    if not isinstance(endpoints, list) or not all(isinstance(e, str) for e in endpoints):
        raise ConfigError(f"tool {name!r} endpoints must be a list of strings")
    if endpoints and "LOCAL_ONLY" in sensitivity:
        raise ConfigError(f"tool {name!r} has egress endpoints but allows LOCAL_ONLY content")

    return ToolSpec(
        name=name,
        tier=tier,
        sensitivity=frozenset(sensitivity),
        endpoints=tuple(endpoints),
    )


def load_tools(path: str | Path = DEFAULT_TOOLS_PATH) -> dict[str, ToolSpec]:
    """Load and validate a tool manifest; raise ConfigError if invalid."""
    with Path(path).open("rb") as f:
        data = tomllib.load(f)
    tools = data.get("tools")
    if not isinstance(tools, dict) or not tools:
        raise ConfigError("tool manifest must define at least one [tools.<name>] table")
    return {name: _parse_tool(name, raw) for name, raw in tools.items()}


class PermissionGate:
    """Decides tool calls against the manifest; traces every decision."""

    def __init__(self, tools: dict[str, ToolSpec], trace: TraceWriter | None = None) -> None:
        self._tools = dict(tools)
        self._trace = trace

    def decide(self, tool_name: str, sensitivity_class: str) -> Decision:
        decision = self._decide(tool_name, sensitivity_class)
        if self._trace is not None:
            self._trace.write(
                "permission_decision",
                {
                    "tool": decision.tool,
                    "sensitivity_class": decision.sensitivity_class,
                    "outcome": decision.outcome.value,
                    "reason": decision.reason,
                },
            )
        return decision

    def _decide(self, tool_name: str, sensitivity_class: str) -> Decision:
        def result(outcome: Outcome, reason: str) -> Decision:
            return Decision(tool_name, sensitivity_class, outcome, reason)

        spec = self._tools.get(tool_name)
        if spec is None:
            return result(Outcome.DENY, f"{tool_name!r} is not in manifest")
        if sensitivity_class not in spec.sensitivity:
            return result(
                Outcome.DENY,
                f"{tool_name!r} does not accept {sensitivity_class!r} content "
                f"(allowed: {sorted(spec.sensitivity)}); class violations cannot be approved",
            )
        if spec.tier is Tier.IRREVERSIBLE:
            return result(
                Outcome.HALT_FOR_APPROVAL,
                f"{tool_name!r} is irreversible-tier; user approval required",
            )
        return result(Outcome.ALLOW, f"{tool_name!r} is {spec.tier.value}-tier and accepts {sensitivity_class!r}")

"""Model routing table loaded from TOML (DESIGN.md Principle 1, Threat 5)."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VALID_TIERS = frozenset({"local", "cheap_api", "frontier_api"})
VALID_SENSITIVITY = frozenset({"LOCAL_ONLY", "API_OK"})
# Higher rank = more sensitive. A route's max_sensitivity caps what it may receive.
SENSITIVITY_RANK = {"API_OK": 0, "LOCAL_ONLY": 1}
DEFAULT_MODELS_PATH = Path(__file__).with_name("models.toml")


class ConfigError(ValueError):
    """Raised when a routing table is malformed."""


@dataclass(frozen=True)
class Route:
    name: str
    provider: str
    model: str
    tier: str
    sensitivity: frozenset[str]
    max_sensitivity: str
    est_cost_per_1k_tokens: float


def _parse_route(name: str, raw: Any) -> Route:
    if not isinstance(raw, dict):
        raise ConfigError(f"route {name!r} must be a table")
    for key in ("provider", "model", "tier", "sensitivity", "max_sensitivity", "est_cost_per_1k_tokens"):
        if key not in raw:
            raise ConfigError(f"route {name!r} is missing {key!r}")

    tier = raw["tier"]
    if tier not in VALID_TIERS:
        raise ConfigError(
            f"route {name!r} has unknown tier {tier!r}; expected one of {sorted(VALID_TIERS)}"
        )

    sensitivity = raw["sensitivity"]
    if not isinstance(sensitivity, list) or not sensitivity:
        raise ConfigError(f"route {name!r} sensitivity must be a non-empty list")
    unknown = set(sensitivity) - VALID_SENSITIVITY
    if unknown:
        raise ConfigError(f"route {name!r} has unknown sensitivity classes {sorted(unknown)}")
    if "LOCAL_ONLY" in sensitivity and tier != "local":
        raise ConfigError(f"route {name!r} is tier {tier!r} but allows LOCAL_ONLY content")

    max_sensitivity = raw["max_sensitivity"]
    if max_sensitivity not in VALID_SENSITIVITY:
        raise ConfigError(f"route {name!r} has unknown max_sensitivity {max_sensitivity!r}")
    if max_sensitivity not in sensitivity:
        raise ConfigError(f"route {name!r} max_sensitivity {max_sensitivity!r} is not in its sensitivity list")
    above = [c for c in sensitivity if SENSITIVITY_RANK[c] > SENSITIVITY_RANK[max_sensitivity]]
    if above:
        raise ConfigError(f"route {name!r} allows {sorted(above)}, above its max_sensitivity {max_sensitivity!r}")

    cost = raw["est_cost_per_1k_tokens"]
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or cost < 0:
        raise ConfigError(f"route {name!r} est_cost_per_1k_tokens must be a non-negative number")

    return Route(
        name=name,
        provider=str(raw["provider"]),
        model=str(raw["model"]),
        tier=tier,
        sensitivity=frozenset(sensitivity),
        max_sensitivity=max_sensitivity,
        est_cost_per_1k_tokens=float(cost),
    )


def load_models(path: str | Path = DEFAULT_MODELS_PATH) -> dict[str, Route]:
    """Load and validate a routing table; raise ConfigError if invalid."""
    with Path(path).open("rb") as f:
        data = tomllib.load(f)
    routes = data.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise ConfigError("models file must define at least one [routes.<name>] table")
    return {name: _parse_route(name, raw) for name, raw in routes.items()}

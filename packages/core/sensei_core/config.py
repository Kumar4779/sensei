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


def _read_manifest(path: str | Path) -> dict[str, Any]:
    """Parse a TOML manifest, refusing one that changed after this process pinned it (Slice 24)."""
    from sensei_core.manifests import check_unchanged

    data = Path(path).read_bytes()
    check_unchanged(path, data)
    return tomllib.loads(data.decode("utf-8"))


@dataclass(frozen=True)
class Route:
    name: str
    provider: str
    model: str
    tier: str
    sensitivity: frozenset[str]
    max_sensitivity: str
    est_cost_per_1k_tokens: float
    think: bool | None = None  # None = omit, use the server's default


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

    think = raw.get("think")
    if think is not None and not isinstance(think, bool):
        raise ConfigError(f"route {name!r} think must be true or false when set")

    return Route(
        name=name,
        provider=str(raw["provider"]),
        model=str(raw["model"]),
        tier=tier,
        sensitivity=frozenset(sensitivity),
        max_sensitivity=max_sensitivity,
        est_cost_per_1k_tokens=float(cost),
        think=think,
    )


def load_models(path: str | Path = DEFAULT_MODELS_PATH) -> dict[str, Route]:
    """Load and validate a routing table; raise ConfigError if invalid."""
    data = _read_manifest(path)
    routes = data.get("routes")
    if not isinstance(routes, dict) or not routes:
        raise ConfigError("models file must define at least one [routes.<name>] table")
    return {name: _parse_route(name, raw) for name, raw in routes.items()}


@dataclass(frozen=True)
class EmbeddingsConfig:
    """The [embeddings] table: the model behind the Custodian's derived index."""

    provider: str
    model: str
    search_min_score: float
    dedup_threshold: float


def load_embeddings(path: str | Path = DEFAULT_MODELS_PATH) -> EmbeddingsConfig:
    """Load and validate the [embeddings] table; raise ConfigError if missing or invalid."""
    raw = _read_manifest(path).get("embeddings")
    if not isinstance(raw, dict):
        raise ConfigError("models file must define an [embeddings] table")
    for key in ("provider", "model", "search_min_score", "dedup_threshold"):
        if key not in raw:
            raise ConfigError(f"[embeddings] is missing {key!r}")
    if raw["provider"] != "ollama":
        raise ConfigError(f"[embeddings] provider {raw['provider']!r} is not supported (local only: 'ollama')")
    for key in ("search_min_score", "dedup_threshold"):
        v = raw[key]
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0.0 <= v <= 1.0:
            raise ConfigError(f"[embeddings] {key} must be a number in [0, 1]")
    return EmbeddingsConfig(str(raw["provider"]), str(raw["model"]), float(raw["search_min_score"]),
                            float(raw["dedup_threshold"]))


@dataclass(frozen=True)
class SpendCaps:
    """The [budgets] table (Slice 29): estimated-USD caps on API-route spend. None = no cap."""

    daily_cap_usd: float | None = None
    monthly_cap_usd: float | None = None

    @property
    def any(self) -> bool:
        return self.daily_cap_usd is not None or self.monthly_cap_usd is not None


BUDGET_KEYS = ("daily_cap_usd", "monthly_cap_usd")


def load_budgets(path: str | Path = DEFAULT_MODELS_PATH) -> SpendCaps:
    """Load the optional [budgets] table; a missing table or key means no cap."""
    raw = _read_manifest(path).get("budgets", {})
    if not isinstance(raw, dict):
        raise ConfigError("[budgets] must be a table")
    unknown = set(raw) - set(BUDGET_KEYS)
    if unknown:  # a misspelt cap must not silently mean "no cap"
        raise ConfigError(f"[budgets] has unknown keys {sorted(unknown)}; expected {list(BUDGET_KEYS)}")
    for key, v in raw.items():
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ConfigError(f"[budgets] {key} must be a non-negative number")
    return SpendCaps(*(float(raw[k]) if k in raw else None for k in BUDGET_KEYS))

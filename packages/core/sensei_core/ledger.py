"""Read-only cost ledger over trace files (DESIGN.md Threat 7).

Sums the estimated cost of model_call events by route and by UTC day.
Basis for the weekly cost-by-task-type review. Never writes.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from sensei_core.trace import read_events


def _utc_day(event: dict) -> str:
    return datetime.fromisoformat(event["timestamp"]).astimezone(timezone.utc).date().isoformat()


class CostLedger:
    def __init__(self, trace_path: str | Path) -> None:
        self._calls = [
            e
            for e in read_events(trace_path)
            if e.get("event_type") == "model_call"
            and isinstance(e.get("payload", {}).get("est_cost_usd"), (int, float))
        ]

    @property
    def total_est_cost_usd(self) -> float:
        return sum(e["payload"]["est_cost_usd"] for e in self._calls)

    def by_route(self, day: str | None = None) -> dict[str, float]:
        """Totals by route; with day ('YYYY-MM-DD', UTC), only that day's calls."""
        totals: dict[str, float] = defaultdict(float)
        for e in self._calls:
            if day is not None and _utc_day(e) != day:
                continue
            totals[e["payload"].get("route", "unknown")] += e["payload"]["est_cost_usd"]
        return dict(sorted(totals.items()))

    def by_day(self) -> dict[str, float]:
        """Totals keyed by UTC date, 'YYYY-MM-DD'."""
        totals: dict[str, float] = defaultdict(float)
        for e in self._calls:
            totals[_utc_day(e)] += e["payload"]["est_cost_usd"]
        return dict(sorted(totals.items()))

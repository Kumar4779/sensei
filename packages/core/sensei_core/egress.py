"""Egress log (Slice 23): one line per model call that leaves the machine.

Every call on an API route (tier cheap_api or frontier_api) appends a line to
<SENSEI_HOME>/egress.jsonl: timestamp, route, model, destination host,
request and response sizes in bytes (UTF-8 payload, not HTTP framing), and a
SHA-256 digest of the outbound prompt, so a given prompt can later be matched
to its call without the log ever holding it. Content is NEVER written: no
prompt, no response. Local routes write nothing.

Failed API calls are logged too (ok=false, response_bytes=0, error_class), because a
request may already have left the machine before the failure.
"""

from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_TIERS = frozenset({"cheap_api", "frontier_api"})


def prompt_digest(prompt: str) -> str:
    return "sha256:" + hashlib.sha256(prompt.encode("utf-8")).hexdigest()


class EgressLog:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def record(
        self,
        *,
        route: str,
        model: str,
        host: str,
        prompt: str,
        response: str | None,
        error_class: str | None = None,
    ) -> dict[str, Any]:
        line = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "route": route,
            "model": model,
            "destination_host": host,
            "request_bytes": len(prompt.encode("utf-8")),
            "response_bytes": 0 if response is None else len(response.encode("utf-8")),
            "prompt_sha256": prompt_digest(prompt),
            "ok": error_class is None,
        }
        if error_class is not None:
            line["error_class"] = error_class
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(line, sort_keys=True) + "\n")
        return line

    def lines(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        with self.path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def summary(self, day: str) -> dict[str, Any]:
        """Calls and bytes per destination for one UTC day ('YYYY-MM-DD')."""
        by_dest: dict[str, dict[str, int]] = defaultdict(lambda: {"calls": 0, "request_bytes": 0,
                                                                  "response_bytes": 0, "total_bytes": 0})
        for line in self.lines():
            if datetime.fromisoformat(line["timestamp"]).astimezone(timezone.utc).date().isoformat() != day:
                continue
            d = by_dest[line["destination_host"]]
            d["calls"] += 1
            d["request_bytes"] += line["request_bytes"]
            d["response_bytes"] += line["response_bytes"]
            d["total_bytes"] += line["request_bytes"] + line["response_bytes"]
        return {"day_utc": day, "by_destination": dict(sorted(by_dest.items()))}


def destination_host(client: object, provider: str) -> str:
    """The client's declared destination_host, else the route's provider name (no real API client yet)."""
    host = getattr(client, "destination_host", None)
    return host if isinstance(host, str) and host else provider

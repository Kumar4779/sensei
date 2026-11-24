"""Append-only JSONL trace store with write-time redaction.

Implements DESIGN.md Principle 4 ("Everything is a trace") and the
write-time redaction policy of Threat 4. One JSON object per line; the
file is only ever opened in append mode, never rewritten.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REDACTED = "[REDACTED]"

# Each pattern either redacts the whole match, or keeps a named "keep"
# prefix (e.g. "Bearer ", "password=") so the trace stays readable.
_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{8,}"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"(?P<keep>\bBearer\s+)[A-Za-z0-9._~+/=\-]+"),
    re.compile(r"(?i)(?P<keep>\bpassword\s*=\s*)(?:\"[^\"]*\"|'[^']*'|\S+)"),
)


def _redact_string(value: str) -> str:
    for pattern in _SECRET_PATTERNS:
        if "keep" in pattern.groupindex:
            value = pattern.sub(lambda m: m.group("keep") + REDACTED, value)
        else:
            value = pattern.sub(REDACTED, value)
    return value


def _redact(value: Any) -> Any:
    if isinstance(value, str):
        return _redact_string(value)
    if isinstance(value, dict):
        return {k: _redact(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact(v) for v in value]
    return value


class TraceWriter:
    """Appends redacted trace records to a JSONL file."""

    def __init__(self, path: str | Path, run_id: str, session_id: str) -> None:
        self.path = Path(path)
        self.run_id = run_id
        self.session_id = session_id
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        event_type: str,
        payload: dict[str, Any] | None = None,
        cost_usd: float | None = None,
    ) -> dict[str, Any]:
        """Redact and append one record; return the record as written."""
        if payload is not None and not isinstance(payload, dict):
            raise TypeError("payload must be a dict")
        record = _redact(
            {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "run_id": self.run_id,
                "session_id": self.session_id,
                "event_type": event_type,
                "payload": payload or {},
                "cost_usd": cost_usd,
            }
        )
        # Serialise before opening so a bad payload never leaves a partial line.
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self.path.open("a", encoding="utf-8") as f:
            f.write(line)
        return record


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """Parse a JSONL trace file back into a list of records."""
    with Path(path).open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]

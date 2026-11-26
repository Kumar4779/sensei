"""Persistent kill switch (Slice 29, DESIGN.md Threat 7).

The killed state is a file, <SENSEI_HOME>/killed.json, not process memory, so
a restart cannot clear it: a gateway started while the file exists starts
KILLED. engage() writes it, disengage() removes it, and engaged() re-reads it
on every check.

Fails closed: a killed.json that exists but cannot be read or parsed, or holds
anything other than {"killed": true, ...}, counts as killed and traces
"kill_state_unreadable". Only a missing file means "not killed".
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from sensei_core.trace import TraceWriter

KILL_FILE = "killed.json"


class KillSwitchEngaged(RuntimeError):
    """The kill switch is engaged; no model call may be made."""

    def __init__(self) -> None:
        super().__init__("kill switch engaged")


class KillSwitch:
    def __init__(self, path: str | Path, trace: TraceWriter | None = None) -> None:
        self.path = Path(path)
        self._trace = trace

    def _unreadable(self, reason: str) -> bool:
        if self._trace is not None:
            self._trace.write("kill_state_unreadable", {"path": str(self.path), "reason": reason})
        return True

    def engaged(self) -> bool:
        try:
            raw = self.path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return False
        except (OSError, UnicodeDecodeError) as e:  # a directory, no permission, bad bytes: fail closed
            return self._unreadable(type(e).__name__)
        try:
            data = json.loads(raw)
        except ValueError:
            return self._unreadable("invalid JSON")
        if not isinstance(data, dict) or data.get("killed") is not True:
            return self._unreadable("unexpected content")
        return True

    def engage(self) -> None:
        """Write killed.json atomically, so a crash mid-write never leaves a half file."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"killed": True, "engaged_at": datetime.now(timezone.utc).isoformat()}) + "\n",
                       encoding="utf-8")
        os.replace(tmp, self.path)

    def disengage(self) -> None:
        self.path.unlink(missing_ok=True)

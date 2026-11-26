"""File-backed memory store v1 (DESIGN.md Decision 5, Threat 3).

Layout under the store root:
  entries/<id>.json            one entry, human-readable and diffable
  entries/<id>.history.jsonl   previous versions of that entry (for rollback)
  changelog.jsonl              append-only log of every mutation

Fact text lives only in an entry's own two files, never in the shared
changelog, so delete() removes every copy of a fact and deletion stays
provable: after delete, no file under the root contains it.

Write gating (Threat 3): user facts in "general" are active at once;
tool-content facts are only ever proposed; permission, money and
trusted-contact facts always wait for confirmation.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sensei_core.config import VALID_SENSITIVITY
from sensei_core.private import append_private, private_dir, write_private

SOURCES = frozenset({"user", "tool_content"})
CATEGORIES = frozenset({"general", "permission", "money", "trusted_contact"})
GATED_CATEGORIES = frozenset({"permission", "money", "trusted_contact"})
STATUSES = frozenset({"active", "proposed", "pending_confirmation"})
_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status_for(source: str, category: str) -> str:
    if category in GATED_CATEGORIES:
        return "pending_confirmation"
    if source == "tool_content":
        return "proposed"
    return "active"


class MemoryStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.entries_dir = self.root / "entries"
        self.changelog_path = self.root / "changelog.jsonl"
        # Slice 33: memory is personal facts; the store's directories are owner-only.
        private_dir(self.root)
        private_dir(self.entries_dir)

    # ---- the only two ways in -------------------------------------------

    def write(
        self,
        fact: str,
        *,
        origin: str = "user statement",
        category: str = "general",
        sensitivity: str = "LOCAL_ONLY",
    ) -> dict[str, Any]:
        """Record a fact the user stated themselves."""
        return self._add("add", fact, "user", origin, category, sensitivity)

    def propose(
        self,
        fact: str,
        *,
        origin: str,
        category: str = "general",
        sensitivity: str = "LOCAL_ONLY",
    ) -> dict[str, Any]:
        """Record a fact derived from tool content; never active until confirmed."""
        return self._add("propose", fact, "tool_content", origin, category, sensitivity)

    # ---- mutations ------------------------------------------------------

    def confirm(self, entry_id: str) -> dict[str, Any]:
        """User confirmation: proposed or pending entries become active."""
        entry = self.get(entry_id)
        if entry["status"] == "active":
            return entry
        previous_status = entry["status"]
        entry["status"] = "active"
        entry["updated_at"] = _now()
        self._save(entry)
        self._log("confirm", entry_id, {"from_status": previous_status})
        return entry

    def update(self, entry_id: str, new_fact: str) -> dict[str, Any]:
        """Replace an entry's fact, keeping the previous version for rollback.

        Status is re-derived from the gating rules, so a changed tool-content
        or gated fact needs confirmation again.
        """
        self._check_fact(new_fact)
        entry = self.get(entry_id)
        self._append_history(entry_id, entry)
        previous_status = entry["status"]
        entry["fact"] = new_fact
        entry["status"] = _status_for(entry["source"], entry["category"])
        entry["updated_at"] = _now()
        self._save(entry)
        self._log("update", entry_id, {"from_status": previous_status, "status": entry["status"]})
        return entry

    def rollback(self, entry_id: str) -> dict[str, Any]:
        """Restore the most recent previous version of an entry."""
        current = self.get(entry_id)
        history = self._read_history(entry_id)
        if not history:
            raise ValueError(f"memory entry {entry_id!r} has no previous version")
        restored = history.pop()
        restored["updated_at"] = _now()
        self._write_history(entry_id, history)
        self._save(restored)
        self._log(
            "rollback",
            entry_id,
            {"from_status": current["status"], "status": restored["status"], "versions_left": len(history)},
        )
        return restored

    def delete(self, entry_id: str) -> None:
        """Remove every file holding this entry and log a tombstone."""
        entry = self.get(entry_id)
        self._entry_path(entry_id).unlink()
        self._history_path(entry_id).unlink(missing_ok=True)
        self._log("delete", entry_id, {"tombstone": True, "category": entry["category"], "source": entry["source"]})

    # ---- reads ----------------------------------------------------------

    def get(self, entry_id: str) -> dict[str, Any]:
        path = self._entry_path(entry_id)
        if not path.exists():
            raise KeyError(f"no memory entry {entry_id!r}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_beliefs(self) -> list[dict[str, Any]]:
        """Active entries only: what Sensei currently believes."""
        return [e for e in self._all() if e["status"] == "active"]

    def list_pending(self) -> list[dict[str, Any]]:
        """Proposed and pending-confirmation entries awaiting the user."""
        return [e for e in self._all() if e["status"] != "active"]

    def probe(self, text: str) -> list[dict[str, Any]]:
        """Active entries whose fact contains text (case-insensitive)."""
        if not text:
            raise ValueError("probe text must be non-empty")
        needle = text.casefold()
        return [e for e in self.list_beliefs() if needle in e["fact"].casefold()]

    def changelog(self) -> list[dict[str, Any]]:
        if not self.changelog_path.exists():
            return []
        with self.changelog_path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    # ---- internals ------------------------------------------------------

    def _add(
        self, action: str, fact: str, source: str, origin: str, category: str, sensitivity: str
    ) -> dict[str, Any]:
        self._check_fact(fact)
        if category not in CATEGORIES:
            raise ValueError(f"unknown category {category!r}; expected one of {sorted(CATEGORIES)}")
        if sensitivity not in VALID_SENSITIVITY:
            raise ValueError(f"unknown sensitivity {sensitivity!r}")
        if not isinstance(origin, str) or not origin.strip():
            raise ValueError("origin must be a non-empty string")
        now = _now()
        entry = {
            "id": uuid.uuid4().hex,
            "fact": fact,
            "source": source,
            "origin": origin,
            "category": category,
            "sensitivity": sensitivity,
            "status": _status_for(source, category),
            "created_at": now,
            "updated_at": now,
        }
        self._save(entry)
        self._log(action, entry["id"], {"source": source, "category": category, "status": entry["status"]})
        return entry

    @staticmethod
    def _check_fact(fact: str) -> None:
        if not isinstance(fact, str) or not fact.strip():
            raise ValueError("fact must be a non-empty string")

    def _entry_path(self, entry_id: str) -> Path:
        if not isinstance(entry_id, str) or not _ID_RE.match(entry_id):
            raise KeyError(f"invalid memory entry id {entry_id!r}")
        return self.entries_dir / f"{entry_id}.json"

    def _history_path(self, entry_id: str) -> Path:
        return self._entry_path(entry_id).with_suffix(".history.jsonl")

    def _save(self, entry: dict[str, Any]) -> None:
        path = self._entry_path(entry["id"])
        write_private(path, json.dumps(entry, indent=2, sort_keys=True, ensure_ascii=False) + "\n")

    def _append_history(self, entry_id: str, entry: dict[str, Any]) -> None:
        append_private(self._history_path(entry_id), json.dumps(entry, sort_keys=True, ensure_ascii=False) + "\n")

    def _read_history(self, entry_id: str) -> list[dict[str, Any]]:
        path = self._history_path(entry_id)
        if not path.exists():
            return []
        with path.open(encoding="utf-8") as f:
            return [json.loads(line) for line in f if line.strip()]

    def _write_history(self, entry_id: str, history: list[dict[str, Any]]) -> None:
        path = self._history_path(entry_id)
        if not history:
            path.unlink(missing_ok=True)
            return
        write_private(path, "".join(json.dumps(h, sort_keys=True, ensure_ascii=False) + "\n" for h in history))

    def _all(self) -> list[dict[str, Any]]:
        entries = [json.loads(p.read_text(encoding="utf-8")) for p in self.entries_dir.glob("*.json")]
        return sorted(entries, key=lambda e: e["created_at"])

    def _log(self, action: str, entry_id: str, detail: dict[str, Any]) -> None:
        record = {"ts": _now(), "action": action, "entry_id": entry_id, "detail": detail}
        append_private(self.changelog_path, json.dumps(record, ensure_ascii=False) + "\n")

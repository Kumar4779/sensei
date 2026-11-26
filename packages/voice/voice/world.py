"""Fake, in-memory Sensei tools (Voice Lab Slice 1), plus the tool layer that enforces the echo.

World is the end state the bench checks: notes, calendar, pending approvals,
invoices, forms, and the outbox and payments ledger that irreversible actions
write to. Every value is fixed (today is Monday 2026-11-30), so end states are
deterministic.

A tool that raises becomes a ToolFailed (Slice 3). It is traced as tool_failed,
an irreversible action is marked FAILED, and nothing retries it.

VoiceTools.execute is the only way to change the World. For an irreversible
tier, per the manifest loaded by core's load_tools, it first redeems the
confirmation receipt with the ConfirmationMachine, so even a buggy loop can't
run an irreversible tool without a matching echo + confirmation.
"""

from __future__ import annotations

import copy
import datetime as dt
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sensei_core.permissions import Tier, ToolSpec, load_tools
from sensei_core.trace import TraceWriter

from voice.confirm import ConfirmationMachine, UnverifiedAction

TOOLS_PATH = Path(__file__).with_name("tools.toml")
TODAY = dt.date(2026, 11, 30)  # a Monday


def voice_tools() -> dict[str, ToolSpec]:
    return load_tools(TOOLS_PATH)


@dataclass
class World:
    today: dt.date = TODAY
    contacts: list[str] = field(default_factory=lambda: ["Alex Chen", "Sam Patel", "Sam Okoye", "Jordan Lee"])
    notes: list[dict[str, str]] = field(default_factory=lambda: [
        {"id": "note-1", "text": "Groceries: eggs, rice, lemons"},
        {"id": "note-2", "text": "Offsite agenda: roadmap, hiring, budget"},
    ])
    events: list[dict[str, str]] = field(default_factory=lambda: [
        {"id": "evt-1", "title": "Standup", "date": "2026-11-30", "time": "09:30"},
        {"id": "evt-2", "title": "Dentist", "date": "2026-11-30", "time": "15:00"},
        {"id": "evt-3", "title": "Design review", "date": "2026-12-01", "time": "11:00"},
        {"id": "evt-4", "title": "Lunch with Alex", "date": "2026-12-02", "time": "12:30"},
    ])
    approvals: dict[str, dict[str, str]] = field(default_factory=lambda: {
        "req-17": {"kind": "expense", "summary": "team offsite catering, $420.00", "status": "pending"},
        "req-9": {"kind": "travel", "summary": "Lisbon conference trip", "status": "pending"},
    })
    invoices: dict[str, dict[str, str]] = field(default_factory=lambda: {
        "INV-204": {"payee": "Acme Paper", "amount": "$1,250.00", "status": "open"},
        "INV-240": {"payee": "Northwind Couriers", "amount": "$3,900.00", "status": "open"},
    })
    forms: dict[str, dict[str, str]] = field(default_factory=lambda: {
        "form-expense-nov": {"name": "expense report", "status": "draft"},
    })
    outbox: list[dict[str, str]] = field(default_factory=list)
    payments: list[dict[str, str]] = field(default_factory=list)
    deleted_notes: list[dict[str, str]] = field(default_factory=list)
    submissions: list[str] = field(default_factory=list)
    brief_headline: str = "Standup is at 9:30 and the dentist is at 3pm."

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy({k: v for k, v in vars(self).items() if k != "today"})

    def note(self, note_id: str) -> dict[str, str]:
        return next(n for n in self.notes if n["id"] == note_id)


class ToolFailed(RuntimeError):
    """A tool raised. It is traced as tool_failed, an irreversible action becomes FAILED, and nothing retries."""


@dataclass(frozen=True)
class ToolResult:
    tool: str
    data: dict[str, Any]
    reply: str


def _fmt_date(iso: str) -> str:
    day = dt.date.fromisoformat(iso)
    return f"{day:%A %B} {day.day}"


class VoiceTools:
    def __init__(self, world: World, machine: ConfirmationMachine, trace: TraceWriter,
                 specs: dict[str, ToolSpec] | None = None) -> None:
        self.world = world
        self.machine = machine
        self.trace = trace
        self.specs = specs if specs is not None else voice_tools()

    def tier(self, tool: str) -> Tier:
        return self.specs[tool].tier

    def execute(self, tool: str, params: dict[str, Any], receipt: str | None = None) -> ToolResult:
        action_id = None
        if self.tier(tool) is Tier.IRREVERSIBLE:
            action_id = self.machine.redeem(receipt, tool, params).action_id  # raises UnverifiedAction
        try:
            result = getattr(self, f"_{tool}")(**params)
        except UnverifiedAction:
            raise
        except Exception as e:  # any tool error: recorded, never retried, never silently swallowed
            error = f"{type(e).__name__}: {e}"
            if action_id is not None:
                self.machine.mark_failed(action_id, error)  # traces tool_failed
            else:
                self.trace.write("tool_failed", {"tool": tool, "tier": self.tier(tool).value, "action_id": None,
                                                 "params": params, "error": error, "retried": False})
            raise ToolFailed(error) from e
        self.trace.write("tool_executed", {"tool": tool, "tier": self.tier(tool).value, "action_id": action_id,
                                           "params": params, "result": result.data})
        return result

    # read
    def _morning_brief(self) -> ToolResult:
        today = [e for e in self.world.events if e["date"] == self.world.today.isoformat()]
        pending = [a for a in self.world.approvals.values() if a["status"] == "pending"]
        reply = (f"Good morning. {self.world.brief_headline} "
                 f"You have {len(today)} events today and {len(pending)} pending requests.")
        return ToolResult("morning_brief", {"events_today": len(today), "pending": len(pending)}, reply)

    def _calendar_query(self, date: str) -> ToolResult:
        events = sorted((e for e in self.world.events if e["date"] == date), key=lambda e: e["time"])
        if not events:
            return ToolResult("calendar_query", {"date": date, "event_ids": []}, f"Nothing on {_fmt_date(date)}.")
        listing = "; ".join(f"{e['title']} at {e['time']}" for e in events)
        return ToolResult("calendar_query", {"date": date, "event_ids": [e["id"] for e in events]},
                          f"On {_fmt_date(date)}: {listing}.")

    # reversible
    def _take_note(self, text: str) -> ToolResult:
        note = {"id": f"note-{len(self.world.notes) + len(self.world.deleted_notes) + 1}", "text": text}
        self.world.notes.append(note)
        return ToolResult("take_note", {"note_id": note["id"]}, f"Noted: {text}")

    def _move_event(self, event_id: str, date: str) -> ToolResult:
        event = next(e for e in self.world.events if e["id"] == event_id)
        event["date"] = date
        return ToolResult("move_event", {"event_id": event_id, "date": date},
                          f"Moved {event['title']} to {_fmt_date(date)}.")

    def _deny_pending(self, request_id: str) -> ToolResult:
        self.world.approvals[request_id]["status"] = "denied"
        return ToolResult("deny_pending", {"request_id": request_id},
                          f"Denied the {self.world.approvals[request_id]['kind']} request.")

    # irreversible: reached only through a redeemed confirmation
    def _approve_pending(self, request_id: str) -> ToolResult:
        self.world.approvals[request_id]["status"] = "approved"
        return ToolResult("approve_pending", {"request_id": request_id},
                          f"Approved the {self.world.approvals[request_id]['kind']} request.")

    def _send_note(self, note_id: str, recipient: str) -> ToolResult:
        self.world.outbox.append({"note_id": note_id, "recipient": recipient, "text": self.world.note(note_id)["text"]})
        return ToolResult("send_note", {"note_id": note_id, "recipient": recipient}, f"Sent the note to {recipient}.")

    def _pay(self, payee: str, amount: str, invoice_id: str | None = None) -> ToolResult:
        self.world.payments.append({"payee": payee, "amount": amount, "invoice_id": invoice_id or ""})
        if invoice_id:
            self.world.invoices[invoice_id]["status"] = "paid"
        return ToolResult("pay", {"payee": payee, "amount": amount, "invoice_id": invoice_id}, f"Paid {payee} {amount}.")

    def _delete_note(self, note_id: str) -> ToolResult:
        note = self.world.note(note_id)
        self.world.notes.remove(note)
        self.world.deleted_notes.append(note)
        return ToolResult("delete_note", {"note_id": note_id}, "Deleted the note.")

    def _submit_form(self, form_id: str) -> ToolResult:
        self.world.forms[form_id]["status"] = "submitted"
        self.world.submissions.append(form_id)
        return ToolResult("submit_form", {"form_id": form_id}, f"Submitted the {self.world.forms[form_id]['name']}.")

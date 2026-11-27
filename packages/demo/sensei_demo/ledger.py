"""The live permission ledger (Demo Slice 1): the session's trace, streamed as it is written.

A LedgerTrace is the session's TraceWriter. Every record it writes is appended
to the trace file exactly as before (core's redaction, owner-only file) and
then published to the session's Ledger, in write order. The ledger is
therefore the trace itself, not a second account of it. Nothing in core, the
gate, the echo machine or the parser changes.

Each published record carries a readable `step` for the page: proposed,
echoed, approved or denied, executed, verified. The mapping is code, so it is
tested; the page only renders it.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

from sensei_core.trace import TraceWriter

# The events a visitor watches; anything else in the trace is shown as a minor step.
LEDGER_EVENTS = ("permission_decision", "action_proposed", "action_echoed", "action_confirmed",
                 "action_declined", "tool_executed", "hard_gate_violation", "hard_gate_check",
                 "route_decision", "model_call", "model_error")  # the last three: free-form answers (#25)
# Core's Kimi error classes (sensei_core/clients.py), plus the demo's own spend_limit.
MODEL_ERRORS = {"spend_limit": "the demo's spending limit is reached", "quota_exhausted": "the model's quota is used up",
                "timeout": "the model timed out", "unreachable": "the model can't be reached",
                "key_absent": "no model key is configured", "auth_failed": "the model key was refused",
                "rate_limited": "the model is rate-limiting requests", "server_error": "the model service had an error",
                "bad_request": "the model rejected the request", "bad_response": "the model's reply was empty"}
GATE_WORDS = {"ALLOW": ("allowed", "No approval needed for this tier."),
              "HALT_FOR_APPROVAL": ("needs your approval", "Irreversible: it must be read back and confirmed first."),
              "DENY": ("denied", "Not in the voice manifest, so it can't run.")}


class Ledger:
    """Records published in trace order, with a condition so a stream can wait for the next one."""

    def __init__(self) -> None:
        self._records: list[dict[str, Any]] = []
        self._changed = threading.Condition()

    def publish(self, record: dict[str, Any]) -> None:
        with self._changed:
            self._records.append({"index": len(self._records), "event_type": record["event_type"],
                                  "timestamp": record["timestamp"], "payload": record["payload"],
                                  "step": step(record["event_type"], record["payload"])})
            self._changed.notify_all()

    def after(self, cursor: int) -> list[dict[str, Any]]:
        with self._changed:
            return self._records[cursor:]

    def wait_after(self, cursor: int, timeout: float) -> list[dict[str, Any]]:
        """Records after `cursor`, waiting up to `timeout` seconds for one if there are none yet."""
        with self._changed:
            self._changed.wait_for(lambda: len(self._records) > cursor, timeout=timeout)
            return self._records[cursor:]

    def __len__(self) -> int:
        with self._changed:
            return len(self._records)


class LedgerTrace(TraceWriter):
    """A TraceWriter that also publishes each record, after it is safely on disk."""

    def __init__(self, path: str | Path, run_id: str, session_id: str, ledger: Ledger) -> None:
        super().__init__(path, run_id=run_id, session_id=session_id)
        self.ledger = ledger

    def write(self, event_type: str, payload: dict[str, Any] | None = None,
              cost_usd: float | None = None) -> dict[str, Any]:
        record = super().write(event_type, payload, cost_usd)
        self.ledger.publish(record)
        return record


def _tool(p: dict[str, Any]) -> str:
    return str(p.get("tool", "")).replace("_", " ")


def step(event_type: str, p: dict[str, Any]) -> dict[str, str]:
    """The readable ledger step for one trace record: kind (for styling), title, detail."""
    if event_type == "permission_decision":
        word, why = GATE_WORDS.get(p.get("outcome", ""), (str(p.get("outcome")), ""))
        return {"kind": "gate", "title": f"Gate: {_tool(p)} {word}", "detail": why}
    if event_type == "action_proposed":
        return {"kind": "proposed", "title": f"Proposed: {_tool(p)}", "detail": _params(p.get("params"))}
    if event_type == "action_echoed":
        return {"kind": "echoed", "title": "Echoed back to you", "detail": str(p.get("echo_text") or "")}
    if event_type == "action_confirmed":
        mine = p.get("source", "user") == "user"
        return {"kind": "approved", "title": "Approved by you" if mine else f"Confirmed ({p.get('source')})",
                "detail": f"Confirmation matches the echo: {_params(p.get('params'))}"}
    if event_type == "action_declined":
        return {"kind": "denied", "title": "Declined: nothing will run", "detail": str(p.get("reason") or "")}
    if event_type == "tool_executed":
        tier = p.get("tier", "")
        return {"kind": "executed", "title": f"Executed: {_tool(p)}",
                "detail": f"{tier} tier" + (", after its echo and your confirmation" if tier == "irreversible" else "")}
    if event_type == "hard_gate_violation":
        return {"kind": "violation", "title": "BLOCKED: unverified action", "detail": str(p.get("reason") or "")}
    if event_type == "hard_gate_check":
        if p.get("passed"):
            n = p.get("irreversible_executed", 0)
            return {"kind": "verified", "title": "Verified: hard gate PASS",
                    "detail": f"{n} irreversible action{'s' if n != 1 else ''} ran, each echoed and confirmed first."
                    if n else "Nothing irreversible ran."}
        return {"kind": "violation", "title": "Verified: hard gate FAIL", "detail": "; ".join(p.get("violations", []))}
    if event_type == "route_decision":
        return {"kind": "route", "title": f"Routed to {p.get('route')} ({p.get('model')})",
                "detail": f"No command matched, so this is a question. {p.get('sensitivity_class')}: {p.get('reason')}."}
    if event_type == "model_call":
        cost = p.get("est_cost_usd") or 0.0
        return {"kind": "model", "title": f"Answered by {p.get('model')}",
                "detail": f"≈${cost:.5f} (core's estimate) · {round(p.get('latency_ms') or 0)} ms · read-only: no tools, "
                          "nothing can run"}
    if event_type == "model_error":
        why = MODEL_ERRORS.get(str(p.get("error_class")), str(p.get("error_class")))
        return {"kind": "error", "title": "No model answer", "detail": f"{p.get('model')}: {why}. Sensei said so instead "
                                                                       "of guessing."}
    if event_type == "speech_error":
        what = "hearing you" if p.get("stage") == "stt" else "speaking the reply"
        return {"kind": "error", "title": f"Speech error while {what}",
                "detail": f"{p.get('provider')}: {p.get('error_class')}" + (" (nothing ran)" if p.get("stage") == "stt" else "")}
    if event_type == "voice_turn":
        return {"kind": "turn", "title": f"Heard: {p.get('transcript', '')}", "detail": f"outcome: {p.get('outcome')}"}
    return {"kind": "minor", "title": event_type.replace("_", " "), "detail": ""}


def _params(params: Any) -> str:
    if not isinstance(params, dict):
        return ""
    return ", ".join(f"{k}={v}" for k, v in params.items() if v not in (None, ""))

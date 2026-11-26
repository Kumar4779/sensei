"""The confirmation-echo state machine (Voice Lab Slice 1): proposed → echoed → confirmed → executed.

Core's PermissionGate decides every tool call. For an irreversible-tier tool
(send / pay / delete / submit / approve) it returns HALT_FOR_APPROVAL, and in
voice the approval is this machine:

  - propose: the parsed action and its parameters, held as PROPOSED. Nothing
    executes from here.
  - echo: the exact tool and parameters are frozen (a canonical JSON copy),
    and the echo text is rendered from that copy. State ECHOED.
  - confirm: needs ECHOED, and the confirmation must carry parameters equal to
    the echoed ones exactly; a mismatch declines the action. State CONFIRMED,
    and a single-use receipt is issued.
  - redeem: called by the tool layer itself (world.VoiceTools.execute), never
    by the loop. The receipt must be valid, the state CONFIRMED, and tool and
    parameters identical to the echo. State EXECUTED, so a second redeem is
    refused.

A second confirmation of an action already confirmed or executed is a no-op,
traced as such. Any attempt to execute without a matching echo + confirmation
is a hard-gate violation: it is traced, it raises UnverifiedAction, and it
marks the run failed. This is enforced in code; no prompt is involved.

Stale-plan guard (Slice 2): `interrupt()` is called when the user barges in.
Every action that is echoed or confirmed but not yet executed becomes stale:
it is declined with the reason "interrupted", and any later confirm or redeem
of it is a hard-gate violation. A plan formed before an interruption can
therefore never run unchanged; to proceed, the loop must propose and echo it
again.

Every confirmation records its `source`. Only "user" (an explicit yes in its
own turn after the echo) counts for the hard gate. The echo-OFF ablation
confirms with source "ablation_auto", which the run verifier
(voice/verify.py) counts as an unconfirmed action.
"""

from __future__ import annotations

import copy
import json
import secrets
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from sensei_core.trace import TraceWriter


class State(str, Enum):
    PROPOSED = "proposed"
    ECHOED = "echoed"
    CONFIRMED = "confirmed"
    EXECUTED = "executed"
    DECLINED = "declined"


class UnverifiedAction(RuntimeError):
    """An irreversible action was attempted without a matching echo + confirmation; the run fails."""


def canonical(params: dict[str, Any]) -> str:
    return json.dumps(params, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


@dataclass
class PendingAction:
    action_id: str
    tool: str
    params: dict[str, Any]
    state: State = State.PROPOSED
    echo_text: str | None = None
    echoed: str | None = None  # canonical JSON of the parameters as echoed
    receipt: str | None = field(default=None, repr=False)
    stale_receipt: str | None = field(default=None, repr=False)  # a receipt voided by decline or interruption

    @property
    def echoed_params(self) -> dict[str, Any] | None:
        return None if self.echoed is None else json.loads(self.echoed)


class ConfirmationMachine:
    def __init__(self, trace: TraceWriter) -> None:
        self._trace = trace
        self._actions: dict[str, PendingAction] = {}
        self._count = 0
        self._stale: set[str] = set()
        self.violations: list[str] = []

    @property
    def run_failed(self) -> bool:
        return bool(self.violations)

    def get(self, action_id: str) -> PendingAction:
        return self._actions[action_id]

    def _violation(self, reason: str, **payload: Any) -> UnverifiedAction:
        self.violations.append(reason)
        self._trace.write("hard_gate_violation", {"reason": reason, **payload})
        return UnverifiedAction(reason)

    def propose(self, tool: str, params: dict[str, Any]) -> PendingAction:
        self._count += 1
        action = PendingAction(f"act-{self._count}", tool, copy.deepcopy(params))
        self._actions[action.action_id] = action
        self._trace.write("action_proposed", {"action_id": action.action_id, "tool": tool, "params": action.params})
        return action

    def echo(self, action_id: str, render: Callable[[str, dict[str, Any]], str]) -> str:
        action = self._actions[action_id]
        if action.state is not State.PROPOSED:
            raise self._violation(f"echo of {action_id} from state {action.state.value}", action_id=action_id)
        action.echoed = canonical(action.params)
        action.echo_text = render(action.tool, json.loads(action.echoed))
        action.state = State.ECHOED
        self._trace.write("action_echoed", {"action_id": action_id, "tool": action.tool,
                                            "params": json.loads(action.echoed), "echo_text": action.echo_text})
        return action.echo_text

    def interrupt(self, point: str) -> list[str]:
        """A barge-in: every in-flight (echoed or confirmed) action becomes stale. Returns their IDs."""
        stale = [a.action_id for a in self._actions.values() if a.state in (State.ECHOED, State.CONFIRMED)]
        for action_id in stale:
            self._stale.add(action_id)
            self.decline(action_id, f"interrupted {point}: the echo must be re-issued before any execution")
        return stale

    def confirm(self, action_id: str, params: dict[str, Any], source: str = "user") -> PendingAction:
        action = self._actions[action_id]
        if action_id in self._stale:
            raise self._violation(f"confirmation of {action_id}, a plan formed before an interruption",
                                  action_id=action_id, stale=True)
        if action.state in (State.CONFIRMED, State.EXECUTED):
            self._trace.write("confirmation_ignored", {"action_id": action_id, "state": action.state.value,
                                                       "reason": "already confirmed; an action runs at most once"})
            return action
        if action.state is not State.ECHOED:
            raise self._violation(f"confirmation of {action_id} without an echo (state {action.state.value})",
                                  action_id=action_id)
        if canonical(params) != action.echoed:
            self.decline(action_id, "confirmation does not match the echoed parameters", params=params)
            return action
        action.receipt = secrets.token_hex(16)
        action.state = State.CONFIRMED
        self._trace.write("action_confirmed", {"action_id": action_id, "tool": action.tool,
                                               "params": json.loads(action.echoed), "source": source})
        return action

    def decline(self, action_id: str, reason: str, params: dict[str, Any] | None = None) -> PendingAction:
        action = self._actions[action_id]
        if action.state in (State.PROPOSED, State.ECHOED, State.CONFIRMED):
            action.state = State.DECLINED
            action.stale_receipt, action.receipt = action.receipt, None
            payload: dict[str, Any] = {"action_id": action_id, "tool": action.tool, "reason": reason}
            if params is not None:
                payload["confirmation_params"] = params
            self._trace.write("action_declined", payload)
        return action

    def redeem(self, receipt: str | None, tool: str, params: dict[str, Any]) -> PendingAction:
        """Called by the tool layer before an irreversible tool runs. Raises UnverifiedAction on any mismatch."""
        stale = next((a for a in self._actions.values() if a.action_id in self._stale and receipt
                      and a.stale_receipt == receipt), None)
        if stale is not None:
            raise self._violation(f"{tool} attempted from a plan formed before an interruption",
                                  action_id=stale.action_id, tool=tool, params=params, stale=True)
        action = next((a for a in self._actions.values() if receipt and a.receipt == receipt), None)
        if action is None:
            raise self._violation(f"{tool} attempted without a confirmed echo", tool=tool, params=params)
        if action.state is not State.CONFIRMED:
            raise self._violation(f"{tool} attempted from state {action.state.value}", action_id=action.action_id,
                                  tool=tool, params=params)
        if tool != action.tool or canonical(params) != action.echoed:
            raise self._violation(f"{tool} parameters differ from the echoed {action.tool} action",
                                  action_id=action.action_id, tool=tool, params=params)
        action.state = State.EXECUTED
        action.receipt = None  # single use
        return action

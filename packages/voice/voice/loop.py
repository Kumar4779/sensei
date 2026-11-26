"""The transcript-first reference loop (Voice Lab Slice 1).

    transcript in → intent + parameters → PermissionGate pre-check → echo state machine
                  → execution against the fake Sensei tools → reply text out

Core's PermissionGate (sensei_core.permissions) decides every call from the
voice manifest (voice/tools.toml):
  - ALLOW (read and reversible tiers) runs the tool;
  - DENY refuses;
  - HALT_FOR_APPROVAL (irreversible tier) proposes the action and echoes it.
Only a separate, explicit yes turn after the echo confirms. A command turn
never confirms itself, a bare yes with nothing echoed confirms nothing, and a
low-confidence turn is never treated as either a command or a yes.

Every turn writes one "voice_turn" trace record through core's TraceWriter:
transcript, STT confidence, intent, gate decision, proposed action and
parameters, echo text, confirmation, execution result, reply and latencies.
The gate and the confirmation machine write their own events to the same
trace. Audio is never stored.
"""

from __future__ import annotations

import time
from typing import Any

from sensei_core.permissions import Outcome, PermissionGate, ToolSpec
from sensei_core.trace import TraceWriter

from voice.adapters import FakeSTT, FakeTTS, ScriptedUtterance, SpeechToText, TextToSpeech
from voice.confirm import ConfirmationMachine, State, UnverifiedAction
from voice.intents import (Clarify, Command, Memory, confirmed_params, is_low_confidence, parse_command,
                           parse_confirmation, render_echo)
from voice.world import VoiceTools, World, voice_tools

SENSITIVITY_CLASS = "LOCAL_ONLY"  # a user's spoken commands are personal content


def _ms(start: float, end: float) -> float:
    return round((end - start) * 1000, 3)


class VoiceSession:
    def __init__(self, trace: TraceWriter, world: World | None = None, stt: SpeechToText | None = None,
                 tts: TextToSpeech | None = None, specs: dict[str, ToolSpec] | None = None) -> None:
        self.world = world if world is not None else World()
        self.trace = trace
        self.stt = stt or FakeSTT()
        self.tts = tts or FakeTTS()
        specs = specs if specs is not None else voice_tools()
        self.gate = PermissionGate(specs, trace)  # core's gate: the only permission decision
        self.machine = ConfirmationMachine(trace)
        self.tools = VoiceTools(self.world, self.machine, trace, specs)
        self.memory = Memory()
        self.pending: str | None = None  # the action awaiting yes/no
        self.last_action: str | None = None  # the most recent confirmed action, so a repeat yes is a no-op
        self.turns = 0

    @property
    def run_failed(self) -> bool:
        return self.machine.run_failed

    def turn(self, utterance: ScriptedUtterance) -> str:
        self.turns += 1
        start = time.perf_counter()
        transcript = self.stt.transcribe(utterance)
        heard = time.perf_counter()
        record: dict[str, Any] = {"turn": self.turns, "transcript": transcript.text,
                                  "stt_confidence": transcript.confidence, "intent": None, "decision": None,
                                  "action_id": None, "proposed": None, "echo_text": None, "confirmation": None,
                                  "execution": None}
        reply, record["outcome"] = self._handle(transcript.text, transcript.confidence, record)
        acted = time.perf_counter()
        self.tts.speak(reply)
        spoke = time.perf_counter()
        record["reply"] = reply
        record["latency_ms"] = {"stt": _ms(start, heard), "understand_and_act": _ms(heard, acted),
                                "time_to_first_audio": _ms(start, spoke), "turn": _ms(start, time.perf_counter())}
        self.trace.write("voice_turn", record)
        return reply

    def _handle(self, text: str, confidence: float | None, record: dict[str, Any]) -> tuple[str, str]:
        if is_low_confidence(confidence):
            if self.pending:
                return "I didn't catch that clearly. Please say yes or no.", "clarify"
            return "I didn't catch that clearly. Could you say it again?", "clarify"

        confirmation = parse_confirmation(text, self.world)
        if self.pending and confirmation is None:  # a new command supersedes the unanswered echo
            self.machine.decline(self.pending, "superseded by a new command")
            self.pending = None
        if confirmation is not None:
            record["confirmation"] = {"affirm": confirmation.affirm, "mentions": confirmation.mentions}
            return self._confirm(confirmation, record)

        parsed = parse_command(text, self.world, self.memory)
        if isinstance(parsed, Clarify):
            record["clarify_reason"] = parsed.reason
            return parsed.question, "clarify"
        return self._command(parsed, record)

    def _command(self, command: Command, record: dict[str, Any]) -> tuple[str, str]:
        record["intent"] = {"tool": command.tool, "params": command.params}
        self.last_action = None  # a later yes belongs to this command, not to an older one
        decision = self.gate.decide(command.tool, SENSITIVITY_CLASS)
        record["decision"] = decision.outcome.value
        if decision.outcome is Outcome.DENY:
            return "I can't do that.", "refused"
        if decision.outcome is Outcome.HALT_FOR_APPROVAL:
            action = self.machine.propose(command.tool, command.params)
            echo = self.machine.echo(action.action_id, lambda tool, p: render_echo(tool, p, self.world))
            self.pending = action.action_id
            record.update(action_id=action.action_id, proposed={"tool": command.tool, "params": command.params},
                          echo_text=echo)
            return echo, "echoed"
        result = self.tools.execute(command.tool, command.params)
        if command.tool == "calendar_query":
            self.memory.last_event_ids = list(result.data["event_ids"])
        if command.tool == "take_note":
            self.memory.last_note_id = result.data["note_id"]
        record["execution"] = {"tool": command.tool, "result": result.data}
        return result.reply, "executed"

    def _confirm(self, confirmation: Any, record: dict[str, Any]) -> tuple[str, str]:
        if self.pending is None:
            if confirmation.affirm and self.last_action is not None:
                self.machine.confirm(self.last_action, self.machine.get(self.last_action).echoed_params or {})
                record["action_id"] = self.last_action
                return "That's already done. I won't do it twice.", "already_done"
            if confirmation.affirm:
                return "There's nothing waiting for your confirmation.", "nothing_to_confirm"
            return "Okay.", "nothing_to_confirm"

        action_id, self.pending = self.pending, None
        action = self.machine.get(action_id)
        record.update(action_id=action_id, proposed={"tool": action.tool, "params": action.params},
                      echo_text=action.echo_text)
        if not confirmation.affirm:
            self.machine.decline(action_id, "user said no")
            return "Okay, cancelled. Nothing was done.", "declined"
        echoed = action.echoed_params or {}
        self.machine.confirm(action_id, confirmed_params(echoed, confirmation))
        if action.state is not State.CONFIRMED:
            return "That doesn't match what I read back, so I've cancelled it. Nothing was done.", "declined"
        self.last_action = action_id
        try:
            result = self.tools.execute(action.tool, echoed, action.receipt)
        except UnverifiedAction:
            return "I couldn't verify that action, so it did not run.", "blocked"
        record["execution"] = {"tool": action.tool, "result": result.data}
        return result.reply, "executed"


def run_script(session: VoiceSession, utterances: list[ScriptedUtterance]) -> list[str]:
    return [session.turn(u) for u in utterances]

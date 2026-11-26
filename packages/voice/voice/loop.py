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

Real-path turns (Slice 6). When a real STT or TTS served the turn, the record
also carries `real_path`: provider and model for each side, stt_latency_ms,
tts_first_byte_ms, tts_total_ms and time_to_first_audio_ms (turn start to
playback start). `latency_ms` is unchanged. Real-path numbers are reported
apart from the transcript path's and never merged with them.

Interruption (Slice 2). A reply is spoken as segments: sentences, or a
calendar listing's header and then one segment per event. An utterance with
`barge_in_after=n` arrived while the previous reply was still playing, after
n segments. The loop then:
  - stops: TTS stops, the rest is never spoken, and a "barge_in" event is
    traced with its timestamp, the point (mid-answer / mid-echo / mid-listing)
    and what had been heard;
  - invalidates: ConfirmationMachine.interrupt() makes every in-flight echo
    stale, in code;
  - re-plans from the interrupting utterance. A yes to an interrupted echo
    re-issues the echo instead of executing. A mid-listing follow-up resolves
    "that one" against the items actually heard, not the full listing.

Echo OFF (the Slice 2 ablation only): irreversible actions are confirmed
automatically, with source "ablation_auto", and run in the same turn. The run
verifier counts each one as an unconfirmed action.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sensei_core.permissions import Outcome, PermissionGate, ToolSpec
from sensei_core.trace import TraceWriter

from voice.adapters import (FakeSTT, FakeTTS, ScriptedUtterance, SpeechToText, SpokenReply, TextToSpeech,
                            Transcript)
from voice.confirm import ConfirmationMachine, EchoIncomplete, State, UnverifiedAction
from voice.intents import (Clarify, Command, Memory, confirmed_params, is_low_confidence, parse_command,
                           parse_confirmation, render_echo, spoken_values)
from voice.world import ToolFailed, VoiceTools, World, voice_tools

SENSITIVITY_CLASS = "LOCAL_ONLY"  # a user's spoken commands are personal content
ECHO_INCOMPLETE_REPLY = "I can't read that back completely, so I won't do it."
TOOL_FAILED_REPLY = "That didn't work, so nothing was changed. I won't retry it automatically."


def _ms(start: float, end: float) -> float:
    return round((end - start) * 1000, 3)


@dataclass
class Speech:
    """The reply currently playing: what an interruption cuts off."""

    segments: list[str]
    point: str  # mid-answer | mid-echo | mid-listing: what a barge-in now would interrupt
    event_ids: list[str] = field(default_factory=list)  # listing only: one per segment after the header


def _sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.?!])\s+", text.strip()) if s]


def _speech(reply: str, outcome: str, record: dict[str, Any]) -> Speech:
    if outcome in ("echoed", "re_echoed"):
        return Speech(_sentences(reply), "mid-echo")
    execution = record.get("execution") or {}
    event_ids = (execution.get("result") or {}).get("event_ids", [])
    if execution.get("tool") == "calendar_query" and len(event_ids) > 1:
        header, items = reply.split(": ", 1)
        return Speech([header + ":"] + items.rstrip(".").split("; "), "mid-listing", list(event_ids))
    return Speech(_sentences(reply), "mid-answer")


class VoiceSession:
    def __init__(self, trace: TraceWriter, world: World | None = None, stt: SpeechToText | None = None,
                 tts: TextToSpeech | None = None, specs: dict[str, ToolSpec] | None = None,
                 echo: bool = True) -> None:
        self.world = world if world is not None else World()
        self.trace = trace
        self.stt = stt or FakeSTT()
        self.tts = tts or FakeTTS()
        specs = specs if specs is not None else voice_tools()
        self.gate = PermissionGate(specs, trace)  # core's gate: the only permission decision
        # Every echo this loop presents must state every parameter value (Slice 3).
        self.machine = ConfirmationMachine(trace, spoken=lambda tool, p: spoken_values(tool, p, self.world))
        self.tools = VoiceTools(self.world, self.machine, trace, specs)
        self.memory = Memory()
        self.pending: str | None = None  # the action awaiting yes/no
        self.last_action: str | None = None  # the most recent confirmed action, so a repeat yes is a no-op
        self.echo = echo  # False only for the echo on/off ablation
        self.speaking: Speech | None = None
        self.reecho: tuple[str, dict[str, Any]] | None = None  # an interrupted echo, to be read back again
        self.turns = 0
        self.last_heard: str | None = None  # the most recent transcript, for the terminal loop to show

    @property
    def run_failed(self) -> bool:
        return self.machine.run_failed

    def turn(self, utterance: ScriptedUtterance) -> str:
        self.turns += 1
        start = time.perf_counter()
        barge = None
        if (utterance.barge_in_after is not None and self.speaking is not None
                and utterance.barge_in_after < len(self.speaking.segments)):
            barge = self._barge_in(utterance.barge_in_after)
        self.speaking = None  # whatever was playing has finished or was cut off
        transcript = self.stt.transcribe(utterance)
        heard = time.perf_counter()
        self.last_heard = transcript.text
        record: dict[str, Any] = {"turn": self.turns, "transcript": transcript.text,
                                  "stt_confidence": transcript.confidence, "after_barge_in": barge, "intent": None,
                                  "decision": None, "action_id": None, "proposed": None, "echo_text": None,
                                  "confirmation": None, "execution": None}
        reply, record["outcome"] = self._handle(transcript.text, transcript.confidence, record, barge)
        acted = time.perf_counter()
        self.speaking = _speech(reply, record["outcome"], record)
        record["reply"] = reply
        try:
            spoken = self.tts.speak(reply)
        except Exception as e:  # a real TTS can fail after the action ran: the turn is still traced
            record["tts_error"] = getattr(e, "error_class", type(e).__name__)
            record["latency_ms"] = {"stt": _ms(start, heard), "understand_and_act": _ms(heard, acted),
                                    "time_to_first_audio": None, "turn": _ms(start, time.perf_counter())}
            self.trace.write("voice_turn", record)
            raise
        spoke = time.perf_counter()
        record["latency_ms"] = {"stt": _ms(start, heard), "understand_and_act": _ms(heard, acted),
                                "time_to_first_audio": _ms(start, spoke), "turn": _ms(start, time.perf_counter())}
        if transcript.provider is not None or spoken.provider is not None:
            record["real_path"] = _real_path(transcript, spoken, start, self.stt.name, self.tts.name)
        self.trace.write("voice_turn", record)
        return reply

    def _barge_in(self, heard: int) -> str:
        """The user spoke over the reply: stop, invalidate in-flight plans, keep only what was heard."""
        speech = self.speaking
        assert speech is not None
        self.tts.stop(heard)
        stale = self.machine.interrupt(speech.point)
        if self.pending in stale:
            action = self.machine.get(self.pending)
            self.reecho = (action.tool, action.echoed_params or {})
            self.pending = None
        if speech.point == "mid-listing":  # "that one" can only mean an item the user actually heard
            self.memory.last_event_ids = speech.event_ids[:max(0, heard - 1)]
        self.trace.write("barge_in", {
            "at": datetime.now(timezone.utc).isoformat(), "point": speech.point, "interrupted_turn": self.turns - 1,
            "segments_heard": heard, "segments_total": len(speech.segments), "heard": " ".join(speech.segments[:heard]),
            "stopped": True, "stale_action_ids": stale})
        return speech.point

    def _handle(self, text: str, confidence: float | None, record: dict[str, Any],
                barge: str | None = None) -> tuple[str, str]:
        if is_low_confidence(confidence):
            if self.pending or self.reecho:
                return "I didn't catch that clearly. Please say yes or no.", "clarify"
            return "I didn't catch that clearly. Could you say it again?", "clarify"

        confirmation = parse_confirmation(text, self.world)
        if self.reecho is not None:
            tool, params = self.reecho
            self.reecho = None
            if confirmation is not None:
                record["confirmation"] = {"affirm": confirmation.affirm, "mentions": confirmation.mentions}
                if not confirmation.affirm:
                    return "Okay, cancelled. Nothing was done.", "declined"
                try:
                    echo = self._propose_and_echo(Command(tool, params), record)
                except EchoIncomplete:
                    return ECHO_INCOMPLETE_REPLY, "refused"
                return f"You interrupted me, so let me read that back again. {echo}", "re_echoed"
        if barge is not None and confirmation is not None and not confirmation.affirm and not self.pending:
            record["confirmation"] = {"affirm": False, "mentions": {}}
            return "Okay, I've stopped.", "stopped"
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
            if not self.echo:
                return self._execute_without_echo(command, record)
            try:
                return self._propose_and_echo(command, record), "echoed"
            except EchoIncomplete:
                return ECHO_INCOMPLETE_REPLY, "refused"
        try:
            result = self.tools.execute(command.tool, command.params)
        except ToolFailed as e:
            record["execution"] = {"tool": command.tool, "error": str(e)}
            return TOOL_FAILED_REPLY, "failed"
        if command.tool == "calendar_query":
            self.memory.last_event_ids = list(result.data["event_ids"])
        if command.tool == "take_note":
            self.memory.last_note_id = result.data["note_id"]
        record["execution"] = {"tool": command.tool, "result": result.data}
        return result.reply, "executed"

    def _propose_and_echo(self, command: Command, record: dict[str, Any]) -> str:
        action = self.machine.propose(command.tool, command.params)
        echo = self.machine.echo(action.action_id, lambda tool, p: render_echo(tool, p, self.world))
        self.pending = action.action_id
        record.update(action_id=action.action_id, proposed={"tool": command.tool, "params": command.params},
                      echo_text=echo)
        return echo

    def _execute_without_echo(self, command: Command, record: dict[str, Any]) -> tuple[str, str]:
        """Ablation arm only: what an agent without the echo does. It acts on the transcript as heard."""
        action = self.machine.propose(command.tool, command.params)
        self.machine.echo(action.action_id, lambda tool, p: "(not spoken: echo off)", presented=False)
        self.machine.confirm(action.action_id, action.echoed_params or {}, source="ablation_auto")
        record.update(action_id=action.action_id, proposed={"tool": command.tool, "params": command.params})
        result = self.tools.execute(action.tool, action.echoed_params or {}, action.receipt)
        record["execution"] = {"tool": action.tool, "result": result.data}
        return result.reply, "executed"

    def _confirm(self, confirmation: Any, record: dict[str, Any]) -> tuple[str, str]:
        if self.pending is None:
            if confirmation.affirm and self.last_action is not None:
                last = self.machine.confirm(self.last_action, self.machine.get(self.last_action).echoed_params or {})
                record["action_id"] = self.last_action
                if last.state is State.FAILED:
                    return "That didn't work earlier, and I won't retry it automatically.", "already_done"
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
        except ToolFailed as e:
            record["execution"] = {"tool": action.tool, "error": str(e)}
            return TOOL_FAILED_REPLY, "failed"
        record["execution"] = {"tool": action.tool, "result": result.data}
        return result.reply, "executed"


def _real_path(transcript: Transcript, spoken: SpokenReply, start: float, stt: str, tts: str) -> dict[str, Any]:
    """Provider timings for a turn a real adapter served. A fake side reports its name and no timings."""
    return {"provider": {"stt": transcript.provider or stt, "tts": spoken.provider or tts},
            "model": {"stt": transcript.model, "tts": spoken.model},
            "stt_latency_ms": transcript.latency_ms, "tts_first_byte_ms": spoken.first_byte_ms,
            "tts_total_ms": spoken.total_ms,
            "time_to_first_audio_ms": None if spoken.playback_started is None else _ms(start, spoken.playback_started)}


def run_script(session: VoiceSession, utterances: list[ScriptedUtterance]) -> list[str]:
    return [session.turn(u) for u in utterances]

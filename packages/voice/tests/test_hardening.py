"""Slice 3 hardening: echo-completeness, the FAILED path, and audio bytes that never reach a trace."""

import base64

import pytest

from sensei_core.trace import TraceWriter, read_events

import voice.loop as loop_module
from voice.adapters import FakeSTT, ScriptedUtterance
from voice.confirm import ConfirmationMachine, EchoIncomplete, State, UnverifiedAction
from voice.intents import render_echo, spoken_values
from voice.loop import TOOL_FAILED_REPLY, VoiceSession
from voice.verify import verify_run
from voice.world import VoiceTools, World

from conftest import say

# One command per irreversible tool, and the parameter whose spoken value the test drops from the echo.
ECHO_CASES = [
    ("send the note to Alex", "send_note", "recipient"),
    ("send the note to Alex", "send_note", "note_id"),
    ("pay invoice 204", "pay", "amount"),
    ("pay invoice 204", "pay", "payee"),
    ("pay invoice 204", "pay", "invoice_id"),
    ("spend forty dollars on Alex", "pay", "amount"),
    ("delete the note about groceries", "delete_note", "note_id"),
    ("submit the expense report", "submit_form", "form_id"),
    ("approve the pending expense request", "approve_pending", "request_id"),
]


# 1. Echo-completeness

@pytest.mark.parametrize("command, tool", sorted({(c, t) for c, t, _ in ECHO_CASES}))
def test_every_real_echo_states_every_parameter_value(session, trace_path, command, tool):
    (echo,) = say(session, command)
    echoed = next(e["payload"] for e in read_events(trace_path) if e["event_type"] == "action_echoed")
    assert echoed["tool"] == tool and echoed["presented"] is True
    for key, form in spoken_values(tool, echoed["params"], session.world).items():
        assert form is None or form in echo, (key, form)


@pytest.mark.parametrize("command, tool, dropped", ECHO_CASES)
def test_an_echo_that_drops_a_parameter_value_is_never_presented(session, trace_path, monkeypatch,
                                                                 command, tool, dropped):
    def lossy(t, p, world):
        text = render_echo(t, p, world)
        return text.replace(spoken_values(t, p, world)[dropped], "…")

    monkeypatch.setattr(loop_module, "render_echo", lossy)
    (reply,) = say(session, command)
    assert reply == "I can't read that back completely, so I won't do it."
    events = read_events(trace_path)
    assert [e["payload"]["missing"] for e in events if e["event_type"] == "echo_incomplete"] == [[dropped]]
    assert not any(e["event_type"] == "action_echoed" for e in events)  # the incomplete echo was never presented
    assert session.machine.get("act-1").state is State.DECLINED
    assert say(session, "yes") == ["There's nothing waiting for your confirmation."]
    assert session.world.outbox == session.world.payments == []


def test_the_machine_itself_raises_on_an_incomplete_echo(trace_path):
    world = World()
    machine = ConfirmationMachine(TraceWriter(trace_path, run_id="m", session_id="m"),
                                  spoken=lambda tool, p: spoken_values(tool, p, world))
    action = machine.propose("pay", {"payee": "Acme Paper", "amount": "$1,250.00", "invoice_id": "INV-204"})
    with pytest.raises(EchoIncomplete, match="amount"):
        machine.echo(action.action_id, lambda tool, p: "Just to confirm: pay Acme Paper for invoice INV-204.")
    assert action.state is State.DECLINED and action.echo_text is None


def test_the_echo_off_ablation_presents_nothing_so_checks_nothing(trace_path):
    session = VoiceSession(TraceWriter(trace_path, run_id="off", session_id="off"), echo=False)
    say(session, "pay invoice 204")
    echoed = next(e["payload"] for e in read_events(trace_path) if e["event_type"] == "action_echoed")
    assert echoed["presented"] is False


# 2. FAILED: a tool that raises after confirmation is recorded and never retried

@pytest.fixture
def failing_send(monkeypatch):
    calls = []

    def boom(self, note_id, recipient):
        calls.append((note_id, recipient))
        raise ConnectionError("mail relay unreachable")

    monkeypatch.setattr(VoiceTools, "_send_note", boom)
    return calls


def test_a_tool_that_raises_after_confirmation_is_failed_and_traced(session, trace_path, failing_send):
    echo, reply = say(session, "send the note to Alex", "yes")
    assert reply == TOOL_FAILED_REPLY and session.world.outbox == []
    assert session.machine.get("act-1").state is State.FAILED
    events = read_events(trace_path)
    (failed,) = [e["payload"] for e in events if e["event_type"] == "tool_failed"]
    assert failed == {"action_id": "act-1", "tool": "send_note",
                      "params": {"note_id": "note-2", "recipient": "Alex Chen"},
                      "error": "ConnectionError: mail relay unreachable", "retried": False}
    assert not any(e["event_type"] == "tool_executed" for e in events)
    turn = [e["payload"] for e in events if e["event_type"] == "voice_turn"][-1]
    assert turn["outcome"] == "failed" and turn["execution"]["error"].startswith("ConnectionError")
    assert verify_run(events).passed  # a failure is not an unverified action


def test_a_failed_action_is_never_retried(session, trace_path, failing_send):
    say(session, "send the note to Alex", "yes")
    assert say(session, "yes", "yes") == ["That didn't work earlier, and I won't retry it automatically."] * 2
    assert len(failing_send) == 1  # the tool was called exactly once
    action = session.machine.get("act-1")
    assert action.state is State.FAILED and action.receipt is None and action.stale_receipt
    with pytest.raises(UnverifiedAction):  # replaying the spent receipt cannot re-run it
        session.tools.execute("send_note", {"note_id": "note-2", "recipient": "Alex Chen"}, action.stale_receipt)
    assert len(failing_send) == 1
    ignored = [e["payload"]["reason"] for e in read_events(trace_path) if e["event_type"] == "confirmation_ignored"]
    assert ignored == ["failed; a failed action is never retried"] * 2


def test_a_failing_reversible_tool_is_traced_too(session, trace_path, monkeypatch):
    monkeypatch.setattr(VoiceTools, "_take_note", lambda self, text: (_ for _ in ()).throw(OSError("disk full")))
    assert say(session, "take a note: buy stamps") == [TOOL_FAILED_REPLY]
    (failed,) = [e["payload"] for e in read_events(trace_path) if e["event_type"] == "tool_failed"]
    assert failed["tool"] == "take_note" and failed["tier"] == "reversible" and failed["retried"] is False
    assert len(session.world.notes) == 2


# 4. Audio bytes go in, and never come out in any trace record

def test_marked_audio_bytes_never_appear_in_any_trace_record(trace_path):
    marker = b"RAW-PCM-AUDIO-\x00\x01\xfe-SENSEI-MARKER-7731"
    stt = FakeSTT()
    session = VoiceSession(TraceWriter(trace_path, run_id="audio", session_id="audio"), stt=stt)
    for text in ("send the note to Alex", "yes", "what's on my calendar today"):
        session.turn(ScriptedUtterance(text, 0.95, audio=marker))
    assert stt.audio_bytes_seen == len(marker)  # the bytes really went through the STT adapter
    raw = trace_path.read_bytes()
    printable = marker.decode("latin-1")
    for form in (marker, marker.hex().encode(), base64.b64encode(marker), b"SENSEI-MARKER-7731",
                 printable.encode("utf-8"), repr(marker).encode()):
        assert form not in raw, form
    events = read_events(trace_path)
    assert len([e for e in events if e["event_type"] == "voice_turn"]) == 3
    assert not any("SENSEI-MARKER" in str(e) for e in events)

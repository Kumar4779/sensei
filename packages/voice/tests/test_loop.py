"""The reference loop: core's gate reused, per-turn trace records, no audio, clarification over guessing."""

import inspect
import json
from pathlib import Path

import pytest

from sensei_core.permissions import Outcome, PermissionGate, Tier
from sensei_core.trace import read_events

from voice import cli
from voice.adapters import FakeSTT, ScriptedUtterance
from voice.world import VoiceTools, voice_tools

from conftest import say

TURN_FIELDS = {"turn", "transcript", "stt_confidence", "intent", "decision", "action_id", "proposed", "echo_text",
               "confirmation", "execution", "outcome", "reply", "latency_ms"}


def test_the_manifest_marks_send_pay_delete_submit_and_approve_irreversible():
    specs = voice_tools()
    irreversible = {name for name, spec in specs.items() if spec.tier is Tier.IRREVERSIBLE}
    assert irreversible == {"send_note", "pay", "delete_note", "submit_form", "approve_pending"}
    gate = PermissionGate(specs)
    assert gate.decide("send_note", "LOCAL_ONLY").outcome is Outcome.HALT_FOR_APPROVAL
    assert gate.decide("calendar_query", "LOCAL_ONLY").outcome is Outcome.ALLOW
    assert gate.decide("shell", "LOCAL_ONLY").outcome is Outcome.DENY  # not in the voice manifest


def _implemented_tools(cls=VoiceTools):
    """Every tool the voice World implements: VoiceTools dispatches tool <name> to its `_<name>` method."""
    return {name[1:] for name, member in inspect.getmembers(cls, inspect.isfunction)
            if name.startswith("_") and not name.startswith("__")}


def test_every_implemented_tool_has_a_manifest_entry():
    # Slice 4, salvaged: a tool with no manifest entry has no tier, so the gate could never decide it.
    implemented, manifest = _implemented_tools(), set(voice_tools())
    assert implemented - manifest == set(), "tools implemented without a tools.toml entry"
    assert manifest - implemented == set(), "tools.toml entries with no implementation"
    assert len(implemented) == 10


def test_the_coverage_check_catches_an_unlisted_tool():
    class WithExtraTool(VoiceTools):
        def _wire_money(self, amount):  # implemented, but never added to tools.toml
            raise AssertionError("never called")

    assert _implemented_tools(WithExtraTool) - set(voice_tools()) == {"wire_money"}


def test_the_voice_loop_console_script_resolves_to_the_cli():
    from importlib.metadata import entry_points
    (script,) = entry_points(group="console_scripts", name="voice-loop")
    assert script.value == "voice.cli:main" and script.load() is cli.main


def test_the_loop_uses_cores_permission_gate_and_traces_its_decisions(session, trace_path):
    assert type(session.gate) is PermissionGate
    say(session, "send the note to Alex", "yes", "read me my morning brief")
    decisions = [e["payload"] for e in read_events(trace_path) if e["event_type"] == "permission_decision"]
    assert [(d["tool"], d["outcome"]) for d in decisions] == [("send_note", "HALT_FOR_APPROVAL"),
                                                              ("morning_brief", "ALLOW")]


def test_a_tool_the_gate_denies_is_refused(trace_path):
    from sensei_core.trace import TraceWriter
    from voice.loop import VoiceSession
    specs = {k: v for k, v in voice_tools().items() if k != "take_note"}  # the gate no longer knows take_note
    session = VoiceSession(TraceWriter(trace_path, run_id="r", session_id="s"), specs=specs)
    (reply,) = say(session, "take a note: buy stamps")
    assert reply == "I can't do that." and len(session.world.notes) == 2


def test_every_turn_writes_one_full_trace_record(session, trace_path):
    say(session, "send the note to Alex", "yes", "what's on my calendar today")
    turns = [e["payload"] for e in read_events(trace_path) if e["event_type"] == "voice_turn"]
    assert len(turns) == 3 and all(set(t) >= TURN_FIELDS for t in turns)
    echo, confirm, read = turns
    assert echo["intent"] == {"tool": "send_note", "params": {"note_id": "note-2", "recipient": "Alex Chen"}}
    assert echo["proposed"] == echo["intent"] and echo["echo_text"].startswith("Just to confirm")
    assert confirm["confirmation"] == {"affirm": True, "mentions": {}}
    assert confirm["execution"]["result"] == {"note_id": "note-2", "recipient": "Alex Chen"}
    assert read["execution"]["tool"] == "calendar_query"
    for t in turns:
        assert t["stt_confidence"] == 0.95
        assert set(t["latency_ms"]) == {"stt", "understand_and_act", "time_to_first_audio", "turn"}
        assert all(v >= 0 for v in t["latency_ms"].values())


def test_audio_is_never_stored(session, trace_path, tmp_path):
    say(session, "send the note to Alex", "yes")
    text = trace_path.read_text()
    assert "audio" not in json.dumps([e["payload"] for e in read_events(trace_path)]).replace("time_to_first_audio", "")
    assert sorted(p.name for p in trace_path.parent.iterdir()) == ["run.jsonl"]  # nothing but the transcript trace
    assert "Offsite agenda" in text  # the transcript-level record is what is kept


def test_a_new_command_supersedes_an_unanswered_echo(session, trace_path):
    say(session, "send the note to Alex", "read me my morning brief", "yes")
    assert session.world.outbox == []
    reasons = [e["payload"]["reason"] for e in read_events(trace_path) if e["event_type"] == "action_declined"]
    assert reasons == ["superseded by a new command"]


def test_a_low_confidence_yes_does_not_confirm(session):
    say(session, "send the note to Alex")
    (reply,) = say(session, "yes", confidence=0.3)
    assert "yes or no" in reply and session.world.outbox == []
    say(session, "yes")
    assert len(session.world.outbox) == 1


@pytest.mark.parametrize("utterance, question", [
    ("send the note to Sam", "Which Sam do you mean: Sam Patel or Sam Okoye?"),
    ("send it", "What would you like me to send, and to whom?"),
    ("send the note to Morgan", "I don't have a contact called Morgan. Who should it go to?"),
    ("approve the request", "Which request: expense or travel?"),
    ("and move it to friday", "Which event should I move?"),
    ("spend the note to alex", "Sorry, I didn't catch that. Could you say it another way?"),
])
def test_ambiguity_clarifies_instead_of_guessing(session, trace_path, utterance, question):
    assert say(session, utterance) == [question]
    assert not any(e["event_type"] in ("action_proposed", "tool_executed") for e in read_events(trace_path))


def test_fake_stt_rejects_impossible_confidence():
    with pytest.raises(ValueError):
        FakeSTT().transcribe(ScriptedUtterance("hi", 1.5))


def test_the_package_never_reads_the_eval_set():
    package = Path(cli.__file__).parent
    for source in package.glob("*.py"):
        assert "evals" not in source.read_text() and "voice-bench" not in source.read_text(), source.name


def test_cli_runs_a_session_and_reports_the_gate(tmp_path, monkeypatch, capsys):
    import io
    trace = tmp_path / "cli.jsonl"
    monkeypatch.setattr("sys.stdin", io.StringIO("send the note to Alex\nyes\n~0.3 pay invoice 204\n"))
    assert cli.main(["--trace", str(trace)]) == 0
    out = capsys.readouterr().out
    assert "Sent the note to Alex Chen." in out and "didn't catch that clearly" in out and "hard gate: PASS" in out

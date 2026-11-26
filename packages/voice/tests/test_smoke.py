"""Slice 1 smoke tests (spec §4, Sunday block): the five required outcomes.

(a) "send the note to Alex" with the echo confirmed executes exactly once.
(b) The same command with the echo declined does not execute.
(c) An entitled read-only task succeeds (positive control).
(d) An action attempted without a matching echo + confirmation fails the run (the hard gate).
(e) A double confirmation executes the action once.
"""

import pytest

from sensei_core.trace import read_events

from voice.confirm import UnverifiedAction
from voice.verify import verify_run

from conftest import say


def _sent(session):
    return [m for m in session.world.outbox if m["recipient"] == "Alex Chen"]


def _executions(trace_path, tool):
    return [e for e in read_events(trace_path) if e["event_type"] == "tool_executed" and e["payload"]["tool"] == tool]


def test_a_confirmed_echo_executes_exactly_once(session, trace_path):
    echo, done = say(session, "send the note to Alex", "yes")
    assert echo.startswith("Just to confirm") and "Alex Chen" in echo and "Offsite agenda" in echo
    assert done == "Sent the note to Alex Chen."
    assert len(_sent(session)) == 1 and len(_executions(trace_path, "send_note")) == 1
    assert verify_run(read_events(trace_path)).passed and not session.run_failed


def test_b_a_declined_echo_does_not_execute(session, trace_path):
    echo, reply = say(session, "send the note to Alex", "no")
    assert "Alex Chen" in echo and reply == "Okay, cancelled. Nothing was done."
    assert _sent(session) == [] and _executions(trace_path, "send_note") == []
    assert any(e["event_type"] == "action_declined" for e in read_events(trace_path))
    assert verify_run(read_events(trace_path)).passed


def test_c_positive_control_a_read_only_task_succeeds(session, trace_path):
    (reply,) = say(session, "What's on my calendar tomorrow?")
    assert reply == "On Tuesday December 1: Design review at 11:00."
    turn = next(e["payload"] for e in read_events(trace_path) if e["event_type"] == "voice_turn")
    assert turn["decision"] == "ALLOW" and turn["outcome"] == "executed" and turn["echo_text"] is None


@pytest.mark.parametrize("attempt", ["no_echo_at_all", "from_proposed", "mismatched_params", "replayed_receipt"])
def test_d_an_action_without_a_matching_echo_and_confirmation_fails_the_run(session, trace_path, attempt):
    machine, tools = session.machine, session.tools
    params = {"note_id": "note-2", "recipient": "Alex Chen"}
    if attempt == "no_echo_at_all":
        receipt = None
    elif attempt == "from_proposed":
        action = machine.propose("send_note", params)
        receipt = "forged-" + action.action_id
    elif attempt == "mismatched_params":
        say(session, "send the note to Alex", "yes")  # a legitimate, verified send first
        action = machine.propose("send_note", params)
        machine.echo(action.action_id, lambda tool, p: "echo")
        machine.confirm(action.action_id, params)
        receipt, params = action.receipt, {**params, "recipient": "Jordan Lee"}
    else:
        action = machine.propose("send_note", params)
        machine.echo(action.action_id, lambda tool, p: "echo")
        machine.confirm(action.action_id, params)
        receipt = action.receipt
        tools.execute("send_note", params, receipt)  # first use is legitimate
    before = list(session.world.outbox)
    with pytest.raises(UnverifiedAction):
        tools.execute("send_note", params, receipt)
    assert session.world.outbox == before  # the tool never ran
    assert session.run_failed
    verdict = verify_run(read_events(trace_path))
    assert not verdict.passed and verdict.violations


def test_d_a_forged_trace_execution_without_confirmation_fails_the_run(session, trace_path):
    say(session, "send the note to Alex")  # echoed, never confirmed
    events = read_events(trace_path)
    echoed = next(e for e in events if e["event_type"] == "action_echoed")["payload"]
    forged = {"event_type": "tool_executed", "payload": {"tool": "send_note", "tier": "irreversible",
                                                         "action_id": echoed["action_id"], "params": echoed["params"]}}
    verdict = verify_run(events + [forged])
    assert not verdict.passed and "without a matching confirmation" in verdict.violations[0]


def test_e_a_double_confirmation_executes_once(session, trace_path):
    echo, done, again = say(session, "send the note to Alex", "yes", "yes")
    assert done == "Sent the note to Alex Chen." and again == "That's already done. I won't do it twice."
    assert len(_sent(session)) == 1 and len(_executions(trace_path, "send_note")) == 1
    events = read_events(trace_path)
    assert [e["payload"]["reason"] for e in events if e["event_type"] == "confirmation_ignored"] == [
        "already confirmed; an action runs at most once"]
    assert verify_run(events).passed

"""Interruption (Slice 2): barge-in at three points, stop and re-plan, and the stale-plan guard in code."""

import hashlib
import json
from pathlib import Path

import pytest

from sensei_core.trace import TraceWriter, read_events

from voice.adapters import FakeTTS, ScriptedUtterance
from voice.confirm import State, UnverifiedAction
from voice.loop import VoiceSession
from voice.verify import verify_run

INTERRUPT_PATH = Path(__file__).resolve().parents[1] / "evals" / "voice-interrupt-v1" / "tasks.json"
INTERRUPT_SHA256 = "7d34a924754fa3118524209f414058574346c6fb24b4c13e1d00fde1af5057d1"


@pytest.fixture
def tts():
    return FakeTTS()


@pytest.fixture
def live(trace_path, tts):
    return VoiceSession(TraceWriter(trace_path, run_id="int", session_id="int"), tts=tts)


def turn(session, text, barge=None):
    return session.turn(ScriptedUtterance(text, 0.95, barge))


def barges(trace_path):
    return [e for e in read_events(trace_path) if e["event_type"] == "barge_in"]


@pytest.mark.parametrize("first, point", [("Read me my morning brief.", "mid-answer"),
                                          ("Send the note to Alex.", "mid-echo"),
                                          ("What's on my calendar today?", "mid-listing")])
def test_barge_in_is_a_traced_event_at_each_point(live, trace_path, tts, first, point):
    turn(live, first)
    turn(live, "Stop.", 1)
    (event,) = barges(trace_path)
    p = event["payload"]
    assert p["point"] == point and p["stopped"] is True and p["segments_heard"] == 1
    assert p["segments_heard"] < p["segments_total"] and p["at"].endswith("+00:00") and event["timestamp"]
    assert tts.stops == [1]
    last = [e["payload"] for e in read_events(trace_path) if e["event_type"] == "voice_turn"][-1]
    assert last["after_barge_in"] == point


def test_mid_answer_stop_stops_and_never_finishes_the_stale_answer(live, tts):
    brief = turn(live, "Read me my morning brief.")
    assert turn(live, "Stop.", 1) == "Okay, I've stopped."
    rest = brief.split(". ", 1)[1]
    assert all(rest not in said for said in tts.spoken[1:])


def test_mid_answer_redirect_re_plans_with_the_new_question(live):
    turn(live, "Read me my morning brief.")
    assert turn(live, "What's on my calendar tomorrow?", 1) == "On Tuesday December 1: Design review at 11:00."


def test_a_yes_over_the_echo_re_issues_the_echo_instead_of_executing(live, trace_path):
    turn(live, "Send the note to Alex.")
    reply = turn(live, "Yes.", 1)
    assert reply.startswith("You interrupted me, so let me read that back again. Just to confirm")
    assert live.world.outbox == []
    declined = [e["payload"] for e in read_events(trace_path) if e["event_type"] == "action_declined"]
    assert declined[0]["action_id"] == "act-1" and declined[0]["reason"].startswith("interrupted mid-echo")
    assert turn(live, "Yes.") == "Sent the note to Alex Chen."
    executed = [e["payload"] for e in read_events(trace_path) if e["event_type"] == "tool_executed"]
    assert len(live.world.outbox) == 1 and [e["action_id"] for e in executed] == ["act-2"]  # the re-echoed plan
    assert verify_run(read_events(trace_path)).passed


def test_a_correction_over_the_echo_discards_the_old_plan(live):
    turn(live, "Send the note to Alex.")
    echo = turn(live, "Actually, send the note to Jordan instead.", 1)
    assert "Jordan Lee" in echo and "Alex" not in echo
    turn(live, "Yes.")
    assert [m["recipient"] for m in live.world.outbox] == ["Jordan Lee"]
    assert live.machine.get("act-1").state is State.DECLINED


def test_stop_over_the_echo_cancels(live):
    turn(live, "Delete the note about groceries.")
    assert turn(live, "Stop.", 1) == "Okay, cancelled. Nothing was done."
    assert live.world.deleted_notes == [] and turn(live, "Yes.") == "There's nothing waiting for your confirmation."


def test_mid_listing_that_one_means_the_item_the_user_heard(live):
    turn(live, "What's on my calendar today?")  # Standup, then Dentist
    assert turn(live, "Move that one to Friday.", 2) == "Moved Standup to Friday December 4."
    assert next(e for e in live.world.events if e["id"] == "evt-2")["date"] == "2026-11-30"  # unheard: untouched


def test_mid_listing_before_any_item_was_heard_clarifies(live):
    turn(live, "What's on my calendar today?")
    assert turn(live, "Move that one to Friday.", 1) == "Which event should I move?"


def test_without_the_listing_guard_that_one_would_be_ambiguous(live):
    turn(live, "What's on my calendar today?")  # heard in full: two events
    assert turn(live, "Move that one to Friday.") == "Which event should I move?"


def test_a_barge_in_after_the_reply_finished_is_not_an_interruption(live, trace_path, tts):
    turn(live, "Read me my morning brief.")
    turn(live, "What's on my calendar tomorrow?", 99)
    assert barges(trace_path) == [] and tts.stops == []


# The stale-plan guard, enforced in code: a plan formed before an interruption never executes unchanged.

def test_the_machine_refuses_to_confirm_a_plan_from_before_the_interruption(live, trace_path):
    turn(live, "Send the note to Alex.")
    live.machine.interrupt("mid-echo")
    with pytest.raises(UnverifiedAction):
        live.machine.confirm("act-1", {"note_id": "note-2", "recipient": "Alex Chen"})
    assert live.run_failed and live.world.outbox == []
    assert "stale_plan" in verify_run(read_events(trace_path)).kinds


def test_a_receipt_issued_before_the_interruption_cannot_be_redeemed(live, trace_path):
    params = {"note_id": "note-2", "recipient": "Alex Chen"}
    action = live.machine.propose("send_note", params)
    live.machine.echo(action.action_id, lambda tool, p: "echo")
    live.machine.confirm(action.action_id, params)
    receipt = action.receipt
    live.machine.interrupt("mid-answer")  # confirmed, not yet executed: now stale
    with pytest.raises(UnverifiedAction, match="before an interruption"):
        live.tools.execute("send_note", params, receipt)
    assert live.world.outbox == [] and "stale_plan" in verify_run(read_events(trace_path)).kinds


def test_the_verifier_fails_a_trace_that_executes_across_a_barge_in(live, trace_path):
    turn(live, "Send the note to Alex.")
    turn(live, "Yes.")
    events = read_events(trace_path)
    echo_at = next(i for i, e in enumerate(events) if e["event_type"] == "action_echoed")
    forged = events[:echo_at + 1] + [{"event_type": "barge_in", "payload": {"point": "mid-echo"}}] + events[echo_at + 1:]
    verdict = verify_run(forged)
    assert not verdict.passed and set(verdict.kinds) == {"stale_plan"}


def test_echo_off_marks_confirmations_automatic_and_fails_the_gate(trace_path):
    session = VoiceSession(TraceWriter(trace_path, run_id="off", session_id="off"), echo=False)
    assert turn(session, "Send the note to Alex.") == "Sent the note to Alex Chen."
    events = read_events(trace_path)
    assert [e["payload"]["source"] for e in events if e["event_type"] == "action_confirmed"] == ["ablation_auto"]
    verdict = verify_run(events)
    assert not verdict.passed and verdict.kinds == ("unconfirmed",)


def test_the_interrupt_task_set_is_frozen_and_covers_all_three_points():
    data = json.loads(INTERRUPT_PATH.read_text())
    assert data["eval_version"] == "voice-interrupt-v1" and data["frozen"] is True
    assert hashlib.sha256(INTERRUPT_PATH.read_bytes()).hexdigest() == INTERRUPT_SHA256
    assert {t["point"] for t in data["tasks"]} == {"mid-answer", "mid-echo", "mid-listing"}
    for t in data["tasks"]:
        assert t["turns"][t["barge_turn"]]["barge_in_after"] is not None and len(t["outcomes"]) == len(t["turns"])

"""Demo Slice 1: the ledger stream against scripted sessions.

Events arrive in trace order, and no tool_executed ever precedes its action's
echo and confirmation. The pay decline shows a decline and no execution.
"""

import threading
import time

import pytest

from sensei_core.trace import read_events

from sensei_demo.ledger import Ledger, step

from conftest import AUTH, ledger, new_session, parse_sse, say

SCRIPTS = {"brief": ["Read me my morning brief."], "note": ["Send the note to Alex.", "Yes."],
           "pay": ["Pay invoice 204.", "No."]}


def assert_no_execution_before_its_echo_and_confirmation(events):
    """The invariant a visitor watches: an irreversible tool_executed follows its echo and its confirmation."""
    seen: dict[str, set[str]] = {}
    for e in events:
        p = e["payload"]
        if e["event_type"] in ("action_proposed", "action_echoed", "action_confirmed", "action_declined"):
            seen.setdefault(p["action_id"], set()).add(e["event_type"])
        if e["event_type"] == "tool_executed" and p["tier"] == "irreversible":
            assert {"action_proposed", "action_echoed", "action_confirmed"} <= seen.get(p["action_id"], set()), e
            assert "action_declined" not in seen[p["action_id"]]


@pytest.mark.parametrize("scenario", SCRIPTS)
def test_the_ledger_is_the_trace_in_order(simulated, tmp_path, scenario):
    sid = new_session(simulated, scenario)
    for line in SCRIPTS[scenario]:
        say(simulated, sid, line)
    streamed = ledger(simulated, sid)
    trace = read_events(tmp_path / "traces" / f"demo-{sid}.jsonl")
    assert [r["index"] for r in streamed] == list(range(len(trace)))
    assert [(r["event_type"], r["payload"]) for r in streamed] == [(e["event_type"], e["payload"]) for e in trace]
    assert [r["timestamp"] for r in streamed] == sorted(r["timestamp"] for r in streamed)
    assert_no_execution_before_its_echo_and_confirmation(streamed)


def kinds(records):
    return [r["step"]["kind"] for r in records if r["step"]["kind"] != "turn"]


def test_the_brief_is_allowed_and_executed_with_nothing_to_approve(simulated):
    sid = new_session(simulated, "brief")
    say(simulated, sid, "Read me my morning brief.")
    records = ledger(simulated, sid)
    assert kinds(records) == ["gate", "executed", "verified"]
    assert records[0]["step"]["title"] == "Gate: morning brief allowed"
    assert records[-1]["step"]["detail"] == "Nothing irreversible ran."


def test_the_note_is_proposed_echoed_approved_executed_and_verified(simulated):
    sid = new_session(simulated, "note")
    say(simulated, sid, "Send the note to Alex.")
    say(simulated, sid, "Yes.")
    records = ledger(simulated, sid)
    assert kinds(records) == ["gate", "proposed", "echoed", "verified", "approved", "executed", "verified"]
    titles = [r["step"]["title"] for r in records]
    assert "Gate: send note needs your approval" in titles and "Approved by you" in titles
    echo = next(r for r in records if r["step"]["kind"] == "echoed")
    assert "Alex Chen" in echo["step"]["detail"]
    last = records[-1]["step"]
    assert last["title"] == "Verified: hard gate PASS" and last["detail"].startswith("1 irreversible action ran")


def test_the_pay_decline_shows_a_decline_and_nothing_executing(simulated):
    sid = new_session(simulated, "pay")
    say(simulated, sid, "Pay invoice 204.")
    say(simulated, sid, "No.")
    records = ledger(simulated, sid)
    assert kinds(records) == ["gate", "proposed", "echoed", "verified", "denied", "verified"]
    assert not any(r["event_type"] == "tool_executed" for r in records)
    declined = next(r for r in records if r["event_type"] == "action_declined")
    assert declined["step"]["title"] == "Declined: nothing will run" and declined["payload"]["reason"] == "user said no"
    assert records[-1]["step"]["detail"] == "Nothing irreversible ran."


def test_a_reconnecting_stream_resumes_after_the_last_event_it_saw(simulated):
    sid = new_session(simulated, "note")
    say(simulated, sid, "Send the note to Alex.")
    first = ledger(simulated, sid)
    say(simulated, sid, "Yes.")
    rest = ledger(simulated, sid, after=len(first))
    assert [r["index"] for r in rest] == list(range(len(first), len(first) + len(rest)))
    r = simulated.get(f"/api/sessions/{sid}/ledger", params={"follow": "false"},
                      headers={**AUTH, "Last-Event-ID": str(len(first) - 1)})
    assert [e["index"] for e in parse_sse(r.text)] == [e["index"] for e in rest]


def test_a_followed_stream_delivers_events_while_the_turn_is_happening(simulated):
    """follow=1: the stream stays open, and each event arrives as the trace writes it."""
    sid = new_session(simulated, "note")
    got: list[dict] = []
    done = threading.Event()

    def watch():
        with simulated.stream("GET", f"/api/sessions/{sid}/ledger", headers=AUTH) as r:
            buffer = ""
            for chunk in r.iter_text():
                buffer += chunk
                *blocks, buffer = buffer.split("\n\n")
                got.extend(parse_sse("\n\n".join(blocks) + "\n\n"))
                if any(e["event_type"] == "tool_executed" for e in got) and got[-1]["event_type"] == "hard_gate_check":
                    break
        done.set()

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    time.sleep(0.3)
    assert got == []  # open, waiting, nothing yet
    say(simulated, sid, "Send the note to Alex.")
    say(simulated, sid, "Yes.")
    assert done.wait(10), "the followed stream never delivered the execution"
    assert [e["index"] for e in got] == list(range(len(got)))
    assert_no_execution_before_its_echo_and_confirmation(got)


def test_wait_after_wakes_on_publish_and_times_out_when_quiet():
    led = Ledger()
    assert led.wait_after(0, timeout=0.05) == []
    threading.Timer(0.05, lambda: led.publish({"event_type": "tool_executed", "timestamp": "t",
                                               "payload": {"tool": "pay", "tier": "irreversible"}})).start()
    started = time.monotonic()
    records = led.wait_after(0, timeout=5)
    assert len(records) == 1 and time.monotonic() - started < 2
    assert records[0]["step"]["title"] == "Executed: pay"


def test_a_blocked_action_and_a_failed_gate_read_as_violations():
    assert step("hard_gate_violation", {"reason": "no echo"})["kind"] == "violation"
    failed = step("hard_gate_check", {"passed": False, "violations": ["a1 confirmed without a matching echo"]})
    assert failed == {"kind": "violation", "title": "Verified: hard gate FAIL",
                      "detail": "a1 confirmed without a matching echo"}
    assert step("permission_decision", {"tool": "shell", "outcome": "DENY"})["title"] == "Gate: shell denied"


def test_the_invariant_check_catches_an_execution_before_its_confirmation():
    def e(kind, **p):
        return {"event_type": kind, "payload": {"action_id": "a1", **p}}
    honest = [e("action_proposed"), e("action_echoed"), e("action_confirmed"), e("tool_executed", tier="irreversible")]
    assert_no_execution_before_its_echo_and_confirmation(honest)
    early = [e("action_proposed"), e("action_echoed"), e("tool_executed", tier="irreversible"), e("action_confirmed")]
    with pytest.raises(AssertionError):
        assert_no_execution_before_its_echo_and_confirmation(early)
    declined = honest[:2] + [e("action_declined"), e("action_confirmed"), e("tool_executed", tier="irreversible")]
    with pytest.raises(AssertionError):
        assert_no_execution_before_its_echo_and_confirmation(declined)

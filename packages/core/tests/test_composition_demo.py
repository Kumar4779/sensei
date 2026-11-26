"""Slice 25: the composition demo's evidence logic (the live run itself needs a person and a phone)."""

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[3] / "scripts" / "composition_demo.py"
spec = importlib.util.spec_from_file_location("composition_demo", SCRIPT)
demo = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = demo  # @dataclass needs the module registered
spec.loader.exec_module(demo)

EVENTS = [
    {"event_type": "brief_generated", "timestamp": "2026-11-25T19:00:00+00:00", "payload": {"verified": False}},
    {"event_type": "brief_generated", "timestamp": "2026-11-25T19:01:00+00:00", "payload": {"verified": True}},
    {"event_type": "memory_confirmed", "timestamp": "2026-11-25T19:05:00+00:00", "payload": {"entry_id": "abc"}},
]


def test_find_event_matches_type_payload_and_time():
    assert demo.find_event(EVENTS, "brief_generated", verified=True)["timestamp"].startswith("2026-11-25T19:01")
    assert demo.find_event(EVENTS, "memory_confirmed", entry_id="other") is None
    assert demo.find_event(EVENTS, "memory_confirmed", after="2026-11-25T19:06") is None


def test_a_claim_without_evidence_is_reported_not_done():
    done = demo.Claim("brief verified", demo.cite(EVENTS[1], "gateway trace"))
    missing = demo.Claim("phone approval", [])
    out = demo.format_summary([done, missing])
    assert "[DONE] brief verified" in out and "brief_generated @ 2026-11-25T19:01:00+00:00" in out
    assert "[NOT DONE] phone approval" in out and "no trace event proves it" in out
    assert "1/2 claims proven by the trace." in out


def test_macpilot_leg_counts_only_goal_reached_audit_lines(tmp_path):
    audit = tmp_path / "audit.jsonl"
    assert demo.macpilot_goal_lines(audit) == []  # no audit log, no evidence
    audit.write_text('{"step": 1, "outcome": "goal_not_reached"}\n{"step": 1, "outcome": "error"}\n')
    assert demo.macpilot_goal_lines(audit) == []
    audit.write_text('{"step": 1, "outcome": "goal_reached", "stop_condition": "the page shows \'Clicked\'"}\n')
    assert demo.macpilot_goal_lines(audit) == [audit.read_text().strip()]


def test_macpilot_run_without_its_venv_reports_instead_of_faking(tmp_path):
    code, out = demo.macpilot_run(tmp_path / "out", tmp_path / "data", root=tmp_path)
    assert code == -1 and "not found" in out and not (tmp_path / "out" / "audit.jsonl").exists()


def test_phone_lines_only_count_the_phones_ip(tmp_path):
    log = tmp_path / "access.log"
    log.write_text('INFO:     127.0.0.1:5000 - "POST /memory/confirm HTTP/1.1" 200 OK\n'
                   'INFO:     100.75.13.33:0 - "GET /memory/pending HTTP/1.1" 200 OK\n'
                   'INFO:     100.75.13.33:0 - "POST /memory/confirm HTTP/1.1" 200 OK\n')
    lines = demo.phone_access_lines(log, "100.75.13.33", "/memory/confirm")
    assert lines == ['INFO:     100.75.13.33:0 - "POST /memory/confirm HTTP/1.1" 200 OK']
    assert demo.phone_access_lines(log, None, "/memory/confirm") == []

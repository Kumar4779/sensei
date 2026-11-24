import pytest

from sensei_core.config import ConfigError
from sensei_core.permissions import Outcome, PermissionGate, Tier, load_tools
from sensei_core.trace import TraceWriter, read_events


@pytest.fixture
def gate():
    return PermissionGate(load_tools())


def test_default_manifest_loads():
    tools = load_tools()
    assert set(tools) == {"web_search", "read_file", "write_file", "send_email", "shell"}
    assert tools["send_email"].tier is Tier.IRREVERSIBLE
    assert tools["send_email"].endpoints == ("mail-provider",)


def test_read_file_local_only_is_allowed(gate):
    assert gate.decide("read_file", "LOCAL_ONLY").outcome is Outcome.ALLOW


def test_send_email_api_ok_halts_for_approval(gate):
    assert gate.decide("send_email", "API_OK").outcome is Outcome.HALT_FOR_APPROVAL


def test_send_email_local_only_is_denied_class_beats_tier(gate):
    d = gate.decide("send_email", "LOCAL_ONLY")
    assert d.outcome is Outcome.DENY
    assert "cannot be approved" in d.reason


def test_unregistered_tool_is_denied(gate):
    d = gate.decide("delete_database", "API_OK")
    assert d.outcome is Outcome.DENY
    assert "not in manifest" in d.reason


def test_each_decision_writes_one_trace_event(tmp_path):
    trace = TraceWriter(tmp_path / "run.jsonl", run_id="r1", session_id="s1")
    gate = PermissionGate(load_tools(), trace=trace)
    d = gate.decide("send_email", "API_OK")
    events = read_events(trace.path)
    assert len(events) == 1
    e = events[0]
    assert e["event_type"] == "permission_decision"
    assert e["payload"] == {
        "tool": "send_email",
        "sensitivity_class": "API_OK",
        "outcome": d.outcome.value,
        "reason": d.reason,
    }
    assert e["payload"]["outcome"] == "HALT_FOR_APPROVAL"


def _manifest(tmp_path, body):
    p = tmp_path / "tools.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_unknown_tier_is_rejected(tmp_path):
    p = _manifest(tmp_path, '[tools.x]\ntier = "admin"\nsensitivity = ["API_OK"]\nendpoints = []\n')
    with pytest.raises(ConfigError, match="unknown tier"):
        load_tools(p)


def test_egress_tool_may_not_accept_local_only(tmp_path):
    p = _manifest(
        tmp_path,
        '[tools.x]\ntier = "read"\nsensitivity = ["LOCAL_ONLY"]\nendpoints = ["somewhere"]\n',
    )
    with pytest.raises(ConfigError, match="LOCAL_ONLY"):
        load_tools(p)

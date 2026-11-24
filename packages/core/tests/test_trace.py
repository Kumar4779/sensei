from sensei_core.trace import REDACTED, TraceWriter, read_events

# Fake secrets, assembled at runtime so secret scanners don't flag this file.
FAKE_OPENAI = "sk-" + "FAKEfake0123456789"
FAKE_GITHUB = "ghp_" + "FAKE" * 9
FAKE_AWS = "AKIA" + "FAKE" * 4
FAKE_BEARER = "fake.jwt." + "token123"
FAKE_PASSWORD = "hunter2-fake"


def _writer(tmp_path):
    return TraceWriter(tmp_path / "traces" / "run.jsonl", run_id="r1", session_id="s1")


def test_planted_fake_keys_are_redacted_on_disk(tmp_path):
    w = _writer(tmp_path)
    w.write(
        "tool_call",
        {
            "key": FAKE_OPENAI,
            "nested": {"gh": f"token is {FAKE_GITHUB}", "list": [FAKE_AWS]},
            "header": f"Authorization: Bearer {FAKE_BEARER}",
            "cmd": f"login password={FAKE_PASSWORD} --user me",
        },
    )
    raw = w.path.read_text(encoding="utf-8")
    for secret in (FAKE_OPENAI, FAKE_GITHUB, FAKE_AWS, FAKE_BEARER, FAKE_PASSWORD):
        assert secret not in raw
    payload = read_events(w.path)[0]["payload"]
    assert payload["key"] == REDACTED
    assert payload["nested"]["gh"] == f"token is {REDACTED}"
    assert payload["nested"]["list"] == [REDACTED]
    assert payload["header"] == f"Authorization: Bearer {REDACTED}"
    assert payload["cmd"] == f"login password={REDACTED} --user me"


def test_appends_never_rewrite_earlier_lines(tmp_path):
    w = _writer(tmp_path)
    w.write("plan", {"step": 1})
    first = w.path.read_bytes()
    w.write("plan", {"step": 2})
    after = w.path.read_bytes()
    lines = after.splitlines(keepends=True)
    assert len(lines) == 2
    assert lines[0] == first
    assert after.startswith(first)


def test_read_events_round_trips(tmp_path):
    w = _writer(tmp_path)
    written = [
        w.write("plan", {"goal": "morning brief"}),
        w.write("model_call", {"model": "qwen", "tokens": 812}, cost_usd=0.0),
        w.write("verdict", {"pass": True, "notes": ["ok", "日本語"]}, cost_usd=0.0042),
    ]
    events = read_events(w.path)
    assert events == written
    for e in events:
        assert set(e) == {"timestamp", "run_id", "session_id", "event_type", "payload", "cost_usd"}
        assert e["timestamp"].endswith("+00:00")
    assert events[0]["cost_usd"] is None

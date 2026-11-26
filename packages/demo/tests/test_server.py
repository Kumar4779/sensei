"""Demo Slice 1: the server's endpoints, offline, in both modes. Providers are mocked; no key, no network."""

import base64
import json
import urllib.error

import pytest
from fastapi.testclient import TestClient

from sensei_core.trace import read_events

import sensei_demo.__main__ as entry
from sensei_demo.server import (MAX_AUDIO_BYTES, DemoConfigError, bind_host, create_app)

from conftest import AUTH, KEY, MIC_AUDIO, TOKEN, TTS_AUDIO, Response, ledger, new_session, say, speak


# 1. The gateway's posture: fail closed without a token, token on every data endpoint, localhost by default

def test_the_server_refuses_to_start_without_a_token(tmp_path, monkeypatch):
    monkeypatch.delenv("SENSEI_DEMO_TOKEN", raising=False)
    with pytest.raises(DemoConfigError, match="SENSEI_DEMO_TOKEN"):
        create_app("simulated", trace_dir=tmp_path)


def test_live_mode_needs_real_adapters_and_an_unknown_mode_is_refused(tmp_path):
    with pytest.raises(DemoConfigError):
        create_app("live", trace_dir=tmp_path, token=TOKEN)
    with pytest.raises(DemoConfigError):
        create_app("loud", trace_dir=tmp_path, token=TOKEN)  # type: ignore[arg-type]


def test_it_binds_localhost_unless_lan_is_explicitly_opted_into():
    assert bind_host({}) == "127.0.0.1"
    assert bind_host({"SENSEI_DEMO_ALLOW_LAN": "true"}) == "127.0.0.1"  # only exactly "1" opts in
    assert bind_host({"SENSEI_DEMO_ALLOW_LAN": "1"}) == "0.0.0.0"


@pytest.mark.parametrize("method, path", [
    ("GET", "/api/status"), ("POST", "/api/sessions"), ("POST", "/api/sessions/x/turn"),
    ("GET", "/api/sessions/x/ledger"), ("GET", "/demo/app/app.js"), ("GET", "/demo/app/app.css")])
@pytest.mark.parametrize("headers", [{}, {"X-Sensei-Token": "wrong"}, {"X-Sensei-Token": TOKEN + "x"}])
def test_every_data_endpoint_needs_the_token(simulated, method, path, headers):
    r = simulated.request(method, path, headers=headers, json={"scenario": "brief"} if method == "POST" else None)
    assert r.status_code == 401 and "X-Sensei-Token" in r.json()["error"]


def test_health_and_the_shell_are_public_and_hold_no_data(simulated):
    assert simulated.get("/health").json() == {"status": "ok", "mode": "simulated"}
    r = simulated.get("/demo")
    assert r.status_code == 200 and r.headers["cache-control"] == "no-store"
    assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    assert "microphone=(self)" in r.headers["permissions-policy"] and r.headers["x-frame-options"] == "DENY"
    for word in ("Alex", "invoice", "Acme", "brief"):  # no demo data and no app code on the public page
        assert word not in r.text
    assert simulated.get("/", follow_redirects=False).headers["location"] == "/demo"


def test_only_the_two_app_files_are_served(simulated):
    assert simulated.get("/demo/app/app.js", headers=AUTH).status_code == 200
    assert simulated.get("/demo/app/app.css", headers=AUTH).headers["content-type"].startswith("text/css")
    for name in ("index.html", "..%2Fserver.py", "server.py"):
        assert simulated.get(f"/demo/app/{name}", headers=AUTH).status_code == 404


# 2. The honest mode badge: the page is told which mode it is, and each mode refuses the other's input

def test_status_states_simulated_mode_and_the_demo_data_label(simulated):
    s = simulated.get("/api/status", headers=AUTH).json()
    assert s["mode"] == "simulated" and s["mode_label"] == "SIMULATED" and "fake adapters" in s["mode_detail"]
    assert s["stt"]["provider"] == "fake-stt" and "spend" not in s
    assert s["demo_data"].startswith("Demo data:") and "synthetic" in s["demo_data"]
    assert [c["id"] for c in s["scenarios"]] == ["brief", "note", "pay"]
    assert s["scenarios"][2]["say"][-1] == "No."  # the pay card tells the visitor to decline


def test_status_states_live_speech_and_the_real_providers(live_factory):
    client, _, _ = live_factory()
    s = client.get("/api/status", headers=AUTH).json()
    assert (s["mode"], s["mode_label"]) == ("live", "LIVE SPEECH")
    assert s["stt"] == {"provider": "deepgram", "model": "nova-3"}
    assert s["tts"] == {"provider": "elevenlabs", "model": "eleven_flash_v2_5"}
    assert s["spend"] == {"estimated_usd": 0.0, "cap_usd": 1.0}


def test_a_simulated_server_never_takes_audio_and_a_live_one_never_takes_typing(simulated, live_factory):
    sid = new_session(simulated, "brief")
    r = speak(simulated, sid)
    assert r.status_code == 409 and "SIMULATED" in r.json()["error"]
    live, stt_api, _ = live_factory()
    sid = new_session(live, "brief")
    r = say(live, sid, "Read me my morning brief.")
    assert r.status_code == 409 and "LIVE SPEECH" in r.json()["error"] and stt_api.requests == []


# 3. Sessions and turns

def test_an_unknown_scenario_or_session_is_refused(simulated):
    assert simulated.post("/api/sessions", json={"scenario": "wire"}, headers=AUTH).status_code == 404
    assert simulated.post("/api/sessions", content=b"nope", headers={**AUTH, "Content-Type": "text/plain"}).status_code == 400
    assert say(simulated, "nope", "hello").status_code == 404
    assert simulated.get("/api/sessions/nope/ledger", headers=AUTH).status_code == 404


def test_simulated_turns_run_the_three_scenarios(simulated):
    brief = new_session(simulated, "brief")
    r = say(simulated, brief, "Read me my morning brief.").json()
    assert r["outcome"] == "executed" and r["reply"].startswith("Good morning.") and r["audio"] is None
    assert r["real_path"] is None and r["hard_gate"] == "PASS"

    note = new_session(simulated, "note")
    assert say(simulated, note, "Send the note to Alex.").json()["outcome"] == "echoed"
    r = say(simulated, note, "Yes.").json()
    assert r["outcome"] == "executed" and r["reply"] == "Sent the note to Alex Chen."
    assert len(simulated.app.state.sessions[note].voice.world.outbox) == 1

    pay = new_session(simulated, "pay")
    r = say(simulated, pay, "Pay invoice 204.").json()
    assert r["outcome"] == "echoed" and "pay Acme Paper $1,250.00" in r["reply"]
    r = say(simulated, pay, "No.").json()
    assert r["outcome"] == "declined" and r["hard_gate"] == "PASS"
    world = simulated.app.state.sessions[pay].voice.world
    assert world.payments == [] and world.invoices["INV-204"]["status"] == "open"


def test_a_typed_low_confidence_prefix_works_as_in_the_cli(simulated):
    sid = new_session(simulated, "pay")
    r = say(simulated, sid, "~0.4 Pay invoice 204.").json()
    assert r["outcome"] == "clarify" and r["confidence"] == 0.4


@pytest.mark.parametrize("text", ["", "   ", "x" * 501])
def test_typed_text_is_bounded(simulated, text):
    sid = new_session(simulated, "brief")
    assert say(simulated, sid, text).status_code == 400


def test_each_scenario_starts_from_fresh_fixtures(simulated):
    first = new_session(simulated, "note")
    say(simulated, first, "Send the note to Alex.")
    say(simulated, first, "Yes.")
    second = new_session(simulated, "note")
    assert simulated.app.state.sessions[second].voice.world.outbox == []


# 4. Live speech, with the providers mocked: audio in, audio out, real-path timings, nothing kept

def test_a_live_turn_sends_the_recording_to_deepgram_and_returns_the_spoken_reply(live_factory):
    client, stt_api, tts_api = live_factory("Send the note to Alex.", "Yes.")
    sid = new_session(client, "note")
    r = speak(client, sid).json()
    assert stt_api.requests[0].data == MIC_AUDIO  # the browser's recording, as sent
    assert r["heard"] == "Send the note to Alex." and r["confidence"] == 0.98 and r["outcome"] == "echoed"
    assert base64.b64decode(r["audio"]) == TTS_AUDIO and r["audio_type"] == "audio/mpeg"
    assert json.loads(tts_api.requests[0].data)["text"] == r["reply"]
    real = r["real_path"]
    assert real["provider"] == {"stt": "deepgram", "tts": "elevenlabs"}
    assert all(real[k] is not None and real[k] >= 0 for k in ("stt_latency_ms", "tts_first_byte_ms", "tts_total_ms",
                                                               "time_to_first_audio_ms"))
    r = speak(client, sid).json()
    assert r["outcome"] == "executed" and r["spend"]["estimated_usd"] > 0


def test_no_key_and_no_audio_reach_a_response_the_ledger_or_the_trace(live_factory, tmp_path):
    client, _, _ = live_factory("Send the note to Alex.", "Yes.")
    sid = new_session(client, "note")
    bodies = [client.get("/api/status", headers=AUTH).text, speak(client, sid).text, speak(client, sid).text,
              client.get(f"/api/sessions/{sid}/ledger", params={"follow": "false"}, headers=AUTH).text,
              client.get("/demo/app/app.js", headers=AUTH).text]
    trace = (tmp_path / "traces" / f"demo-{sid}.jsonl").read_bytes()
    for blob in bodies:
        assert KEY not in blob and base64.b64encode(MIC_AUDIO).decode() not in blob
    assert base64.b64encode(TTS_AUDIO).decode() not in bodies[3]  # the reply's audio is in its turn response only
    for marker in (KEY.encode(), MIC_AUDIO, TTS_AUDIO, b"SENSEI-DEMO-MIC", b"SENSEI-DEMO-REPLY",
                   base64.b64encode(MIC_AUDIO), base64.b64encode(TTS_AUDIO)):
        assert marker not in trace
    assert stat_mode(tmp_path / "traces" / f"demo-{sid}.jsonl") == 0o600


def stat_mode(path):
    import os
    import stat
    return stat.S_IMODE(os.stat(path).st_mode)


def test_an_oversized_or_empty_recording_is_refused_before_any_provider_call(live_factory):
    client, stt_api, _ = live_factory()
    sid = new_session(client, "brief")
    assert speak(client, sid, b"\0" * (MAX_AUDIO_BYTES + 1)).status_code == 413
    assert speak(client, sid, b"").status_code == 400
    assert stt_api.requests == []


def test_live_turns_stop_at_the_spend_cap(live_factory):
    client, stt_api, _ = live_factory("Read me my morning brief.", "Read me my morning brief.", max_usd=0.000001)
    sid = new_session(client, "brief")
    assert speak(client, sid).status_code == 200  # the first turn spends past the tiny cap
    r = speak(client, sid)
    assert r.status_code == 429 and "spend cap" in r.json()["error"] and len(stt_api.requests) == 1


def test_a_provider_failure_is_reported_and_the_gate_still_checked(live_factory):
    client, _, _ = live_factory(urllib.error.HTTPError("u", 401, "no", {}, None))
    sid = new_session(client, "brief")
    r = speak(client, sid)
    assert r.status_code == 502 and r.json()["error_class"] == "http_401" and r.json()["hard_gate"] == "PASS"
    assert KEY not in r.text
    records = ledger(client, sid)
    assert [e["event_type"] for e in records] == ["speech_error", "hard_gate_check"]  # nothing ran
    assert records[0]["payload"] == {"stage": "stt", "error_class": "http_401", "provider": "deepgram"}
    assert records[0]["step"]["title"] == "Speech error while hearing you"


def test_a_reply_that_cannot_be_spoken_still_returns_the_executed_turn(live_factory):
    client, _, _ = live_factory("Send the note to Alex.", "Yes.",
                                tts=[Response(TTS_AUDIO), urllib.error.URLError(ConnectionResetError())])
    sid = new_session(client, "note")
    speak(client, sid)
    r = speak(client, sid)
    assert r.status_code == 200 and r.json()["outcome"] == "executed" and r.json()["audio"] is None
    assert r.json()["error_class"] == "unreachable"
    assert len(client.app.state.sessions[sid].voice.world.outbox) == 1
    tts_error = [e for e in ledger(client, sid) if e["event_type"] == "speech_error"]
    assert [e["payload"]["stage"] for e in tts_error] == ["tts"]


# 5. The entry point: live by default, and it fails closed without keys instead of simulating

def test_the_entry_point_refuses_live_mode_without_keys_and_never_falls_back(monkeypatch, capsys):
    import voice.speech as speech
    monkeypatch.setenv("SENSEI_DEMO_TOKEN", TOKEN)
    monkeypatch.setattr(speech, "keychain_secret", lambda service, **kw: None)
    served = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: served.append(a))
    assert entry.main([]) == 2
    err = capsys.readouterr().err
    assert "sensei-deepgram-api-key" in err and "--simulated" in err and served == []


def test_the_entry_point_serves_simulated_only_when_asked(monkeypatch, tmp_path):
    monkeypatch.setenv("SENSEI_DEMO_TOKEN", TOKEN)
    served = []
    monkeypatch.setattr("uvicorn.run", lambda app, host, port, **kw: served.append((app, host, port)))
    assert entry.main(["--simulated", "--trace-dir", str(tmp_path)]) == 0
    app, host, port = served[0]
    assert host == "127.0.0.1" and port == 8766
    assert TestClient(app).get("/health").json()["mode"] == "simulated"


def test_the_live_entry_point_formats_spoken_numbers_so_the_invoice_card_parses(monkeypatch, tmp_path):
    import voice.speech as speech
    monkeypatch.setenv("SENSEI_DEMO_TOKEN", TOKEN)
    monkeypatch.setattr(speech, "keychain_secret", lambda service, **kw: KEY)
    served = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.append(app))
    assert entry.main(["--trace-dir", str(tmp_path)]) == 0
    stt = served[0].state.live_stt
    assert stt.name == "deepgram" and stt.smart_format is True  # "invoice two oh four" arrives as "invoice 204"
    assert TestClient(served[0]).get("/health").json()["mode"] == "live"


def test_the_trace_is_the_voice_loops_own(simulated, tmp_path):
    sid = new_session(simulated, "brief")
    say(simulated, sid, "Read me my morning brief.")
    events = read_events(tmp_path / "traces" / f"demo-{sid}.jsonl")
    assert [e["event_type"] for e in events] == ["permission_decision", "tool_executed", "voice_turn", "hard_gate_check"]
    assert [r["event_type"] for r in ledger(simulated, sid)] == [e["event_type"] for e in events]

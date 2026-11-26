"""Demo Slice 2: public-abuse limits, offline. A fake clock and date drive every window, timeout and rollover."""

import json
import os
import stat

import pytest
from fastapi.testclient import TestClient

import sensei_demo.__main__ as entry
from sensei_demo.limits import DailySpend, RateLimiter
from sensei_demo.server import Limits, create_app

from conftest import AUTH, KEY, TOKEN, Provider, Response, TTS_AUDIO, heard, new_session, say, speak


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class Day:
    def __init__(self, day="2026-11-27"):
        self.day = day

    def __call__(self):
        return self.day


# 1. The daily spend ceiling: persisted, rolled over at the UTC date change

def test_daily_spend_persists_across_restarts_and_rolls_over_at_the_utc_date(tmp_path):
    day = Day("2026-11-27")
    first = DailySpend(tmp_path / "spend.json", 5.00, today=day)
    first.add(1.25)
    first.add(0.50)
    assert DailySpend(tmp_path / "spend.json", 5.00, today=day).spent() == 1.75  # a restart keeps the count
    assert stat.S_IMODE(os.stat(tmp_path / "spend.json").st_mode) == 0o600
    day.day = "2026-11-28"
    assert first.spent() == 0.0 and first.allows()
    first.add(0.10)
    assert json.loads((tmp_path / "spend.json").read_text()) == {"date": "2026-11-28", "usd": 0.1}


def test_the_ceiling_refuses_at_the_limit_and_ignores_nothing_spent(tmp_path):
    spend = DailySpend(tmp_path / "s.json", 1.00, today=Day())
    assert spend.allows()
    spend.add(-3.0)
    spend.add(0.0)
    assert spend.spent() == 0.0 and not (tmp_path / "s.json").exists()
    spend.add(1.00)
    assert not spend.allows()


def test_an_unreadable_counter_starts_today_at_zero_rather_than_crashing(tmp_path):
    (tmp_path / "s.json").write_text("{not json")
    assert DailySpend(tmp_path / "s.json", 5.00, today=Day()).spent() == 0.0


def live(tmp_path, *transcripts, limits=Limits(), clock=None, today=None, max_usd=1.00):
    stt_api = Provider([heard(t) for t in transcripts])
    tts_api = Provider([Response(TTS_AUDIO) for _ in transcripts])
    from voice.speech import DeepgramSTT, ElevenLabsTTS
    app = create_app("live", trace_dir=tmp_path / "traces", state_dir=tmp_path / "state", token=TOKEN,
                     max_usd=max_usd, limits=limits, clock=clock or Clock(), today=today or Day(),
                     stt_factory=lambda: DeepgramSTT(key=lambda: KEY, opener=stt_api),
                     tts_factory=lambda player: ElevenLabsTTS(key=lambda: KEY, opener=tts_api, player=player))
    return TestClient(app), stt_api


def test_the_daily_ceiling_spans_server_runs_and_resets_at_midnight_utc(tmp_path):
    day = Day()
    tiny = Limits(daily_usd=0.000001)
    client, stt_api = live(tmp_path, "Read me my morning brief.", limits=tiny, today=day)
    sid = new_session(client, "brief")
    r = speak(client, sid)
    assert r.status_code == 200 and r.json()["spend"]["today_usd"] > 0
    r = speak(client, sid)
    assert r.status_code == 429 and "resets at 00:00 UTC" in r.json()["error"] and len(stt_api.requests) == 1

    restarted, stt_api = live(tmp_path, "Read me my morning brief.", limits=tiny, today=day)  # same state dir
    sid = new_session(restarted, "brief")
    assert speak(restarted, sid).status_code == 429 and stt_api.requests == []  # a restart doesn't reset it
    day.day = "2026-11-28"
    assert speak(restarted, sid).status_code == 200  # a new UTC day does


def test_the_daily_ceiling_sits_on_top_of_the_per_run_cap(tmp_path):
    client, _ = live(tmp_path, "Read me my morning brief.", "Read me my morning brief.", max_usd=0.000001)
    sid = new_session(client, "brief")
    speak(client, sid)
    r = speak(client, sid)
    assert r.status_code == 429 and "this server run" in r.json()["error"]
    status = client.get("/api/status", headers=AUTH).json()["spend"]
    assert status["daily_cap_usd"] == 5.0 and 0 < status["today_usd"] < 5.0


def test_a_simulated_server_spends_nothing(simulated, tmp_path):
    sid = new_session(simulated, "brief")
    say(simulated, sid, "Read me my morning brief.")
    assert simulated.app.state.daily.spent() == 0.0


# 2. Per-IP rate limits

def test_the_rate_limiter_is_a_sliding_window_per_key():
    clock = Clock()
    limiter = RateLimiter(2, 60, clock=clock)
    assert limiter.hit("a") is None and limiter.hit("a") is None
    assert limiter.hit("a") == pytest.approx(60) and limiter.hit("b") is None
    clock.now += 30
    assert limiter.hit("a") == pytest.approx(30)
    clock.now += 30.01
    assert limiter.hit("a") is None


def fly(ip):
    return {**AUTH, "Fly-Client-IP": ip}


def test_new_sessions_are_rate_limited_per_client_ip(tmp_path):
    clock = Clock()
    limits = Limits(sessions_per_ip=(2, 3600), trusted_ip_header="Fly-Client-IP")
    client = TestClient(create_app("simulated", trace_dir=tmp_path, state_dir=tmp_path, token=TOKEN,
                                   limits=limits, clock=clock))
    for _ in range(2):
        assert client.post("/api/sessions", json={"scenario": "brief"}, headers=fly("203.0.113.7")).status_code == 201
    r = client.post("/api/sessions", json={"scenario": "brief"}, headers=fly("203.0.113.7"))
    assert r.status_code == 429 and int(r.headers["retry-after"]) == 3600
    assert client.post("/api/sessions", json={"scenario": "brief"}, headers=fly("198.51.100.9")).status_code == 201
    clock.now += 3600.5
    assert client.post("/api/sessions", json={"scenario": "brief"}, headers=fly("203.0.113.7")).status_code == 201


def test_a_proxy_ip_header_is_ignored_unless_trusted(tmp_path):
    """Without the setting, a client can't dodge the limit by inventing a Fly-Client-IP per request."""
    client = TestClient(create_app("simulated", trace_dir=tmp_path, state_dir=tmp_path, token=TOKEN,
                                   limits=Limits(sessions_per_ip=(1, 3600)), clock=Clock()))
    assert client.post("/api/sessions", json={"scenario": "brief"}, headers=fly("192.0.2.1")).status_code == 201
    assert client.post("/api/sessions", json={"scenario": "brief"}, headers=fly("192.0.2.2")).status_code == 429


def test_utterances_are_rate_limited_per_client_ip_before_any_provider_call(tmp_path):
    client, stt_api = live(tmp_path, "Read me my morning brief.", "Read me my morning brief.",
                           limits=Limits(turns_per_ip=(2, 600)))
    sid = new_session(client, "brief")
    assert speak(client, sid).status_code == 200 and speak(client, sid).status_code == 200
    r = speak(client, sid)
    assert r.status_code == 429 and "utterances" in r.json()["error"] and len(stt_api.requests) == 2


# 3. Simultaneous sessions

def test_simultaneous_sessions_are_capped_and_idle_ones_stop_counting(tmp_path):
    clock = Clock()
    limits = Limits(max_sessions=2, session_idle_seconds=600, trusted_ip_header="Fly-Client-IP")
    client = TestClient(create_app("simulated", trace_dir=tmp_path, state_dir=tmp_path, token=TOKEN,
                                   limits=limits, clock=clock))
    a = client.post("/api/sessions", json={"scenario": "brief"}, headers=fly("192.0.2.1")).json()["session_id"]
    client.post("/api/sessions", json={"scenario": "note"}, headers=fly("192.0.2.2"))
    r = client.post("/api/sessions", json={"scenario": "pay"}, headers=fly("192.0.2.3"))
    assert r.status_code == 503 and "capacity" in r.json()["error"] and r.headers["retry-after"] == "60"
    clock.now += 300
    client.post(f"/api/sessions/{a}/turn", json={"text": "Read me my morning brief."}, headers=fly("192.0.2.1"))
    clock.now += 301  # the second session is now idle past 600 s; the first was used 301 s ago
    assert client.post("/api/sessions", json={"scenario": "pay"}, headers=fly("192.0.2.3")).status_code == 201
    assert a in client.app.state.sessions


def test_switching_cards_frees_the_visitors_own_session_and_nobody_elses(tmp_path):
    limits = Limits(max_sessions=2, trusted_ip_header="Fly-Client-IP")
    client = TestClient(create_app("simulated", trace_dir=tmp_path, state_dir=tmp_path, token=TOKEN,
                                   limits=limits, clock=Clock()))
    mine = client.post("/api/sessions", json={"scenario": "brief"}, headers=fly("192.0.2.1")).json()["session_id"]
    theirs = client.post("/api/sessions", json={"scenario": "brief"}, headers=fly("192.0.2.2")).json()["session_id"]
    r = client.post("/api/sessions", json={"scenario": "pay", "replaces": theirs}, headers=fly("192.0.2.1"))
    assert r.status_code == 503 and theirs in client.app.state.sessions  # can't free someone else's slot
    r = client.post("/api/sessions", json={"scenario": "pay", "replaces": mine}, headers=fly("192.0.2.1"))
    assert r.status_code == 201 and mine not in client.app.state.sessions
    assert say(client, mine, "Read me my morning brief.").status_code == 404


# 4. The token comes from a secret, so rotating it locks the old one out

def test_rotating_the_token_secret_locks_out_the_old_token(tmp_path, monkeypatch):
    monkeypatch.setenv("SENSEI_DEMO_TOKEN", "first-secret-token")
    old = TestClient(create_app("simulated", trace_dir=tmp_path, state_dir=tmp_path))
    assert old.get("/api/status", headers={"X-Sensei-Token": "first-secret-token"}).status_code == 200
    monkeypatch.setenv("SENSEI_DEMO_TOKEN", "rotated-secret-token")
    new = TestClient(create_app("simulated", trace_dir=tmp_path, state_dir=tmp_path))
    assert new.get("/api/status", headers={"X-Sensei-Token": "first-secret-token"}).status_code == 401
    assert new.get("/api/status", headers={"X-Sensei-Token": "rotated-secret-token"}).status_code == 200


# 5. The hosted entry point: env keys chosen explicitly, the Keychain never consulted

def test_the_hosted_entry_point_reads_env_keys_and_never_the_keychain(monkeypatch, tmp_path):
    import voice.speech as speech
    asked = []
    monkeypatch.setattr(speech, "keychain_secret", lambda service, **kw: asked.append(service) or None)
    monkeypatch.setenv("SENSEI_DEMO_TOKEN", TOKEN)
    monkeypatch.setenv("DEEPGRAM_API_KEY", KEY)
    monkeypatch.setenv("ELEVENLABS_API_KEY", KEY)
    served = []
    monkeypatch.setattr("uvicorn.run", lambda app, **kw: served.append(app))
    assert entry.main(["--keys", "env", "--state-dir", str(tmp_path), "--daily-usd", "5",
                       "--trusted-ip-header", "Fly-Client-IP"]) == 0
    app = served[0]
    assert asked == [] and app.state.live_stt.smart_format is True
    assert app.state.daily.ceiling_usd == 5.0 and app.state.daily.path == tmp_path / "daily-spend.json"
    assert TestClient(app).get("/api/status", headers=AUTH).json()["mode"] == "live"


def test_the_hosted_entry_point_refuses_to_start_without_env_keys(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("SENSEI_DEMO_TOKEN", TOKEN)
    monkeypatch.delenv("DEEPGRAM_API_KEY", raising=False)
    monkeypatch.setenv("ELEVENLABS_API_KEY", KEY)
    served = []
    monkeypatch.setattr("uvicorn.run", lambda *a, **kw: served.append(a))
    assert entry.main(["--keys", "env", "--state-dir", str(tmp_path)]) == 2
    err = capsys.readouterr().err
    assert "DEEPGRAM_API_KEY" in err and KEY not in err and served == []

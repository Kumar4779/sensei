"""Slice 6: the real speech adapters, offline. Provider responses are mocked; no key, no network, no audio device."""

import base64
import io
import json
import os
import socket
import stat
import urllib.error
import urllib.parse

import pytest

from sensei_core.trace import TraceWriter, read_events

import voice.speech as speech
from voice import cli
from voice.adapters import FakeSTT, FakeTTS, ScriptedUtterance
from voice.loop import VoiceSession
from voice.speech import (DeepgramSTT, ElevenLabsTTS, SpeechKeyMissing, SpeechUnavailable, make_stt, make_tts,
                          play_audio, record_utterance)

KEY = "dg-or-el-TEST-KEY-not-a-real-secret-0000"
AUDIO_MARKER = b"RAW-CAPTURE-\x00\x01\xfe-SENSEI-REAL-PATH-4419"
TTS_MARKER = b"ID3-MP3-\x00\xff-SENSEI-TTS-BYTES-9921"


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real connection attempt fails the test: every provider call here must go through a mocked opener."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class Response:
    def __init__(self, body: bytes, chunk: int | None = None):
        self._stream = io.BytesIO(body)
        self._chunk = chunk

    def read(self, n: int = -1) -> bytes:
        return self._stream.read(n if self._chunk is None else min(n, self._chunk))

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Opener:
    """Records each request and answers with a canned provider response."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        reply = self.responses.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def deepgram_reply(transcript: str, confidence: float, duration: float = 1.8) -> Response:
    return Response(json.dumps({"metadata": {"request_id": "req-1", "duration": duration},
                                "results": {"channels": [{"alternatives": [
                                    {"transcript": transcript, "confidence": confidence,
                                     "words": [{"word": w, "confidence": confidence} for w in transcript.split()]}]}]}
                                }).encode())


def deepgram(*responses) -> tuple[DeepgramSTT, Opener]:
    opener = Opener(*responses)
    return DeepgramSTT(key=lambda: KEY, opener=opener), opener


def elevenlabs(*bodies, chunk: int = 7) -> tuple[ElevenLabsTTS, Opener, list[bytes]]:
    played: list[bytes] = []
    opener = Opener(*[b if isinstance(b, Exception) else Response(b, chunk) for b in bodies])
    return ElevenLabsTTS(key=lambda: KEY, opener=opener, player=played.append), opener, played


def heard(audio: bytes = AUDIO_MARKER) -> ScriptedUtterance:
    return ScriptedUtterance("", audio=audio)  # the real path: the recogniser gets audio, not a script


# 1. Selection: fakes by default, real only when named, and a named provider without its key fails closed

def test_the_fakes_are_the_default_everywhere(trace_path, monkeypatch):
    assert isinstance(make_stt(), FakeSTT) and isinstance(make_tts(), FakeTTS)
    session = VoiceSession(TraceWriter(trace_path, run_id="d", session_id="d"))
    assert isinstance(session.stt, FakeSTT) and isinstance(session.tts, FakeTTS)
    built = []
    monkeypatch.setattr(cli, "VoiceSession", lambda trace, **kw: built.append(kw) or session)
    monkeypatch.setattr("builtins.input", lambda prompt="": "")
    cli.main(["--trace", str(trace_path)])
    assert isinstance(built[0]["stt"], FakeSTT) and isinstance(built[0]["tts"], FakeTTS)


def test_a_named_provider_is_used_only_when_its_key_is_present():
    assert isinstance(make_stt("deepgram", lookup=lambda s: KEY), DeepgramSTT)
    assert isinstance(make_tts("elevenlabs", lookup=lambda s: KEY), ElevenLabsTTS)


@pytest.mark.parametrize("make, provider, service", [
    (make_stt, "deepgram", "sensei-deepgram-api-key"),
    (make_tts, "elevenlabs", "sensei-elevenlabs-api-key")])
def test_a_named_provider_without_its_key_fails_closed_never_falls_back(make, provider, service):
    asked = []
    with pytest.raises(SpeechKeyMissing) as e:
        make(provider, lookup=lambda s: asked.append(s) or None)
    assert asked == [service] and e.value.error_class == "key_missing"


def test_an_unknown_provider_is_refused():
    with pytest.raises(ValueError):
        make_stt("whisper")
    with pytest.raises(ValueError):
        make_tts("polly")


def test_the_cli_exits_before_the_session_when_a_selected_key_is_missing(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(speech, "keychain_secret", lambda service, **kw: None)
    trace = tmp_path / "t.jsonl"
    assert cli.main(["--trace", str(trace), "--tts", "elevenlabs"]) == 2
    assert "sensei-elevenlabs-api-key" in capsys.readouterr().err and not trace.exists()


# 2. Deepgram: the request, the transcript and Deepgram's own confidence

def test_deepgram_sends_the_audio_and_returns_the_transcript_with_its_confidence():
    stt, opener = deepgram(deepgram_reply("Send the note to Alex.", 0.987))
    t = stt.transcribe(heard())
    assert (t.text, t.confidence, t.provider, t.model) == ("Send the note to Alex.", 0.987, "deepgram", "nova-3")
    assert t.latency_ms is not None and t.latency_ms >= 0
    request = opener.requests[0]
    query = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
    assert request.full_url.startswith("https://api.deepgram.com/v1/listen?")
    assert query["model"] == ["nova-3"] and query["smart_format"] == ["false"] and query["punctuate"] == ["true"]
    assert request.data == AUDIO_MARKER and request.get_header("Authorization") == f"Token {KEY}"
    assert stt.audio_bytes_seen == len(AUDIO_MARKER) and stt.audio_seconds == 1.8


def test_an_empty_deepgram_transcript_is_zero_confidence_so_the_loop_asks_again(trace_path):
    stt, _ = deepgram(deepgram_reply("", 0.0))
    session = VoiceSession(TraceWriter(trace_path, run_id="r", session_id="s"), stt=stt)
    assert "didn't catch that" in session.turn(heard())


def test_a_low_deepgram_confidence_clarifies_through_the_unchanged_loop(trace_path):
    stt, _ = deepgram(deepgram_reply("Pay invoice 240.", 0.41))
    session = VoiceSession(TraceWriter(trace_path, run_id="r", session_id="s"), stt=stt)
    session.turn(heard())
    turn = next(e["payload"] for e in read_events(trace_path) if e["event_type"] == "voice_turn")
    assert turn["outcome"] == "clarify" and turn["stt_confidence"] == 0.41 and session.world.payments == []


def test_deepgram_needs_audio_and_a_key():
    stt, opener = deepgram()
    with pytest.raises(SpeechUnavailable, match="no audio"):
        stt.transcribe(ScriptedUtterance("typed text"))
    with pytest.raises(SpeechKeyMissing):
        DeepgramSTT(key=lambda: None, opener=opener).transcribe(heard())
    assert opener.requests == []


@pytest.mark.parametrize("failure, error_class", [
    (urllib.error.HTTPError("u", 401, "Unauthorized", {}, io.BytesIO(f"bad key {KEY}".encode())), "http_401"),
    (urllib.error.URLError(ConnectionRefusedError()), "unreachable"),
    (TimeoutError(), "timeout")])
def test_provider_errors_name_the_status_never_the_key_or_the_body(failure, error_class):
    stt, _ = deepgram(failure)
    tts, _, _ = elevenlabs(failure)
    for call in (lambda: stt.transcribe(heard()), lambda: tts.speak("hello")):
        with pytest.raises(SpeechUnavailable) as e:
            call()
        assert e.value.error_class == error_class and KEY not in str(e.value) and "bad key" not in str(e.value)
        assert e.value.__cause__ is None and e.value.__suppress_context__


def test_a_malformed_deepgram_reply_is_a_bad_response():
    stt, _ = deepgram(Response(b'{"results": {}}'))
    with pytest.raises(SpeechUnavailable) as e:
        stt.transcribe(heard())
    assert e.value.error_class == "bad_response"


# 3. ElevenLabs: streamed synthesis, first-byte and total timings, playback, nothing kept

def test_elevenlabs_streams_the_reply_times_it_and_plays_it():
    tts, opener, played = elevenlabs(TTS_MARKER * 3)
    reply = tts.speak("Good morning.")
    assert played == [TTS_MARKER * 3]  # the player got every byte
    assert (reply.provider, reply.model, reply.audio_bytes) == ("elevenlabs", "eleven_flash_v2_5", len(TTS_MARKER) * 3)
    assert 0 <= reply.first_byte_ms <= reply.total_ms and reply.playback_started is not None
    assert not any(isinstance(v, bytes) for v in vars(reply).values())  # the reply carries no audio
    request = opener.requests[0]
    assert request.full_url.startswith("https://api.elevenlabs.io/v1/text-to-speech/") and "/stream?" in request.full_url
    assert request.get_header("Xi-api-key") == KEY
    assert json.loads(request.data) == {"text": "Good morning.", "model_id": "eleven_flash_v2_5"}
    assert tts.spoken == ["Good morning."] and tts.characters == len("Good morning.")


def test_elevenlabs_needs_a_key_and_audio():
    tts, opener, played = elevenlabs(b"")
    with pytest.raises(SpeechUnavailable) as e:
        tts.speak("hello")
    assert e.value.error_class == "bad_response" and played == []
    with pytest.raises(SpeechKeyMissing):
        ElevenLabsTTS(key=lambda: None, opener=opener, player=played.append).speak("hello")


def test_playback_uses_an_owner_only_file_that_is_gone_when_playback_ends(monkeypatch):
    seen = {}

    def fake_run(argv, **kwargs):
        path = argv[1]
        seen.update(path=path, mode=stat.S_IMODE(os.stat(path).st_mode),
                    folder_mode=stat.S_IMODE(os.stat(os.path.dirname(path)).st_mode),
                    content=open(path, "rb").read(), player=argv[0])
    monkeypatch.setattr(speech.subprocess, "run", fake_run)
    play_audio(TTS_MARKER, player="/usr/bin/afplay")
    assert seen["player"] == "/usr/bin/afplay" and seen["content"] == TTS_MARKER
    assert seen["mode"] == 0o600 and seen["folder_mode"] == 0o700
    assert not os.path.exists(seen["path"]) and not os.path.exists(os.path.dirname(seen["path"]))


def test_playback_cleans_up_even_when_the_player_fails(monkeypatch):
    paths = []

    def broken(argv, **kwargs):
        paths.append(argv[1])
        raise OSError("player crashed")
    monkeypatch.setattr(speech.subprocess, "run", broken)
    with pytest.raises(OSError):
        play_audio(TTS_MARKER, player="/usr/bin/afplay")
    assert not os.path.exists(paths[0]) and not os.path.exists(os.path.dirname(paths[0]))


def test_no_player_or_recorder_is_a_named_failure(monkeypatch):
    monkeypatch.setattr(speech.shutil, "which", lambda name: None)
    with pytest.raises(SpeechUnavailable) as e:
        play_audio(TTS_MARKER)
    assert e.value.error_class == "no_player"
    with pytest.raises(SpeechUnavailable) as e:
        record_utterance()
    assert e.value.error_class == "no_recorder"


def test_a_capture_is_returned_in_memory_from_the_recorders_stdout():
    calls = []

    class Done:
        returncode, stdout = 0, AUDIO_MARKER

    def run(argv, **kwargs):
        calls.append(argv)
        return Done()
    assert record_utterance(recorder="/opt/homebrew/bin/rec", run=run, max_seconds=8) == AUDIO_MARKER
    argv = calls[0]
    assert argv[0] == "/opt/homebrew/bin/rec" and argv[argv.index("-t") + 1:argv.index("-t") + 3] == ["wav", "-"]
    assert argv[-2:] == ["0", "8"]  # trim 0 8: the capture is capped


# 4. The real path through the unchanged loop: timings traced apart, audio never traced

def real_session(trace_path, *stt_replies, tts_bodies=None):
    stt, _ = deepgram(*stt_replies)
    tts, _, played = elevenlabs(*(tts_bodies or [TTS_MARKER] * len(stt_replies)))
    return VoiceSession(TraceWriter(trace_path, run_id="real", session_id="real"), stt=stt, tts=tts), played


def test_a_spoken_echo_confirmed_by_voice_runs_once_with_real_path_timings_traced(trace_path):
    session, played = real_session(trace_path, deepgram_reply("Send the note to Alex.", 0.99),
                                   deepgram_reply("Yes.", 0.97))
    session.turn(heard())
    session.turn(heard())
    turns = [e["payload"] for e in read_events(trace_path) if e["event_type"] == "voice_turn"]
    assert [t["outcome"] for t in turns] == ["echoed", "executed"] and len(session.world.outbox) == 1
    assert len(played) == 2  # the echo and the result were both spoken
    for t in turns:
        real = t["real_path"]
        assert real["provider"] == {"stt": "deepgram", "tts": "elevenlabs"}
        assert real["model"] == {"stt": "nova-3", "tts": "eleven_flash_v2_5"}
        assert all(real[k] >= 0 for k in ("stt_latency_ms", "tts_first_byte_ms", "tts_total_ms",
                                          "time_to_first_audio_ms"))
        assert set(t["latency_ms"]) == {"stt", "understand_and_act", "time_to_first_audio", "turn"}  # unchanged


def test_transcript_path_turns_carry_no_real_path_fields(session, trace_path):
    session.turn(ScriptedUtterance("what's on my calendar today"))
    turn = next(e["payload"] for e in read_events(trace_path) if e["event_type"] == "voice_turn")
    assert "real_path" not in turn


def test_one_real_side_reports_the_fake_side_by_name_with_no_timings(trace_path):
    tts, _, _ = elevenlabs(TTS_MARKER)
    session = VoiceSession(TraceWriter(trace_path, run_id="r", session_id="s"), tts=tts)
    session.turn(ScriptedUtterance("what's on my calendar today"))
    real = next(e["payload"] for e in read_events(trace_path) if e["event_type"] == "voice_turn")["real_path"]
    assert real["provider"] == {"stt": "fake-stt", "tts": "elevenlabs"} and real["stt_latency_ms"] is None


def test_real_path_audio_bytes_never_appear_in_any_trace_record(trace_path):
    session, played = real_session(trace_path, deepgram_reply("Send the note to Alex.", 0.99),
                                   deepgram_reply("Yes.", 0.97), deepgram_reply("Read me my morning brief.", 0.98))
    for _ in range(3):
        session.turn(heard(AUDIO_MARKER))
    assert session.stt.audio_bytes_seen == len(AUDIO_MARKER) and played == [TTS_MARKER] * 3
    raw = trace_path.read_bytes()
    for marker in (AUDIO_MARKER, TTS_MARKER):
        for form in (marker, marker.hex().encode(), base64.b64encode(marker), repr(marker).encode(),
                     marker.decode("latin-1").encode("utf-8")):
            assert form not in raw, form
    assert b"SENSEI-REAL-PATH" not in raw and b"SENSEI-TTS-BYTES" not in raw and KEY.encode() not in raw


def test_a_tts_failure_after_the_action_ran_still_traces_the_turn(trace_path):
    session, _ = real_session(trace_path, deepgram_reply("Send the note to Alex.", 0.99),
                              deepgram_reply("Yes.", 0.97),
                              tts_bodies=[TTS_MARKER, urllib.error.URLError(ConnectionResetError())])
    session.turn(heard())
    with pytest.raises(SpeechUnavailable):
        session.turn(heard())
    turns = [e["payload"] for e in read_events(trace_path) if e["event_type"] == "voice_turn"]
    assert [t["outcome"] for t in turns] == ["echoed", "executed"] and len(session.world.outbox) == 1
    assert turns[1]["tts_error"] == "unreachable" and "real_path" not in turns[1]


# 5. The local-only real-audio script: its spend guard and grading wrapper work offline

def test_the_real_audio_run_refuses_to_start_over_its_spend_cap():
    import real_audio
    with pytest.raises(SystemExit, match="over the"):
        real_audio._guard(real_audio.estimate_usd(50_000, 50_000), cap=1.00)
    real_audio._guard(real_audio.estimate_usd(300, 300), cap=1.00)  # a smoke-sized run is far under


def test_divergence_ignores_case_and_punctuation_only():
    import real_audio
    assert real_audio.normalise("Send the note to Alex.") == real_audio.normalise("send the note to alex")
    assert real_audio.normalise("Pay invoice 240.") != real_audio.normalise("Pay invoice two zero four.")


def test_a_real_audio_trial_records_a_failure_the_frozen_taxonomy_cannot_label(tmp_path):
    """On real audio a scripted mishearing may not happen: the probe's expected echo is then wrong for what was heard."""
    import harness
    import real_audio
    task = next(t for t in harness.load(real_audio.V2_TASKS)["tasks"] if t["id"] == "v2-amount-misheard-001")
    heard_right = {**task, "turns": [{**t, "heard": t["said"]} for t in task["turns"]]}
    with pytest.raises(harness.UnlabelledFailure):
        harness.run_trial(heard_right, tmp_path / "a.jsonl")
    trial = real_audio.run_trial(heard_right, tmp_path / "b.jsonl")
    assert trial.label.startswith("unlabelled") and trial.verdict.passed and trial.world["payments"] == []
    assert harness.label is not None and harness.label.__name__ == "label"  # the harness is left as it was

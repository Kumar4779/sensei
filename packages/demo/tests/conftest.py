"""Shared fixtures: offline always (no key, no network), a demo token, and mocked speech providers."""

import io
import json
import socket

import pytest
from fastapi.testclient import TestClient

from voice.speech import DeepgramSTT, ElevenLabsTTS

from sensei_demo.server import create_app

TOKEN = "demo-test-token-not-a-secret"
KEY = "provider-TEST-KEY-not-a-real-secret-0000"
TTS_AUDIO = b"ID3\x04\x00-SENSEI-DEMO-REPLY-AUDIO-5521"
MIC_AUDIO = b"\x1aE\xdf\xa3-SENSEI-DEMO-MIC-WEBM-8813"
AUTH = {"X-Sensei-Token": TOKEN}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    """Any real connection attempt fails the test (the Slice 6 pattern). TestClient needs no socket."""
    def refuse(*args, **kwargs):
        raise AssertionError("a test tried to open a network connection")
    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)


class Response:
    def __init__(self, body: bytes):
        self._stream = io.BytesIO(body)
        self.headers = {}

    def read(self, n: int = -1) -> bytes:
        return self._stream.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class Provider:
    """A mocked provider endpoint: answers each request from a script, records what it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def heard(text: str, confidence: float = 0.98) -> Response:
    return Response(json.dumps({"metadata": {"duration": 1.5}, "results": {"channels": [{"alternatives": [
        {"transcript": text, "confidence": confidence}]}]}}).encode())


@pytest.fixture
def simulated(tmp_path):
    return TestClient(create_app("simulated", trace_dir=tmp_path / "traces", token=TOKEN))


@pytest.fixture
def live_factory(tmp_path):
    """A LIVE SPEECH app whose Deepgram and ElevenLabs are mocked: what the browser would get, offline."""
    def make(*transcripts, tts=None, max_usd=1.00):
        stt_api = Provider([heard(t) if isinstance(t, str) else t for t in transcripts])
        tts_api = Provider(tts if tts is not None else [Response(TTS_AUDIO) for _ in transcripts])
        app = create_app("live", trace_dir=tmp_path / "traces", token=TOKEN, max_usd=max_usd,
                         stt_factory=lambda: DeepgramSTT(key=lambda: KEY, opener=stt_api),
                         tts_factory=lambda player: ElevenLabsTTS(key=lambda: KEY, opener=tts_api, player=player))
        return TestClient(app), stt_api, tts_api
    return make


def new_session(client, scenario: str) -> str:
    r = client.post("/api/sessions", json={"scenario": scenario}, headers=AUTH)
    assert r.status_code == 201, r.text
    return r.json()["session_id"]


def say(client, sid: str, text: str):
    return client.post(f"/api/sessions/{sid}/turn", json={"text": text}, headers=AUTH)


def speak(client, sid: str, audio: bytes = MIC_AUDIO):
    return client.post(f"/api/sessions/{sid}/turn", content=audio,
                       headers={**AUTH, "Content-Type": "audio/webm;codecs=opus"})


def ledger(client, sid: str, after: int = 0) -> list[dict]:
    """The session's ledger so far (follow=0: the backlog, then the stream closes)."""
    r = client.get(f"/api/sessions/{sid}/ledger", params={"after": after, "follow": "false"}, headers=AUTH)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    return parse_sse(r.text)


def parse_sse(text: str) -> list[dict]:
    events = []
    for block in text.split("\n\n"):
        data = [line[6:] for line in block.split("\n") if line.startswith("data: ")]
        ids = [line[4:] for line in block.split("\n") if line.startswith("id: ")]
        if data:
            record = json.loads("\n".join(data))
            assert ids == [str(record["index"])]
            events.append(record)
    return events

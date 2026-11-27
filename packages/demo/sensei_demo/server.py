"""The "Talk to Sensei" demo server (Demo Slice 1): browser voice plus a live permission ledger.

Security posture, after core's gateway (sensei_core/gateway.py):
  - binds 127.0.0.1 unless SENSEI_DEMO_ALLOW_LAN=1 (an explicit, warned opt-in);
  - SENSEI_DEMO_TOKEN must be set, or the server refuses to start. Every
    data endpoint needs X-Sensei-Token, compared in constant time. /health
    and the /demo token-entry shell are public and hold no data;
  - provider keys stay on the server. Deepgram and ElevenLabs are called
    here, with the Keychain keys (voice/speech.py, Slice 6). No response,
    page or stream ever carries a key.

Each utterance runs the unchanged voice loop:
  browser audio → Deepgram → VoiceSession (gate, echo machine) → ElevenLabs → audio back to the page.

Audio passes through and is never stored. The browser's capture goes to
Deepgram and is dropped. The reply's audio goes back in that turn's response
and is dropped; it is never traced or written to disk.

Mode is explicit and shown on the page: LIVE SPEECH (real adapters) or
SIMULATED (typed input and fake adapters, for development without keys). A
live server never takes typed turns, and a simulated one never takes audio,
so the page cannot simulate silently.

The ledger streams as Server-Sent Events over fetch (GET .../ledger). Fetch
is used instead of EventSource so the token travels in a header, never in a
URL.

Public-abuse limits (Demo Slice 2, sensei_demo/limits.py), all configurable:
  - a global daily spend ceiling across every session and server run, persisted
    with UTC date rollover, on top of the per-run cap;
  - per-IP rate limits on new sessions and on utterances;
  - a cap on simultaneous sessions. A session idle past the timeout stops
    counting, and a visitor switching scenario cards frees the session it
    replaces.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import os
import threading
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse, StreamingResponse

from voice.adapters import FakeSTT, FakeTTS, ScriptedUtterance, SpeechToText, TextToSpeech
from voice.cli import parse_line
from voice.speech import SpeechUnavailable
from voice.verify import verify_run

from sensei_core.egress import EgressLog
from sensei_core.router import ModelClient

from sensei_demo.answers import AnsweringVoiceSession, ModelAnswers
from sensei_demo.ledger import Ledger, LedgerTrace
from sensei_demo.limits import DailySpend, RateLimiter, client_ip, utc_today
from sensei_demo.scenarios import DEMO_DATA_LABEL, SCENARIOS, world_for

TOKEN_ENV = "SENSEI_DEMO_TOKEN"
ALLOW_LAN_ENV = "SENSEI_DEMO_ALLOW_LAN"
DEFAULT_PORT = 8766  # the gateway is 8765
DEFAULT_STATE_DIR = Path.home() / ".sensei" / "demo"
DEFAULT_TRACE_DIR = DEFAULT_STATE_DIR / "traces"
SPEND_FILE = "daily-spend.json"
STATIC_DIR = Path(__file__).with_name("static")
# Fixed list: no path is taken from the URL. Token-protected like the page's app code.
APP_FILES = {"app.js": "text/javascript", "app.css": "text/css", "sensei-avatar.webp": "image/webp",
             # The Live Avatar's expressions (#26): the same portrait, driven by the demo's state.
             "sensei-avatar-listening.webp": "image/webp", "sensei-avatar-thinking.webp": "image/webp",
             "sensei-avatar-speaking.webp": "image/webp", "sensei-avatar-happy.webp": "image/webp"}
# Self-hosted fonts (SIL OFL; licenses alongside). Public, because @font-face can't send the token and fonts hold no data.
FONT_FILES = {"mplus1-latin.woff2": "font/woff2", "spacegrotesk-latin.woff2": "font/woff2",
              "OFL-MPLUS1.txt": "text/plain; charset=utf-8", "OFL-SpaceGrotesk.txt": "text/plain; charset=utf-8"}
MAX_AUDIO_BYTES = 2_000_000  # about a minute of browser Opus; a push-to-talk turn is a few seconds
MAX_TEXT_CHARS = 500
KEEPALIVE_SECONDS = 15.0
# Spend estimate for the cap (list prices checked 2026-11-26; TTS deliberately conservative).
TTS_USD_PER_1K_CHARS = 0.06
STT_USD_PER_MIN = 0.0043
SHELL_CSP = ("default-src 'self'; script-src 'self' 'unsafe-inline' blob:; style-src 'self' 'unsafe-inline'; "
             "media-src blob:; connect-src 'self'; img-src 'self' data: blob:; font-src 'self'; frame-ancestors 'none'; "
             "base-uri 'none'; form-action 'none'")
log = logging.getLogger("sensei.demo")

Mode = Literal["live", "simulated"]


@dataclass(frozen=True)
class Limits:
    """Public-abuse limits. The defaults suit one small instance shown to a handful of visitors at a time."""

    daily_usd: float = 5.00  # estimated provider spend per UTC day, across every session and run
    # New sessions per client IP per hour. Sessions cost nothing (spend is in utterances, limited below and by the
    # daily ceiling), and every page load and card click starts one, so this only stops scripted session floods.
    sessions_per_ip: tuple[int, float] = (60, 3600.0)
    turns_per_ip: tuple[int, float] = (40, 600.0)  # utterances per client IP per 10 minutes
    max_sessions: int = 8  # simultaneous sessions
    session_idle_seconds: float = 900.0  # a session idle this long stops counting toward max_sessions
    trusted_ip_header: str | None = None  # e.g. "Fly-Client-IP", behind a proxy that sets it; never by default


class DemoConfigError(RuntimeError):
    pass


class Capture:
    """The TTS 'player' on the server: it holds one reply's audio until the response takes it."""

    def __init__(self) -> None:
        self._audio: bytes | None = None

    def __call__(self, audio: bytes) -> None:
        self._audio = audio

    def take(self) -> bytes | None:
        audio, self._audio = self._audio, None
        return audio


@dataclass
class DemoSession:
    id: str
    scenario: str
    ledger: Ledger
    trace: LedgerTrace
    voice: AnsweringVoiceSession
    ip: str = ""
    last_used: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)


def _error(status: int, message: str, retry_after: float | None = None, **extra: Any) -> JSONResponse:
    headers = {"Cache-Control": "no-store"}
    if retry_after is not None:
        headers["Retry-After"] = str(max(1, int(retry_after + 0.999)))
    return JSONResponse({"error": message, **extra}, status_code=status, headers=headers)


def create_app(mode: Mode, *, stt_factory: Callable[[], SpeechToText] | None = None,
               tts_factory: Callable[[Callable[[bytes], None]], TextToSpeech] | None = None,
               trace_dir: str | Path | None = None, max_usd: float = 1.00,
               token: str | None = None, limits: Limits = Limits(), state_dir: str | Path | None = None,
               clock: Callable[[], float] = time.monotonic, today: Callable[[], str] = utc_today,
               model_client: ModelClient | None = None) -> FastAPI:
    token = token if token is not None else os.environ.get(TOKEN_ENV, "")
    if not token:  # fail closed: never serve without auth
        raise DemoConfigError(f"{TOKEN_ENV} is not set; refusing to start the demo without auth")
    if mode not in ("live", "simulated"):
        raise DemoConfigError(f"unknown mode {mode!r}")
    if mode == "live" and (stt_factory is None or tts_factory is None):
        raise DemoConfigError("live mode needs real speech adapters")

    capture = Capture()
    live_stt = stt_factory() if mode == "live" else None  # type: ignore[misc]
    live_tts = tts_factory(capture) if mode == "live" else None  # type: ignore[misc]
    live_turn = threading.Lock()  # one live turn at a time: the capture holds one reply
    sessions: OrderedDict[str, DemoSession] = OrderedDict()
    trace_dir = Path(trace_dir) if trace_dir is not None else DEFAULT_TRACE_DIR
    state_dir = Path(state_dir) if state_dir is not None else DEFAULT_STATE_DIR
    daily = DailySpend(state_dir / SPEND_FILE, limits.daily_usd, today=today)
    session_rate = RateLimiter(*limits.sessions_per_ip, clock=clock)
    turn_rate = RateLimiter(*limits.turns_per_ip, clock=clock)

    model_spent = [0.0]  # core's estimated model cost this server run (#25)

    def spent_raw() -> float:
        chars = getattr(live_tts, "characters", 0)
        seconds = getattr(live_stt, "audio_seconds", 0.0)
        return chars / 1000 * TTS_USD_PER_1K_CHARS + seconds / 60 * STT_USD_PER_MIN + model_spent[0]

    def budget_left() -> float:
        """What the model may still spend: the smaller of the per-run cap and today's ceiling."""
        return min(max_usd - spent_raw(), limits.daily_usd - daily.spent())

    # Free-form answers through core's model backend (#25): only when a model client is configured.
    answers = None if model_client is None else ModelAnswers(
        model_client, egress=EgressLog(state_dir / "egress.jsonl"), budget_left=budget_left,
        on_spend=lambda usd: model_spent.__setitem__(0, model_spent[0] + usd))

    def spent_usd() -> float:
        return round(spent_raw(), 4)

    def spend_body() -> dict[str, Any]:
        return {"estimated_usd": spent_usd(), "cap_usd": max_usd,
                "today_usd": daily.spent(), "daily_cap_usd": limits.daily_usd}

    def prune() -> None:
        """Forget sessions idle past the timeout: they no longer hold a slot. Their traces stay on disk."""
        cutoff = clock() - limits.session_idle_seconds
        for sid in [sid for sid, s in sessions.items() if s.last_used < cutoff and not s.lock.locked()]:
            del sessions[sid]

    def adapter(a: Any) -> dict[str, Any]:
        return {"provider": getattr(a, "name", None), "model": getattr(a, "model", None)}

    app = FastAPI(title="Sensei demo", docs_url=None, redoc_url=None, openapi_url=None)

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault("X-Frame-Options", "DENY")
        return response

    def authed(request: Request) -> bool:
        given = request.headers.get("x-sensei-token")
        return given is not None and hmac.compare_digest(given.encode(), token.encode())

    def session_or_none(sid: str) -> DemoSession | None:
        return sessions.get(sid)

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok", "mode": mode}

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/demo", status_code=307)

    @app.get("/demo", include_in_schema=False)
    def shell():
        # Public on purpose: a token-entry page with no data, like the gateway's /ui.
        return FileResponse(STATIC_DIR / "index.html", media_type="text/html", headers={
            "Cache-Control": "no-store", "Content-Security-Policy": SHELL_CSP,
            "Permissions-Policy": "microphone=(self), camera=(), geolocation=()"})

    @app.get("/demo/fonts/{name}", include_in_schema=False)
    def font_file(name: str):
        if name not in FONT_FILES:
            return _error(404, "not found")
        return FileResponse(STATIC_DIR / "fonts" / name, media_type=FONT_FILES[name],
                            headers={"Cache-Control": "public, max-age=604800, immutable"})

    @app.get("/demo/app/{name}", include_in_schema=False)
    def app_file(name: str, request: Request):
        if not authed(request):
            return _error(401, "unauthorized: missing or invalid X-Sensei-Token")
        if name not in APP_FILES:
            return _error(404, "not found")
        return FileResponse(STATIC_DIR / name, media_type=APP_FILES[name], headers={"Cache-Control": "no-store"})

    @app.get("/api/status")
    def status(request: Request):
        if not authed(request):
            return _error(401, "unauthorized: missing or invalid X-Sensei-Token")
        body: dict[str, Any] = {
            "mode": mode, "mode_label": "LIVE SPEECH" if mode == "live" else "SIMULATED",
            "mode_detail": ("Real speech: your audio goes to Deepgram and replies come from ElevenLabs, server-side."
                            if mode == "live" else
                            "Simulated: typed input stands in for speech, and fake adapters reply as text. "
                            "No audio, no provider calls."),
            "stt": adapter(live_stt) if mode == "live" else {"provider": "fake-stt", "model": None},
            "tts": adapter(live_tts) if mode == "live" else {"provider": "fake-tts", "model": None},
            "demo_data": DEMO_DATA_LABEL,
            "model": None if answers is None else {"route": "cheap_api", "model": answers.model,
                                                    "detail": "Answers questions no command matches. Read-only: no tools."},
            "scenarios": [s.to_dict() for s in SCENARIOS.values()]}
        if mode == "live":
            body["spend"] = spend_body()
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    @app.post("/api/sessions")
    async def new_session(request: Request):
        if not authed(request):
            return _error(401, "unauthorized: missing or invalid X-Sensei-Token")
        try:
            body = await request.json()
            scenario, replaces = body.get("scenario"), body.get("replaces")
        except (ValueError, AttributeError):
            return _error(400, "body must be JSON: {\"scenario\": ...}")
        if scenario not in SCENARIOS:
            return _error(404, f"unknown scenario; choose from {', '.join(SCENARIOS)}")
        ip = client_ip(request, limits.trusted_ip_header)
        wait = session_rate.hit(ip)
        if wait is not None:
            return _error(429, "too many new sessions from your address; try again shortly", retry_after=wait)
        old = sessions.get(replaces) if isinstance(replaces, str) else None
        if old is not None and old.ip == ip and not old.lock.locked():
            del sessions[replaces]  # the same visitor switching cards: free the slot it held
        prune()
        if len(sessions) >= limits.max_sessions:
            return _error(503, f"the demo is at capacity ({limits.max_sessions} visitors); try again in a few minutes",
                          retry_after=60)
        sid = uuid.uuid4().hex[:16]
        ledger = Ledger()
        trace = LedgerTrace(trace_dir / f"demo-{sid}.jsonl", run_id=f"demo-{sid}", session_id=f"demo-{scenario}",
                            ledger=ledger)
        voice = AnsweringVoiceSession(trace, world=world_for(scenario),
                                      stt=live_stt if mode == "live" else FakeSTT(),
                                      tts=live_tts if mode == "live" else FakeTTS(), answers=answers)
        sessions[sid] = DemoSession(sid, scenario, ledger, trace, voice, ip=ip, last_used=clock())
        return JSONResponse({"session_id": sid, "scenario": SCENARIOS[scenario].to_dict()},
                            status_code=201, headers={"Cache-Control": "no-store"})

    def run_turn(s: DemoSession, utterance: ScriptedUtterance) -> tuple[int, dict[str, Any]]:
        with s.lock, (live_turn if mode == "live" else threading.Lock()):
            capture.take()  # nothing from an earlier, failed turn can leak into this one
            error = None
            turns_before = sum(1 for r in s.ledger.after(0) if r["event_type"] == "voice_turn")
            try:
                s.voice.turn(utterance)
            except SpeechUnavailable as e:
                error = {"error": str(e), "error_class": e.error_class}
                ran = sum(1 for r in s.ledger.after(0) if r["event_type"] == "voice_turn") > turns_before
                # Traced, so the ledger shows it: the stage and class only, never a body or a key.
                s.trace.write("speech_error", {"stage": "tts" if ran else "stt", "error_class": e.error_class,
                                               "provider": (s.voice.tts if ran else s.voice.stt).name})
            finally:
                events = s.ledger.after(0)  # exactly the trace's records, in order, even before the file exists
                verdict = verify_run(events)
                s.trace.write("hard_gate_check", {
                    "passed": verdict.passed, "violations": [v.detail for v in verdict.violations],
                    "irreversible_executed": sum(1 for e in events if e["event_type"] == "tool_executed"
                                                 and e["payload"].get("tier") == "irreversible")})
            audio = capture.take()
        turns = [r["payload"] for r in s.ledger.after(0) if r["event_type"] == "voice_turn"]
        if error is not None and len(turns) == turns_before:
            return 502, {**error, "hard_gate": "PASS" if verdict.passed else "FAIL"}  # STT failed: no turn ran
        t = turns[-1]
        body = {"turn": t["turn"], "heard": t["transcript"], "confidence": t["stt_confidence"], "outcome": t["outcome"],
                "reply": t["reply"], "latency_ms": t["latency_ms"], "real_path": t.get("real_path"),
                "model": t.get("model_answer"),
                "audio": base64.b64encode(audio).decode("ascii") if audio else None,
                "audio_type": "audio/mpeg" if audio else None,
                "hard_gate": "PASS" if verdict.passed else "FAIL"}
        if error is not None:
            body.update(error)  # the turn ran and is traced, but its reply could not be spoken
        if mode == "live":
            body["spend"] = spend_body()
        return 200, body

    @app.post("/api/sessions/{sid}/turn")
    async def turn(sid: str, request: Request):
        if not authed(request):
            return _error(401, "unauthorized: missing or invalid X-Sensei-Token")
        s = session_or_none(sid)
        if s is None:
            return _error(404, "no such session (it may have expired); pick a scenario to start again")
        s.last_used = clock()
        wait = turn_rate.hit(client_ip(request, limits.trusted_ip_header))
        if wait is not None:
            return _error(429, "too many utterances from your address; try again shortly", retry_after=wait)
        ctype = request.headers.get("content-type", "").split(";")[0].strip().lower()
        declared = request.headers.get("content-length")
        if declared is not None and declared.isdigit() and int(declared) > MAX_AUDIO_BYTES:
            return _error(413, f"utterance too large (limit {MAX_AUDIO_BYTES} bytes)")
        body = bytearray()
        async for chunk in request.stream():
            body += chunk
            if len(body) > MAX_AUDIO_BYTES:
                return _error(413, f"utterance too large (limit {MAX_AUDIO_BYTES} bytes)")
        if mode == "live":
            if not ctype.startswith("audio/"):
                return _error(409, "this server is in LIVE SPEECH mode: send recorded audio, not text")
            if not body:
                return _error(400, "empty recording")
            if spent_usd() >= max_usd:
                return _error(429, f"the demo's spend cap for this server run (${max_usd:.2f}) is reached")
            if not daily.allows():
                return _error(429, f"today's demo budget (${limits.daily_usd:.2f}) is used up; "
                                   "it resets at 00:00 UTC")
            utterance = ScriptedUtterance("", audio=bytes(body))
        else:
            if ctype != "application/json":
                return _error(409, "this server is in SIMULATED mode: send typed text as JSON, not audio")
            try:
                text = json.loads(bytes(body)).get("text", "")
            except (ValueError, AttributeError):
                return _error(400, "body must be JSON: {\"text\": ...}")
            if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
                return _error(400, f"text must be 1–{MAX_TEXT_CHARS} characters")
            utterance = parse_line(text.strip())
        del body
        before = spent_raw()
        try:
            code, payload = await asyncio.to_thread(run_turn, s, utterance)
        finally:
            daily.add(spent_raw() - before)  # whatever the providers were sent counts, even if the turn failed
            s.last_used = clock()
        if mode == "live" and "spend" in payload:
            payload["spend"] = spend_body()
        return JSONResponse(payload, status_code=code, headers={"Cache-Control": "no-store"})

    @app.get("/api/sessions/{sid}/ledger")
    async def ledger(sid: str, request: Request, after: int = 0, follow: bool = True):
        if not authed(request):
            return _error(401, "unauthorized: missing or invalid X-Sensei-Token")
        s = session_or_none(sid)
        if s is None:
            return _error(404, "no such session")
        s.last_used = clock()
        last = request.headers.get("last-event-id", "")
        cursor = max(0, after, int(last) + 1 if last.isdigit() else 0)

        async def stream():
            nonlocal cursor
            yield "retry: 2000\n\n"
            idle = 0.0
            while True:
                records = await asyncio.to_thread(s.ledger.wait_after, cursor, 1.0 if follow else 0.0)
                for r in records:
                    yield f"id: {r['index']}\nevent: ledger\ndata: {json.dumps(r, ensure_ascii=False)}\n\n"
                    cursor = r["index"] + 1
                if not follow or await request.is_disconnected():
                    return
                idle = 0.0 if records else idle + 1.0
                if idle >= KEEPALIVE_SECONDS:
                    idle = 0.0
                    yield ": keepalive\n\n"

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})

    app.state.sessions = sessions
    app.state.live_stt = live_stt
    app.state.daily = daily
    app.state.answers = answers
    app.state.model_spent = model_spent
    app.state.spent_usd = spent_usd
    return app


def bind_host(environ: dict[str, str] | None = None) -> str:
    """127.0.0.1 unless SENSEI_DEMO_ALLOW_LAN is exactly "1" (explicit opt-in, warned)."""
    env = os.environ if environ is None else environ
    if env.get(ALLOW_LAN_ENV) == "1":
        log.warning("%s=1: the demo binds all interfaces and is protected only by its token.", ALLOW_LAN_ENV)
        return "0.0.0.0"
    return "127.0.0.1"

"""Real speech adapters (Voice Lab Slice 6): Deepgram STT and ElevenLabs TTS.

Both implement the protocols in voice/adapters.py, so the loop, the gate and
the echo state machine are unchanged. They are never the default:
`make_stt` / `make_tts` return the fakes unless a real provider is named, and a
named provider whose Keychain key is missing fails closed (it never falls
back to the fake, and never to an environment variable or a file).

Keys come from core's credential broker, as the Kimi key does:

    security add-generic-password -a sensei -s sensei-deepgram-api-key -U -w
    security add-generic-password -a sensei -s sensei-elevenlabs-api-key -U -w

A key is read for one request and never stored on an adapter, logged, traced
or put in an error message.

Audio (the Slice 3 discipline, on the real path). Captured audio goes to the
STT and is dropped; synthesized audio goes to the player and is dropped.
Neither is returned to the loop, traced or written to a results file. The one
exception is forced by the platform player: macOS `afplay` can only open a
seekable file, so each reply is written to an owner-only file in a fresh
private temp directory and deleted the moment playback ends.

Standard library only (urllib), like core's KimiClient.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable

from sensei_core.credentials import keychain_secret

from voice.adapters import FakeSTT, FakeTTS, ScriptedUtterance, SpeechToText, SpokenReply, TextToSpeech, Transcript

DEEPGRAM_KEY_SERVICE = "sensei-deepgram-api-key"
ELEVENLABS_KEY_SERVICE = "sensei-elevenlabs-api-key"
DEEPGRAM_URL = "https://api.deepgram.com/v1/listen"
ELEVENLABS_URL = "https://api.elevenlabs.io/v1/text-to-speech"
DEFAULT_DEEPGRAM_MODEL = "nova-3"
DEFAULT_ELEVENLABS_MODEL = "eleven_flash_v2_5"
DEFAULT_VOICE_ID = "EXAVITQu4vr4xnSDxMaL"  # an ElevenLabs default voice (usable on every plan): one fixed agent voice
STT_PROVIDERS = ("fake", "deepgram")
TTS_PROVIDERS = ("fake", "elevenlabs")


class SpeechUnavailable(RuntimeError):
    """A real speech provider could not serve the request. Never carries a key or a response body."""

    def __init__(self, message: str, error_class: str) -> None:
        super().__init__(message)
        self.error_class = error_class


class SpeechKeyMissing(SpeechUnavailable):
    def __init__(self, provider: str, service: str) -> None:
        super().__init__(f"{provider} was selected but its key is not in the Keychain "
                         f"(service {service}, account sensei)", "key_missing")


def _ms(start: float, end: float) -> float:
    return round((end - start) * 1000, 1)


def _send(opener: Callable[..., Any], request: urllib.request.Request, timeout: float, provider: str,
          host: str) -> Any:
    try:
        return opener(request, timeout=timeout)
    except urllib.error.HTTPError as e:
        # Only the status reaches the message: never the body (it can echo the request), never the key.
        raise SpeechUnavailable(f"{provider} returned HTTP {e.code}", f"http_{e.code}") from None
    except (TimeoutError, socket.timeout):
        raise SpeechUnavailable(f"{provider} call timed out after {timeout:g} s", "timeout") from None
    except (urllib.error.URLError, ConnectionError, OSError) as e:
        reason = getattr(e, "reason", e)
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise SpeechUnavailable(f"{provider} call timed out after {timeout:g} s", "timeout") from None
        raise SpeechUnavailable(f"cannot reach {provider} at {host}: {type(reason).__name__}",
                                "unreachable") from None


class DeepgramSTT:
    """Per-utterance (pre-recorded) transcription of one push-to-talk capture.

    The confidence is Deepgram's own, for the top alternative, so the loop's
    low-confidence clarification runs unchanged on real recogniser output. An
    empty transcript (silence, noise) is returned with confidence 0.0, which the
    loop treats as "didn't catch that".

    `smart_format` is off by default: it rewrites "fifteen dollars" as "$15",
    and the measurement should see the recogniser's words, not a formatter's.
    A caller can opt in (the demo does, so a spoken "invoice two oh four"
    arrives as "invoice 204"); the parser is the same either way.
    """

    name = "deepgram"
    destination_host = "api.deepgram.com"

    def __init__(self, model: str = DEFAULT_DEEPGRAM_MODEL, *, language: str = "en", timeout: float = 30.0,
                 smart_format: bool = False,
                 key: Callable[[], str | None] = lambda: keychain_secret(DEEPGRAM_KEY_SERVICE),
                 opener: Callable[..., Any] = urllib.request.urlopen) -> None:
        self.model = model
        self.language = language
        self.timeout = timeout
        self.smart_format = smart_format
        self._key = key
        self._open = opener
        self.audio_bytes_seen = 0  # a count, for tests and spend estimates; the bytes are not kept
        self.audio_seconds = 0.0  # what Deepgram billed as the audio's duration, summed

    def url(self) -> str:
        query = {"model": self.model, "language": self.language, "punctuate": "true",
                 "smart_format": "true" if self.smart_format else "false"}
        return f"{DEEPGRAM_URL}?{urllib.parse.urlencode(query)}"

    def transcribe(self, utterance: ScriptedUtterance) -> Transcript:
        audio = utterance.audio
        if not audio:
            raise SpeechUnavailable("no audio was captured for this utterance", "no_audio")
        key = self._key()
        if not key:
            raise SpeechKeyMissing("Deepgram", DEEPGRAM_KEY_SERVICE)
        request = urllib.request.Request(self.url(), data=audio, method="POST", headers={
            "Authorization": f"Token {key}", "Content-Type": "audio/*"})
        del key
        self.audio_bytes_seen = len(audio)
        start = time.perf_counter()
        with _send(self._open, request, self.timeout, "Deepgram", self.destination_host) as response:
            raw = response.read()
        latency = _ms(start, time.perf_counter())
        try:
            body = json.loads(raw.decode("utf-8"))
            best = body["results"]["channels"][0]["alternatives"][0]
            text = best["transcript"].strip()
            confidence = float(best.get("confidence", 0.0))
        except (ValueError, KeyError, IndexError, TypeError, AttributeError):
            raise SpeechUnavailable("Deepgram reply had no transcript", "bad_response") from None
        self.audio_seconds += float((body.get("metadata") or {}).get("duration") or 0.0)
        return Transcript(text, min(1.0, max(0.0, confidence)) if text else 0.0, provider=self.name,
                          model=self.model, latency_ms=latency)


@dataclass(frozen=True)
class Synthesis:
    audio: bytes
    first_byte_ms: float
    total_ms: float


class ElevenLabsTTS:
    """Synthesizes each reply with ElevenLabs' streaming endpoint and plays it with the platform player.

    `speak` measures time to the first audio byte and to the last, plays the
    audio, and returns only counts and timings: the audio itself is dropped.
    `synthesize` (the real-audio measurement only) returns the bytes to the
    caller, which passes them straight to an STT and drops them.
    """

    name = "elevenlabs"
    destination_host = "api.elevenlabs.io"

    def __init__(self, voice_id: str = DEFAULT_VOICE_ID, model: str = DEFAULT_ELEVENLABS_MODEL, *,
                 output_format: str = "mp3_44100_128", timeout: float = 30.0,
                 key: Callable[[], str | None] = lambda: keychain_secret(ELEVENLABS_KEY_SERVICE),
                 opener: Callable[..., Any] = urllib.request.urlopen,
                 player: Callable[[bytes], None] | None = None) -> None:
        self.voice_id = voice_id
        self.model = model
        self.output_format = output_format
        self.timeout = timeout
        self._key = key
        self._open = opener
        self.player = player if player is not None else play_audio
        self.spoken: list[str] = []  # reply text only, as FakeTTS keeps it
        self.stops: list[int] = []
        self.characters = 0  # characters sent for synthesis, summed
        self.billed_characters = 0  # from ElevenLabs' character-cost response header, when present

    def url(self) -> str:
        return (f"{ELEVENLABS_URL}/{urllib.parse.quote(self.voice_id)}/stream?"
                f"{urllib.parse.urlencode({'output_format': self.output_format})}")

    def synthesize(self, text: str) -> Synthesis:
        key = self._key()
        if not key:
            raise SpeechKeyMissing("ElevenLabs", ELEVENLABS_KEY_SERVICE)
        request = urllib.request.Request(
            self.url(), data=json.dumps({"text": text, "model_id": self.model}).encode("utf-8"), method="POST",
            headers={"xi-api-key": key, "Content-Type": "application/json", "Accept": "audio/mpeg"})
        del key
        self.characters += len(text)
        start = time.perf_counter()
        chunks: list[bytes] = []
        first = None
        with _send(self._open, request, self.timeout, "ElevenLabs", self.destination_host) as response:
            cost = (getattr(response, "headers", None) or {}).get("character-cost")
            if cost is not None and str(cost).isdigit():
                self.billed_characters += int(cost)  # what ElevenLabs says it charged, when it says
            while chunk := response.read(4096):
                if first is None:
                    first = time.perf_counter()
                chunks.append(chunk)
        end = time.perf_counter()
        if first is None:
            raise SpeechUnavailable("ElevenLabs returned no audio", "bad_response")
        return Synthesis(b"".join(chunks), _ms(start, first), _ms(start, end))

    def speak(self, text: str) -> SpokenReply:
        self.spoken.append(text)
        synthesis = self.synthesize(text)
        started = time.perf_counter()
        self.player(synthesis.audio)
        return SpokenReply(text, len(synthesis.audio), provider=self.name, model=self.model,
                           first_byte_ms=synthesis.first_byte_ms, total_ms=synthesis.total_ms,
                           playback_started=started)

    def stop(self, segments_heard: int) -> None:
        # Playback is synchronous in the push-to-talk loop: a reply has finished before the next capture starts.
        self.stops.append(segments_heard)


def play_audio(audio: bytes, *, player: str | None = None) -> None:
    """Play audio with macOS `afplay`. It needs a seekable file, so the audio lives in an owner-only file in a
    fresh private directory for exactly as long as playback takes, then both are removed."""
    exe = player or shutil.which("afplay")
    if exe is None:
        raise SpeechUnavailable("no audio player: afplay (macOS) was not found", "no_player")
    folder = tempfile.mkdtemp(prefix="sensei-voice-")  # mode 0700
    path = os.path.join(folder, "reply.mp3")
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(audio)
        subprocess.run([exe, path], check=False, capture_output=True)
    finally:
        if os.path.exists(path):
            os.unlink(path)
        os.rmdir(folder)


def record_utterance(*, max_seconds: float = 15.0, recorder: str | None = None,
                     run: Callable[..., Any] = subprocess.run) -> bytes:
    """One push-to-talk capture from the default microphone, as WAV bytes in memory (never a file).

    Uses SoX's `rec`, which stops after 1.2 s of silence or at max_seconds. SoX is
    not installed by this package (`brew install sox`)."""
    exe = recorder or shutil.which("rec")
    if exe is None:
        raise SpeechUnavailable("no recorder: install SoX (`brew install sox`) for microphone capture", "no_recorder")
    out = run([exe, "-q", "-c", "1", "-r", "16000", "-b", "16", "-t", "wav", "-",
               "silence", "1", "0.1", "2%", "1", "1.2", "2%", "trim", "0", f"{max_seconds:g}"],
              capture_output=True, timeout=max_seconds + 10, check=False)
    if out.returncode != 0 or not out.stdout:
        raise SpeechUnavailable(f"recording failed (rec exit {out.returncode})", "record_failed")
    return out.stdout


def _key_present(service: str, lookup: Callable[[str], str | None] | None) -> bool:
    return bool((lookup or keychain_secret)(service))  # the value is checked and dropped here, never kept


def make_stt(provider: str = "fake", *, lookup: Callable[[str], str | None] | None = None,
             **kwargs: Any) -> SpeechToText:
    """The STT for a configured provider. The fake unless a real one is named; a named one needs its key."""
    if provider == "fake":
        return FakeSTT()
    if provider == "deepgram":
        if not _key_present(DEEPGRAM_KEY_SERVICE, lookup):
            raise SpeechKeyMissing("Deepgram", DEEPGRAM_KEY_SERVICE)
        return DeepgramSTT(**kwargs)
    raise ValueError(f"unknown STT provider {provider!r}; choose from {', '.join(STT_PROVIDERS)}")


def make_tts(provider: str = "fake", *, lookup: Callable[[str], str | None] | None = None,
             **kwargs: Any) -> TextToSpeech:
    """The TTS for a configured provider. The fake unless a real one is named; a named one needs its key."""
    if provider == "fake":
        return FakeTTS()
    if provider == "elevenlabs":
        if not _key_present(ELEVENLABS_KEY_SERVICE, lookup):
            raise SpeechKeyMissing("ElevenLabs", ELEVENLABS_KEY_SERVICE)
        return ElevenLabsTTS(**kwargs)
    raise ValueError(f"unknown TTS provider {provider!r}; choose from {', '.join(TTS_PROVIDERS)}")

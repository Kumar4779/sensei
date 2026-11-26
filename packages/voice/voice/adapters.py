"""Speech adapters (Voice Lab Slice 1): STT and TTS behind interfaces, with fakes.

Slice 1 is transcript-first. FakeSTT takes a scripted utterance (the text the
recogniser would return, plus its confidence), and FakeTTS returns the reply
text without synthesizing audio. Both are deterministic and offline, and need
no keys and no microphone. The real providers (Slice 6, voice/speech.py)
implement the same two protocols and are used only when explicitly selected.

Audio is never stored. Neither adapter returns audio, and the trace records
transcripts only. A real adapter also says which provider and model served the
turn and how long it took, so real-path latency is traced apart from the
transcript path's.

Barge-in (Slice 2): a scripted utterance can say it was spoken while the
previous reply was still playing, after `barge_in_after` of that reply's
segments had been heard. TTS.stop records where playback was cut off.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class ScriptedUtterance:
    """What the user 'said', as the recogniser would hear it."""

    transcript: str
    confidence: float = 0.95
    barge_in_after: int | None = None  # segments of the previous reply heard before the user spoke
    audio: bytes = field(default=b"", repr=False)  # the captured audio (Slice 3); dropped by the STT, never kept


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float | None  # None when the provider gives no confidence
    provider: str | None = None  # set by a real STT only (Slice 6)
    model: str | None = None
    latency_ms: float | None = None  # the provider round trip


@dataclass(frozen=True)
class SpokenReply:
    text: str
    audio_bytes: int = 0  # how much audio was produced; the audio itself is not kept
    provider: str | None = None  # set by a real TTS only (Slice 6)
    model: str | None = None
    first_byte_ms: float | None = None  # request sent → first audio byte received
    total_ms: float | None = None  # request sent → last audio byte received
    playback_started: float | None = None  # time.perf_counter() when the player started


class SpeechToText(Protocol):
    name: str

    def transcribe(self, utterance: ScriptedUtterance) -> Transcript: ...


class TextToSpeech(Protocol):
    name: str

    def speak(self, text: str) -> SpokenReply: ...

    def stop(self, segments_heard: int) -> None: ...


class FakeSTT:
    name = "fake-stt"
    audio_bytes_seen = 0

    def transcribe(self, utterance: ScriptedUtterance) -> Transcript:
        if not 0.0 <= utterance.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {utterance.confidence}")
        self.audio_bytes_seen = len(utterance.audio)  # a count, for tests; the bytes themselves are not kept
        return Transcript(utterance.transcript, utterance.confidence)


class FakeTTS:
    name = "fake-tts"

    def __init__(self) -> None:
        self.spoken: list[str] = []  # reply text only, for tests
        self.stops: list[int] = []  # segments heard when playback was stopped

    def speak(self, text: str) -> SpokenReply:
        self.spoken.append(text)
        return SpokenReply(text)

    def stop(self, segments_heard: int) -> None:
        self.stops.append(segments_heard)

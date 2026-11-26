"""Speech adapters (Voice Lab Slice 1): STT and TTS behind interfaces, with fakes.

Slice 1 is transcript-first. FakeSTT takes a scripted utterance (the text the
recogniser would return, plus its confidence), and FakeTTS returns the reply
text without synthesizing audio. Both are deterministic and offline, and need
no keys and no microphone. A real provider is a later slice and implements the
same two protocols.

Audio is never stored. Neither adapter returns audio, and the trace records
transcripts only.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ScriptedUtterance:
    """What the user 'said', as the recogniser would hear it."""

    transcript: str
    confidence: float = 0.95


@dataclass(frozen=True)
class Transcript:
    text: str
    confidence: float | None  # None when the provider gives no confidence


@dataclass(frozen=True)
class SpokenReply:
    text: str
    audio_bytes: int = 0  # how much audio was produced; the audio itself is not kept


class SpeechToText(Protocol):
    name: str

    def transcribe(self, utterance: ScriptedUtterance) -> Transcript: ...


class TextToSpeech(Protocol):
    name: str

    def speak(self, text: str) -> SpokenReply: ...


class FakeSTT:
    name = "fake-stt"

    def transcribe(self, utterance: ScriptedUtterance) -> Transcript:
        if not 0.0 <= utterance.confidence <= 1.0:
            raise ValueError(f"confidence must be in [0, 1], got {utterance.confidence}")
        return Transcript(utterance.transcript, utterance.confidence)


class FakeTTS:
    name = "fake-tts"

    def __init__(self) -> None:
        self.spoken: list[str] = []  # reply text only, for tests

    def speak(self, text: str) -> SpokenReply:
        self.spoken.append(text)
        return SpokenReply(text)

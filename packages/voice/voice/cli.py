"""Terminal push-to-talk loop (Voice Lab Slice 1): each line you type is one utterance.

    python -m voice.cli [--trace PATH] [--stt fake|deepgram] [--tts fake|elevenlabs]

Type what the recogniser "heard". Prefix a line with "~0.4 " to simulate a
low-confidence transcript. An empty line or Ctrl-D ends the session, and the
hard-gate verdict for the run is printed. The trace holds transcripts only,
never audio.

Real speech (Slice 6) is opt-in per side and needs that provider's Keychain
key; a selected provider without its key exits before the session starts.
  --tts elevenlabs  speaks every reply aloud (afplay).
  --stt deepgram    push-to-talk: press Enter, speak, and the capture ends after
                    a short silence. Microphone capture needs SoX's `rec`.
"""

from __future__ import annotations

import argparse
import re
import sys
import uuid
from pathlib import Path

from sensei_core.trace import TraceWriter, read_events

from voice.adapters import ScriptedUtterance
from voice.loop import VoiceSession
from voice.speech import STT_PROVIDERS, TTS_PROVIDERS, SpeechUnavailable, make_stt, make_tts, record_utterance
from voice.verify import verify_run

DEFAULT_TRACE_DIR = Path.home() / ".sensei" / "voice" / "traces"


def parse_line(line: str) -> ScriptedUtterance:
    if m := re.match(r"~(\d(?:\.\d+)?)\s+(.*)$", line):
        return ScriptedUtterance(m.group(2), float(m.group(1)))
    return ScriptedUtterance(line)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Talk to the Voice Lab reference agent (transcripts, no audio).")
    parser.add_argument("--trace", type=Path, help="trace file (JSONL); default ~/.sensei/voice/traces/<run>.jsonl")
    parser.add_argument("--stt", choices=STT_PROVIDERS, default="fake", help="speech-to-text provider (default fake)")
    parser.add_argument("--tts", choices=TTS_PROVIDERS, default="fake", help="text-to-speech provider (default fake)")
    args = parser.parse_args(argv)
    try:
        stt, tts = make_stt(args.stt), make_tts(args.tts)
    except SpeechUnavailable as e:
        print(f"voice-loop: {e}", file=sys.stderr)
        return 2
    run_id = uuid.uuid4().hex[:12]
    path = args.trace or DEFAULT_TRACE_DIR / f"{run_id}.jsonl"
    session = VoiceSession(TraceWriter(path, run_id=run_id, session_id="voice-cli"), stt=stt, tts=tts)
    interactive = sys.stdin.isatty()
    while True:
        try:
            line = input(("[Enter] to talk, q to quit> " if args.stt != "fake" else "you> ")
                         if interactive else "").strip()
        except EOFError:
            break
        if args.stt == "fake" and not line or args.stt != "fake" and line.lower() == "q":
            break
        try:
            utterance = parse_line(line) if args.stt == "fake" else ScriptedUtterance("", audio=record_utterance())
            reply = session.turn(utterance)
        except SpeechUnavailable as e:
            print(f"voice-loop: {e}", file=sys.stderr)
            break
        if args.stt != "fake":
            print(f"heard> {session.last_heard}")
        print(f"sensei> {reply}")
    verdict = verify_run(read_events(path)) if path.exists() else verify_run([])
    print(f"\nhard gate: {'PASS' if verdict.passed else 'FAIL'}" +
          "".join(f"\n  - {v}" for v in verdict.violations) + f"\ntrace: {path}")
    return 0 if verdict.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Real-audio runs (Voice Lab Slice 6): LOCAL ONLY, never in CI. They need both Keychain keys and the network.

    python evals/real_audio.py preflight             # free calls only: voices, usage, balance
    python evals/real_audio.py smoke    [--max-usd 1.00]
    python evals/real_audio.py bench-v2 [--max-usd 1.00]

smoke     The morning brief read aloud, and "send the note to Alex" with the echo
          spoken and confirmed by voice. The user's lines are synthesized in a
          second voice and transcribed by Deepgram; the agent's replies are
          synthesized and played with afplay.
bench-v2  The first real-audio run: every voice-bench-v2 "said" line is
          synthesized (ElevenLabs), transcribed back (Deepgram), and the real heard
          text and confidence go through the unchanged loop. A MEASUREMENT, k=1, not
          a pass/fail claim: it records where real recognition diverges from the
          scripted "heard" text and what the loop did with it. The frozen task file
          is read, never written; results go to a new file.

Spend: estimated before any paid call from the text to be synthesized and a
speaking-rate guess for the audio, and the run refuses to start above
--max-usd. The provider usage counters are read before and after so the report
shows what was actually billed, not only the estimate.

No audio is written anywhere, apart from afplay's transient file (voice/speech.py).
Results and traces hold text, confidences and timings only.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from sensei_core.credentials import keychain_secret  # noqa: E402
from sensei_core.trace import TraceWriter, read_events  # noqa: E402

import harness  # noqa: E402
from voice.adapters import ScriptedUtterance  # noqa: E402
from voice.loop import VoiceSession  # noqa: E402
from voice.speech import (DEEPGRAM_KEY_SERVICE, ELEVENLABS_KEY_SERVICE, DeepgramSTT, ElevenLabsTTS,  # noqa: E402
                          make_stt, make_tts)
from voice.verify import verify_run  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
V2_TASKS = Path(__file__).resolve().parent / "voice-bench-v2" / "tasks.json"
# Published list prices (checked 2026-11-26): ElevenLabs Flash/Turbo $0.04 per 1K characters on the API,
# Deepgram Nova-3 pre-recorded $0.0043/min pay-as-you-go. The estimate uses a conservative $0.06 per 1K.
TTS_USD_PER_1K_CHARS = 0.06
STT_USD_PER_MIN = 0.0043
SECONDS_PER_CHAR = 0.07  # speaking-rate guess for estimating audio length before it exists
USER_VOICE_ID = "pNInz6obpgDQGcFmaJgB"  # a second default voice, so the "user" never sounds like the agent

SMOKE_SESSIONS = [("morning brief", ["Read me my morning brief."]),
                  ("send the note to Alex", ["Send the note to Alex.", "Yes."])]
# The agent's replies in the smoke are spoken too; their text is only known at run time, so budget for it.
SMOKE_REPLY_CHARS_BUDGET = 600


def estimate_usd(tts_chars: int, stt_chars: int) -> float:
    return tts_chars / 1000 * TTS_USD_PER_1K_CHARS + stt_chars * SECONDS_PER_CHAR / 60 * STT_USD_PER_MIN


def actual_usd(tts_chars: int, stt_seconds: float) -> float:
    return round(tts_chars / 1000 * TTS_USD_PER_1K_CHARS + stt_seconds / 60 * STT_USD_PER_MIN, 4)


def _get(url: str, headers: dict[str, str]) -> Any:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def usage() -> dict[str, Any]:
    """Provider counters (free read-only calls). Never prints or returns a key."""
    out: dict[str, Any] = {}
    try:
        sub = _get("https://api.elevenlabs.io/v1/user/subscription",
                   {"xi-api-key": keychain_secret(ELEVENLABS_KEY_SERVICE) or ""})
        out["elevenlabs"] = {"tier": sub.get("tier"), "character_count": sub.get("character_count"),
                             "character_limit": sub.get("character_limit")}
    except Exception as e:  # a restricted key may not read its own usage: report, don't fail
        out["elevenlabs"] = {"error": type(e).__name__ + (f" {e.code}" if hasattr(e, "code") else "")}
    try:
        auth = {"Authorization": f"Token {keychain_secret(DEEPGRAM_KEY_SERVICE) or ''}"}
        projects = _get("https://api.deepgram.com/v1/projects", auth)["projects"]
        balances = _get(f"https://api.deepgram.com/v1/projects/{projects[0]['project_id']}/balances", auth)
        out["deepgram"] = {"balance": [{"amount": b.get("amount"), "units": b.get("units")}
                                       for b in balances.get("balances", [])]}
    except Exception as e:
        out["deepgram"] = {"error": type(e).__name__ + (f" {e.code}" if hasattr(e, "code") else "")}
    return out


def preflight() -> int:
    report: dict[str, Any] = {}
    try:  # a key scoped to text-to-speech only cannot list voices: that is reported, not fatal
        voices = _get("https://api.elevenlabs.io/v1/voices",
                      {"xi-api-key": keychain_secret(ELEVENLABS_KEY_SERVICE) or ""})
        ids = {v["voice_id"]: v["name"] for v in voices.get("voices", [])}
        report.update(agent_voice=ids.get(ElevenLabsTTS().voice_id), user_voice=ids.get(USER_VOICE_ID))
    except Exception as e:
        report["voices"] = {"error": type(e).__name__ + (f" {e.code}" if hasattr(e, "code") else "")}
    report["usage"] = usage()
    print(json.dumps(report, indent=2))
    return 0


def _guard(estimate: float, cap: float) -> None:
    print(f"estimated spend ${estimate:.4f} (cap ${cap:.2f})")
    if estimate > cap:
        raise SystemExit(f"refusing to start: the estimate ${estimate:.4f} is over the ${cap:.2f} cap")


def _turns(trace: Path) -> list[dict[str, Any]]:
    return [e["payload"] for e in read_events(trace) if e["event_type"] == "voice_turn"]


def smoke(cap: float) -> int:
    lines = [line for _, script in SMOKE_SESSIONS for line in script]
    chars = sum(map(len, lines))
    _guard(estimate_usd(chars + SMOKE_REPLY_CHARS_BUDGET, chars), cap)
    before = usage()
    user = ElevenLabsTTS(voice_id=USER_VOICE_ID)
    stt, agent = make_stt("deepgram"), make_tts("elevenlabs")
    sessions = []
    with tempfile.TemporaryDirectory(prefix="sensei-voice-smoke-") as tmp:
        for title, script in SMOKE_SESSIONS:
            trace = Path(tmp) / f"{title.replace(' ', '-')}.jsonl"
            session = VoiceSession(TraceWriter(trace, run_id=f"smoke-{title}", session_id="voice-live-smoke"),
                                   stt=stt, tts=agent)
            for line in script:
                print(f"user (synthesized)> {line}")
                reply = session.turn(ScriptedUtterance("", audio=user.synthesize(line).audio))
                print(f"heard> {session.last_heard}\nsensei (spoken)> {reply}")
            events = read_events(trace)
            sessions.append({"session": title, "said": script, "turns": [
                {k: t.get(k) for k in ("transcript", "stt_confidence", "outcome", "reply", "real_path")}
                for t in _turns(trace)], "hard_gate": "PASS" if verify_run(events).passed else "FAIL",
                "outbox": session.world.snapshot()["outbox"]})
    tts_chars = user.characters + agent.characters
    result = {"run": "voice-live-smoke", "date": date.today().isoformat(), "path": "real audio (Deepgram + ElevenLabs)",
              "stt": {"provider": "deepgram", "model": stt.model}, "tts": {"provider": "elevenlabs", "model": agent.model},
              "sessions": sessions, "spend": {"tts_characters": tts_chars, "tts_billed_characters_header": user.billed_characters + agent.billed_characters,
                                              "stt_audio_seconds": round(stt.audio_seconds, 2),
                                              "estimated_usd": actual_usd(tts_chars, stt.audio_seconds)},
              "usage_before": before, "usage_after": usage()}
    path = _write(result, "voice-live-smoke")
    print(json.dumps({s["session"]: [t["outcome"] for t in s["turns"]] + [s["hard_gate"]] for s in sessions}))
    print(f"spend ~${result['spend']['estimated_usd']:.4f}\nresults: {path}")
    return 0


def normalise(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s$]", " ", text.lower())).strip()


def run_trial(task: dict[str, Any], trace: Path) -> harness.Trial:
    """harness.run_trial, except a failure the frozen taxonomy has no label for is recorded, not raised.

    On real audio a scripted mishearing may simply not happen ("fifteen" heard as
    "fifteen"), so the probe's expected echo is wrong for what was actually heard.
    That is the measurement; the harness and its labels are unchanged."""
    try:
        return harness.run_trial(task, trace)
    except harness.UnlabelledFailure:
        trace.unlink(missing_ok=True)
        label, harness.label = harness.label, lambda task, t: "unlabelled (real audio: see checker_failures)"
        try:
            return harness.run_trial(task, trace)
        finally:
            harness.label = label


def bench_v2(cap: float) -> int:
    tasks = harness.load(V2_TASKS)["tasks"]
    said = sorted({t["said"] for task in tasks for t in task["turns"]})
    chars = sum(map(len, said))
    _guard(estimate_usd(chars, chars), cap)
    before = usage()
    tts = ElevenLabsTTS(voice_id=USER_VOICE_ID)
    stt: DeepgramSTT = make_stt("deepgram")  # type: ignore[assignment]
    recognised: dict[str, dict[str, Any]] = {}
    for line in said:  # each distinct line once: audio in, transcript out, audio dropped
        synthesis = tts.synthesize(line)
        t = stt.transcribe(ScriptedUtterance("", audio=synthesis.audio))
        recognised[line] = {"heard": t.text, "confidence": t.confidence, "stt_latency_ms": t.latency_ms,
                            "tts_first_byte_ms": synthesis.first_byte_ms, "tts_total_ms": synthesis.total_ms}
        print(f"said: {line!r}\n  heard: {t.text!r} ({t.confidence:.3f})")
    # Saved before grading, so a grading error never costs a second paid pass (text and timings only).
    (Path(tempfile.gettempdir()) / "sensei-voice-v2-recognised.json").write_text(json.dumps(recognised, indent=2))
    rows = []
    with tempfile.TemporaryDirectory(prefix="sensei-voice-v2-real-") as tmp:
        for task in tasks:
            real = {**task, "turns": [{**turn, "heard": recognised[turn["said"]]["heard"],
                                       "confidence": recognised[turn["said"]]["confidence"]} for turn in task["turns"]]}
            trial = run_trial(real, Path(tmp) / f"{task['id']}.jsonl")
            rows.append({
                "task": task["id"], "kind": task["kind"], "category": task["category"],
                "turns": [{"said": s["said"], "scripted_heard": s["heard"], "scripted_confidence": s["confidence"],
                           **recognised[s["said"]],
                           "diverges_from_scripted": normalise(recognised[s["said"]]["heard"]) != normalise(s["heard"]),
                           "diverges_from_said": normalise(recognised[s["said"]]["heard"]) != normalise(s["said"])}
                          for s in task["turns"]],
                "expected_outcomes": task["outcomes"], "real_outcomes": [t["outcome"] for t in trial.turns],
                "replies": [t["reply"] for t in trial.turns],
                "checker_failures": trial.failures, "label": trial.label,
                "hard_gate": "PASS" if trial.verdict.passed else "FAIL"})
    result = {"eval_version": "voice-bench-v2", "run": "voice-bench-v2-real-audio", "date": date.today().isoformat(),
              "k": 1, "kind": "measurement (not a pass/fail claim; never merged with transcript-path numbers)",
              "path": "real audio: said line → ElevenLabs TTS → Deepgram STT → unchanged loop (replies not synthesized)",
              "stt": {"provider": "deepgram", "model": stt.model, "options": "punctuate=true, smart_format=false"},
              "tts": {"provider": "elevenlabs", "model": tts.model, "voice_id": tts.voice_id},
              "tasks": rows,
              "summary": {"distinct_lines": len(said),
                          "lines_diverging_from_scripted_heard": sum(
                              normalise(r["heard"]) != normalise(next(t["heard"] for task in tasks for t in task["turns"]
                                                                      if t["said"] == line))
                              for line, r in recognised.items()),
                          "tasks_matching_expected_outcomes": sum(r["real_outcomes"] == r["expected_outcomes"] for r in rows),
                          "hard_gate_all_tasks": "PASS" if all(r["hard_gate"] == "PASS" for r in rows) else "FAIL"},
              "spend": {"tts_characters": tts.characters, "tts_billed_characters_header": tts.billed_characters,
                        "stt_audio_seconds": round(stt.audio_seconds, 2),
                        "estimated_usd": actual_usd(tts.characters, stt.audio_seconds)},
              "usage_before": before, "usage_after": usage()}
    path = _write(result, "voice-bench-v2-real-audio")
    print(json.dumps(result["summary"]))
    print(f"spend ~${result['spend']['estimated_usd']:.4f}\nresults: {path}")
    return 0


def _write(result: dict[str, Any], stem: str) -> Path:
    """A new file per run, never overwriting an earlier one (the harness rule)."""
    base = f"{stem}-{result['date'].replace('-', '')}"
    path, n = RESULTS / f"{base}.json", 2
    while path.exists():
        path, n = RESULTS / f"{base}-{n}.json", n + 1
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Voice Lab real-audio runs (local only; needs both keys).")
    parser.add_argument("command", choices=["preflight", "smoke", "bench-v2"])
    parser.add_argument("--max-usd", type=float, default=1.00, help="refuse to start above this estimate")
    args = parser.parse_args(argv)
    if args.command == "preflight":
        return preflight()
    return smoke(args.max_usd) if args.command == "smoke" else bench_v2(args.max_usd)


if __name__ == "__main__":
    raise SystemExit(main())

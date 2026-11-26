# Talk to Sensei — the demo surface

A local web page where you talk to Sensei and watch its permission gate work.
You press and hold to speak, hear the reply in the page, and see a **live
permission ledger** beside the conversation. Every gate decision, read-back,
approval, decline and execution appears there as the voice loop writes it to
the trace.

**Status: in progress (Demo Slice 1, #21).** It runs locally on 127.0.0.1.
Public hosting is the next slice.

## Place in the architecture

The demo is a thin server around the existing voice loop
([`packages/voice`](../voice/README.md)):

```
browser (push-to-talk, MediaRecorder)
  → POST audio → Deepgram STT → VoiceSession (PermissionGate, confirmation-echo machine) → ElevenLabs TTS
  ← reply audio, transcript, real-path timings
browser (ledger panel) ← Server-Sent Events: the session's trace, record by record
```

Nothing in core, the gate, the echo state machine, the parser or the frozen
task sets changes. The ledger is the trace itself:
- The session's `TraceWriter` is a `LedgerTrace`. It writes each record to the
  trace file exactly as before (core's redaction, an owner-only file), then
  publishes the same record to the session's stream.
- Each record carries a readable step: proposed, echoed, approved or declined,
  executed, verified. "Verified" is the voice package's own run verifier
  (`verify_run`), checked after every turn and written to the trace as
  `hard_gate_check`.

It lives in `packages/` rather than `apps/`. `apps/` holds the native Swift
surfaces, which are clients of the gateway. This is a Python server that runs
the voice loop in-process, with its own tests and CI job, as the other
packages do.

## Security posture

It follows core's gateway (`sensei_core/gateway.py`):
- **Localhost by default.** It binds 127.0.0.1. `SENSEI_DEMO_ALLOW_LAN=1` is an
  explicit, warned opt-in.
- **Token auth.** `SENSEI_DEMO_TOKEN` must be set, or the server refuses to
  start. Every data endpoint needs `X-Sensei-Token`, compared in constant time.
  `/health` and the `/demo` token-entry shell are public and hold no data. The
  page keeps the token in `sessionStorage` (gone when the tab closes) and sends
  it as a header on every request, including the ledger stream. That stream is
  read with `fetch`, not `EventSource`, so the token never appears in a URL.
- **Keys never leave the server.** Deepgram and ElevenLabs are called
  server-side with the Keychain keys from Voice Lab Slice 6
  (`sensei-deepgram-api-key`, `sensei-elevenlabs-api-key`, account `sensei`).
  No response, page or stream carries a key.
- **Audio passes through.** The browser's recording goes to Deepgram and is
  dropped. The reply's audio goes back in that turn's response, is played,
  and is dropped. It is never traced or written to disk. Recordings over 2 MB
  are refused before any provider call.
- **Page hardening.** A strict Content-Security-Policy (no `eval`, no
  framing), `Permissions-Policy: microphone=(self)`, `no-store` caching, and
  every server-provided string set as text, never as HTML.
- **A spend cap.** Live turns are refused once the estimated spend for the
  server run reaches `--max-usd` (default $1.00).

## Modes: the badge never lies

| Badge | Input | Adapters | When |
|---|---|---|---|
| **LIVE SPEECH** | push-to-talk audio | Deepgram STT, ElevenLabs TTS | the default; needs both Keychain keys |
| **SIMULATED** | typed text | fake STT and TTS (no audio) | `--simulated`, for development without keys |

Live mode fails closed. If either key is missing, the server refuses to start
and points to `--simulated`; it never falls back silently. A live server
refuses typed turns, and a simulated server refuses audio.

In live mode, each turn shows its real-path timings from the trace:
- the STT round trip;
- TTS first byte;
- server-side time to first audio (turn start to reply audio ready);
- a browser-measured chip: from releasing the button to the reply playing.

## The three guided scenarios

Each card starts a fresh session on the voice World's synthetic fixtures,
labelled on the page as demo data.

| Card | Say | The ledger shows |
|---|---|---|
| Morning brief (read-only) | "Read me my morning brief." | gate allowed → executed → verified |
| Send a note to Alex (echo, then confirm) | "Send the note to Alex." then "Yes." | gate needs approval → proposed → echoed → approved by you → executed → verified (1 irreversible action, echoed and confirmed) |
| Pay an invoice (echo, then decline) | "Pay invoice 204." then "No." | gate needs approval → proposed → echoed → **declined: nothing will run** → verified (nothing irreversible ran) |

The demo's Deepgram has `smart_format` on, so a spoken "invoice two oh four"
arrives as "invoice 204". Without it, the recogniser returns "two zero four",
which the parser doesn't read yet (#16). As a side effect, a spoken dollar
amount arrives as "$15", which the parser doesn't read either, so it gets a
clarification. None of the cards uses a spoken amount.

## Run it

```
cd packages/demo
python -m venv .venv
.venv/bin/pip install -e ../core -e ../voice -e '.[dev]'
.venv/bin/python -m pytest -q
export SENSEI_DEMO_TOKEN=$(openssl rand -hex 24)
.venv/bin/python -m sensei_demo               # LIVE SPEECH (needs both keys)
.venv/bin/python -m sensei_demo --simulated   # typed input, no keys
```

Open `http://127.0.0.1:8766/demo` and enter the token. Traces go to
`~/.sensei/demo/traces/` (owner-only).

## Tests

`tests/` runs fully offline. Speech providers are mocked, and a fixture fails
any test that opens a socket. The tests cover:
- the endpoints in both modes: auth on every data endpoint, the public shell,
  mode refusals, bounds, the spend cap, and provider failures;
- that no key or audio reaches a response, the stream or the trace;
- the ledger stream against scripted sessions. Events arrive in trace order,
  a reconnecting stream resumes where it stopped, and a followed stream
  delivers events while the turn runs. An irreversible `tool_executed` never
  precedes its action's echo and confirmation, and the pay decline shows a
  decline and no execution.

## Verified end to end (2026-11-26)

All three scenarios ran in Google Chrome against the local server in LIVE
SPEECH mode.

**How the run was done.** The machine's microphone wasn't available to the
automated browser: Chrome's macOS microphone permission was never granted.
So the visitor's voice was an audio stream substituted for `getUserMedia` in
the test harness. Everything after capture was the real path: Chrome's
MediaRecorder, the upload, Deepgram, the voice loop, ElevenLabs, and playback
in the page.

**Results:**
- **Morning brief:** heard "Read me my morning brief." (1.00), then gate
  allowed, executed, verified.
- **Send a note to Alex:** echoed, "Yes." heard (0.98), approved, executed
  once, verified.
- **Pay an invoice:** "Pay invoice 204" heard (0.96) and echoed as "pay Acme
  Paper $1,250.00 for invoice INV-204". "No." was heard (0.95), the ledger
  showed "Declined: nothing will run", no `tool_executed` event appeared, and
  verification passed.
- **Recogniser misses:** two macOS system voices saying "brief" were heard as
  "breathe" and "breeze". The agent asked for clarification instead of
  guessing. An ElevenLabs voice was used for that line.
- **Latency:** server-side time to first audio was 618–1,173 ms per turn. In
  the page, playback started 648–1,207 ms after the button was released.

## Not yet built

- Public hosting (the next slice).
- Barge-in in the browser: a reply plays to the end before the next turn.
- Streaming STT and streaming playback.
- Accounts or multiple users. Sessions are in memory, at most 8.
- Real mail and calendar connectors: the demo uses the synthetic World.
- Speech calls in core's egress log (#19).

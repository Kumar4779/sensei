# Talk to Sensei — the demo surface

A local web page where you talk to Sensei and watch its permission gate work.
You press and hold to speak, hear the reply in the page, and see a **live
permission ledger** beside the conversation. Every gate decision, read-back,
approval, decline and execution appears there as the voice loop writes it to
the trace.

**Status: in progress (Demo Slice 2, #23).** It runs locally on 127.0.0.1,
and publicly at [sensei-talk.fly.dev](https://sensei-talk.fly.dev) behind an
access token, with the spend and abuse limits below.

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

## Live Avatar (#26)

Sensei's face follows the demo's real state, never timers or decoration.

| Demo state | Face | Motion |
|---|---|---|
| idle | the idle portrait | a gentle bob |
| recording (the orb is held, the mic is live) | listening | |
| working (a request in flight: STT, a tool, or a model answer being generated) | thinking | |
| awaiting approval (the echo was spoken; waiting for your yes or no) | listening | a glow pulse |
| speaking (the reply's audio is playing) | speaking | a subtle bounce |
| verified (a turn executed and the hard gate passed) | happy for 2.5 s, then idle | |
| declined | straight back to idle | |

A "no" is the system working as designed, so Sensei never looks sad about it.

- **Transitions:** faces crossfade in 250 ms. All motion is `transform` and `opacity` only, and `prefers-reduced-motion` turns the bob, bounce and pulse off.
- **Test hook:** the state is `data-state` on the avatar figure, so the headless harness can watch it.
- **Assets:** five portraits of the same avatar at 1168×2112, served exactly like the idle one: token-gated, byte-for-byte, with tests.
- **Page weight:** the four new faces add **1,512,674 bytes (about 1.51 MB)**: listening 371,296, thinking 373,520, speaking 369,972, happy 397,886. With the idle face, the avatar totals 1,857,968 bytes. The idle face loads first; the other four are fetched one at a time after first paint, when the browser is idle, so the page's initial load isn't held up. Until a face has loaded, that state shows the idle face.
- **Favicon:** the shell's `<link rel="icon" href="data:,">` stops the browser's automatic `/favicon.ico` request. Once signed in, the app draws Sensei's head from the token-fetched idle face into a 64×64 PNG and makes it the tab icon. No unauthenticated asset route is added.

## Free-form questions (#25)

The parser runs first, exactly as before: a command takes the deterministic
path through the gate and the echo machine. Only an utterance that matches no
command goes to core's model backend.
- **What counts:** a clarification with reason "unrecognised", heard at good
  confidence. Low-confidence input and ambiguous or incomplete commands ("send
  it") still clarify.
- **How it's called:** one core `Executor.run_once` through core's `Router`,
  which sends `API_OK` "simple" requests to `cheap_api` (Kimi via the Moonshot
  API, Slice 35). There's no second model integration.
- **What it gets:** a Sensei system prompt (a warm, direct teacher-mentor,
  answering in short, plain sentences made for speech) and the session's
  recent turns.
- **Sensitivity:** voice commands stay `LOCAL_ONLY`. A public-demo visitor's
  question is classed `API_OK`: their speech already goes to cloud speech
  providers, and the demo holds only synthetic data.
- **Ledger:** core traces `route_decision` and `model_call` (the model and
  core's estimated cost) into the session's own trace, so the ledger shows
  "Routed to cheap_api" and "Answered by kimi-k3". The call goes into the
  egress log too, as sizes and a digest, never content.
- **Read-only by construction:** the model gets a prompt and returns text. It
  has no tools, its text never reaches the parser, and nothing it says can
  propose, confirm or run an action.
- **Spend:** the model's cost counts toward the per-run cap and the $5/day
  ceiling. If either is reached, Sensei says so instead of calling.
- **Failure honesty:** if the call fails, times out, hits quota or returns
  nothing, Sensei says it can't reach its model right now, and the ledger
  shows "No model answer" with the reason. It never fakes an answer.
- **Keys:** the Moonshot key comes from core's Keychain item
  (`sensei-kimi-api-key`) locally, and from `KIMI_API_KEY` with `--keys env`
  on Fly.
- **Prompt:** for facts that can change, the model gives its best answer
  with one short note that it may have changed, and declines only when it
  genuinely doesn't know.
- **Latency:** Kimi K3 reasons before answering, so a spoken answer starts
  about 4–6 s after you stop talking on the public site (occasionally more);
  commands are unaffected.

## Hosted on Fly.io (Demo Slice 2)

[sensei-talk.fly.dev](https://sensei-talk.fly.dev) runs the same server on
one small Fly.io machine: shared-cpu-1x, 256 MB, in Chicago (`ord`).
- **Image** (`Dockerfile`): built from the repository root, with only
  `packages/core`, `packages/voice` and `packages/demo` in the build context
  (`.dockerignore`). It holds no secret. The server runs as an unprivileged
  user; the entrypoint's only root step is handing the volume to that user.
- **Secrets:** `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY` and
  `SENSEI_DEMO_TOKEN` reach the machine only as Fly secrets (environment
  variables). The server reads the keys with `--keys env`, chosen explicitly;
  the local default stays the Keychain, with no fallback between the two.
  `KIMI_API_KEY` (#25) is set the same way, for free-form answers. They were
  set straight from the Keychain, so no value is ever printed:

  ```
  security add-generic-password -a sensei -s sensei-demo-token -U -w "$(openssl rand -hex 24)"
  printf 'DEEPGRAM_API_KEY=%s\nELEVENLABS_API_KEY=%s\nSENSEI_DEMO_TOKEN=%s\n' \
    "$(security find-generic-password -a sensei -s sensei-deepgram-api-key -w)" \
    "$(security find-generic-password -a sensei -s sensei-elevenlabs-api-key -w)" \
    "$(security find-generic-password -a sensei -s sensei-demo-token -w)" \
    | fly secrets import --app sensei-talk
  ```

  To rotate the token, run the first and last lines again. Setting a secret
  restarts the machine, and the old token stops working.
- **Spend:** $1.00 per server run, and a **$5.00 daily ceiling** across every
  session and run. The daily counter lives on a 1 GB volume (`/data`), so a
  restart or redeploy can't reset it; it rolls over at 00:00 UTC.
- **Abuse limits** (`sensei_demo/limits.py`, configurable):
  - 60 new sessions per client IP per hour, and 40 utterances per client IP
    per 10 minutes;
  - 8 simultaneous sessions. A session idle for 15 minutes stops counting,
    and switching scenario cards frees the visitor's own previous session.
  - The client IP comes from Fly's `Fly-Client-IP` header, trusted only
    because the server is configured to (`--trusted-ip-header`); by default no
    proxy header is trusted.
- **Visitors' transcripts:** traces go to the machine's ephemeral disk
  (`/tmp`) and are gone when it stops. Nothing but the spend counter is kept
  on the volume, and no audio is ever written.
- **Idle cost:** the machine auto-stops when idle, and the next visitor starts
  it again (a cold start of a few seconds).

Deploy from the repository root:

```
fly deploy . --config packages/demo/fly.toml --dockerfile packages/demo/Dockerfile --ha=false
```

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

`tests/` runs fully offline. Speech providers are mocked, a fixture fails
any test that opens a socket, and another points every default state path at
a temporary directory, so no test writes under `~/.sensei`. The tests cover:
- the endpoints in both modes: auth on every data endpoint, the public shell,
  mode refusals, bounds, the spend cap, and provider failures;
- that no key or audio reaches a response, the stream or the trace;
- the ledger stream against scripted sessions. Events arrive in trace order,
  a reconnecting stream resumes where it stopped, and a followed stream
  delivers events while the turn runs. An irreversible `tool_executed` never
  precedes its action's echo and confirmation, and the pay decline shows a
  decline and no execution;
- the limits, with an injected clock and date: the daily ceiling persists
  across restarts and rolls over at the UTC date; per-IP windows; an untrusted
  proxy header is ignored; the session cap, idle expiry, and freeing only your
  own session; token rotation; and the hosted entry point reading env keys
  and never the Keychain.

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

## Verified over the public URL (2026-11-26)

All three scenarios ran in Google Chrome against https://sensei-talk.fly.dev in
LIVE SPEECH mode, with the same substitute microphone as in Slice 1:
- **Morning brief:** heard at 1.00, gate allowed, executed, verified.
- **Send a note to Alex:** echoed, "Yes." heard (0.98), approved, executed
  once, verified.
- **Pay an invoice:** "Pay invoice 204" heard (0.96) and echoed as Acme Paper,
  $1,250.00. "No." was heard (0.95), the ledger showed "Declined: nothing will
  run", no `tool_executed` event appeared, and verification passed.
- **Time to first audio, public path:** 431–715 ms server-side per turn. In
  the page, playback started 496–839 ms after the button was released, the
  full round trip from the browser to Fly to the providers and back.

## Not yet built

- A custom domain, and bring-your-own-key access.
- More than one instance: sessions, rate limits and the per-run cap are held
  in one machine's memory.
- Barge-in in the browser: a reply plays to the end before the next turn.
- Streaming STT and streaming playback.
- Accounts or multiple users. Sessions are in memory, at most 8.
- Real mail and calendar connectors: the demo uses the synthetic World.
- Speech calls in core's egress log (#19).

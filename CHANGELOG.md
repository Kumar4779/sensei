# Changelog

The slice-by-slice record of how Sensei was built, moved here from the README
unchanged. Each slice is one commit; `git log` has the full sequence (Slices 1–20
are recorded in their commit messages and in the SenseiBench notes in the README).

## Design-phase steps

1. **Product Definition** — vision, users, personality, scope
2. **Architecture** — system design, model router, components
3. **Threat Model** — permissions, sandboxing, abuse and failure cases
4. **Eval Design** — SenseiBench, designed before code
5. **Design Review Gate** — sign-off required before implementation begins

See [DESIGN.md](DESIGN.md) for the design document and [STANDARDS.md](STANDARDS.md) for the engineering standards every component must meet.

## Custodian: Sensei's memory over MCP (Slice 21)

Sensei's memory store, shared with any MCP client (Claude Code included), with
its gates intact. Run it as a local stdio server; there is no network listener:

```
cd packages/core && python -m sensei_core.custodian_server serve [--root DIR]
```

- **Four tools:**
  - `memory_search(query)`: concept search over **active** entries, returning
    ids and short summaries.
  - `memory_get(id)`: one full entry, with its provenance.
  - `memory_propose(text, category)`: a new entry, stored as pending.
  - `memory_list_pending()`: entries awaiting confirmation.
- **No client can activate anything.** A proposal is stored through
  `MemoryStore.propose`, with `source=tool_content` and
  `origin="mcp:<client name>"`. It becomes active only when the user confirms
  it Sensei-side (`python -m sensei_core.custodian_server confirm <id>`).
  Confirming is deliberately not an MCP tool. The `MemoryStore` file format
  and its semantics are unchanged.
- **Embeddings are a derived layer.** They are computed locally with
  `nomic-embed-text` via Ollama (`[embeddings]` in `models.toml`) and kept in
  `<root>/derived/embeddings.json`. The index holds vectors only, never fact
  text. It is re-synced from the entry files before every search and propose,
  so deleting it and rebuilding reproduces it, and a deleted entry leaves
  search at once. If Ollama is unavailable, search falls back to keyword
  matching and says so (`mode: keyword`).
- **Dedup flags, never drops.** A proposal close to an active entry is still
  stored, with `possible_duplicate_of: <id>` appended to its origin.
- **Every call is traced** (`mcp_tool_call`): client, operation, entry ids and
  counts, never entry text.
- **Live check (2026-11-25):** a real MCP client (the official Python SDK,
  over stdio) worked against a scratch store with real embeddings.
  - It proposed "The user prefers aisle seats on flights." The entry came back
    `proposed` and appeared in `memory_list_pending`.
  - It proposed "User has a peanut allergy." That entry was stored and flagged
    as a possible duplicate of the user's active "The user is allergic to
    peanuts." (similarity 0.894).
  - After a Sensei-side `confirm`, the search "seating preference when flying"
    returned the aisle-seat entry (score 0.792), with no shared keywords. An
    unrelated query ("favourite football team") returned nothing.
  - Claude Code itself was not connected in this check.

## Web UI: Sensei on your phone (Slice 22)

A phone-first web page served by the gateway itself: the same FastAPI app and
the same token. It has no build step and no framework, just three static files
in `packages/core/sensei_core/ui/`.

```
cd packages/core
SENSEI_GATEWAY_TOKEN=<long random token> \
SENSEI_BRIEF_FIXTURE=../../evals/tasks/morning-brief-001/fixture \
  python -m sensei_core.gateway          # then open http://127.0.0.1:8765/ui
```

- **What it shows:**
  - **Morning brief:** today's brief, marked **Verified** or **Unverified**
    (with the failed checks) from the workflow's own verification.
  - **Approvals:** pending memory entries, including those proposed through the
    Custodian, with Confirm (`POST /memory/confirm`) and Reject
    (`DELETE /memory/{id}`, a permanent delete, so the UI asks first).
  - **Today's spend:** per route, from the cost ledger. These are estimates,
    for the UTC day.
  - **A Kill / Revive switch** (`/kill`, `/revive`).
- **The brief's data is synthetic for now.** There is no mail or calendar
  connector yet; `SENSEI_BRIEF_FIXTURE` points the brief at a fixture
  directory. Without it, the brief endpoints say so (HTTP 409).
- **Auth:** `/ui` itself is a public page that only asks for the token; it
  holds no data. The token is kept in `sessionStorage`, so it is gone when the
  tab closes. The app files (`/ui/app/*`) and every data endpoint require
  `X-Sensei-Token`.

### Reaching it from the iPhone

The gateway binds **127.0.0.1 only**, by default and always unless you opt in.
There are two supported paths, and there is no public exposure in either.

1. **Tailscale (recommended).** Install Tailscale on the Mac and the iPhone, on
   the same tailnet. Keep the gateway on localhost and publish it to your
   tailnet only:
   ```
   tailscale serve --bg 8765
   ```
   Then open `https://<mac-name>.<tailnet>.ts.net/ui` on the iPhone. Traffic is
   encrypted, and only your own tailnet devices can reach the gateway.
   `tailscale serve reset` withdraws it.
2. **Same Wi-Fi, explicit opt-in.** `SENSEI_GATEWAY_ALLOW_LAN=1` (exactly `1`)
   makes the gateway bind all interfaces, and it logs a warning at start-up.
   Open `http://<mac-lan-ip>:8765/ui`. **This widens the security boundary:**
   every device on that network can reach the gateway, which is then protected
   only by its token, and the token crosses the Wi-Fi in plain HTTP. Use it
   only on a network you trust, and only while you need it. Never combine it
   with port forwarding.

The native iOS app (TestFlight), the Watch companion and an App Store release
are later slices.

### Live check: complete (2026-11-25)

Each step below counts only because the gateway's own access log and trace
show it. There was one gateway listening on 127.0.0.1:8765, a scratch Sensei
home, the morning-brief-001 fixture, and a pending entry proposed through the
Custodian MCP server.

- **Mac (Chrome, http://127.0.0.1:8765/):**
  - The browser loaded the app with the token, and the brief was shown
    Verified (`brief_generated`, `"verified": true`).
  - The pending entry was confirmed from the UI (`memory_confirmed`), and the
    entry on disk became `active`.
  - Kill was tapped (`kill_switch_engaged`), after which `POST /run` returned
    503 (`gateway_run_refused`).
- **iPhone (Safari, over Tailscale):** published with `tailscale serve --bg 8765`
  (tailnet only), with the gateway still bound to localhost.
  - Requests arrived from the iPhone's tailnet address: login, app load, brief,
    approvals list and costs.
  - Revive was tapped on the phone (`kill_switch_disengaged`).
  - Serve was withdrawn afterwards.
- **Lesson for future checks:** two reported steps never reached the gateway.
  In the first session no browser request arrived at all. In this session one
  revive came from the Mac's own tailnet address, and the phone's first
  attempts came before its Tailscale tunnel had ever connected (zero bytes, no
  handshake). Check the trace, not the screen. A unique query string
  (`/health?probe=…`) proves which device a request came from.

## Pre-publish hardening (Slice 23)

- **Egress log.** Every model call on an API route (`cheap_api`,
  `frontier_api`) appends one line to `<SENSEI_HOME>/egress.jsonl`.
  - Each line records the timestamp, route, model, destination host, request
    and response bytes, and a SHA-256 digest of the outbound prompt.
  - The content itself is never written. Failed calls are logged too
    (`ok: false`), because the request may already have left the machine.
  - Local routes write nothing.
  - `/costs` gains an `egress` summary for today: calls and bytes per
    destination.
  - No real API client exists yet, so "destination host" is the client's
    declared `destination_host`, or else the route's provider name.
- **No silent fallback.** When an API call fails, the run retries or escalates
  only by the Router's existing rules: the same route, or `frontier_api` for
  tasks that earn it. Otherwise the run fails.
  - The executor refuses any other next route, so the local model and other
    providers are never used as silent stand-ins.
  - Every failure is traced as `route_failure` (route, model, error class).
  - LOCAL_ONLY behaviour is unchanged.
- **Ground-truth isolation.** A bench workflow receives only a `TrialInput`: a
  sandboxed **copy** of the fixture, plus the instruction, sensitivity and
  budget. It runs with that sandbox as its working directory.
  - It never receives the task, its `checker.py`, expected values or the
    results directory.
  - Grading happens afterwards, in the runner. This closed real leaks; see the
    Slice 23 commit.
  - This is isolation by construction, not an OS sandbox. Code running in the
    same process could still open an absolute path it already knew.

## Manifest integrity (Slice 24)

The agent cannot rewrite its own rules. `tools.toml` (the permission gate) and
`models.toml` (the router) are pinned by SHA-256 when the executor is built.
The gateway's startup goes through that, and the digests (never the contents)
are traced as `manifests_loaded`.

- **Before every run,** both files are re-read and re-checked.
  - If either has changed or been deleted, the run is refused and traced as
    `manifest_tamper_detected`, naming the file.
  - The gateway answers HTTP 500 with `"error": "manifest_tamper_detected"`,
    and refuses to start on a mismatch.
  - The bench and the CLI raise `ManifestTamperError` rather than record a
    failed trial.
- **There is no reload path.** Pins are process-wide: the manifest loaders,
  and any new guard, refuse a pinned file whose bytes changed. To adopt an
  intended edit, restart.
- **Loose permissions get a warning, not a refusal (v1).** A group- or
  world-writable manifest is traced as `manifest_permissions_loose` at
  startup. The shipped files are `644`.

## Composition demo (Slice 25)

One scripted run through the real components, end to end:

```
packages/core/.venv/bin/python scripts/composition_demo.py [--port 8765] [--wait-minutes 15]
```

1. Start the gateway (scratch Sensei home, fresh token).
2. Propose a memory entry through the Custodian MCP server.
3. Run the brief with `POST /brief/run` on the morning-brief-001 fixture.
4. Run the MacPilot leg.
5. Publish the UI to the tailnet with `tailscale serve`, and wait for the
   entry to be confirmed from the iPhone.
6. Test the kill switch: engage it, check that `/run` returns 503, then revive.

The script then prints a summary in which **every claim cites the trace event
(or access-log line) that proves it**. Anything unproven is printed as NOT
DONE. "Confirmed from the iPhone" needs both `memory_confirmed` and the
iPhone's tailnet address on `POST /memory/confirm`. Records are kept in the
scratch home.

- **MacPilot today:** `packages/macpilot` holds only its README. It has no
  browser task and no perception step, so the demo reports that leg as NOT
  DONE rather than faking it.
- **First run (2026-11-25): 4 of 6 claims proven.**
  - Proven: the Custodian proposal (`mcp_tool_call` `memory_propose`), the
    verified brief (`brief_generated`, `verified: true`), egress (0 API calls;
    every call was local), and the kill switch (`kill_switch_engaged` →
    `gateway_run_refused` → `kill_switch_disengaged`).
  - Not done: the MacPilot leg (no code yet).
  - Not done: the phone approval. The entry was confirmed (`memory_confirmed`),
    but the request came from the Mac's own tailnet address, not the iPhone's,
    so the demo did not count it.

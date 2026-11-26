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

- **The MacPilot leg** runs MacPilot's own Slice 1 demo task
  (`python -m macpilot.demo`, in MacPilot's virtualenv, with a scratch browser
  profile). It counts only if MacPilot's own audit log records `goal_reached`.
- **Latest run (2026-11-25, US Central; 2026-11-26 UTC): 6 of 6 claims proven.**
  - **Custodian proposal:** `mcp_tool_call` `memory_propose` (status
    `proposed`) in the Custodian trace.
  - **Brief verified:** `brief_generated` with `verified: true`, 1 attempt and
    no failed checks.
  - **MacPilot:** the audit log records the click at normalized (500, 500),
    grounded to (640, 360) on a 1280×720 viewport, with `outcome:
    goal_reached` for the stop condition "the page shows 'Clicked'".
  - **Phone approval:** `memory_confirmed` for the proposed entry, and
    `POST /memory/confirm` in the access log from the iPhone's tailnet
    address. The access log has no requests from the Mac's tailnet address;
    the script's own calls came from 127.0.0.1.
  - **Egress:** 0 API calls and 0 bytes. Every model call was local.
  - **Kill switch:** `kill_switch_engaged`, then `gateway_run_refused` (HTTP
    503), then `kill_switch_disengaged`.
- **Earlier runs:**
  - The first run (2026-11-25) proved 4 of 6. MacPilot had no code yet, and
    the confirm came from the Mac's tailnet address, not the iPhone's.
  - A second run proved 5 of 6. MacPilot reached its goal, but no confirm
    arrived within the 20-minute wait.

## Research adoptions (Slice 28)

Docs only; no code changed.

- **Corrections go down** ([STANDARDS.md](STANDARDS.md)). A fix lands at the
  highest layer that can hold it:
  1. architecture;
  2. a test or lint rule that CI enforces;
  3. a skill;
  4. a prompt, last and weakest.

  A prompt-only fix must say why no higher layer could hold it. The worked
  example is SenseiBench probe v3. Slices 16–19 added four prompt rules, and
  all four configurations scored pass^1 0.00. Slices 20 and 20.1 moved the fix
  into architecture (a code-assembled inbox and scoped prose), reaching
  pass^1 1.00.
- **A published trust ladder** ([TRUST_LADDER.md](TRUST_LADDER.md)). Five
  rungs, from "human does it" to "agent acts, human reviews after". For each
  capability Sensei has today, it states the rung, the machine-run checks,
  what still needs the human, and the evidence that would move it up:
  - morning briefs: rung 4, synthetic fixtures only;
  - memory proposals: rung 2, by design;
  - MacPilot: rung 0, a capability test only;
  - code via MergeJudge: rung 2, the human approves the winner. Auto-merge is
    not adopted: test-pass is not mergeability, and MergeJudge's mergeability
    judge did not calibrate (2/10 twice against an 8/10 bar).
- **Threat 6 industry note** ([DESIGN.md](DESIGN.md) §3). Anthropic's Managed
  Agents rebuild reached the same credential design: credentials are injected
  at egress and never held by the agent.

## Threat 7 completion (Slice 29)

Runaway loops and cost (DESIGN.md §3, Threat 7): the kill switch persists,
spend caps are enforced, and loops are detected.

- **The kill switch persists** (`killswitch.py`).
  - **The killed state is a file,** `<SENSEI_HOME>/killed.json`. `POST /kill`
    writes it atomically, and `POST /revive` removes it.
  - **It is re-read on every request,** for `/run`, `/brief/run` and
    `/health`. A gateway started while the file exists starts KILLED, and
    says so in a log warning.
  - **Every model call checks it, local routes included,** so a multi-call run
    such as a brief's draft and repair stops at its next call after a kill.
    A run already inside a model call still finishes that call.
  - **It fails closed.** A `killed.json` that exists but cannot be read, is not
    JSON, or is anything other than `{"killed": true, …}` counts as killed and
    traces `kill_state_unreadable`. Only a missing file means "not killed".
  - The `kill_switch_engaged` and `kill_switch_disengaged` events are
    unchanged.
- **Spend caps** (`[budgets]` in `models.toml`: `daily_cap_usd`,
  `monthly_cap_usd`).
  - **They are estimated USD, on the same basis as the CostLedger.** Today's
    and this month's spend are read from the trace (UTC day and month).
  - **The check comes before every API-route call.** If the call's estimated
    input cost would push spend past a cap, it is refused before anything is
    sent: `SpendCapReached` (a `BudgetExceeded`), traced as
    `spend_cap_reached` with the cap type. The gateway returns HTTP 402.
  - **Local routes are exempt from the caps,** since they cost nothing, but
    not from the kill switch.
  - **No cap unless set.** The shipped table sets none, so behaviour,
    existing tests and the bench are unchanged. An unknown key, such as a
    misspelt cap, is a config error, not silently "no cap".
- **Spend alerts.** After an API call, if today's spend has reached 50% or 80%
  of the daily cap, the executor traces `spend_alert` with the percentage. Each
  threshold fires once per UTC day, and the trace records which have fired, so
  a restart does not send them again.
- **Loop detection.** A run is `Executor.run_scope()`: `run_once` opens one,
  and `build_brief` wraps its draft and repair in one.
  - **What halts a run:** if the run asks for the same call (same route, same
    prompt SHA-256, the egress digest) after it has already succeeded twice,
    the third call is not sent. The run halts with `LoopDetected`, traced as
    `loop_detected` (route, digest, count; never the prompt). The gateway
    returns HTTP 508.
  - **Only successful calls count,** so retries of a failed call are exempt.
- **Not yet done:**
  - DESIGN.md's Threat 7 asks for *near-identical* calls and "no measurable
    progress". This slice detects only exact repeats.
  - Alerts reach the trace, not yet the phone or Watch.
  - There are no step budgets yet.
  - The caps re-read the trace on each API call, which is fine at today's
    trace sizes.

## Shadow router (Slice 30)

Shadow verification, applied to routing: a new policy earns trust from a
measured agreement record before it is ever allowed to decide.

- **The policy, `cheapest-eligible-v1`** (`shadow.py`). It takes the same
  inputs the Router sees: sensitivity, difficulty, prior failures and prompt
  size.
  1. **Eligible routes are those the rules allow,** and the shadow inherits
     every hard guardrail:
     - The route's sensitivity list must contain the class: the Router's
       own `RoutingError` check.
     - LOCAL_ONLY additionally requires a local-tier route.
     - Hard API_OK work is frontier-tier only.
     - After a failure, only the rules' route or `frontier_api` (no silent
       fallback, Slice 23).
  2. **Among those, it picks the lowest estimated input cost,** using
     `est_cost_per_1k_tokens` from `models.toml`: the field the rules Router
     ignores. Ties go to the rules' route, then to the route name.
- **It decides on every run and never executes.** After the Router's first
  decision in each Executor run, the shadow decides too. The executor traces
  one `shadow_decision`:
  - policy and task family;
  - the inputs (sensitivity, difficulty, prior failures, prompt size);
  - `rules_route`, `shadow_route` and `agree`;
  - the eligible set, and each route's estimated input cost.

  The shadow's choice is never called and makes no model call. A shadow error
  is traced as `shadow_error` and never reaches the real run.
- **The report:** `python -m sensei_core.shadow_report [PATH ...]`. It is
  read-only; the default is every trace under `$SENSEI_HOME/traces`. It
  prints, per task family and overall:
  - runs;
  - the agreement rate;
  - the summed estimated cost difference had the shadow decided (shadow minus
    rules).
- **The bench is now traced.** `python -m sensei_core.bench` writes
  `$SENSEI_HOME/traces/bench.jsonl`, where it had no trace before. Each trial
  runs in a scope named `<task id>@v<version>`, so its shadow decisions carry
  the task family. Grading, tasks and checkers are unchanged.
- **What the record will show first:** every SenseiBench task today is
  LOCAL_ONLY, where the only eligible route is local. The bench record will
  therefore read 100% agreement and $0 difference by construction; it measures
  nothing yet about cost. The shadow disagrees with the rules only on API_OK
  simple and standard work, where it picks the free local route over
  `cheap_api`. Whether local is good enough there is the open question this
  record is meant to answer, once API_OK tasks exist.

## Crash-safe resume and deterministic replay (Slice 31)

- **Run records** (`runs.py`).
  - **What a run is:** an Executor run scope. That is one `run_once`, or a
    workflow such as the brief's draft and repair.
  - **What it writes:** `<SENSEI_HOME>/runs/<run_id>.json`, holding the run
    id, task family, the workflow that can re-execute it, ordered steps
    (route, model, prompt SHA-256, estimated cost, outcome), the verifier
    reports, and the final status (`completed`, `failed` or `killed`).
  - **When it writes:** each step is written `in_flight` before its model call
    and updated after it, and every write is atomic. A process that dies
    therefore leaves the interrupted step as the record's last.
  - **Crashed is inferred:** a record still `in_progress` that another process
    wrote.
  - **What is stored:** prompts never, only digests. Responses are kept only
    for resumable runs, so a resume can reuse them.
  - **In the trace:** every run scope is now bracketed by `run_opened` (run
    id, family, resumed, the route models and spend caps in force) and
    `run_closed` (status, error). `run_started` and `model_call` carry the
    prompt digest.
- **Resume:** `python -m sensei_core.resume <run_id>`, or the gateway's
  `POST /runs/<id>/resume`.
  - **How it works:** the run's workflow is re-executed from its recorded
    params, and each recorded step (same route, same prompt digest) is served
    from the record instead of calling the model. Completed steps never run
    twice. The interrupted step is marked `interrupted` and runs again live.
  - **On the record:** new steps carry `resumed: true`, and the record gains a
    `resumes` entry.
  - **Divergence:** a resumed run whose re-derived call differs from the
    recorded one stops with `ResumeDiverged`, rather than guessing.
  - **Clean errors, not re-runs:** resuming a completed or failed run, a run
    still live in this process, or a run without a workflow.
  - **The same gates as a new run:** the kill switch and the manifest guard
    apply at the endpoint and on every model call.
  - **What can resume today:** the gateway brief is the resumable workflow
    (`workflows.py`). A plain `/run` prompt is never stored, so those runs are
    recorded but not resumable.
- **Replay:** `python -m sensei_core.replay <trace_file>`. It makes no model
  call and writes nothing.
  - **What it re-derives,** from the trace's own inputs, using the same code
    that decided:
    - route choices, retries and escalations, and shadow decisions;
    - budget, spend-cap and loop halts, and spend alerts;
    - each model call's token and cost estimates;
    - verifier verdicts (all checks must pass);
    - run completions, repairs, brief summaries, and each run's close status.
  - **What it reports:** MATCH, or the first divergence, with its line,
    decision number, and each field as re-derived and as recorded.
  - **Crashes:** a process that died mid-run shows as a crashed run, not a
    divergence.
  - **Config:** if `models.toml` differs from the digest the trace recorded,
    replay says so.
- **Verified live:**
  - **A real brief** on `qwen3:8b` replays to MATCH, and resuming that
    completed run is refused.
  - **A real crash:** the gateway was killed with `kill -9` while a step was in
    flight. The resume ran only that step again, completed verified, and the
    whole trace (crash plus resume) replays to MATCH.
- **Limits:**
  - **Replay outcomes are outside facts.** Whether a call succeeded, a reply's
    length and each check's result are taken as recorded. Replay proves the
    decisions follow from them, not that they were true.
  - **Interleaved runs aren't separated.** Concurrent gateway requests
    interleave runs in one trace, and replay does not separate them yet.
  - **An interrupted API call is sent again on resume.** It may already have
    left the machine.

## Containment self-probe (Slice 32)

Sensei tests the parts of its safety posture it can test, at startup, and
records the results, failures included.

- **The probe:** `containment.py`, run at gateway startup and standalone as
  `python -m sensei_core.containment [--strict]`. Each check returns pass,
  warn or fail, with one line of evidence:
  - **manifests:** `tools.toml` and `models.toml` are not writable by group or
    other. World-writable fails; group-writable warns. This is Slice 24's
    warning promoted to a result.
  - **ground_truth:** fresh trial sandboxes are built with the bench's own
    `prepare_trial`, and the Slice 23 hostile probe is re-run from each one:
    the sandbox tree (including copies of a checker under another name), the
    working directory's parents, known relative paths, and the environment. A
    pass says what it is: isolation by construction, not an OS sandbox.
  - **egress:** stated, never tested by sending. It names the API routes and
    whether their clients are fakes, and always reports `egress enforcement
    not built: egress is LOGGED, NOT ENFORCED`, a warn. A test pins this
    wording, so the probe cannot start claiming enforcement that doesn't
    exist.
  - **kill_state:** `killed.json` as it reads now. An engaged switch warns; a
    present file that is not honoured fails.
  - **trace_store:** the traces directory and its files are not readable by
    group or other (Threat 4). World-readable fails; group-readable warns.
    The evidence includes the `chmod` that fixes it.
  - **token:** `SENSEI_GATEWAY_TOKEN` is set and at least 16 characters. Only
    the length is reported.
- **Where results go:**
  - one `containment_probe` trace event at startup;
  - printed when `python -m sensei_core.gateway` starts;
  - `GET /posture`, which needs the token and re-probes on request (the same
    structure).
- **Strict mode:** the default reports and starts, since on a dev machine
  posture is information. `--strict`, or `SENSEI_STRICT_POSTURE=1`, refuses to
  start on any fail; warns are allowed. Manifest tamper is governed by Slice
  24, unchanged.
- **First real result on the development Mac, 2026-11-25: 4 pass, 1 warn,
  1 fail.**
  - **Pass:** manifests (both 644), ground truth (4 tasks, nothing
    reachable), kill state (not engaged), token (32 characters).
  - **Warn:** egress (logged, not enforced; both API routes use fake clients).
  - **Fail: trace_store.** A fresh gateway creates `~/.sensei/traces` as 755
    and `gateway.jsonl` as 644 under the default umask, so traces are readable
    by any local user. The probe found a real gap in Sensei itself.
    `TraceWriter` does not yet create traces owner-only; until it does, run
    `chmod -R go-rwx ~/.sensei/traces`.

## Owner-only stores (Slice 33)

The Slice 32 probe's first finding, fixed in code. `TraceWriter` created the
traces directory and files with the process umask, so under umask 022 traces
were world-readable (Threat 4), and a fresh gateway failed `trace_store`.

- **`private.py`:** directories 0700 and files 0600, set explicitly whatever
  the umask. Files are opened with the mode and then `fchmod`'d; atomic
  replaces create the temporary file owner-only too.
- **Tightening on open:** opening a store tightens only the directories and
  files that store owns, and only if this user owns them. Parent directories
  and neighbouring files are never touched.
- **Audit of every writer under the Sensei home:**

  | Writer | Writes | Before | Now |
  |---|---|---|---|
  | `trace.py` (`TraceWriter`) | `traces/*.jsonl` | umask (755 / 644) | directory 0700, files 0600, tightened on open |
  | `egress.py` | `egress.jsonl` | umask (644) | 0600 |
  | `runs.py` | `runs/*.json` (may hold model responses) | umask | directory 0700, files 0600 |
  | `memory.py` | `memory/`, `entries/*.json`, history, changelog | umask | directories 0700, files 0600 |
  | `custodian.py` | `memory/derived/embeddings.json` | umask | directory 0700, file 0600 |
  | `brief.py`, `workflows.py` | `briefs/<day>.md`, `<day>.json` | umask | directory 0700, files 0600 |
  | `killswitch.py` | `killed.json` | umask | unchanged: state, not content |
  | `bench.py` | `evals/results/*.json` | umask | unchanged: published results in the repo, not the Sensei home |

- **Tests:**
  - The Slice 32 test that pinned the failure now asserts the opposite: a
    fresh gateway under umask 022 passes `trace_store`.
  - Every store is created under umask 000, where a file left to the umask
    would be 666, and must come out 0700 / 0600.
  - A regression guard runs a fresh home under umasks 000, 022 and 002.
- **Verified live, real gateway startups under umask 022:**
  - **A fresh home** passes `trace_store` (700 / 600): 5 pass, 1 warn, 0 fail.
  - **The real `~/.sensei`** gives the same result. The startup also tightened
    the existing `~/.sensei/memory` from 755 to 700. The Sensei home directory
    itself is left as it is.

## The probe covers every content store (Slice 34)

Slice 32's probe checked only the traces directory. Slice 33 made every content
store owner-only. The probe now verifies each store at every startup, so a
regression in any one writer is caught the way the traces one was.

- **One check per store, same severity split:**
  - Readable or writable by any local user fails; by the group, warns; owner
    only passes.
  - The checks are `trace_store` (`traces/`, unchanged), `run_store`
    (`runs/`), `memory_store` (`memory/`: entries, history, changelog,
    `derived/embeddings.json`), `brief_store` (`briefs/`) and `egress_log`
    (`egress.jsonl` itself; its parent is the Sensei home, which the probe
    does not judge).
  - Each line names the path and mode that decided it, plus the `chmod` that
    fixes it.
- **A store not created yet is skipped, not failed:** it reports
  `not_created`. The probe re-runs at every startup, so a store created loose
  later is caught then. A store a gateway was not given (for example no run
  records) reports `not_configured`. The summary counts pass, warn, fail and
  skipped separately.
- **`GET /posture`** adds a `stores` table (store, path, status, the path and
  mode that decided it, evidence), the same rows as the startup print and the
  traced `containment_probe` event.
- **The gateway probes the stores it was actually given** (its trace, run
  store, memory store, brief directory and egress log), not paths guessed
  from the home.
- **Tests:**
  - A synthetic home with each store loose in turn: exactly that store fails
    and the others pass. This covers nested files and a world-writable
    directory.
  - Group-readable and group-writable each warn.
  - A fully owner-only home passes every store, and an absent store is
    skipped.
  - Strict mode refuses startup on any one store failing and starts on a
    clean home.
  - The live regression guard: a fresh home under umask 022 runs one brief,
    one API-route run and one memory write, so every store exists. The next
    startup's probe passes every store.
  - A planted regression (run records written with the umask) fails the live
    guard and every strict-mode test, naming `run_store`.
- **Verified live, a real gateway startup on `~/.sensei` under umask 022:** 6
  pass, 1 warn (egress, not enforced), 0 fail, 3 skipped. `traces/` and
  `memory/` pass. `runs/`, `briefs/` and `egress.jsonl` have not been written
  on this machine yet, so they are `not_created`.

## First real backend: Kimi behind the gates (Slice 35, #1)

The `cheap_api` route now calls a real model. Every existing gate still applies:
sensitivity routing, no silent fallback, spend caps, the kill switch, loop
detection, run records, the egress log and the containment probe.
`frontier_api` stays a fake client, so there is deliberately one real backend.

- **`KimiClient` (`clients.py`):** the same `complete(prompt) -> str` interface
  as `OllamaClient`, standard library only.
  - It sends `POST https://api.moonshot.ai/v1/chat/completions` with
    `Authorization: Bearer <key>` and the body `{model, messages: [{role:
    user}], stream: false, max_completion_tokens: 4096, reasoning_effort:
    "low"}`.
  - It returns `choices[0].message.content` and never `reasoning_content`.
  - `temperature`, `top_p` and `n` are left out because Moonshot fixes them
    for K3-series models.
  - The default timeout is 120 s.
- **What was used, verified against Moonshot's docs on 2026-11-26:**
  - **Base URL and path:** `https://api.moonshot.ai/v1` and
    `/chat/completions`
    ([API reference](https://platform.kimi.ai/docs/api/chat),
    [quick start](https://platform.kimi.ai/docs/guide/start-using-kimi-api)).
    The `platform.moonshot.ai` docs now redirect to `platform.kimi.ai`.
  - **Model id:** `kimi-k3`, from the
    [model list](https://platform.kimi.ai/docs/models.md). The docs'
    flagship is `kimi-k3.1-pro`, but its price could not be read: the
    [pricing page](https://platform.kimi.ai/docs/pricing/chat) rendered its
    tables empty. `kimi-k3` is the one current model with a published price,
    so it is the one used; no price was guessed.
  - **Pricing:** $3.00 / MTok uncached input, $0.30 / MTok cached input,
    $15.00 / MTok output, from Moonshot's
    [K3 announcement](https://forum.moonshot.ai/t/kimi-k3-is-here-our-most-capable-model/480)
    (2026-07-22). That is Moonshot's own forum, not the docs pricing table.
  - **`est_cost_per_1k_tokens = 0.015`:** the output price applied to every
    estimated token, an upper bound on visible tokens. Hidden reasoning
    tokens are not counted; `max_completion_tokens` bounds them per call.
    Real billed usage is on the Moonshot console.
- **Credentials (Threat 6), `credentials.py`:**
  - A broker reads the key at call time from the macOS Keychain (service
    `sensei-kimi-api-key`, account `sensei`) via `security
    find-generic-password`.
  - It never reads the repo, `models.toml` or the environment, and the key is
    not kept on the client.
  - Error messages carry only the HTTP status and Moonshot's error *type*,
    never the response body (which can echo a request) and never the key. A
    test proves the key reaches no trace, egress record, run record or error,
    including a key that matches none of the trace writer's redaction
    patterns.
- **Key absent fails closed:**
  - `KimiKeyMissing` is traced as `route_failure` (`error_class:
    key_absent`) and the run stops.
  - It is not retried and not escalated, because re-routing would substitute
    another backend. This is the one new rule in `Executor`: an error marked
    `fail_closed` stops the run after it is traced.
- **Other failures keep the Slice 23 rules unchanged:**
  - The classes are `auth_failed` (401/403), `rate_limited` (429),
    `server_error` (5xx), `bad_request`, `timeout`, `unreachable` and
    `bad_response`.
  - A simple task retries the same route once; a standard task escalates to
    `frontier_api`; nothing falls back to local.
- **Config (`models.toml`):**
  - `cheap_api` is `model = "kimi-k3"` at `est_cost_per_1k_tokens = 0.015`.
  - The `frontier_api` placeholder estimate rises from 0.01 to 0.05, so the
    frontier tier stays above the real cheap price. Otherwise the shadow
    policy would prefer the fake frontier after a failure.
  - **`[budgets] daily_cap_usd = 1.00`:** a conservative cap for the first
    real-backend period. It is config, the owner's to change. The Slice 29
    spend alerts at 50% and 80% of it now have real spend to fire on.
- **The probe** now reports `cheap_api (real client), frontier_api (fake
  client)`. Egress is still LOGGED, NOT ENFORCED.
- **Tests (no network in CI):** contract tests against a stubbed HTTP layer
  cover:
  - request shape, response parsing and malformed replies;
  - each HTTP status mapping, the timeout, and unreachable;
  - the key read on every call, and key-absent failing closed before any
    request;
  - the Keychain broker;
  - escalation unchanged, the key never leaking, and the probe line.
  - The Slice 29/30/31 cap and shadow tests now take their amounts from the
    shipped rate instead of a hard-coded $0.001.
- **First live attempt (2026-11-26), one `API_OK` `/run` through the gateway
  on `~/.sensei`, before the account had a balance:**
  - **The result:** Moonshot returned **HTTP 429
    `exceeded_current_quota_error`**. The key was accepted (no 401), but the
    account has no quota or balance, so no completion came back.
  - **The route and traces:** the route was `cheap_api` (`kimi-k3`). The
    trace shows `route_failure` (`rate_limited`), `run_retried` on the same
    route, a second `route_failure`, then `run_failed`; there was no
    escalation for a simple task.
  - **The first real `egress.jsonl` entries (two, one per attempt):**
    `destination_host: api.moonshot.ai`, `request_bytes: 53`,
    `response_bytes: 0`, `ok: false`, `prompt_sha256:
    sha256:ea4c8a15…9f039b`. The file is mode 600.
  - **Cap state:** $0.00 of the $1.00 daily cap spent; nothing was billed or
    estimated.
  - **`shadow_report`:** 1 run, 0 agree. The shadow picked `local_private`,
    the rules `cheap_api`. That is the first real API-route decision, but not
    yet agreement data on a completed call.
  - **The key** was in none of the files the run wrote.
  - **Re-run** after the owner added a balance (below).
- **Live check completed (2026-11-26):** one `API_OK` / `simple` `/run`
  through the gateway on `~/.sensei`, with the real Keychain key and no code
  changes.
  - **The run:** route `cheap_api` (`kimi-k3`); status `completed` in
    2,982 ms. It was not escalated or retried.
  - **The answer** was a real completion from `choices[0].message.content`
    (31 characters), not reasoning text.
  - **The egress entry:** `destination_host: api.moonshot.ai`, `ok: true`,
    `request_bytes: 53`, `response_bytes: 31`, `prompt_sha256:
    sha256:ea4c8a15…9f039b`. That is the digest only, with no content.
  - **Cap state:** about $0.00033 estimated of the $1.00 daily cap (0.03%),
    so no spend alert fired.
  - **`shadow_report`:** 2 runs, 0 agree (0.0%), est diff -$0.000420. This
    run is the first completed API-route call in the agreement record. The
    rules picked `cheap_api`; the shadow picked `local_private`.
  - **The key** was in none of the files the run wrote.
  - **The `/run` response shows `verified: false`** because `/run` passes no
    checks; it does not mean the completion was rejected.
  - **Issue #1 is closed:** every acceptance criterion is met.

## A quota-exhausted 429 fails closed (Slice 36, #2)

In Slice 35's first live attempt, Moonshot returned HTTP 429
`exceeded_current_quota_error` and the Executor retried once on the same route.
That retry cannot succeed while the account has no balance. A rate limit is a
burst, and worth retrying; an exhausted quota is a state, so a retry only wastes
a call.

- **`KimiClient` tells the two 429s apart** by Moonshot's `error.type`:
  - `exceeded_current_quota_error` raises `KimiQuotaExhausted`, a fail-closed
    error (the Slice 35 marker) with `error_class: quota_exhausted`. The
    Executor traces `route_failure` and stops the run, with no retry and no
    escalation.
  - Every other 429, typed or not, is still `rate_limited` and keeps the
    retry-then-escalate rules, unchanged.
- **The stop is recorded as a fact:**
  - A fail-closed `route_failure` now carries `fail_closed: true` beside its
    `error_class`. Replay applies the Executor's rule to that fact: a
    fail-closed failure means `run_failed` next.
  - This also closes a Slice 35 gap. A key-absent run (the other fail-closed
    case) stopped without a retry, but nothing in its trace said why, so
    replay would have expected a retry and reported a divergence.
  - Traces without the field (every one before this slice) replay as before.
    The real `~/.sensei` trace, including Slice 35's live 429 run, still
    replays to MATCH.
- **Tests:**
  - The client separates a quota 429 from rate-limit, untyped and bodiless
    429s.
  - A stubbed quota 429 makes exactly one client call and fails the run, for
    simple and standard tasks alike, even when a second call would succeed;
    the run record is `failed`.
  - A stubbed rate-limit 429 still retries (simple) and escalates (standard)
    exactly as before.
  - A trace with a quota stop, a rate-limit retry, a rate-limit escalation
    and a key-absent stop replays to MATCH. With the `fail_closed` fact
    removed, replay diverges at the decision it would then re-derive.
  - Planted regressions are caught: dropping the quota branch fails 4 tests,
    and dropping the replay rule fails the replay test.

## The first API_OK bench task: doc-summary on Kimi (Slice 37, #3)

Every earlier SenseiBench task is LOCAL_ONLY, so the bench had never measured
the real backend. `doc-summary-001` is API_OK by design and graded the same
way as every other task: final state, never transcripts.

- **The `doc_summary` workflow (`doc_summary.py`):** from a source document it
  writes `summary.json` with `title`, `key_points` (a list), `entities` and a
  one-sentence `abstract`.
  - Verify and repair work as in the brief: one draft, the workflow's own
    checks, and at most one repair from the failed check labels.
  - Its checks come from the input alone: the shape, entities grounded in the
    main article, one sentence, and no distinctive term from the section the
    source marks as unrelated.
  - Requests are API_OK and "simple", so they go to `cheap_api`. A failure
    retries the same route and never escalates to the fake frontier client.
- **The task package, `evals/tasks/doc-summary-001/`:**
  - **Fixture:** a frozen synthetic article of 579 words, with unambiguous
    facts and a distractor section marked as unrelated.
  - **`task.json`:** declares `"workflow": "doc_summary"` and
    `"sensitivity": "API_OK"`.
  - **`checker.py`:** deterministic checks only: the required fields, the key
    facts verbatim in the right fields, and no distractor facts.
  - **Ground truth** is in `expected.json` beside the checker, outside
    `fixture/`.
- **The bench dispatches by task metadata.** A task that names a `workflow`
  gets that one, and an unknown name is refused when the task loads. The four
  frozen brief packages name none and dispatch by id as before; their files
  are untouched.
  - Failed trials' outputs keep their own file name as artifacts
    (`summary.json`, not always `brief.md`).
- **The bench CLI now runs the real gates on `cheap_api`:** `KimiClient`
  (Slice 35) instead of a fake, the shipped `[budgets]` caps, and the egress
  log. LOCAL_ONLY tasks never reach that route, so brief runs are unchanged.
  An API_OK run records `cheap_api.model`, `reasoning_effort` and
  `max_completion_tokens` in its settings.
- **Ground truth stays isolated (Slice 23):** a trial's sandbox holds only
  `fixture/source.md`. The hostile probe is unchanged and passes over all five
  tasks.
- **Tests:**
  - The checker passes the gold summary. It fails empty, invalid and missing
    output, a distractor leak in each field, a fact in the wrong field, and a
    two-sentence abstract.
  - The workflow's own checks, the repair (exactly one) and its prompt (labels
    only, no grading material) are covered.
  - Dispatch is tested: doc-summary goes to `doc_summary`, every brief task to
    the brief workflow, and an unknown name is refused.
  - A stubbed bench run is graded by the subprocess checker under the family
    `doc-summary-001@v1`. A failed trial keeps its artifact, and the sandbox
    contents are checked.
- **The bench run (2026-11-26, dev claim, k=3, real `cheap_api` / `kimi-k3`,
  `reasoning_effort=low`, Keychain key, $1.00 daily cap in force):**
  - **Estimated before running:** about $0.019 per call, so about $0.06 for
    three trials (about $0.12 if every trial repaired).
  - **pass^1 1.00, pass@3 1.00, pass^3 1.00.** Every trial passed on its
    first draft, with no repairs.
  - **Latency:** p50 15.1 s, p95 15.8 s.
  - **Cost:** $0.0205 estimated per trial. Today's estimated API spend is
    $0.0618 of the $1.00 cap, so no spend alert fired.
  - **Egress:** 3 records to `api.moonshot.ai`, `ok: true`, 4,095 bytes out
    and 1,179–1,523 bytes back each, as digests only.
  - **Results:** `evals/results/doc-summary-001-v1-20261126.json`.
- **Shadow report for `doc-summary-001@v1`:** 3 runs, 0 agree (0.0%), est diff
  -$0.046080.
  - The rules router sent every run to `cheap_api`. The cost-aware shadow
    policy would have sent all three to `local_private`, because it is $0.00
    and eligible for API_OK content.
  - That is the first bench-scale disagreement on a real API route. Whether
    the local model would also pass this task is unmeasured; a local run of
    `doc-summary-001` would answer it.

## Permission-aware retrieval: corpus, ACL pre-filter, flat answering (GraphRAG Slice 1, #5)

Flagship 4, the Permission-Aware GraphRAG Verifier, starts as
`packages/graphrag`, Sensei's knowledge module (commit 8819484). The governing
spec is `specs/graphrag-verifier.md`, committed in 5995ed1.

- **Corpus v1, frozen.** A fictional company is generated from a fixed seed
  (`20261128`): 180 documents, 5 groups and 3 tiers (public 56, internal 69,
  restricted 55), with an explicit ACL on every document.
  - 10 canary documents (2 per group) each hold a unique fake secret, so a
    leak is a string match.
  - The generator scales to ~500 documents without changes, and the committed
    corpus regenerates byte for byte on Python 3.11, 3.12 and 3.13.
  - Ground truth (canaries and probe questions) lives in `evals/v1/`, apart
    from the corpus, and the answering code never reads it.
- **Chunks carry their labels.** All 240 chunks carry `doc_id`, tier, ACL
  groups and a canary flag.
- **The ACL filter is inside the retrieval query.** Retrieval partitions the
  chunks by the requester's groups before any scoring. BM25 (with IDF over the
  permitted partition only), dense scoring and fusion never see a chunk the
  requester can't read. It is a pre-filter in code, not a post-filter or a
  prompt.
- **Flat answering.** A CLI answers extractively: every claim is a retrieved
  sentence cited by chunk ID, and it abstains when no permitted sentence
  supports the question.
- **Tests:**
  - Gate tests are asserted on the retrieved chunk set. For every canary,
    every unauthorized role and four attack shapes, at k=5 and k=50, no chunk
    outside the role's access is retrieved.
  - Positive controls: all 253 entitled (probe, role) pairs retrieve their
    document.
  - Smoke test: as Sales, an HR canary attacked by direct ask, paraphrase and
    role-play retrieves 0 HR chunks and abstains each time, and an entitled
    Sales question is answered with a correct citation.
  - Disabling the gate fails 10 tests. 35 pass, with 1 strict xfail.
- **A known failure, published as that strict xfail.** A near-miss (a common
  region name that the lexical support rule can't tell apart) can be answered
  with another region's policy. It is a correctness failure, not a leak, and
  is assigned to the verifier slice.

## Voice Lab: package scaffold, task set v1, confirmation-echo gate (Voice Lab Slice 1, #7)

Flagship 5, the Voice Agent Reliability Lab, starts as `packages/voice`,
Sensei's voice module (commit 5adf4df). The governing spec is
`specs/voice-lab.md`, committed in 0322b13. Input is transcript-first, with
fake STT/TTS adapters: no audio, no API keys, no network.

- **Task set `voice-bench-v1`, frozen.** 20 scripted tasks (10 positive
  controls, 10 safety cases) with deterministic end states checked in code.
  The version is recorded in the data, and a test pins its SHA-256.
- **The confirmation-echo state machine.** Irreversible tools (send / pay /
  delete / submit / approve) move `proposed → echoed → confirmed → executed`:
  - the echo freezes the exact parameters;
  - a confirmation must match them exactly;
  - execution redeems a single-use receipt inside the tool layer;
  - a second yes is a traced no-op.
- **No second gate.** Core's `PermissionGate` decides every call from a voice
  manifest in core's format. For irreversible tools it returns
  `HALT_FOR_APPROVAL`, and the echo resolves it. Traces use core's
  `TraceWriter`, one `voice_turn` record per turn, and audio is never stored.
- **The action-safety hard gate is checked from the trace** in code, with no
  model.
- **Tests: 58**, including the spec's smoke tests (a)–(e). Bypassing the echo
  fails 24 of them.

## Voice Lab: interruption, eval harness v1, the echo ablation (Voice Lab Slice 2, #9)

Commit cf7f608.

- **Interruption.** Barge-in is a traced event with its timestamp, point
  (mid-answer / mid-echo / mid-listing) and the segments heard. The agent
  stops, never speaks the unheard rest, and re-plans.
- **The stale-plan guard, in code.** `ConfirmationMachine.interrupt()` makes
  every in-flight echo or confirmation stale. Confirming or redeeming one is a
  hard-gate violation, and the verifier fails any trace that confirms or
  executes across a barge-in. A yes spoken over an echo re-issues the echo.
- **A new frozen probe set, `voice-interrupt-v1`:** 9 tasks across the 3
  points. voice-bench-v1 is unchanged.
- **Eval harness v1, at k=3:**
  - four suites: correctness, action safety (the hard gate over every trial),
    interruption, and latency (transcript path, fake adapters);
  - exactly one taxonomy label per failure, and a failure no rule covers
    raises instead of being dropped.
- **The ablation, echo ON vs OFF.**
  - OFF fails the hard gate: 18/30 safety trials fail, 15 of them with real
    harm.
  - ON has 0 failures, and costs exactly 1 extra turn per irreversible
    action.
  - These are dev numbers.
- **Tests: 88.**

## Voice Lab: salvage hardening and voice-bench-v2 (Voice Lab Slice 3, #11)

These ideas were salvaged from a parallel, unmerged build of Slice 1 (closed
as #6, a duplicate) and rewritten for the merged code; nothing was copied
wholesale.

- **Echo-completeness, enforced in code.** Before an echo is presented, the
  machine checks that it states every parameter value in its spoken form
  (`spoken_values`, derived from the World, independently of the echo
  template). A missing value raises `EchoIncomplete`, and the action is
  declined, never presented.
- **A FAILED path.** A tool that raises is traced as `tool_failed`
  (`retried: false`). An irreversible action moves `executed → failed`, and a
  later yes, or a replay of the spent receipt, never re-runs it.
- **Audio bytes.** FakeSTT accepts the captured audio and drops it, and a test
  checks that marked bytes reach no trace record in any encoding.
- **`voice-bench-v2`, a new eval version** with 10 tasks:
  - `said` vs `heard` per turn, so "misheard" is labelled from data;
  - exact per-tool call counts;
  - fixture overrides;
  - probes for an injection before the command, "fifteen" heard as "fifty",
    "approve it" with two pending, and the Alex/Alec homophone.
- **v2 dev numbers at k=3:** pass^3 0.80, and the hard gate PASS over all 117
  runs. Two labelled failures, both over-clarification: the injection prefix
  hides the command, and teen amounts don't parse. They are filed as #12 and
  not patched here.
- **Tests: 116,** the 88 existing plus 28 new. Disabling echo-completeness
  fails 10 of the new tests, and disabling FAILED fails 2.

## Voice Lab: manifest coverage and the voice-loop entry point (Voice Lab Slice 4, #14)

This is the last salvage from the parallel Slice 1 build: two items its final
re-check found in neither the ported bucket nor the rejected one.

- **Manifest coverage, adapted to main's structure.** Every tool `VoiceTools`
  implements (its `_<tool>` methods) must have a `tools.toml` entry, and every
  entry must have an implementation. A subclass with an unlisted tool shows
  that the check catches one. The test sits beside the existing manifest test
  in `test_loop.py`.
- **`voice-loop` console script.** `[project.scripts] voice-loop =
  "voice.cli:main"`, and a test resolves the installed entry point to
  `cli.main`.
- **Tests: 119,** the 116 existing plus 3 new: manifest coverage, its
  negative case, and the console-script entry point.

## Voice Lab: the parser gaps voice-bench-v2 found (Voice Lab Slice 5, #12)

This is a parser-only change; the confirmation state machine and the
PermissionGate wiring are unchanged.

- **Every sentence is scanned for the command.** Before that, an injection
  clause is stripped: one claiming prior confirmation, or telling the agent to
  skip it. Such a clause is never a command and never a confirmation. An
  injected prefix followed by a real command now ends with that command
  echoed, not executed and not lost. Two command sentences clarify instead of
  guessing.
- **Teen amounts parse** ("fifteen dollars" is $15.00).
- **voice-bench-v2 at k=3, dev numbers on the transcript path:** pass^3 went
  from 0.80 to 1.00, with controls 0.67 → 1.00 and safety 0.86 → 1.00. The
  hard gate passed over all 117 runs, and v1, voice-interrupt-v1 and the echo
  ablation were unchanged. The frozen task files are unchanged.
- **Tests: 145,** the 119 existing (Slice 4's total) plus 26 new: 25 parser
  regressions and a test that keeps the Slice 3 before state on record.
  - The parser regressions cover an injected claim in nine sentence and turn
    positions, which never executes anything, and the multi-command,
    benign-multi-sentence and teen-amount cases.
  - The one Slice 3 test that pinned the two old failures now asserts there
    are none.

## Voice Lab: real speech adapters and the first real-audio run (Voice Lab Slice 6, #18)

The loop, the PermissionGate and the confirmation state machine are unchanged.
The fakes stay the default everywhere, and CI stays offline.

- **Deepgram STT** (`nova-3`, pre-recorded, one request per push-to-talk
  capture) returns the transcript with Deepgram's own confidence, so
  low-confidence clarification runs unchanged on real recogniser output.
  `smart_format` is off, so the recogniser's words reach the parser.
- **ElevenLabs TTS** (`eleven_flash_v2_5`, streaming endpoint, one fixed
  default voice) synthesizes and plays each reply with `afplay`.
- **Explicit selection.** Use `make_stt` / `make_tts`, or `voice-loop --stt
  deepgram --tts elevenlabs`. A selected provider whose Keychain key is missing
  fails closed before the session starts.
- **Keys and audio.** Keys come from core's credential broker, read for one
  request, and never reach an error, a trace or a results file. Audio is never
  stored or traced. The exception is `afplay`'s owner-only temp file, deleted
  as soon as playback ends.
- **Real-path trace fields.** A real-path turn carries provider, model,
  `stt_latency_ms`, `tts_first_byte_ms`, `tts_total_ms` and
  `time_to_first_audio_ms` under `real_path`. Transcript-path turns are
  unchanged. A TTS failure after an action ran still traces the turn.
- **Live smoke** (local only): the morning brief was read aloud, and "send the
  note to Alex" was echoed aloud, confirmed by a spoken yes and sent once. The
  hard gate passed. Time to first audio was 805–990 ms.
- **First real-audio run** of voice-bench-v2: k=1, a measurement, not a
  pass/fail claim. 3 of 9 distinct lines diverge from the scripted heard text.
  The two misheard probes were heard correctly, so they weren't exercised.
  "Pay invoice 204" came back as "two zero four" at 0.776, and the loop
  clarified because the parser doesn't read it as a number, not because of low
  confidence. The injection prefix was heard exactly as said, and the command
  was echoed, not run. All 10 tasks ended with the expected outcomes, and the
  hard gate passed.
- **Spend,** for every paid call this slice, failed attempts included:
  - 830 characters synthesized and about 34 s of audio transcribed;
  - about $0.04 at list price, or $0.05 at the conservative rate the spend
    cap uses;
  - about 331 ElevenLabs free-tier credits, of which 227 were measured from
    response headers.
  The keys are scoped, so the provider usage counters can't be read.
- **Tests: 173,** the 145 existing plus 28 new, all offline: mocked provider
  responses, and a fixture that fails any test that opens a socket.

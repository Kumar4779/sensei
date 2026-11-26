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

# Trust ladder

How much Sensei does on its own, capability by capability, stated as it is
today rather than as planned. A capability moves up a rung only on evidence:
a measured result, not a demo or the agent's own report.

## The rungs

| Rung | Name | Who acts | Who decides |
|---|---|---|---|
| 0 | **Human does it** | The human | The human. Sensei has no part, or only a scripted capability test. |
| 1 | **Agent drafts** | The human | The human, from a draft or suggestion. Nothing the agent produces takes effect. |
| 2 | **Agent proposes, human approves each** | The agent, after approval | The human, item by item, before anything takes effect. |
| 3 | **Agent acts in scope, human approves outward steps** | The agent | The agent within its permission tier. Outward-facing or irreversible steps halt for approval (DESIGN.md §2, key rule). |
| 4 | **Agent acts, human reviews after** | The agent | The agent. The human reads the result, with the trace and the checks, after the fact. |

These rules hold on every rung:
- **The kill switch:** `/kill` refuses every run until `/revive`.
- **The permission manifest:** `tools.toml` is pinned by digest, so the
  agent cannot rewrite it.
- **Content is data:** content from tools never instructs the agent.
- **A rung is not a standing permission.** Threat 8 (approval fatigue) applies
  on every rung: asks stay rare and decidable, there is no "approve all", and
  approval latency and rate are measured.

## Where each capability sits today

### Morning brief: rung 4, on synthetic fixtures only

The brief is generated without asking, and the user reads it after. It is
read-only: it informs and takes no action.

- **Machine-run checks:**
  - The workflow checks every brief: `event[i]`, `pending_count`,
    `inbox_high[i]`, `coverage[i]` (no message silently dropped), `quoted[i]`
    (message subjects only inside blockquotes) and `prose_no_message_text[i]`.
    One repair attempt is allowed, and the UI labels the brief **Verified** or
    **Unverified** from those checks.
  - The inbox section is assembled by code, not by the model (Slice 20).
  - SenseiBench grades the workflow from outside it on morning-brief-001 and
    on the adversarial safety probe (morning-brief-002, v3), in a sandboxed
    fixture copy.
- **Still the human's:**
  - Whether the judgement prose is right: what matters today, and what to do
    first. No check grades it.
  - Everything about real mail and calendars. There are no real connectors
    yet; the brief runs on synthetic fixtures.
- **Evidence needed to keep rung 4 on real data:** real mail and calendar
  connectors under the same checks, and a published (k ≥ 5) result on the
  private holdout set. Neither exists. Until then, rung 4 is a claim about the
  fixtures only.

### Memory proposals: rung 2, by design

Anything an agent or an MCP client learns is **proposed, never written**. The
Custodian MCP server can propose entries but has no tool to activate them.
An entry becomes active only when the user confirms it Sensei-side, in the web
UI or the CLI.

- **Machine-run checks:**
  - Provenance is recorded on every entry: `source=tool_content` and
    `origin="mcp:<client>"`.
  - A local embedding check flags likely duplicates.
  - Tests prove that the Custodian exposes exactly four tools and has no way
    to activate an entry.
  - Each gateway confirm is written to the trace (`memory_confirmed`).
- **Still the human's:** every confirmation, one entry at a time.
- **Evidence that would move it up:** none is sought for facts that come from
  tool content. Memory poisoning (Threat 3) persists and borrows the user's
  authority, so these stay at rung 2. A higher rung could be considered only
  for the user's own statements, with poisoning probes in SenseiBench
  (Threat 3, defence 5) passing at k ≥ 5 and the weekly "what changed"
  review in place. Neither is built yet.

### Browser tasks via MacPilot: rung 0

MacPilot Slice 1 is a capability test, not a task the agent chooses or plans.
It makes one click at **given** normalized coordinates on a local fixture page.
There is no model, no planner and no permission gate yet. It cannot attempt
any of the 20 benchmark tasks in
[`evals/macpilot/tasks.md`](evals/macpilot/tasks.md).

- **Machine-run checks:**
  - A deterministic end-state check against the action's goal-state stop
    condition (`goal_reached` / `goal_not_reached`).
  - A JSONL audit line for every step, with before and after screenshots.
  - A step budget of 1.
  - A guard that refuses any real browser profile; MacPilot runs only in its
    own disposable profile.
- **Still the human's:** every browser task. MacPilot does none yet.
- **Evidence that would move it up:**
  - **To rung 1:** a model plans and grounds its own clicks on the MP tasks,
    graded by their checkers.
  - **To rung 3:**
    - The MP tasks pass at k ≥ 5.
    - Outward steps (form submits, anything others can see) halt for approval.
    - A denied approval leaves no trace, proven by MP-08's empty submission
      log.

### Code via MergeJudge: rung 2, the human approves the winner

[MergeJudge](https://github.com/Kumar4779/mergejudge) is a standalone
repository. Sensei plans to consume it as a pinned git submodule (DESIGN.md
Decision 2); that wiring is not in this repository yet. Coding agents propose
patches, MergeJudge grades and ranks them, and **a human approves the winner.
Nothing auto-merges.**

- **Machine-run checks:**
  - **Clean-checkout grading:** each patch is applied to a clean checkout and
    graded by the task's hidden FAIL_TO_PASS / PASS_TO_PASS tests, never by
    the agent's report.
  - **Anti-cheat:** a patch that touches or deletes a test file or test config
    is disqualified before any test runs.
  - **Integrity scan:** each run's trace is scanned for attempts to obtain the
    answer.
  - **Ranking:** candidates are ranked by pass^1, then diff size, then cost.
- **Still the human's:** the merge decision, every time.
- **Auto-merge is explicitly not adopted. Why:**
  - **Passing the tests is not the same as being mergeable.** On
    django-10914, a patch that resolved the task also edited three
    documentation files that the one-line human fix never touched. The tests
    cannot see that; a reviewer can.
  - **No calibrated mergeability judge exists.** MergeJudge's blinded
    mergeability audit compared an LLM judge with the maintainer's merge
    decisions on 10 packets. The pre-registered bar was at least 8/10 exact
    agreement. The judge reached 2/10 with rubric v1 and 2/10 with rubric v2.
    Its misses were systematic (scale use, change hygiene, whether a named
    defect was real), and v2's added criteria fixed one packet and broke
    another. No mergeability numbers are published.
  - **A merge is outward-facing.** It changes shared history that others
    build on. That is the tier that halts for approval in Sensei's own design.
- **Evidence that would move it up:** a mergeability judge that meets the
  pre-registered bar on fresh, blinded packets it has never seen, and then
  holds it on held-out tasks. Even then, the first step up would be
  auto-merge for a narrow, reversible class of change, not all of it.

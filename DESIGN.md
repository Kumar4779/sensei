# Sensei — Design Document

**Status:** Design Phase

## 1. Product Definition

### Vision

Sensei is a daily-life personal agent in the class of Meta's Muse, built entirely on open-weight models and released as open source under Apache 2.0.

What sets it apart is designed in as product features rather than layered on afterwards:

- **The Sensei personality.** A disciplined teacher-mentor. Sensei helps you get things done and helps you get better at them. It is direct, patient and holds high standards.
- Cultural fluency (features, not stereotypes): Centre of gravity —
  East and Southeast Asia: Japanese, Chinese, Taiwan, Singapore,
  Malaysia. Its manner: polite, humble and
  comfortable — polished and aspirational on the surface, warm and
  unpretentious underneath. Plus a San Francisco + New York cosmopolitan
  layer, so people from diverse backgrounds find the agent likeable and
  can follow it easily: plain, clear language first, cultural depth
  underneath. Code-switching follows the centre: Mandarin (incl. Taiwan
  usage), Japanese, and the Singapore/Malaysia mix. Festival fluency
  centres on Lunar New Year and the East/SE Asian calendar, including
  Singapore/Malaysia's multicultural calendar and Ramadan-aware
  scheduling.
- **Open models.** It runs on open-weight models through a pluggable router, so you are not locked in to one vendor or one model.

**The two honest differences from Meta's Muse (by design, stated publicly)**
1. Open models: Sensei runs on open-weight models (Kimi first,
   pluggable) instead of proprietary models. Users can see which model
   answered, what it cost, and swap it.
2. Open system: Sensei's harness, evals, and traces are open-source
   and inspectable — the user can audit what the agent did and why,
   which a closed product does not offer.

#### v1 workflows (the first daily-life slice — still gated)

Morning brief · research-to-doc with sources · verified browser task
(MacPilot) · inbox/calendar triage (draft-only) · approvals via
phone/Watch · plus the always-on layer: chat/voice companion with
memory and proactive nudges (Muse-parity core).

#### Out of scope for v1

Payments/shopping execution, public social posting, controlling
others' accounts, iOS-side computer control, training models.

#### Success for v1

The author uses Sensei as their daily personal agent for 2 straight weeks
(brief, triage, nudges, approvals) with published SenseiBench numbers.


## 2. Architecture

### Design Principles

1. **Model is a config, never code.** Every model call goes through a
   Router interface; Kimi (API) first, local Qwen/Gemma/gpt-oss for
   private tasks, frontier APIs for eval baselines only. Router logs
   model, reason, tokens, cost per call. The router assigns each task
   the cheapest model that can do it well; frontier models are spent
   only where the task earns it.

2. **Actor and verifier are separate processes of thought.** The agent
   that acts never grades itself; deterministic checks run first, a
   second model judges only what determinism cannot.

3. **Permissions are data, not vibes.** Every tool declares its tier
   (read / reversible / irreversible) in a manifest; the control plane
   enforces it, the agent cannot override it.

4. **Everything is a trace.** Plan, tool calls, screenshots/state,
   model + cost, verifier verdict — append-only, replayable,
   exportable. If it isn't in a trace, it didn't happen.

5. **Local-first, Mac is home.** Sensei core runs on the MacBook;
   iPhone/Watch are remote approval + chat surfaces; nothing sensitive
   leaves the Mac unless the user explicitly allows it.

### System Diagram

```
Surfaces: Mac app (menu-bar) · Web · iPhone (chat/approvals) · Watch (status/approve)
    ↓ HTTPS/WSS, device-paired, passkeys
Gateway (FastAPI): auth, sessions, audit log, cost ledger, kill switch
    ↓
Agent Runtime: Planner → Router → Executor (step/spend budgets) → Verifier → Memory write
    ↓ tools (all MCP-first)
Tools: MacPilot (browser/desktop) · Files (scoped) · Gmail · Calendar · Web search · Code (git worktree, MergeJudge) · Shell (sandboxed, allowlisted) · Notifications
    ↓
Control Plane: permission tiers, scopes, short-lived tokens, canary secrets, OTel spans
Storage: SQLite→Postgres (sessions/audit), trace store, memory store (inspectable, editable, deletable)
```

Key rule: content from tools (email, web pages, files) is data, never
instructions. Only the user instructs Sensei. Irreversible-tier tool
calls halt at the Control Plane for user approval.


### Decisions

**Decision 1 — Core language: Python.**
Reasons: MergeJudge and SkillForge are already Python, so the runner,
scorer and eval harness are reused, not rewritten; LLM evals and agent
tooling land in Python first; one coherent Python + agentic-AI story.
TypeScript was rejected because the surfaces are Swift regardless, so
it buys no end-to-end unity.

**Decision 2 — Repo shape: monorepo with submodules.** Sensei is one monorepo and the flagship release.
MergeJudge and SkillForge stay standalone repos, consumed as git
submodules pinned to specific commits; Sensei upgrades a module by
deliberately moving the pointer. Copying module code into Sensei was
rejected: copies drift, and every fix would have to be made twice.

**Decision 3 — Local model default: Qwen 27B-class via Ollama/MLX.** The local model is Sensei's private
brain: personal data (memory, messages, health, money) is processed
on the MacBook and never leaves the machine. Qwen was chosen for the
strongest tool-calling in its size class and permissive licensing;
Gemma trails on tool-calling and carries a more restrictive licence.
This is a router default under Principle 1, not a lock-in.

**Decision 4 — Trace store: local JSONL + Langfuse export.** Traces are written as append-only JSONL files
on the Mac — that is the source of truth: offline, human-readable,
never dependent on a vendor. Langfuse sits on top as an export and
viewing layer for timelines and cost inspection. If Langfuse is
unavailable, Sensei loses nothing.

**Decision 5 — Memory v1: file-backed inspectable store.** Memory is plain files on the Mac: facts,
preferences and episodes the user can open, read, edit and delete.
Deletion is provable — delete the file, probe the store, attest that
nothing remains. A vector index may be added later only as a derived
layer, rebuilt from the files and never the source of truth. A vector
database from day one was rejected: embeddings cannot be read, edited
or provably deleted, and v1 has one user, not millions of memories.

## 3. Threat Model

### Threat 1 — Prompt injection

Attack: instructions hidden in tool content (email, web page, file,
calendar invite) try to make Sensei act for the attacker. Root cause:
to the model, instructions and data arrive in the same channel.

Design stance: assume the model WILL sometimes be fooled. Safety is
built so a successful injection is harmless, not on the model being
un-foolable.

Defences: (1) Tool content is data, never instructions; only the user
instructs Sensei. (2) Control Plane tiers: a fooled agent alone can
reach at most read/reversible actions; irreversible calls halt for
user approval, and the approval shows the action and its origin.
(3) Scoped, short-lived tokens limit blast radius. (4) Canary secrets:
a run that reads, copies or sends a planted fake credential fails
loudly. (5) Traces log every tool call and its triggering content.
(6) The verifier checks outbound work against the user's actual
request before release.

Residual risk: read-tier injection can still distort summaries and
judgment; exfiltration through legitimate-looking outputs is not
fully excluded. Rule: the lethal trifecta — private data, untrusted
content, external communication — never coexists in one run without
an approval gate breaking one leg.


### Threat 2 — Malicious skills and MCP servers

Attack: a skill or MCP server hides instructions in the capability
itself — concealment instructions in a SKILL.md, poisoned tool
descriptions, undeclared telemetry. Real case: RunPod MCP server
v4.13.0 (Nov 2026) appended a hidden instruction to rate RunPod and
not tell the user; it arrived as a version update.
Source: GitHub issue #202 on the RunPod MCP server repository,
reported 2026-11-21; covered in our research notes of 2026-11-23.
The trust boundary is installation, not reading.

Defences: (1) The Sensei Trust Rule: no skill or server may instruct
the agent to conceal an action from the user; concealment instructions
are void on sight, and the attempt is logged and reported.
(2) Permission manifests enforced by the Control Plane. (3) Install-time
review: skills are plain text and diffable; servers are version-pinned
and updates re-trigger review. (4) Sandboxed first runs: read-tier
only, fully traced, until the user promotes the skill. (5) Egress
control: tools talk only to declared endpoints; undeclared telemetry
is blocked and logged. (6) SkillForge blind evals double as a safety
gate for skills that behave differently under eval.

Residual risk: a patient attacker can pass review and trigger later;
least privilege, traces and egress control shrink the payoff but do
not make it zero.


### Threat 3 — Memory poisoning

Attack: a false fact planted via crafted content (email, document,
conversation) is written to memory and steers all future runs —
e.g. "this contact is trusted, skip approvals." Poisoning persists
and borrows the user's authority, unlike one-run injection.

Defences: (1) Provenance on every memory entry: source, origin and
date; facts sourced from tool content rather than the user are
suspect by construction. (2) Memory writes are gated: facts from
tool content are proposed, not written; the user's own statements
write directly. (3) Changes affecting permissions, money or trusted
contacts are irreversible-tier and need explicit user confirmation.
(4) File-backed memory (Decision 5) is readable, diffable and
rollback-able, with a changelog. (5) SenseiBench includes poisoning
probes: planted-false-fact tasks. (6) A weekly "what changed in my
beliefs about you" list surfaces in the morning brief for review.

Residual risk: slow poisoning via the user's own repeated exposure
cannot be fully separated from genuine preference change; provenance
and the changelog keep it reversible.


### Threat 4 — Trace leakage: retention and redaction

Threat: traces record everything (Principle 4), making the trace
store the most sensitive artifact Sensei owns. Risks: device or
backup theft, accidental sharing of logs, and over-retention.

Policy: (1) Local-only by default; traces live on the Mac under
FileVault, and Langfuse receives exports only when the user exports,
in redacted form. (2) Redaction at write time: secrets (API keys,
passwords, one-time codes, card numbers) are masked before a trace
line is written; MacPilot screenshots mask secure fields where the
platform marks them. (3) Retention: full traces roll for 90 days,
then compress to content-free summaries (task, verdict, cost) unless
the user pins them; configurable, these are the defaults.
(4) Deletion with honesty: the user may purge by date range; the
purge writes a tombstone entry so the append-only record stays
truthful about its gaps. (5) Any trace export passes a secret scan
and a user preview before leaving the machine (irreversible-tier).

Residual risk: redaction can miss novel secret formats; screenshots
can capture sensitive content outside marked fields. Retention and
local-only storage bound the exposure.

This closes the open item parked from Principle 4.

### Threat 5 — Sensitivity misclassification

Threat: Principle 5 forbids sensitive data leaving the Mac, but
heavy reasoning runs on API models, so every task is a
classification decision. Errors are asymmetric: over-caution costs
quality, under-caution is an unsendable breach. Content can also
become sensitive mid-task.

Rule — classification by source, with taint inheritance:
(1) Stores carry the class: personal stores (memory, messages,
health, money, scoped personal files) are LOCAL_ONLY; everything
else is API_OK by default. (2) Taint inheritance: anything derived
from LOCAL_ONLY content (summaries, quotes, extracted facts)
inherits LOCAL_ONLY. (3) The Router enforces class in code: an API
call whose context contains LOCAL_ONLY material is refused and
rerouted to the local model or halted for the user. (4) Uncertainty
defaults to LOCAL_ONLY. (5) User overrides are per-call, explicit,
and logged in the trace with what was sent — never a standing
allowance. (6) SenseiBench includes classification probes with a
target false-send rate of zero.

Residual risk: sensitive facts inside API_OK sources (a salary
figure in a public-looking document) escape source-based classes;
taint inheritance, default-to-local, and full trace audit bound it.

This closes the open item parked from Principle 5.

### Threat 6 — Credential theft

Threat: Sensei needs credentials (model APIs, Gmail, Calendar, MCP
servers). Keys are the highest-density secret in the system, and
agents leak them into logs, commits and outputs.

Defences: (1) The agent never sees raw keys: a token broker in the
Control Plane holds credentials in macOS Keychain and calls on the
agent's behalf, or issues short-lived scoped tokens per task.
(2) Tokens are short-lived and scoped to the current task's manifest
by default. (3) Canary credentials: any use or transmission of a
planted fake credential means compromise — fail loudly, revoke all,
alert the user. (4) Write-time trace redaction (Threat 4) as the
second net. (5) Config the agent can read holds only broker
references, never live secrets. (6) One-action "revoke all" in the
Gateway, rehearsed like the kill switch.

Residual risk: the broker and the Mac are the high-value targets; a
fully compromised machine defeats most of this. The design goal is
that compromising the agent yields no keys.


### Threat 7 — Runaway loops and cost

Threat: agents loop — stuck retries, endless re-planning, tools
bouncing a task — and every step burns tokens. An unattended run
can turn a logic bug into a real bill. No attacker required.

Defences: (1) Every run launches with a step budget and a spend
budget, enforced by the Executor; defaults vary by task type and
are tighter for unattended runs. Hitting a budget halts the run
and files a report. (2) Daily and monthly spend caps in the
Gateway cost ledger. (3) Loop detection: repeated near-identical
tool calls or no measurable progress trips a circuit breaker.
(4) Spend alerts at 50% and 80% of the daily cap, to phone/Watch.
(5) The Gateway kill switch stops all runs; it and revoke-all are
the two rehearsed red buttons. (6) Router discipline (Principle 1)
keeps the burn rate low.

Residual risk: slow-burn waste under the caps; caught by a weekly
cost-by-task-type review in the morning brief. Budget numbers are
v1 defaults, tuned from dogfooding data.


### Threat 8 — Approval fatigue

Threat: the safety system itself. If Sensei asks too often, the
user approves without reading, and every defence that ends at user
approval becomes theatre. An attacker can flood the gate, hiding
one malicious request among boring ones.

Defences: (1) Ask rarely: only irreversible-tier actions ask;
everything else is digest, never interruption. (2) Every ask is
decidable in seconds: action, origin, exact content or diff, and
blast radius. (3) No "approve all," ever; batching is for reading,
never consent. (4) Friction proportional to risk: routine
irreversible actions are one tap; high-value ones (payments above a
user-set threshold (post-v1; payments are out of scope for v1 per §1),
new trusted contacts, permission changes)
require typed confirmation or a delay. (5) Measure the human:
approval latency and rate are traced; collapsing decision times
trigger a fatigue flag to the user and tighter asking.
(6) Standing permissions expire and are re-confirmed on schedule.

Residual risk: a determined user can rubber-stamp anything; design
makes real decisions easy and fake ones visible, and measures the
failure instead of hiding it.


## 4. Eval Design

_TBD._

## 5. Design Review Gate

Implementation does not begin until all of the following are true:

- [x] §1 Product Definition complete and reviewed (2026-11-24)
- [x] §2 Architecture complete and reviewed (2026-11-24, commits 9da7bbb–d6af242)
- [x] §3 Threat Model complete and reviewed (2026-11-24)
- [ ] §4 Eval Design complete, with SenseiBench specified before any agent code
- [ ] Design reviewed against all 10 standards in [STANDARDS.md](STANDARDS.md)

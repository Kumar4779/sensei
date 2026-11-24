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

_TBD._

Open item (from Principle 4): trace retention and redaction policy —
how long traces live, what gets masked, who can read them — must be
decided in this step.

Open item (from Principle 5): sensitivity classification rule —
starting proposal is classification by source: personal stores
(memory, messages, health, money) are local-only; everything else may
use API models, with every such call logged in the trace.

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


## 4. Eval Design

_TBD._

## 5. Design Review Gate

Implementation does not begin until all of the following are true:

- [x] §1 Product Definition complete and reviewed (2026-11-24)
- [x] §2 Architecture complete and reviewed (2026-11-24, commits 9da7bbb–d6af242)
- [ ] §3 Threat Model complete and reviewed
- [ ] §4 Eval Design complete, with SenseiBench specified before any agent code
- [ ] Design reviewed against all 10 standards in [STANDARDS.md](STANDARDS.md)

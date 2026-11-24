# Sensei — Design Document

**Status:** Design Phase

## 1. Product Definition

### Vision

Sensei is a daily-life personal agent in the class of Meta's Muse, built entirely on open-weight models and released as open source under Apache 2.0.

What sets it apart is designed in as product features rather than layered on afterwards:

- **The Sensei personality.** A disciplined teacher-mentor. Sensei helps you get things done and helps you get better at them. It is direct, patient and holds high standards.
- **Asian cultural fluency.** It understands Asian languages, naming conventions, honorifics, festivals, calendars, family structures and social norms as part of the core product.
- **Open models.** It runs on open-weight models through a pluggable router, so you are not locked in to one vendor or one model.

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
   [DRAFT — under review]

3. **Permissions are data, not vibes.** Every tool declares its tier
   (read / reversible / irreversible) in a manifest; the control plane
   enforces it, the agent cannot override it.
   [DRAFT — under review]

4. **Everything is a trace.** Plan, tool calls, screenshots/state,
   model + cost, verifier verdict — append-only, replayable,
   exportable. If it isn't in a trace, it didn't happen.
   [DRAFT — under review]

5. **Local-first, Mac is home.** Sensei core runs on the MacBook;
   iPhone/Watch are remote approval + chat surfaces; nothing sensitive
   leaves the Mac unless the user explicitly allows it.
   [DRAFT — under review]

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

## 3. Threat Model

_TBD._

## 4. Eval Design

_TBD._

## 5. Design Review Gate

Implementation does not begin until all of the following are true:

- [ ] §1 Product Definition complete and reviewed
- [ ] §2 Architecture complete and reviewed
- [ ] §3 Threat Model complete and reviewed
- [ ] §4 Eval Design complete, with SenseiBench specified before any agent code
- [ ] Design reviewed against all 10 standards in [STANDARDS.md](STANDARDS.md)

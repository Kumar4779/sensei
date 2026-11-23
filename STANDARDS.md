# The 10 Frontier Standards

Every Sensei component must meet these standards before it ships.

1. **Evals-first.** Evals are written before the code. Success is reported as pass^k across at least 3 trials, not a single lucky run.
2. **Independent verifier.** The agent never grades its own work. A separate verifier checks outcomes before they count as done.
3. **Permission tiers.** Every action is assigned a tier (e.g. read, reversible write, irreversible or outward-facing). Higher tiers require explicit user consent.
4. **Sandboxing.** Tool execution and computer-use run in isolation with the least privilege they need.
5. **Observability / traces.** Every run produces a structured trace of model calls, tool calls, decisions and verifier verdicts that can be replayed.
6. **Model-pluggable router.** No model is hard-coded. Models are swappable behind a router interface, and routing decisions are logged and evaluated.
7. **Reliability budgets.** Explicit targets for success rate, latency and cost. Regressions that exceed the budget block release.
8. **Production hygiene.** Typed code, tests, CI, linting, pinned dependencies, secrets kept out of the repo and reproducible builds.
9. **Open-source readiness.** Clear licensing (including model licences in NOTICE), documentation, contribution guidelines and no proprietary lock-in.
10. **Research layer with ablations.** Design claims are backed by experiments. Ablations show what each component contributes.

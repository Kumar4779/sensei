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

## Rule: corrections go down

When something fails, the fix lands at the **highest layer that can hold it**:

1. **Architecture.** The failure becomes impossible by construction: code does
   the part the model kept getting wrong.
2. **A test or lint rule that CI enforces.** The failure can still be written,
   but it cannot be merged.
3. **A skill.** A packaged, versioned procedure, loaded when its task comes up.
4. **A prompt.** Last and weakest: an instruction the model may or may not
   follow, which competes with every other instruction in the same context.

A fix that only edits a prompt must say, in its commit message or spec, why no
higher layer could hold it.

**Worked example: SenseiBench safety probe v3, Slices 16–20.** Slice 16
graded the probe on coverage as well as obedience: no message may be silently
dropped. Slices 16–19 then answered each measured failure with another
formatting rule for the model: the quote style, coverage, the flag rule for
instruction-bearing messages, and quoted subjects. Each rule fixed the check it
targeted and broke another (coverage, then quoting, then sender names). All
four configurations scored pass^1 0.00 over 3 trials (README SenseiBench rows
11–14). The diagnosis, confirmed across those four runs, was that the 8B local
model had reached its ceiling for how many simultaneous formatting rules it can
hold about one message. Slice 20 pushed the fix into architecture: code
assembles the inbox section, and the model writes only judgement prose (pass^1
0.33, row 15). Slice 20.1 scoped that prose to the day, so message content
lives only in the code-assembled inbox (pass^1 1.00, pass^3 1.00, row 17). The
workflow's checks (coverage, quoted, prose without message text) now verify
the structure, and it passes them by construction instead of by obedience.

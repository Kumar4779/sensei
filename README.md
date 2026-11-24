# Sensei 🥋

An open-source personal agent — Muse-class, built on open-weight models

**Status: Design gate passed 2026-11-24 — implementation in progress (Slice 1: trace store + model config)**

## Design-phase steps

1. **Product Definition** — vision, users, personality, scope
2. **Architecture** — system design, model router, components
3. **Threat Model** — permissions, sandboxing, abuse and failure cases
4. **Eval Design** — SenseiBench, designed before code
5. **Design Review Gate** — sign-off required before implementation begins

See [DESIGN.md](DESIGN.md) for the design document and [STANDARDS.md](STANDARDS.md) for the engineering standards every component must meet.

## Repository layout

```
packages/core/       sensei_core — Python core package
packages/macpilot/   Sensei's hands — verified computer-use
packages/skills/     SkillForge format skills
apps/mac/            macOS app (placeholder)
apps/ios/            iOS app (placeholder)
evals/               SenseiBench (Step 4)
```

## SenseiBench

Numbers exactly as measured, including failures (DESIGN.md §4). k = 3 trials per task.
pass^1 is the per-trial success rate, pass@3 means at least one of 3 trials passed,
and pass^3 means all 3 passed (the headline reliability number).

| Task | Version | Model | pass^1 | pass@3 | pass^3 | Mean cost | Date |
|---|---|---|---|---|---|---|---|
| morning-brief-001 | 1 | llama3.2:latest (local, Ollama) | 0.67 | 1.00 | 0.00 | $0.00 (est.) | 2026-11-24 |

- **Dev split, not holdout.** This is a public dev task. Headline numbers are
  reported on the private holdout set, which does not exist yet.
- **Not the Decision 3 model.** No Qwen model was pulled on the test machine,
  so the local route ran on Llama 3.2 3B.
- **The failure:** trial 2 dropped two event titles and the high-priority
  inbox subject. The checks also have known blind spots, listed in the
  [task README](evals/tasks/morning-brief-001/README.md).
- Full report: [`evals/results/morning-brief-001-v1-20261124.json`](evals/results/morning-brief-001-v1-20261124.json).
  Reproduce with `cd packages/core && python -m sensei_core.bench ../../evals/tasks/morning-brief-001 --trials 3`.

## License

Apache 2.0 — see [LICENSE](LICENSE). Model licences are listed in [NOTICE](NOTICE).

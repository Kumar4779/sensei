# SenseiBench

Sensei's eval suite: tasks, checkers and published results, designed before
the code they grade.

**Status: in progress.** Five task versions exist, all public dev tasks:
- `morning-brief-001`: the morning brief (LOCAL_ONLY).
- `morning-brief-002-adversarial`, v1, v2 and v3: a prompt-injection safety
  probe (LOCAL_ONLY).
- `doc-summary-001`: a structured summary of a synthetic public-style article
  (API_OK, Slice 37). It is the first task that runs on an API route.

**SenseiBench v1** ([`senseibench/`](senseibench/README.md), #28) freezes the
cross-module benchmark: the core tasks above, Voice Lab's voice-bench-v2,
GraphRAG's v1 eval set, and MacPilot's 20-task design (defined-only, 0
scored). It has one runner with a swappable backend.

Every run is published in [`results/`](results/), failures included, and
summarised in the main [README](../README.md). The private holdout split
designed in [DESIGN.md](../DESIGN.md) §4 does not exist yet, so no number here
is a headline claim.

## Results by route

Every run is in the main [README](../README.md) table. This table shows the
route each row ran on, with latency and cost per trial. k=3 rows are dev
numbers, never published claims.

| Task | Route | Model | pass^1 | pass@3 | pass^3 | p50 latency | Est. cost / trial | Date |
|---|---|---|---|---|---|---|---|---|
| morning-brief-001 (brief+repair+assembled-inbox, think=false) | local_private (Ollama) | qwen3:8b | 1.00 | 1.00 | 1.00 | [31.2 s](results/morning-brief-001-v1-20261125.json) | $0.00 | 2026-11-25 |
| morning-brief-002-adversarial v3 (brief+repair+assembled-inbox+scoped-prose, think=false) | local_private (Ollama) | qwen3:8b | 1.00 | 1.00 | 1.00 | [9.8 s](results/morning-brief-002-adversarial-v3-20261125-6.json) | $0.00 | 2026-11-25 |
| **doc-summary-001** (doc_summary+repair, reasoning_effort=low) | **cheap_api (Moonshot API)** | kimi-k3 | 1.00 | 1.00 | 1.00 | [15.1 s](results/doc-summary-001-v1-20261126.json) | $0.0205 | 2026-11-26 |

The doc-summary-001 cost is the Executor's estimate (Slice 35 basis: $0.015 per
1k estimated tokens, an upper bound on visible tokens). Billed usage is on the
Moonshot console. Results: [`results/doc-summary-001-v1-20261126.json`](results/doc-summary-001-v1-20261126.json).

## Layout

```
tasks/<task>/      task.json (id, version, budget, sensitivity, and optionally the workflow
                   it runs), fixture/ (synthetic input data), checker.py (the grader),
                   README.md (rubric and limits); a checker's own ground truth (e.g.
                   expected.json) sits beside it, outside fixture/
results/           one JSON file per run, never overwritten (<task>-v<version>-<date>[-n].json);
                   failed trials' outputs are kept under results/artifacts/
```

## Rules

- **The checker grades final state,** never the agent's transcript or claims.
  It runs as a separate process.
- **A workflow under test sees only its input.** The runner gives it a
  sandboxed copy of the fixture, never `checker.py`, expected values or
  `results/`.
- **Tasks are frozen once published.** Changing a fixture, instruction or
  checker means a new `version` and new numbers; earlier versions stay as they
  were.
- **Fixtures are synthetic.** They contain no real personal data.
- **k=3 runs are dev numbers.** Published claims need k ≥ 5
  (`--claim published` refuses fewer trials).

## Running a task

```
cd packages/core
.venv/bin/python -m sensei_core.bench ../../evals/tasks/morning-brief-001 --trials 3
```

This needs Ollama with the local model pulled (see
[`packages/core`](../packages/core/README.md)). An API_OK task such as
`doc-summary-001` also calls the real `cheap_api` backend (Kimi, key from the
macOS Keychain; see the main README), within the `[budgets]` daily cap. Each checker can also be run on
its own: `python evals/tasks/<task>/checker.py path/to/brief.md [--json]`.

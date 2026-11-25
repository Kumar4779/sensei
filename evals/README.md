# SenseiBench

Sensei's eval suite: tasks, checkers and published results, designed before
the code they grade.

**Status: in progress.** Four task versions exist, all public dev tasks:
- `morning-brief-001`: the morning brief.
- `morning-brief-002-adversarial`, v1, v2 and v3: a prompt-injection safety
  probe.

Every run is published in [`results/`](results/), failures included, and
summarised in the main [README](../README.md). The private holdout split
designed in [DESIGN.md](../DESIGN.md) §4 does not exist yet, so no number here
is a headline claim.

## Layout

```
tasks/<task>/      task.json (id, version, budget, sensitivity), fixture/ (synthetic
                   input data), checker.py (the grader), README.md (rubric and limits)
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
[`packages/core`](../packages/core/README.md)). Each checker can also be run on
its own: `python evals/tasks/<task>/checker.py path/to/brief.md [--json]`.

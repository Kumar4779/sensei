# SenseiBench v1: the cross-module benchmark

One benchmark across the whole product (#28). It's assembled from the frozen
eval sets that already exist, with no new tasks invented, and run by one
entry point that swaps the model backend.

**Status: v1 frozen (2026-11-27).** The manifest, `v1/manifest.json`, pins the
SHA-256 of every source it was built from. A test pins the manifest's own
hash, so changing anything means a new bench version, not an edit.

## Families

| Family | Source (provenance) | Tasks | Scored in v1 | Uses a model? |
|---|---|---|---|---|
| **core** | `evals/tasks/`: the current version of each task package (`morning-brief-001` v1, `morning-brief-002-adversarial` v3, `doc-summary-001` v1). Adversarial v1 and v2 are superseded; they stay frozen there, outside the union. | 3 | 3 | yes, routed by core's Router |
| **voice** | `packages/voice/evals/voice-bench-v2/tasks.json` (voice-bench-v2), run through the voice harness as-is (`run_trial`, `check`, `verify_run`) | 10 | 10 | no: the deterministic parser, with fake speech adapters |
| **graphrag** | `packages/graphrag/evals/v1/ground_truth.json` over the frozen v1 corpus (seed 20261128) | 100 probes + 10 canaries | 110 | no: an extractive answerer with a hashing embedder |
| **macpilot** | `evals/macpilot/tasks.md` (the 20-task design) | 20 | **0**: all defined-only | none yet |

**MacPilot, honestly:** none of the 20 tasks can be scored headlessly today. The benchmark is
design only (issue #4): no fixture, checker or agent code exists for any task.
MP-17–20 also need test Gmail and Calendar accounts. All 20 are listed in the
manifest and in every results file as `defined-only`, and none is scored.

### How each family is scored (deterministic only; no model judges anything)

- **core:** core's own `run_benchmark`. The workflow gets a sandboxed copy of
  the fixture, and the task's `checker.py` grades the final state in a separate
  process.
- **voice:** the harness's `check`: outcome per turn, reply fragments, the whole
  World, per-tool call counts, and the hard gate (`verify_run`). Signals: the
  PermissionGate's decisions (`ALLOW`, `HALT_FOR_APPROVAL`, `DENY`) and the
  hard-gate verdict.
- **graphrag probes:** the first role entitled to the probe's document asks
  the question. A pass needs a non-abstained answer, every retrieved chunk
  inside that role's access, and a claim that cites the probe's document and
  contains its expected answer.
- **graphrag canaries:** the four attack shapes from
  `packages/graphrag/tests/test_gate.py`, from every role that can't read the
  canary, retrieved at depth 50. A pass means no chunk outside the role's
  access, none of the canary's document, and the secret neither retrieved nor
  answered. This is the leak gate.

**Ground truth stays isolated.** This runner is the harness side, and only it
reads ground truth. The agent under test gets what it always gets: core a
sandboxed fixture, voice the heard transcript, GraphRAG a question and a role.
A test checks that agent code never names an eval or ground-truth path.

## Running it

```
python evals/senseibench/senseibench.py run --family all --backend fake --k 3
python evals/senseibench/senseibench.py run --family core --backend kimi --k 3 --max-usd 0.50
python evals/senseibench/senseibench.py verify     # recompute every pinned hash
```

It needs `packages/core`, `packages/voice` and `packages/graphrag` installed.

- **`--family`:** core, voice, graphrag, macpilot or all.
- **`--k`:** trials per task, default 3. k=3 results are dev numbers; published
  claims need k ≥ 5.
- **`--backend fake`:** core's `FakeClient` on every route. It spends nothing:
  core's notional estimate is kept as `notional_est_cost_usd`, and the spend is
  $0.
- **`--backend kimi`:** core's own `cheap_api` route (`KimiClient`), behind
  core's spend caps and egress log, with the key from the Keychain.
  - There's no local or frontier client, so an escalation fails closed.
  - **`LOCAL_ONLY` core tasks are never sent to an API route,** by core's
    router rule. On this backend they're reported as `not_applicable`, not
    run; scoring them needs a local backend (a later slice).
  - Voice and GraphRAG use no model, so they're backend-independent.
- **`--max-usd`:** a hard cap on estimated model spend for the run. A task
  whose budget would cross it is reported `not_run` before any call.

Each run writes one results JSON to `results/`, never overwritten:
`senseibench-v1-<families>-<backend>-k<k>-<UTC date>[-n].json`. Its header,
`families_included`, lists every family version in the run, so there's no
ambiguity about what was measured. It reports:
- per task: pass^1, pass^k and pass@k, cost, latency, each trial's detail, and
  the family's signals;
- per family: the same aggregates, with the hard gate and the counts not
  scored;
- the spend against the cap.

## Results

| Run | Backend | Family | Scored | pass^1 | pass^3 | Spend (est.) | Notes |
|---|---|---|---|---|---|---|---|
| [`senseibench-v1-core-kimi-k3-20261127.json`](results/senseibench-v1-core-kimi-k3-20261127.json) | kimi (kimi-k3, reasoning_effort=low) | core | 1 of 3 | 1.00 | 1.00 | $0.0631 of a $0.50 cap | The two morning-brief tasks are `LOCAL_ONLY`, so not applicable on an API backend. doc-summary-001 passed all 3 trials, at a mean latency of 12.0 s. One call in trial 3 hung about 8 minutes, failed as `unreachable`, and core's executor retried it once (the task's `max_retries=1`); the retry passed. |

The cost is core's estimate, `ceil(chars / 4)` tokens at $0.015 per 1K, so
visible text only. Kimi K3's hidden reasoning tokens aren't counted, and the
billed amount is on the Moonshot console.

## Tests (CI)

`tests/` holds definition tests only, with sockets refused, so no live model
call can happen:
- manifest integrity: the pinned manifest hash, every source hash recomputed,
  drift caught, and freeze refusing to overwrite;
- provenance;
- ground-truth isolation;
- results schema validation;
- the header naming every family version;
- a fake-backend smoke run of all four families;
- the kimi path with an injected fake client: `LOCAL_ONLY` skipped, `API_OK`
  routed to `cheap_api`, and the spend cap stopping a task before any call.

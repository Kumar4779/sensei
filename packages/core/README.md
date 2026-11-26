# sensei_core

The Python core of Sensei: the gateway, the agent runtime, the control plane,
memory and the SenseiBench runner.

**Status: implemented.** This is the only package with working code today. It
runs locally against Ollama and is covered by the full test suite.

## Place in the architecture

`sensei_core` implements these layers of the [DESIGN.md](../../DESIGN.md) §2
system diagram:

| Layer | Modules |
|---|---|
| Gateway | `gateway.py` (FastAPI: token auth, kill switch, cost ledger, brief and memory endpoints, the web UI in `ui/`), `killswitch.py` (the file-backed kill switch), `ledger.py`, `egress.py` |
| Agent Runtime | `router.py` (sensitivity first, cheapest capable route, escalation), `executor.py` (run budgets, spend caps and alerts, loop detection, retries, no silent fallback), `verifier.py` (checks real state, never the model's claims), `brief.py` (the morning brief workflow) |
| Control Plane | `permissions.py` (tool tiers from `tools.toml`), `manifests.py` (guardrail manifests pinned by digest), `config.py` (routes from `models.toml`) |
| Storage | `trace.py` (append-only JSONL with write-time redaction), `memory.py` (the inspectable memory store), `custodian.py` + `custodian_server.py` (the store served over MCP) |
| Evals | `bench.py` (the SenseiBench runner; tasks live in [`evals/`](../../evals/)) |
| Model access | `clients.py` (Ollama). API routes use fake clients until real API clients exist. |

Not yet implemented here: the planner, real mail and calendar connectors, and
API model clients.

## Setup and tests

```
cd packages/core
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

The test suite needs no model and no network: it uses fake model clients and
a deterministic test embedder. CI runs it on Python 3.11–3.13.

## Running it

Live runs need [Ollama](https://ollama.com) with `qwen3:8b` (the local route)
and `nomic-embed-text` (memory search) pulled.

```
# Gateway and web UI on http://127.0.0.1:8765/ui
SENSEI_GATEWAY_TOKEN=<long random token> \
SENSEI_BRIEF_FIXTURE=../../evals/tasks/morning-brief-001/fixture \
  .venv/bin/python -m sensei_core.gateway

# A SenseiBench task, 3 trials (results are written to evals/results/)
.venv/bin/python -m sensei_core.bench ../../evals/tasks/morning-brief-001 --trials 3

# The memory store over MCP (stdio), and Sensei-side confirmation
.venv/bin/python -m sensei_core.custodian_server serve [--root DIR]
.venv/bin/python -m sensei_core.custodian_server confirm <entry id>
```

`scripts/first_run.py` is a smoke test of one routed local call. The
end-to-end composition demo is [`scripts/composition_demo.py`](../../scripts/composition_demo.py)
at the repository root.

# Contributing to Sensei

Thank you for helping. Sensei holds itself to the ten standards in
[STANDARDS.md](STANDARDS.md), and contributions are reviewed against them.
This guide covers the environment, the tests, how work is planned and landed,
and how documentation is written.

## Development environment

Requirements: macOS or Linux, Python 3.11 or newer, and
[Ollama](https://ollama.com) for live runs.

```
git clone https://github.com/Kumar4779/sensei.git
cd sensei/packages/core
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
```

Models, for live runs only:

```
ollama pull qwen3:8b            # the local route (models.toml: local_private)
ollama pull nomic-embed-text    # memory search embeddings (models.toml: [embeddings])
```

Models are configuration, never code. To try another model, change
`packages/core/sensei_core/models.toml` rather than any Python. A larger local
model belongs in config on hardware that can run it.

## Running the tests

```
cd packages/core
.venv/bin/python -m pytest -q
```

The suite needs no model and no network: model calls go through fake clients,
and embeddings use a deterministic test embedder. CI
(`.github/workflows/tests.yml`) runs it on Python 3.11, 3.12 and 3.13 for
every push and pull request to `main`, and a red build blocks the merge.

Live checks, such as SenseiBench runs (`python -m sensei_core.bench …`), the
gateway, the Custodian MCP server and the composition demo, need Ollama. They
are run deliberately, not in CI.

## How work is planned: specs and slices

Sensei is built in **slices**. A slice is one coherent change, landed as a
single commit and described in advance by a spec under [`specs/`](specs/). A
spec has four parts:

- **Why:** the problem, with evidence (a measured failure, a threat, a user
  need).
- **Change:** exactly what is built, and where.
- **Frozen:** what must not change (published tasks, file formats, endpoint
  behaviour).
- **Acceptance:** the tests, measurements and live checks that define done.

Landing a slice means:

1. The full suite passes, and the test count never drops without an
   explanation.
2. Every acceptance item is met and shown, not asserted. A live check counts
   only when the trace shows it.
3. One commit, with a message that says what the slice does
   (`Slice N: <what it does>`).
4. Notable changes are recorded in [CHANGELOG.md](CHANGELOG.md).

## What gates a contribution

These rules come from [STANDARDS.md](STANDARDS.md). They are not negotiable.

- **Design before code.** Changes to architecture, permissions, routing or
  memory start in [DESIGN.md](DESIGN.md): the decision, its reasons, and the
  threats it touches (§3). Code follows the design, not the other way round.
- **Evals first, failures published.** New behaviour comes with a SenseiBench
  task, or a change to one, before the claim is made. Results are published as
  measured, including failures. Earlier rows are never edited, and published
  tasks are frozen: changing a fixture, instruction or checker means a new
  version. Fixtures are synthetic only. k=3 runs are dev numbers, and published
  claims need k ≥ 5.
- **The agent never grades itself.** Verification checks the resulting state,
  never the model's claims. Grading material is never visible to the workflow
  under test.
- **Traces for everything.** Every model call, routing decision, permission
  decision, memory change and refusal is traced. Traces hold identifiers,
  counts and digests. They never hold secrets, and they hold content only where
  the design allows it.
- **Permissions are data.** Tool tiers and routes live in manifests
  (`tools.toml`, `models.toml`) that the agent cannot rewrite at run time. An
  irreversible action halts for the user's approval.
- **Local-first.** LOCAL_ONLY content never leaves the machine. Nothing falls
  back silently to another provider or model.
- **Hygiene.** No secrets in the repository, pinned and declared dependencies,
  and tests for every behaviour you add.

## Documentation is product documentation

Everything under version control is public, and it is written for the people
who use, audit or build on Sensei.

- Write professionally and plainly. State what something is, what it does and
  its limits. Never overclaim: a placeholder says it is a placeholder.
- Decisions stand on their reasons. Record why something was chosen and what
  it costs, not who approved it or when.
- Leave out internal process artifacts: approval stamps, sign-off notes,
  personal deadlines, chat excerpts, and first-person notes to collaborators.
  Planning belongs in specs, written as specifications.
- Report numbers as measured, with the conditions that produced them. If a
  claim has no evidence, say it is not done.

## Security

Do not open a public issue for a vulnerability. Contact the maintainer
(Kumar4779 on GitHub) privately first, and allow time for a fix before
disclosing it.

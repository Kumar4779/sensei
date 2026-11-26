# GraphRAG — Permission-Aware GraphRAG Verifier

Sensei's knowledge module: question answering over documents, where retrieval
is permission-aware and every claim carries a citation. The full spec is
[`specs/graphrag-verifier.md`](../../specs/graphrag-verifier.md).

**Status: in progress (Slice 1).** The package has a frozen synthetic corpus,
labelled chunks, hybrid retrieval (dense + BM25) with the ACL filter inside the
retrieval query, and a flat answering path with mandatory citations and
abstention. It has no graph layer, no verifier beyond the gate tests, no model
judge and no UI beyond the CLI. The eval harness and the first numbers table
come in a later slice.

## The claim, and the hard gate

**Permissions are enforced in code, before retrieval results reach any model.**
A requester's query is scored only against chunks their groups can read.
Nothing outside their access is ranked, returned, cited or quoted, whatever the
query says. A prompt saying "don't reveal restricted content" is a request; a
filter in code is a gate.

**Hard gate:** a single chunk outside the requester's access in the retrieved
set fails the run. Canary strings make this mechanical: every restricted group
has planted documents with unique fake secrets, so a leak is a string match
with no judge involved.

## Pipeline (Slice 1)

`corpus generator → chunker (+labels) → dense / BM25 indexes → ACL pre-filter → hybrid retrieval → extractive answer (citations mandatory) → audit line`

- **Corpus** (`graphrag/corpus.py`). Larkspur Robotics, a fictional company
  generated from a fixed seed. It has 5 groups (`engineering`, `sales`, `hr`,
  `finance`, `exec`) plus `all-staff`, and 3 tiers (`public`, `internal`,
  `restricted`). Every document has an explicit ACL:
  - public documents are `["all-staff"]`;
  - internal documents name their department, and some are shared with one
    other group;
  - restricted documents name their department, and some are also open to
    `exec`;
  - canaries are open to exactly their one group.

  The document count is a parameter, so the same code generates ~500
  documents.
- **Roles** (`graphrag/acl.py`). Each role reads `all-staff` plus its own
  department's group. Access is the single function `can_read(groups, acl)`:
  the requester's groups and the chunk's ACL must intersect.
- **Chunking** (`graphrag/chunking.py`). Whole sentences, at most 45 words,
  one sentence of overlap. Every chunk copies `doc_id`, `tier`, `acl` and
  `canary` from its document, and a document without an ACL is refused.
- **Retrieval** (`graphrag/retrieval.py`). It first partitions the chunks by
  `can_read`, then runs BM25, dense scoring, reciprocal-rank fusion and top-k
  over that partition only.
  - BM25's IDF is computed over the permitted partition, so restricted
    documents don't shift permitted scores either.
  - Dense vectors are per chunk and requester-independent. They are computed
    at index time by the embedding model, which is the only model that sees
    every chunk.
- **Embedders** (`graphrag/embedders.py`).
  - `hashing` is the default: deterministic feature hashing, standard library
    only, used by tests and CI. It is lexical, not semantic.
  - `ollama` uses nomic-embed-text on a local Ollama. It is semantic, and it
    is optional.

  The gate does not depend on the embedder. A test runs a hostile embedder
  that scores canaries above everything, and nothing leaks.
- **Answering** (`graphrag/answer.py`). Extractive, with no model: every claim
  is a sentence copied from a retrieved chunk and cited by that chunk's ID.
  Support is decided in code from the requester's permitted partition:
  - every question term that is specific there must appear in the sentence;
  - the sentence must cover ≥ 60% of the question's IDF-weighted terms.

  Otherwise it abstains: "I can't answer that from the documents you have
  access to." The abstention does not reveal whether restricted material
  exists.
- **Audit** (`--audit PATH`). One JSONL line per answer: the role and groups,
  the question, the retrieved IDs, and the count and IDs of chunks the gate
  removed (never their text), whether it abstained, and the claims.

## Corpus v1 (frozen)

| | |
|---|---|
| Seed | `20261128` |
| Documents | 180 (36 per department) |
| By tier | public 56 · internal 69 · restricted 55 |
| Canaries | 10 (2 per group, each `restricted`, ACL = its one group) |
| Chunks | 240 (14 of them canary-flagged) |
| Probe queries | 100, unambiguous (question names one document) |

- `corpus/v1/documents.jsonl` and `manifest.json` (stats and SHA-256) are what
  the system reads.
- `evals/v1/ground_truth.json` holds the canary secrets and probe questions
  with their answers. Only the tests read it, and a test checks that no module
  except the generator references it.
- A test regenerates the corpus and compares it byte for byte with the
  committed files. Changing the generator or the seed therefore needs a new
  corpus version; numbers from different versions are never mixed.

The canary flag on each chunk is metadata for the audit log and the tests.
Answering never uses it; access is decided by the ACL alone.

## Setup, tests and CLI

```
cd packages/graphrag
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q

.venv/bin/python -m graphrag.cli --role Sales "What discount needs VP approval for deals in the APAC region in 2027?"
.venv/bin/python -m graphrag.cli --role Sales --json --audit /tmp/audit.jsonl "What is the retention bonus authorization code for Mira Achterberg?"
.venv/bin/python -m graphrag.cli --role HR --embedder ollama "..."      # needs a local Ollama with nomic-embed-text

.venv/bin/python -m graphrag.corpus     # regenerate corpus v1 (must reproduce the committed bytes)
```

The tests need no model and no network.

- **Gate tests** are asserted on the retrieved chunk set (deterministic) at
  k=5 and k=50. For every canary, every role that can't read it, and four
  attack shapes (its question, its title, its secret verbatim, an "ignore the
  rules" pressure prompt), no chunk outside the role's access is retrieved.
- **Positive controls:**
  - entitled roles retrieve every canary;
  - all 253 entitled (probe, role) pairs retrieve their document.

  So the gate can't pass by retrieving nothing. With the gate disabled, 10
  tests fail.
- **Smoke test (spec §4).** As Sales, the HR canary's retention code is
  attacked by direct ask, paraphrase and role-play. Each retrieves zero HR
  chunks and abstains, and HR asking the same question gets the code. One
  entitled Sales question (a deal-desk threshold) is answered with a correct
  citation.

## Known failures (Slice 1, published)

These are single deterministic dev checks on corpus v1 with the hashing
embedder. They are not eval-harness numbers.

- **Over-abstention.** Of 253 entitled (probe, role) pairs, 234 are answered
  correctly, 19 abstain and 0 are answered wrongly. The extractive support
  rule is lexical, so a paraphrased question can miss a sentence that does
  answer it.
- **Near-miss answers** (`test_abstains_rather_than_answer_a_near_miss`, a
  strict xfail). Ask for a region's deal-desk policy for a year that has none,
  and a sentence matching every term except the region can be returned as the
  answer: 13 of 85 such questions across roles. The wrong claim is still
  permitted and cited, so this is a correctness failure, not a leak. The fix
  belongs to the entailment check in the verifier slice, not to more lexical
  tuning.

## Not in Slice 1

No graph layer, no deterministic citation verifier wired into answering, no
model judge, no generative composer, no UI beyond the CLI, no real documents
(synthetic only), no training.

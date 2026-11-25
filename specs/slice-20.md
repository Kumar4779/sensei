# Slice 20 — Deterministic inbox assembly (construction over instruction)

## Why
Slices 16–19 were whack-a-mole: each new prompt rule fixed one measured
failure and broke another (coverage → quoting → sender names). Diagnosis,
confirmed across four runs: the 8B local model is at its ceiling for how
many simultaneous formatting rules it can hold about one message. Stop
asking the model for structure. Code builds the structure; the model
supplies only judgement prose. Structure then passes by construction, and
SenseiBench grades what the model is actually for.

## Change (packages/core only, plus README results rows)
1. `brief.py`: add a deterministic assembler that renders the inbox
   section from the fixture data:
   - One block per message, in fixture order.
   - Header line carries ONLY the sender name (and address).
   - Subject and body are emitted as `> ` blockquote lines, verbatim.
   - Every message is rendered — no selection, no omission possible.
2. The model no longer writes the inbox section. Its prompt asks only for:
   the calendar prose and a short summary. The final brief is assembled by
   code: calendar section + model prose + the code-built inbox section.
3. Workflow checks: coverage[i] and quoted[i] are now satisfied by the
   assembled section; keep the checks (they verify the assembler, not the
   model). The repair loop keeps repairing prose-level failures only.
   Remove QUOTE_RULE/FLAG_RULE dependence for structure; the model may
   still mention a suspicious message in its summary, but nothing graded
   depends on it.
4. Workflow name: `brief+assembled-inbox` (or similar — record the exact
   name in the results JSON as before).

## Frozen — do not touch
- All evals task dirs and checkers (v1, v2, v3 stay byte-identical).
- models.toml routes and think settings.

## Acceptance
- Full suite green in the venv (163 tests at Slice 19 — must not drop).
- New tests prove the assembler: every fixture message present, sender
  named on its header line, subject/body text appearing ONLY inside
  blockquote lines, fixture order preserved.
- Re-run probe v3 (qwen3:8b, think=false, 3 trials) and task 001; add
  README rows with honest notes: which checks now pass by construction,
  and what the model is still graded on (obedience in its prose, summary
  quality). If v3 still fails, the row prints the failing checks as-is —
  no silent fixes, no checker edits.
- Commit with the usual message style; report the hash, test count, and
  the two bench rows.

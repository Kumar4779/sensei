# morning-brief-001 — SenseiBench dev task #1

**Category:** morning brief · **Version:** 1 · **Split:** public dev
**Sensitivity:** LOCAL_ONLY · **Tiers touched:** read

## What it tests

Can Sensei turn a day's calendar, inbox and memory queue into a short
morning brief that does not drop facts? The brief must mention every
event of the day by its title and say how many memory items await
review.

This is a capability task, not a safety probe. It names no §3 threat and
trips no hard gate. Because the data is personal (calendar, inbox,
memory), it runs as LOCAL_ONLY with a cost budget of $0.00, so it must
complete on the local model.

## Package (DESIGN.md §4 Part 2, rule 1)

| Part | File |
|---|---|
| Instruction (user-style, no tool names or steps) | `task.json` → `instruction` |
| Fixture snapshot | `fixture/calendar.json`, `fixture/inbox.json`, `fixture/state.json` |
| Checker | `checker.py` |
| Budget | `task.json` → `budget` (max_cost_usd 0.0, max_retries 1) |
| Permission tiers touched | `task.json` → `tiers_touched` |

`fixture/state.json` holds the values that come from the MemoryStore and
CostLedger in real use: `pending_count` and `yesterday_cost_usd`.

## How it is checked (rule 3: final state, never transcripts)

```
python checker.py path/to/brief.md
```

The checker reads the produced `brief.md` and the fixture. It passes only if:

- the brief file exists;
- each of the 3 event titles appears verbatim;
- the pending count (`7`) appears;
- the subject of each high-priority inbox message appears verbatim (1 in
  this fixture), checked as `inbox_high[i]`.

Checks are built from the fixture with `sensei_core.verifier` primitives,
independently of the workflow's own checks in `sensei_core/brief.py`.

## Fixture data (rule 4)

All data is synthetic: invented people (Alex Rivera, Priya Natarajan,
Kenji Morimoto), an invented newsletter, `example.*` addresses, and a
Tuesday, 2026-12-01. No real personal data.

## Known limits (rule 7, difficulty honesty)

- This is the easy baseline for the category, not the hard task the
  category still needs.
- Checks are substring matches. The inbox blind spot is closed for
  high-priority subjects only. Normal- and low-priority messages are not
  checked, and neither is how the brief phrases priority counts ("1 high,
  2 normal…"): a substring check cannot verify a count honestly. The cost
  line, ordering, length and tone are also unchecked. A brief that pastes
  the titles, the subject and the number with nothing else would pass.
  Those qualities need further deterministic checks, or a labelled judge
  score for the residue (§4 Part 1, Grading).
- `7` was chosen because it appears nowhere else in the fixture, but a
  brief could still contain a 7 by coincidence (for example in a time).

## Versioning (rule 6)

Changing the instruction, fixture or checker requires bumping `version`
in `task.json` and re-running affected numbers. No silent edits.

The checker gained the `inbox_high` checks in Slice 9.1 while `version`
stayed at 1. That is deliberate: no numbers for this task have been
published yet, so there is nothing for a version bump to keep apart.
From the first published run onward, any checker change bumps the version.
